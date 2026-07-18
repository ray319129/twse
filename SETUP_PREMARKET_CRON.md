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
