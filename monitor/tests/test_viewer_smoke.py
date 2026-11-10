"""Viewer smoke test: builds the window headless, replays a run, pauses, rewinds, renders."""

import os
import re

import pytest

pytest.importorskip("pyqtgraph")
pytestmark = pytest.mark.gui


def test_viewer_replays_pauses_rewinds_and_renders(synthetic_run, tmp_path, monkeypatch, capsys):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("PRINTMON_SETTINGS", str(tmp_path / "settings.ini"))
    data, _ = synthetic_run
    from printmon import viewer

    shot = tmp_path / "shot.png"
    viewer.main(["--sim", "--data", str(data), "--speed", "1", "--no-3d",
                 "--pause-after", "2.2", "--rewind", "1.0",
                 "--quit-after", "3.2", "--screenshot", str(shot)])
    for tab in ("monitor", "bead_layers"):
        out = tmp_path / "shot_{}.png".format(tab)
        assert out.exists() and out.stat().st_size > 5_000
    out = capsys.readouterr().out
    assert "paused True" in out
    counts = {k: int(v) for k, v in re.findall(r"(\w+) (\d+)", out.split("records:")[1])}
    for key in ("set_speed", "feed", "C", "P0", "water_flow", "scan"):
        assert counts[key] > 0, key  # every stream, including NC-derived signals, populated
