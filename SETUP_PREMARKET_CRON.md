# 盤前/盤中「準時」觸發設定(方案 A:外部 cron → GitHub workflow_dispatch)

> 目的:GitHub 內建 `schedule` 會被排隊**延遲 5~30 分**;改用免費外部排程(cron-job.org)
> 在精準時間呼叫 GitHub 的 `workflow_dispatch` API,觸發的 run **通常幾秒內啟動** → 準時、免開電腦、免費。
> 程式都已就緒,你只需做下面兩步(約 10 分鐘,一次性)。

三條排程(台北,週一~五):
- **盤前 08:45**(premarket preopen) · UTC 00:45
- **盤中 09:25**(premarket orb) · UTC 01:25
- **盤後 21:30**(daily 選股)· UTC 13:30 —— ⚠️ 別早於 ~15:30(台股 13:30 收盤後 yfinance 當日 K 棒約需 1.5~2 小時才齊)。
  **2026-07-18 由 16:00 改為 21:30**:券商分點 21:00 才發布,16:00 跑只吃得到「昨天」的籌碼。改 21:30 後,
  分點逆向因子(`scoring.branch_bonus`)才會真正生效,且三大法人(20:00)/融資券(21:00)/外資持股(21:00)
  也一併變成**當天**資料而非昨天。程式端已做保護:查當日分點若尚未發布會回空 → 自動不加成,不會用到過期籌碼。

---

## 步驟 1:建立 GitHub 細粒度 PAT(權限最小化)

1. 開 https://github.com/settings/personal-access-tokens/new (Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token)
2. 設定:
   - **Token name**:`premarket-cron`
   - **Expiration**:90 天(到期前 GitHub 會寄信提醒,屆時重建貼新的即可)
   - **Resource owner**:`ray319129`
   - **Repository access**:選 **Only select repositories** → 勾 `ray319129/twse`
   - **Permissions** → **Repository permissions** → 找 **Actions** → 設 **Read and write**
     (其餘維持 No access;Metadata 會自動變 Read-only,正常)
3. 按 **Generate token**,**複製** `github_pat_…`(只會顯示一次)。

> 這個 token 只能對「twse 這個 repo 的 Actions」動作,無法讀你的程式碼內容或其他 repo,風險可控。

---

## 步驟 2:在 cron-job.org 建兩個排程

1. 到 https://cron-job.org 免費註冊、登入。
2. **建第一個(盤前)**:Create cronjob
   - **Title**:`premarket preopen`
   - **URL**:`https://api.github.com/repos/ray319129/twse/actions/workflows/premarket.yml/dispatches`
   - **Schedule**:Time zone 選 **Asia/Taipei**;時間 **08:45**;星期勾 **Mon–Fri**(只留週一到週五)。
     （若介面只吃 UTC,就設 **00:45 Mon–Fri**。）
   - 切到 **Advanced**(進階)分頁:
     - **Request method**:`POST`
     - **Headers**(逐行新增):
       ```
       Accept: application/vnd.github+json
       Authorization: Bearer github_pat_你的token
       X-GitHub-Api-Version: 2022-11-28
       Content-Type: application/json
       User-Agent: premarket-cron
       ```
     - **Request body**:
       ```json
       {"ref":"main","inputs":{"phase":"preopen"}}
       ```
   - 儲存。
3. **建第二個(盤中 ORB)**:同上,只改三處
   - **Title**:`premarket orb`
   - **時間**:**09:25**(或 UTC **01:25**)Mon–Fri
   - **Request body**:`{"ref":"main","inputs":{"phase":"orb"}}`
4. **建第三個(盤後 daily 選股)**:同樣的 Headers,但改 URL/時間/body
   - **Title**:`daily screener`
   - **URL**(注意是 `daily.yml`):`https://api.github.com/repos/ray319129/twse/actions/workflows/daily.yml/dispatches`
   - **時間**:**21:30**(或 UTC **13:30**)Mon–Fri　※2026-07-18 由 16:00 改為 21:30,讓選股吃得到當天的券商分點(21:00 發布);別早於 15:30(見上方說明)
   - **Request body**:`{"ref":"main"}`　(daily 不需 phase;會正常跑當天並提交資料)

---

## 驗證

- 在 cron-job.org 對任一個排程按「**Run now / 立即執行**」測試:
  - 成功回應是 **HTTP 204**(No Content)= GitHub 已接受觸發。
  - 到 GitHub → repo → **Actions** → **Premarket Watch**,應看到一個 run 幾秒內啟動。
- 若回 **404**:多半是 token 權限不足(Actions 要 Read **and write**)或 repo 路徑打錯。
- 若回 **401**:token 錯/過期。

> 備援:你隨時可在 **Actions → Premarket Watch → Run workflow** 手動觸發(選 phase),不依賴 cron-job.org。

---

## 注意事項

- **premarket 與 daily 都已移除 GitHub 內建 schedule**:現在三條都只由「cron-job.org 觸發」或「手動」啟動。
  好處是不再延遲、也不會一天收到「準時 + 遲到」兩封重複信;代價是若 cron-job.org 當天掛了,
  那天就不會自動跑(可在 Actions → 對應 workflow → Run workflow 手動補)。
- **daily 是整個系統的源頭**:它沒跑當天就沒有核心選股,隔天 premarket 也會空跑。所以 daily 那條 cron 尤其別漏設;若哪天 cron-job.org 出狀況,記得手動補跑 daily。
- **仍要先有當日盤後核心選股**才有內容:premarket 讀最近一次 Daily Screener 的核心 10;
  若那天核心 0 檔,它會乾淨跳過(不寄信、不報錯)。
- PAT 到期(90 天)後記得到 cron-job.org 兩個排程把 Authorization 換成新 token。

---

## 第四~十條 cron:全市場快照存檔(2026-07-19 新增)

FinMind Sponsor 的全市場即時快照**存下來才是自己的** —— 訂閱到期後沒有任何 API
補得回均價/量比/委買賣。所以每個交易日固定存 7 個檢查點。

**Workflow:** `Market Snapshot Archive`(`.github/workflows/snapshot.yml`),
與 premarket 一樣走 `workflow_dispatch`,但多帶一個 `tag` 參數。

Request body 範例(把 `TAG` 換成該時點):
```json
{"ref":"main","inputs":{"tag":"0900"}}
```

| 台北時間 | tag | 為什麼要這個點 |
|---|---|---|
| 09:00 | `0900` | 開盤缺口 —— 台帳 27 筆跳空棄單,被棄的平均 −18.94%,這規則很值錢 |
| 09:30 | `0930` | 開盤半小時強弱 → 收盤(經典當沖命題) |
| 10:00 | `1000` | 盤中是否守住開盤方向 |
| 11:00 | `1100` | 同上 |
| 12:00 | `1200` | 同上 |
| 13:00 | `1300` | 尾盤前 |
| 13:30 | `1330` | **最重要** —— 當日 VWAP / 量比定案,因子研究主要用這一份 |

> 只想設一條的話就設 **13:30 那條**;其餘六條是為了做「盤中型態 → 收盤」研究才需要。

**成本:** 一份快照 parquet 約 150 KB,7 點/日 ≈ 0.4 MB/日、8 MB/月。
API 額度用 7/6000 per hour,可以忽略。

> ⚠️ **不要改成每分鐘跑。** 額度撐得住(6000/hr),但 repo 撐不住 —— 每分鐘存一次
> 是 900 MB/月。瓶頸是儲存不是額度。要高頻只用於「盤中監控」(不存檔)。

**非交易日會自己跳過:** 快照的時間戳不是今天(假日/颱風假/尚未更新)就不寫檔,
job 也不會變紅。訂閱到期後同樣乾淨跳過。

---

## 盤中訊號掃描 cron(2026-07-19 新增)

**Workflow:** `Intraday Signal Scan`(`.github/workflows/intraday.yml`),帶 `mode` 參數。

| 台北時間 | mode | 說明 |
|---|---|---|
| 08:50 | `levels` | **必要** —— 盤前算好均線/前高快取。沒跑這條,盤中掃描會直接跳過 |
| 09:05–13:25 每 5 分鐘 | `scan` | 全市場掃描 |

Request body:
```json
{"ref":"main","inputs":{"mode":"scan"}}
```

> cron-job.org 支援 `*/5 9-13 * * 1-5` 這種寫法,一條排程就能涵蓋整個盤中。

**每 5 分鐘不會變成每 5 分鐘一封信:** 去重狀態存在 `data/alerts/YYYY-MM-DD.json`,
**同一檔同一種訊號當日只通知一次**。沒有新訊號時不寄信、不 commit、不留痕跡。

**成本:** 每次掃描 1 次 API 呼叫(全市場快照)。整個盤中約 54 次 = 額度的 0.9%。

---

## ⚠️ 盤中掃描與分點已改為「全自動」,不需要設 cron(2026-07-20)

前面「盤中訊號掃描 cron」那一節**已作廢**。現在兩條 workflow 都用 GitHub 內建 schedule,
**使用者不必手動點,也不必到 cron-job.org 設定**:

| Workflow | 觸發(台北) | 做什麼 |
|---|---|---|
| `Intraday Signal Scan` | 每個工作日 **08:20** | 自動重建 levels → 睡到 09:00 → 每 20 秒掃到 13:35;<br>途中每 10 分鐘更新內外盤比/走勢線,7 個檢查點存全市場快照 |
| `Branch Chips (nightly)` | 每個工作日 **21:40** | 分點籌碼 + 連買連賣(分點 21:00 才發布) |

### 為什麼這兩條可以用內建 schedule,daily/premarket 卻不行

內建 schedule 會延遲 5~30 分鐘。daily/premarket 在乎準時,所以走外部 cron。
但盤中這條 **job 自己會等到 09:00 才開始掃**(`intraday_scan._sleep_until`),
所以延遲變成無害 —— 早觸發就等、晚觸發就直接開始。分點那條晚十幾分鐘也沒差。

### 已不再需要的排程
- ~~08:50 `mode=levels`~~ → watch 模式會自動重建過期的 levels
- ~~09:05–13:25 每 5 分鐘 `mode=scan`~~ → 改成單一常駐 job
- ~~`Market Snapshot Archive` 的 7 條~~ → 併進盯盤迴圈的檢查點
  (`snapshot.yml` 保留供手動補跑)

### 台灣假日
沒有內建假日行事曆,但**不需要**:快照的時間戳不是今天時 `scan` 會自己跳過、
不寄信、不 commit,job 乾淨結束。

---

## ⚠️ 盤中掃描請「加設」外部 cron(2026-07-21 補)

`intraday.yml` 用了 GitHub 內建 `schedule: "20 0 * * 1-5"`(台北 08:20),
但 **2026-07-20、07-21 連續兩天都沒有自動觸發** —— 同一天 premarket(走外部 cron)
卻準時跑了 08:45 與 09:25。

**GitHub 內建 schedule 在高負載時會延遲甚至整個略過**,這正是本專案當初把
daily / premarket 改走外部 cron 的原因。我原本以為「job 自己等開盤」就能免疫,
但那隻解決「延遲」,**解決不了「根本沒觸發」**。

→ **在 cron-job.org 加一條**(內建 schedule 保留當備援,兩者同時觸發也沒關係,
`concurrency: intraday` 會擋掉重複執行):

| 台北時間 | Workflow | Request body |
|---|---|---|
| 每個工作日 **08:20** | `intraday.yml` | `{"ref":"main","inputs":{"mode":"watch"}}` |
| 每個工作日 **21:40** | `chips.yml` | `{"ref":"main","inputs":{}}` |

URL 與 Header 與 premarket 那條相同,只是把 workflow 檔名換掉:
```
https://api.github.com/repos/ray319129/twse/actions/workflows/intraday.yml/dispatches
```

### 怎麼確認它有在跑
盤中開 `docs/freshness.json`(或 GitHub 上的該檔案),裡面有 **`heartbeat`** 欄位:
```json
{"heartbeat":"2026-07-21 10:32:15","polls":389,"fired_today":3,"checked":866}
```
時間戳在幾分鐘內 = job 活著。**沒有這個欄位或時間很舊 = job 沒在跑**,不是「今天沒訊號」。
