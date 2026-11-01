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
    slippage_pct: float = 15.0
    priority_fee_sol: float = 0.0005
    use_jito: bool = True
    jito_block_engine: str = "https://mainnet.block-engine.jito.wtf"
    jito_tip_sol: float = 0.001
    also_send_rpc: bool = True  # same signature, cannot land twice; improves landing rate
    confirm_timeout_s: float = 30.0
    paper_slippage_pct: float = 3.0
    paper_fee_sol: float = 0.0015  # assumed fee + tip per paper trade


@dataclass
class FeedConfig:
    pumpportal_ws: str = "wss://pumpportal.fun/api/data"
    record_launches: bool = True  # store every new token so the analyzer can process it later
    funder_lookup_on_create: bool = False  # 2 RPC calls per launch: catches fresh dev wallets funded by known bundlers


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
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


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
    return cfg
