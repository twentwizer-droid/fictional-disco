"""Telegram bot: check tokens, analyze narratives, get trade alerts, all from your phone.

Uses the plain Bot API over HTTPS (long polling), no extra library. Only chat ids listed in
BH_TELEGRAM_ALLOWED_IDS get answers; anyone else is told their own id and nothing more, so
nobody can spend your RPC credits or read your positions.
"""

from __future__ import annotations

import asyncio
import html
import os
import tempfile
import logging
import time
from typing import Any

import httpx

from .clusters import ClusterStat
from .config import SelectionConfig
from .i18n import verdict_ua
from .larp import SOL_ADDRESS, _ago_ua, _short
from .narrative import Member
from .service import CheckResult, NarrativeResult, Service
from .traders import TraderStats
from .walletsort import CATEGORY_ORDER, CATEGORY_UA, FOLLOW, SortResult

log = logging.getLogger(__name__)

MAX_MESSAGE = 3800  # Telegram limit is 4096 characters
LEVEL_ICON = {"red": "🔴", "yellow": "🟡", "green": "🟢", "info": "ℹ️"}
NARRATIVE_UA = {"ORIGINAL": "оригінал", "COPY": "копія", "LARP": "LARP", "SIDE TOKEN": "побічний",
                "FIRST, SUSPICIOUS": "перший, підозрілий"}
BUNDLE_UA = {"PROFITABLE BUNDLER": "прибуткова бандл-група", "HEAVY BUNDLE": "важкий бандл",
             "BUNDLE SOLD": "бандл продав", "CLEAN": "чисто", "MODERATE": "помірно", "-": "-"}

HELP = """<b>bundlehunter</b> 🤖

<b>/check</b> &lt;адреса&gt; — LARP-перевірка + холдери (як на Axiom)
<b>/narrative</b> &lt;наратив&gt; — усі токени наратива: оригінал, копії, бандли
<b>/watch</b> &lt;наратив&gt; — повідомляти про нові токени під наратив
<b>/unwatch</b> — зупинити стеження
<b>/clusters</b> — групи бандлерів «в плюсі»
<b>/cluster</b> &lt;id або гаманець&gt; — деталі групи
<b>/positions</b> — угоди бота
<b>/status</b> — стан бота

<b>Сигнали від трейдерів</b>
<b>/addtrader</b> &lt;гаманець&gt; [назва] — додати трейдера, бот пише про кожну його купівлю
<b>/traders</b> — твої трейдери з їхнім PnL
<b>/trader</b> &lt;гаманець&gt; — перевірити будь-який гаманець: чи він у плюсі
<b>/deltrader</b> &lt;гаманець&gt; — прибрати трейдера
<b>Надішли файл</b> (експорт гаманців з Axiom / GMGN, .txt чи .json) — бот відсортує й додасть трейдерів
<b>/rank</b> [скільки] — завантажити історію і відсортувати трейдерів за реальним PnL

Можна просто надіслати адресу токена або слова наратива."""


def e(x: Any) -> str:
    return html.escape(str(x), quote=False)


def _pct(x: float | None) -> str:
    return f"{x:.1%}" if x is not None else "-"


def fmt_check(r: CheckResult, sel: SelectionConfig) -> str:
    rep, m = r.report, r.report.meta
    score = rep.score
    icon = "🔴" if score >= 60 else "🟡" if score >= 30 else "🟢"
    lines = [f"<b>{e(m.symbol or '?')}</b> {e(m.name)}", f"<code>{e(r.mint)}</code>",
             f"{icon} <b>Ризик {score}/100</b> — {e(verdict_ua(score))}"]
    links = [f"{k}: {e(v)}" for k, v in (("X", m.twitter), ("TG", m.telegram), ("сайт", m.website)) if v]
    if links:
        lines.append(" · ".join(links))
    if r.panel:
        met, p = r.panel.metrics(), r.panel
        lines += [
            "",
            "<b>Холдери</b> (тримають зараз / на старті)",
            f"Top 10: {_pct(met['top10_pct'])}",
            f"Dev: {_pct(met['dev_pct'])} / {_pct(p.pct(p.groups['dev'].initial))}",
            f"Bundlers: {_pct(met['bundlers_pct'])} / {_pct(p.pct(p.groups['bundlers'].initial))} "
            f"({len(p.groups['bundlers'].wallets)} гам.)",
            f"Insiders: {_pct(met['insiders_pct'])} ({len(p.groups['insiders'].wallets)} гам.)",
            f"Snipers: {_pct(met['snipers_pct'])} ({len(p.groups['snipers'].wallets)} гам.)",
        ]
    elif r.note:
        lines += ["", f"<i>{e(r.note)}</i>"]
    if r.cluster:
        c = r.cluster
        lines += ["", f"<b>Група розробника</b> {e(c.cluster_id)}: {len(c.launches)} запусків, "
                      f"{c.win_rate:.0%} в плюс, {c.realized_pnl:+.1f} SOL"
                      + (" — <b>в плюсі, бот її копіює</b>" if r.cluster_black else "")]
    order = {"red": 0, "yellow": 1, "green": 2, "info": 3}
    if rep.findings:
        lines += ["", "<b>Знахідки</b>"]
        lines += [f"{LEVEL_ICON[f.level]} {e(f.text_ua)}" for f in sorted(rep.findings, key=lambda f: order[f.level])]
    if rep.ai:
        lines += ["", f"<b>ШІ:</b> {e(str(rep.ai.get('risk', '?')).upper())} — {e(rep.ai.get('summary', ''))}"]
    return "\n".join(lines)


def fmt_member(m: Member, rank: int | None = None) -> str:
    met = m.metrics or {}
    head = f"{rank}. " if rank else ""
    mcap = f" · ${m.market_cap / 1e3:.0f}k" if m.market_cap else ""
    dev = ""
    if m.cluster:
        dev = f"\n   група {e(m.cluster.cluster_id)} {m.cluster.realized_pnl:+.1f} SOL" + (" (в плюсі)" if m.cluster_black else "")
    return (
        f"{head}<b>{e(m.symbol or m.name or '?')}</b> <code>{e(m.mint)}</code>\n"
        f"   {e(_ago_ua(m.create_time))}{mcap} · LARP {m.larp.score if m.larp else '-'} · "
        f"<b>{e(NARRATIVE_UA.get(m.narrative_label, m.narrative_label))}</b>\n"
        f"   бандл {_pct(met.get('bundlers_pct'))} (старт {_pct(m.linked_initial_pct)}) · "
        f"інсайдери {_pct(met.get('insiders_pct'))} · {e(BUNDLE_UA.get(m.bundle_label, m.bundle_label))}{dev}"
    )


def fmt_narrative(r: NarrativeResult) -> str:
    analyzed = sorted([m for m in r.members if m.larp or m.metrics], key=lambda m: m.create_time or float("inf"))
    lines = [f"<b>Наратив «{e(r.query.label)}»</b>: знайдено {len(r.members)}, проаналізовано {len(analyzed)}", ""]
    lines += [fmt_member(m, i) for i, m in enumerate(analyzed, 1)]
    best, why = r.pick
    lines += ["", f"🎯 <b>Вибір:</b> {e(why)}" if best else f"🚫 {e(why)}"]
    bad = [m for m in analyzed if m.narrative_label in ("LARP", "COPY")]
    if bad:
        lines.append("⛔ <b>Уникати:</b> " + ", ".join(f"{e(m.symbol or '?')} <code>{e(_short(m.mint))}</code>" for m in bad))
    return "\n".join(lines)


def fmt_clusters(rows: list[ClusterStat]) -> str:
    if not rows:
        return "Поки немає груп «в плюсі». Бот має попрацювати, щоб назбирати запуски (або додай відомі гаманці: bh seed)."
    lines = ["<b>Групи бандлерів «в плюсі»</b>", ""]
    for c in rows:
        mfs = f"{c.median_first_sell_s:.0f}с" if c.median_first_sell_s is not None else "-"
        lines.append(f"<b>{e(c.cluster_id)}</b>: {len(c.launches)} запусків · {c.win_rate:.0%} · "
                     f"{c.realized_pnl:+.1f} SOL · перший продаж {mfs} · {len(c.wallets)} гам.")
    return "\n".join(lines)


def fmt_cluster(c: ClusterStat, sel: SelectionConfig) -> str:
    reasons = c.black_reasons(sel)
    hours = ", ".join(f"{h}:00" for h, _ in c.active_hours.most_common(3)) or "-"
    lines = [
        f"<b>{e(c.cluster_id)}</b> — " + ("в плюсі ✅" if not reasons else "не копіюється: " + e("; ".join(reasons))),
        f"{len(c.wallets)} гаманців · {len(c.launches)} запусків ({c.created} створили самі)",
        f"PnL {c.realized_pnl:+.2f} SOL · відкрито {c.unrealized_pnl:+.2f} SOL · в плюс {c.win_rate:.0%}",
        f"Коли: частіше о {hours} UTC · перший продаж через {c.median_first_sell_s}с · вихід через {c.median_exit_s}с",
        f"З ким: {e(', '.join(k for k, _ in c.partners.most_common(5)) or '-')}",
        "",
        "<b>Токени</b>",
    ]
    for ls in sorted(c.launches, key=lambda x: x.created_at or 0, reverse=True)[:10]:
        lines.append(f"<code>{e(_short(ls.mint))}</code> {e(_ago_ua(ls.created_at))} · вклали {ls.sol_in:.2f} · "
                     f"PnL {ls.pnl:+.2f}{'' if ls.closed else ' (ще тримають)'}")
    lines += ["", "<b>Гаманці</b>"] + [f"<code>{e(w)}</code>" for w in c.wallets[:15]]
    if len(c.wallets) > 15:
        lines.append(f"… і ще {len(c.wallets) - 15}")
    return "\n".join(lines)


def fmt_positions(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "Угод ще не було."
    total = sum(r["pnl_sol"] or 0 for r in rows if r["closed_at"])
    lines = [f"<b>Угоди</b> (останні {len(rows)}), PnL закритих {total:+.4f} SOL", ""]
    for r in rows[:20]:
        mode = "LIVE" if r["live"] else "paper"
        state = f"PnL {r['pnl_sol']:+.4f}" if r["closed_at"] else "<b>відкрита</b>"
        lines.append(f"{e(r.get('symbol') or '?')} <code>{e(_short(r['mint']))}</code> · {mode} · "
                     f"{r['size_sol']:.3f} SOL · {state}\n   {e(r.get('reason_open_ua') or r['reason_open'] or '')} → "
                     f"{e(r.get('reason_close_ua') or r['reason_close'] or '')}")
    return "\n".join(lines)


def fmt_status(s: dict[str, Any]) -> str:
    tokens = s["tokens"]
    mode = {"live": "🔴 LIVE (реальні гроші)", "paper": "📝 paper (без грошей)", "off": "вимкнено"}[s["mode"]]
    lines = [
        "<b>Стан бота</b>",
        f"Торгівля: {mode}",
        f"Працює: {s['uptime_s'] / 3600:.1f} год",
        f"Записано запусків: {sum(tokens.values())} (проаналізовано {tokens.get('ok', 0)}, "
        f"пропущено {tokens.get('skipped', 0)}, в черзі {tokens.get('pending', 0)})",
        f"Груп: {s['clusters']}, в плюсі: {s['black_clusters']}, стежу за {s['tracked_wallets']} гаманцями",
        f"Позиції: відкрито {s['positions_open']}, закрито {s['positions_closed']}",
        f"PnL: paper {s['realized_pnl_sol']['paper']:+.4f} SOL · live {s['realized_pnl_sol']['live']:+.4f} SOL",
        f"Твоїх трейдерів: {s.get('traders', 0)} · сигналів надіслано: {s.get('signals_sent', 0)}",
    ]
    if s["watching"]:
        lines += ["", "<b>Зараз стежу</b>"]
        for w in s["watching"][:10]:
            state = "у позиції" if w["in_position"] else (f"чекаю: {e(w.get('blocked_ua') or w['blocked'])}" if w["blocked"] else "чекаю входу")
            lines.append(f"{e(w['symbol'] or '?')} <code>{e(_short(w['mint']))}</code> · {e(w['cluster_id'])} · {state}")
    if s["narrative_watches"]:
        lines += ["", "Наративи: " + e(", ".join(s["narrative_watches"]))]
    return "\n".join(lines)


def fmt_trader(st: TraderStats) -> str:
    icon = {"в плюсі": "✅", "в мінусі": "🔻"}.get(st.verdict, "❔")
    hold = f"{st.median_hold_s / 60:.0f} хв" if st.median_hold_s is not None else "–"
    lines = [
        f"👤 <b>{e(st.name)}</b> <code>{e(st.address)}</code>",
        f"{icon} <b>{e(st.verdict)}</b> · PnL {st.realized_pnl:+.2f} SOL · {st.win_rate:.0%} в плюс",
        f"Токенів {len(st.tokens)}, закрито {len(st.closed)} · середній вхід {st.avg_buy_sol:.2f} SOL · тримає ~{hold}",
    ]
    recent = sorted(st.tokens, key=lambda t: t.first_buy or 0, reverse=True)[:8]
    if recent:
        lines += ["", "<b>Останні токени</b>"]
        for t in recent:
            state = f"{t.pnl:+.2f} SOL" if t.closed else "ще тримає"
            lines.append(f"<code>{e(_short(t.mint))}</code> {e(_ago_ua(t.first_buy))} · вклав {t.sol_in:.2f} · {state}")
    return "\n".join(lines)


def fmt_traders(rows: list[TraderStats]) -> str:
    if not rows:
        return "Трейдерів ще немає. Додай: /addtrader &lt;гаманець&gt; [назва]"
    lines = [f"<b>Твої трейдери</b> ({len(rows)}, найкращі зверху)", ""]
    for st in rows[:40]:
        icon = {"в плюсі": "✅", "в мінусі": "🔻"}.get(st.verdict, "❔")
        lines.append(f"{icon} <b>{e(st.name)}</b> <code>{e(_short(st.address))}</code> · {st.realized_pnl:+.1f} SOL · "
                     f"{st.win_rate:.0%} · {len(st.closed)} угод")
    if len(rows) > 40:
        lines.append(f"… і ще {len(rows) - 40}. Повний список: веб-панель, вкладка «Трейдери».")
    return "\n".join(lines)


def split_message(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """Split on line boundaries so HTML tags (always closed within a line) stay intact."""
    parts: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts


class TelegramBot:
    def __init__(
        self,
        token: str,
        allowed_ids: set[int],
        service: Service,
        api_url: str = "https://api.telegram.org",
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.token, self.allowed, self.service = token, allowed_ids, service
        self.base = f"{api_url.rstrip('/')}/bot{token}"
        self.http = http or httpx.AsyncClient(timeout=70)
        self._tasks: set[asyncio.Task] = set()
        service.signals.send = self.notify_html

    @staticmethod
    def parse_ids(raw: str | None) -> set[int]:
        out = set()
        for part in (raw or "").replace(";", ",").split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                out.add(int(part))
        return out

    async def api(self, method: str, **params: Any) -> Any:
        resp = await self.http.post(f"{self.base}/{method}", json=params)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method}: {data.get('description')}")
        return data.get("result")

    async def send(self, chat_id: int, text: str) -> None:
        for part in split_message(text):
            try:
                await self.api("sendMessage", chat_id=chat_id, text=part, parse_mode="HTML",
                               disable_web_page_preview=True)
            except Exception as exc:
                log.warning("telegram send failed: %s", exc)

    async def notify(self, text: str) -> None:
        for chat_id in self.allowed:
            await self.send(chat_id, e(text))

    async def notify_html(self, text: str) -> None:
        """Signals: already formatted (and escaped) HTML."""
        for chat_id in self.allowed:
            await self.send(chat_id, text)

    async def run(self) -> None:
        me = await self.api("getMe")
        log.info("telegram bot @%s is up (%d allowed chats)", me.get("username"), len(self.allowed))
        offset, backoff = None, 1.0
        while True:
            try:
                params: dict[str, Any] = {"timeout": 50, "allowed_updates": ["message"]}
                if offset is not None:
                    params["offset"] = offset
                updates = await self.api("getUpdates", **params)
                backoff = 1.0
            except Exception as exc:
                log.warning("telegram polling: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            for u in updates or []:
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                if msg.get("text") or msg.get("document"):
                    task = asyncio.create_task(self.handle(msg))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)

    async def handle(self, msg: dict[str, Any]) -> None:
        chat_id = msg["chat"]["id"]
        user_id = (msg.get("from") or {}).get("id")
        if chat_id not in self.allowed and user_id not in self.allowed:
            await self.send(chat_id, (
                f"🔒 Доступ закритий. Твій ID: <code>{chat_id}</code>\n"
                "Додай його в .env як BH_TELEGRAM_ALLOWED_IDS і перезапусти бота."
            ))
            return
        if msg.get("document"):
            try:
                await self.import_document(chat_id, msg["document"])
            except Exception as exc:
                log.exception("telegram import failed")
                await self.send(chat_id, f"❌ Не вдалося прочитати файл: {e(exc)}")
            return
        text = msg["text"].strip()
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower() if cmd.startswith("/") else ""
        arg = arg.strip() if cmd else text
        try:
            await self.dispatch(chat_id, cmd, arg)
        except ValueError as exc:
            await self.send(chat_id, f"⚠️ {e(exc)}")
        except Exception as exc:
            log.exception("telegram command %s failed", cmd)
            await self.send(chat_id, f"❌ Помилка: {e(exc)}")

    async def dispatch(self, chat_id: int, cmd: str, arg: str) -> None:
        sel = self.service.cfg.selection
        if cmd in ("/start", "/help"):
            await self.send(chat_id, HELP)
        elif cmd == "/check" or (not cmd and SOL_ADDRESS.fullmatch(arg)):
            if not arg:
                raise ValueError("додай адресу: /check <адреса>")
            await self.send(chat_id, "⏳ Перевіряю токен, це до хвилини…")
            await self.send(chat_id, fmt_check(await self.service.check(arg), sel))
        elif cmd == "/narrative" or (not cmd and arg):
            if not arg:
                raise ValueError("додай наратив: /narrative trump cat")
            await self.send(chat_id, "⏳ Шукаю токени наратива і аналізую бандли, це може зайняти 1–3 хвилини…")
            await self.send(chat_id, fmt_narrative(await self.service.narrative_search(arg)))
        elif cmd == "/watch":
            if not arg:
                mine = self.service.watches_of(str(chat_id))
                await self.send(chat_id, "Стежу за: " + e(", ".join(mine)) if mine else "Додай наратив: /watch trump cat")
                return

            async def alert(member: Member, family: int) -> None:
                tag = "🎯 <b>прибуткова група, ціль для копіювання</b>\n" if member.cluster_black and \
                    member.narrative_label not in ("LARP", "COPY") else ""
                await self.send(chat_id, f"🆕 <b>Новий токен під «{e(arg)}»</b> (у сімействі {family})\n{tag}{fmt_member(member)}")

            q = self.service.add_watch(arg, str(chat_id), alert)
            await self.send(chat_id, f"👀 Стежу за новими запусками під «{e(q.label)}». Зупинити: /unwatch")
        elif cmd == "/unwatch":
            n = self.service.remove_watches(str(chat_id))
            await self.send(chat_id, f"Зупинено стежень: {n}")
        elif cmd == "/clusters":
            await self.send(chat_id, fmt_clusters(self.service.clusters()))
        elif cmd == "/cluster":
            c = self.service.cluster(arg) if arg else None
            await self.send(chat_id, fmt_cluster(c, sel) if c else "Не знайшов такої групи чи гаманця.")
        elif cmd == "/addtrader":
            wallet, _, label = arg.partition(" ")
            if not wallet:
                raise ValueError("додай гаманець: /addtrader <гаманець> [назва]")
            await self.send(chat_id, "⏳ Додаю і завантажую історію угод трейдера, це до 1–2 хвилин…")
            st = await self.service.add_trader(wallet, label)
            await self.send(chat_id, "✅ Трейдера додано, сигнали про його купівлі будуть тут.\n\n" + fmt_trader(st))
        elif cmd == "/deltrader":
            ok = await self.service.remove_trader(arg) if arg else False
            await self.send(chat_id, "Прибрано." if ok else "Такого трейдера немає в списку.")
        elif cmd == "/traders":
            await self.send(chat_id, fmt_traders(self.service.traders()))
        elif cmd == "/rank":
            n = int(arg) if arg.strip().isdigit() else 30
            await self.send(chat_id, f"⏳ Завантажую історію до {n} трейдерів без даних і рахую PnL. "
                                     "Це кілька хвилин; можна запускати /rank ще раз для наступних.")
            await self.send(chat_id, fmt_traders(await self.service.rank_traders(limit=n)))
        elif cmd == "/trader":
            if not arg:
                raise ValueError("додай гаманець: /trader <гаманець>")
            await self.send(chat_id, "⏳ Рахую PnL гаманця…")
            await self.send(chat_id, fmt_trader(await self.service.trader(arg)))
        elif cmd == "/positions":
            await self.send(chat_id, fmt_positions(self.service.positions()))
        elif cmd == "/status":
            await self.send(chat_id, fmt_status(self.service.status()))
        else:
            await self.send(chat_id, "Не знаю такої команди.\n\n" + HELP)


    async def import_document(self, chat_id: int, doc: dict[str, Any]) -> None:
        if (doc.get("file_size") or 0) > 5_000_000:
            raise ValueError("файл більший за 5 МБ")
        await self.send(chat_id, "⏳ Читаю і сортую гаманці…")
        info = await self.api("getFile", file_id=doc["file_id"])
        url = self.base.replace("/bot", "/file/bot", 1) + "/" + info["file_path"]
        resp = await self.http.get(url)
        resp.raise_for_status()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "wallets.txt")
            with open(path, "wb") as fh:
                fh.write(resp.content)
            result, added = await self.service.import_wallets([path])
        await self.send(chat_id, fmt_import(result, added))


def fmt_import(result: SortResult, added: int) -> str:
    groups = result.by_category()
    lines = [f"📥 <b>Гаманців у файлі:</b> {result.total_in} · унікальних {len(result.wallets)} · "
             f"дублікатів {result.duplicates}" + (f" · невалідних {len(result.invalid)}" if result.invalid else ""), ""]
    for cat in CATEGORY_ORDER:
        if groups[cat]:
            mark = "➕" if cat in FOLLOW else "➖"
            lines.append(f"{mark} {e(CATEGORY_UA[cat])}: {len(groups[cat])}")
    bundles = result.bundle_groups()
    if bundles:
        lines += ["", "📦 Бандл-групи: " + ", ".join(f"{e(k)} ({len(v)})" for k, v in list(bundles.items())[:6])]
    multi = sorted(((n, c) for n, c in result.name_counts().items() if c >= 5), key=lambda x: -x[1])[:6]
    if multi:
        lines.append("👥 Одна людина, багато гаманців: " + ", ".join(f"{e(n)} ({c})" for n, c in multi))
    lines += ["", f"✅ Додано в стеження: <b>{added}</b> (➕ категорії). Біржі, розробники, снайпери й «погані» не додані.",
              "Далі: /rank — бот завантажить історію і покаже, хто реально в плюсі."]
    return "\n".join(lines)


def started_line(service: Service) -> str:
    s = service.status()
    return f"✅ bundlehunter запущено ({s['mode']}) {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}"
