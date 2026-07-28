#!/usr/bin/env python3
"""
Solana 庄钱包追踪器 — 零外部依赖，纯Python标准库

流程:
  1. 从 DexScreener 发现 Solana 新代币 → 记入 tokens.json (pending)
  2. 对存活≥6h 的 pending 代币查市值 → ≥$300K → qualified
  3. 对 qualified 代币通过 Solana RPC 拉前20持仓 → 存入 holders.json
  4. 新发现的庄地址 → Telegram 推送 + 导出 whales.csv

数据源 (全部免费):
  - DexScreener API (token-profiles + token-boosts): 发现新币
  - DexScreener API (token info): 查市值、创建时间
  - Solana RPC (推荐 Helius 免费 tier, 10万次/天): getTokenLargestAccounts

环境变量:
  TELEGRAM_BOT_TOKEN  — Telegram Bot Token
  TELEGRAM_CHAT_ID    — Telegram 目标 Chat ID
  SOLANA_RPC_URL      — Solana RPC (默认不配置则跳过holder收集)
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone


def _load_dotenv() -> None:
    """把 .env 文件加载到环境变量（不覆盖已有值）"""
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except FileNotFoundError:
        pass


_load_dotenv()

# ============================================================
# 配置
# ============================================================

DATA_DIR = "data"
TOKENS_FILE = f"{DATA_DIR}/tokens.json"
HOLDERS_FILE = f"{DATA_DIR}/holders.json"
WHALES_CSV = f"{DATA_DIR}/whales.csv"
LOG_FILE = f"{DATA_DIR}/run.log"

DEX_BASE = "https://api.dexscreener.com"
SOLANA_RPC = os.environ.get("SOLANA_RPC_URL", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

MIN_MCAP = 300_000          # $300K
MATURE_HOURS = 6            # 存活满6小时才检查
TOP_N = 20                  # 每个币收集前N持仓
BATCH_SIZE = 30             # 每批最多查N个token info
BATCH_SLEEP = 2             # 批次间等待(秒)

# 热点筛选 (模拟 GMGN 热点早晚报的逻辑)
MIN_TXNS_HOT = 200          # 24h买卖笔数≥200 → 有热度
MIN_VOLUME_HOT = 30_000     # 24h交易量≥$30K → 有热度


# ============================================================
# 工具函数
# ============================================================

def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def http_get(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers={"User-Agent": "whale-tracker/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def http_post(url: str, data: dict, timeout: int = 30):
    payload = json.dumps(data).encode()
    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
        "User-Agent": "whale-tracker/1.0",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def load_json(path: str, default=None):
    if default is None:
        default = {}
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


# ============================================================
# Step 1: 发现新代币
# ============================================================

def discover_tokens() -> list:
    addresses = []
    endpoints = [
        f"{DEX_BASE}/token-profiles/latest/v1",
        f"{DEX_BASE}/token-boosts/latest/v1",
    ]
    for url in endpoints:
        label = url.split("/")[-2]
        try:
            data = http_get(url)
            sol = [item["tokenAddress"] for item in data if item.get("chainId") == "solana"]
            addresses.extend(sol)
            log(f"  {label}: +{len(sol)} Solana token")
        except Exception as e:
            log(f"  {label} 失败: {e}")
    return list(dict.fromkeys(addresses))


# ============================================================
# Step 2: 查token详情
# ============================================================

def fetch_token_info(address: str) -> dict | None:
    url = f"{DEX_BASE}/latest/dex/tokens/{address}"
    try:
        data = http_get(url)
    except Exception as e:
        log(f"    DexScreener查失败 {address[:10]}...: {e}")
        return None
    pairs = data.get("pairs") or []
    if not pairs:
        return None
    sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
    pair = sol_pairs[0] if sol_pairs else pairs[0]
    base = pair.get("baseToken") or {}
    liq = pair.get("liquidity") or {}
    mcap = pair.get("marketCap")
    created_ms = pair.get("pairCreatedAt") or 0
    txns = pair.get("txns") or {}
    vol = pair.get("volume") or {}
    h24 = txns.get("h24") or {}
    return {
        "symbol": base.get("symbol", ""),
        "name": base.get("name", ""),
        "market_cap": float(mcap) if mcap else 0,
        "price_usd": float(pair.get("priceUsd") or 0),
        "liquidity_usd": float(liq.get("usd") or 0),
        "pair_created_at": created_ms,
        "pair_created_iso": ms_to_iso(created_ms) if created_ms else "",
        "dex": pair.get("dexId", ""),
        "txns_24h": int(h24.get("buys", 0) or 0) + int(h24.get("sells", 0) or 0),
        "volume_24h": float(vol.get("h24", 0) or 0),
    }


def check_matured_tokens(tokens_db: dict) -> list:
    pending = [(a, i) for a, i in tokens_db.items() if i.get("status") == "pending"]
    if not pending:
        return []
    log(f"  待检查: {len(pending)} 个")

    qualified = []
    n_no_data, n_low_mcap, n_young, n_cold = 0, 0, 0, 0

    for i in range(0, len(pending), BATCH_SIZE):
        batch = pending[i:i + BATCH_SIZE]
        for addr, info in batch:
            ti = fetch_token_info(addr)
            if ti is None:
                info["retry_count"] = info.get("retry_count", 0) + 1
                if info["retry_count"] >= 3:
                    info["status"] = "dead"
                    n_no_data += 1
                continue

            info["symbol"] = ti["symbol"]
            info["name"] = ti["name"]
            info["market_cap"] = ti["market_cap"]
            info["price_usd"] = ti["price_usd"]
            info["liquidity_usd"] = ti["liquidity_usd"]
            info["pair_created_at"] = ti["pair_created_at"]
            info["pair_created_iso"] = ti["pair_created_iso"]
            info["dex"] = ti["dex"]
            info["txns_24h"] = ti["txns_24h"]
            info["volume_24h"] = ti["volume_24h"]
            info["last_checked_at"] = now_iso()

            # 判断是否存活 ≥6h
            created_ms = ti["pair_created_at"]
            if created_ms and created_ms > 0:
                pair_dt = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
                age_hours = (datetime.now(timezone.utc) - pair_dt).total_seconds() / 3600
            else:
                first_seen = info.get("first_seen_at", "")
                try:
                    fs_dt = datetime.fromisoformat(first_seen)
                    age_hours = (datetime.now(timezone.utc) - fs_dt).total_seconds() / 3600
                except (ValueError, TypeError):
                    age_hours = 0

            matured = age_hours >= MATURE_HOURS

            if not matured:
                n_young += 1
                info["age_hours"] = round(age_hours, 1)
                continue

            # 热点判断 (模拟 GMGN 热点早晚报)
            is_hot = (ti["txns_24h"] >= MIN_TXNS_HOT or
                      ti["volume_24h"] >= MIN_VOLUME_HOT)

            if not is_hot:
                info["status"] = "skipped_cold"
                n_cold += 1
                continue

            if ti["market_cap"] >= MIN_MCAP:
                info["status"] = "qualified"
                info["age_hours"] = round(age_hours, 1)
                qualified.append((addr, ti))
            else:
                info["status"] = "skipped"
                n_low_mcap += 1

        if i + BATCH_SIZE < len(pending):
            time.sleep(BATCH_SLEEP)

    log(f"  结果: ✅{len(qualified)}达标  ❌市值不足{n_low_mcap}  "
        f"🥶非热点{n_cold}  ⏳未成熟{n_young}  💀已死{n_no_data}")
    return qualified


# ============================================================
# Step 3: 收集前20持仓
# ============================================================

def fetch_top_holders(mint_address: str) -> list:
    if not SOLANA_RPC:
        return []
    try:
        data = http_post(SOLANA_RPC, {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenLargestAccounts",
            "params": [mint_address],
        })
        result = data.get("result", {}).get("value") or []
        holders = []
        for i, item in enumerate(result[:TOP_N]):
            dec = item.get("decimals", 0)
            amt = float(item["amount"])
            if dec > 0:
                amt /= (10 ** dec)
            holders.append({
                "rank": i + 1,
                "address": item["address"],
                "amount_raw": item["amount"],
                "amount": round(amt, 2),
            })
        return holders
    except Exception as e:
        log(f"    RPC失败 {mint_address[:10]}...: {e}")
        return []


# ============================================================
# Telegram
# ============================================================

def send_telegram(text: str) -> bool:
    if not TG_TOKEN or not TG_CHAT_ID:
        return False
    try:
        http_post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", {
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
        return True
    except Exception as e:
        log(f"  Telegram失败: {e}")
        return False


# ============================================================
# CSV导出
# ============================================================

def export_csv(holders_db: dict, tokens_db: dict) -> None:
    addr_to_sym = {}
    for t_addr, info in tokens_db.items():
        if info.get("symbol"):
            addr_to_sym[t_addr] = info["symbol"]

    rows = []
    for h_addr, h_info in holders_db.items():
        tlist = h_info.get("tokens", [])
        recent = tlist[-1] if tlist else h_info.get("first_seen_with", "")
        sym = addr_to_sym.get(recent, "") or recent[:10]
        rows.append((
            h_addr,
            h_info.get("appearances", 1),
            sym,
            h_info.get("first_seen_at", ""),
            "|".join(tlist),
        ))
    rows.sort(key=lambda r: r[1], reverse=True)

    with open(WHALES_CSV, "w") as f:
        f.write("address,appearances,recent_token,first_seen_at,token_list\n")
        for r in rows:
            f.write(f"{r[0]},{r[1]},{r[2]},{r[3]},{r[4]}\n")
    log(f"  CSV已导出: {len(rows)} 条 → {WHALES_CSV}")


# ============================================================
# 主流程
# ============================================================

def main() -> None:
    log("=" * 50)
    rpc_status = "已配置" if SOLANA_RPC else "❌未配"
    tg_status = "已配置" if (TG_TOKEN and TG_CHAT_ID) else "❌未配"
    log(f"🐳 庄钱包追踪器 | MCap≥${MIN_MCAP:,} | ≥{MATURE_HOURS}h | RPC:{rpc_status} | TG:{tg_status}")

    tokens_db = load_json(TOKENS_FILE)
    holders_db = load_json(HOLDERS_FILE, {"holders": {}}).get("holders", {})

    # Step 1
    log("[1/4] 发现新代币...")
    new_addrs = discover_tokens()
    n_new = 0
    for addr in new_addrs:
        if addr not in tokens_db:
            tokens_db[addr] = {
                "first_seen_at": now_iso(),
                "status": "pending",
                "symbol": "", "name": "", "market_cap": 0,
                "retry_count": 0,
            }
            n_new += 1
    log(f"  新发现: {n_new} (总追踪: {len(tokens_db)})")

    # Step 2
    log("[2/4] 检查成熟代币...")
    qualified = check_matured_tokens(tokens_db)
    save_json(TOKENS_FILE, tokens_db)

    # Step 3
    log("[3/4] 收集庄地址...")
    all_new = []
    for addr, ti in qualified:
        if tokens_db[addr].get("holders_collected"):
            continue
        if not SOLANA_RPC:
            log(f"  跳过 {ti['symbol'] or addr[:10]}: 未配SOLANA_RPC_URL")
            continue
        holders = fetch_top_holders(addr)
        if not holders:
            continue
        n_this = 0
        for h in holders:
            ha = h["address"]
            if ha not in holders_db:
                holders_db[ha] = {
                    "first_seen_with": addr,
                    "first_seen_at": now_iso(),
                    "appearances": 1,
                    "tokens": [addr],
                }
                all_new.append({**h, "token_symbol": ti["symbol"],
                                "token_address": addr, "market_cap": ti["market_cap"]})
                n_this += 1
            else:
                holders_db[ha]["appearances"] += 1
                if addr not in holders_db[ha]["tokens"]:
                    holders_db[ha]["tokens"].append(addr)
        tokens_db[addr]["holders_collected"] = True
        tokens_db[addr]["status"] = "collected"
        if n_this:
            log(f"  {ti['symbol'] or addr[:10]}: +{n_this}新地址 MCap${ti['market_cap']:,.0f}")

    save_json(HOLDERS_FILE, {"holders": holders_db, "updated_at": now_iso()})
    save_json(TOKENS_FILE, tokens_db)

    # Step 4
    log("[4/4] 导出CSV + Telegram推送...")
    export_csv(holders_db, tokens_db)

    if all_new:
        by_token = {}
        for w in all_new:
            k = w["token_symbol"] or w["token_address"][:8]
            by_token.setdefault(k, []).append(w)
        lines = [f"🐳 <b>新庄地址告警</b> (+{len(all_new)} 个)\n"]
        for sym, whales in list(by_token.items())[:10]:
            w0 = whales[0]
            lines.append(f"<b>{sym}</b> MCap ${w0['market_cap']:,.0f} (+{len(whales)}地址)")
            for w in whales[:5]:
                short = w["address"][:6] + "..." + w["address"][-4:]
                lines.append(f"  #{w['rank']} <code>{short}</code>")
        if len(by_token) > 10:
            lines.append(f"\n... 还有 {len(by_token) - 10} 个币")
        if send_telegram("\n".join(lines)):
            log(f"  Telegram推送成功: {len(all_new)} 个新地址")
    else:
        log("  无新庄地址，跳过推送")

    # 统计
    n_pending = sum(1 for i in tokens_db.values() if i.get("status") == "pending")
    n_qualified = sum(1 for i in tokens_db.values() if i.get("status") == "qualified")
    n_collected = sum(1 for i in tokens_db.values() if i.get("status") == "collected")
    n_skipped = sum(1 for i in tokens_db.values() if i.get("status") == "skipped")
    n_cold = sum(1 for i in tokens_db.values() if i.get("status") == "skipped_cold")
    n_dead = sum(1 for i in tokens_db.values() if i.get("status") == "dead")
    n_multi = sum(1 for h in holders_db.values() if h.get("appearances", 0) >= 2)

    log(f"📊 追踪:{len(tokens_db)} 待查:{n_pending} 达标:{n_qualified} "
        f"已收:{n_collected} 淘汰:{n_skipped} 冷门:{n_cold} 死:{n_dead} "
        f"庄地址:{len(holders_db)}(≥2次:{n_multi})")
    log("完成 ✅")


if __name__ == "__main__":
    main()
