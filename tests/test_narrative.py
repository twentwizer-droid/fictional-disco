import asyncio
import time

import httpx
from factory import add_launch
from solders.keypair import Keypair

from bundlehunter.config import Config
from bundlehunter.holders import Group, TokenPanel
from bundlehunter.larp import Finding, LarpReport, TokenMeta
from bundlehunter.models import MarketEvent
from bundlehunter.narrative import Member, NarrativeAnalyzer, format_family, parse_query
from bundlehunter.storage import Store

NOW = time.time()


def _addr() -> str:
    return str(Keypair().pubkey())


def test_parse_query_kinds():
    q = parse_query("$Trump Cat!")
    assert q.terms == ["trump", "cat"] and not q.twitter
    assert parse_query("https://twitter.com/SomeOne/status/12").twitter == "x.com/someone/status/12"
    assert parse_query("https://www.catcoin.xyz/about").website == "catcoin.xyz"
    mint = _addr()
    assert parse_query(mint).seed_mint == mint


class FixedReport(LarpReport):
    """A LARP report with a preset score."""

    fixed = 0

    @property
    def score(self) -> int:
        return self.fixed


class FakeLarp:
    def __init__(self, scores):
        self.scores = scores

    async def metadata(self, mint, live=True):
        return TokenMeta(symbol="CAT", twitter="https://x.com/catdev")

    async def check(self, mint, panel=None, live=True):
        score, codes = self.scores.get(mint, (0, []))
        report = FixedReport(mint, TokenMeta(), [Finding("yellow", c, c) for c in codes])
        report.fixed = score
        return report


class FakeScanner:
    def __init__(self, held):
        self.held = held

    async def panel(self, mint, w2c, **kw):
        bundlers, insiders, linked_init, linked_held = self.held[mint]
        groups = {g: Group() for g in ("dev", "bundlers", "snipers", "insiders", "linked")}
        groups["bundlers"].held, groups["insiders"].held = bundlers * 1e9, insiders * 1e9
        groups["linked"].initial, groups["linked"].held = linked_init * 1e9, linked_held * 1e9
        return TokenPanel(mint, 1e9, None, None, groups, {}, None, "rpc")


def _world():
    store = Store(":memory:")
    for i in range(3):  # the dev group has made money before
        add_launch(store, f"old{i}", f"devX{i}", ["b1", "b2"], int(NOW) - 90_000 + i * 3600, i * 1000, 1.0, 3.0)
        store.set_wallet(f"devX{i}", funder="MotherX", history_complete=1, tx_count=3)
    og, copy, larp_tok, dex_only = _addr(), _addr(), _addr(), _addr()
    store.set_wallet("devNew", funder="MotherX", history_complete=1, tx_count=2)
    store.upsert_token(og, name="Cat Coin", symbol="CAT", creator="devNew", create_time=NOW - 7200,
                       twitter="x.com/catdev", status="ok")
    store.upsert_token(copy, name="CAT", symbol="CAT", creator="rando", create_time=NOW - 3600, twitter="x.com/catdev")
    store.upsert_token(larp_tok, name="Official Cat", symbol="OCAT", creator="rando2", create_time=NOW - 1800)
    store.upsert_token("unrelated", name="Dog", symbol="DOG", create_time=NOW - 100)
    return store, og, copy, larp_tok, dex_only


def _dex_transport(dex_only):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["q"] == "cat"
        return httpx.Response(200, json={"pairs": [
            {"chainId": "solana", "baseToken": {"address": dex_only, "name": "Cat Inu", "symbol": "CATINU"},
             "liquidity": {"usd": 5000}, "marketCap": 250_000, "volume": {"h24": 90_000}, "pairCreatedAt": (NOW - 600) * 1000,
             "info": {"socials": [{"type": "twitter", "url": "https://x.com/catinu"}]}},
            {"chainId": "ethereum", "baseToken": {"address": "0xabc", "name": "Cat", "symbol": "CAT"}},
            {"chainId": "solana", "baseToken": {"address": _addr(), "name": "Bird", "symbol": "BRD"}},  # fuzzy noise
        ]})
    return httpx.MockTransport(handler)


def test_family_labels_bundle_state_and_pick():
    store, og, copy, larp_tok, dex_only = _world()
    cfg = Config()
    larp = FakeLarp({og: (10, []), copy: (45, ["copied_twitter", "copycat_name"]), larp_tok: (75, ["claims"]),
                     dex_only: (20, [])})
    scanner = FakeScanner({og: (0.12, 0.05, 0.40, 0.20), copy: (0.45, 0.25, 0.6, 0.5),
                           larp_tok: (0.05, 0.02, 0.1, 0.1), dex_only: (0.03, 0.01, 0.05, 0.04)})
    na = NarrativeAnalyzer(cfg, store, larp, scanner, http=httpx.AsyncClient(transport=_dex_transport(dex_only)))

    async def go():
        members = await na.find(parse_query("cat"))
        await na.analyze(members)
        return members

    members = asyncio.run(go())
    by = {m.mint: m for m in members}
    assert set(by) == {og, copy, larp_tok, dex_only}  # DOG, the ETH pair and the fuzzy match are out
    assert by[dex_only].sources == {"dexscreener"} and by[dex_only].market_cap == 250_000
    assert store.token(dex_only)["twitter"] == "x.com/catinu"  # remembered for later copycat checks

    assert by[og].first and by[og].narrative_label == "ORIGINAL"
    assert by[og].cluster_black and by[og].bundle_label == "PROFITABLE BUNDLER"  # dev funded by MotherX
    assert by[copy].narrative_label == "COPY" and by[copy].bundle_label == "HEAVY BUNDLE"
    assert by[larp_tok].narrative_label == "LARP"
    assert by[dex_only].narrative_label == "SIDE TOKEN" and by[dex_only].bundle_label == "CLEAN"

    best, why = na.pick(members)
    assert best is by[og]
    assert "original token" in why and "in the black" in why
    text = format_family(parse_query("cat"), members, (best, why))
    assert "PICK:" in text and "AVOID: 2 copies / LARPs" in text


def test_seed_mint_and_link_queries():
    store, og, copy, *_ = _world()
    na = NarrativeAnalyzer(Config(), store, FakeLarp({}), None, http=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"pairs": []}))))
    members = asyncio.run(na.find(parse_query(og), use_dex=False))
    assert {og, copy} <= {m.mint for m in members}  # same ticker + same X account
    members = asyncio.run(na.find(parse_query("https://x.com/catdev"), use_dex=False))
    assert {m.mint for m in members} == {og, copy}


def test_watch_matches_new_launches_by_name():
    na = NarrativeAnalyzer(Config(), Store(":memory:"), FakeLarp({}), None)
    q = parse_query("trump cat")
    assert na.matches_create(q, MarketEvent("create", "M", "d", 0, 0, 0, 0, name="TrumpCat", symbol="TCAT"))
    assert not na.matches_create(q, MarketEvent("create", "M", "d", 0, 0, 0, 0, name="Trump Dog", symbol="TDOG"))


def test_no_pick_when_everything_is_borrowed():
    na = NarrativeAnalyzer(Config(), Store(":memory:"), FakeLarp({}), None)
    a = Member("A", larp=LarpReport("A", TokenMeta(), [Finding("red", "x", "")] * 3), first=True)
    best, why = na.pick([a])
    assert best is None and "borrowed" in why
