import pytest

from bundlehunter.config import load_config
from bundlehunter.execution import PaperBroker
from bundlehunter.feeds import normalize


def test_normalize_pumpportal_messages():
    create = {
        "signature": "s", "mint": "M", "traderPublicKey": "Dev", "txType": "create", "initialBuy": 3e7,
        "solAmount": 1.0, "vTokensInBondingCurve": 1.0e9, "vSolInBondingCurve": 31.0, "name": "N", "symbol": "SYM",
    }
    ev = normalize(create, 5.0)
    assert (ev.kind, ev.wallet, ev.token_amount, ev.symbol) == ("create", "Dev", 3e7, "SYM")
    assert ev.price == pytest.approx(31.0 / 1.0e9)
    trade = {"mint": "M", "traderPublicKey": "W", "txType": "sell", "tokenAmount": 100, "solAmount": 0.001}
    assert normalize(trade, 1.0).price == pytest.approx(1e-5)
    assert normalize({"message": "Successfully subscribed"}, 0) is None


def test_config_toml_and_env(tmp_path, monkeypatch):
    p = tmp_path / "c.toml"
    p.write_text('db_path = "x.db"\n[strategy]\nmode = "snipe"\nbuy_sol = 0.05\n')
    monkeypatch.setenv("BH_RPC_URL", "https://example-rpc")
    monkeypatch.chdir(tmp_path)
    cfg = load_config(p)
    assert (cfg.db_path, cfg.strategy.mode, cfg.strategy.buy_sol, cfg.rpc.url) == ("x.db", "snipe", 0.05, "https://example-rpc")
    p.write_text("[strategy]\nbuy_sool = 1\n")
    with pytest.raises(ValueError, match="buy_sool"):
        load_config(p)


def test_paper_broker_applies_slippage_and_fees():
    b = PaperBroker(slippage_pct=10, fee_sol=0.01)
    buy = b.fill_buy("M", 1.0, 1e-6)
    assert buy.price == pytest.approx(1.1e-6) and buy.sol == pytest.approx(1.01)
    sell = b.fill_sell("M", buy.tokens, 2e-6)
    assert sell.sol == pytest.approx(buy.tokens * 1.8e-6 - 0.01)
