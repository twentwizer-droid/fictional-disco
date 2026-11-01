import asyncio

import pytest
from factory import trade
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.token.associated import get_associated_token_address

from bundlehunter.config import ClusterConfig
from bundlehunter.holders import HolderScanner, classify, db_holdings, format_panel
from bundlehunter.models import ParsedTx
from bundlehunter.storage import Store

MINT = "M"


def _data():
    roles = {"dev": "creator", "b1": "bundle", "b2": "bundle", "s1": "sniper", "s2": "sniper"}
    trades = [
        trade("t0", MINT, "dev", "buy", 1.0, 30e6, 100, 0),
        trade("t1", MINT, "b1", "buy", 1.0, 25e6, 100, 0),
        trade("t2", MINT, "b2", "buy", 1.0, 20e6, 100, 0),
        trade("t3", MINT, "s1", "buy", 0.5, 10e6, 101, 1),
        trade("t4", MINT, "s2", "buy", 0.5, 8e6, 102, 1),
        trade("t5", MINT, "late", "buy", 0.5, 5e6, 900, 60),
        trade("t6", MINT, "b2", "sell", 0.9, 20e6, 950, 90),
        trade("t7", MINT, "dev", "sell", 0.9, 30e6, 960, 95),
    ]
    # b1 spreads 10M over fresh wallets d1 -> d2 (distribution chain)
    transfers = [("b1", "d1", 6e6, 300), ("b1", "d2", 4e6, 301), ("d1", "d3", 1e6, 302)]
    funders = {"dev": "mother", "s1": "dev", "s2": "cex", "late": "cex", "b1": "cex", "d2": "cex"}
    return roles, trades, transfers, funders


def test_classify_dev_bundlers_snipers_insiders():
    roles, trades, transfers, funders = _data()
    g = classify(roles, trades, transfers, funders, hubs={"cex"}, wallet_to_cluster={}, window_end_slot=110)
    assert g["dev"].wallets == {"dev"}
    assert g["bundlers"].wallets == {"b1", "b2"} and g["snipers"].wallets == {"s1", "s2"}
    # s1 funded by the dev; d1/d2/d3 got tokens from a bundler; s2 and late share only a CEX
    assert g["insiders"].wallets == {"s1", "d1", "d2", "d3"}
    assert g["bundlers"].initial == 45e6 and g["snipers"].initial == 18e6 and g["dev"].initial == 30e6
    assert g["insiders"].initial == 10e6 + 6e6 + 4e6 + 1e6
    assert g["linked"].wallets == {"dev", "b1", "b2", "s1", "d1", "d2", "d3"}
    assert g["linked"].initial == 30e6 + 45e6 + 10e6  # internal transfers not double counted


def test_classify_uses_dev_cluster_and_dev_funder():
    roles, trades, transfers, funders = _data()
    funders["late"] = "mother"  # same mother wallet as the dev
    g = classify(roles, trades, [], funders, {"cex"}, {"dev": "C1", "s2": "C1"})
    assert {"s1", "s2", "late"} <= g["insiders"].wallets


def test_db_holdings_net_of_sells_and_transfers():
    roles, trades, transfers, _ = _data()
    h = db_holdings(trades, transfers)
    assert (h["dev"], h["b1"], h["b2"], h["d1"], h["d3"]) == (0, 15e6, 0, 5e6, 1e6)


class FakeRpc:
    def __init__(self, mint, balances, largest, owners):
        self.mint, self.balances, self.largest, self.owners = mint, balances, largest, owners

    async def get_parsed_account(self, mint):
        return {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                "data": {"parsed": {"info": {"supply": str(10**15), "decimals": 6}}}}

    async def get_multiple_accounts(self, addresses):
        out = []
        for a in addresses:
            if a in self.balances:
                out.append({"data": {"parsed": {"info": {"tokenAmount": {"uiAmountString": str(self.balances[a])},
                                                          "owner": self.owners.get(a, "")}}}})
            elif a in self.owners:
                out.append({"data": {"parsed": {"info": {"owner": self.owners[a]}}}})
            else:
                out.append(None)
        return out

    async def get_token_largest_accounts(self, mint):
        return [{"address": a, "uiAmountString": str(v)} for a, v in self.largest]


def test_scanner_live_panel_with_rpc_balances_and_top10():
    dev, b1, s1, whale = (str(Keypair().pubkey()) for _ in range(4))
    mint = str(Keypair().pubkey())
    curve, _ = Pubkey.find_program_address([b"bonding-curve", bytes(Pubkey.from_string(mint))],
                                           Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"))
    ata = {w: str(get_associated_token_address(Pubkey.from_string(w), Pubkey.from_string(mint))) for w in (dev, b1, s1, whale)}
    store = Store(":memory:")
    store.upsert_token(mint, creator=dev, create_slot=100, status="ok", supply=1e9, symbol="TST")
    store.replace_roles(mint, [(dev, "creator", 100, True), (b1, "bundle", 100, True), (s1, "sniper", 101, False)])
    store.add_parsed([ParsedTx("x", 0, None, False, "", [], trades=[
        trade("a", mint, dev, "buy", 1, 50e6, 100, 0), trade("b", mint, b1, "buy", 1, 40e6, 100, 0),
        trade("c", mint, s1, "buy", 1, 20e6, 101, 1)])])
    store.set_wallet(s1, funder=dev, history_complete=1, tx_count=2)
    store.set_wallet(dev, funder="mom", history_complete=1, tx_count=2)
    store.set_wallet(b1, funder="mom2", history_complete=1, tx_count=2)
    balances = {ata[dev]: 0.0, ata[b1]: 40e6, ata[s1]: 15e6, ata[whale]: 90e6, "curveAta": 700e6}
    largest = [("curveAta", 700e6), (ata[whale], 90e6), (ata[b1], 40e6), (ata[s1], 15e6)]
    owners = {"curveAta": str(curve), ata[whale]: whale, ata[b1]: b1, ata[s1]: s1}
    scanner = HolderScanner(FakeRpc(mint, balances, largest, owners), store, analyzer=None, cluster_cfg=ClusterConfig())

    async def go():
        p = await scanner.panel(mint, {}, live=True, analyze=False)
        return p

    scanner.analyzer = type("A", (), {"quick_funders": staticmethod(lambda ws: asyncio.sleep(0, {}))})()
    p = asyncio.run(go())
    m = p.metrics()
    assert p.source == "rpc"
    assert m["top10_pct"] == pytest.approx(0.145)  # bonding curve PDA excluded
    assert m["dev_pct"] == 0 and m["bundlers_pct"] == pytest.approx(0.04)
    assert m["snipers_pct"] == pytest.approx(0.015) and m["insiders_pct"] == pytest.approx(0.015)  # s1 funded by dev
    assert m["linked_pct"] == pytest.approx(0.055)
    text = format_panel(p)
    assert "Top 10 H." in text and "Insiders" in text and "Bundlers" in text and "TST" in text
    assert "insiders 1.5%" in format_panel(p, compact=True)
