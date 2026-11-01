from factory import add_launch, trade

from bundlehunter.backtest import run_backtest
from bundlehunter.config import Config
from bundlehunter.models import ParsedTx
from bundlehunter.storage import Store

T0 = 1_700_000_000


def test_walk_forward_backtest_follows_profitable_cluster():
    s = Store(":memory:")
    for i in range(3):  # training history: cluster is profitable
        add_launch(s, f"X{i}", f"devX{i}", ["b1", "b2"], T0 + i * 3600, i * 10_000, 1.0, 3.0)
        s.set_wallet(f"devX{i}", funder="motherX", history_complete=1, tx_count=3)
    # test launch by the same cluster: new dev funded by the same mother
    mint, t = "X3", T0 + 4 * 3600
    s.set_wallet("devX3", funder="motherX", history_complete=1, tx_count=3)
    s.upsert_token(mint, creator="devX3", create_time=t, create_slot=50_000, status="ok", supply=1e9)
    s.replace_roles(mint, [("devX3", "creator", 50_000, True), ("b1", "bundle", 50_000, True)])
    tokens = 1_000_000
    path = [1e-8, 1.5e-8, 2e-8, 3e-8, 2.5e-8, 2e-8, 2.15e-8, 2.6e-8, 3.2e-8, 3.6e-8, 4.5e-8]
    trades = [
        trade("c", mint, "devX3", "buy", 0.1, 10_000_000, 50_000, t),
        trade("d", mint, "b1", "buy", 0.11, 10_000_000, 50_000, t),
    ]
    trades += [trade(f"r{i}", mint, f"retail{i}", "buy", p * tokens, tokens, 50_001 + i, t + 2 + i) for i, p in enumerate(path)]
    trades.append(trade("dump", mint, "b1", "sell", 0.4, 5_000_000, 50_100, t + 60))
    s.add_parsed([ParsedTx("x", 0, None, False, "", [], trades=trades)])

    res = run_backtest(s, Config(), split=0.75)
    assert (res.train_launches, res.test_launches, res.black_clusters, res.watched) == (3, 1, 1, 1)
    (tr,) = res.trades
    assert "dip" in tr["reason_open"] and tr["cluster_id"] == "C-b1"
    assert tr["reason_close"] == "take profit"  # 2.15e-8 * 1.03 entry, +100% hit at 4.5e-8
    assert tr["pnl_sol"] > 0
