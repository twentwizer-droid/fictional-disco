import json

from bundlehunter.walletsort import classify, load, merge, write_csv

A = "7Dkpx4CgZNchxz2d3kzrr4Hr6orZ1T3F2frR2UWcHUv6"
B = "kQdJVZvix2BPCz2i46ErUPk2a74Uf37QZL5jsRdAD8y"
C = "3kebnKw7cPdSkLRfiMEALyZJGZ4wdiSRvmoN4rD1yPzV"
D = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"


def _axiom(items):
    return json.dumps([{"trackedWalletAddress": a, "name": n, "emoji": e, "alertsOnToast": t, "alertsOnBubble": False,
                        "alertsOnFeed": False, "groups": ["Main"]} for a, n, e, t in items], ensure_ascii=False)


def test_classify_names():
    cases = {
        "EXP guy 80%hr,1m$ pnl": "good", "1.3M+": "good", "120k 9999": "good", "God Willing +": "good",
        "Ansem": "trader", "CryptoDevinL": "trader", "Rename wallet": "unnamed", "": "unnamed",
        "Yugi Bundle": "bundle", "fartcoin dev 2": "dev", "topdev": "dev", "FIW deployer": "dev",
        "sniper2": "sniper", "Copybot": "sniper", "Insyder": "insider",
        "Mr. Frigger (DONT COPY)": "avoid", "unprofitable": "avoid", "mev scam": "avoid",
        "BINANCE": "exchange", "Bybit": "exchange", "FTX estate": "exchange",
    }
    assert {n: classify(n) for n in cases} == cases
    assert classify("West", "⛔") == "avoid" and classify("whatever", "🏦") == "exchange"


def test_load_utf16_and_utf8_merge_and_csv(tmp_path):
    f1 = tmp_path / "Wallets.txt"
    f1.write_bytes(_axiom([(A, "Rename wallet", "👻", True), (B, "KryptoKing", "🙈", False)]).encode("utf-16"))
    f2 = tmp_path / "WalletTracker.txt"
    f2.write_text(_axiom([(A, "Cupsey", "👻", False), (C, "Yugi Bundle", "✂️", False), (D, "BINANCE", "🏦", False),
                          ("not-an-address", "x", "", False)]), encoding="utf-8")
    f3 = tmp_path / "list.tsv"
    f3.write_text(f"{B}\tKryptoKing\n{C}\tYugi Bundle\n", encoding="utf-8")
    r = merge([load(f1), load(f2), load(f3)])
    assert (r.total_in, len(r.wallets), r.duplicates, r.invalid) == (8, 4, 3, ["not-an-address"])
    by = {w.address: w for w in r.wallets}
    assert by[A].label == "Cupsey" and by[A].alerts and by[A].category == "trader"  # real name beats "Rename wallet"
    assert by[C].category == "bundle" and r.bundle_groups() == {"Yugi": [by[C]]}
    assert [w.category for w in r.wallets] == ["trader", "trader", "bundle", "exchange"]  # sorted by category
    assert {w.address for w in r.to_follow()} == {A, B}
    out = tmp_path / "sorted.csv"
    write_csv(r, out)
    text = out.read_text(encoding="utf-8-sig")
    assert text.splitlines()[0].startswith("category,") and "біржа (CEX)" in text
