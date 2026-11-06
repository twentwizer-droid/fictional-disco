"""Learn entry rules and a take-profit level from your own exported trades.

Put CSV / TSV / JSON / JSONL files in a folder (default `export/`) and run `bh learn`.
Each row is one trade (or one token you looked at) with a result: pnl %, profit SOL, ROI, or "x".
Any other numeric column the file has becomes a feature: market cap at entry, token age,
holders, bundlers %, insiders %, top-10 %, dev %, snipers %, smart wallets, buy volume, ...
Column names are matched loosely (English / Russian / Ukrainian, any case).

How it learns, and why it should not fool you:
  * rows are sorted by time; the OLDEST 70% are for learning, the NEWEST 30% only for judging;
  * rules have the form "feature <= X" or "feature >= X" with X at a quantile of the data,
    at most 3 rules, and each kept rule must leave >= 25% of the learning rows (no 5-trade magic);
  * a rule is kept only if it raises the average result on the learning part by >= 2 points;
  * if a "peak" column exists (max x / ATH after entry), the take-profit that would have caught
    the most is chosen on the learning part too (a TP not reached falls back to the real result);
  * the strategy is PROMOTED only if on the unseen 30% it has >= 8 trades, a positive average,
    a positive median and beats taking every trade. Otherwise it is saved as a report only.

A promoted strategy becomes an extra filter for the scalper (rules on features it knows live).
Wallet lists without results (Axiom exports) are not trades: they are reported and can be
imported with `bh trader import`.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

LEARNED_FILE = "data/learned_strategy.json"
EXTS = {".csv", ".tsv", ".json", ".jsonl", ".txt"}

# canonical feature -> name fragments (lower case, spaces/underscores removed before matching)
ALIASES: dict[str, tuple[str, ...]] = {
    "pnl_pct": ("pnl%", "pnlpct", "pnlpercent", "roi", "return", "profit%", "result%", "прибыль%", "прибуток%",
                "результат%", "доходность"),
    "pnl_x": ("multiple", "resultx", "exitx", "иксы"),
    "pnl_sol": ("pnlsol", "profitsol", "pnl", "profit", "прибыль", "прибуток", "результат", "realized"),
    "peak_x": ("peakx", "maxx", "athx", "peak", "ath", "максx", "пик", "пік"),
    "mcap": ("entrymcap", "mcapentry", "marketcap", "mcap", "mc", "капа", "капитализация", "капіталізація"),
    "age_s": ("tokenage", "age", "возраст", "вік"),
    "holders": ("holders", "холдеры", "холдери", "холдеров"),
    "bundlers_pct": ("bundlers", "bundle", "бандл"),
    "insiders_pct": ("insiders", "insider", "инсайд", "інсайд"),
    "snipers_pct": ("snipers", "sniper", "снайп"),
    "dev_pct": ("devhold", "dev%", "devpct", "dev", "дев"),
    "top10_pct": ("top10", "топ10"),
    "smart_count": ("smart", "kol", "trackers", "трекер", "смарт"),
    "buyers": ("buyers", "покупател", "покупц"),
    "buy_sol": ("buyvol", "volume", "vol", "объем", "обсяг"),
    "liquidity": ("liquidity", "liq", "ликвид", "ліквід"),
    "hold_s": ("holdtime", "hold", "duration", "удерж", "тримав"),
    "size_sol": ("sizesol", "amountsol", "insol", "buysol", "invested", "вложено", "сума"),
    "larp_score": ("larp",),
}
TIME_COLS = ("time", "date", "timestamp", "opened", "entrytime", "buytime", "ts", "время", "дата", "час")
MINT_COLS = ("mint", "token", "ca", "address", "contract", "coin", "токен", "монета", "адрес")
NOT_FEATURES = {"pnl_pct", "pnl_x", "pnl_sol", "peak_x", "hold_s", "size_sol"}
# features the scalper knows at the moment it decides (others are advice for the overlay only)
# (market cap and volume are left out: exports give them in USD, the bot sees SOL)
LIVE_FEATURES = {"bundlers_pct", "insiders_pct", "snipers_pct", "dev_pct", "top10_pct", "larp_score",
                 "smart_count", "buyers"}
PCT_FEATURES = {"bundlers_pct", "insiders_pct", "snipers_pct", "dev_pct", "top10_pct"}

LABELS = {
    "mcap": "капа на вході", "age_s": "вік токена, с", "holders": "холдерів", "bundlers_pct": "бандлери",
    "insiders_pct": "інсайдери", "snipers_pct": "снайпери", "dev_pct": "дев тримає", "top10_pct": "топ-10",
    "smart_count": "сильних гаманців", "buyers": "покупців", "buy_sol": "обсяг купівель", "liquidity": "ліквідність",
    "larp_score": "LARP-оцінка",
}

_SUFFIX = {"k": 1e3, "к": 1e3, "m": 1e6, "м": 1e6, "b": 1e9, "млрд": 1e9}
_NUM = re.compile(r"^([+-]?\d+(?:[.,]\d+)?)\s*(k|к|m|м|b|млрд)?\s*(%|x|х)?$")
_DUR = re.compile(r"(\d+(?:[.,]\d+)?)\s*(d|д|h|ч|г|m|м|мин|хв|s|с|сек)\b")


def _key(name: str) -> str:
    return re.sub(r"[\s_\-()\[\]./]", "", name.lower())


def parse_number(v: Any) -> float | None:
    """'$12.3K' -> 12300, '45%' -> 45, '3x' -> 3, '-0,5' -> -0.5. Returns None for text."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    s = str(v).strip().lower().replace("$", "").replace("◎", "").replace("sol", "").replace(" ", "").replace(" ", "")
    if s.count(",") == 1 and "." not in s:
        s = s.replace(",", ".")
    s = s.replace(",", "")
    m = _NUM.match(s)
    if not m:
        return None
    x = float(m.group(1).replace(",", "."))
    return x * _SUFFIX.get(m.group(2) or "", 1.0)


def parse_duration(v: Any) -> float | None:
    """'5m' / '2h 3m' / '40s' / plain seconds -> seconds."""
    n = parse_number(v)
    if n is not None and not re.search(r"[a-zа-я]", str(v).lower().replace("e", "")):
        return n
    parts = _DUR.findall(str(v).lower())
    if not parts:
        return n
    mult = {"d": 86400, "д": 86400, "h": 3600, "ч": 3600, "г": 3600, "m": 60, "м": 60, "мин": 60, "хв": 60}
    return sum(float(a.replace(",", ".")) * mult.get(u, 1) for a, u in parts)


def parse_time(v: Any) -> float | None:
    n = parse_number(v)
    if n is not None:
        return n / 1000 if n > 1e12 else n if n > 1e8 else None
    s = str(v).strip().replace("Z", "+00:00")
    for fmt in (None, "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M"):
        try:
            return (datetime.fromisoformat(s) if fmt is None else datetime.strptime(s, fmt)).timestamp()
        except ValueError:
            continue
    return None


# -- loading ----------------------------------------------------------------------------------

def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-16", "cp1251", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _flatten(d: dict, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(_flatten(v, f"{prefix}{k}_"))
        elif not isinstance(v, list):
            out[f"{prefix}{k}"] = v
    return out


def read_rows(path: Path) -> list[dict[str, Any]]:
    text = _read_text(path).strip()
    if not text:
        return []
    if text[0] in "[{":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = [json.loads(line) for line in text.splitlines() if line.strip().startswith("{")]
        if isinstance(data, dict):  # {"trades": [...]} or {mint: {...}}
            lists = [v for v in data.values() if isinstance(v, list) and v and isinstance(v[0], dict)]
            data = lists[0] if lists else [{"mint": k, **v} for k, v in data.items() if isinstance(v, dict)]
        return [_flatten(r) for r in data if isinstance(r, dict)]
    first = text.splitlines()[0]
    delim = "\t" if "\t" in first else ";" if first.count(";") > first.count(",") else ","
    return [dict(r) for r in csv.DictReader(io.StringIO(text), delimiter=delim)]


def map_columns(columns: Iterable[str]) -> dict[str, str]:
    """canonical name -> column in the file. Longer aliases win, each column is used once."""
    cols = list(columns)
    keys = {c: _key(c) for c in cols}
    out: dict[str, str] = {}
    pairs = sorted(((len(a), canon, a) for canon, al in ALIASES.items() for a in al), reverse=True)
    used: set[str] = set()
    for _, canon, alias in pairs:
        if canon in out:
            continue
        for c in cols:
            if c in used:
                continue
            k = keys[c]
            if k == alias or (len(alias) >= 3 and alias in k):
                out[canon] = c
                used.add(c)
                break
    for canon, al in (("_time", TIME_COLS), ("_mint", MINT_COLS)):
        for c in cols:
            if c not in used and any(keys[c] == a or (len(a) >= 4 and a in keys[c]) for a in al):
                out[canon] = c
                used.add(c)
                break
    return out


@dataclass
class Sample:
    ts: float
    mint: str
    pnl_pct: float  # % result of the trade, -100..+inf
    peak_x: float | None
    features: dict[str, float]
    source: str


@dataclass
class Dataset:
    samples: list[Sample] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)  # file -> what was found


def _pct(v: float | None, canon: str) -> float | None:
    if v is None:
        return None
    if canon in PCT_FEATURES:
        return v / 100 if v > 1 else v  # store as fraction, like the holder panel
    return v


def to_samples(rows: list[dict[str, Any]], source: str) -> tuple[list[Sample], str]:
    if not rows:
        return [], "порожній"
    cols = map_columns(rows[0].keys())
    has_result = any(k in cols for k in ("pnl_pct", "pnl_x", "pnl_sol"))
    if not has_result:
        addr = sum(1 for r in rows for v in r.values() if isinstance(v, str) and 32 <= len(v.strip()) <= 44)
        return [], ("список гаманців без результатів: імпортуй через `bh trader import`" if addr
                    else "немає колонки з результатом (pnl %, profit, roi, x)")
    out: list[Sample] = []
    for i, r in enumerate(rows):
        pnl: float | None = None
        if "pnl_pct" in cols:
            pnl = parse_number(r.get(cols["pnl_pct"]))
        if pnl is None and "pnl_x" in cols:
            x = parse_number(r.get(cols["pnl_x"]))
            pnl = (x - 1) * 100 if x is not None else None
        if pnl is None and "pnl_sol" in cols and "size_sol" in cols:
            p, s = parse_number(r.get(cols["pnl_sol"])), parse_number(r.get(cols["size_sol"]))
            pnl = p / s * 100 if p is not None and s else None
        if pnl is None or pnl < -100:
            continue
        feats: dict[str, float] = {}
        for canon, col in cols.items():
            if canon.startswith("_") or canon in NOT_FEATURES:
                continue
            raw = r.get(col)
            val = parse_duration(raw) if canon == "age_s" else parse_number(raw)
            val = _pct(val, canon)
            if val is not None:
                feats[canon] = val
        ts = parse_time(r.get(cols["_time"])) if "_time" in cols else None
        peak = parse_number(r.get(cols["peak_x"])) if "peak_x" in cols else None
        out.append(Sample(ts if ts is not None else float(i), str(r.get(cols.get("_mint", ""), "") or ""),
                          pnl, peak, feats, source))
    found = ", ".join(sorted(k for k in cols if not k.startswith("_")))
    return out, f"{len(out)} угод; колонки: {found}"


def load_dir(path: str | Path) -> Dataset:
    root = Path(path)
    if not root.exists():
        ds = Dataset()
        ds.files[str(root)] = "папки немає: створи її й поклади туди експорт угод"
        return ds
    files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.suffix.lower() in EXTS)
    ds = Dataset()
    for f in files:
        try:
            samples, note = to_samples(read_rows(f), f.name)
        except (ValueError, csv.Error, json.JSONDecodeError) as exc:
            samples, note = [], f"не прочитав: {exc}"
        ds.samples.extend(samples)
        ds.files[str(f)] = note
    ds.samples.sort(key=lambda s: s.ts)
    return ds


# -- learning ---------------------------------------------------------------------------------

@dataclass
class Rule:
    feature: str
    op: str  # "<=" | ">="
    value: float

    def ok(self, feats: dict[str, Any]) -> bool | None:
        v = feats.get(self.feature)
        if v is None:
            return None  # unknown: the rule cannot judge
        return v <= self.value if self.op == "<=" else v >= self.value

    def text(self) -> str:
        v = f"{self.value:.0%}" if self.feature in PCT_FEATURES else f"{self.value:,.4g}"
        return f"{LABELS.get(self.feature, self.feature)} {'≤' if self.op == '<=' else '≥'} {v}"

    def to_dict(self) -> dict[str, Any]:
        return {"feature": self.feature, "op": self.op, "value": self.value, "text": self.text(),
                "live": self.feature in LIVE_FEATURES}


def _passes(s: Sample, rules: list[Rule]) -> bool:
    return all(r.ok(s.features) is not False for r in rules)


def _result(s: Sample, tp_pct: float | None) -> float:
    if tp_pct is not None and s.peak_x is not None and (s.peak_x - 1) * 100 >= tp_pct:
        return tp_pct
    return s.pnl_pct


def stats(samples: list[Sample], rules: list[Rule], tp_pct: float | None = None, cost_pct: float = 0.0) -> dict[str, Any]:
    res = [_result(s, tp_pct) - cost_pct for s in samples if _passes(s, rules)]
    if not res:
        return {"n": 0, "avg": None, "median": None, "win_rate": None, "share": 0.0}
    return {"n": len(res), "avg": statistics.fmean(res), "median": statistics.median(res),
            "win_rate": sum(r > 0 for r in res) / len(res), "share": len(res) / max(1, len(samples))}


def _quantiles(vals: list[float]) -> list[float]:
    vals = sorted(set(vals))
    if len(vals) < 3:
        return vals
    return sorted({vals[int(q * (len(vals) - 1))] for q in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)})


def learn_rules(train: list[Sample], max_rules: int = 3, min_keep: float = 0.25, min_gain: float = 2.0) -> list[Rule]:
    rules: list[Rule] = []
    floor = max(15, int(len(train) * min_keep))
    for _ in range(max_rules):
        base = stats(train, rules)
        best: tuple[float, Rule] | None = None
        feats = sorted({f for s in train for f in s.features} - {r.feature for r in rules})
        for f in feats:
            vals = [s.features[f] for s in train if f in s.features and _passes(s, rules)]
            if len(vals) < floor:
                continue
            for q in _quantiles(vals):
                for op in ("<=", ">="):
                    cand = Rule(f, op, q)
                    st = stats(train, rules + [cand])
                    if st["n"] < floor or st["avg"] is None:
                        continue
                    if best is None or st["avg"] > best[0]:
                        best = (st["avg"], cand)
        if best is None or base["avg"] is None or best[0] < base["avg"] + min_gain:
            break
        rules.append(best[1])
    return rules


def learn_tp(train: list[Sample], rules: list[Rule]) -> float | None:
    """Take-profit (%) that would have earned most on the learning part, if peaks are known."""
    picked = [s for s in train if _passes(s, rules) and s.peak_x is not None]
    if len(picked) < 15:
        return None
    best_tp, best = None, stats(picked, [])["avg"]
    for tp in (15, 20, 25, 30, 40, 50, 60, 75, 100, 150, 200, 300):
        avg = stats(picked, [], tp)["avg"]
        if avg is not None and (best is None or avg > best):
            best_tp, best = float(tp), avg
    return best_tp


def learn(ds: Dataset, split: float = 0.7, cost_pct: float = 0.0, min_test: int = 8) -> dict[str, Any]:
    s = ds.samples
    out: dict[str, Any] = {"created": time.time(), "files": ds.files, "samples": len(s), "promoted": False,
                           "rules": [], "tp_pct": None, "train": None, "test": None, "baseline_test": None,
                           "why": ""}
    if len(s) < 30:
        out["why"] = f"замало угод для навчання: {len(s)} (треба ≥ 30, краще 200+)"
        return out
    cut = int(len(s) * split)
    train, test = s[:cut], s[cut:]
    rules = learn_rules(train)
    tp = learn_tp(train, rules)
    tr, te = stats(train, rules, tp, cost_pct), stats(test, rules, tp, cost_pct)
    base = stats(test, [], None, cost_pct)
    out.update(rules=[r.to_dict() for r in rules], tp_pct=tp, train=tr, test=te, baseline_test=base,
               baseline_train=stats(train, [], None, cost_pct), split=split)
    problems = []
    if te["n"] < min_test:
        problems.append(f"на нових даних лише {te['n']} угод (треба ≥ {min_test})")
    if te["avg"] is None or te["avg"] <= 0:
        problems.append("на нових даних середня угода не в плюсі")
    if te["median"] is None or te["median"] <= 0:
        problems.append("медіанна угода на нових даних не в плюсі")
    if te["avg"] is not None and base["avg"] is not None and te["avg"] <= base["avg"]:
        problems.append("правила не кращі, ніж брати все підряд")
    if not rules and tp is None:
        problems.append("не знайшов жодного правила, що стабільно допомагає")
    out["promoted"] = not problems
    out["why"] = "; ".join(problems) or "перевірено на останніх 30% угод, яких навчання не бачило"
    return out


def save(result: dict[str, Any], path: str = LEARNED_FILE) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def load_learned(path: str = LEARNED_FILE) -> dict[str, Any] | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def live_rules(result: dict[str, Any] | None) -> list[Rule]:
    """Rules the scalper applies: only from a promoted result, only on features it knows live."""
    if not result or not result.get("promoted"):
        return []
    return [Rule(r["feature"], r["op"], float(r["value"])) for r in result.get("rules", []) if r.get("live")]


def _fmt(st: dict[str, Any] | None) -> str:
    if not st or not st.get("n"):
        return "0 угод"
    return (f"{st['n']} угод, середня {st['avg']:+.1f}%, медіана {st['median']:+.1f}%, "
            f"у плюсі {st['win_rate']:.0%}")


def format_result(r: dict[str, Any]) -> str:
    lines = ["Навчання на експорті", ""]
    for f, note in r.get("files", {}).items():
        lines.append(f"  {Path(f).name}: {note}")
    lines.append(f"Усього угод: {r.get('samples', 0)}")
    if r.get("train") is None:
        lines += ["", r.get("why", "")]
        return "\n".join(lines)
    lines += ["", "Правила входу:"]
    lines += [f"  • {x['text']}" + ("" if x.get("live") else "  (бот не знає цього наживо: підказка в оверлеї)")
              for x in r["rules"]] or ["  (жодне правило не допомогло стабільно)"]
    if r.get("tp_pct"):
        lines.append(f"Тейк-профіт: +{r['tp_pct']:.0f}%")
    lines += ["", f"Навчання (старі {r['split']:.0%}): {_fmt(r['train'])}",
              f"   без правил: {_fmt(r.get('baseline_train'))}",
              f"Перевірка (нові {1 - r['split']:.0%}): {_fmt(r['test'])}",
              f"   без правил: {_fmt(r['baseline_test'])}", "",
              ("✅ СТРАТЕГІЮ ПРИЙНЯТО: " if r["promoted"] else "⛔ НЕ ПРИЙНЯТО: ") + r["why"],
              "Минулі результати не гарантують майбутніх. Не фінансова порада."]
    return "\n".join(lines)
