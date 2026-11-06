import os

import pytest

from bundlehunter.overlay import axiom_url, pick_candidate, pnl_pct, short, stars

SCALP = {"watching": [
    {"mint": "AAAAbbbbcccc", "grade": "B", "smart": ["x"], "age_s": 5},
    {"mint": "BBBBccccdddd", "grade": "A", "smart": ["x", "y"], "age_s": 9},
    {"mint": "CCCCddddeeee", "grade": "A", "smart": ["x"], "age_s": 1, "in_position": True},
    {"mint": "DDDDeeeeffff", "grade": "D", "smart": ["x", "y", "z"], "age_s": 1},
]}


def test_helpers():
    assert axiom_url("M") == "https://axiom.trade/meme/M?chain=sol"
    assert short("AAAAbbbbcccc") == "AAAA…cccc" and stars(2) == "★★☆☆☆" and stars(9) == "★★★★★"
    assert pnl_pct(1.0, 1.2) == pytest.approx(0.2) and pnl_pct(None, 1) is None


def test_card_picks_best_not_held_not_dismissed():
    assert pick_candidate(SCALP, set())["mint"] == "BBBBccccdddd"
    assert pick_candidate(SCALP, {"BBBBccccdddd"})["mint"] == "AAAAbbbbcccc"
    assert pick_candidate(SCALP, {"BBBBccccdddd", "AAAAbbbbcccc"}) is None  # D is never offered
    assert pick_candidate(None, set()) is None


def test_window_builds_and_renders_status():
    pytest.importorskip("PySide6.QtWidgets")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from bundlehunter.overlay import Overlay

    app = QApplication.instance() or QApplication([])
    w = Overlay("http://127.0.0.1:1", None)
    status = {"mode": "paper", "paper": {"balance_sol": 1.5}, "scalp": {
        **SCALP, "today_pnl_sol": 0.01, "state": "працює",
        "positions": [{"mint": "CCCCddddeeee", "entry": 1.0, "price": 1.1}],
        "skipped": [{"ts": 0, "mint": "EEEE", "why": "час минув"}]}}
    w.on_status(status, "")
    assert w.c_grade.text() == "A" and w.field.rowCount() == 4 and w.stream.rowCount() == 1
    assert "1.500" in w.money.text()
    w.skip_card()
    assert "BBBBccccdddd" in w.dismissed
    w.on_status({}, "Connection refused")
    assert "bh serve" in w.err.text()
    w.close()
    app.processEvents()
