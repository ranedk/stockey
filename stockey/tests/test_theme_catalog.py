import pytest

from fundamentals.themes import catalog


def test_v1_has_the_26_themes_the_operator_kept():
    themes = catalog.load(1)
    assert len(themes) == 26
    assert len({t.area for t in themes}) == 5
    assert {t.id for t in themes} >= {"solar_cells", "data_centres", "defence_electronics", "cdmo"}


def test_young_themes_use_the_lower_exposure_bar():
    by_id = {t.id: t for t in catalog.load(1)}
    for young in ("green_hydrogen", "drones", "semiconductors", "data_centres", "battery_storage"):
        assert by_id[young].min_exposure == 10
    assert by_id["solar_modules"].min_exposure == 20


def test_a_malformed_theme_fails_loudly(tmp_path, monkeypatch):
    (tmp_path / "themes_v9.yaml").write_text(
        "version: 9\nthemes:\n  - {id: x, name: X, area: a, layer: sideways, definition: d,"
        " include: [k], min_exposure: 10, capacity: [c], policy: [p]}\n")
    monkeypatch.setattr(catalog, "HERE", tmp_path)
    with pytest.raises(ValueError, match="unknown layer"):
        catalog.load(9)
