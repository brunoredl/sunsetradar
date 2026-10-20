"""Testy pro app/national_grid.py (milestone "Data Foundation" -
celostátní katalog, zadání bod 5 - deterministická mřížka)."""

from __future__ import annotations

from app.national_grid import generate_grid_cells


def test_generate_grid_cells_covers_whole_bbox_without_gaps_or_overlap():
    bbox = (48.5, 12.0, 51.1, 19.0)
    cells = generate_grid_cells(bbox, cell_size_deg=1.0)
    # každý bod uvnitř bbox musí padnout do PRÁVĚ JEDNÉ dlaždice
    south, west, north, east = bbox
    for lat in (48.5, 49.0, 50.5, 51.09):
        for lon in (12.0, 13.5, 18.9):
            matches = [c for c in cells if c.bbox[0] <= lat < c.bbox[2] and c.bbox[1] <= lon < c.bbox[3]]
            assert len(matches) == 1, (lat, lon, matches)


def test_generate_grid_cells_deterministic_keys_and_order():
    cells1 = generate_grid_cells()
    cells2 = generate_grid_cells()
    assert [c.key for c in cells1] == [c.key for c in cells2]
    assert len(set(c.key for c in cells1)) == len(cells1)  # unikátní klíče


def test_generate_grid_cells_last_row_and_column_never_exceed_bbox():
    bbox = (48.5, 12.0, 51.1, 19.0)
    cells = generate_grid_cells(bbox, cell_size_deg=1.0)
    for c in cells:
        south, west, north, east = c.bbox
        assert south >= bbox[0] and west >= bbox[1] and north <= bbox[2] and east <= bbox[3]


def test_generate_grid_cells_small_bbox_gives_single_cell():
    cells = generate_grid_cells((49.0, 18.0, 49.5, 18.5), cell_size_deg=1.0)
    assert len(cells) == 1
    assert cells[0].bbox == (49.0, 18.0, 49.5, 18.5)
