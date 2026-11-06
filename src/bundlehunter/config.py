"""Configuration: dataclass defaults, overridden by a TOML file, then by env vars.

Secrets (private key, paid RPC URL) belong in environment variables / `.env`,
never in the TOML file or the repository.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any


@dataclass
class RpcConfig:
    url: str = "https://api.mainnet-beta.solana.com"  # use a paid RPC (Helius, Triton, QuickNode...)
    max_concurrency: int = 8
    requests_per_second: float = 8.0
    timeout_s: float = 20.0


@dataclass
class AnalyzerConfig:
    early_slots: int = 3  # launch window: create slot .. create slot + N
    bundle_min_wallets_per_slot: int = 3  # early slot with >= N buyers = bundle too (0: create slot only)
    max_mint_sig_pages: int = 10  # 1000 signatures per page
    max_wallet_sig_pages: int = 2
    max_wallet_txs: int = 150  # newest txs fetched per profiled wallet
    min_funding_sol: float = 0.05  # ignore dust (address-poisoning) when finding a wallet's funder
    quick_funder_sigs: int = 100  # funder-only lookup: wallets with longer histories are skipped
    max_quick_funders: int = 40  # per token, for the insiders check
    profile_snipers: bool = False
    max_full_txs: int = 3000  # `analyze --full`: oldest N txs of the token (price path for backtests)
    # cheap prefilter used for automatically recorded launches
    min_create_slot_txs: int = 3  # txs in the create slot, incl. the create itself
    min_total_txs: int = 150
    min_age_s: float = 1800  # analyze a launch only after the bundle had time to sell


@dataclass
class ClusterConfig:
    hub_funder_max_wallets: int = 40  # a funder of more wallets than this is a CEX / hub: no link
    ignore_funders: list[str] = field(default_factory=list)  # known exchange hot wallets etc.
    min_co_launches: int = 2  # wallets bundled together in >= N launches are linked
    min_co_jaccard: float = 0.6  # ...and overlap >= 60% of their launches (filters sniper bots)
    closed_sold_frac: float = 0.9  # a launch is "closed" once the cluster sold 90% of what it bought


@dataclass
class SelectionConfig:
    """Which clusters count as 'in the black' and get copied."""

    min_launches: int = 3
    min_closed_launches: int = 2
    min_win_rate: float = 0.6
    min_total_pnl_sol: float = 3.0
    min_median_first_sell_s: float = 20.0  # if they always dump in < 20s you cannot follow them out
    max_wallets: int = 400


@dataclass
class StrategyConfig:
    mode: str = "dip"  # "dip" = wait for a pullback; "snipe" = buy as soon as the bundle is seen
    buy_sol: float = 0.1
    max_open_positions: int = 3
    max_watches: int = 50
    daily_loss_limit_sol: float = 1.0
    entry_window_s: float = 600.0  # only enter within N seconds of the watch starting
    # dip logic
    dip_pct: float = 0.25  # price must be >= 25% below the local high...
    rebound_pct: float = 0.05  # ...and have bounced >= 5% off the low (do not catch a falling knife)
    max_entry_vs_bundle: float = 4.0  # never pay more than 4x the bundle's average entry ("the highs")
    min_trades_before_entry: int = 5
    # rug / dump guards
    abort_if_cluster_sold_pct: float = 0.15  # bundle already sold 15% -> it is dumping, stay out
    max_cluster_supply_pct: float = 0.6  # bundle holds > 60% of supply -> too much dump risk
    require_token_checks: bool = True  # mint + freeze authority must be revoked
    # Axiom-style holder panel, "% of supply held now" (1.0 = filter off). Costs ~1-3 s of RPC
    # after launch, so with require_holder_panel=true even snipe mode waits for it.
    require_holder_panel: bool = True
    panel_delay_s: float = 3.0  # let the launch window (create slot + early_slots) land first
    panel_refresh_s: float = 10.0
    max_top10_pct: float = 0.50
    max_dev_pct: float = 0.20
    max_snipers_pct: float = 0.30
    max_insiders_pct: float = 0.30
    max_bundlers_pct: float = 0.50  # you are copying the bundle, so this is looser than on Axiom
    # LARP check (narrative / socials / copycats), 0-100; runs in parallel with the holder panel
    require_larp_check: bool = True
    max_larp_score: int = 60
    # exits
    exit_on_cluster_sell_pct: float = 0.10  # bundle sells 10% of its bag after our entry -> exit
    exit_on_linked_drop_pct: float = 0.25  # dev+bundlers+insiders hold 25% less than at entry -> exit
    take_profit_pct: float = 1.0  # +100%
    stop_loss_pct: float = 0.30  # -30%
    trailing_stop_pct: float = 0.0  # 0 = off
    max_hold_s: float = 1200.0


@dataclass
class ExecutionConfig:
    swap_api: str = "https://pumpportal.fun/api/trade-local"  # returns an unsigned tx we sign locally
    pool: str = "auto"
    slippage_pct: float = 5.0  # low on purpose: 20-50% slippage gets sandwiched ~18x more often than 1-3%
    priority_fee_sol: float = 0.0005
    use_jito: bool = True
    jito_block_engine: str = "https://mainnet.block-engine.jito.wtf"
    jito_tip_sol: float = 0.001
    also_send_rpc: bool = True  # same signature, cannot land twice; improves landing rate
    confirm_timeout_s: float = 30.0
    paper_slippage_pct: float = 2.0
    paper_fee_pct: float = 1.25  # pool / platform fee per side (PumpSwap ~1.25%)
    paper_fee_sol: float = 0.0007  # priority fee + tip per paper trade
    paper_start_sol: float = 1.0  # first paper balance (bh paper reset / deposit to change)
    # sizing and hard limits, paper and live
    max_stake_pct: float = 0.25  # one trade <= 25% of the free balance
    reserve_sol: float = 0.02  # always left for fees
    live_max_total_loss_sol: float = 1.0  # all-time live loss at which live buying stops for good


@dataclass
class FeedConfig:
    pumpportal_ws: str = "wss://pumpportal.fun/api/data"
    record_launches: bool = True  # store every new token so the analyzer can process it later
    funder_lookup_on_create: bool = False  # 2 RPC calls per launch: catches fresh dev wallets funded by known bundlers
    fetch_metadata: bool = True  # fetch every launch's metadata JSON so the LARP check can spot copied socials


@dataclass
class LarpConfig:
    """LARP check: is the token's story (socials, site, name, holders) real or borrowed?"""

    ipfs_gateway: str = "https://ipfs.io/ipfs/"
    http_timeout_s: float = 6.0
    fetch_website: bool = True
    rdap: bool = True  # domain age via rdap.org
    fresh_max_txs: int = 10  # a top holder with <= N txs in its whole history is a fresh wallet
    famous_handles: list[str] = field(
        default_factory=lambda: [
            "elonmusk", "realdonaldtrump", "potus", "cz_binance", "binance", "coinbase", "solana",
            "aeyakovenko", "vitalikbuterin", "saylor", "kanyewest", "openai", "nasa", "spacex", "tesla",
        ]
    )
    ai_model: str = "claude-opus-5-5"  # optional AI verdict (`bh check --ai`, needs ANTHROPIC_API_KEY)
    ai_effort: str = "medium"


@dataclass
class NarrativeConfig:
    """`bh narrative`: every token riding one narrative, each with LARP + bundle analysis."""

    dexscreener: bool = True  # also search DexScreener (free public API), not only your recorded launches
    dexscreener_url: str = "https://api.dexscreener.com/latest/dex/search"
    max_analyze: int = 10  # tokens analyzed in depth per query (RPC cost)
    watch_delay_s: float = 20.0  # --watch: wait for the bundle and metadata to land before analyzing


@dataclass
class ScalpConfig:
    """Scalping: take 20-60% of a move that strong wallets started and volume confirms."""

    enabled: bool = True
    buy_sol: float = 0.1
    max_open_positions: int = 2
    # 1. a reason to enter: your traders (bh trader rank gives them a score)
    min_trader_score: float = 60.0  # a "smart" wallet = one of your traders scoring >= 60
    strong_trader_score: float = 80.0  # one wallet this strong is reason enough...
    min_smart_wallets: int = 2  # ...otherwise at least 2 different smart people
    min_smart_buy_sol: float = 0.2
    entry_window_s: float = 300.0  # no confirmation within 5 min -> skip (a missed trade costs 0)
    # 2. confirmation, not a blind chase
    momentum_window_s: float = 60.0
    min_buy_volume_sol: float = 3.0
    min_buy_pressure: float = 1.5  # buy SOL / sell SOL in the window
    min_unique_buyers: int = 5
    max_chase_pct: float = 0.5  # never pay more than +50% above the first smart buy
    max_spike_pct: float = 0.25  # no entry into a vertical candle: +25% within spike_window_s
    spike_window_s: float = 15.0
    max_larp_score: int = 60
    max_bundlers_pct: float = 0.30
    max_insiders_pct: float = 0.20
    # 3. the plan, fixed before entry
    take_profit_max_pct: float = 0.6  # small position
    take_profit_min_pct: float = 0.2  # large position (harder to exit)
    size_for_min_tp_sol: float = 2.0
    stop_loss_pct: float = 0.15
    trailing_pct: float = 0.10  # once half of the take profit is reached
    max_hold_s: float = 600.0
    # exit mode: "smart" (TP1 part -> break-even -> trailing ladder), "never_lose" (no red exit once
    # break-even is locked; before that only an emergency stop), "fixed" (all-or-nothing TP / SL)
    exit_mode: str = "candle"
    # "candle": the whole trade lives inside ONE candle: enter on a green, confirmed candle,
    # sell everything at TP / stop / trailing or when the candle closes. Many short trades, no hour-long holds.
    candle_s: float = 60.0  # candle length = max time in a position
    candle_entry_candles: int = 3  # no setup within 3 candles of the first smart buy -> skip, free the slot
    candle_tp_pct: float = 0.15
    candle_min_tp_cost_multiple: float = 2.0  # TP still >= 2x round-trip costs, or fees eat every win
    candle_sl_pct: float = 0.08  # hard stop, no waiting: there is no time inside one candle
    candle_trail_pct: float = 0.05  # after half the TP: out 5% below the candle's high
    candle_cooldown_s: float = 60.0  # after a loss: sit out one candle, not ten minutes
    candle_max_trades_per_hour: int = 30
    round_trip_cost_pct: float = 0.06  # fees + slippage both ways; paper uses the broker's real figure
    min_tp_cost_multiple: float = 3.0  # take profit is never below 3x the costs (a +12% TP on 3.3% costs bleeds)
    tp1_fraction: float = 0.5  # share sold at TP1: gets the stake back, the rest runs for free
    stop_confirm_s: float = 4.0  # price must stay under the stop this long: no wick-outs...
    emergency_stop_pct: float = 0.30  # ...except a crash this deep: out at once
    runner_trail_pct: float = 0.25  # wider trailing once the trade has doubled
    never_lose_arm_pct: float = 0.10  # never_lose: profit beyond costs that locks break-even
    never_lose_max_wait_s: float = 1800.0  # never_lose: still under water after 30 min -> exit anyway
    # validation gate: trade only settings that made money on recorded data they never saw
    gate: str = "live"  # "live" = live needs a validated strategy (paper keeps trading to collect data),
    #                     "always" = paper too, "off" = never
    eval_every_s: float = 6 * 3600
    eval_modes: list[str] = field(default_factory=lambda: ["candle"])  # modes the validation may choose from
    eval_split: float = 0.7  # pick the exit mode on the oldest 70% of tokens, judge it on the newest 30%
    fill_delay_s: float = 2.0  # replay fills at the price this much after the decision
    min_test_trades: int = 8
    min_avg_trade_pct: float = 0.01  # >= +1% per trade on unseen tokens
    max_token_profit_share: float = 0.20  # no single token may bring more than 20% of the profit
    validated_max_age_s: float = 3 * 86400
    # fake-pump filters
    staircase_veto: bool = True  # even buys in steps, no sells, no wicks = a painted chart
    dump_into_smart_veto_pct: float = 0.30  # bundle/insiders already sold 30% when smart wallets buy = two-step setup
    # 4. leave when the idea stops working
    exit_sell_pressure: float = 1.5  # sells outweigh buys 1.5x in the window
    exit_on_smart_sell: bool = True
    # 5. no revenge trading
    cooldown_after_loss_s: float = 600.0
    max_consecutive_losses: int = 3
    pause_after_streak_s: float = 3600.0
    daily_loss_limit_sol: float = 0.5
    max_trades_per_hour: int = 6


@dataclass
class SignalsConfig:
    """Signal bot: your trader wallets + bundle groups in the black -> Telegram."""

    trader_buys: bool = True
    trader_sells: bool = True  # only for tokens we signalled a buy for
    min_trader_buy_sol: float = 0.2  # smaller buys are noise
    confluence_window_s: float = 1800.0  # several traders in the same token within 30 min = stronger signal
    analyze: bool = True  # follow-up message with bundle panel + LARP check
    bundle_signals: bool = True  # profitable bundle group launched / bought a token
    trader_history_txs: int = 400  # txs fetched when you add a trader (for PnL stats)


@dataclass
class TelegramConfig:
    """Telegram bot (bh serve). Token and allowed chat ids come from .env: BH_TELEGRAM_TOKEN,
    BH_TELEGRAM_ALLOWED_IDS. Without allowed ids the bot answers nobody but tells you your id."""

    notify_trades: bool = True  # message on every buy / sell
    api_url: str = "https://api.telegram.org"


@dataclass
class WebConfig:
    """Web panel (bh serve). Anything but localhost needs BH_WEB_PASSWORD."""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8080


@dataclass
class AutoConfig:
    analyze_every_s: float = 300.0
    analyze_batch: int = 20
    rebuild_clusters_every_s: float = 900.0


@dataclass
class Config:
    db_path: str = "data/bundlehunter.db"
    rpc: RpcConfig = field(default_factory=RpcConfig)
    analyzer: AnalyzerConfig = field(default_factory=AnalyzerConfig)
    clusters: ClusterConfig = field(default_factory=ClusterConfig)
    selection: SelectionConfig = field(default_factory=SelectionConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    feed: FeedConfig = field(default_factory=FeedConfig)
    larp: LarpConfig = field(default_factory=LarpConfig)
    narrative: NarrativeConfig = field(default_factory=NarrativeConfig)
    scalp: ScalpConfig = field(default_factory=ScalpConfig)
    signals: SignalsConfig = field(default_factory=SignalsConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    web: WebConfig = field(default_factory=WebConfig)
    auto: AutoConfig = field(default_factory=AutoConfig)


def _apply(obj: Any, data: dict[str, Any], path: str = "") -> None:
    names = {f.name for f in fields(obj)}
    unknown = set(data) - names
    if unknown:
        raise ValueError(f"unknown config keys in [{path or 'root'}]: {sorted(unknown)}")
    for name, value in data.items():
        current = getattr(obj, name)
        if is_dataclass(current):
            if not isinstance(value, dict):
                raise ValueError(f"[{path}{name}] must be a table")
            _apply(current, value, f"{path}{name}.")
        else:
            setattr(obj, name, value)


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader (KEY=VALUE lines); existing env vars win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if value[:1] in ("'", '"') and value[1:].find(value[0]) >= 0:
            value = value[1 : 1 + value[1:].find(value[0])]  # quoted: take it literally
        elif value.startswith("#"):
            value = ""  # KEY=   # comment
        else:
            value = value.split(" #", 1)[0].split("\t#", 1)[0].strip()  # drop an inline comment
        os.environ.setdefault(key.strip(), value)


def load_config(path: str | Path | None = None) -> Config:
    load_dotenv()
    cfg = Config()
    path = path or os.environ.get("BH_CONFIG")
    if path is None and Path("config.toml").exists():
        path = "config.toml"
    if path:
        with open(path, "rb") as fh:
            _apply(cfg, tomllib.load(fh))
    if os.environ.get("BH_RPC_URL"):
        cfg.rpc.url = os.environ["BH_RPC_URL"]
    if os.environ.get("BH_DB_PATH"):
        cfg.db_path = os.environ["BH_DB_PATH"]
    if os.environ.get("BH_JITO_URL"):
        cfg.execution.jito_block_engine = os.environ["BH_JITO_URL"]
    if os.environ.get("BH_WEB_HOST"):
        cfg.web.host = os.environ["BH_WEB_HOST"]
    if os.environ.get("BH_WEB_PORT"):
        cfg.web.port = int(os.environ["BH_WEB_PORT"])
    return cfg
