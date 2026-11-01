# bundlehunter

Finds Solana memecoin **bundlers** (the wallet groups that buy their own launch inside a
Jito bundle), keeps a track record for each group, and copy-trades only the groups
that are **in the black**. It buys **dips, not highs**, avoids rugs, and **follows the
bundle out** when the bundle starts to sell.

```
            ┌──────────────── bh run (one process, 24/7) ─────────────────┐
PumpPortal  │  record every launch ──► SQLite ◄── analyzer (RPC)           │
 websocket ─┼─► strategy ◄── clusters "in the black" (rebuilt every 15 min)│
            │      │                                                       │
            │      └─► broker: paper (default) | live: sign → Jito bundle  │
            └──────────────────────────────────────────────────────────────┘
```

It answers three questions for every bundler cluster:

| question | how |
|---|---|
| **Which** tokens they launch / bundle | every launch where a cluster wallet was the creator or bought in the create slot |
| **When** | launch hour (UTC) histogram, seconds from launch to their first sell and to their exit |
| **With whom** | wallets linked by funding source, token transfers and repeated co-bundling; other clusters that show up in the same launches |

…and how much they made: realized PnL in SOL (fees and Jito tips included), win rate, open bags.

For every token it also builds an **Axiom-style holder panel** (`bh token <MINT>`):
**Top 10 H., Dev H., Snipers H., Insiders, Bundlers**. These are used as entry filters, and
as an exit signal when insiders and bundlers unload.

## Read this first: what the code can and cannot do

* **You cannot be "first in the bundle" of somebody else.** A Jito bundle is built
  and signed by the bundler and lands all at once. Nobody else's transaction can get
  inside it, and the mint does not exist until the bundle lands. The fastest you can
  be is the slot after you *see* the launch. What helps: a Geyser/gRPC stream, a
  server in the same region as the Jito block engine, and sending through Jito with
  a tip (all supported or noted below).
* **"First" and "buy the dip, not the high" pull in opposite directions.** Right
  after the bundle is usually the most expensive moment compared to what the bundle
  paid. So there are two modes:
  * `mode = "dip"` (default): wait for a pullback from the local high **and** a small
    bounce, and never pay more than `max_entry_vs_bundle` × the bundle's own average
    entry price;
  * `mode = "snipe"`: buy as soon as the cluster is seen. Fastest, and the most risk.
* **Bundlers make money by selling to the people who buy after them.** Copying their
  *entry* makes you their exit liquidity. The edge is knowing who holds the bag and
  **selling when they sell** (`exit_on_cluster_sell_pct`). Clusters that always dump
  within seconds (`min_median_first_sell_s`) are skipped because you cannot get out
  ahead of them.
* Wallet linking uses heuristics. Bundlers who fund wallets through exchanges or
  mixers break the funding link. The co-bundling and token-transfer links still
  catch wallets they reuse.
* Paper trading is the default. Most memecoin trades lose money, and nothing here
  is financial advice. Backtest and paper-trade before you risk anything.

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp config.example.toml config.toml
cp .env.example .env        # put your RPC URL in BH_RPC_URL
pytest                      # 37 offline tests
```

Use a **paid RPC** (Helius, Triton, QuickNode…). The public endpoint is too
rate-limited for wallet profiling. Rough cost: analyzing one launch takes about
`1 + (txs in the first 4 slots) + per bundle wallet (1–2 + up to max_wallet_txs)`
RPC calls.

## Workflow

### 1. Build the database

Pick one, or combine them:

```bash
# a) you already know a bundler wallet (from GMGN, Bubblemaps, your own digging)
bh seed <WALLET> [<WALLET> ...] --max-launches 20

# b) analyze specific launches
bh analyze <MINT> [<MINT> ...]            # add --full to store the price path for backtests

# c) fully automatic: record every pump.fun launch and analyze the bundled ones
bh run                                     # paper mode; leave it running
bh scan --limit 100                        # or process recorded launches manually
```

`analyze` finds the create transaction and its slot S. Buyers in slot S are the
**bundle** (the mint did not exist before S). Buyers in S+1…S+3 are **snipers**.
Then the creator and every bundle wallet are profiled: recent trades on any token,
and the **funder** (first deposit of at least 0.05 SOL, so dust and address
poisoning are ignored).

### 2. Look at the clusters

```bash
bh clusters                    # ranking by realized PnL, column "black" = will be copied
bh clusters --json out.json    # everything, machine readable
bh cluster <CLUSTER_ID|WALLET> # which / when / with whom, wallet list with funders, per-token PnL
```

What "in the black" means is set in `[selection]`: minimum launches, closed
launches, win rate, total PnL, and that they don't dump too fast to follow.

### 3. Check a token like on Axiom

```bash
bh token <MINT>              # analyzes the launch if needed, then live balances via RPC
bh token <MINT> --wallets    # the wallets behind every number, their funders, top-10 tags
bh token <MINT> --json
bh token <MINT> --db-only    # no RPC: holdings estimated from stored trades
```

Example output (illustrative numbers):

```
PEPE NewMintPEPE
created 10 min ago by devX9   holdings: live (RPC)

               wallets  held now  at launch
Top 10 H.           10     23.4%
Dev H.               1      2.0%       2.0%
Snipers H.           2      1.9%       1.9%
Insiders             5     10.0%      15.0%
Bundlers             2      6.5%      11.5%
Linked (all)         6     12.0%      14.5%

Dev cluster C-MotherX: 10 wallets, 4 launches, win 100%, pnl +13.50 SOL -> IN THE BLACK (copied)
Entry filters: pass
```

These are our own definitions. Axiom does not publish its formulas, so numbers can differ:

| row | wallets counted |
|---|---|
| **Top 10 H.** | 10 largest holders, without the bonding curve, AMM pools and lockers (program-owned, off-curve addresses) and the burn address |
| **Dev H.** | the creator |
| **Bundlers** | bought in the create slot (inside the dev's Jito bundle), or in an early slot where ≥ `bundle_min_wallets_per_slot` wallets bought at once |
| **Snipers H.** | every other buyer in the launch window (create slot + `early_slots`) |
| **Insiders** | linked to the dev: same cluster; funded by the dev, a bundler, or the dev's own funder; the dev's funder itself; or received the token by transfer from dev / bundlers / insiders (the "distribution" wallets) |
| **Linked (all)** | dev + bundlers + insiders, each wallet counted once |

"Held now" is % of total supply from live balances. "At launch" is what the group bought in
the launch window; for insiders it also includes tokens handed to them. Rows can overlap:
a bundle wallet funded by the dev is a bundler **and** an insider.

### 4. Backtest (walk-forward)

```bash
bh backtest --split 0.7 [--mode dip|snipe]
```

Clusters are scored on the oldest 70% of launches only, then the strategy is
replayed on the newest 30%, so no cluster is picked using the launches it is tested
on. Use `analyze --full` so the replay has the whole early price path.

### 5. Paper trade, then go live with small size

```bash
bh run                         # paper: records launches, analyzes, rebuilds clusters, trades on paper
bh positions                   # results

# live (dedicated hot wallet with only what you can afford to lose)
export BH_PRIVATE_KEY=...      # or BH_KEYPAIR_PATH=keypair.json
export BH_LIVE_TRADING=1
bh run --live
```

Live orders: PumpPortal `trade-local` builds an **unsigned** transaction
(pump.fun curve, PumpSwap and Raydium are routed automatically). It is signed
locally, so the key never leaves the process. It is sent as a **Jito bundle**
`[swap, tip]` and, as a fallback, through RPC (one signature can only land once).
The fill is read back from the chain.

## Run it 24/7 ("upload once, it runs by itself")

```bash
cp config.example.toml config.toml && cp .env.example .env   # edit both
docker compose up -d --build
docker compose logs -f
```

The `bot` service runs `bh run`: one websocket and one SQLite file in `./data`.
It records every launch, analyzes bundled ones once they are `min_age_s` old,
rebuilds clusters every `rebuild_clusters_every_s`, subscribes to the wallets of
clusters in the black, and trades. To trade live, add `--live` to `command:` and
`BH_LIVE_TRADING=1` to `.env`.

For speed, rent a VPS in the same region as a Jito block engine (Frankfurt,
Amsterdam, New York, Tokyo, Salt Lake City) and set `BH_JITO_URL` to that region,
e.g. `https://frankfurt.mainnet.block-engine.jito.wtf`.

## Strategy in detail

Watch opens when a wallet of a black cluster creates or buys a token. With
`feed.funder_lookup_on_create = true`, a fresh dev wallet funded by a known cluster
wallet also opens a watch (2 RPC calls per launch).

The holder panel is computed `panel_delay_s` (3 s) after the watch opens. That
costs about `2 + launch-window txs + up to max_quick_funders × 2` RPC calls, so raise
`requests_per_second` on a paid plan. It is refreshed every `panel_refresh_s`. While top 10,
dev, snipers, insiders or bundlers are above their `max_*_pct`, entry is blocked (the
entry window bounds the wait).

Cancelled (no entry) when:
- the token still has a mint or freeze authority;
- the bundle holds more than `max_cluster_supply_pct` of supply;
- the bundle has already sold `abort_if_cluster_sold_pct` of its bag (dump in progress);
- `entry_window_s` passes without an entry.

**Entry, dip mode:** at least `min_trades_before_entry` trades, price at least `dip_pct`
below the local high, at least `rebound_pct` up from the low after that high, and not
above `max_entry_vs_bundle` × the bundle's average entry. Limits:
`max_open_positions` and `daily_loss_limit_sol`.

**Exit:** whichever comes first:
1. the cluster sells `exit_on_cluster_sell_pct` of its bag after our entry;
2. dev + bundlers + insiders together hold `exit_on_linked_drop_pct` less than at our
   entry (catches dumps from wallets outside the copied cluster, too);
3. take profit;
4. stop loss;
5. optional trailing stop;
6. `max_hold_s`.

The backtest skips the holder panel, because stored trades can't give exact balances at
every moment.

All parameters are in `config.example.toml`.

## Code map

| file | purpose |
|---|---|
| `parser.py` | jsonParsed tx → trades / token transfers / SOL transfers / Jito tips / created mints. Works by diffing balances, not by decoding each program, so it handles pump.fun, PumpSwap, Raydium, WSOL routes and multi-signer bundle txs |
| `analyzer.py` | launch analysis (create slot, bundle, snipers), wallet profiling + funder, seeding, auto pipeline with a cheap 1-call prefilter |
| `holders.py` | Axiom-style holder panel: group classification, live balances via associated token accounts, top 10 without pools |
| `clusters.py` | union-find wallet clustering (funder, transfers, group-level co-bundling with Jaccard overlap to keep sniper bots out), per-cluster PnL / timing / partners |
| `strategy.py` | pure decision logic (same code for live, paper, backtest) |
| `execution.py` | `PaperBroker`, `LiveBroker` (PumpPortal tx + local signing + Jito bundle + RPC fallback) |
| `feeds.py` | PumpPortal websocket (new tokens, tracked wallets, watched tokens), auto-reconnect |
| `runner.py` | the 24/7 loop tying it all together |
| `backtest.py` | walk-forward replay |
| `storage.py` | SQLite schema and queries |

## Making it faster

1. Replace `PumpPortalFeed` with a **Yellowstone gRPC** (Geyser) stream from your RPC
   provider. The strategy only needs `MarketEvent`s.
2. Build pump.fun swap instructions locally instead of calling the swap API. This
   saves one HTTP round trip, but you have to follow pump.fun's program upgrades.
3. Use a low-latency "sender" endpoint from your RPC provider next to Jito, and tune
   `priority_fee_sol` / `jito_tip_sol`.
4. For the last few milliseconds, port the hot path (feed → decision → signed tx) to Rust.

## Known limitations

- A SOL transfer to another wallet inside the same transaction as a swap is counted
  as part of the swap cost.
- If a bundle wallet sends tokens to a wallet outside its cluster, those tokens count
  as "not sold", so the launch can look still open.
- The live feed covers pump.fun (PumpPortal). Other launchpads need another feed.
- Program and API changes (pump.fun, PumpPortal, Jito) can break the live path. Re-run
  the tests and paper-trade after upgrades.
