#!/usr/bin/env python3
"""
一次性回溯扫描：用多个搜索词交叉覆盖 DexScreener，
找出最近 N 天内创建 + 热点达标 + 市值≥$300K 的 Solana 代币，收集庄地址。

用法: python3 backfill.py [回溯天数，默认7]
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

# 复用 tracker.py 的基础设施（加载 .env 等）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tracker import (
    log, http_get, http_post, load_json, save_json, now_iso,
    fetch_token_info, fetch_top_holders, send_telegram, export_csv,
    DATA_DIR, TOKENS_FILE, HOLDERS_FILE, WHALES_CSV,
    MIN_MCAP, MATURE_HOURS, TOP_N, MIN_TXNS_HOT, MIN_VOLUME_HOT,
    SOLANA_RPC, TG_TOKEN, TG_CHAT_ID,
)

DEX_BASE = "https://api.dexscreener.com"

# 搜索词列表：用多种关键词交叉覆盖 Solana meme 币
# 基础代币黑名单（非meme币，跳过）
SKIP_TOKENS = {
    "So11111111111111111111111111111111111111112",  # SOL (wrapped)
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}

SEARCH_TERMS = [
    "pump", "meme", "sol", "moonshot", "based", "raydium",
    "cto", "dev", "chad", "dog", "cat", "pepe", "wojak",
    "solana", "ai", "agent",
]


def search_pairs(term: str) -> list[str]:
    """搜索 DexScreener，返回代币地址列表"""
    url = f"{DEX_BASE}/latest/dex/search?q={term}"
    try:
        data = http_get(url)
        pairs = data.get("pairs") or []
        addrs = []
        for p in pairs:
            if p.get("chainId") != "solana":
                continue
            base = p.get("baseToken") or {}
            addr = base.get("address", "")
            if addr:
                addrs.append(addr)
        return addrs
    except Exception as e:
        log(f"  搜索 '{term}' 失败: {e}")
        return []


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    log("=" * 50)
    log(f"🔍 回溯扫描: 最近 {days} 天内创建的热点币")
    log(f"   搜索词: {len(SEARCH_TERMS)} 个 | 市值≥${MIN_MCAP:,} | "
        f"txns≥{MIN_TXNS_HOT} | vol≥${MIN_VOLUME_HOT:,}")

    # Step 1: 多关键词交叉搜索
    log("[1/4] 多关键词搜索...")
    all_addrs: list[str] = []
    for term in SEARCH_TERMS:
        addrs = search_pairs(term)
        all_addrs.extend(addrs)
        time.sleep(0.3)  # 避免触发限流
    all_addrs = list(dict.fromkeys(all_addrs))
    log(f"  去重后: {len(all_addrs)} 个代币地址")

    # Step 2: 逐个查详情 + 筛选
    log("[2/4] 逐个检查详情...")
    candidates = []
    n_total, n_not_sol, n_old, n_cold, n_low_mcap = 0, 0, 0, 0, 0

    for addr in all_addrs:
        n_total += 1

        # 跳过基础代币
        if addr in SKIP_TOKENS:
            n_not_sol += 1
            continue

        ti = fetch_token_info(addr)
        if ti is None:
            n_not_sol += 1
            continue

        # 时间筛选：创建时间在回溯范围内
        created_ms = ti["pair_created_at"]
        if created_ms and created_ms > 0:
            created_dt = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
        else:
            continue  # 没有创建时间，跳过

        if created_dt < cutoff:
            n_old += 1
            continue

        # 热度筛选
        is_hot = (ti["txns_24h"] >= MIN_TXNS_HOT or
                  ti["volume_24h"] >= MIN_VOLUME_HOT)
        if not is_hot:
            n_cold += 1
            continue

        # 市值筛选（对于回溯，可能有些币已过6h但市值还在）
        if ti["market_cap"] < MIN_MCAP:
            n_low_mcap += 1
            continue

        age_hours = (datetime.now(timezone.utc) - created_dt).total_seconds() / 3600
        candidates.append((addr, ti, age_hours))
        log(f"  ✅ {ti['symbol']:12s} MCap=${ti['market_cap']:>10,.0f}  "
            f"txns={ti['txns_24h']}  age={age_hours:.0f}h")

    log(f"  扫描: {n_total} → 过期:{n_old} 非Sol:{n_not_sol} 冷门:{n_cold} "
        f"低市值:{n_low_mcap} → ✅候选:{len(candidates)}")

    if not candidates:
        log("没有找到符合条件的代币")
        return

    # Step 3: 收集庄地址
    log("[3/4] 收集庄地址...")
    holders_db = load_json(HOLDERS_FILE, {"holders": {}}).get("holders", {})
    tokens_db = load_json(TOKENS_FILE)
    all_new = []

    for addr, ti, age_h in candidates:
        if not SOLANA_RPC:
            log(f"  跳过 {ti['symbol']}: 未配RPC")
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

        # 记录到 tokens_db
        tokens_db[addr] = {
            "first_seen_at": now_iso(),
            "status": "collected",
            "symbol": ti["symbol"],
            "name": ti["name"],
            "market_cap": ti["market_cap"],
            "txns_24h": ti["txns_24h"],
            "volume_24h": ti["volume_24h"],
            "age_hours": round(age_h, 1),
            "dex": ti.get("dex", ""),
            "holders_collected": True,
            "source": "backfill",
        }
        log(f"  {ti['symbol']}: +{n_this}新地址 MCap${ti['market_cap']:,.0f}")

    save_json(HOLDERS_FILE, {"holders": holders_db, "updated_at": now_iso()})
    save_json(TOKENS_FILE, tokens_db)

    # Step 4: 导出 + 推送
    log("[4/4] 导出CSV + Telegram推送...")
    export_csv(holders_db, tokens_db)

    if all_new:
        by_token = {}
        for w in all_new:
            k = w["token_symbol"] or w["token_address"][:8]
            by_token.setdefault(k, []).append(w)
        lines = [f"🔍 <b>回溯扫描结果</b> ({days}天, +{len(all_new)}地址)"]
        for sym, whales in list(by_token.items())[:15]:
            w0 = whales[0]
            lines.append(
                f"\n<b>{sym}</b> MCap ${w0['market_cap']:,.0f} | "
                f"{len(whales)}个新地址"
            )
            for w in whales[:3]:
                short = w["address"][:6] + "..." + w["address"][-4:]
                lines.append(f"  #{w['rank']} <code>{short}</code>")
        if send_telegram("\n".join(lines)):
            log(f"  Telegram推送成功: {len(all_new)} 个新地址")

    n_multi = sum(1 for h in holders_db.values() if h.get("appearances", 0) >= 2)
    log(f"📊 新增候选币:{len(candidates)} 庄地址:{len(holders_db)}(≥2次:{n_multi})")
    log("完成 ✅")


if __name__ == "__main__":
    main()
