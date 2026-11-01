"use strict";

// Every value from the API is inserted as text (never innerHTML): token names, links and
// descriptions are written by whoever launched the token.

const $ = (id) => document.getElementById(id);

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "onclick") node.addEventListener("click", v);
    else node.setAttribute(k, v);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

async function api(path) {
  const resp = await fetch(path, { credentials: "same-origin" });
  const data = await resp.json().catch(() => ({ error: `HTTP ${resp.status}` }));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

const pct = (x, digits = 1) => (x == null ? "–" : `${(x * 100).toFixed(digits)}%`);
const signed = (x, digits = 3) => (x == null ? "–" : `${x >= 0 ? "+" : ""}${x.toFixed(digits)}`);
const short = (m) => (m && m.length > 12 ? `${m.slice(0, 6)}…${m.slice(-4)}` : m || "");
const usd = (x) => (!x ? "–" : x >= 1e6 ? `$${(x / 1e6).toFixed(1)}M` : x >= 1e3 ? `$${(x / 1e3).toFixed(0)}k` : `$${x.toFixed(0)}`);
const pnlClass = (x) => (x == null ? "" : x >= 0 ? "pos" : "neg");

function ago(ts) {
  if (!ts) return "–";
  const s = Date.now() / 1000 - ts;
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} хв тому`;
  if (s < 172800) return `${Math.round(s / 3600)} год тому`;
  return `${Math.round(s / 86400)} дн тому`;
}

function mint(m) {
  return el("span", {
    class: "mono copy", title: `${m} (натисни, щоб скопіювати)`,
    onclick: () => navigator.clipboard && navigator.clipboard.writeText(m),
  }, short(m));
}

function table(headers, rows) {
  return el("div", { class: "table-wrap" }, el("table", {},
    el("thead", {}, el("tr", {}, headers.map((h) => el("th", { class: h.num ? "num" : null }, h.label || h)))),
    el("tbody", {}, rows.length ? rows.map((r) => el("tr", r.attrs || {},
      r.cells.map((c, i) => el("td", { class: headers[i] && headers[i].num ? "num" : null }, c))))
      : el("tr", {}, el("td", { colspan: headers.length, class: "muted" }, "порожньо")))));
}

function busy(container, text) {
  container.replaceChildren(el("div", { class: "busy" }, text));
}

function fail(container, err) {
  container.replaceChildren(el("div", { class: "error" }, `Помилка: ${err.message || err}`));
}

const NARRATIVE = { ORIGINAL: "оригінал", COPY: "копія", LARP: "LARP", "SIDE TOKEN": "побічний", "FIRST, SUSPICIOUS": "перший, підозрілий" };
const NARRATIVE_LVL = { ORIGINAL: "green", COPY: "red", LARP: "red", "SIDE TOKEN": "info", "FIRST, SUSPICIOUS": "yellow" };
const BUNDLE = { "PROFITABLE BUNDLER": "прибуткова група", "HEAVY BUNDLE": "важкий бандл", "BUNDLE SOLD": "бандл продав", CLEAN: "чисто", MODERATE: "помірно", "-": "–" };
const BUNDLE_LVL = { "PROFITABLE BUNDLER": "green", "HEAVY BUNDLE": "red", "BUNDLE SOLD": "yellow", CLEAN: "green", MODERATE: "info", "-": "info" };

function tag(text, level) {
  return el("span", { class: `tag lvl-${level}` }, text);
}

function verdictUa(score) {
  if (score >= 60) return "Високий ризик: схоже на LARP або підготовку рагу";
  if (score >= 30) return "Підозріло";
  return "Серйозних червоних прапорців немає";
}

// -- tabs ----------------------------------------------------------------------------

const loaders = {};
let refreshTimer = null;

function showTab(name) {
  for (const b of document.querySelectorAll("#tabs button")) b.classList.toggle("active", b.dataset.tab === name);
  for (const s of document.querySelectorAll(".tab")) s.classList.toggle("active", s.id === name);
  if (loaders[name]) loaders[name]();
  clearInterval(refreshTimer);
  if (name === "overview") refreshTimer = setInterval(loaders.overview, 15000);
  try { localStorage.setItem("bh-tab", name); } catch (_) { /* private mode */ }
}

// -- overview ------------------------------------------------------------------------

loaders.overview = async function () {
  try {
    const [s, positions] = await Promise.all([api("/api/status"), api("/api/positions")]);
    const banner = $("mode-banner");
    banner.className = `banner ${s.mode}`;
    banner.textContent = { live: "LIVE: торгує реальними грошима", paper: "PAPER: торгує на папері, без реальних грошей", off: "Торгівля вимкнена" }[s.mode];
    const t = s.tokens || {};
    const total = Object.values(t).reduce((a, b) => a + b, 0);
    const card = (label, value, cls) => el("div", { class: "card" }, el("div", { class: "label" }, label), el("div", { class: `value ${cls || ""}` }, value));
    $("cards").replaceChildren(
      card("Записано запусків", total),
      card("Проаналізовано", t.ok || 0),
      card("У черзі на аналіз", t.pending || 0),
      card("Груп / в плюсі", `${s.clusters} / ${s.black_clusters}`),
      card("Стежу за гаманцями", s.tracked_wallets),
      card("PnL paper, SOL", signed(s.realized_pnl_sol.paper, 3), pnlClass(s.realized_pnl_sol.paper)),
      card("PnL live, SOL", signed(s.realized_pnl_sol.live, 3), pnlClass(s.realized_pnl_sol.live)),
      card("Працює", `${(s.uptime_s / 3600).toFixed(1)} год`),
    );
    $("watching").replaceChildren(table(
      ["Токен", "Mint", "Група", "Відколи", "Стан"],
      s.watching.map((w) => ({ cells: [w.symbol || "?", mint(w.mint), w.cluster_id, ago(Date.now() / 1000 - w.age_s),
        w.in_position ? tag("у позиції", "green") : w.blocked ? `чекаю: ${w.blocked_ua || w.blocked}` : "чекаю входу"] })),
    ));
    $("open-positions").replaceChildren(positionsTable(positions.filter((p) => !p.closed_at)));
  } catch (err) {
    fail($("cards"), err);
  }
};

// -- token check -----------------------------------------------------------------------

function renderCheck(r) {
  const larp = r.larp;
  const meta = larp.meta;
  const level = larp.score >= 60 ? "red" : larp.score >= 30 ? "yellow" : "green";
  const out = [];
  out.push(el("div", { class: "box" },
    el("h3", {}, `${meta.symbol || "?"} ${meta.name || ""}`),
    el("div", { class: "mono" }, r.mint),
    el("p", {}, el("span", { class: `score lvl-${level}` }, `Ризик ${larp.score}/100`), larp.verdict_ua || verdictUa(larp.score)),
    el("div", { class: "muted" }, [["X", meta.twitter], ["Telegram", meta.telegram], ["Сайт", meta.website]]
      .filter(([, v]) => v).map(([k, v]) => `${k}: ${v}`).join(" · ")),
  ));

  const grid = el("div", { class: "grid2" });
  if (r.panel) {
    const g = r.panel.groups;
    const row = (label, key) => ({ cells: [label, g[key].wallets, pct(g[key].held_pct), pct(g[key].initial_pct)] });
    grid.append(el("div", {},
      el("h3", {}, "Холдери (як на Axiom)"),
      table(["", { label: "гаманців", num: true }, { label: "тримають зараз", num: true }, { label: "на старті", num: true }], [
        { cells: ["Top 10", r.panel.top10.length, pct(r.panel.metrics.top10_pct), ""] },
        row("Dev", "dev"), row("Snipers", "snipers"), row("Insiders", "insiders"), row("Bundlers", "bundlers"),
        row("Разом пов'язані", "linked"),
      ]),
      el("p", { class: "muted" }, r.panel.source === "rpc" ? "Баланси наживо з блокчейну." : "Баланси оцінені за збереженими угодами."),
    ));
  } else if (r.note) {
    grid.append(el("div", { class: "box muted" }, r.note));
  }
  const findings = el("ul", { class: "findings" },
    [...larp.findings].sort((a, b) => ["red", "yellow", "green", "info"].indexOf(a.level) - ["red", "yellow", "green", "info"].indexOf(b.level))
      .map((f) => el("li", { class: `lvl-${f.level}` }, f.ua || f.text)));
  grid.append(el("div", {}, el("h3", {}, "Знахідки"), larp.findings.length ? findings : el("p", { class: "muted" }, "нічого")));
  out.push(grid);

  if (r.dev_cluster) {
    const c = r.dev_cluster;
    out.push(el("div", { class: "box" },
      el("h3", {}, `Група розробника ${c.cluster_id}`),
      el("p", {}, `${c.launches} запусків, ${pct(c.win_rate, 0)} в плюс, PnL ${signed(c.realized_pnl_sol, 2)} SOL. `,
        r.dev_cluster_in_the_black ? tag("в плюсі: бот її копіює", "green") : tag("не копіюється", "info")),
    ));
  }
  if (larp.ai) {
    out.push(el("div", { class: "box" },
      el("h3", {}, `ШІ-вердикт: ${String(larp.ai.risk || "?").toUpperCase()}`),
      el("p", {}, larp.ai.summary || ""),
      el("ul", {}, (larp.ai.reasons || []).map((x) => el("li", {}, x))),
    ));
  }
  return out;
}

async function runCheck(ev) {
  ev.preventDefault();
  const box = $("check-result");
  const button = ev.submitter || $("check-form").querySelector("button");
  const m = $("check-input").value.trim();
  button.disabled = true;
  busy(box, "Перевіряю токен: метадані, сайт, копії, бандл і холдери. Це до хвилини…");
  try {
    const r = await api(`/api/check/${encodeURIComponent(m)}${$("check-ai").checked ? "?ai=1" : ""}`);
    box.replaceChildren(...renderCheck(r));
  } catch (err) {
    fail(box, err);
  } finally {
    button.disabled = false;
  }
}

// -- narrative -------------------------------------------------------------------------

function renderNarrative(r) {
  const tokens = r.tokens.filter((t) => t.larp_score != null || t.holder_panel);
  const rest = r.tokens.length - tokens.length;
  const out = [el("p", {}, `Знайдено ${r.tokens.length} токен(ів), проаналізовано ${tokens.length}.`)];
  out.push(el("div", { class: `box ${r.pick ? "" : "error"}` }, el("strong", {}, r.pick ? "Вибір: " : "Вибору немає: "), r.pick_reason));
  out.push(table(
    ["#", "Токен", "Mint", "Створено", { label: "Mcap", num: true }, { label: "LARP", num: true }, "Наратив",
      { label: "Бандл зараз", num: true }, { label: "Інсайдери", num: true }, "Група розробника", "Бандл"],
    tokens.map((t, i) => {
      const hp = t.holder_panel || {};
      return { cells: [i + 1, t.symbol || t.name || "?", mint(t.mint), ago(t.create_time), usd(t.market_cap_usd),
        t.larp_score ?? "–", tag(NARRATIVE[t.narrative] || t.narrative, NARRATIVE_LVL[t.narrative] || "info"),
        pct(hp.bundlers_pct), pct(hp.insiders_pct),
        t.dev_cluster ? `${t.dev_cluster}${t.dev_cluster_in_the_black ? " (в плюсі)" : ""}` : "–",
        tag(BUNDLE[t.bundle] || t.bundle, BUNDLE_LVL[t.bundle] || "info")] };
    }),
  ));
  if (rest > 0) out.push(el("p", { class: "muted" }, `Ще ${rest} не аналізувались (ліміт [narrative] max_analyze).`));
  return out;
}

async function runNarrative(ev) {
  ev.preventDefault();
  const box = $("narrative-result");
  const button = ev.submitter || $("narrative-form").querySelector("button");
  button.disabled = true;
  busy(box, "Шукаю токени наратива (твоя база + DexScreener) і аналізую бандли. Це 1–3 хвилини…");
  try {
    box.replaceChildren(...renderNarrative(await api(`/api/narrative?q=${encodeURIComponent($("narrative-input").value.trim())}`)));
  } catch (err) {
    fail(box, err);
  } finally {
    button.disabled = false;
  }
}

// -- clusters --------------------------------------------------------------------------

loaders.clusters = async function () {
  const box = $("clusters-table");
  busy(box, "Завантажую групи…");
  try {
    const rows = await api(`/api/clusters${$("clusters-all").checked ? "?all=1" : ""}`);
    box.replaceChildren(table(
      ["Група", { label: "Гаманців", num: true }, { label: "Запусків", num: true }, { label: "Закрито", num: true },
        { label: "В плюс", num: true }, { label: "PnL, SOL", num: true }, { label: "Перший продаж", num: true }, "Статус"],
      rows.map((c) => ({
        attrs: { class: "clickable", onclick: () => showCluster(c.cluster_id) },
        cells: [c.cluster_id, c.wallets.length, c.launches, c.closed, pct(c.win_rate, 0),
          el("span", { class: pnlClass(c.realized_pnl_sol) }, signed(c.realized_pnl_sol, 2)),
          c.median_first_sell_s == null ? "–" : `${Math.round(c.median_first_sell_s)} с`,
          c.in_the_black ? tag("в плюсі", "green") : tag("ні", "info")],
      })),
    ), ...(rows.length ? [] : [el("p", { class: "muted" }, "Поки порожньо: бот має назбирати запуски, або додай відомі гаманці командою bh seed.")]));
  } catch (err) {
    fail(box, err);
  }
};

async function showCluster(id) {
  const box = $("cluster-detail");
  busy(box, "Завантажую…");
  try {
    const c = await api(`/api/cluster/${encodeURIComponent(id)}`);
    const hours = Object.entries(c.active_hours_utc || {}).sort((a, b) => b[1] - a[1]).slice(0, 3).map(([h]) => `${h}:00`).join(", ");
    box.replaceChildren(el("div", { class: "box" },
      el("h3", {}, `${c.cluster_id}: ${c.in_the_black ? "в плюсі" : "не копіюється"}`),
      c.rejected_because && c.rejected_because.length ? el("p", { class: "muted" }, `Чому ні: ${c.rejected_because.join("; ")}`) : null,
      el("p", {}, `Коли: частіше о ${hours || "–"} UTC · перший продаж через ${c.median_first_sell_s ?? "–"} с · вихід через ${c.median_exit_s ?? "–"} с`),
      el("p", {}, `З ким: ${Object.keys(c.partners || {}).join(", ") || "–"}`),
      el("h3", {}, "Токени"),
      table(["Створено", "Mint", { label: "Вклали", num: true }, { label: "Вивели", num: true }, { label: "PnL", num: true }, "Стан", { label: "Перший продаж", num: true }],
        c.tokens.slice().reverse().map((t) => ({ cells: [ago(t.created_at), mint(t.mint), t.sol_in.toFixed(3), t.sol_out.toFixed(3),
          el("span", { class: pnlClass(t.pnl) }, signed(t.pnl, 3)), t.closed ? "закрито" : "ще тримають",
          t.first_sell_after_s == null ? "–" : `${t.first_sell_after_s} с`] }))),
      el("h3", {}, `Гаманці (${c.wallets.length})`),
      el("div", { class: "mono wallets" }, c.wallets.join("\n")),
    ));
    box.scrollIntoView({ behavior: "smooth" });
  } catch (err) {
    fail(box, err);
  }
}

// -- positions -------------------------------------------------------------------------

function positionsTable(rows) {
  return table(
    ["Відкрито", "Токен", "Mint", "Режим", { label: "Розмір, SOL", num: true }, { label: "PnL, SOL", num: true }, "Вхід → вихід"],
    rows.map((p) => ({ cells: [ago(p.opened_at), p.symbol || "?", mint(p.mint), p.live ? tag("LIVE", "red") : "paper",
      (p.size_sol || 0).toFixed(3),
      p.closed_at ? el("span", { class: pnlClass(p.pnl_sol) }, signed(p.pnl_sol, 4)) : tag("відкрита", "green"),
      `${p.reason_open_ua || p.reason_open || ""} → ${p.reason_close_ua || p.reason_close || "…"}`] })),
  );
}

loaders.positions = async function () {
  const box = $("positions-table");
  busy(box, "Завантажую угоди…");
  try {
    box.replaceChildren(positionsTable(await api("/api/positions")));
  } catch (err) {
    fail(box, err);
  }
};

// -- start -----------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  for (const b of document.querySelectorAll("#tabs button")) b.addEventListener("click", () => showTab(b.dataset.tab));
  $("check-form").addEventListener("submit", runCheck);
  $("narrative-form").addEventListener("submit", runNarrative);
  $("clusters-all").addEventListener("change", loaders.clusters);
  let tab = "overview";
  try { tab = localStorage.getItem("bh-tab") || tab; } catch (_) { /* private mode */ }
  showTab(document.getElementById(tab) ? tab : "overview");
});
