"""Desktop overlay: a small always-on-top window over the chart, fed by the web panel's JSON API.

    bh serve            # the bot, in one terminal
    bh overlay          # this window, in another (pip install PySide6-Essentials)

Tabs: Зараз (the "Купити?" card + open positions), Поле (every token being watched, graded),
Потік (the bot's decisions as they happen), Журнал (closed trades), Розбір (learned strategy
and validation), Ще (window settings).

The overlay never places orders and never sees keys. Enter opens the token in Axiom so YOU
decide; Esc hides the card. The bot's own paper/live trades keep going in `bh serve`.
Grades: A ready to buy, B waits only for volume confirmation, C waits for checks or risk
limits, D red flag right now, E failed the token check.
"""

from __future__ import annotations

import base64
import json
import math
import struct
import tempfile
import time
import wave
from pathlib import Path
from typing import Any, Callable

try:
    from PySide6.QtCore import QByteArray, Qt, QTimer, QUrl
    from PySide6.QtGui import QColor, QDesktopServices, QFont, QGuiApplication, QKeySequence, QShortcut
    from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
    from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QFrame, QHBoxLayout, QHeaderView,
                                   QLabel, QPlainTextEdit, QPushButton, QSlider, QTableWidget, QTableWidgetItem,
                                   QTabWidget, QVBoxLayout, QWidget)
except ImportError:  # pragma: no cover - optional dependency
    QApplication = None  # type: ignore[assignment]
    QWidget = object  # type: ignore[assignment,misc]

BG, PANEL, LINE, TEXT, MUTED = "#101216", "#181b21", "#2a2f38", "#e8eaee", "#8b93a1"
GOLD, GREEN, RED = "#FFD60A", "#30D158", "#FF453A"  # gold = attention; green/red only for price
GRADE_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}

QSS = f"""
* {{ font-family: "Inter", "Segoe UI", system-ui, sans-serif; font-size: 13px; color: {TEXT}; }}
QWidget#root {{ background: {BG}; }}
QTabWidget::pane {{ border: 0; }}
QTabBar::tab {{ background: transparent; color: {MUTED}; padding: 6px 10px; border: 0; }}
QTabBar::tab:selected {{ color: {GOLD}; border-bottom: 2px solid {GOLD}; }}
QFrame#card {{ background: {PANEL}; border: 1px solid {LINE}; border-radius: 12px; }}
QPushButton {{ background: {PANEL}; border: 1px solid {LINE}; border-radius: 8px; padding: 6px 10px; }}
QPushButton:hover {{ border-color: {MUTED}; }}
QPushButton#primary {{ background: {GOLD}; color: #101216; border: 0; font-weight: 700; }}
QTableWidget {{ background: {BG}; border: 0; gridline-color: {LINE}; }}
QHeaderView::section {{ background: {BG}; color: {MUTED}; border: 0; border-bottom: 1px solid {LINE}; padding: 4px; }}
QTableWidget::item {{ padding: 2px 4px; border-bottom: 1px solid {LINE}; }}
QTableWidget::item:selected {{ background: {PANEL}; color: {TEXT}; }}
QPlainTextEdit {{ background: {PANEL}; border: 1px solid {LINE}; border-radius: 10px; padding: 6px;
                 font-family: "JetBrains Mono", Consolas, monospace; font-size: 12px; }}
QLabel#muted {{ color: {MUTED}; }}
"""


# -- pure helpers (tested without Qt) ---------------------------------------------------------

def axiom_url(mint: str) -> str:
    return f"https://axiom.trade/meme/{mint}?chain=sol"


def dex_url(mint: str) -> str:
    return f"https://dexscreener.com/solana/{mint}"


def short(mint: str) -> str:
    return f"{mint[:4]}…{mint[-4:]}" if len(mint) > 10 else mint


def stars(n: int) -> str:
    n = max(0, min(5, int(n)))
    return "★" * n + "☆" * (5 - n)


def pick_candidate(scalp: dict | None, dismissed: set[str]) -> dict | None:
    """Best token for the 'Купити?' card: not held, not dismissed, best grade, then most smart wallets."""
    rows = [w for w in (scalp or {}).get("watching", [])
            if not w.get("in_position") and w["mint"] not in dismissed and w.get("grade") in ("A", "B", "C")]
    if not rows:
        return None
    return min(rows, key=lambda w: (GRADE_ORDER.get(w.get("grade", "E"), 9), -len(w.get("smart", [])), w.get("age_s", 0)))


def pnl_pct(entry: float | None, price: float | None) -> float | None:
    return (price / entry - 1) if entry and price else None


def fmt_age(s: float | None) -> str:
    if s is None:
        return "—"
    s = int(s)
    return f"{s}с" if s < 60 else f"{s // 60}хв" if s < 3600 else f"{s // 3600}г"


def _tone_file(freqs: tuple[int, ...], path: Path) -> Path:
    rate = 22050
    frames = b""
    for f in freqs:
        for i in range(int(rate * 0.09)):
            env = min(1.0, i / 200, (rate * 0.09 - i) / 400)
            frames += struct.pack("<h", int(9000 * env * math.sin(2 * math.pi * f * i / rate)))
        frames += b"\x00\x00" * int(rate * 0.04)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return path


# -- window -----------------------------------------------------------------------------------

class Api:
    def __init__(self, base: str, password: str | None) -> None:
        self.base = base.rstrip("/")
        self.nam = QNetworkAccessManager()
        self.auth = ("Basic " + base64.b64encode(f"bh:{password}".encode()).decode()) if password else None

    def _req(self, path: str) -> "QNetworkRequest":
        req = QNetworkRequest(QUrl(self.base + path))
        if self.auth:
            req.setRawHeader(b"Authorization", self.auth.encode())
        return req

    def _done(self, reply: "QNetworkReply", cb: Callable[[Any, str], None]) -> None:
        def finished() -> None:
            body = bytes(reply.readAll().data())
            err = "" if reply.error() == QNetworkReply.NetworkError.NoError else reply.errorString()
            reply.deleteLater()
            try:
                data = json.loads(body) if body else None
            except ValueError:
                data = None
            if isinstance(data, dict) and data.get("error"):
                err = str(data["error"])
            cb(data, err)
        reply.finished.connect(finished)

    def get(self, path: str, cb: Callable[[Any, str], None]) -> None:
        self._done(self.nam.get(self._req(path)), cb)

    def post(self, path: str, body: dict, cb: Callable[[Any, str], None]) -> None:
        req = self._req(path)
        req.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
        req.setRawHeader(b"X-BH-Action", b"1")
        self._done(self.nam.post(req, QByteArray(json.dumps(body).encode())), cb)


def _label(text: str = "", muted: bool = False, size: int | None = None, bold: bool = False) -> "QLabel":
    lb = QLabel(text)
    if muted:
        lb.setObjectName("muted")
    if size or bold:
        f = lb.font()
        if size:
            f.setPointSize(size)
        f.setBold(bold)
        lb.setFont(f)
    lb.setWordWrap(True)
    return lb


def _table(headers: list[str]) -> "QTableWidget":
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setShowGrid(False)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    t.horizontalHeader().setSectionResizeMode(len(headers) - 1, QHeaderView.ResizeMode.Stretch)
    t.horizontalHeader().setStretchLastSection(True)
    t.setWordWrap(True)
    t.setTextElideMode(Qt.TextElideMode.ElideNone)
    t.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    return t


def _item(text: str, color: str | None = None, bold: bool = False, data: str | None = None) -> "QTableWidgetItem":
    it = QTableWidgetItem(text)
    it.setToolTip(text)
    if color:
        it.setForeground(QColor(color))
    if bold:
        f = it.font()
        f.setBold(True)
        it.setFont(f)
    if data:
        it.setData(Qt.ItemDataRole.UserRole, data)
    return it


class Overlay(QWidget):
    def __init__(self, base: str, password: str | None) -> None:
        super().__init__()
        self.setObjectName("root")
        self.setWindowTitle("bundlehunter")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.resize(420, 560)
        self.setStyleSheet(QSS)
        self.api = Api(base, password)
        self.dismissed: set[str] = set()
        self.alerted: dict[str, str] = {}
        self.candidate: dict | None = None
        self.sound_on = True
        self.symbols: dict[str, str] = {}
        self._sounds = self._make_sounds()

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        head = QHBoxLayout()
        self.mode = _label("…", bold=True)
        self.money = _label("", muted=True)
        self.money.setAlignment(Qt.AlignmentFlag.AlignRight)
        head.addWidget(self.mode)
        head.addWidget(self.money, 1)
        root.addLayout(head)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self.tabs.addTab(self._tab_now(), "Зараз")
        self.field = _table(["", "Токен", "Сила", "Вік", "Чому"])
        self.tabs.addTab(self.field, "Поле")
        self.stream = _table(["Час", "Токен", "Рішення"])
        self.tabs.addTab(self.stream, "Потік")
        self.journal = _table(["Закрито", "Токен", "SOL", "Причина"])
        self.tabs.addTab(self.journal, "Журнал")
        self.tabs.addTab(self._tab_review(), "Розбір")
        self.tabs.addTab(self._tab_more(base), "Ще")
        for t in (self.field, self.stream, self.journal):
            t.cellDoubleClicked.connect(lambda r, c, t=t: self._open_row(t, r))
        self.err = _label("", muted=True)
        root.addWidget(self.err)

        QShortcut(QKeySequence(Qt.Key.Key_Return), self, self.buy_card)
        QShortcut(QKeySequence(Qt.Key.Key_Enter), self, self.buy_card)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self.skip_card)
        for i in range(6):
            QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self, lambda i=i: self.tabs.setCurrentIndex(i))

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1500)
        self.slow = QTimer(self)
        self.slow.timeout.connect(self.refresh_slow)
        self.slow.start(10000)
        self.refresh()
        self.refresh_slow()

    # -- tabs ---------------------------------------------------------------------------------

    def _tab_now(self) -> "QWidget":
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 8, 0, 0)
        self.card = QFrame()
        self.card.setObjectName("card")
        c = QVBoxLayout(self.card)
        top = QHBoxLayout()
        self.c_title = _label("Купити?", muted=True)
        self.c_grade = _label("—", size=28, bold=True)
        self.c_grade.setAlignment(Qt.AlignmentFlag.AlignRight)
        top.addWidget(self.c_title, 1)
        top.addWidget(self.c_grade)
        c.addLayout(top)
        self.c_token = _label("Чекаю на сильні гаманці…", size=15, bold=True)
        self.c_stars = _label("", muted=False)
        self.c_why = _label("", muted=True)
        c.addWidget(self.c_token)
        c.addWidget(self.c_stars)
        c.addWidget(self.c_why)
        row = QHBoxLayout()
        self.b_buy = QPushButton("Axiom  ⏎")
        self.b_buy.setObjectName("primary")
        self.b_buy.clicked.connect(self.buy_card)
        self.b_skip = QPushButton("Пропустити  Esc")
        self.b_skip.clicked.connect(self.skip_card)
        self.b_ca = QPushButton("CA")
        self.b_ca.clicked.connect(lambda: self.candidate and self.copy(self.candidate["mint"]))
        row.addWidget(self.b_buy, 2)
        row.addWidget(self.b_skip, 1)
        row.addWidget(self.b_ca)
        c.addLayout(row)
        lay.addWidget(self.card)
        lay.addWidget(_label("Відкриті позиції", muted=True))
        self.pos_box = QVBoxLayout()
        lay.addLayout(self.pos_box)
        lay.addStretch(1)
        self.show_card(None)
        return w

    def _tab_review(self) -> "QWidget":
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 8, 0, 0)
        row = QHBoxLayout()
        b1 = QPushButton("Навчити з export/")
        b1.clicked.connect(lambda: self._post("/api/learn", "Вчуся на export/…"))
        b2 = QPushButton("Перевірити скальпер")
        b2.clicked.connect(lambda: self._post("/api/scalp/eval", "Перевіряю на нових даних…"))
        row.addWidget(b1)
        row.addWidget(b2)
        lay.addLayout(row)
        self.review = QPlainTextEdit()
        self.review.setReadOnly(True)
        lay.addWidget(self.review, 1)
        return w

    def _tab_more(self, base: str) -> "QWidget":
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 8, 0, 0)
        top = QCheckBox("Поверх усіх вікон")
        top.setChecked(True)
        top.toggled.connect(self._on_top)
        snd = QCheckBox("Звук на A та B")
        snd.setChecked(True)
        snd.toggled.connect(lambda v: setattr(self, "sound_on", v))
        lay.addWidget(top)
        lay.addWidget(snd)
        lay.addWidget(_label("Прозорість", muted=True))
        sl = QSlider(Qt.Orientation.Horizontal)
        sl.setRange(40, 100)
        sl.setValue(100)
        sl.valueChanged.connect(lambda v: self.setWindowOpacity(v / 100))
        lay.addWidget(sl)
        clear = QPushButton("Повернути пропущені токени")
        clear.clicked.connect(lambda: (self.dismissed.clear(), self.refresh()))
        lay.addWidget(clear)
        lay.addWidget(_label(f"Панель: {base}", muted=True))
        lay.addWidget(_label("Enter — відкрити в Axiom (купуєш ти), Esc — пропустити, Ctrl+1…6 — вкладки, "
                             "подвійний клік — токен в Axiom.", muted=True))
        lay.addWidget(_label("Оверлей не купує і не бачить ключів. Не фінансова порада.", muted=True))
        lay.addStretch(1)
        return w

    def _on_top(self, on: bool) -> None:
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
        self.show()

    # -- data ---------------------------------------------------------------------------------

    def refresh(self) -> None:
        self.api.get("/api/status", self.on_status)

    def refresh_slow(self) -> None:
        self.api.get("/api/positions", self.on_positions)
        self.api.get("/api/learned", self.on_learned)

    def on_status(self, s: Any, err: str) -> None:
        if err or not isinstance(s, dict):
            self.err.setText(f"Немає зв'язку з {self.api.base}: {err or 'порожня відповідь'}. Запущено bh serve?")
            return
        self.err.setText("")
        scalp = s.get("scalp") or {}
        for w in s.get("watching", []) + scalp.get("watching", []) + scalp.get("positions", []):
            if w.get("symbol"):
                self.symbols[w["mint"]] = w["symbol"]
        mode = s.get("mode", "off")
        self.mode.setText({"live": "● LIVE", "paper": "○ PAPER", "off": "○ ТІЛЬКИ АНАЛІЗ"}.get(mode, mode))
        self.mode.setStyleSheet(f"color: {GOLD if mode == 'live' else TEXT};")
        bits = []
        if s.get("paper"):
            bits.append(f"баланс {s['paper']['balance_sol']:.3f} SOL")
        if scalp:
            bits.append(f"сьогодні {scalp.get('today_pnl_sol', 0):+.3f}")
            bits.append(scalp.get("state", ""))
        self.money.setText(" · ".join(bits))
        self._fill_field(scalp)
        self._fill_stream(scalp)
        self._fill_positions(scalp)
        cand = pick_candidate(scalp, self.dismissed)
        self.show_card(cand)
        if cand and self.alerted.get(cand["mint"]) != cand["grade"] and cand["grade"] in ("A", "B"):
            self.alerted[cand["mint"]] = cand["grade"]
            self.play(cand["grade"])

    def on_positions(self, rows: Any, err: str) -> None:
        if err or not isinstance(rows, list):
            return
        closed = [r for r in rows if r.get("closed_at")][:100]
        self.journal.setRowCount(len(closed))
        for i, r in enumerate(closed):
            pnl = r.get("pnl_sol") or 0.0
            mint = r.get("mint", "")
            self.journal.setItem(i, 0, _item(time.strftime("%d.%m %H:%M", time.localtime(r["closed_at"]))))
            self.journal.setItem(i, 1, _item(r.get("symbol") or short(mint), data=mint))
            self.journal.setItem(i, 2, _item(f"{pnl:+.4f}", GREEN if pnl > 0 else RED if pnl < 0 else None))
            self.journal.setItem(i, 3, _item(r.get("reason_close_ua") or r.get("reason_close") or ""))

    def on_learned(self, data: Any, err: str) -> None:
        if self.review.property("busy"):
            return
        from .learn import format_result

        self.review.setPlainText(format_result(data) if isinstance(data, dict) else
                                 "Ще не вчився. Поклади експорт угод (CSV / JSON) у папку export/ і натисни "
                                 "«Навчити з export/» або запусти: bh learn")

    def _post(self, path: str, busy: str) -> None:
        self.review.setProperty("busy", True)
        self.review.setPlainText(busy)

        def done(data: Any, err: str) -> None:
            self.review.setProperty("busy", False)
            self.review.setPlainText(err or (data or {}).get("text", ""))
        self.api.post(path, {}, done)

    def _fill_field(self, scalp: dict) -> None:
        rows = sorted(scalp.get("watching", []), key=lambda w: (GRADE_ORDER.get(w.get("grade", "E"), 9), w.get("age_s", 0)))
        self.field.setRowCount(len(rows))
        for i, w in enumerate(rows):
            g = "•" if w.get("in_position") else w.get("grade", "?")
            hot = g in ("A", "B")
            self.field.setItem(i, 0, _item(g, GOLD if hot else MUTED, bold=hot))
            self.field.setItem(i, 1, _item(self.symbols.get(w["mint"]) or short(w["mint"]), data=w["mint"]))
            self.field.setItem(i, 2, _item(stars(w.get("stars", 0)), GOLD if hot else MUTED))
            self.field.setItem(i, 3, _item(fmt_age(w.get("age_s"))))
            self.field.setItem(i, 4, _item("в позиції" if w.get("in_position") else (w.get("blocked") or "готовий")))

    def _fill_stream(self, scalp: dict) -> None:
        rows = list(reversed(scalp.get("skipped", [])))
        self.stream.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.stream.setItem(i, 0, _item(time.strftime("%H:%M:%S", time.localtime(r["ts"]))))
            self.stream.setItem(i, 1, _item(self.symbols.get(r["mint"]) or short(r["mint"]), data=r["mint"]))
            self.stream.setItem(i, 2, _item(r.get("why", ""), MUTED))

    def _fill_positions(self, scalp: dict) -> None:
        while self.pos_box.count():
            item = self.pos_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        positions = scalp.get("positions", [])
        if not positions:
            self.pos_box.addWidget(_label("немає", muted=True))
            return
        for p in positions:
            mint = p["mint"]
            row = QFrame()
            row.setObjectName("card")
            h = QHBoxLayout(row)
            h.setContentsMargins(10, 6, 10, 6)
            ch = pnl_pct(p.get("entry"), p.get("price"))
            name = _label(self.symbols.get(mint) or short(mint), bold=True)
            val = _label("—" if ch is None else f"{ch:+.1%}")
            if ch is not None:
                val.setStyleSheet(f"color: {GREEN if ch > 0 else RED if ch < 0 else TEXT}; font-weight: 700;")
            h.addWidget(name, 1)
            h.addWidget(val)
            for text, fn in (("Axiom", lambda m=mint: self.open(axiom_url(m))),
                             ("Dex", lambda m=mint: self.open(dex_url(m))),
                             ("CA", lambda m=mint: self.copy(m))):
                b = QPushButton(text)
                b.clicked.connect(fn)
                h.addWidget(b)
            self.pos_box.addWidget(row)

    # -- card ---------------------------------------------------------------------------------

    def show_card(self, w: dict | None) -> None:
        self.candidate = w
        for b in (self.b_buy, self.b_skip, self.b_ca):
            b.setEnabled(w is not None)
        if not w:
            self.c_grade.setText("—")
            self.c_grade.setStyleSheet(f"color: {MUTED};")
            self.c_token.setText("Чекаю на сильні гаманці…")
            self.c_stars.setText("")
            self.c_why.setText("Картка з'явиться, коли твої трейдери зайдуть у токен.")
            return
        g = w.get("grade", "?")
        self.c_grade.setText(g)
        self.c_grade.setStyleSheet(f"color: {GOLD if g in ('A', 'B') else MUTED};")
        self.c_token.setText(f"{self.symbols.get(w['mint']) or short(w['mint'])}   ·   {fmt_age(w.get('age_s'))}")
        self.c_stars.setText(f"{stars(w.get('stars', 0))}   {', '.join(w.get('smart', []))}")
        self.c_stars.setStyleSheet(f"color: {GOLD if g in ('A', 'B') else MUTED};")
        self.c_why.setText(w.get("blocked") or "усі перевірки пройдено: бот входить")

    def buy_card(self) -> None:
        if self.candidate:
            self.open(axiom_url(self.candidate["mint"]))

    def skip_card(self) -> None:
        if self.candidate:
            self.dismissed.add(self.candidate["mint"])
            self.refresh()

    # -- actions ------------------------------------------------------------------------------

    def _open_row(self, table: "QTableWidget", row: int) -> None:
        it = table.item(row, 1)
        mint = it.data(Qt.ItemDataRole.UserRole) if it else None
        if mint:
            self.open(axiom_url(mint))

    def open(self, url: str) -> None:
        QDesktopServices.openUrl(QUrl(url))

    def copy(self, text: str) -> None:
        QGuiApplication.clipboard().setText(text)
        self.err.setText(f"скопійовано {short(text)}")

    def _make_sounds(self) -> dict:
        try:
            from PySide6.QtMultimedia import QSoundEffect
        except ImportError:
            return {}
        d = Path(tempfile.mkdtemp(prefix="bh-sound-"))
        out = {}
        for g, freqs in (("A", (880, 1320)), ("B", (660,))):
            eff = QSoundEffect(self)
            eff.setSource(QUrl.fromLocalFile(str(_tone_file(freqs, d / f"{g}.wav"))))
            eff.setVolume(0.5)
            out[g] = eff
        return out

    def play(self, grade: str) -> None:
        if not self.sound_on:
            return
        eff = self._sounds.get(grade)
        if eff:
            eff.play()
        else:
            QApplication.beep()


def run_overlay(base: str, password: str | None = None) -> None:
    if QApplication is None:
        raise SystemExit("overlay needs PySide6: pip install PySide6-Essentials  (or pip install -e '.[overlay]')")
    app = QApplication.instance() or QApplication([])
    app.setFont(QFont("Inter", 10))
    win = Overlay(base, password)
    win.show()
    app.exec()
