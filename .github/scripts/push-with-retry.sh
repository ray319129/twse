#!/usr/bin/env bash
#
# 共用的 git push 重試(2026-08-13 新增)。用法:
#
#     bash .github/scripts/push-with-retry.sh ["失敗後果的一句話說明"]
#
# ## 為什麼需要這支
#
# 2026-08-13 `Branch Chips (nightly)` 白跑一整輪:分點抓完、parquet 寫好、commit 也做了,
# **最後 `git push` 被 GitHub 以 `remote: Internal Server Error` 拒絕**,
# runner 一銷毀當天資料就沒了。GitHub 側的隨機 500,沒有重試 = 一次平台抖動白跑 40 分鐘。
#
# 當時五支 workflow 全是同一段寫法(`git pull --rebase origin main || true` + 裸 `git push`),
# 所以這個 500 打到 daily.yml 就是掉一整天的選股。抽成共用腳本,一處修全部生效。
#
# ## 兩個刻意的設計
#
# 1. **每次嘗試前都先 rebase**。機器人 commit 很頻繁(盤中每分鐘都有),
#    push 前落後於 main 是常態而非例外,所以 pull 放在迴圈裡、不是只做一次。
#
# 2. **rebase 撞衝突就 `--abort`,不用 `|| true` 吞掉**。
#    舊寫法的 `|| true` 會留下「rebase 進行中」的狀態,接著的 push 會推出半套結果。
#    寧可這輪失敗(資料類的 job 隔天 catchup 會補),也不要污染 main。
#
set -e

NOTE="${1:-}"

for i in 1 2 3; do
  # 落後 main 是常態 —— 每次嘗試前都對齊,而不是只在第一次
  git pull --rebase origin main || { git rebase --abort || true; }

  if git push; then
    echo "pushed (attempt $i)"
    exit 0
  fi

  echo "push 失敗(第 $i 次)"
  if [ "$i" -lt 3 ]; then
    sleep $((i * 5))          # 退避 5 / 10 秒
  fi
done

echo "push 連續 3 次失敗。${NOTE}"
exit 1
