"""STEP 2 probe: run once on your PC.

  python desk\\probe.py                 waits for 1 live graduation (up to 15 min)
  python desk\\probe.py --mint <MINT>   uses a mint you give it instead

It saves 1 raw response of every source to desk/logs/raw/, runs the proofs
(RPC getSlot, DexScreener, RugCheck, layer 8 getProgramAccounts, Telegram, Jev)
and writes desk/logs/raw/REPORT.txt. Send me REPORT.txt. It contains no keys.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent))
import links  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
RAW = HERE / "logs" / "raw"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS5EL6dWr8hUwRb3GyCK4Do6ZG"
AXIOM_SAMPLE = "BcLbdrwpSrbxLsANtzqAPczvmZEWpNa3FmqVAmKXfR7B"
B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

report: list[str] = []
SECRETS: list[str] = []


def out(line: str = "") -> None:
    for s in SECRETS:
        if s:
            line = line.replace(s, "***")
    print(line)
    report.append(line)


def load_env() -> dict[str, str]:
    env = {}
    f = HERE / ".env"
    if f.exists():
        for raw in f.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if raw and not raw.startswith("#") and "=" in raw:
                k, v = raw.split("=", 1)
                env[k.strip()] = v.strip()
    for k in ("RPC_URL", "TG_TOKEN", "TYPESAFE_API_KEY"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def save(name: str, data) -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, ensure_ascii=False) if not isinstance(data, str) else data
    for s in SECRETS:
        if s:
            text = text.replace(s, "***")
    (RAW / name).write_text(text, encoding="utf-8")


def key_paths(obj, prefix: str = "") -> dict[str, str]:
    """Every leaf path in a JSON value; list indexes collapse to [i]."""
    found: dict[str, str] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            found.update(key_paths(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        if not obj:
            found[prefix + "[]"] = "empty list"
        for item in obj[:20]:
            for p, s in key_paths(item, prefix + "[i]").items():
                found.setdefault(p, s)
    else:
        s = json.dumps(obj, ensure_ascii=False)
        found[prefix] = s if len(s) <= 60 else s[:57] + "..."
    return found


def dump_paths(title: str, obj) -> None:
    out(f"\n--- keys in {title} ---")
    for p, s in key_paths(obj).items():
        out(f"  {p} = {s}")


async def rpc(client: httpx.AsyncClient, url: str, method: str, params=None, timeout=30):
    r = await client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []},
                          timeout=timeout)
    r.raise_for_status()
    return r.json()


async def wait_migration(timeout_s: int) -> tuple[list, dict | None]:
    msgs: list = []
    out(f"PumpPortal: waiting up to {timeout_s // 60} min for 1 graduation...")
    deadline = time.time() + timeout_s
    async with websockets.connect("wss://pumpportal.fun/api/data", open_timeout=20) as ws:
        await ws.send(json.dumps({"method": "subscribeMigration"}))
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(1, deadline - time.time()))
            except asyncio.TimeoutError:
                break
            try:
                msg = json.loads(raw)
            except ValueError:
                msg = raw
            msgs.append(msg)
            if isinstance(msg, dict) and any(isinstance(v, str) and B58.match(v) for v in msg.values()):
                return msgs, msg
            out(f"  (non-coin message: {str(msg)[:80]})")
    return msgs, None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mint", help="use this graduated mint instead of waiting for one")
    ap.add_argument("--wait-min", type=int, default=15)
    args = ap.parse_args()

    env = load_env()
    rpc_url = env.get("RPC_URL") or "https://api.mainnet-beta.solana.com"
    tg = env.get("TG_TOKEN", "")
    jev = env.get("TYPESAFE_API_KEY", "")
    SECRETS.extend([tg, jev])
    m = re.search(r"api[-_]?key=([^&]+)", rpc_url)
    if m:
        SECRETS.append(m.group(1))
    out(f"probe.py  python {sys.version.split()[0]}  rpc_url {rpc_url}")
    results: dict[str, str] = {}

    # 1. PumpPortal migration
    mint = args.mint
    migration = None
    try:
        msgs, migration = await wait_migration(args.wait_min * 60)
        save("pumpportal_migration.json", msgs)
        if migration:
            dump_paths("PumpPortal migration message", migration)
            mint = mint or migration.get("mint")
            if not mint:
                out("  no key named 'mint' in the message; see the keys above")
            results["PumpPortal"] = "OK"
        else:
            results["PumpPortal"] = "connected, no graduation in time"
    except Exception as e:
        results["PumpPortal"] = f"FAIL {type(e).__name__}: {e}"
        out(f"PumpPortal FAIL: {e}")

    if not mint:
        out("\nNo mint to test. Rerun with:  python desk\\probe.py --mint <a graduated pump.fun mint>")
        mint = None

    async with httpx.AsyncClient(headers={"User-Agent": "desk-probe/1"}, follow_redirects=True) as c:
        # 2. RPC getSlot
        try:
            j = await rpc(c, rpc_url, "getSlot")
            out(f"\nRPC getSlot -> {j.get('result')}  {j.get('error') or ''}")
            results["RPC getSlot"] = "OK" if "result" in j else f"FAIL {j.get('error')}"
        except Exception as e:
            results["RPC getSlot"] = f"FAIL {e}"
            out(f"RPC getSlot FAIL: {e}")

        # 3. Axiom link: is the address in your sample link a mint or a pool?
        try:
            r = await c.get(f"https://api.dexscreener.com/latest/dex/pairs/solana/{AXIOM_SAMPLE}", timeout=20)
            as_pair = (r.json() or {}).get("pairs") or []
            r2 = await c.get(f"https://api.dexscreener.com/tokens/v1/solana/{AXIOM_SAMPLE}", timeout=20)
            as_token = r2.json() if r2.status_code == 200 else []
            kind = "POOL (pair address)" if as_pair else ("MINT (token address)" if as_token else "unknown")
            out(f"\nAxiom sample link address {AXIOM_SAMPLE} is a {kind}")
            results["Axiom link address"] = kind
        except Exception as e:
            results["Axiom link address"] = f"FAIL {e}"

        if mint:
            out(f"\nTest mint: {mint}")
            # 4. DexScreener
            try:
                r = await c.get(f"https://api.dexscreener.com/tokens/v1/solana/{mint}", timeout=20)
                pairs = r.json()
                save("dexscreener_pair.json", pairs)
                out(f"DexScreener HTTP {r.status_code}, {len(pairs) if isinstance(pairs, list) else '?'} pair(s)")
                if isinstance(pairs, list) and pairs:
                    dump_paths("DexScreener pair[0]", pairs[0])
                    out("  dexIds: " + ", ".join(str(p.get("dexId")) for p in pairs))
                results["DexScreener"] = f"HTTP {r.status_code}"
            except Exception as e:
                results["DexScreener"] = f"FAIL {e}"

            # 5. RugCheck report + insiders graph
            for name, path in (("rugcheck_report.json", "report"), ("rugcheck_insiders_graph.json", "insiders/graph")):
                try:
                    r = await c.get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/{path}", timeout=30)
                    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
                    save(name, data)
                    out(f"\nRugCheck {path}: HTTP {r.status_code}")
                    if r.status_code == 200:
                        dump_paths(f"RugCheck {path}", data)
                    results[f"RugCheck {path}"] = f"HTTP {r.status_code}"
                except Exception as e:
                    results[f"RugCheck {path}"] = f"FAIL {e}"

            # 6. RPC samples
            for name, method, params in (
                ("rpc_getAccountInfo.json", "getAccountInfo", [mint, {"encoding": "jsonParsed"}]),
                ("rpc_getTokenSupply.json", "getTokenSupply", [mint]),
                ("rpc_getTokenLargestAccounts.json", "getTokenLargestAccounts", [mint]),
            ):
                try:
                    j = await rpc(c, rpc_url, method, params)
                    save(name, j)
                    out(f"\nRPC {method}: {'OK' if 'result' in j else j.get('error')}")
                    if "result" in j:
                        dump_paths(f"RPC {method}", j["result"])
                    results[f"RPC {method}"] = "OK" if "result" in j else f"FAIL {j.get('error')}"
                except Exception as e:
                    results[f"RPC {method}"] = f"FAIL {e}"

            # 7. LAYER 8 PROOF
            out("\nLAYER 8 PROOF: getProgramAccounts filtered by mint, jsonParsed")
            total, ok = 0, True
            for prog, filters in ((TOKEN_PROGRAM, [{"dataSize": 165}, {"memcmp": {"offset": 0, "bytes": mint}}]),
                                  (TOKEN_2022, [{"memcmp": {"offset": 0, "bytes": mint}}])):
                t0 = time.time()
                try:
                    j = await rpc(c, rpc_url, "getProgramAccounts",
                                  [prog, {"encoding": "jsonParsed", "filters": filters}], timeout=60)
                    if "result" in j:
                        n = len(j["result"])
                        total += n
                        out(f"  {prog[:8]}...: {n} accounts in {time.time() - t0:.1f}s")
                        if prog == TOKEN_PROGRAM and j["result"]:
                            save("rpc_getProgramAccounts_sample.json", j["result"][:3])
                    else:
                        ok = False
                        out(f"  {prog[:8]}...: REFUSED {j.get('error')}")
                except Exception as e:
                    ok = False
                    out(f"  {prog[:8]}...: FAIL {type(e).__name__}: {e}")
            results["LAYER 8 PROOF"] = f"PASS, {total} accounts" if ok else "FAIL (need another rpc_url)"
            out(f"\nAxiom link the desk would send: {links.coin_url(mint)}  (mint version)")

        # 8. Telegram
        if tg:
            try:
                me = (await c.get(f"https://api.telegram.org/bot{tg}/getMe", timeout=20)).json()
                out(f"\nTelegram bot: @{me.get('result', {}).get('username')}  ok={me.get('ok')}")
                upd = (await c.get(f"https://api.telegram.org/bot{tg}/getUpdates", timeout=20)).json()
                owner = None
                for u in upd.get("result", []):
                    msg = u.get("message") or {}
                    if (msg.get("text") or "").startswith("/start"):
                        owner = msg["chat"]["id"]
                        break
                if owner is None:
                    results["Telegram"] = "bot OK, but nobody sent /start yet"
                    out("  send /start to your bot in Telegram, then rerun probe.py")
                else:
                    (HERE / "logs").mkdir(exist_ok=True)
                    (HERE / "logs" / "owner.json").write_text(json.dumps({"owner_chat_id": owner}))
                    url = links.coin_url(mint or AXIOM_SAMPLE)
                    r = await c.post(f"https://api.telegram.org/bot{tg}/sendMessage", json={
                        "chat_id": owner,
                        "text": "PAPER · Grok Bot test push. The button opens Axiom.",
                        "reply_markup": {"inline_keyboard": [[{"text": "Open on Axiom", "url": url}]]},
                    }, timeout=20)
                    results["Telegram"] = f"test push sent={r.json().get('ok')}, owner_chat_id saved"
                    out(f"  owner_chat_id saved, test push ok={r.json().get('ok')}, button url {url}")
            except Exception as e:
                results["Telegram"] = f"FAIL {type(e).__name__}"
        else:
            results["Telegram"] = "skipped"

        # 9. Jev
        if jev:
            try:
                r = await c.post("https://api.typesafe.ai/v1/systemone",
                                 headers={"Authorization": f"Bearer {jev}"},
                                 json={"model": "jev-latest",
                                       "state": "A test. The sky is blue today.",
                                       "questions": {"blue": {"type": "noul",
                                                              "instructions": "The sky is blue today"}}},
                                 timeout=20)
                j = r.json() if "json" in r.headers.get("content-type", "") else {"text": r.text[:300]}
                save("jev_test.json", j)
                out(f"\nJev HTTP {r.status_code}: answers={j.get('answers')} usage={j.get('usage')} model={j.get('model')}")
                results["Jev"] = f"HTTP {r.status_code}"
            except Exception as e:
                results["Jev"] = f"FAIL {type(e).__name__}: {e}"
        else:
            results["Jev"] = "skipped (Analyst will run in rules mode)"

    out("\n===== SUMMARY =====")
    for k, v in results.items():
        out(f"  {k:32} {v}")
    RAW.mkdir(parents=True, exist_ok=True)
    (RAW / "REPORT.txt").write_text("\n".join(report), encoding="utf-8")
    print(f"\nWrote {RAW / 'REPORT.txt'}. Send me that file (it has no keys in it).")


if __name__ == "__main__":
    asyncio.run(main())
