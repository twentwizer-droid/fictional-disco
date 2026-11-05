import asyncio
import base64

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bundlehunter.config import Config, ScalpConfig
from bundlehunter.scalp import ScalpStrategy
from bundlehunter.scalp_lab import evaluate, summarize
from bundlehunter.storage import Store


def record_token(store, mint, t0, peak, n_crowd=6):
    """A recorded token: a strong trader buys, a varied crowd confirms, price goes to `peak`, then dumps."""
    rows = [(mint, t0, "buy", "strong", 1.0, 1e6, 1e-6, "Ansem", 85.0)]
    for i in range(n_crowd):
        size = 0.8 * (0.6 + 0.15 * (i % 5))
        rows.append((mint, t0 + 1 + i, "buy", f"r{mint}{i}", size, size / 1e-6, 1e-6 * (1 + 0.004 * i), None, None))
    rows.append((mint, t0 + 20, "buy", "late", 0.3, 1.0, peak, None, None))
    rows += [(mint, t0 + 30 + i, "sell", f"d{mint}{i}", 1.0, 1.0, peak * 0.7, None, None) for i in range(6)]
    store.record_scalp_events(rows)
    store.record_scalp_check(mint, True, "", {"bundlers_pct": 0.1, "insiders_pct": 0.05, "larp_score": 10})


def test_evaluate_validates_a_consistent_edge(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = Store(":memory:")
    for i in range(40):  # every token runs to +80%: wins spread over many tokens
        record_token(store, f"T{i:02d}", 1000 + i * 10_000, 1.8e-6)
    cfg = Config()
    r = evaluate(store, cfg)
    assert r["tokens"] == {"total": 40, "choose_on": 28, "judge_on": 12}
    assert r["validated"], r["reasons"]
    assert r["unseen"]["trades"] >= 8 and r["unseen"]["avg_ret"] > 0.01 and r["unseen"]["top_token_share"] <= 0.2
    assert (tmp_path / "data" / "scalp_validated.json").exists()


def test_evaluate_refuses_one_lucky_token_and_losers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = Store(":memory:")
    for i in range(30):  # losers everywhere...
        record_token(store, f"L{i:02d}", 1000 + i * 10_000, 0.95e-6)
    record_token(store, "LUCKY", 1000 + 31 * 10_000, 9e-6)  # ...and one 9x in the unseen part
    r = evaluate(store, Config())
    assert not r["validated"]
    assert any("медіанна" in x or "середня" in x or "одного токена" in x for x in r["reasons"])


def test_summarize_token_share():
    s = summarize([{"mint": "A", "pnl_sol": 0.8, "ret": 0.8}, {"mint": "B", "pnl_sol": 0.2, "ret": 0.2}])
    assert s["top_token_share"] == pytest.approx(0.8) and s["median_ret"] == pytest.approx(0.5)


def test_gate_blocks_live_without_validation_but_not_paper():
    s = ScalpStrategy(ScalpConfig())
    assert s.gate_block(live=False, now=0) == ""
    assert "немає перевіреної" in s.gate_block(live=True, now=0)
    s.validated = {"validated": True, "ts": 0}
    assert s.gate_block(live=True, now=100) == ""
    assert "застаріла" in s.gate_block(live=True, now=10 * 86400)
    assert "немає перевіреної" in ScalpStrategy(ScalpConfig(gate="always")).gate_block(live=False, now=0)


def test_dashboard_paper_and_eval_buttons(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from test_service_ui import _service

    from bundlehunter.web import create_app

    svc = _service()
    auth = {"Authorization": "Basic " + base64.b64encode(b"me:pw").decode()}
    act = {**auth, "X-BH-Action": "1"}

    async def go():
        async with TestClient(TestServer(create_app(svc, password="pw"))) as c:
            assert (await c.post("/api/paper", json={"action": "deposit", "amount": 1}, headers=auth)).status == 403
            r = await c.post("/api/paper", json={"action": "deposit", "amount": 1.5}, headers=act)
            assert (await r.json())["balance_sol"] == pytest.approx(2.5)
            r = await c.post("/api/paper", json={"action": "reset", "amount": -1}, headers=act)
            assert r.status == 400
            r = await c.post("/api/scalp/eval", headers=act)
            assert r.status == 200 and "НЕ ПЕРЕВІРЕНО" in (await r.json())["text"]

    asyncio.run(go())
