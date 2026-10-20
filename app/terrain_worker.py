"""Milestone "Persistent Terrain Cache + Background Precomputation" -
jednoduchý in-process background worker (zadání bod 9 - "NEPŘIDÁVEJ
Redis/Celery/RabbitMQ, to je pro tenhle projekt zbytečné").

Architektura: jedna `queue.Queue` + N pevných worker vláken
(`TERRAIN_WORKER_CONCURRENCY`, výchozí 1 - "DEM provider může být
citlivý na paralelní requesty", zadání bod 9). Vlákna běží po celou
dobu života procesu (démoni - neblokují shutdown), zpracovávají
kandidáty JEDEN PO DRUHÉM (respektuje concurrency limit), ukládají
výsledek/chybu do `app/terrain_cache.py`, pokračují po jednotlivém
selhání (zadání bod 9 - "continue po failure")."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass

from app import terrain_cache
from app.analyze_location import compute_static_terrain

logger = logging.getLogger(__name__)

TERRAIN_WORKER_CONCURRENCY = 1  # bezpečná výchozí hodnota - nezahltit ČÚZK/Copernicus


@dataclass
class PrecomputeTask:
    candidate_id: str
    lat: float
    lon: float
    cache_key: str
    region: str | None = None
    candidate_type: str | None = None


class TerrainWorker:
    """Jeden worker = jedna fronta + N vláken. Deduplikace (zadání bod
    19 - "duplicate jobs"): `_enqueued` set drží (candidate_id,
    cache_key) párů, které už čekají/běží - opakovaný `enqueue()` pro
    stejný pár je no-op, dokud se nezpracuje (úspěšně nebo s chybou)."""

    def __init__(self, concurrency: int = TERRAIN_WORKER_CONCURRENCY, db_path=terrain_cache.DEFAULT_DB_PATH):
        self.concurrency = max(1, concurrency)
        self.db_path = db_path
        self._queue: queue.Queue[PrecomputeTask] = queue.Queue()
        self._enqueued: set[tuple[str, str]] = set()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        for i in range(self.concurrency):
            t = threading.Thread(target=self._run, name=f"terrain-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def enqueue(self, candidate_id: str, lat: float, lon: float, cache_key: str, region: str | None = None, candidate_type: str | None = None) -> bool:
        """Vrátí True, pokud byl úkol skutečně přidán do fronty (nebyl
        už frontě/zpracování). Volající (viz app/main.py) tohle číslo
        používá k reportování 'enqueued_count' - skutečné, ne odhadnuté."""
        self.start()
        key = (candidate_id, cache_key)
        with self._lock:
            if key in self._enqueued:
                return False
            self._enqueued.add(key)
        terrain_cache.mark_running(candidate_id, cache_key, lat, lon, db_path=self.db_path)
        self._queue.put(PrecomputeTask(candidate_id, lat, lon, cache_key, region, candidate_type))
        return True

    def is_pending(self, candidate_id: str, cache_key: str) -> bool:
        with self._lock:
            return (candidate_id, cache_key) in self._enqueued

    def queue_size(self) -> int:
        return self._queue.qsize()

    def _run(self) -> None:
        while True:
            task = self._queue.get()
            try:
                self._process(task)
            except Exception:  # noqa: BLE001 - jedno selhání workeru nesmí ukončit vlákno navěky (zadání bod 9)
                logger.exception("Terrain worker: neočekávaná chyba u %s", task.candidate_id)
            finally:
                with self._lock:
                    self._enqueued.discard((task.candidate_id, task.cache_key))
                self._queue.task_done()

    def _process(self, task: PrecomputeTask) -> None:
        t0 = time.time()
        try:
            static = compute_static_terrain(task.lat, task.lon)
        except Exception as exc:  # noqa: BLE001 - viz app/candidate_batch.py analyze_candidates() - stejný vzor
            logger.warning("Terrain precompute selhal pro %s (%s): %s", task.candidate_id, (task.lat, task.lon), exc)
            terrain_cache.save_failed(task.candidate_id, task.cache_key, task.lat, task.lon, str(exc), db_path=self.db_path)
            return
        duration = time.time() - t0
        terrain_cache.save_ready(
            task.candidate_id, task.cache_key, task.lat, task.lon, static, duration,
            db_path=self.db_path, region=task.region, candidate_type=task.candidate_type,
        )
        logger.info("Terrain precompute hotovo pro %s za %.1fs", task.candidate_id, duration)


# Modulový singleton - jeden worker pro celý proces (zadání bod 8/9 -
# "musí ukládat výsledky, pokračovat po failure, neblokovat request").
_default_worker: TerrainWorker | None = None


def get_worker() -> TerrainWorker:
    global _default_worker
    if _default_worker is None:
        _default_worker = TerrainWorker()
    return _default_worker
