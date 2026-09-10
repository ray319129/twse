#!/usr/bin/env bash
# 把當沖層的產出提交並推上去。**盯盤前後都要呼叫。**
#
# 為什麼要獨立成一支:原本只有 job 最後那一步會 commit,而盯盤要跑 4.5 小時 ——
# 於是有兩個後果:
#   1. `data/daytrade/pool-$TODAY.json` 一直到 13:35 才進 repo,而防止重複盯盤的
#      檔案守衛是在 checkout 出來的 repo 裡找這個檔案 → 09:25 那一班永遠找不到,
#      **兩個盯盤迴圈同時跑、Discord 收到雙份通知**(2026-09-10 實際發生,兩班都手動砍掉)。
#   2. 盯盤中途掛掉的話,一整天的池子與台帳全部丟失(這個專案已經被這個模式咬過三次)。
# 呼叫端:.github/workflows/daytrade.yml
set -u
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
# 逐一 add:`git add A B C` 只要有一個路徑不存在就 fatal 且整批不 stage
# (2026-07-22 盤中整天沒發布的真兇,這裡不重蹈覆轍)。
for p in data/daytrade docs/daytrade.json docs/daytrade_latency.json; do
  [ -e "$p" ] && git add "$p" 2>/dev/null || true
done
if git diff --cached --quiet; then
  echo "No daytrade changes."
  exit 0
fi
git commit -m "daytrade: ${1:-$(date -u +'%Y-%m-%d %H:%M')Z}"
bash .github/scripts/push-with-retry.sh "當沖資料未發布,下一輪會重試。"
