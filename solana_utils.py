#!/usr/bin/env python3
"""
Solana 链上工具 — 底层方法，零依赖，纯Python标准库

核心功能:
  fetch_real_holders(mint, top_n=20) → 只返回真正的个人钱包（owner=System Program）

过滤逻辑:
  代币账户(ATA) → 解析 owner → 验证 owner 是否由 System Program 控制
  ✅ System Program = 私钥钱包 = 真正的个人/庄钱包
  ❌ 其他 Program = DEX合约/Pump.fun AMM/订单簿 = 死地址

跨模块使用: from solana_utils import fetch_real_holders
"""

from __future__ import annotations

import base64
import json
import os
import urllib.request
from typing import Any

# ============================================================
# 常量
# ============================================================

# Solana 原生 System Program — 所有普通钱包的 owner
SYSTEM_PROGRAM = "11111111111111111111111111111111"

# SPL Token Program (标准)
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

# Token-2022 Program (扩展)
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

# 自动加载 .env
def _load_dotenv() -> None:
    """从项目根 .env 加载环境变量（不覆盖已有值）"""
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

# 默认 RPC
DEFAULT_RPC = os.environ.get("SOLANA_RPC_URL", "")

# Base58 字母表
_B58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


# ============================================================
# Base58
# ============================================================

def b58encode(data: bytes) -> str:
    """bytes → Solana 地址格式"""
    if not data:
        return ""
    zeros = sum(1 for b in data if b == 0)
    # 遇到第一个非零字节就停（前导零计数）
    leading = 0
    for b in data:
        if b == 0:
            leading += 1
        else:
            break
    n = int.from_bytes(data, "big")
    chars = []
    while n > 0:
        n, rem = divmod(n, 58)
        chars.append(_B58_ALPHABET[rem])
    return "1" * leading + "".join(chr(c) for c in reversed(chars))


# ============================================================
# RPC 调用
# ============================================================

def _rpc(method: str, params: list[Any], rpc_url: str | None = None,
         retries: int = 2) -> dict:
    """底层 JSON-RPC 调用，自动重试"""
    import time as _time
    url = rpc_url or DEFAULT_RPC
    if not url:
        raise ValueError("未配置 SOLANA_RPC_URL")
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": method, "params": params,
    }).encode()
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=payload, headers={
                "Content-Type": "application/json",
                "User-Agent": "solana-utils/1.0",
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except Exception as e:
            last_err = e
            if attempt < retries:
                _time.sleep(1 * (attempt + 1))  # 递增等待
    raise last_err


# ============================================================
# 代币账户解析
# ============================================================

def get_token_largest_accounts(mint: str, rpc_url: str | None = None) -> list[dict]:
    """
    getTokenLargestAccounts → 代币账户地址列表
    返回: [{"address": "ATA地址", "amount": "原始数量", "decimals": 6}, ...]
    """
    data = _rpc("getTokenLargestAccounts", [mint], rpc_url)
    return data.get("result", {}).get("value") or []


def resolve_ata_owners(
    ata_addresses: list[str],
    rpc_url: str | None = None,
) -> dict[str, str]:
    """
    批量解析代币账户的 owner（真正持有者地址）

    输入: ATA 地址列表
    返回: {ATA地址: owner地址, ...}

    解析 SPL Token Account 结构 (base64 data):
      bytes [0..31]   = mint
      bytes [32..63]  = owner  ← 我们需要的
      bytes [64..71]  = amount (u64 LE)
    """
    if not ata_addresses:
        return {}

    data = _rpc("getMultipleAccounts", [
        ata_addresses,
        {"encoding": "base64"},
    ], rpc_url)
    accts = data.get("result", {}).get("value") or []

    result: dict[str, str] = {}
    for addr, acct in zip(ata_addresses, accts):
        if acct is None:
            continue
        raw = acct.get("data", [])
        if not raw or len(raw) == 0 or not isinstance(raw[0], str):
            continue
        decoded = base64.b64decode(raw[0])
        if len(decoded) < 64:
            continue
        owner = b58encode(decoded[32:64])
        result[addr] = owner
    return result


def is_real_wallet(
    addresses: list[str],
    rpc_url: str | None = None,
) -> dict[str, bool]:
    """
    批量判断地址是否是真正的个人钱包

    规则: owner program == System Program (111111...111)
    ✅ 普通钱包 (私钥控制)
    ❌ DEX合约、Pump.fun AMM、订单簿、代币账户 等

    返回: {地址: True/False}
    """
    if not addresses:
        return {}

    data = _rpc("getMultipleAccounts", [
        addresses,
        {"encoding": "base64"},
    ], rpc_url)
    accts = data.get("result", {}).get("value") or []

    result: dict[str, bool] = {}
    for addr, acct in zip(addresses, accts):
        if acct is None:
            result[addr] = False
            continue
        owner_program = acct.get("owner", "")
        result[addr] = (owner_program == SYSTEM_PROGRAM)
    return result


# ============================================================
# 钱包行为分析
# ============================================================

def count_token_interactions(address: str, max_check: int = 21,
                            rpc_url: str | None = None) -> int:
    """
    快速检查钱包交互过的代币种类数。
    用 limit=max_check+1 截断: 如果返回 >= max_check+1 说明"至少这么多"。
    这样避免了拉取海王地址的几千条记录。
    """
    total = 0
    for program_id in [TOKEN_PROGRAM, TOKEN_2022_PROGRAM]:
        try:
            data = _rpc("getTokenAccountsByOwner", [
                address,
                {"programId": program_id},
                {"encoding": "base64"},
                {"limit": max_check + 1},  # 截断
            ], rpc_url)
            accts = data.get("result", {}).get("value") or []
            total += len(accts)
            if total > max_check:
                return total  # 已经超了，不用继续
        except Exception:
            pass
    return total


def filter_low_token_count(
    addresses: list[str],
    max_tokens: int = 20,
    rpc_url: str | None = None,
) -> dict[str, dict]:
    """
    批量检查地址的交互币种数，排除"太花心"的地址

    返回: {地址: {"token_count": N, "is_focused": True/False}}
    """
    results: dict[str, dict] = {}
    for addr in addresses:
        try:
            count = count_token_interactions(addr, rpc_url)
            results[addr] = {
                "token_count": count,
                "is_focused": count <= max_tokens,
            }
        except Exception:
            results[addr] = {"token_count": 999, "is_focused": False}
    return results


# ============================================================
# 交易频率检查 — 排除高频机器人
# ============================================================

def check_wallet_activity(
    addresses: list[str],
    max_txn_per_day: int = 10,
    rpc_url: str | None = None,
) -> dict[str, dict]:
    """
    批量检查地址的链上活跃度，识别高频机器人

    对每个地址调 getSignaturesForAddress(limit=100)，
    统计最近24小时内的交易数。

    返回: {
      地址: {
        "total_txn": 100,          # 获取的签名总数
        "txn_24h": 15,             # 最近24h交易数
        "is_low_frequency": False, # 是否是低频地址（日交易≤阈值）
        "first_txn_age_hours": 72.3, # 最早一笔交易距今多久
      }
    }
    """
    from datetime import datetime, timedelta, timezone

    if not addresses:
        return {}

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    results: dict[str, dict] = {}

    for addr in addresses:
        try:
            data = _rpc("getSignaturesForAddress", [
                addr,
                {"limit": 100},
            ], rpc_url)
            sigs = data.get("result", [])
            total = len(sigs)

            # 统计最近24h内交易数
            txn_24h = 0
            first_ts = None
            for s in sigs:
                bt = s.get("blockTime")
                if bt:
                    dt = datetime.fromtimestamp(bt, tz=timezone.utc)
                    if first_ts is None:
                        first_ts = dt
                    if dt > cutoff:
                        txn_24h += 1

            # 最早交易距今多久
            first_age_hours = None
            if first_ts:
                first_age_hours = round(
                    (datetime.now(timezone.utc) - first_ts).total_seconds() / 3600, 1
                )

            is_low_freq = txn_24h <= max_txn_per_day

            results[addr] = {
                "total_txn": total,
                "txn_24h": txn_24h,
                "is_low_frequency": is_low_freq,
                "first_txn_age_hours": first_age_hours,
            }
        except Exception:
            # 查询失败保守排除
            results[addr] = {
                "total_txn": 0,
                "txn_24h": 999,
                "is_low_frequency": False,
                "first_txn_age_hours": None,
            }

    return results


# ============================================================
# 一站式方法 — 真正的庄钱包
# ============================================================

def fetch_real_holders(
    mint: str,
    top_n: int = 20,
    max_txn_per_day: int = 10,
    max_token_interactions: int = 20,
    rpc_url: str | None = None,
) -> list[dict]:
    """
    获取代币前N大持仓中真正的个人庄钱包

    四步筛选:
      1. getTokenLargestAccounts → 代币账户地址
      2. 解析每个ATA的 owner 地址
      3. 验证 owner == System Program（排除DEX合约/AMM等）
      4. 验证 日交易 ≤ max_txn_per_day（排除高频机器人/做市商/sniper bot）

    返回: [
      {
        "rank": 1,
        "address": "xxx",        # 真正钱包地址
        "ata": "xxx",            # 代币账户地址(链上验证用)
        "amount": 123456.78,     # 持仓数量
        "amount_raw": "xxx",     # 原始数量
        "is_real_wallet": True,  # 通过System Program验证
        "txn_24h": 3,            # 最近24h交易数
        "is_low_frequency": True,# 低频地址
      }, ...
    ]

    用法:
      from solana_utils import fetch_real_holders
      holders = fetch_real_holders("token_mint", top_n=20, max_txn_per_day=10)
    """
    if not rpc_url:
        rpc_url = DEFAULT_RPC
    if not rpc_url:
        return []

    # Step 1: 获取代币账户
    token_accounts = get_token_largest_accounts(mint, rpc_url)
    if not token_accounts:
        return []

    # 只取前 top_n 个 ATA — 不强凑
    candidates = token_accounts[:top_n]

    # Step 2: 解析 owner
    ata_addrs = [t["address"] for t in candidates]
    ata_to_owner = resolve_ata_owners(ata_addrs, rpc_url)

    # Step 3: 验证哪些 owner 是真钱包（System Program）
    all_owners = list(set(ata_to_owner.values()))
    wallet_check = is_real_wallet(all_owners, rpc_url)

    # Step 4: 验证低频交易
    real_owners = [o for o in all_owners if wallet_check.get(o, False)]
    frequency_check = check_wallet_activity(real_owners, max_txn_per_day, rpc_url)

    # Step 5: 验证交互币种数
    token_count_check = filter_low_token_count(real_owners, max_token_interactions, rpc_url)

    # Step 6: 组合过滤
    real: list[dict] = []
    for item in candidates:
        ata = item["address"]
        owner = ata_to_owner.get(ata)
        if not owner:
            continue
        if not wallet_check.get(owner, False):
            continue
        freq = frequency_check.get(owner, {})
        if not freq.get("is_low_frequency", False):
            continue
        tkc = token_count_check.get(owner, {})
        if not tkc.get("is_focused", False):
            continue

        dec = item.get("decimals", 0)
        amt_raw = item["amount"]
        amt = float(amt_raw)
        if dec > 0:
            amt /= (10 ** dec)

        real.append({
            "rank": len(real) + 1,
            "address": owner,
            "ata": ata,
            "amount": round(amt, 2),
            "amount_raw": amt_raw,
            "is_real_wallet": True,
            "txn_24h": freq.get("txn_24h", 0),
            "is_low_frequency": freq.get("is_low_frequency", False),
            "token_count": tkc.get("token_count", 0),
            "is_focused": tkc.get("is_focused", False),
        })

        if len(real) >= top_n:
            break

    return real


# ============================================================
# 快速测试
# ============================================================

if __name__ == "__main__":
    # 测试：HBULL 前10真钱包
    HBULL = "7V6Sk63y8Rr1MvcN5mYNp61wgFhy4EeQg5gUASk9pump"
    print(f"RPC: {DEFAULT_RPC[:50]}...")
    print(f"\n=== HBULL 前10 真正钱包 (System Program) ===\n")
    holders = fetch_real_holders(HBULL, top_n=10)
    if holders:
        for h in holders:
            print(f"  #{h['rank']:2d} {h['address']}  ({h['amount']:>15,.0f})")
        print(f"\n共 {len(holders)} 个真钱包")
    else:
        print("  无结果 (检查 RPC 配置)")
