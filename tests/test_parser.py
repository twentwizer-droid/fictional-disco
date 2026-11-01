from factory import RENT, SOL, TIP, init_mint, make_tx, pump_buy_tx, sys_transfer, tok

from bundlehunter.constants import WSOL_MINT
from bundlehunter.parser import parse_transaction

MINT = "Mint111"


def test_pump_buy_with_tip_and_create():
    ptx = parse_transaction(pump_buy_tx("sig1", 100, "Dev", MINT, sol=0.5, tokens=1_000_000, tip_sol=0.001, create=True))
    assert ptx.created_mints == [MINT]
    assert ptx.fee_payer == "Dev"
    assert ptx.tips_lamports == int(0.001 * SOL)
    (t,) = ptx.trades
    assert (t.wallet, t.side, t.mint) == ("Dev", "buy", MINT)
    assert t.token_amount == 1_000_000
    assert abs(t.sol_amount - 0.5) < 1e-9  # ATA rent, fee and tip are not part of the price
    assert abs(t.tip_sol - 0.001) < 1e-12
    assert abs(t.price - 0.5 / 1_000_000) < 1e-15
    assert abs(t.cash_flow - -(0.5 + 0.000005 + 0.001)) < 1e-9


def test_sell_closing_token_account():
    raw = 2_000_000 * 1_000_000
    keys = [("Seller", True), ("Seller-ata", False), ("curve", False), ("curve-ata", False)]
    pre = [1 * SOL, RENT, 40 * SOL, RENT]
    post = [1 * SOL + int(0.8 * SOL) - 5000 + RENT, 0, 40 * SOL - int(0.8 * SOL), RENT]
    tx = make_tx(
        "sig2", 200, keys, pre, post,
        pre_tok=[tok(1, MINT, "Seller", raw), tok(3, MINT, "curve", 10**15)],
        post_tok=[tok(3, MINT, "curve", 10**15 + raw)],
    )
    (t,) = parse_transaction(tx).trades
    assert (t.side, t.token_amount) == ("sell", 2_000_000)
    assert abs(t.sol_amount - 0.8) < 1e-9


def test_wsol_route_counts_as_sol():
    keys = [("Trader", True), ("Trader-wsol", False), ("Trader-ata", False), ("pool", False), ("pool-ata", False)]
    pre = [1 * SOL, RENT + 1 * SOL, RENT, 0, RENT]
    post = [1 * SOL - 5000, RENT + SOL // 2, RENT, 0, RENT]
    tx = make_tx(
        "sig3", 300, keys, pre, post,
        pre_tok=[tok(1, WSOL_MINT, "Trader", SOL, 9), tok(2, MINT, "Trader", 0), tok(4, MINT, "pool", 10**12)],
        post_tok=[tok(1, WSOL_MINT, "Trader", SOL // 2, 9), tok(2, MINT, "Trader", 5 * 10**6), tok(4, MINT, "pool", 10**12 - 5 * 10**6)],
    )
    (t,) = parse_transaction(tx).trades
    assert (t.side, t.token_amount) == ("buy", 5)
    assert abs(t.sol_amount - 0.5) < 1e-9


def test_token_transfer_without_sol_leg():
    keys = [("A", True), ("A-ata", False), ("B-ata", False)]
    tx = make_tx(
        "sig4", 400, keys, [SOL, RENT, 0], [SOL - 5000 - RENT, RENT, RENT],
        pre_tok=[tok(1, MINT, "A", 100 * 10**6)],
        post_tok=[tok(1, MINT, "A", 0), tok(2, MINT, "B", 100 * 10**6)],
    )
    ptx = parse_transaction(tx)
    assert ptx.trades == []
    (x,) = ptx.token_transfers
    assert (x.src, x.dst, x.amount) == ("A", "B", 100)


def test_multi_signer_bundle_tx():
    keys = [("Payer", True), ("W2", True), ("Payer-ata", False), ("W2-ata", False), ("curve", False), ("curve-ata", False)]
    pre = [3 * SOL, 2 * SOL, 0, 0, 30 * SOL, RENT]
    post = [3 * SOL - SOL - 10000 - RENT, 2 * SOL - SOL // 2 - RENT, RENT, RENT, 31 * SOL + SOL // 2, RENT]
    tx = make_tx(
        "sig5", 500, keys, pre, post,
        pre_tok=[tok(5, MINT, "curve", 10**15)],
        post_tok=[tok(2, MINT, "Payer", 20 * 10**6), tok(3, MINT, "W2", 10 * 10**6), tok(5, MINT, "curve", 10**15 - 30 * 10**6)],
        instructions=[init_mint("Other")],
        fee=10000,
    )
    ptx = parse_transaction(tx)
    by_wallet = {t.wallet: t for t in ptx.trades}
    assert abs(by_wallet["Payer"].sol_amount - 1.0) < 1e-9 and by_wallet["Payer"].fee_sol == 10000 / SOL
    assert abs(by_wallet["W2"].sol_amount - 0.5) < 1e-9 and by_wallet["W2"].fee_sol == 0
    assert ptx.tips_lamports == 0
    assert ptx.created_mints == ["Other"]


def test_funding_transfer_in_inner_instructions():
    keys = [("Mother", True), ("Fresh", False)]
    tx = make_tx("sig7", 700, keys, [10 * SOL, 0], [9 * SOL - 5000, SOL], inner=[sys_transfer("Mother", "Fresh", SOL)])
    ptx = parse_transaction(tx)
    assert [(s.src, s.dst, s.lamports) for s in ptx.sol_transfers] == [("Mother", "Fresh", SOL)]
    assert ptx.trades == [] and ptx.token_transfers == []


def test_failed_tx_has_no_effects():
    tx = pump_buy_tx("sig6", 600, "U", MINT, 1, 1000, tip_sol=0.01, create=True)
    tx["meta"]["err"] = {"InstructionError": [0, "Custom"]}
    ptx = parse_transaction(tx)
    assert ptx.failed and not ptx.trades and not ptx.created_mints and ptx.tips_lamports == 0


def test_tip_account_constant_is_used():
    assert TIP.startswith(("3AV", "96g", "ADa", "ADu", "Cw8", "DfX", "Dtt", "HFq"))
