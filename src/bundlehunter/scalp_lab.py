"""Validate the scalper on recorded data it never saw, before it is allowed to risk money.

Idea taken from CodingNoodless/memecoinscalper: "no champion = no trades".

  1. Record:   while `bh serve` / `bh run` work, every event the scalper sees for a token it
               watches is stored (scalp_events), with the trader score at that moment and the
               holder / LARP metrics of the check (scalp_checks).
  2. Replay:   the recordings go through the real ScalpStrategy, with real paper costs and a
               fill delay (orders fill at the price `fill_delay_s` after the decision).
  3. Choose:   the exit mode (smart / never_lose / fixed) is picked on the OLDEST 70% of tokens.
  4. Judge:    that mode is then run on the NEWEST 30%, which played no part in the choice.
  5. Promote:  only if, on those unseen tokens, there are >= min_test_trades trades, the
               average trade makes >= +1%, the median trade is positive and no single token
               brings more than 20% of the profit. Otherwise nothing is validated.

The result goes to data/scalp_validated.json. With gate = "live" (default) live trading
needs a fresh validated result; paper keeps trading so that there is data to learn from.
"""

from __future__ import annotations

import bisect
import json
import statistics
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import Config, ScalpConfig
from .execution import PaperBroker
from .models import MarketEvent
from .scalp import ScalpStrategy
from .storage import Store

VALIDATED_FILE = "data/scalp_validated.json"
MODES = ("candle", "smart", "never_lose", "fixed")


def replay(events: dict[str, list[dict]], checks: dict[str, dict], scfg: ScalpConfig, broker: PaperBroker,
           fill_delay_s: float = 2.0) -> list[dict[str, Any]]:
    """Run recorded tokens through one ScalpStrategy (shared risk state, like live). Returns closed trades."""
    s = ScalpStrategy(replace(scfg, gate="off"))
    s.cost_model = broker.round_trip_cost_pct
    traders = {r["wallet"]: (r["person"], r["score"]) for evs in events.values() for r in evs if r.get("person")}
    s.update_traders(traders)
    times = {m: [r["ts"] for r in evs] for m, evs in events.items()}

    def price_after(mint: str, ts: float, fallback: float) -> float:
        evs, ts_list = events[mint], times[mint]
        i = bisect.bisect_left(ts_list, ts + fill_delay_s)
        for r in evs[i:]:
            if r["price"]:
                return r["price"]
        return fallback

    trades: list[dict[str, Any]] = []
    stream = sorted(((r["ts"], m, r) for m, evs in events.items() for r in evs), key=lambda x: (x[0], x[1]))
    checked: set[str] = set()

    def execute(actions: list, ts: float) -> None:
        for a in actions:
            px = price_after(a.mint, ts, a.ref_price)
            if a.side == "buy":
                f = broker.fill_buy(a.mint, a.sol_amount, px, ts)
                s.on_buy_filled(a.mint, f.price, f.tokens, f.sol, ts, a.reason)
            elif a.fraction < 0.999:
                f = broker.fill_sell(a.mint, a.token_amount, px, ts)
                s.on_partial_filled(a.mint, f.price, f.sol, f.tokens, ts)
            else:
                f = broker.fill_sell(a.mint, a.token_amount, px, ts)
                size = s.positions[a.mint].size_sol
                pnl = s.on_sell_filled(a.mint, f.price, f.sol, ts, a.reason)
                trades.append({"mint": a.mint, "pnl_sol": pnl, "ret": pnl / size if size else 0.0, "reason": a.reason})

    last_ts = 0.0
    for ts, mint, r in stream:
        last_ts = ts
        execute(s.on_tick(ts), ts)
        ev = MarketEvent(r["kind"], mint, r["wallet"] or "", ts, r["sol"] or 0.0, r["tokens"] or 0.0, r["price"] or 0.0)
        actions = s.on_event(ev)
        if mint in s.watches and mint not in checked:
            checked.add(mint)
            c = checks.get(mint, {"ok": True, "reason": "", "metrics": {}})
            s.set_check(mint, c["ok"], c["reason"], metrics=c["metrics"])
        s.drain_new_watches()
        s.drain_finished()
        execute(actions, ts)
    for mint, pos in list(s.positions.items()):  # still open when the recording ends
        px = next((r["price"] for r in reversed(events[mint]) if r["price"]), pos.entry_price)
        f = broker.fill_sell(mint, pos.tokens, px, last_ts)
        pnl = s.on_sell_filled(mint, f.price, f.sol, last_ts, "кінець запису")
        trades.append({"mint": mint, "pnl_sol": pnl, "ret": pnl / pos.size_sol, "reason": "кінець запису"})
    return trades


def summarize(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {"trades": 0, "pnl_sol": 0.0, "avg_ret": None, "median_ret": None, "win_rate": None, "top_token_share": None}
    rets = [t["ret"] for t in trades]
    per_token: dict[str, float] = {}
    for t in trades:
        per_token[t["mint"]] = per_token.get(t["mint"], 0.0) + t["pnl_sol"]
    gains = [v for v in per_token.values() if v > 0]
    return {
        "trades": len(trades),
        "pnl_sol": round(sum(t["pnl_sol"] for t in trades), 6),
        "avg_ret": statistics.mean(rets),
        "median_ret": statistics.median(rets),
        "win_rate": sum(r > 0 for r in rets) / len(rets),
        "top_token_share": max(gains) / sum(gains) if gains else None,
    }


def evaluate(store: Store, cfg: Config, save: bool = True) -> dict[str, Any]:
    scfg, ex = cfg.scalp, cfg.execution
    events, checks = store.scalp_recordings()
    events = {m: evs for m, evs in events.items() if evs}
    order = sorted(events, key=lambda m: events[m][0]["ts"])
    cut = int(len(order) * scfg.eval_split)
    train = {m: events[m] for m in order[:cut]}
    test = {m: events[m] for m in order[cut:]}

    def broker() -> PaperBroker:
        return PaperBroker(ex.paper_slippage_pct, ex.paper_fee_sol, ex.paper_fee_pct)

    modes = [m for m in scfg.eval_modes if m in MODES] or [scfg.exit_mode]
    by_mode = {m: summarize(replay(train, checks, replace(scfg, exit_mode=m), broker(), scfg.fill_delay_s)) for m in modes}
    candidates = [m for m in modes if by_mode[m]["trades"]]
    chosen = max(candidates, key=lambda m: by_mode[m]["pnl_sol"]) if candidates else scfg.exit_mode
    unseen = summarize(replay(test, checks, replace(scfg, exit_mode=chosen), broker(), scfg.fill_delay_s))

    reasons = []
    if unseen["trades"] < scfg.min_test_trades:
        reasons.append(f"на нових токенах лише {unseen['trades']} угод з {scfg.min_test_trades}")
    else:
        if unseen["avg_ret"] < scfg.min_avg_trade_pct:
            reasons.append(f"середня угода {unseen['avg_ret']:+.1%} < {scfg.min_avg_trade_pct:+.0%}")
        if unseen["median_ret"] <= 0:
            reasons.append(f"медіанна угода {unseen['median_ret']:+.1%}: більшість угод у мінусі")
        if unseen["top_token_share"] is not None and unseen["top_token_share"] > scfg.max_token_profit_share:
            reasons.append(f"{unseen['top_token_share']:.0%} прибутку з одного токена (> {scfg.max_token_profit_share:.0%}): "
                           "це удача, а не стратегія")
    result = {
        "validated": not reasons,
        "mode": chosen,
        "reasons": reasons,
        "tokens": {"total": len(order), "choose_on": len(train), "judge_on": len(test)},
        "choose": by_mode,
        "unseen": unseen,
        "ts": time.time(),
    }
    if save:
        Path(VALIDATED_FILE).parent.mkdir(parents=True, exist_ok=True)
        Path(VALIDATED_FILE).write_text(json.dumps(result, ensure_ascii=False, indent=1))
    return result


def load_validated(path: str = VALIDATED_FILE) -> dict[str, Any] | None:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def format_result(r: dict[str, Any]) -> str:
    u = r["unseen"]
    head = "ПЕРЕВІРЕНО ✅" if r["validated"] else "НЕ ПЕРЕВІРЕНО ⛔"
    lines = [
        f"{head} · режим виходу: {r['mode']}",
        f"токенів у записі {r['tokens']['total']}: вибір на {r['tokens']['choose_on']}, перевірка на {r['tokens']['judge_on']} нових",
    ]
    for m, st in r["choose"].items():
        avg = f"{st['avg_ret']:+.1%}" if st["avg_ret"] is not None else "–"
        lines.append(f"  вибір · {m:10} угод {st['trades']:3} · PnL {st['pnl_sol']:+.4f} SOL · сер. {avg}")
    if u["trades"]:
        lines.append(f"нові токени: угод {u['trades']} · PnL {u['pnl_sol']:+.4f} SOL · сер. {u['avg_ret']:+.1%} · "
                     f"медіана {u['median_ret']:+.1%} · в плюс {u['win_rate']:.0%}"
                     + (f" · найбільший токен {u['top_token_share']:.0%} прибутку" if u["top_token_share"] is not None else ""))
    lines += [f"  ✗ {x}" for x in r["reasons"]]
    if not r["validated"]:
        lines.append("Live не купує, папір торгує далі й збирає дані. Наступна перевірка автоматично.")
    return "\n".join(lines)
