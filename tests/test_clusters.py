from factory import add_launch

from bundlehunter.clusters import UnionFind, build_clusters
from bundlehunter.config import ClusterConfig, SelectionConfig
from bundlehunter.storage import Store

T0 = 1_700_000_000


def _store() -> Store:
    s = Store(":memory:")
    # Cluster X: fresh dev wallets funded by one mother wallet, reused bundle wallets b1/b2. Profitable.
    for i in range(3):
        add_launch(s, f"X{i}", f"devX{i}", ["b1", "b2"], T0 + i * 3600, 1000 + i * 10_000, 1.0, 3.0,
                   snipers=["sniperbot"])
        s.set_wallet(f"devX{i}", funder="motherX", history_complete=1, tx_count=3)
    # Cluster Y: losing bundler, same pattern.
    for i in range(3):
        add_launch(s, f"Y{i}", f"devY{i}", ["y1", "y2"], T0 + 500 + i * 3600, 2000 + i * 10_000, 1.0, 0.4)
        s.set_wallet(f"devY{i}", funder="motherY", history_complete=1, tx_count=3)
    # A sniper bot that lands in the create slot of everybody's launches.
    for i in range(6):
        add_launch(s, f"R{i}", f"rnd{i}", ["fastbot"], T0 + 9000 + i, 50_000 + i * 10, 0.5, 0.6)
    for mint in ["X0", "X1", "X2", "Y0", "Y1", "Y2"]:
        rows = [(w, role, 0, True) for m, w, role in s.roles([mint])] + [("fastbot", "bundle", 0, True)]
        s.replace_roles(mint, rows)
    # A CEX hot wallet that funds a lot of unrelated wallets must not merge them.
    for i in range(50):
        s.set_wallet(f"retail{i}", funder="cexHot", history_complete=1, tx_count=1)
    s.set_wallet("b1", funder="cexHot", history_complete=1, tx_count=9)
    s.set_wallet("y1", funder="cexHot", history_complete=1, tx_count=9)
    s.set_wallet("devX9", funder="motherX", history_complete=1, tx_count=1)  # funded, not launched yet
    return s


def test_union_find():
    uf = UnionFind()
    uf.union("a", "b")
    uf.union("c", "b")
    assert uf.find("a") == uf.find("c") and uf.find("d") == "d"


def test_clusters_link_funders_and_co_bundlers_but_not_hubs_or_snipers():
    idx = build_clusters(_store(), ClusterConfig())
    w2c = idx.wallet_to_cluster
    x = w2c["devX0"]
    assert w2c["devX1"] == w2c["devX2"] == w2c["motherX"] == w2c["b1"] == w2c["b2"] == x
    assert w2c["devY0"] == w2c["y1"] != x
    assert w2c["fastbot"] not in (x, w2c["y1"])  # sniper bot: low overlap with every group
    assert "cexHot" not in w2c and "retail0" not in w2c  # hub funder ignored
    assert w2c["devX9"] == x  # next dev wallet is known before it launches


def test_cluster_stats_and_selection():
    idx = build_clusters(_store(), ClusterConfig())
    x = idx.clusters[idx.wallet_to_cluster["devX0"]]
    y = idx.clusters[idx.wallet_to_cluster["devY0"]]
    assert len(x.launches) == 3 and x.created == 3 and len(x.closed) == 3
    # each launch: 3 wallets x (3.0 - 1.0) SOL
    assert abs(x.realized_pnl - 18.0) < 1e-9 and x.win_rate == 1.0
    assert x.median_first_sell_s == 120
    assert x.partners[idx.wallet_to_cluster["fastbot"]] == 3  # WITH WHOM
    assert sum(x.active_hours.values()) == 3  # WHEN
    assert y.realized_pnl < 0
    black = idx.black(SelectionConfig())
    assert [c.cluster_id for c in black] == [x.cluster_id]
    assert y.black_reasons(SelectionConfig())
    d = x.to_dict(SelectionConfig())
    assert d["in_the_black"] is True and len(d["tokens"]) == 3


def test_fast_dumpers_are_not_copied():
    s = Store(":memory:")
    for i in range(3):
        add_launch(s, f"F{i}", "fastdev", ["fb"], T0 + i * 100, 10 + i * 1000, 1.0, 2.0, sell_after=3)
    idx = build_clusters(s, ClusterConfig())
    c = idx.clusters[idx.wallet_to_cluster["fastdev"]]
    assert c.realized_pnl > 0
    assert any("too fast" in r for r in c.black_reasons(SelectionConfig()))
