# 台股短線選股系統 — 交接文件 (HANDOFF)

> 給新對話的冷啟動說明。讀完這份就能接續開發。最後更新隨 commit 同步(以 `git log` 為準)。
> repo: github.com/ray319129/twse · 分支 main · 平台 Windows + Python(CI 用 3.11)
> ⚠️ **慣例(使用者明確要求):每完成一件實質改動,結束前一定先補這份 HANDOFF 再 push,別等提醒。**

---

## 0. 一句話定位
盤後在 **GitHub Actions** 自動跑的台股**短線**(隔日沖/隔週/月內)選股系統:全市場用免費資料算 0~100 信心分 → 排序出「核心 10 + 觀察 20」→ 寄 Email + 更新互動網頁(GitHub Pages),並**自動追蹤每檔選股後續績效**(含止盈止損出場模擬)。完全免費、無自有伺服器(2026-06-30 起多一層**選用**的 Vercel serverless,見第 10 節,不影響原本完全免費的批次流程)。

**2026-06-30 新增「個股健檢」模組**(`scripts/health/`):財務體質/成長能力/估值分析/風險分析/技術面/籌碼分析/新聞分析七個 Engine,可解釋(每個數字附公式/來源/更新時間)、可換投資風格權重,見第 10 節。

使用者(Ray)是**短線交易者**,用富邦證券。重點訴求:不要追高、要能驗證勝率、視覺要現代化。

---

## 1. 架構與資料流

```
外部 cron(cron-job.org)~16:00 台北 → workflow_dispatch (.github/workflows/daily.yml)
  ※ GitHub 內建 schedule 已移除(會延遲);觸發見 SETUP_PREMARKET_CRON.md
  └ python -m scripts.main
      1. fetch_stock_info → filter_tradable_stocks(全市場約 1900 檔)
      2. fetch_valuation_snapshot(TWSE 估值,1 次免費) + fetch_index_history(^TWII 大盤,1 次)
      3. 逐檔:load_prices(本機 parquet,增量更新) → compute_all + compute_relative_strength
         → scoring.compute_conviction(只用免費資料算信心分)
      4. 排序 → 核心(trigger 且分≥min_score,取前10) / 觀察(brewing,取前20)
      5. 只對「核心 + 自選池」約15檔補抓 FinMind 籌碼/財報(控 API 額度)+ 算進場計畫
      6. track.build_report:回看所有歷史核心選股,出場模擬 + 績效台帳
      7. 輸出檔案 + 寄 Email + git commit data/ docs/
          ↓
GitHub Pages (Settings→Pages: main /docs) → docs/index.html 讀 docs/data.json
```

### 檔案地圖
- `scripts/main.py` — 主流程 `daily_run()`;`_json_safe`(NaN→null)、`_enrich_pick`、`STRATEGY_LABEL`。
- `scripts/scoring.py` — **信心分** `compute_conviction()`:趨勢25+相對強度25+短線時機量能25+品質15+流動性10,過熱(5日>22%/乖離20MA>18%/RSI>88/漲停)× 0.55 重罰。回傳 trigger/brewing/exhausted/profile 等。
- `scripts/track.py` — **出場模擬 + 績效**:`compute_entry_plan()`(停損/TP1/A·B·C價位線)、`compute_position_size()`(2026-06-27 加,資金×風險%÷R 反推建議張數)、`_net_return()`(扣手續費/證交稅/滑價)、`_simulate_exit()`(隔日開盤進場+跳空保護,2026-06-27 加催化劑放寬棄單門檻)、`build_report()`(台帳/勝率/各天期/出場統計)。可 `python -m scripts.track` 單獨跑。
- `scripts/indicators.py` — 手刻指標(MA/KD/MACD/RSI/ATR/布林/bb_width)、`compute_relative_strength`(rs_line/rs_ratio,需大盤)。
- `scripts/screener.py` — 舊 12 策略 + 4 combo + 4 領先訊號(現降為自選池標籤用)。
- `scripts/fetchers.py` — yfinance 價格/指數(**已 dropna(close)**)、FinMind 籌碼/財報、TWSE 估值(`fetch_valuation_snapshot`)+ TPEx 上櫃估值(`fetch_valuation_snapshot_tpex`,2026-06-27 加)、Google News。
- `scripts/storage.py` — parquet 讀寫;`load_prices` **讀取時忽略 NaN 收盤列**。
- `scripts/{config,industry,notify,utils}.py`、`templates/daily_email.html`、`docs/index.html`(SPA)。
  - `docs/index.html` 前端輔助(2026-06-26 加):`slink(id)` 把股票代號(全分頁)做成連結 → `cmoney.tw/forum/stock/<代號>`(新分頁看走勢/技術線圖);`whyPanel(s)` 核心卡可摺疊「為什麼選這檔(解讀)」,把信心分五維(`s.trend/rs/setup/quality/liquidity`)+stage-2 加成+題材 `evidence`+`risk_flags` 翻成白話,**純用既有資料、零 API**。改 SPA 後務必 `node --check`(抽 `<script>` 驗語法)。
- **盤前自動看盤(獨立於盤後,見第 9 節)**:`scripts/premarket.py`、`templates/premarket_email.html`、`.github/workflows/premarket.yml`;輸出 `docs/premarket.json`,網頁「盤中即時」分頁讀它。
- **個股健檢(2026-06-30 新增,獨立於盤後排序,見第 10 節)**:`scripts/health/`(9 個 Engine + orchestrator)、`api/health.py`(Vercel 即時查詢,選用)、`vercel.json`、`VERCEL_SETUP.md`;輸出 `docs/health/{代號}.json` + `docs/health/index.json` + `docs/health/industry_benchmarks.json`,網頁「個股健檢」分頁讀它。
- `config/screeners.yaml` — 所有可調參數(見下)。
- `data/` — prices/、signals/{date}.json、performance.json、meta/、health/(同業平均彙總)。`docs/` — data.json、dates.json、history/{date}.json、health/(個股健檢報告)。

### config/screeners.yaml 可調區塊
- `ranking`: core_count 10 / watch_count 20 / min_score 45 / min_dollar_volume 3000萬 / enrich_top_n 30
- `scoring`: 信心分權重/門檻全抽進此區塊(預設=原硬寫值);含 stage-2 重排四加成 `chip_bonus`(籌碼)/`fundamental_bonus`(FinMind 季財報:毛利/營益/ROE/負債/現金流,[fundamentals.py](scripts/fundamentals.py))/`catalyst_bonus`(Claude Haiku 對30天新聞做事件分類,[catalyst.py](scripts/catalyst.py),需 `ANTHROPIC_API_KEY`、沒設自動略過)/`industry_bonus`(2026-06-27 加,身處當下最強產業前 top_n 名才加分,預設關閉)。各 `enabled:false` 可關。詳見 [STRATEGY.md](STRATEGY.md)。
  - **法人目標價摘錄(2026-06-26 加,同一支 catalyst.py LLM call,零額外成本)**:同一次 Haiku 呼叫多問一欄 `target_prices`(每筆 broker/price/asof/evidence,evidence 必為新聞原文引用,寧缺勿濫)。**誠實邊界**:只是「新聞剛好有報導才抓到」,不是完整即時目標價清單——查證過台灣沒有免費合法 API 能拿到完整法人目標價或券商分點資料(分點/目標價皆已查證見專案記憶,別重查)。不影響信心分(`catalyst_score` 只算 catalysts,不算 target_prices),純資訊呈現。網頁 `docs/index.html` 核心卡顯示一行(`tp` 變數)+ `whyPanel` 展開原文引用;email `templates/daily_email.html` 同步顯示一行。
- 新 secret(選用):`ANTHROPIC_API_KEY`(GitHub Actions secret)→ 啟用新聞催化劑 AI 分類(Haiku,每日數美分);沒設則 catalyst_bonus 恆 0、不報錯。
- `entry`: max_chase 0.03(隔日開盤 ±3% 以上跳空棄單);`catalyst_chase`(2026-06-27 加,預設關閉)強催化劑+帶量時放寬開高棄單門檻。
- `exit`: hard_stop 0.07 / r_multiple 2.0 / max_hold_days 30 / momentum{struct_lookback 2, ma_stop 5, trail_ma 5} / swing{10,20,10} / trail{atr_mult 1.5, min_pct .03, max_pct .07}
- `cost`(交易成本,2026-06-27 加,2026-06-27 同日修正稅率): fee_rate 0.001425×fee_discount 0.6 / **tax_rate 0.003(一般證交稅)** / tax_rate_daytrade 0.0015(僅 hold_days==0 才用)/ slippage_pct 0.001
- `account`(建議風險倉位,2026-06-27 加,預設關閉): enabled false / capital 0 / risk_pct 0.01
- `health`(個股健檢,2026-06-30 加,見第 10 節): enabled true / quarters 20 / per_history_days 1825 / weights.{value_investing,growth_investing,short_term} / risk_cap / news / ai_summary / industry_min_sample

---

## 2. 核心設計決策(與原因)

1. **排序器 ≠ 過濾器**:舊版「符合就全列」某日醞釀區達 731 檔=沒篩,且抓到當天漲停股(追高)。改成信心分排序只取核心10+觀察20。
2. **免費資料優先 + 只對前段補 FinMind**:FinMind 免費版會 402 爆額度(舊版對上百檔抓籌碼)。全市場排序只用 yfinance + TWSE 估值;只核心+自選抓 FinMind(~75 次)。
3. **隔日開盤進場 + 跳空保護**:盤後算的收盤價隔天會因跳空失效。模擬改以隔日開盤成交;開高/開低超過 max_chase 即「跳空棄單」,算真實已實現勝率。
4. **R 倍數 + 移動停利**:依使用者專業框架。硬停損 −7%、TP1=進場+2R、突破後移動停利(動能5MA/波段20MA);回檔幅度用 **ATR 自適應**取代「股本分級」(repo 無股本資料,意圖相同,日後有大型股清單可改回)。
5. **風格分流**:profile=動能 或 breakout → 動能流;否則波段流。
6. **A/B/C 決策卡**:每檔核心給「平盤/開高/開低」三劇本的具體價位+動作,使用者盤中照表手動執行。

---

## 3. 踩過的雷(務必記得,別重蹈)
- **NaN 進 JSON**:Python `json.dump` 把 `float('nan')` 寫成裸字 `NaN` → 非法 JSON → 瀏覽器 `fetch().json()` 整包失敗。**所有輸出已過 `_json_safe`**。`default=str` 擋不住(nan 可序列化)。檢查別用子字串("NaN" 會出現在 Google 新聞 base64 網址裡),要用嚴格解析 `json.loads(t, parse_constant=...)`。
- **NaN 收盤壞 K 棒**:yfinance 對剛收盤未定的最新棒偶爾回 NaN 收盤 → 均線全毀 → 評分全 None → 核心 0 檔。已三層 dropna(fetch 兩處 + load_prices)。
- **sticky `<th>`**:`position:sticky` 在非捲動容器會讓表頭浮到資料中間錯位。已移除。
- **merge conflict markers**:本專案歷史曾被 `<<<<<<<` 衝突標記汙染整段函式;改檔後務必 `python -m py_compile scripts/*.py` 驗證。
- **enrich 資料抓取順序**(2026-07-07 修 Bug 1):`_enrich_pick` 必須「先抓籌碼/財報,再呼叫 `screen_stock`」。若 `screen_stock` 在前,D 籌碼類/E 基本面類與**全部 combos 永遠 False**(2026-07-06 審查:11 天 355 檔命中 0 次)。加策略時別把資料抓取搬回 screen 之後。
- **量比分母別含今日**(2026-07-07 修 Bug 2):`vol_ratio` 分母用 `vol_ma5.shift(1)`(前 5 日均量)。含今日會稀釋自己的分母、數學上限被壓到 5.0,系統性低估爆量。`vol_ma5`/`vol_ma20`(兩均量比值)維持含今日不受影響。
- **籌碼合併用 combine_first + 重疊回補**(2026-07-07 修 Bug 3):三大法人(~16:00)/融資券(~21:00)/外資持股(隔日)出表時間差 → 當天跑那列後兩者是 NaN。`upsert_chips` 用 `combine_first`(新 NaN 不覆蓋舊值)、`_update_chips` 從 `last-4d` 重疊回補,否則缺洞永久補不回。外資 30 日變化一律用「日期差」不用「位置差」(序列有洞位置差會飄到 40~60 天)。

## 4. 離線測試法(關鍵,無需網路/API)
本機有 ~1976 個 `data/prices/*.parquet`(到約 6/18)。可 monkeypatch 網路函式跑完整 `daily_run`:
```python
import scripts.main as M
M.fetch_stock_info=lambda *a,**k: <cached info df>
M.fetch_valuation_snapshot=lambda *a,**k: {}
M.fetch_index_history=lambda *a,**k: <equal-weight proxy from closes>
M.fetch_price_history=lambda *a,**k: pd.DataFrame()   # 不動 data 檔
M.fetch_chips_history / fetch_monthly_revenue = lambda: pd.DataFrame()
# ⚠️ 2026-07-07 起 EPS 不再由 main._update_eps 打 API,改由 update_fundamentals(fetch_financial_statements)
#    的 fin["eps"] 取得;離線要壓 EPS 就 patch scripts.fundamentals.fetch_financial_statements。
# ⚠️ 2026-07-07 起 daily_run 會呼叫 fetch_restricted_stocks(打 TWSE/TPEX 官方 API);離線需一併 patch:
M.fetch_restricted_stocks = lambda *a, **k: set()
M.fetch_news=lambda *a,**k: []
M.send_email=lambda *a,**k: None
M.daily_run(test_mode=True)
# 之後務必 git checkout -- data/ ; rm 測試產生的 docs/*.json 與 data/signals/<today>.json
```
驗證 JSON:`json.loads(text, parse_constant=lambda c:(_ for _ in()).throw(ValueError(c)))`。
驗證網頁 JS:抽出 `<script>` 內容 `node --check`。

### 4.1 指定基準日測試 `--date`(休市也能跑)
`python -m scripts.main --date YYYY-MM-DD`:以指定日為基準,**本機快取價格 + 大盤指數截到當天**(訊號/決策卡/相對強度都反映那天收盤、不取未來棒),跳過交易日檢查、**不抓網路增量**(結果可重現)、寄 `[測試]` 信。沒給 `--date` 時行為完全不變(跑當天)。
- GitHub Actions → Daily Screener → Run workflow → 填 `date` 欄即可雲端跑。**填了日期的手動跑會跳過 commit**(`if: github.event.inputs.date == ''`),不覆蓋線上 `data.json`;排程與日期留空的手動跑照常 commit。
- 本機跑會就地寫 `docs/*.json`、`data/signals/<date>.json` 等,測完照慣例 `git checkout -- data/ docs/` + `git clean -fd -- data/signals docs/history`。

---

## 5. 目前狀態 / 立即待辦(使用者動作)
- 6/26 每日排程已正常跑過一次(`75efce0cf data: 2026-06-26 daily update`),線上 data.json 不再是 6/18 bug 期間那份。之後每個交易日排程會自動更新,無需手動觸發。
- GitHub Pages 已啟用(/docs),網頁可載入。
- ✅ **2026-06-27:出場模擬已納入交易成本**(見下方第6節 #1)。下次排程跑完後,信件/網頁的「已實現勝率/平均報酬」會自動變成扣成本後的數字。
- ✅ **2026-06-27:大量查證網路策略/研究後,做了一輪優化**(見下方第6節 #1~#5):修正證交稅預設值、補上櫃估值資料源、加建議倉位、跳空分級、產業加成。全部預設值為「修 bug/補資料」性質的已直接啟用;新功能類(建議倉位/跳空分級/產業加成)預設關閉,要在 config 手動開。

## 6. 下一步任務
1. ~~【優先】交易成本納入出場模擬~~ ✅ **2026-06-27 已完成,且當天稍後又修正一次稅率**:新增 `config/screeners.yaml` → `cost` 區塊。`scripts/track.py` 新增 `_net_return()`:買進價×(1+滑價)×(1+手續費),賣出價×(1-滑價)×(1-手續費-證交稅)。
   - ⚠️ **稅率修正(同日)**:查證後發現「當沖證交稅減半0.15%」只適用同日買賣且需另外申請當沖權限,本系統設計是隔日開盤才進場,本來就不是當沖。`tax_rate` 預設改回一般稅率 **0.3%**;新增 `tax_rate_daytrade: 0.0015`,只有 `hold_days==0`(進場當天就出場,理論上才可能符合當沖)才套用,且系統無法驗證帳戶資格,當作邊際近似值。
   - `exit_ret` 為扣成本後淨報酬;保留 `exit_ret_gross`/`cost_pct` 供對照。`exit_sim` 摘要新增 `avg_ret_gross`/`avg_cost_pct`/`fee_rate`/`tax_rate`/`tax_rate_daytrade`/`slippage_pct`。email/網頁已同步顯示「扣前/成本」對照。詳見 [STRATEGY.md](STRATEGY.md) 第5節。
2. ✅ **2026-06-27 已完成:補接 TPEx 上櫃估值 API**(`scripts/fetchers.py` 新增 `fetch_valuation_snapshot_tpex()`,打 `tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis`,免費、零金鑰)。`scripts/main.py` 的 `daily_run()` 抓完 TWSE 估值後再合併 TPEx,解決「上櫃股估值快照常缺、品質面向恆拿中性0.5」的問題(STRATEGY.md 第10節B項)。實測約888檔上櫃股有解。
3. ✅ **2026-06-27 已完成:建議風險倉位**(`config: account`,預設關閉)。`scripts/track.py` 新增 `compute_position_size(plan, account_cfg)`:資金×風險% ÷ R(初始停損價差)反推建議張數,把 R-multiple 框架用滿(研究指出倉位規模對績效變異影響遠大於進場訊號本身)。要填自己的 `capital`/`risk_pct` 並把 `enabled` 改 true 才會在決策卡顯示。
4. ✅ **2026-06-27 已完成:跳空棄單分級**(`config: entry.catalyst_chase`,預設關閉)。強催化劑(`catalyst_signal` 達門檻)+ 進場當日帶量(量比達門檻)時,開高棄單門檻放寬到 `max_chase+extra_chase`,而非一律棄單——查證顯示「有強催化劑的突破缺口」回補率遠低於無故跳空。`scripts/track.py` `_simulate_exit()` 新增 `catalyst_signal`/`catalyst_chase_cfg` 參數;`_load_core_picks()` 多讀 `catalyst_signal`。需 `scoring.catalyst_bonus` 也開啟才有效。開低棄單規則不變(別接刀)。
5. ✅ **2026-06-27 已完成:產業相對強度納入 stage-2 排序**(`config: scoring.industry_bonus`,預設關閉)。`scripts/main.py` 把 `compute_industry_trends()` 移到 `_rank_core()` 之前算,傳入產業排名;身處當下最強產業(前 `top_n` 名)依排名線性給 0~`weight` 分加成,概念對應 IBD 產業群組排名。零額外 API,沿用既有 `industry_rows`。
6. **累積 1~2 個月真實數據後,用績效回頭調評分權重**(哪種 profile / 分數區間 / 觸發型態真有 edge)。
7. **盤中執行層(獨立大專案)**:富邦 API 即時報價 + 開盤區間/VWAP/帶量吞噬判讀 + 提醒或半自動下單。需盤中持續運行的程式(非 Actions)+ 資安考量。日線測不出盤中順序,這層才能真正執行 A/B/C。
8. (選配,暫緩,需先有真實績效資料才該做)絕對門檻→分位數排名重構(Stockopedia StockRank 式);大盤/RSI情境化規則。研究上有支持,但本系統樣本只經歷多頭、無法驗證,屬於過擬合風險,別先猜著改。

## 7. 必記前提(誠實風險)
- **策略尚未驗證有 edge**:樣本太少、過去只經歷多頭、回測未扣成本。**先紙上跟單、讓【五】歷史追蹤累積真實已實現勝率再說**,別急著實盤或斷言穩賺。
- 短線扣 0.15% 證交稅 + 手續費後要穩定贏 0050 非常難。系統價值在「縮小該盯的範圍 + 擋追高 + 客觀記錄績效」,不是印鈔機。

---

## 8. 慣例
- commit 訊息用繁中,結尾加 `Co-Authored-By: Claude ...`;改完先 py_compile + 離線跑一次驗證再 push;push 前常需 `git pull --rebase`(每日 workflow 會 commit data)。
- 別把使用者根目錄的「新增 文字文件.txt」(他貼的郵件原文)commit 進去。
- ⚠️ commit 訊息含中文/多行時,**Bash 工具**別用 PowerShell 的 `@'...'@`(會被當字面值汙染主旨);用 `git commit -F - <<'EOF' … EOF` 這種 bash heredoc。

---

## 9. 盤前自動看盤(premarket)— 已上線,獨立於盤後流程
解決「隔天開盤同時盯不了 10 檔」:盤前/開盤後自動看盤,只告訴你哪幾檔符合進場。**完全不碰盤後選股**。

### 觸發方式(重要):外部 cron → workflow_dispatch
GitHub 內建 `schedule` 會延遲 5~30 分,**已移除**;改由 **cron-job.org 在 08:45 / 09:25(台北,一~五)呼叫 `workflow_dispatch` API**(觸發的 run 通常幾秒啟動 → 準時、免開電腦)。設定步驟見 [SETUP_PREMARKET_CRON.md](SETUP_PREMARKET_CRON.md)(含建 PAT + cron-job.org)。`premarket.yml` 用 `inputs.phase`(preopen/orb)分流;也可在 Actions 分頁手動 Run workflow。若 cron-job.org 當天掛掉則該日不自動跑(可手動補)。

### 兩個 phase(`.github/workflows/premarket.yml`)
- **08:45 台北 `--phase preopen`**:讀最近一次盤後核心10 + 各自 `plan` → 抓個股『試撮/預估開盤價』(TWSE MIS API,免金鑰)+ **大盤盤前閘門(2026-07-08 升級成連續風險分數)**+ ADR 佐證 → 把每檔分類 **A平盤 / B開高 / C開低 / ❌棄單(跳空過進場上限)/ ❌作廢(開盤即破停損)**,寄信。
  - **閘門(`compute_gate`)= 各隔夜%的加權平均**,不再是 ±1 投票:**台指期夜盤(FinMind `TaiwanFuturesDaily` after_market,權重最重,台股自身重定價)** > 台積電ADR(TSM) ≈ 費半SOX > 美股期(NQ/ES);VIX 絕對≥25 或單日跳升≥15% 再扣分 → `score`(≈隔夜加權%)映射 risk-on/中性/risk-off。**方向性**:做多時只有負分(偏空)觸發減碼。
  - **族群 β(`_sector_beta`,item 2)**:用 `industry_category` 把每檔標 high(電子/半導體/光電/通訊)/med/low(金融/內需/防禦);risk-off 早盤高β檔給 `overnight_note` 減碼提示,並在排序上把高連動檔往後(低β防禦檔優先)。
  - **台指夜盤取得(`fetch_tx_night`,item 3)**:實測 FinMind after_market 標記日 D = 『D-1 傍晚開→D 清晨05:00收』夜盤,`spread_per` 即隔夜%;近月=同日同session最大量,排除價差單。**防禦**:`is_today=False`(今晨夜盤 08:45 前尚未發布)則忽略夜盤、退回美股代理。**待驗**:08:45 實跑時 FinMind 是否已發布今晨夜盤,看 log「非今日 → 改用美股代理」出現頻率。
- **09:25 台北 `--phase orb`**:對(依真實開盤判為)A 的股抓 **09:00–09:15 yfinance 1分K** → 算開盤區間高 ORH → 判 09:15 後是否**帶量突破**(`premarket.orb.volume_filter`,預設開)→ 寄信「✅已突破可進 / ⏸尚未突破 / ❌跌破區間低」。

### 設計重點 / 注意
- **ORB 用 yfinance 歷史 1分K(非即時快照)**:Actions 排程常延遲 5~15 分,歷史分K 即使晚抓 09:00–09:15 區間仍在,能正確重建;MIS 只能給「當下」快照,延遲會汙染區間。
- **誠實邊界**:盤前能決定「棄單/作廢 + 分類 A/B/C + 該等什麼」;最終扣板機 A 由 ORB 自動判,**B(回測不破)/C(站回+吞噬)第一版只回報、不自動觸發**(那需逐筆,屬未來盤中執行層)。
- `assert_env(require_finmind=False)`:盤前不需 FinMind,只需 Gmail(沿用既有 secrets)。
- **要有當日盤後核心選股才會動**;`data/signals/<最新>.json` core 為空 → 安靜略過不寄信、不寫網頁。
- MIS / yfinance 1分K 皆免費非官方,缺資料降級不整包死。

### 網頁「盤中即時」分頁
- preopen / orb 兩個 phase 除寄信外也寫 **`docs/premarket.json`** 並 commit(workflow `contents: write`,只提交這一個新檔,不碰 data.json)。
- `docs/index.html` 的 `renderLive()` 讀它,進入分頁時每 60 秒輪詢(cache-bust)+「↻ 重新整理」鈕。**非逐秒即時**:只在 08:45/09:25(與手動觸發)更新,UI 已誠實標示。瀏覽器無法直連 MIS(CORS),故走「Actions 產檔 → 網頁讀檔」。

### config(`config/screeners.yaml` → `premarket`)
`gate`(2026-07-08 改版:`weights`{tx_night 3 / tsm 2 / sox 2 / index 1}、`riskon_score`/`riskoff_score` 分數門檻、`vix_high`/`vix_pen`/`vix_spike`/`vix_spike_pen`、族群 `beta_high`/`beta_med`/`beta_low`)、`orb`(range_minutes 15 / confirm_until 09:30 / volume_filter / volume_mult)、`adr`(台股代號→ADR ticker:2330→TSM、2303→UMC、3711→ASX、2317→HNHPF、2409→AUOTY)。
**premarket.yml 已加 `FINMIND_TOKEN` env**(台指夜盤需要;沒設會退匿名低額度)。

### 手動測試
- 雲端:Actions → **Premarket Watch** → Run workflow → 選 `preopen`/`orb`。
- 本機:`python -m scripts.premarket --phase preopen --test`(需 Gmail 環境變數;會就地寫 `docs/premarket.json`,測完 `rm` 或 `git checkout`)。
- 離線單元測試:`classify_preopen` / `orb_decide` / `compute_gate` 都是純函式,可餵合成資料驗證(見開發紀錄)。

### 下一步(承第 6 節 #3)
盤前 MVP 已涵蓋 A 的自動觸發;真正的盤中逐筆執行層(富邦 API 即時報價、B/C 的回測/吞噬自動判、半自動下單)仍是獨立大專案。
- ~~台指夜盤接進閘門~~ **已完成(2026-07-08,見上)**。待驗:08:45 夜盤發布新鮮度。
- ~~第 4 項:選股當晚的事件預警~~ **已完成(2026-07-09,見第 13 節)**。
- 閘門門檻(`riskon/riskoff_score`、β 係數、VIX 門檻)目前為經驗值,**待用回測校準**(隔夜跳空 vs 隔日實際損益)。

---

## 10. 個股健檢(Stock Health)— 2026-06-30 新增,獨立模組

> 設計方案全文(架構決策的「為什麼」、各 Engine 公式細節、Tier1/Tier2 誠實邊界)見對話留存的設計文件;這裡只記實作現況與接續開發要點。`specs/07_stock_health.md` 有完整面向/公式速查表。

### 一句話定位
不是只給分數,是回答「這家公司現在值不值得投資」。9 個獨立 Engine(財務體質/成長能力/估值分析/風險分析/技術面/籌碼分析/新聞分析/AI解讀/Final Scoring)+ 可解釋的 Metric 契約(每個數字都附公式/來源/asof/更新時間/缺資料原因),總分依「價值投資/成長投資/短線交易」三組可切換權重,Risk 命中 Critical 規則時總分強制封頂(不是平均稀釋掉)。

### 雙路徑架構
- **路徑 A(批次,免費,核心+自選池)**:`scripts/main.py daily_run()` 在既有 stage-2 enrichment 之後,對核心10+自選池呼叫 `scripts/health/engine.py: build_ctx_batch()` + `compute_stock_health()`,寫 `docs/health/{代號}.json` + `docs/health/index.json`(manifest)。同時用 `scripts/health/industry_benchmark.py` 掃描本地已累積的 `data/financials|balance/*.parquet`(吃核心榜每天輪動的副產品,零額外 API)彙總同業平均,寫 `data/health/industry_benchmarks.json` 並複製一份到 `docs/health/`(供路徑 B 讀取)。
- **路徑 B(即時,選用,任意代號)**:`api/health.py`(Vercel Python Serverless Function)reuse 同一套 `scripts/health/*` 引擎,平行抓取(ThreadPoolExecutor)後現抓現算,**不依賴本地 parquet 累積**(serverless 無持久磁碟)。預設**未啟用**——需照 [VERCEL_SETUP.md](VERCEL_SETUP.md) 部署,並把 `docs/index.html` 的 `HEALTH_API_ENABLED` 改 `true` 才會在前端被呼叫。沒部署也完全不影響路徑 A 正常運作。

### 檔案地圖
- `scripts/health/metric.py` — 可解釋性資料契約(`metric()`/`engine_result()`),所有 Engine 共用。
- `scripts/health/quarterly.py` — 季資料存取小工具(`last`/`at`/`yoy`/`qoq`/`trend`/`ratio`/`cagr`/`consecutive`),financial/growth/risk 三個 Engine 共用,避免重複造輪子。
- `scripts/health/{financial,growth,value,risk,technical,chip,news}_engine.py` — 七個面向,各自 `compute(ctx) -> engine_result`。
- `scripts/health/ai_summary.py` — 規則產生事實句(零成本),LLM 只負責潤飾語句(prompt 強制不能新增數字/結論),沒 `ANTHROPIC_API_KEY` 自動降級成純規則句。
- `scripts/health/industry_benchmark.py` — 同業平均彙總(吃本地已累積快取,零額外 API)。
- `scripts/health/scoring.py` — Final Scoring Engine:三組投資風格權重、Risk Critical 封頂、星等診斷、Swing Score(當沖/隔日沖/波段/中長線適合度)。
- `scripts/health/engine.py` — Orchestrator/Registry(`ENGINES` dict)+ `build_ctx_batch()`(批次路徑專用的額外資料補抓:20季財報、長窗PE/PB、持股分散表)。
- `api/health.py` + `vercel.json` + `VERCEL_SETUP.md` — 路徑 B。
- `docs/index.html` 新分頁「個股健檢」:搜尋框 + 健康總分(沿用既有 `donut()`)+ 雷達圖(純 SVG,新函式 `healthRadar()`)+ 風險燈號 + AI 解讀 + 投資風格切換(純前端重算,不重打 API)+ 短線評估 + 選配 DCF + 泛型 Engine 卡片(`engineCard()`/`metricRow()`,新增 Engine 不必改前端)。

### 這次順手擴充的既有檔案(都是新增欄位/防護,沒改既有行為)
- `scripts/fetchers.py`:`_BS_TYPES`/`_CF_TYPES`/`_FS_TYPES` 新增流動資產/流動負債/存貨/應收帳款/現金/短期長期借款/折舊攤銷/利息費用欄位(候選名稱**未實測驗證**,缺資料時對應指標自動標 missing,不影響既有 `fundamental_bonus`);新增 `fetch_holder_distribution()`(集保庫存股權分散表,大戶持股/股東人數,欄位名稱同樣未實測驗證);`fetch_news()` 新增 `published_date` 欄位(feedparser 已解析好的日期,供新聞 7/30/90 天分桶,**不影響**既有 `catalyst.py` 用法)。
- `scripts/storage.py`:`_upsert()`/`upsert_revenue()` 寫入失敗(唯讀檔案系統,Vercel 會踩到)改成只記警告、不拋例外(`_try_write_parquet`),批次路徑(正常有寫入權限)行為不變。
- `scripts/indicators.py`:新增 `adx()`(獨立函式,不進 `compute_all()` 既有輸出欄位,零風險)。
- `config/screeners.yaml`:新增 `health:` 區塊。

### ⚠️ 還沒做 / 已知限制(誠實列出,別假裝做完了)
1. **這次只做到本機合成資料驗證**,沒有真實 FinMind token 跑過完整一次(離線測試全程 monkeypatch 掉 FinMind 呼叫)。**下次排程實際跑、或本機餵真 token 跑一次 `--date`**,務必檢查:`_BS_TYPES`/`_CF_TYPES` 新增的候選欄位名稱(current_assets/current_liab/inventory/accounts_receivable/cash/short_term_debt/long_term_debt/depreciation_amortization/interest_expense)有沒有命中 FinMind 實際回傳的 type 名稱——命中失敗就只是該指標顯示「資料不足」,不會讓流程當掉,但價值/財務/風險面向會比預期空。
2. **`fetch_holder_distribution()` 解析的 `TaiwanStockHoldingSharesPer` 真實欄位 schema 同樣未驗證**,大戶持股/股東人數兩項可能一開始是空的,需要拿真實 API 回應核對 `level_col`/`people_col`/`pct_col` 候選名單。
3. **DCF/EV-EBITDA** 屬於假設密集型,刻意不計入分數,公式/假設已攤開在 `value_engine.py`/前端,但沒有拿真實財報數字驗證過合理性。
4. **Tier 2 風險(董監質押/重大違約/重大減資/財報重編)** 仍是「沒有確認可行的免費資料源」狀態,只能靠新聞最佳努力涵蓋——別在未來對話裡講成「已經在監控」。
5. **Vercel 即時路徑(api/health.py)只做過 monkeypatch 過的本機邏輯測試**,從沒真的 `vercel deploy` 過,也沒驗證過 serverless function 的實際 bundle 體積會不會超過平台上限(reuse 了含 yfinance 的整包 `scripts/`)——`VERCEL_SETUP.md` 已誠實列出這個風險,要部署的人務必先試跑一次再依賴它。
6. **權重數字(三組投資風格、各 Tier1 風險規則的 penalty 分數)都是設計時的合理猜測**,沒有用真實績效回頭驗證過,跟 `compute_conviction` 當初的態度一樣:先讓系統能跑、能解釋,**累積真實資料後再回頭調參數**,不要假裝這組權重已經調優。

### 下一步建議
1. 排程實際跑一次(或本機用真 `FINMIND_TOKEN` 跑 `--date`),核對上面 ⚠️ #1/#2 的欄位命中狀況,修正 `_BS_TYPES`/`_CF_TYPES`/`fetch_holder_distribution` 的候選名稱。
2. 觀察 `docs/health/*.json` 實際內容,確認 Financial/Growth/Value 面向不是恆缺資料。
3. 想要任意代號即時查再做 Vercel 部署(`VERCEL_SETUP.md`),非必要功能,路徑 A 已經可獨立運作。
4. 累積一段時間後,比照 `STRATEGY.md` 對信心分的態度,回頭檢視三組投資風格權重與 Tier1 風險規則門檻是否合理。

---

## 11. 程式碼審查修正(2026-07-07)— Bug 1~4

依 [twse_程式碼審查與修正清單.md](twse_程式碼審查與修正清單.md)(2026-07-06 審查)修掉四個確定性 bug。**已用本機快取資料離線驗證,尚未用真實 FinMind token 跑過完整一輪排程。**

- **Bug 1 — combo/籌碼/基本面策略從未觸發**([main.py](scripts/main.py) `_enrich_pick`):`screen_stock` 原本在抓籌碼/財報**之前**被呼叫 → D 籌碼類/E 基本面類與全部 combos 恆 False(11 天 355 檔命中 0 次)。改為先抓 `chips_df`/`revenue_df`,並由 `update_fundamentals` 的 `fin["eps"]` 取 EPS(不再另打 API),三者一起傳入 `screen_stock`。移除死掉的 `_update_eps` 與 `fetch_eps_quarterly`/`load_eps`/`upsert_eps` import。
  - 驗證:對 409 檔有籌碼快取的股票重跑 `screen_stock`,`monthly_revenue_growth` 160 / `inst_consecutive_buy` 111 / `foreign_holding_increase` 45 / `short_cover_with_buy` 31 次命中,combos「主升段啟動」「強勢續攻」開始觸發(修前全 0)。
- **Bug 2 — 量比低估**([indicators.py](scripts/indicators.py) `compute_all`):`vol_ratio` 分母改 `vol_ma5.shift(1)`(新增 `vol_ma5_prev` 欄)。驗證:末日 3 倍量正確顯示 3.0000(修前為 300/140≈2.14)。
- **Bug 3 — 籌碼永久缺洞 + 外資窗飄移**:`_update_chips` 起點改 `last-4d` 重疊回補;[storage.py](scripts/storage.py) `upsert_chips` 改 `combine_first`(新 NaN 不覆蓋舊值);外資 30 日變化改「日期差」([main.py](scripts/main.py) `_chip_summary` 與 [screener.py](scripts/screener.py) `_foreign_holding_up`)。新增一次性 backfill 工具 [scripts/backfill_chips.py](scripts/backfill_chips.py)(`python -m scripts.backfill_chips`,需 FINMIND_TOKEN)補既有快取的歷史缺洞。驗證:合成缺洞測試,回補後缺格被填、且未覆蓋 refetch 未涵蓋的舊值。
- **Bug 4 — 受限股未排除**([fetchers.py](scripts/fetchers.py) 新增 `fetch_restricted_stocks`):抓 TWSE `TWT85U`(變更交易/全額交割)+ `announcement/punish`(處置,依處置期間迄日過濾)+ TPEX `tpex_cmode`(變更交易/分盤/管理/停止買賣),排除採分盤撮合的股票(隔日沖大忌)。[main.py](scripts/main.py) `daily_run` 建 universe 後依 `global.exclude_full_cash`(預設 true)過濾;歷史測試/來源全掛則不過濾(優雅降級)。config 同步註明 `min_market_cap`/`chip_accumulation.max_scan` 為未實作狀態。驗證:實測 TWSE 端點回 40 檔受限股,處置期間迄日過濾正確(已結束的 2 檔被排除);TPEX 本機因 SSL 憑證環境問題失敗但優雅降級(生產環境同 host 的估值 API 正常)。

**待辦(接續)**:下次排程實跑或本機餵真 token 跑一次 `--date`,確認 email/網頁 hits 出現 D/E 標籤、combos 有值;跑一次 `scripts.backfill_chips` 補歷史缺洞。

### 11.1 P0 續作(2026-07-07)— Bug 5 + 出場規則 3.1
接續把審查清單的 **P0** 做完(Bug 1~4 已完成,見上;第三~五節結構性項目仍待使用者決定是否開做)。

- **Bug 5 最小版 — 價格尺度偏移偵測(除權息旺季必備)**:[storage.py](scripts/storage.py) 新增純函式 `prices_scale_shift(cur, new_df, threshold=0.03)`;[main.py](scripts/main.py) 增量價格路徑偵測到重疊日收盤差 >3%(疑減資/分割 yfinance 回溯調整)→ 整段重抓 400 天覆蓋,重抓失敗則**保留舊快取、不合併新尺度增量**(避免舊+新尺度混在一起毀掉所有指標)。只抓分割/減資,不會被除息誤觸發(auto_adjust=False 原始收盤不因配息回溯調整;除息雜訊要雙軌 adj_close 解,屬 P2)。驗證:合成減資序列正確偵測 True、正常增量 False。
- **出場規則 3.1 — 均線停損寬限 + 報表拆分**([track.py](scripts/track.py)):
  - `_simulate_exit` 新增 `ma_stop_grace_days`(config `exit.momentum: 2` / `exit.swing: 0`):進場後前 N 個交易日只用初始停損,第 N+1 日起才啟用均線停損 —— 突破股隔日進場常正常回測一天就破 5MA,太緊會被單日洗盤掃出(實測均線停損佔出場 53.7%、平均持有 1.2 天)。
  - `build_report` 的 `exit_sim` 新增 `by_trigger`(突破/回測轉強/其他)與 `by_style`(動能/波段)拆分(`_exit_stats`/`_trigger_of` helper);console `_print_report`、email、網頁 SPA 皆已呈現,作為後續調參依據。
  - ⚠️ **誠實觀察**:在既有小樣本(75~77 筆 closed)上,grace=2 確實把均線停損比例 53.2%→44.0%、止損 37.7%→46.7%、持有 1.2→1.4 日(符合「少被單日洗」的設計意圖),但**已實現勝率反而 13.0%→12.0%**(樣本小、集中在一段壞窗)。3.1 本就是「需回測驗證」的假設,預設 2 依審查清單建議;可在 config 設 0 關閉或依累積真實資料調。改 SPA 後已 `node --check` 通過;email 模板已試 render 通過。

### 11.2 P1 完整版(2026-07-07)— 3.2 漲停複合 + 3.4-1 收盤位置 + 3.3 大盤閘門
使用者指示「P1 直接上完整版,不要最小版」。

- **3.2 漲停複合條件 + 首板/衝高未鎖**([scoring.py](scripts/scoring.py)):漲停(`limit_up_today`)**不再單獨**判過熱 —— 舊規則把「鎖死的第一根漲停」(惜售、隔天最易續攻)錯殺、卻放「衝到 9.4% 沒鎖」(尾盤被打開、隔天最易開低)進核心。新規則:`exhausted` 的漲停項改為 `limit_up_today AND 連續大漲 >= consec_big_up_days(3)`(連噴多日的漲停才算過熱),`ret5/乖離/RSI` 三項仍各自獨立判過熱;**首板** `first_board`(漲停+非連噴多日+`close_pos>=0.90`)總分 ×(1+`first_board_bonus`);**衝高未鎖** `spike_no_lock`(今日漲幅 ≥ `spike_watch_lo 0.07` 但 `close_pos < spike_close_pos 0.70`)總分 ×`spike_penalty 0.80` 且不進核心觸發。
- **3.4-1 收盤相對位置**(`close_pos=(收-低)/(高-低)`):對「非首板、非衝高未鎖」的一般個股,收高(≥`close_pos_hi 0.80`)/收弱(≤`close_pos_lo 0.50`)做 ±`close_pos_adj 0.06` 調整。三者互斥不重複加減。新輸出欄位 `close_pos/consec_big_up/spike_no_lock/first_board`。config 見 `scoring.setup`、`scoring.exhausted`。
- **3.3 大盤閘門完整版**(新檔 [scripts/market.py](scripts/market.py) `compute_market_regime`):三組免費訊號投票(指數 vs MA20/MA5/MA20斜率、市場廣度=站上MA20家數比+上漲家數比、漲跌停家數失衡)→ 依 config `market.tiers` 分「積極/中性/保守/觀望」四級,**動態決定 core_count(10/7/5/3)與 min_score(45/50/55/60)**,弱盤(保守/觀望)`prefer_pullback` 讓純追突破股排序分扣 `breakout_penalty_weak`(顯示分不變)。[main.py](scripts/main.py) 第一遍評分後算 breadth(零額外 API,吃 `industry_rows` 副產品)→ regime → 覆寫 core_count/min_score。`market.enabled=false` 回舊行為。regime 寫入 `data.json`/`history/*.json`/email ctx,email 表頭與網頁 subtitle 顯示閘門級別+核心上限+門檻(取代原本純裝飾的 `index_below_ma20`,該欄保留相容)。
- **驗證**:離線 monkeypatch 跑完整 `daily_run(as_of=2026-07-06)` 通過 —— regime 正確(積極/votes=5,廣度:站上月線 64%、漲停 61/跌停 12、掃描 1938)並序列化進 data.json;一檔 `first_board=True` 鎖死首板突破股進核心(`trigger=True`),正是舊碼會「漲停即過熱」錯殺的型態。scoring 四情境 + regime 四級 + SPA `node --check` + email render 全通過。測試產生的 data/docs 檔已用精確路徑 `git checkout` 還原(未動原始碼,參見 [[twse-offline-test-cleanup-targeted-not-broad]])。
- ⚠️ 權重/門檻(tier 投票對應、close_pos/spike/first_board 各係數)皆設計時合理猜測、未用真實績效驗證;累積資料後再回調,可在 config 個別關閉。

### 11.3 P2 完整版(2026-07-07)— 排序強化 + 新股軌道 + 當沖比 + 雙軌價格
使用者指示「P2 直接上完整版」。**關鍵資料源偵察結論**:「籌碼進基礎分 25%(全市場)」如 v2 所想**不可行** —— 三大法人買賣超(T86)無免費 bulk 源(TWSE openapi 未提供),當沖成交量亦無 bulk(TWTB4U 只有 Suspension 旗標);故沿用兩階段架構,以「調高 stage-2 籌碼權重」逼近其意圖。

- **P2-A 排序/評分**([main.py](scripts/main.py) `_rank_core` + [config](config/screeners.yaml)):`industry_bonus` 打開(weight 4→8,v2 產業升一級因子);新增 **combo 共振加分**(`combo_bonus`,每命中一個 combo +`per_combo`、上限 `weight`;Bug 1 修好後才真有值);**品質估值降權** 0.15→0.05,釋出 0.10 分給 trend/rs/setup(短線該重的三項,新權重 0.28/0.28/0.29/0.05/0.10);**chip_bonus** 權重 10→15(籌碼基礎分意圖的可行實現)。scoring.py 的 weights 預設同步改。
- **P2-B 新股獨立軌道**([scoring.py](scripts/scoring.py) + [main.py](scripts/main.py)):`compute_conviction` 支援 `min_history_new`(60);60~119 根 K 棒標 `new_stock=True`(<60 才淘汰)。main 第一遍 cutoff 由 120 降到 min_history_new;`scoring.new_stock.max_core`(2)保留核心名額給觸發新股(不足則以最佳新股替換核心中 rank_score 最低的老股,維持 core_count)。email/網頁「🆕 新股」標記。
- **P2-C 當沖比(3.4-2)**([fetchers.py](scripts/fetchers.py) `fetch_day_trade_ratio` + `_rank_core` `day_trade_penalty`):FinMind `TaiwanStockDayTrading`(免費,只在 enrich 階段對核心候選抓,防禦式多候選欄位、無 token/失效→None 不扣分)。當沖比 > `threshold(0.40)` 線性扣到 `penalty(8)`,並在卡片標「當沖比高 NN%」。
- **P2-D 雙軌價格**([fetchers.py](scripts/fetchers.py) 保留 `adj_close` + [indicators.py](scripts/indicators.py) `compute_all`):指標(均線/KD/MACD/RSI/布林/ATR)改吃**還原價**(adj_close 去除息假跳空),`close_raw` 保留原始成交價供漲停/停損/顯示。`compute_all` 內 `adj_close` 覆蓋率 ≥95% 才整段等比例縮放 OHLC,否則退回原始價(避免「一半還原一半原始」的接縫斷層)。[main.py](scripts/main.py) 顯示/漲跌停家數用 `close_raw`,均線比較用還原價。**漸進生效**:舊快取無 adj_close→退回原始;新股/被尺度偏移重抓(Bug 5)/自然汰換的股票會逐步取得 adj_close 後啟用還原。track.py 出場模擬讀原始 parquet 不受影響。
- **驗證**:離線完整 `daily_run(2026-07-06)` 跑通(scored 929、core 10、regime 積極、`industry_bonus`/`combo_bonus`/`chip_bonus`/`fund_bonus` 皆流入、`first_board` 3 檔進核心、render_email 產出 10 萬字無例外);`compute_all` 雙軌單元測試(還原啟用/覆蓋率不足退回/無欄不報錯)、new_stock 旗標(80→True、full→False、50→None)、新股保留替換邏輯、SPA `node --check`、email render(核心卡新股/首板/當沖/共振/combo 名稱皆顯示)全通過。測試檔已精確路徑還原。
- ⚠️ 一樣是「先能跑能解釋、待真實績效回調」:新權重/combo/當沖/新股各係數未經真實績效驗證;雙軌價格對既有快取需等 adj_close 覆蓋率補齊才生效(可另寫價格 backfill 加速,類似 [scripts/backfill_chips.py](scripts/backfill_chips.py))。**「籌碼進基礎分 25%」全市場版受免費資料限制未做**(見本節開頭)。至此審查清單 P0~P2 全部完成。

---

## 12. 個股詳情頁(2026-07-07 新增)— 仿 FinMind 官方 dashboard,完整版已上線
起因:使用者看 FinMind repo 的 Plotting/dashboard,想要「網站內每個個股點進去都有價量/籌碼圖」。可行性研究結論:FinMind 官方 dashboard(Flask+PyEcharts)就 4 張圖(K線+法人+融資券疊圖 / 月營收長條 / 外資持股折線 / 股權分散圓餅),**資料 fetchers.py 全都在抓**;還原股價/分K 這兩個 FinMind 要付費的,我們靠 yfinance 早就免費在用。決策:不照抄它 Flask 後端出圖(本站純靜態),後端只回 JSON、前端用 **ECharts**(=PyEcharts 的 JS 本體)畫;**即時查詢不預算全市場**(免費 300 次/hr、帶 token 600/hr,全市場會撞牆)。完整規劃見記憶 `twse-stock-detail-page`。

**本次已做 = MVP(K線+量+MA + 三大法人):**
- **後端** [api/detail.py](api/detail.py):Vercel serverless `GET /api/detail?stock=2330[&days=250]`,獨立於 [api/health.py](api/health.py)(關注點/payload 不同,不塞進 health)。`ThreadPoolExecutor` 平行抓 `fetch_price_history`(yfinance)+ `_fetch_institutional`(FinMind 三大法人),`compute_all` 補 ma5/20/60,全過 `_json_safe`(NaN→null)。回傳 `{stock_id,name,industry,market, kline:[[date,o,h,l,c,vol]], ma:{ma5,ma20,ma60}, inst:[[date,外資,投信,自營]淨張數]}`。CDN 快取 30 分。**離線實測 2330 通**(yfinance 給價、FinMind 免 token 也回法人)。
- **前端** [docs/index.html](docs/index.html):加 ECharts CDN(`echarts@5.5.1`,只點開個股才用到)+ `.detail-overlay` 全螢幕 modal + `openDetail/renderDetail/drawKChart/drawInstChart`。K線用西式配色(漲綠 `--up`/跌紅 `--down`,與既有迷你K棒一致);雙 grid(價+量)、MA5/20/60 疊線、dataZoom 可拖;法人堆疊長條(外資=accent/投信橘/自營青)。`sessionStorage` 概念用 `DETAIL_CACHE` 記憶體快取(同 session 同代號不重抓)。**進入點**:`slink(id)` 改成點代號開站內詳情頁(`#stock=2330` 深連結,ESC/點背景關閉),CMoney 外連移到 modal 內。逾時/無資料優雅降級。
- **驗證**:`node --check`(抽 inline script)過;preview 餵合成 120 根 K 棒 + 40 日法人 → 兩張圖 canvas 皆 init、overlay 開、hash 設、無 console error、截圖確認外觀正確。
- ⚠️ **需 Vercel 部署才實際生效**(同 health API,靜態 GitHub Pages 上 `/api/detail` 會 404,前端已對失敗降級)。bundle size 與 health 共用 yfinance 相依,部署時一併確認未超上限。

**完整版(2026-07-07 同日補齊四面向):**
- **後端** [api/detail.py](api/detail.py):`ThreadPoolExecutor` workers 2→6,並行多抓 `_fetch_margin`(融資券)/`_fetch_holding`(外資持股)/`fetch_monthly_revenue`(月營收 18 期)/`fetch_holder_distribution_latest`(集保股權分散最近一更新日)。回傳新增 `margin:[[date,融資餘額,融券餘額]張]`、`holding:[[date,外資持股%]]`、`revenue:[[YYYY-MM,營收元,YoY%]]`、`holder:{date,pie:[[桶名,%]]}`。每面向失敗各自回空(fetcher 自帶降級),互不影響主圖。
  - 新增 fetcher [fetch_holder_distribution_latest](scripts/fetchers.py):集保 `TaiwanStockHoldingSharesPer` 取最近日原始級距(只留能解析下界的列 → 自動排除『差異數/合計』彙總列,避免圓餅重複計數)。detail.py 內 `_bucket_holder_pie` 依股數下界聚合成 5 桶(散戶<10張/小戶10–50/中實戶50–100/大戶100–400/400張以上,1張=1000股)。
- **前端** [docs/index.html](docs/index.html):`renderDetail` 在法人圖後多 4 面板 + `drawRevChart`(營收長條億元＋YoY 雙軸折線)/`drawMarginChart`(融資融券雙折線＋dataZoom)/`drawHoldingChart`(外資持股面積折線)/`drawPieChart`(集保分散環形圖,標題帶更新日)。`_disposeDetailCharts`/resize/instance 變數皆納入 6 張圖。空資料各面板獨立顯示「無…資料」。
- **驗證**:`ast.parse` 過 + `_bucket_holder_pie` 單測(5 桶加總=100);preview 餵合成完整 payload → 6 張圖各 init 出 canvas、無 console error、截圖確認(K線/法人/月營收雙軸/融資券 tooltip/外資持股/分散環形圖)外觀正確。**未用真實 Vercel 端點驗證**(同 MVP,靜態 preview 無 serverless)。
- **仍待辦**:與健檢面向卡整合成單一詳情頁(目前詳情頁與健檢分頁各自獨立)。

---

## 13. 事件行事曆(2026-07-09 新增)— 盤前第 4 項,選股當晚 + 盤前雙掛
起因:選股當晚下的單會留倉,若撞上 FOMC/CPI/非農/台積法說/台指結算,波動與開盤跳空風險陡升卻無提示。做法照記憶要求:**確定性行事曆(公式 + 固定清單),完全不爬 Investing.com/即時網頁**。

**架構(兩類來源合併,把手動維護量壓到最低):**
- **A 公式推算(永不過期、零維護)** — [scripts/events.py](scripts/events.py):`settlement_day()` 台指結算=每月第三個週三、`nfp_day()` 非農=每月第一個週五,底層共用 `_nth_weekday(year,month,weekday,n)`。
- **B 固定行事曆(一年補一次)** — [config/events.yaml](config/events.yaml):FOMC(Fed 官方 2026 八次)/ 美國 CPI(BLS 排程)/ 台積電法說(季度)。每筆只填 `date + type`,title/impact/note 走 `events.py` 的 `_TYPE_META` 預設(yaml 可覆寫)。⚠ **CPI 與台積法說日期為預估/約略,務必每年初依官方公告核對**(yaml 註解已附三個官方來源 URL);FOMC 較確定。

**對外介面**:`upcoming_events(today, horizon_days=7, calendar=None)` → `{horizon_days, events:[{date,weekday,days_ahead,type,title,impact,region,note}], has_high, calendar_exhausted, caution}`。純函式、不打網;`calendar` 參數供離線測覆寫。`impact=high`(FOMC/CPI/台積)才觸發整體 `caution` 風控句;`med`(結算/非農)只列出。**`calendar_exhausted`**:B 類名單已無 ≥ 今天的未來項 → 信裡提醒補下年度(公式事件不會過期,故只看 B)。

**接線**:
- 選股當晚 [main.py](scripts/main.py) `daily_run`:讀 `cfg["events"]` → `ctx["events"]` → [daily_email.html](templates/daily_email.html) 大盤閘門下方一段「📅 未來 N 日重大事件」(高風險紅底、附 caution)。
- 盤前 [premarket.py](scripts/premarket.py) `run_preopen`:同樣算好塞 `ctx["events"]` + 寫進 `docs/premarket.json`(payload 含 `events` 欄位) → [premarket_email.html](templates/premarket_email.html) 閘門框下方精簡版事件列。
- **網頁盤中即時分頁** [docs/index.html](docs/index.html):新增 `eventsBox(ev)` 函式,從 `po.events`(即 `premarket.json` 的 `preopen.events`)渲染事件欄,插在閘門框與「盤前分類」段之間。高風險事件紅底、caution 句、行事曆到期提示皆有。
- config [screeners.yaml](config/screeners.yaml) 新增 `events: {enabled, horizon_days}`;`enabled:false` → 三處(選股信/盤前信/網頁)都不顯示。

**驗證**:離線純函式 + 兩模板 render 全過(同上);`docs/index.html` JS `node --check` 通過;瀏覽器 preview 餵合成 `premarket.json` → 盤中即時分頁確認三筆事件(CPI 重/結算中/台積法說重)、caution 正確渲染、無 console error。**尚未在真正排程或完整 `daily_run` 跑過**(events 段獨立、與選股邏輯無耦合,風險低);CPI/台積日期正確性依賴人工核對 yaml。

---

## 14. 我的持倉(2026-07-09 新增)— 純前端 localStorage,零隱私外洩
起因:使用者要「匯入實際持倉」看真倉損益。**關鍵前提:repo 是 public**,實際成本/張數若走既有 watchlist 路徑(commit → 烤進 data.json)等於把部位與資產規模公開。決策:**成本/張數只存瀏覽器 localStorage,絕不 commit、不進信件、不進批次**;只在網頁看(使用者選定)。**全部改動集中在 [docs/index.html](docs/index.html) 一個檔,零 Python 改動**。

**資料模型**(localStorage key `twse-portfolio`):`{v:1,positions:[{id,name,lots(張),cost(每股均價),note,ts}],updated}`。

**檔案地圖(都在 docs/index.html)**:
- 新分頁「我的持倉」(`data-p="port"` / `#p-port`),`renderAll()` + 初始 boot 都呼叫 `renderPortfolio()`;boot 先 `portLoad()` 再渲染一次(持倉獨立於 data.json,即使 data.json 失敗也能用)。
- **輸入**:`portShowPaste()`(貼券商庫存)→ `portParse()`(表頭偵測欄位:代碼/商品/成交/成本;無表頭則抓 4~6 碼代號 + CJK 名)→ `portStage()`(**可編輯預覽表,逐列補張數/均價再匯入**,防呆:money 數字絕不靜默匯入)→ `portCommit()`;`portShowManual()`/`portAddManual()` 手動保底;`portEdit/portDelete/portClear`;`portExport()`/`portImportFile()`(JSON 備份,換裝置/清快取自保)。
- **現價**:`portPriceOf()` 先查 data.json(core/watch/watchlist 已帶 close),查無 → `portResolveMissing()` 打 `/api/detail?stock=` 取 K 線末筆(需 Vercel;沒部署則 404 → 該檔損益顯示「—」,成本/張/停損照算,優雅降級)。
- **呈現**:總覽(總成本/總市值/未實現損益額+%/檔數;任一檔缺價 → 總市值/損益顯示「—」避免誤導)、部位集中度長條(全用同一基準:全有價依市值、否則依成本)、持股明細表(張/均價/現價/市值/未實現%+額/停損價=均價×0.93 及「距 x%」)。
- **跨頁徽章**:`heldTag(id)` 在核心/觀察/自選池卡 + `portDetailBanner(d)` 在個股詳情頁 modal 頂端顯示「🔖 持倉 · N 張 · 均價 · 未實現%」。

**驗證(preview 實跑,非只靠 node --check)**:`node --check` 過;preview 用**使用者真實庫存截圖格式**測 `portParse` → 正確抽出 id(去 `>>`、6 碼 ETF 009816 OK)/名/成交價、cost 留空;完整 UI 路徑 貼上→stage(2列)→填張數均價→commit→localStorage 正確持久化;總成本/P&L/停損距離數字正確;缺價檔(不在 data.json)404 後顯示「—」不崩;集中度基準一致(修過一次 per-row market/cost 混用 bug);held 徽章跨頁出現;詳情頁 banner P&L 正確;無 console error。測完已清 localStorage 測試資料。

**誠實邊界 / 待辦**:
1. **使用者券商庫存畫面(截圖)目前欄位全是報價欄,沒有『庫存量/成本』欄** → 貼上只能自動帶代碼/名/成交價,張數與均價需自己填(那兩個數字只有他知道)。若他日後在看盤軟體「欄位」加入庫存量/成本欄,`portParse` 的 cost 表頭偵測會自動帶入(lots 目前刻意不自動帶,避免『股 vs 張』1000 倍陷阱,一律手填)。
2. 現價要準需 Vercel 部署 `/api/detail`(同 health/detail);GitHub Pages 靜態上非 data.json 內的持股會顯示「—」。
3. 損益**未計手續費/證交稅**(單純市值差);要精算可日後接 track.py 的 `_net_return` 概念。
4. 部位感知的 **email/盤前信推播**(把成本停損帶進信件)使用者這次**明確不要**(只在網頁);要做需把持倉放 GitHub Secret 給批次讀,屬下一階段。
5. β 集中度未做(β 分級在伺服器端 premarket,前端沒有);目前只有產業/權重集中度。
