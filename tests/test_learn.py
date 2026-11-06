import json
import random

import pytest

from bundlehunter.config import ScalpConfig
from bundlehunter.learn import (Rule, format_result, learn, live_rules, load_dir, map_columns, parse_duration,
                                parse_number, save, load_learned)
from bundlehunter.scalp import ScalpStrategy, grade
from test_scalp import PANEL, TRADERS, M, crowd, ev


def test_numbers_and_durations():
    assert parse_number("$12.3K") == pytest.approx(12300)
    assert parse_number("45%") == 45 and parse_number("3x") == 3 and parse_number("-0,5") == -0.5
    assert parse_number("1,234.5") == pytest.approx(1234.5) and parse_number("abc") is None
    assert parse_duration("2h 3m") == 7380 and parse_duration("40s") == 40 and parse_duration("90") == 90


def test_columns_any_language():
    cols = map_columns(["Токен", "Дата", "PnL %", "Market Cap", "Холдеры", "Bundlers %", "Hold time", "Peak X"])
    assert cols["pnl_pct"] == "PnL %" and cols["mcap"] == "Market Cap" and cols["holders"] == "Холдеры"
    assert cols["bundlers_pct"] == "Bundlers %" and cols["hold_s"] == "Hold time" and cols["peak_x"] == "Peak X"
    assert cols["_mint"] == "Токен" and cols["_time"] == "Дата"


def _write_trades(path, n, edge):
    rng = random.Random(7)
    rows = ["mint,time,pnl %,bundlers %,holders,peak x"]
    for i in range(n):
        b = rng.uniform(0, 60)
        good = b < 25
        pnl = rng.gauss(18, 15) if (good and edge) else rng.gauss(-12, 15)
        peak = max(1.0, 1 + max(pnl, 0) / 100 + rng.uniform(0, 0.3))
        rows.append(f"T{i},{1_700_000_000 + i * 60},{pnl:.1f},{b:.1f}%,{rng.randint(50, 900)},{peak:.2f}x")
    path.write_text("\n".join(rows), encoding="utf-8")


def test_learns_a_real_edge_and_checks_it_on_unseen_trades(tmp_path):
    _write_trades(tmp_path / "trades.csv", 300, edge=True)
    (tmp_path / "wallets.json").write_text(json.dumps([{"address": "9" * 44, "name": "x"}]))
    ds = load_dir(tmp_path)
    assert len(ds.samples) == 300
    assert "гаманців" in ds.files[str(tmp_path / "wallets.json")]
    r = learn(ds)
    assert r["promoted"], r["why"]
    first = r["rules"][0]
    assert first["feature"] == "bundlers_pct" and first["op"] == "<=" and first["value"] <= 0.3 and first["live"]
    assert r["test"]["avg"] > r["baseline_test"]["avg"]
    text = format_result(r)
    assert "ПРИЙНЯТО" in text and "бандлери ≤" in text


def test_no_edge_is_not_promoted(tmp_path):
    _write_trades(tmp_path / "t.csv", 300, edge=False)
    r = learn(load_dir(tmp_path))
    assert not r["promoted"] and live_rules(r) == []


def test_too_few_trades(tmp_path):
    _write_trades(tmp_path / "t.csv", 10, edge=True)
    r = learn(load_dir(tmp_path))
    assert not r["promoted"] and "замало" in r["why"]


def test_save_and_live_rules_only_from_promoted(tmp_path):
    r = {"promoted": True, "rules": [{"feature": "bundlers_pct", "op": "<=", "value": 0.2, "live": True},
                                     {"feature": "holders", "op": ">=", "value": 100, "live": False}]}
    save(r, str(tmp_path / "l.json"))
    rules = live_rules(load_learned(str(tmp_path / "l.json")))
    assert [x.feature for x in rules] == ["bundlers_pct"]


def test_scalper_obeys_learned_rule():
    s = ScalpStrategy(ScalpConfig(buy_sol=0.1, exit_mode="fixed"), TRADERS)
    s.learned = [Rule("bundlers_pct", "<=", 0.05)]  # PANEL has 10% bundlers
    s.on_event(ev("buy", "strong", 1e-6, 0))
    s.set_check(M, True, metrics=PANEL)
    assert crowd(s, 1, 1e-6, n=7) == []
    assert "вивчене правило" in s.watches[M].block_reason
    st = s.status(10)
    assert st["watching"][0]["grade"] == "D" and st["learned"] == ["бандлери ≤ 5%"]


def test_grades():
    assert grade("", True, 2) == "A"
    assert grade("немає підтвердження: купівлі 1 SOL", True, 1) == "B"
    assert grade("чекаю перевірки токена", None, 1) == "C"
    assert grade("вже +40% від входу сильних: не доганяю", True, 2) == "D"
    assert grade("", False, 2) == "E"
