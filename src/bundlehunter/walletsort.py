"""Sort wallet lists (Axiom / GMGN wallet-tracker exports) before they go into the signal bot.

Reads Axiom's JSON export in any encoding (UTF-8, UTF-16 with BOM), or a plain list of
`address<TAB>name` lines. Wallets from several files are merged by address. Each one gets a
category from the name you (or whoever shared the list) gave it, because names like "fartcoin
dev", "Bybit", "sniper2" or "DONT COPY" already say what the wallet is:

  good      names that claim a track record: "pnl", "%wr", "120k", "1.3M+", a trailing "+", "profit", "whale"
  trader    any other real name (KOLs, traders)
  unnamed   "Rename wallet" / no name: unknown, needs the PnL check
  bundle    wallets of a named bundle group ("Yugi Bundle"): seeds for the bundle analyzer (bh seed)
  dev       dev / deployer wallets: they launch tokens, buying after them is exit liquidity
  sniper    snipers / bots / copy bots: in and out in seconds, impossible to follow
  insider   wallets marked as insiders
  avoid     marked as bad: "dont copy", "unprofitable", "scam", "mev", trash / red-flag emoji
  exchange  CEX hot wallets (Binance, Bybit, MEXC...): not traders at all

The name is only a hint. What decides is the real PnL from the chain: `bh trader rank`.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
CATEGORY_ORDER = ("good", "trader", "unnamed", "bundle", "dev", "sniper", "insider", "avoid", "exchange")
FOLLOW = ("good", "trader", "unnamed")  # worth checking / following
CATEGORY_UA = {
    "good": "заявлений плюс (pnl/wr/+ у назві)", "trader": "трейдер / KOL", "unnamed": "без назви",
    "bundle": "бандл-група", "dev": "розробник (dev)", "sniper": "снайпер / бот", "insider": "інсайдер",
    "avoid": "позначений як поганий", "exchange": "біржа (CEX)",
}

_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("exchange", re.compile(r"binance|bybit|mexc|bitget|kraken|okx|coinbase|kucoin|gate\.?io|htx|huobi|"
                            r"\bftx\b|\bcex\b|exchange|hot ?wallet", re.I)),
    ("avoid", re.compile(r"don'?t copy|unprofitable|\bscam|\brug|\bmev\b|honeypot|\bfake\b|\bfarm\b", re.I)),
    ("bundle", re.compile(r"bundl", re.I)),
    ("dev", re.compile(r"dev(?![a-z])|deploy|creator", re.I)),
    ("insider", re.compile(r"insid|insyd", re.I)),
    ("sniper", re.compile(r"snip|bot\b|buy asap", re.I)),
    ("good", re.compile(r"pnl|\d+ ?% ?(wr|hr)|copy ?trade|profit|good|\bgod\b|whale|insane|\d+(\.\d+)?\s?[km]\b|\+\s*$",
                        re.I)),
]
_EMOJI = {"🏦": "exchange", "🗑️": "avoid", "🗑": "avoid", "❌": "avoid", "⛔️": "avoid", "⛔": "avoid", "🚩": "avoid",
          "💎": "good"}
_GENERIC = re.compile(r"^(rename wallet|wallet|new wallet|untitled|unnamed|\(offensive vanity name\))?$", re.I)


@dataclass
class Wallet:
    address: str
    name: str = ""
    emoji: str = ""
    alerts: bool = False  # you had notifications on for it: you cared about it
    sources: set[str] = field(default_factory=set)
    names: list[str] = field(default_factory=list)  # every name it had across files

    @property
    def category(self) -> str:
        return classify(self.name, self.emoji)

    @property
    def label(self) -> str:
        return "" if _GENERIC.match(self.name.strip()) else self.name.strip()

    @property
    def group(self) -> str:
        """Bundle group name: 'Yugi Bundle' -> 'Yugi'."""
        return re.sub(r"\s*bundl\w*\s*", " ", self.label, flags=re.I).strip() or self.label


def classify(name: str, emoji: str = "") -> str:
    text = (name or "").strip()
    for cat, rx in _RULES[:2]:  # exchange / avoid win over everything else
        if rx.search(text):
            return cat
    if emoji.strip() in _EMOJI and _EMOJI[emoji.strip()] in ("exchange", "avoid"):
        return _EMOJI[emoji.strip()]
    for cat, rx in _RULES[2:]:
        if rx.search(text):
            return cat
    if emoji.strip() in _EMOJI:
        return _EMOJI[emoji.strip()]
    return "unnamed" if _GENERIC.match(text) else "trader"


def _decode(raw: bytes) -> str:
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8")
    if len(raw) > 1 and raw[1:2] == b"\x00":
        return raw.decode("utf-16-le")
    return raw.decode("utf-8", "replace")


def load(path: str | Path) -> list[Wallet]:
    """Axiom JSON export, a JSON list of addresses, or `address[TAB name]` lines."""
    p = Path(path)
    text = _decode(p.read_bytes()).strip()
    out: list[Wallet] = []
    if text.startswith("["):
        for item in json.loads(text):
            if isinstance(item, str):
                out.append(Wallet(item.strip()))
            elif isinstance(item, dict):
                addr = item.get("trackedWalletAddress") or item.get("address") or item.get("wallet") or ""
                alerts = any(bool(item.get(k)) for k in ("alertsOnToast", "alertsOnBubble", "alertsOnFeed"))
                out.append(Wallet(str(addr).strip(), str(item.get("name") or ""), str(item.get("emoji") or ""), alerts))
    else:
        for line in text.splitlines():
            parts = [x.strip() for x in line.replace(",", "\t", 1).split("\t")] if "\t" not in line else line.split("\t")
            if not parts or not parts[0].strip():
                continue
            w = Wallet(parts[0].strip(), parts[1].strip() if len(parts) > 1 else "",
                       parts[2].strip() if len(parts) > 2 else "")
            w.alerts = len(parts) > 3 and parts[3].strip() in ("1", "true", "yes")
            out.append(w)
    for w in out:
        w.sources.add(p.name)
        if w.name:
            w.names.append(w.name)
    return out


@dataclass
class SortResult:
    wallets: list[Wallet]
    invalid: list[str]
    duplicates: int
    total_in: int

    def by_category(self) -> dict[str, list[Wallet]]:
        groups: dict[str, list[Wallet]] = {c: [] for c in CATEGORY_ORDER}
        for w in self.wallets:
            groups[w.category].append(w)
        return groups

    def to_follow(self) -> list[Wallet]:
        return [w for w in self.wallets if w.category in FOLLOW]

    def name_counts(self) -> dict[str, int]:
        """How many wallets share a name: one person running many wallets."""
        out: dict[str, int] = {}
        for w in self.wallets:
            if w.label:
                out[w.label.lower()] = out.get(w.label.lower(), 0) + 1
        return out

    def bundle_groups(self) -> dict[str, list[Wallet]]:
        groups: dict[str, list[Wallet]] = {}
        for w in self.by_category()["bundle"]:
            groups.setdefault(w.group, []).append(w)
        return dict(sorted(groups.items(), key=lambda kv: -len(kv[1])))


def merge(lists: Iterable[list[Wallet]]) -> SortResult:
    seen: dict[str, Wallet] = {}
    invalid: list[str] = []
    total = 0
    for items in lists:
        for w in items:
            total += 1
            if not ADDRESS.match(w.address):
                invalid.append(w.address)
                continue
            cur = seen.get(w.address)
            if cur is None:
                seen[w.address] = w
                continue
            cur.sources |= w.sources
            cur.alerts = cur.alerts or w.alerts
            cur.names += [n for n in w.names if n not in cur.names]
            # keep the most telling name: a real one over "Rename wallet", a flagged one over a plain one
            if (not cur.label and w.label) or (classify(w.name, w.emoji) in ("avoid", "exchange")):
                cur.name, cur.emoji = w.name, w.emoji
    rank = {c: i for i, c in enumerate(CATEGORY_ORDER)}
    wallets = sorted(seen.values(), key=lambda w: (rank[w.category], not w.alerts, w.label.lower() or "~", w.address))
    return SortResult(wallets, invalid, total - len(invalid) - len(seen), total)


def write_csv(result: SortResult, path: str | Path) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:  # BOM: opens correctly in Excel
        wr = csv.writer(fh)
        wr.writerow(["category", "category_ua", "address", "name", "wallets_with_this_name", "other_names",
                     "alerts_on", "sources"])
        same = result.name_counts()
        for w in result.wallets:
            wr.writerow([w.category, CATEGORY_UA[w.category], w.address, w.label, same.get(w.label.lower(), 1) if w.label else "",
                         " | ".join(n for n in w.names if n != w.name), int(w.alerts), " ".join(sorted(w.sources))])
