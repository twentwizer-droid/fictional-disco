import asyncio

from factory import SOL, make_tx, pump_buy_tx, sys_transfer

from bundlehunter.analyzer import Analyzer
from bundlehunter.config import AnalyzerConfig
from bundlehunter.storage import Store

MINT = "MintAAA"


class FakeRpc:
    def __init__(self, txs, sigs_by_address):
        self.txs = txs
        self.sigs = sigs_by_address

    async def signatures(self, address, max_pages=1, until=None, page_size=1000):
        rows = self.sigs.get(address, [])
        return rows[: page_size * max_pages], len(rows) <= page_size * max_pages

    async def get_transaction(self, sig):
        return self.txs.get(sig)

    async def get_token_supply(self, mint):
        return 1e9


def _sig(sig, slot):
    return {"signature": sig, "slot": slot, "err": None}


def _world():
    txs = {
        "create": pump_buy_tx("create", 100, "Dev", MINT, 1.0, 30_000_000, tip_sol=0.002, create=True),
        "bundle1": pump_buy_tx("bundle1", 100, "B1", MINT, 0.8, 20_000_000),
        "bundle2": pump_buy_tx("bundle2", 100, "B2", MINT, 0.8, 18_000_000),
        "snipe": pump_buy_tx("snipe", 101, "Sniper", MINT, 0.5, 9_000_000),
        "late": pump_buy_tx("late", 500, "Retail", MINT, 0.5, 2_000_000),
        "fund_dev": make_tx("fund_dev", 90, [("Mother", True), ("Dev", False)], [10 * SOL, 0], [8 * SOL, 2 * SOL],
                            instructions=[sys_transfer("Mother", "Dev", 2 * SOL)]),
        "dust": make_tx("dust", 95, [("Poisoner", True), ("B1", False)], [SOL, 0], [SOL - 1, 1],
                        instructions=[sys_transfer("Poisoner", "B1", 1)]),
        "fund_b1": make_tx("fund_b1", 96, [("Mother", True), ("B1", False)], [8 * SOL, 0], [7 * SOL, SOL],
                           instructions=[sys_transfer("Mother", "B1", SOL)]),
    }
    sigs = {
        MINT: [_sig("late", 500), _sig("snipe", 101), _sig("bundle2", 100), _sig("bundle1", 100), _sig("create", 100)],
        "Dev": [_sig("create", 100), _sig("fund_dev", 90)],
        "B1": [_sig("bundle1", 100), _sig("fund_b1", 96), _sig("dust", 95)],
        "B2": [_sig("bundle2", 100)],
    }
    return FakeRpc(txs, sigs)


def test_analyze_launch_roles_bundle_and_funders():
    store = Store(":memory:")
    an = Analyzer(_world(), store, AnalyzerConfig(early_slots=3))
    r = asyncio.run(an.analyze_launch(MINT))
    assert r.status == "ok" and r.creator == "Dev" and r.create_slot == 100
    assert sorted(r.bundle_wallets) == ["B1", "B2"] and r.sniper_wallets == ["Sniper"]
    assert abs(r.bundle_sol - 2.6) < 1e-9 and abs(r.bundle_supply_pct - 0.068) < 1e-9
    roles = {w: role for _, w, role in store.roles([MINT])}
    assert roles == {"Dev": "creator", "B1": "bundle", "B2": "bundle", "Sniper": "sniper"}
    assert "Retail" not in roles
    funders = store.funders()
    assert funders["Dev"] == "Mother" and funders["B1"] == "Mother"  # dust transfer ignored
    assert "B2" not in funders
    assert store.token(MINT)["status"] == "ok"


def test_quick_scan_counts_create_slot_txs():
    an = Analyzer(_world(), Store(":memory:"), AnalyzerConfig())
    scan = asyncio.run(an.quick_scan(MINT))
    assert (scan.create_slot, scan.bundle_txs, scan.total_txs, scan.complete) == (100, 3, 5, True)


def test_process_pending_skips_unbundled_launches():
    store = Store(":memory:")
    store.upsert_token(MINT, create_time=1.0)
    an = Analyzer(_world(), store, AnalyzerConfig(min_create_slot_txs=3, min_total_txs=100, min_age_s=0))
    asyncio.run(an.process_pending(10))
    assert store.token(MINT)["status"] == "skipped"
    store.upsert_token(MINT, status="pending")
    an.cfg.min_total_txs = 5
    asyncio.run(an.process_pending(10))
    assert store.token(MINT)["status"] == "ok"


def test_same_slot_group_after_create_counts_as_bundle_and_quick_funder():
    rpc = _world()
    for i in range(3):
        rpc.txs[f"grp{i}"] = pump_buy_tx(f"grp{i}", 102, f"G{i}", MINT, 0.3, 5_000_000)
    rpc.sigs[MINT] = [_sig(f"grp{i}", 102) for i in range(3)] + rpc.sigs[MINT]
    store = Store(":memory:")
    an = Analyzer(rpc, store, AnalyzerConfig(early_slots=3, bundle_min_wallets_per_slot=3))
    r = asyncio.run(an.analyze_launch(MINT, profile=False, final=False))
    assert sorted(r.bundle_wallets) == ["B1", "B2", "G0", "G1", "G2"] and r.sniper_wallets == ["Sniper"]
    assert store.token(MINT)["status"] == "pending"  # final=False leaves it for the full pipeline
    assert asyncio.run(an.quick_funders(["Dev", "B2"])) == {"Dev": "Mother"}
    assert store.wallet("Dev")["full_profile"] == 0
    asyncio.run(an.analyze_launch(MINT))  # full analysis still profiles the dev afterwards
    assert store.wallet("Dev")["full_profile"] == 1
