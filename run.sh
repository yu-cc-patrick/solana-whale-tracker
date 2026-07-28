#!/bin/bash
set -e
cd "$(dirname "$0")"

echo "[$(date -Iseconds)] run.sh 开始"

# 拉取远端最新状态
git pull origin main 2>&1 || echo "[warn] git pull 失败，继续用本地状态"

# 执行采集
python3 tracker.py

# 配置 git 身份 (GitHub Actions 需要)
git config user.email "bot@whale-tracker.local"
git config user.name "Whale Tracker Bot"

# 提交状态文件
git add data/tokens.json data/holders.json data/whales.csv data/run.log

if ! git diff --staged --quiet; then
    git commit -m "auto: update data [skip ci]" --allow-empty
    git push origin main 2>&1 || echo "[warn] git push 失败"
    echo "[$(date -Iseconds)] 状态已提交"
else
    echo "[$(date -Iseconds)] 无变化，跳过提交"
fi
