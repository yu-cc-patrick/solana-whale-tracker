# 🐳 Solana Whale Tracker

> **一句话**：自动发现 Solana 上的新代币，抓出持仓前 20 的钱包地址，把新出现的"庄钱包"实时推送到 Telegram。
>
> **状态**：🟢 运行中 · 58 次提交 · 每 15 分钟自动运行
>
> **为什么有价值**：跟庄的前提是知道庄的地址，但没有人会主动告诉你。这个系统用"同一地址在多个币的前 20 大持仓里反复出现"作为行为指纹，把隐藏的关联地址浮出来。

自动发现 Solana 上存活 ≥6h 且市值仍 >$300K 的代币，收集前 20 大持仓地址（庄钱包），Telegram 实时推送。

## 原理

```
DexScreener API              6h 后复查市值
  (新币发现)                     │
     │                          ▼
     ▼                     ≥$300K? ──→ Solana RPC
  记入追踪库                       getTokenLargestAccounts
  (tokens.json)                        │
                                       ▼
                                 前20持仓地址
                                       │
                              ┌────────┴────────┐
                              ▼                 ▼
                         holders.json       Telegram 推送
                         + whales.csv       新庄地址告警
```

- **发现**: 每 15 分钟从 DexScreener 的 token-profiles + token-boosts 拉取 Solana 新代币
- **筛选**: 存活 ≥6h + 市值 ≥$300K（天然过滤 rug pull）
- **收集**: 通过 Solana RPC 获取前 20 大持仓地址
- **推送**: 新发现的庄地址实时推送 Telegram
- **去重**: 同一地址出现在多个币中 → 标记为高频庄钱包（CSV 中按出现次数降序）

## 快速开始

### 1. 克隆并配置

```bash
git clone <your-repo-url>
cd solana-whale-tracker
cp .env.example .env
# 编辑 .env 填入你的 Telegram Bot Token 和 Chat ID
```

### 2. 获取免费 API Key

| 服务 | 用途 | 注册地址 | 免费额度 |
|------|------|---------|---------|
| Telegram Bot | 推送通知 | [@BotFather](https://t.me/BotFather) | 无限 |
| Helius | Solana RPC | [helius.dev](https://www.helius.dev/) | 10万次/天 |

Helius 注册 30 秒: Sign Up → 创建 API Key → 复制 `https://mainnet.helius-rpc.com/?api-key=xxx` 到 `.env`

### 3. 本地运行

```bash
# 零外部依赖，Python 3.10+ 即可
python3 tracker.py
```

### 4. 部署到 GitHub Actions

1. 把项目 push 到 GitHub
2. 在仓库 Settings → Secrets and variables → Actions 添加三个 Secrets:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `SOLANA_RPC_URL`
3. GitHub Actions 每 15 分钟自动运行，状态和结果自动 git commit 回仓库

## 输出说明

| 文件 | 内容 |
|------|------|
| `data/tokens.json` | 代币追踪状态（pending → qualified → collected） |
| `data/holders.json` | 庄地址库，记录每个地址出现在哪些币中 |
| `data/whales.csv` | 庄地址 CSV 导出，按出现次数降序 |
| `data/run.log` | 运行日志 |

### CSV 字段

`address, appearances, recent_token, first_seen_at, token_list`

- **appearances** ≥2 的地址 = 高频庄钱包，重点关注
- **token_list** 包含该地址出现过的所有币

## 与手工流程对比

| 步骤 | 手工 (GMGN) | 自动化后 |
|------|------------|---------|
| 发现新币 | 手动刷 GMGN | DexScreener API 自动发现 |
| 筛选存活币 | 脑记/手记 | 代码判断 6h+300K |
| 收集地址 | 点进每个币手动复制 | RPC 自动拉取 |
| 集群监控 | 肉眼比对 | CSV 里 appearances≥2 自动浮出 |
| 推送通知 | 无 | Telegram 实时推 |
