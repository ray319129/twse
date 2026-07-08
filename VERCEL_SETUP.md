# 個股健檢 — 即時查詢(Vercel)部署說明

> 這是「個股健檢」雙路徑設計裡的**路徑 B**:任意股票代號即時查。**路徑 A**(核心精選 + 自選池,每日批次)完全不需要這份文件就能跑,已隨 `daily.yml` 自動產生 `docs/health/*.json`。只有想要「輸入任何代號都能查」才需要做這裡的部署。

## 為什麼需要 Vercel(而不是純 GitHub Pages 就好)

GitHub Pages 是純靜態,沒有地方執行「使用者剛打的代號 → 即時打 FinMind → 算分」這件事,而且：
1. `FINMIND_TOKEN`、`ANTHROPIC_API_KEY` 絕對不能放進前端 JS(任何人按 F12 就看光)。
2. 任意股票即時查必須在伺服器端發 HTTP 請求(瀏覽器直接打 FinMind 可能撞 CORS)。

`api/health.py` 是一支 Vercel Python Serverless Function,**重用 `scripts/health/*.py` 同一套引擎**(跟每日批次完全相同的財務公式,單一事實來源),只是資料「現抓現算」,不像批次路徑有本地 parquet 累積。

## 部署步驟

1. 安裝 Vercel CLI(本機一次性):`npm i -g vercel`
2. 在 repo 根目錄:`vercel login` → `vercel link`(第一次會問專案名稱,選新建即可)
3. 設定環境變數(Vercel Dashboard → Project → Settings → Environment Variables,或用 CLI):
   ```
   vercel env add FINMIND_TOKEN production
   vercel env add ANTHROPIC_API_KEY production   # 選用;沒設 AI 解讀潤飾/新聞分析會自動降級成規則句
   ```
4. 部署:`vercel --prod`
5. 測試:`curl "https://<你的專案>.vercel.app/api/health?stock=2330"`,應該幾秒~十幾秒後回傳 JSON(第一次查某檔會比較久,因為要平行打好幾支 FinMind API)。

`vercel.json` 已設定 `outputDirectory: "docs"`,所以同一個部署**同時**會：
- 把 `docs/` 整包當靜態網站服務(等於 GitHub Pages 的替代品,網頁本身也能直接用這個網址開)
- 自動把 `api/health.py` / `api/detail.py` / `api/portfolio_ocr.py` 變成對應端點(Vercel 對 `/api` 目錄的零設定慣例)

### 另一個用到 API key 的端點:`/api/portfolio_ocr`(我的持倉 → 截圖辨識)
`api/portfolio_ocr.py` 讓網頁「我的持倉」分頁上傳券商庫存截圖 → Claude(Haiku 4.5 vision)辨識成持股清單。**需要 `ANTHROPIC_API_KEY`**(同上面 env 步驟;對這個功能是必需,不是選用)。沒設或未部署時,前端會顯示「辨識失敗…需部署 Vercel」並請使用者改用「貼上/手動」——不影響其餘功能。前端呼叫走同源相對路徑 `/api/portfolio_ocr`(沿用 `HEALTH_API_BASE`),不需另設旗標。**誠實邊界**:截圖(含成本數字)會一次性經此函式 → Anthropic 辨識,不留存;成本最終仍只由前端存 localStorage。一張截圖約 Haiku US$0.003(不到 1 美分)。

## 啟用前端的即時查詢

預設**關閉**(`docs/index.html` 裡 `HEALTH_API_ENABLED = false`),只用每日批次的 `docs/health/*.json`。確認 API 部署成功後：

- 若 Vercel 部署**就是**你主要在用的網域(docs+api 同一個 project,上面步驟的預設情況):把 `HEALTH_API_ENABLED` 改成 `true`,`HEALTH_API_BASE` 留空字串即可(同源相對路徑 `/api/health`,沒有 CORS 問題)。
- 若你還是想繼續用 GitHub Pages 當主網址,只把 Vercel 當 API 用:`HEALTH_API_BASE` 填 Vercel 網域(例如 `'https://twse-health.vercel.app'`,不要結尾斜線);`api/health.py` 已經回應 `Access-Control-Allow-Origin: *`,跨網域呼叫沒問題。

改完 commit、push(GitHub Pages 會吃到新的 `docs/index.html`;若你是用 Vercel 當主站,`vercel --prod` 重新部署一次)。

## 已知限制(部署前/後都該知道)

1. **延遲**:任意代號即時查詢要平行打 6~8 支 FinMind/TWSE API + 算技術指標,正常情況約 5~10 秒,FinMind 那端變慢時可能更久。前端已有對應的 loading 文案,**不是秒開的體驗**,跟「核心精選/自選池走批次路徑瞬開」不一樣。
2. **timeout**:`vercel.json` 設了 `maxDuration: 60`,但實際上限仍受你的 Vercel 方案影響(Hobby 方案的實際可用上限請以部署當下 Vercel 官方文件 / Dashboard 顯示為準,本文件無法保證最新數字)。長尾查詢(從未入榜過的冷門股,FinMind 第一次抓要重建較長歷史)逾時風險較高。
3. **Serverless function 體積**:這支函式 reuse 既有 `scripts/` 程式碼,間接依賴 `requirements.txt` 列的所有套件(含 `yfinance`)。**第一次部署務必確認**有沒有超過 Vercel serverless function 的 unzipped 體積上限——若部署失敗或報體積錯誤,最直接的縮減方式是把 `api/health.py` 改用 FinMind 的 `TaiwanStockPrice` 資料集取代 `fetch_price_history`/`fetch_index_history`(兩者目前都走 `yfinance`),換掉最重的相依套件;這份文件先誠實列出風險、不假裝已驗證過。
4. **唯讀檔案系統**:Vercel serverless function 的檔案系統大多是唯讀的(`/tmp` 除外)。`scripts/storage.py` 已加了寫入失敗保護(`_try_write_parquet`,2026-06-30 commit)——寫入失敗只會記警告、不會讓整次查詢 500,但也代表**即時查詢不會在 Vercel 端累積快取**,每次都是真的「現抓現算」。
5. **FinMind 免費額度**:個人低流量使用(你自己偶爾查幾檔)在免費額度內沒問題;若這個網址被分享出去、流量變大,可能撞到 FinMind 免費版的速率限制,屆時查詢會優雅降級成「資料不足」而非整包報錯,但體驗會變差。
6. **同業平均**:即時路徑讀的是 `docs/health/industry_benchmarks.json`(每日批次產生、隨 `docs/` 一起發布的靜態檔),不會在 Vercel 端重新聚合全市場——這份檔案的新鮮度跟著你最近一次 `daily.yml` 跑的時間,不是即時的。

## 本機測試(不必先部署)

```bash
npm i -g vercel
vercel dev
# 另開一個終端機:
curl "http://localhost:3000/api/health?stock=2330"
```

`vercel dev` 會用本機環境變數(讀 `.env` 或你 shell 裡已 export 的 `FINMIND_TOKEN`/`ANTHROPIC_API_KEY`),不需要先部署到雲端就能測整條路徑。
