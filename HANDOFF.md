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
- **yfinance 颱風假/補假 volume=0 假K棒(2026-07-13 修)**:yfinance 對台股非交易日(颱風假 7/10 驗證)會回填 `close=前收、volume=0` 的假K棒。若未過濾:①`_is_trading_day` 看到 `inc.max=假日` ≠ today → 整天誤判非交易日跳出;②假K棒進 parquet 稀釋 `vol_ma5`/`vol_ratio`/均量指標。已在 `fetch_price_history` 與 `fetch_index_history` 加 `df[volume > 0]` 過濾,同樣原理的假日皆自動排除。
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
- **路徑 B(即時,選用,任意代號)**:`api/health.py`(Vercel Python Serverless Function)reuse 同一套 `scripts/health/*` 引擎,平行抓取(ThreadPoolExecutor)後現抓現算,**不依賴本地 parquet 累積**(serverless 無持久磁碟;籌碼 2026-07-09 修為 `build_ctx_batch` 內現抓,見文末當日條目)。預設**未啟用**——需照 [VERCEL_SETUP.md](VERCEL_SETUP.md) 部署,並把 `docs/index.html` 的 `HEALTH_API_ENABLED` 改 `true` 才會在前端被呼叫。沒部署也完全不影響路徑 A 正常運作。

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

**驗證(preview 實跑,非只靠 node --check)**:`node --check` 過;preview 用**使用者真實庫存截圖格式**測 `portParse`;完整 UI 路徑 貼上→stage→commit→localStorage 正確持久化;總成本/P&L/停損距離數字正確;集中度基準一致(修過一次 per-row market/cost 混用 bug);held 徽章跨頁出現;詳情頁 banner P&L 正確;無 console error。測完已清 localStorage 測試資料。

**⚠️ 單位修正(2026-07-09 同日,關鍵)**:使用者第二張截圖(券商『即時未實現損益』)顯示**「即時庫存」欄是股數(股)不是張數**——他持有大量零股(聯發科 3 股、大立光/群聯各 5 股、光寶科 150 股、009816 ETF 1000 股)。原模型用「張」×1000 換算市值會**差 1000 倍**。已把 canonical 單位改成**股(`shares`)**,`portLoad` 對舊 `lots` 欄自動 ×1000 遷移;market value = `shares×price`(不再 ×1000);顯示以股為主,`shares%1000===0` 才附「N 張」。
- `portParse` 重寫成相容兩種畫面:①報價『庫存』(無股數/成本→只帶代碼/名/現價,手填)②『即時未實現損益』(有「即時庫存」+「成本均價」→**全自動帶入**)。表頭偵測放寬(代碼類 OR 股票名稱)+ **代碼可從「聯發科(2454)」括號內抽**(`col.id` 缺時 fallback `portCode(name)`);cost **優先「成本均價(均價)」、避開「付出成本(總額)」**;`portCleanName` 去掉名稱裡的 (代碼)。
- **匯入時現價快照**:貼上的「現價」欄存進 position.price,當 data.json/detail 都沒有該檔時以它當現價(標「匯入價」,非即時);2454/3008 這種有進批次的仍用即時價。→ 使用者零股大多不在每日榜單,否則市值全顯示「—」。
- 驗證:preview 餵**使用者真實五檔**(009816/2301/2454/3008/8299)完整流程 貼上→自動帶入股數+均價→commit → 總成本 88,061≈券商 88,060、**總市值 89,795 與券商完全一致**、股數顯示正確(1000→「1 張」、零股照實)、2454/3008 用即時價其餘標「匯入價」、毛損益 +1,734(券商淨額 1,372,差額=賣出費稅,已標「未計費稅」)。

**持股撞事件提醒(2026-07-09 同日 v1)**:事件行事曆 × 持倉的 **client-side join**(成本仍不出瀏覽器)。
- **資料源**:`scripts/main.py daily_run` 把 `upcoming_events()` 結果(市場級公開資訊,非個資)**移到 data.json 寫入前算好,並寫進 `data.json` + `history/*.json` 的 `events` 欄**(原本只給 email ctx;premarket.json 的 events 只有跑過 preopen 才有,不可靠)。email ctx 沿用同一份 `events`。**唯一 Python 改動**。
- **前端**([docs/index.html](docs/index.html)):`portEventInfo()`(`EVENT_STOCK={tsmc:['2330']}` 個股事件對照,目前僅台積法說→2330)+ `portEventAlertHtml()`。兩層:①**個股點名**(持有 2330 且有台積法說 → 「你持有的 2330 台積電 — 台積電法說會(日期/N天後)」)②**市場級高風險 × 曝險**(FOMC/CPI 等 high 事件 → 「未來 N 日高風險:CPI…。你留倉 X 檔·市值/成本 YYY·最集中 半導體 ZZ%」)。持股明細代碼欄對有個股事件者掛 ⚠ chip。只在有 high 或個股 hit 時才顯示(med-only/無事件 → 不顯示,避免雜訊)。
- **驗證**:node --check;preview 餵合成 `DATA.events`(CPI/台積法說/結算)+ 持 2330/2454/8299 → 個股點名 + 市場摘要(半導體 59% 集中度正確算出)+ ⚠ 僅 2330;無事件/med-only 皆正確不顯示;`upcoming_events(2026-07-09)` 純函式 + `scripts.main` import 皆過。

**誠實邊界 / 待辦**:
1. **兩種券商畫面**:①報價『全部庫存』只有報價欄(無股數/成本)→ 貼上只帶代碼/名/現價,股數與均價手填;②『即時未實現損益』有「即時庫存(股)+成本均價」→ **全自動帶入**(建議使用者用這個貼)。`portParse` 兩者皆相容。
2. 現價要準需 Vercel 部署 `/api/detail`(同 health/detail);GitHub Pages 靜態上非 data.json 內的持股會顯示「—」。
3. 損益**未計手續費/證交稅**(單純市值差);要精算可日後接 track.py 的 `_net_return` 概念。
4. 部位感知的 **email/盤前信推播**(把成本停損帶進信件)使用者這次**明確不要**(只在網頁);要做需把持倉放 GitHub Secret 給批次讀,屬下一階段。
5. β 集中度未做(β 分級在伺服器端 premarket,前端沒有);目前只有產業/權重集中度。
6. **撞事件個股層級目前只有台積法說→2330**;要「每檔持股撞自己的除權息/法說/財報日」需擴充 `scripts/events.py` 產個股事件表(除權息/法說排程 TWSE/MOPS 有免費源),再把個股事件寫進 data.json(僅選股榜/自選池內的股批次認得;任意持股需 detail API)。**新版 data.json 尚未實跑產生**(下次排程或本機真 token 跑 `--date` 後,`data.json` 才會帶 `events`;在那之前線上 data.json 無 events,撞事件提醒不顯示,優雅降級)。

**截圖辨識匯入(2026-07-09 同日,選配,需 Vercel)**:使用者要「上傳截圖自動辨識」。因本站是 public 靜態頁、API key 不能進前端,走 serverless 代理。
- **後端** [api/portfolio_ocr.py](api/portfolio_ocr.py):`POST /api/portfolio_ocr {image(base64), media_type}` → Claude **Opus 4.8 vision**(`claude-opus-4-8`)抽持股 → 回 `{positions:[{id,name,shares,cost,price}]}`(與前端 portParse 同形)。**⚠️ 2026-07-09 實測修正**:一開始用 Haiku 4.5,使用者實跑發現嚴重錯誤(009816凱基TOP50→誤認9050鴻海、光寶科→光磊科、群聯→智易,且把『淨值/資產市值』當均價/現價、甚至自行÷1000)——密集數字表格 Haiku 讀不準。改 **Opus 4.8** + 前端送**近原尺寸(≤2400px)PNG 無損**(原本縮 1600px+JPEG 把小字糊掉)+ prompt 明確禁止「拿淨值/資產市值當均價現價、÷1000、猜相近股名」。約 1~2 美分/張。system prompt 強調「即時庫存=股數不是張、成本取均價非付出成本、代號從括號抽、忽略合計列、讀不到填 null」;防禦式 JSON 解析(容忍圍欄)。讀 `os.environ["ANTHROPIC_API_KEY"]`,沒設回 error。`vercel.json` 已註冊。一張約 US$0.003。
- **前端** [docs/index.html](docs/index.html):`portShowOcr`(檔案選擇)→ `portOcrPicked`(canvas 縮到 ≤1600px 寬 → jpeg base64,壓 token/體積)→ POST → 結果進**共用預覽表** `portStageTable(rows)`(貼上與截圖共用;`portStage` 也改用它)→ commit。dedup 沿用 `portUpsert`(依代號)。`portCommit` 改查 `#p-port .port-stage-table tbody tr`(涵蓋貼上與截圖兩處)。**順手移除**檔案裡一顆 dead 的 `portShowImage()` 按鈕(函式不存在)。
- **驗證**:`py_compile` + `node --check` 過;preview 實跑:OCR 鈕開表單、canvas 縮圖+base64+POST 全跑、`/api/portfolio_ocr` 本機 501 → **優雅降級顯示「需部署 Vercel」**、模擬 OCR 結果進共用預覽表 → commit → localStorage 正確、貼上流程重構後仍正常。**⚠️ 真實 Claude 辨識未驗**(api/* 從沒真的 deploy 過;要部署+設 ANTHROPIC_API_KEY 才會動,見 VERCEL_SETUP.md)。**隱私**:截圖(含成本)會一次性經 Anthropic 辨識(貼上/手動則 100% 不出瀏覽器)——已在 UI/文件標明。

---

**修:即時健檢籌碼分析永遠「資料不足」(2026-07-09)**:使用者回報健檢的**籌碼分析**面向恆缺資料。根因——即時路徑 [api/health.py](api/health.py) `compute_live_health` 用 `load_chips(stock_id)` 讀本機 parquet,但 Vercel serverless **無持久磁碟 → 快取恆空**,`chip_engine` 每個法人/融資券指標都落到 `missing_metric`。財報、`holder_dist` 在 `build_ctx_batch` 都會現抓,唯獨籌碼漏了這步。
- **修法** [scripts/health/engine.py](scripts/health/engine.py) `build_ctx_batch`:仿既有 `holder_dist` 現抓模式,傳入的 `chips_df` 若 `_stale` 或 <21 交易日,就 `fetch_chips_history` 現抓近 `chips_days`(預設 120 日曆日,足量供外資持股/融資5日窗)+ `upsert_chips` 合併回傳(寫檔已有唯讀防護)。`build_ctx_batch` **只被即時路徑呼叫**(批次健檢已停用,見 §main.py:687),不會對批次重複打 FinMind。
- **驗證**:離線 monkeypatch(patch `scripts.fetchers`/`scripts.storage` 層,因 engine 函式內 from-import 綁定)餵空 `chips_df` → `ctx['chips']` 現抓 40 列、chip score 72.8、三大法人連買/今日/近5日淨買超、外資買賣超、外資持股、融資5日、融券回補、日均成交額**全部齊全**。**⚠️ 未用真實 FinMind token / 真 Vercel 端點跑**(本機無 token);要真正確認需部署後查一檔實看籌碼面向。大戶持股/股東人數走另一條 `holder_dist` 現抓,不在本次修正範圍。
- **同輪查證:新聞分析即時路徑「資料不足」= 部署 config,非程式錯**。`news_engine.analyze_news` 開頭 `if not ANTHROPIC_API_KEY or not news_items: return None` —— 新聞情緒要呼叫 Claude 逐則標記,**Vercel 未設 `ANTHROPIC_API_KEY` → 新聞面向恆缺**;批次(GitHub Actions secret 有設)故 docs/health/*.json 新聞正常(實查 1727 score 83.2、2317 54.5 都有 8 指標)。修法=在 Vercel 專案設 `ANTHROPIC_API_KEY`(OCR 同一把),不需改碼(`output_config` 寫法正確、`claude-haiku-4-5` 支援)。使用者已於 Vercel 設好該 key(Production)。見記憶 [[twse-live-health-missing-data]]。

**讀內文版新聞分析(2026-07-09)**:回應使用者「標題常誇大/不實,要 AI 讀內文」。原本 `analyze_news` 只餵**標題**給 Haiku;現在餵 AI 前先 best-effort 抓每則新聞的**實際內文摘要**,標題+內文一起送,system prompt 明確要求「**有內文時以內文為準**」。
- **內文抓取** [scripts/fetchers.py](scripts/fetchers.py) `enrich_news_content`:平行(ThreadPoolExecutor)+ wall-clock budget 上限(保護 serverless timeout)。抽取優先取發布者 `og:description`/meta(靜態 HTML 就有,半付費牆/JS 渲染也拿得到、且非誇大標題)再補內文 `<p>` 段落到 `max_chars`。每執行緒各自 `requests.Session`(Session 非執行緒安全)。
- **關鍵:Google News 新版 opaque URL 解碼**。Google News RSS 的 link 是 `news.google.com/rss/articles/CBMi...` 轉址包裝,**新版路徑段 base64 解開只是內部 ID(`AU_yq...`)不再內嵌網址**,單純 302/base64 都解不出(實測 live 0/8)。改用 `_resolve_gnews_batchexecute`:GET 文章殼頁取 `data-n-a-id/-sg/-ts`(簽章/時間戳)→ POST Google 私有 `batchexecute` RPC(`Fbv4je`/`garturlreq`)→ 換回真實發布者網址。舊格式仍保留 base64 快路徑(省 2 次請求)。**⚠️ batchexecute 是 Google 未公開內部協定,改版可能失效** → 全程包 try,失敗該則退回只用標題。
- **驗證(真實 live)**:`fetch_news('2330')` → `enrich_news_content` 實測 **7/8 則抓到真內文**(Yahoo/UDN/cmoney/cmnews/LTN/FTNN),3.8s 內完成;唯一失敗是純表格論壇頁(無 og/`<p>`)→ 乾淨降級。`analyze_news→compute` hermetic 測試:內文進 prompt、`content_read` 帶出、compute 產出「已讀取內文則數」metric(前端泛型渲染自動出現)。
- **設定** [config/screeners.yaml](config/screeners.yaml) `health.news`:`read_content`(預設 true)、`content_max_items`(10)、`content_timeout`(5s)、`content_max_chars`(600)、`content_budget`(30s);設 `read_content: false` 可退回純標題版。feature 預設 ON,不需改 config。**成本**:input token 增加(10 篇×~600 字),Haiku $1/1M 仍數美分;延遲增加(每則最多 GET 殼頁+POST+GET 真文 3 趟,但平行)。

**真因:新聞/催化劑 AI 恆「資料不足」= `output_config` 在 Vercel 丟例外(2026-07-09,推翻前一條「只要設 key」結論)**。使用者反映 key 早設好新聞仍空。實測 live `twse-main.vercel.app/api/health?stock=2330`:news 面向 `missing_reason=api_unavailable`(代表 news_items 非空、`analyze_news` 回 None),但**同一份回應的 `ai_summary.narrative_ai` 有 LLM 潤飾內容** → 證明 (a) key 確實有進 `config.ANTHROPIC_API_KEY`、(b) 純 `messages.create` 在 Vercel 正常。唯一差異=news/catalyst 傳了 `output_config={"format":{"type":"json_schema",...}}`,**Vercel 上安裝的 anthropic 版本對此參數丟例外** → 被 except 吞掉回 None。OCR/ai_summary 用純呼叫故正常。
- **修法**:[news_engine.py](scripts/health/news_engine.py) 與 [catalyst.py](scripts/catalyst.py) 都改回**純 `messages.create`(拿掉 output_config)+ 自己 parse JSON**,prompt 明確要求「只輸出 JSON、不要圍欄」,用新的 [utils.extract_json](scripts/utils.py)(容忍 ```json 圍欄/前後雜訊,沿用 OCR `_extract_json` 邏輯)解析,並移除兩檔已無用的 `_SCHEMA`。**與 OCR/ai_summary 一致的成功寫法,不依賴特定 anthropic 版本**。⚠️ claude-api 參考文件說 `output_config` 是現行正解——但本專案 Vercel 實測會爆;若日後在 requirements 釘更新的 anthropic 版本,可重新評估是否回用結構化輸出。
- **驗證**:extract_json 對 純JSON/```json 圍欄/前後夾雜文字/非JSON 皆正確;news `analyze_news→compute` 與 catalyst `classify_catalysts`(含圍欄回應)mock 測試皆過。

**第二層 bug:純呼叫重新暴露 JSON 截斷脆弱性(2026-07-09,接續上條)**。拿掉 output_config 部署後(deploy a8ce47f Ready),`ai_summary` AI 解讀正常但新聞**仍**「資料不足」。推論:純呼叫無結構化輸出保護,一次要 AI 逐則分類 60 則 → 輸出 JSON 超過 `max_tokens=1600` 被**截斷** → 不完整 JSON → `extract_json` 回 {} → `analyze_news` 靜默回 None。(這正是 output_config「有用」處——保證 JSON 完整;拿掉後得自己控輸出量。)
- **修法** [news_engine.py](scripts/health/news_engine.py):①新增 `ai_max_items`(預設 30)只送最新 30 則給 AI(視窗本以近期為主,足量);②`max_tokens` 1600→3000;③在**每一條靜默 return None 路徑**加 `log.warning`(呼叫失敗印 type+msg、JSON 解析失敗印 `stop_reason`+回應前 160 字、解析成功但無有效標記)→ 之後若還失敗,直接看 **Vercel Logs** 拿確切原因(`stop_reason=max_tokens` ⟹ 仍截斷)。yaml `health.news` 同步加 `ai_max_items: 30`、`max_tokens: 3000`。
- **✅ 已在真 Vercel 驗證(2026-07-09)**:`twse-main.vercel.app/api/health?stock=2330` 新聞面向 score=61.6、已讀取內文 10 則、近90天分類 30 則;AI 摘要含「外資連續6天賣超並提款310億元」等**只能從內文讀到**的細節,證明讀內文版真的生效。三個 bug(chips 快取空→現抓、output_config→純呼叫、JSON 截斷→限則數+加 token)全部解決。若日後又「資料不足」,先看 **Vercel Logs** 的 `健檢新聞分析 ... 失敗` 行拿確切原因。

**新聞分析可展開「分析了哪些新聞」小頁(2026-07-09)**:使用者要在新聞分析面向內看到「分析了哪些新聞的連結」。
- **後端** [news_engine.py](scripts/health/news_engine.py) `compute()`:回傳的 news engine 結果多帶 `analyzed_news`——把 AI 逐則標記的 `items` 依 idx 映射回 `news_items` 的標題/連結/來源/發布日,加上該則 sentiment/impact 與 `has_content`(是否讀到內文),依日期新→舊排序。`compute_stock_health` 已把 engine 結果整包 spread 進 `engines[]`,故前端自動拿得到。
- **前端** [docs/index.html](docs/index.html):`engineCard` 泛型渲染時,若 `eng.analyzed_news` 有值就多渲一個巢狀 `<details class="hnl-panel">`(標題「📰 分析了哪些新聞(N 則,點標題看原文)」),內含 `newsLinkRow`:情緒色 badge(利多綠/利空紅/中性灰)+ 可點標題(`target=_blank` 開原文)+ 來源·日期·已讀內文。只有 news engine 有此欄,其他面向不受影響。**連結用 news_items 的 Google News link**(瀏覽器點了會自動轉址到原文)。
- **驗證**:後端 `compute()` 單元測試 analyzed_news 含連結/情緒/內文旗標/日期排序正確;前端 preview 注入合成報告,DOM 驗證面板存在、3 則、badge 利多/利空/中性、href+target=_blank、已讀內文 tag 數正確,截圖確認外觀。

**我的持倉「AI 總覽」(2026-07-10 新增)**:使用者要一次整理所有持倉的完整重點 + 總結 + AI 依各股預期判斷停利停損。
- **端點** [api/portfolio_review.py](api/portfolio_review.py)(新 Vercel serverless,vercel.json 已加 maxDuration 60):收前端組好的 `{positions:[{id,name,shares,cost,price,pnl_pct,market_value,health:digest}],totals}` → 純 `messages.create`(Opus 4.8,**不用 output_config**,沿用 2026-07-09 教訓)+ `scripts.utils.extract_json` → 回 `{overall:{summary,health,concentration_risk,action_priority[]}, positions:[{id,verdict,outlook,key_points[],take_profit,stop_loss,pnl_note}], analyzed}`。verdict 限白名單【續抱/加碼/減碼/停利了結/停損/觀望】。
- **隱私**:同 OCR 精神——持倉(含成本)一次性送 AI、不留存;成本仍只存前端 localStorage。前端與端點都有明示。
- **資料來源不重抓**:每檔 health digest 由前端從既有 `/api/health`(HEALTH_CACHE,批次健檢已快取)萃取(七面向分數+關鍵指標+swing+估值+AI摘要優缺點+新聞摘要),端點只做 AI 綜合判讀,避免 serverless 重抓逾時。
- **前端** [docs/index.html](docs/index.html):持倉工具列加「🧠 AI 總覽」鈕(gated on HEALTH_API_ENABLED);`portAIReview()` 先平行補齊各檔 health(顯示進度)→ 組 payload → POST → `_portRenderAIReview` 渲染:整體卡(總結/體質/集中度風險/優先處理)+ 逐檔卡(verdict 色標 badge + outlook + key_points + 綠停利/紅停損 + 損益短評)。`_portHealthDigest` 負責萃取。
- **⚠️→✅ 60s(Hobby)逾時真解:前端分塊(2026-07-10,推翻單次呼叫版)**。使用者真實持倉觸發「AI 分析失敗:Unexpected token 'A', "An error o"...」= Vercel 回 **504 FUNCTION_INVOCATION_TIMEOUT** 非 JSON 錯誤頁。實測**即使 Sonnet,單次分析 ~10 檔仍 >60s 被平台硬砍**;且 **anthropic SDK 的 `timeout` 是讀取逾時(有 token 就重置)、無法當 wall-clock 上限**,所以 `_SDK_TIMEOUT` 沒能在 50s 攔下。**單次大呼叫本質塞不進 60s**。
  - **改法**:端點分兩 task——`positions`(只析這批 ≤`_MAX_PER_CALL`=6 檔,回 positions[])、`overall`(吃各檔精簡+已定 verdict,只回 overall,輸出短)。前端 `portAIReview` 市值排序取前 15 → **切每塊 3 檔平行 POST(task=positions)→ 合併 → 再 1 次 task=overall** → 渲染。`_portReviewPost` 統一「讀 text 再 parse,非 JSON=平台逾時給友善訊息」。model=`claude-sonnet-4-6`。
  - **真 Vercel 實測延遲**:task=positions 3檔 ~38s、4檔 ~43s;task=overall(10 檔精簡)~16s(且正確算出最大持股 12.1%、金融業曝險 21.2%)→ 故定 **3 檔/塊**留足 60s 餘裕。
- **驗證**:後端兩 task 分流 mock 過 + 真 Vercel 端到端延遲實測(上述);前端 preview 10 檔 → 正確切 3 塊(3/3/3/1)... 實為 chunks of 3、+1 overall、10 卡渲染、header 正確。**整條 UX**:先平行補齊 health(有 HEALTH_CACHE 則秒過)→ 分塊逐檔 → 總結,每個 HTTP 都 <60s。

---

**回測系統第一版(純技術)上線(2026-07-10)**:使用者要求「開始做回測,不可跳過偷懶自行省略」。這是第一次對選股訊號做**真回測**(非上線後前向追蹤);見記憶 [[twse-backtest-plan]]。
- **新檔** [scripts/backtest.py](scripts/backtest.py)。架構照計畫:**A 撮合直接複用 [track._simulate_exit](scripts/track.py)**(隔日開盤進場/跳空棄單/R倍數/移動停利/扣交易成本,零重寫);**B 訊號重放**是本檔核心——對每個歷史交易日 d 只用「當日可知」資訊重算指標→[compute_conviction](scripts/scoring.py)→選核心。
- **關鍵正確性(為何能「每檔只算一次指標」而非每天重算)**:compute_all/compute_relative_strength 內所有指標皆**因果**(rolling/ewm+min_periods/shift,無置中無未來洩漏),故某日 d 的指標值不論算在完整序列或截到 d 的序列上都相同 → 先對完整歷史算一次,再 `ind.iloc[:cut]` 切片餵評分,結果與逐日重算逐位元一致但快 ~400 倍。**⚠️ 日後若在 indicators 加任何非因果轉換,此假設失效,回測須改回逐日重算。**
- **benchmark**:一次抓 ^TWII 快取到 `data/meta/twii.parquet`(離線可重現),同時供相對強度(與線上一致)+ 超額報酬基準。無檔則降級。
- **執行**:`python -m scripts.backtest`(全量 1976 檔 × ~380 天約 7 分鐘,precompute ~30s)。旗標 `--limit N`(冒煙)/`--start`/`--end`/`--no-regime`。輸出 `data/backtest.json` + 主控台報表。Windows 主控台 cp950 會亂碼,程式已 `sys.stdout.reconfigure(utf-8)`;跑時建議 `PYTHONIOENCODING=utf-8`。
- **📉 第一版核心發現(2026-07-10 全量)**:**選股層有 alpha,現行出場規則把它賠光。**
  - 選股層(買在收盤持有到 N 日,不含進出場規則):**各天期絕對與超額報酬皆為正**,且隨天期遞增(30日 +8.95%、超額 +2.66% vs TWII;10日超額 +0.66%)。但勝率僅 46~53%、贏大盤比率僅 43~46% → edge 來自右尾少數大贏家,非穩定性。
  - 執行層(_simulate_exit 實際規則,2413 筆已實現):**勝率 28.3%、平均淨報酬 −0.40%(扣成本前 +0.25%)、超額 −0.78%、平均持有僅 3.0 天**。出場原因 **均線停損 50.8%**、止損 29.1%、移動停利 20.1%。→ **出場太早**:訊號要 10~30 天才兌現,但均線停損 3 天就把票洗掉,alpha 全數回吐。這正是 track.py `ma_stop_grace_days` 註解早警告過的洗盤問題,回測首次量化證實。
  - 依進場型態:突破(30.7% 勝率)> 回測轉強(22.8%),但兩者執行後皆不賺。
  - 依信心分四分位:**非單調**——Q2 最佳(+0.18%)、Q1最低最差(−1.16%)、Q4最高竟 −0.20%。信心分排序對「執行後結果」edge 很弱(只有 Q1 明顯最爛)。
  - 依大盤狀態:弱盤(指數跌破月線,337 筆)25.2% vs 強盤 28.9%,弱盤無存活優勢(但樣本以多頭為主,證明力有限)。
  - 依月份:高度變動(+2.86% ~ −4.97%),證實時間段偏誤。
- **誠實邊界(程式報表末尾也印)**:①倖存者偏誤(下市股已消失)②除權息跳空污染(adj 覆蓋率<3% → 全程原始價,除權息旺季尤重)③僅 ~22 個月單一多頭段,證明不了空頭 ④純技術層,不含線上 stage-2 籌碼/基本面/新聞/產業/combo 加成 ⑤估值快照無歷史時點對齊、品質面一律中性 ⑥**近端右設限**:最後 ~30 天選的票前向資料不足,winner 還沒兌現就被算成 open/快速止損,故 2026-06/07 特別難看有一部分是這個假象。
- **下一步(最高槓桿)**:發現直指**出場規則**而非選股。可試 (a) 拉長 `ma_stop_grace_days` / 改用更寬的移動停利 / TP1 後才啟用均線;(b) 純選股層已證明有超額 → 驗證「訊號對、出場錯」後,把出場當主要調參對象。第二版才考慮 vectorbt(大量 walk-forward 調參時,見 [[twse-backtest-plan]])。

**回測校準出場規則:溫和放寬均線停損(2026-07-10,接上條)**:第一版回測發現「選股層有 alpha、出場規則賠光」後,使用者要求做出場參數敏感度掃描並修正。
- **掃描工具** [scripts/backtest.py](scripts/backtest.py) 重構:拆成 `_replay`(訊號重放,慢 ~7 分)+ `_simulate`(套一組出場參數,快 ~幾秒)+ `sweep_exits`(重放一次、套 9 組出場參數比較)。因**出場參數不影響選股**,重放一次即可掃描。`python -m scripts.backtest --sweep` → `data/backtest_sweep.json`。
- **掃描結果(2979 筆選股全歷史,單調)**:放寬均線停損寬限,勝率/淨/超額**全部單調變好**。baseline 淨 -0.40%/超額 -0.78%/均線停損 50.8% → 最激進(TP1前全關均線)淨 +0.80%/超額 -0.36%/均線停損 0%。證實均線停損是主漏洞。**但每一組超額 vs TWII 仍為負**(最佳 -0.36%)→ 修完逼近但未贏大盤,不是證實的 alpha 策略。附帶:max_hold 30→45 無差別(交易極少撐到上限,該拉桿丟棄)。
- **「0% 均線停損」不是 bug**:寬限設 = max_hold 時,`i >= ma_grace` 在持有迴圈內永不成立 → 均線停損永不觸發,那些出場**重分配**到止損(29→63.5%)+移動停利(20→33.5%)+到期(3%),合計仍 100%。
- **過擬合判斷**:①單調非尖峰、②方向是 track.py 既有註解**掃描前就預言**的(非 grid-search 挑幸運組)、③樣本大且超額仍負(沒 p-hack 出假贏)→ **參數過擬合風險低**。但**真正風險是 regime 污染**:資料僅單一多頭段,「放寬停損」在上漲段幾乎必然變好(套套邏輯),證明不了空頭;全關停損在下跌段會裸奔。**故採溫和版而非最激進版**。
- **✅ 已套用(config/screeners.yaml exit)**:momentum.ma_stop_grace_days 2→5、swing 0→3、trail.atr_mult 1.5→2.5、min_pct .03→.04、max_pct .07→.10。全歷史掃描下:淨 -0.40%→+0.16%、均線停損 50.8%→30.6%、持有 3.0→4.7 天(仍短線)、超額 -0.78%→-0.53%。**保留均線停損當後盾**(下跌段仍有下檔保護)。影響:track.py 出場模擬 + compute_entry_plan 給使用者的每日停損/移動停利建議一併更新。
- **誠實結論**:這是**止血**(把出場規則從扣分變接近打平),不是找到打敗大盤的機械策略。超額仍略負;要真正驗證需空頭段資料(現無)。下一步若要繼續:降交易成本假設敏感度、或把選股層本身(已證實有超額)當機械訊號來源而非追求日內執行 edge。

**投資組合回測:資金有限 + 滿倉換股(2026-07-10,接出場校準)**:使用者提「現金有限、不會每檔都買」→ 建投資組合層回測(固定資金、最多同時 N 檔、湊不滿擺現金)。規則與使用者逐項確認:同時 N 檔(3/4/5)、滿倉才換股、最弱持股=三因子等權綜合(今天重評分數↓/帳面損益↓/持有天數↑)、明顯強過=失去訊號 或 分差≥M、最短持有閘 min_hold。
- **工具** [scripts/backtest.py](scripts/backtest.py):`simulate_portfolio`(逐日狀態機:自然出場空名額→填空→滿倉換股;每檔自然出場複用 _simulate_exit)、`sweep_portfolio`(重放一次,掃 N×M×min_hold=27 組)、`print_portfolio`。`python -m scripts.backtest --portfolio` → `data/backtest_portfolio.json`。權益曲線 N 本等權帳,mark-to-market 算 CAGR/最大回撤,對比買 TWII 抱著。
- **⚠️ 換股規則第一版有結構偏誤(已修)**:原本「新訊號今天分數 vs 持股今天重評分數」比較不公平——新訊號因『今天剛觸發』分數虛高,阿持股抱幾天分數自然衰減 → 新訊號幾乎永遠贏 → 每天狂換,**換股 77~440 次/年**、手續費啃光本金(CAGR −73%)。**修法**:①「明顯強過」改比持股『進場當時分數』entry_score(凍結,蘋果比蘋果)②「失去訊號」收緊成真破底(重評 None/過熱/分數<`LOST_SIGNAL_FLOOR`=30,非掉到入榜門檻45)。修後換股降到 35~91 次/年。
- **📉 核心結論(全量修正後)**:**資金有限下,沒有任何一組(N/M/min_hold)贏得了大盤。** TWII 同期 CAGR +53.4%;27 組超額全負(−19%~−92%),最佳組 N=4 CAGR +34% 仍輸 19pp 且回撤更大。**且跨參數極不穩定(CAGR −39%~+34%,相鄰參數大跳)→ 結果被『3~5 名額剛好抱到哪幾檔』的運氣主導,非穩定 edge。**
- **根因鏈**:①純技術訊號 edge razor-thin(逐筆 +0.16%/超額 −0.53%)②集中到 3~5 檔=高變異小樣本,且實測高分票子集比平均更差(呼應信心分四分位非單調)③多頭段裡「有時空手擺現金」對always-invested指數天生落後(cash drag)④換股成本。
- **對使用者的誠實建議**:以這段資料+扣成本,機械式交易這套**打不贏直接買 0050/TWII**、還多吞回撤。可行方向:(a)把選股當**研究靈感/觀察清單**、自己判斷,別機械全買;(b)資金主體擺指數,只用一小 sleeve 交易 1~2 檔高信心;(c)真要做,槓桿在**選股層本身**(唯一有超額的地方)不是換股/執行。**雙面誠實**:這是單一多頭段+倖存者偏誤;空頭裡指數也會 −20%+、有停損的系統『可能』反而保本更好——但憑現有證據,打不贏指數。
- **未 commit 前狀態**:`data/backtest_portfolio.json` 為修正後結果快照。

---

## 15. 族群熱力圖(2026-07-13 新增)— Finviz 式三層 Treemap,新分頁

起因:使用者要「盤前選股/盤中監控雙用」的族群熱力圖,不靠任何名單、光看熱力圖決定今天看哪幾個族群再去撈個股。

### 功能概述
- **視覺風格**:Finviz 式方塊 treemap(大方塊=族群、小方塊=個股),直接複用已有的 ECharts 5.5.1。
- **三層下鑽**:`nodeClick:'zoomToNode'` + breadcrumb:大族群(12個) → 子族群(42個) → 個股(~880檔)。
- **方塊大小**:`dollar_vol_m`(日均成交額,百萬),市值代理,越熱越大。
- **顏色切換**:工具列 toggle 按鈕:
  - **今日漲跌**(預設):±6% 映射到紅/綠,中性灰。
  - **20日動能**(`ret20_pct`):±20% 映射到紅/綠。
- **個股方塊內容**:股票代號 + 今日漲跌% + 評分(sc)。
- **點擊行為**:
  - 點族群/子族群 → ECharts 原生下鑽。
  - 點個股 → 快速摘要 popup(今日漲跌/20日動能/信心分/日均成交額/子族群路徑)+ 「查看詳情」連結跳個股詳情頁。
- **主題感知**:亮/暗主題切換後重渲。

### 新增檔案

**`docs/sector_map.json`**:族群對照表,兩層結構
(⚠️ **2026-07-19 已整包換成 FinMind 產業鏈,以下 `_industry_map`/`_override` 描述僅供考古,見第 24 節**):
- `_industry_map`:TWSE 官方 46 個產業類別 → `{sector, sub_sector}` 預設映射。
- `_override`:130 個股 ID → `{sector, sub_sector}` 覆蓋(優先度高於 _industry_map)。
- **12 個大族群**:半導體、AI/伺服器、電子零組件、光電顯示、終端設備、車用電動車、能源電力、金融、傳產、生技醫療、通訊網路、其他。
- **42 個子族群**:晶圓代工/IC設計/封測/記憶體DRAM/化合物半導體/半導體設備材料/IC通路/散熱模組/伺服器品牌/ABF載板/PCB/被動元件/連接器/面板/觸控面板/光學零組件/工業電腦/筆電桌機/電力設備/電機機械/綠能環保/車用電子/金控/銀行/證券/塑化化工/鋼鐵/航運/紡織/食品飲料/材料/生技醫療/醫材/新藥研發/化學生技/網通設備/電信/數位雲端/建設營造/觀光/貿易百貨/其他。

**`docs/heatmap.json`**:每日熱力圖資料包,由 `main.py` 生成。格式:
```json
{"date": "YYYY-MM-DD", "stocks": [
  {"id": "2330", "n": "台積電", "ind": "半導體業", "chg": 1.5, "r20": 15.2, "sc": 85.3, "vol": 12000.0}
]}
```
`chg` = 今日漲跌%、`r20` = 20日動能%、`sc` = 信心分、`vol` = 日均成交額(百萬)、`ind` = TWSE 產業類別(前端用來查 sector_map)。

⚠️ **目前 `heatmap.json` 只含 188 支從歷史訊號萃取的股票(測試資料)**,待下次 `main.py` 排程跑後才會產生完整 ~880 檔。

### 修改檔案

**`scripts/main.py`**(新增 heatmap.json 輸出):在 `dates.json` write 之後加一段,從 `scored`(全市場所有已評分股票)輸出 compact JSON:
```python
heatmap_stocks = [{"id":s["stock_id"],"n":s.get("name",""),"ind":s.get("industry",""),
  "chg":s.get("change_pct"),"r20":s.get("ret20_pct"),"sc":round(float(s.get("score") or 0),1),
  "vol":max(float(s.get("dollar_vol_m") or 1),1)} for s in scored]
with open(docs_dir/"heatmap.json","w",encoding="utf-8") as f:
    json.dump({"date":today.isoformat(),"stocks":heatmap_stocks},f,ensure_ascii=False)
```
`scored` 已含全市場 ~880 檔(含未進核心/觀察的),`ret20_pct`/`dollar_vol_m` 來自 `compute_conviction()`。

**`docs/index.html`**(5 處修改):
- Tab 按鈕(`data-p="heatmap"`)插入「個股健檢」與「歷史追蹤」之間。
- Panel `#p-heatmap`:工具列(兩個 toggle 按鈕 + 資料日期 + 說明)+ `#heatmap-chart` div。
- CSS:`.hm-toolbar`/`.hm-toggle-btn`/`.hm-popup`/`.hm-popup-inner`/`.hm-stat`/`.hm-detail-btn` 等。
- Popup div `#hm-popup`(modal overlay,點外部關閉)。
- JS 區塊:
  - `initHeatmap()`:lazy(只第一次點 tab 才 fetch),平行抓 `heatmap.json` + `sector_map.json`。
  - `_buildHmTree(stocks,sm,mode)`:依 `_override` 優先、`_industry_map` fallback 分群 → ECharts treemap data 格式;leaf 含顏色(RGB lerp)、label(代號+漲跌%)、`_s` 原始資料。
  - `renderHeatmap()`:三層 level style(大族群 borderWidth 6/粗字、子族群 3、個股 1)、breadcrumb、ResizeObserver 響應式。
  - `setHmMode(mode)`:切換今日/動能,更新 toggle active 狀態並重渲。
  - `_showHmPopup(node)`:填 popup HTML,4 格統計 + detail 連結。
  - applyThemeLabel hook:主題變換時重渲熱力圖。

### 驗證結果(browser preview)
- `hmReady:true`, stocks:188, chartExists:true。
- 12 個大族群正確渲染。
- 半導體下鑽:IC設計(26支)/晶圓代工(6)/封測(6)/半導體(9)/半導體設備材料(3)/IC通路(4)/化合物半導體(2)。
- 今日漲跌 ↔ 20日動能 切換正常。
- 點個股(2303)popup 開、四格數字正確、連結到詳情頁。

### 2026-07-13 修正(颱風假後修資料 + 分類/顯示三項改進)

**heatmap.json 重建(2026-07-13)**:原始 188 支測試資料混了多個不同日期(最早 6/22、最晚 7/9),今日漲跌全錯。以本機全量 1976 個 parquet 跑 `compute_conviction()` + `fetch_stock_info()` 重建,得 874 支(資料基準日 2026-07-09,為颱風假前最後一個交易日)。7/9 漲跌:漲 319/跌 515/平 40,符合該日市場實際偏空走勢。

**sector_map.json 分類修正(2026-07-13)**:舊的 `_override` 僅 130 個,造成 70 支半導體被落入「半導體→半導體」同名 catch-all。增補 40 個 override(共 170 個),依成交額排序手動分類 IC 設計/封測/化合物半導體/記憶體 DRAM/半導體設備材料/IC 通路。catch-all 由 70 → 30,重點股驗證:8299 群聯→IC設計、2449 京元電子→封測、3105 穩懋→化合物半導體、3260 威剛→記憶體DRAM、6510 精測→半導體設備材料 全部正確。

**docs/index.html 兩項顯示修正(2026-07-13)**:
1. **個股方塊加股票名稱**:leaf label formatter 從 `s.id+'\n'+disp` 改成 `(s.n?s.n+'\n':'')+s.id+' '+disp`,塊夠大時顯示名稱+代號+漲跌%,加 `overflow:'truncate'`。
2. **子族群 upperLabel 加深色背景**:原本 `upperLabel` 在淺色底上文字幾乎看不到。加 `backgroundColor:'rgba(0,0,0,0.45)', padding:[3,8], fontWeight:'bold', height:22`,讓子族群標題清晰可讀。

### 2026-07-13 第二輪修正(全族群 catch-all 普查)

使用者反映不只半導體有 sector==sub_sector 問題,已全面掃描修正:

**`_industry_map` 修正(5 條)**:
- `文化創意業` → 其他/數位娛樂(鈊象/橘子等遊戲公司)
- `運動休閒`/`運動休閒類` → 傳產/運動休閒(寶成/巨大/美利達等)
- `居家生活`/`居家生活類` → 傳產/消費品

**`_override` 增補(306 個,+136 vs 第一輪 170)**,主要:
- 電子工業大型錯分類:中華電/遠傳/台灣大 → 通訊網路/電信;智邦 → 網通設備;聯詠/新唐/智原/祥碩 → IC設計;強茂/富鼎 → 功率半導體(新);漢唐/致茂/鴻勁/尖點 → 半導體設備材料;大聯大 → IC通路;創見/十銓 → 記憶體DRAM;玉晶光/聯鈞/亞光 → 光電/光學;技嘉 → AI/伺服器品牌
- 電子零組件業:PCB+17支、被動元件+9、連接器+7、電源供應器+8(康舒/精成科/全漢/群電/致伸/博智)、健策/建準 → AI散熱、嘉澤/富世達 → AI連接器(新)
- 半導體業剩餘:13支IC設計、千附/精拓 → 半導體設備材料
- 生技醫療業:新藥研發11支、醫材+1(鐿鈦)

**修正結果**:同名 catch-all 230 → 79 支:半導體/半導體 30→**0**;生技醫療 21→**4**;電子零組件 113→**56**(低量長尾);其他/其他 22→**19**(TWSE 真正「其他」類,難細分)。

### 下一步
1. 等下次 `main.py` 排程跑完,`heatmap.json` 會自動更新到最新交易日的 ~880 支全市場資料。
2. 電子零組件/電子零組件 仍剩 56 支低量個股;電子工業/電子工業 仍有 96 支。若要繼續細分,可對低量個股逐一加 override。
3. 其他/其他 剩 19 支來自 TWSE `其他` 類(真正雜項),難以細分,屬正常殘留。

---

## 16. 補價格史到 2021(含 2022 空頭)+ 修 backtest bug(2026-07-18)

**動機:** 回測與分點因子驗證原本只有 2024-09→2026-07 這段**單一多頭**,證明不了空頭。用 FinMind Sponsor 把價格史往前補到 2021,納入 **2022 台股大空頭**(TWII 18500→12600 −32%),讓回測變多空雙 regime。

**做了什麼:**
1. **補價格史**:FinMind bulk `TaiwanStockPrice`(raw)+`TaiwanStockPriceAdj`(還原價)**逐日**抓(range 不給 data_id 會回 0 列,只能逐日),1242 交易日 × 2 = ~2484 呼叫 @1.6s(≈37/min,無 ban,~117 分)。`data/prices/*.parquet` 每檔補到 **2021-06-01→2026-07-09**;TWII 用 yfinance ^TWII 補到 2021-05。**adj 覆蓋率 2.7%→100%** → `compute_all` 還原價分支全檔啟用,除息假跳空對「指標」污染徹底解決。FinMind 原始收盤與舊 yfinance 收盤逐檔相等,接縫無虞。
2. **⚠️ 踩坑 A(資料)**:FinMind 對停牌/零星交易日回 **OHLC 全 0** 但帶微量 volume(4/98/106),`volume>0` 擋不掉 → -100% 及隔天 +數千% 假報酬。修:合併時要求 open/high/low/close 全 >0。清後 >10.5% 跳空 1.0%→0.09%(剩真除權/減資,均值中性)。
3. **⚠️ 踩坑 B(程式,重要)**:補史令 adj 覆蓋率達標後,`backtest._replay` 的 `sig_close` 誤取 `compute_all` 輸出(=還原價),但前向報酬/出場用原始價 → 選股層假膨脹 **+10.71%**、執行層幾乎全跳空棄單。**修 `scripts/backtest.py`:sig_close 改取 `raws[sid]` 原始收盤**(與報酬/出場同基準)。修後選股層 h=1 回 +0.29%、已實現交易 2375→7407。**線上 `track.py` 無此 bug**(df=load_prices 原始價、sig_close=p["entry"] 原始成交價,已查證)。詳見記憶 `twse-backtest-signal-close-adj-bug`。

**驗證結果(誠實):**
- **基礎回測**(2021-11→2026-07,~58 個月):選股層仍有溫和 alpha(h=1 +0.29%、30日超額 +1.16%),但**執行層淨 −0.20%/筆(出場規則賠光,老結論跨 regime 依舊成立)**。2022 空頭月份純多動能大失血(1/4/6/9/12 月 −3~−4.5%),證實偏多系統。弱盤(跌破月線)樣本 389→1678,可信度大增。
- **分點因子多空 regime 驗證**(`branch_validation/validate_regime.py`,TWII 對 60 日均線判強/弱,弱盤樣本 282→**1223** 含整個 2022 空頭):因子**沒完全翻臉但半邊崩**——
  - `net_breadth` 逆向:**避開擁擠買(Q4 最差)= regime-robust**(強/弱盤 Q4 皆最差);但**買低廣度冷門股(Q1 最佳)是純多頭現象**(強盤 Q1 執行 +1.11%,弱盤 Q1 執行 **−1.66% 最差**)。
  - `top5_net_conc` 集中:強盤 Q4 +1.12% 成立,弱盤 edge 幾乎歸零。
  - **接線上時:逆向 Q1 加分要掛大盤 regime(弱盤關掉),Q4 擁擠買不分多空一律扣。**

**產物/可重用:** merge 腳本與每日快取在 scratchpad(`merge_history.py`、`pricecache/`、`prices_backup`=最原始 yfinance 備份);`branch_validation/` 內 `validate_regime.py`、`picks_regime.pkl`(8712 筆重放)、`branch_cache.parquet`(擴到 4472 筆含 2022)。跑腳本需 `.finmind_token`(不留存,需重貼)。

**未解:** 倖存者偏誤(universe=今天還在的股,下市股缺席)——需 point-in-time 上市清單,優先度低,標記待查。

---

## 17. 券商分點 / 短沖主力面板(2026-07-18 新增,個股詳情頁)

**目的:** 使用者要在個股詳情頁看籌碼與分點,並標出**哪些券商是短沖主力**、該股近日有無被主力大買/大賣、隔日倒貨或拉抬的可能。

**檔案:**
- `scripts/branch.py`(新)— `fetch_branch_daily()` + `analyze_branch()` + `_next_day_alert()`。
- `api/detail.py` — 回應多一個 `branch` 區塊(`_BRANCH_DAYS=15`);任何失敗回 `{}`,前端該面板不畫,不影響其他圖。
- `docs/index.html` — `branchAlertCard()`(警示卡)、`drawStNetChart()`(主力每日淨買賣+佔量%)、`drawBranchChart()`(分點排行,🔥=短沖主力)。

**⚠️ 關鍵實作坑:分點資料必須逐日抓。** `TaiwanStockTradingDailyReport` 單日就 ~16000 列(分公司×成交價),**給日期區間會被 FinMind 以 400 (size too large) 拒絕**。故用 ThreadPool 平行逐日抓 15 個交易日(交易日清單取自 price_df),實測 ~1.5 秒、整支 detail API ~6 秒。另需先 `group by securities_trader_id` 把同分點多價位列加總才是該分點當日買賣量。

**短沖主力判定=雙軌(不單靠寫死名單):**
1. `KNOWN_SHORT_TERM`(11 家公開常被點名的隔日沖分點:美林1440/摩根大通8440/台灣摩根1470/高盛1480/花旗1590/凱基台北9268/凱基三多9275/富邦建國9658/永豐金9A00/日盛1650/國泰8880)—— 僅當**先驗**。
2. **資料驅動 `reversal` 分數** = 該分點在**這檔**淨額序列的 lag-1 自相關取負值,>0.35 且該分點毛量 ≥ 全體 0.5% 才算。這是隔日沖的**行為定義**、會自我更新(實測抓到不在名單內的「港麥格理」反轉 0.42)。
   - ⚠️ 調參經驗:未設量門檻 + 只要 5 天序列時,**128/817 家被誤標**;改成 ≥8 天 + 0.5% 毛量後降到 **12~13 家**,合理。

**隔日判讀 `_next_day_alert`:** 依「主力當日淨買佔成交量%」給 **high(≥20%)/ medium(8~20%)/ low** 三級 + 白話依據。**刻意不輸出假精準機率**,並在 UI 明寫「這是風險提示不是預測,主力大買後也可能續拉」。實測 2603 長榮 = high(佔量 51.8%,買方全是名單分點);2330 = low(主力淨賣、壓力已釋放)。

**⚠️ 部署前提:Vercel 的 `FINMIND_TOKEN` 必須是 Sponsor 級**,否則分點 dataset 回 4xx → 面板自動空白(不會壞頁,但看不到)。

**驗證方式:** 已在瀏覽器用 mock 資料實跑 `renderDetail`,確認警示卡文案、🔥 標記、雙序列圖、以及 `branch:{}` 的降級路徑(顯示友善提示、其餘圖表正常)皆正確、無 console 錯誤。

---

## 18. 修信心分排序(2026-07-18)— scoring.weights v3

**問題:** 使用者是「照信心分由高到低逐檔人工分析」的用法,但回測 8694 筆已觸發選股顯示 **分數越高、前向報酬越差**:總分四分位 5日超額 Q1 +0.29 / Q2 +0.56 / Q3 +0.23 / **Q4(最高分) −0.21**,Q4−Q1 = **−0.59pp**,且 **6 年裡 5 年為負**(不是雜訊)。這等於把他的分析時間系統性導向錯的股票 —— 對這個使用者是實質 bug。

**逐分項診斷(`scratchpad/score_diag.py`,把 `compute_conviction` 的分項帶進 `_replay` 的 selections):**

| 分項 | Q4−Q1 | 判讀 |
|---|---|---|
| `rs` 相對強度 | **−0.66pp** | **最強反指**;rs 最弱那組 5日超額 +0.77%、且是唯一執行淨為正(+0.08%) |
| `liquidity` | −0.45pp | 雜訊 |
| `trend` | +0.11pp | 幾乎不分辨(84% 擠在最高桶) |
| `setup` 短線時機量能 | **+0.30pp ✓** | 唯一乾淨單調 |
| `quality` | — | 回測中無變異(valuation=None),線上才有 |

→ 與「台股是反轉市場」(見第 16 節、`twse-branch-factor-validated` 記憶)完全一致:**評分把「已經漲很兇」當優點,但台股會反過來咬。**

**修正:** `config/screeners.yaml` → `scoring.weights` 改為 **trend 0.176 / rs −0.176 / setup 0.706 / quality 0.059 / liquidity 0.059**(rs 轉負權重 = 同為突破候選時偏好還沒漲過頭的;setup 主導)。縮放方式:令**理論最大值 = 100**(rs 負權重最佳貢獻 = 0),故**順序完整保留、不需夾在 100**(先前試過正規化到總和 1.0 會讓 2.78% 超過 100,夾住會把最好的頭部壓成同分、破壞你最在意的排序)。
- 效果:Q4−Q1 **−0.59 → +0.51pp**,且**強盤 +0.69 / 弱盤 +0.24 皆為正**(四個候選權重中唯一兩邊都正的)。
- ⚠️ 誠實邊界:**非全年份皆正**(2021 −1.03、2023 −0.92 仍負),屬「從可靠地壞 → 平均變好」,**不是保證乾淨的排序**。

**⚠️ 連帶發現:`min_score` 門檻從來沒發揮過作用。** 舊尺度下已觸發選股最低 52.3 分,`45/50/55/60` 的通過率是 **100%/100%/100%/99.8%** → 真正把關的一直是 `core_count`(取前 N 名)。新權重把分數攤開成 ~5~93 後門檻才「有能力」咬人,但**要不要讓它生效屬於策略改動**,故本次把 `ranking.min_score` 與 `market.tiers.*.min_score` 一併改成**等效非約束值**(5/5/5/8/12),維持既有行為、隔離這次的純排序修正。是否啟用真正的分數門檻 → 留給整合回測決定。

**連動修正:** `docs/index.html` 的「信心分分布」長條圖分桶由 `80+/70–79/60–69/<60` 改為 **`75+/60–74/45–59/<45`**(沿用舊分桶會讓所有股在新尺度下看起來像變差)。其餘 `>=70`/`>=80` 出現處分別是「0–1 分項百分比」與「個股健檢分數」,屬不同計分系統,**不受影響**。

---

## 19. 整合回測 + 出場/風險狀態上線(2026-07-18)

### 19.1 整合回測結果(`scratchpad/integrated_bt.py`:出場 × regime 閘門,新權重 v3 已生效)

一次重放 8698 筆,3 種出場 × 3 種閘門:

| 出場 | 閘門 | n | 淨% | 超額% | 勝率 | 持有 |
|---|---|---|---|---|---|---|
| 現行 | 不設 | 7505 | −0.40 | −0.79 | 32.1 | 5.0 |
| 現行 | TWII>60MA+法人偏多 | 2724 | +0.58 | **−0.14** | 35.8 | 5.3 |
| **穩健(ma10+grace8)** | **TWII>60MA+法人偏多** | 2723 | **+0.70** | −0.19 | 34.2 | 7.3 |
| 積極(關均線+hard.10) | TWII>60MA+法人偏多 | 2709 | **+1.17** | −0.64 | 39.4 | 15.1 |

**三個改動確實相乘**(基準 −0.40% → 積極+雙閘門 +1.17%,+1.57pp/筆),且**超加成**(閘門單獨 +0.98、出場單獨 +0.42,合計應 +1.00,實際 +1.17)→ 印證「寬出場必須配閘門」。

**但三個誠實的壞消息:**
1. **超額 vs 大盤全為負**(最好 −0.14%)。淨報酬變好主要是 **beta**(閘門讓你只在多頭在場),持有期間仍跑輸大盤。ABC 三個修正都沒突破這個天花板。
2. **逐年不一致,且閘門在 2021 反而幫倒忙**(每種出場加了法人條件後 2021 都更差:−1.19→−2.02 / −1.36→−2.45 / −1.46→−3.62)→ 閘門有一部分是擬合後期資料。
3. **交易次數砍 64%**(7505→2724)。
4. **`min_score` 確定無效**:新尺度下測 0/30/45/55/65,淨報酬全 +0.68~0.70%、n 幾乎不變(2723→2641)。**永久不啟用**。

### 19.2 上線內容(A)

**① 出場改「穩健」**(`config/screeners.yaml` → `exit.momentum`):`ma_stop` 5→10、`ma_stop_grace_days` 5→8、`trail_ma` 5→10。
- 選穩健而非積極的理由:超額較佳(−0.19 vs −0.64)、空頭年沒那麼慘、且不靠拉長持有吃 beta(**倖存者偏誤對長持有特別不利**)。
- ⚠️ 這會改變網頁/信件上顯示的**停損/停利價位建議**(`compute_entry_plan` 吃這些參數)。

**② regime 做成「提示」不是「過濾」**(`scripts/market.py` `compute_risk_gate()`):
- 訊號 = 指數 vs 60MA **+** 期貨三大法人未平倉淨額 vs 20 日均(新 fetcher `fetch_futures_inst_net_oi()`,`TaiwanFuturesInstitutionalInvestors` data_id=TX,每日 1 次呼叫)。
- 三態:`risk_on 順風` / `risk_off 逆風` / `mixed 分歧`;抓不到期貨資料→只用指數均線,自動降級。
- **刻意不硬過濾**:①使用者是人工判斷型,硬藏推薦會剝奪他判斷的機會;②閘門在 2021 幫倒忙,不放心讓它決定看不看得到。
- 呈現:`main.py` 塞進 `market_regime.risk_gate` → 網頁 regime hero 卡片 + `templates/daily_email.html` 提示區塊。**完全不影響選股名單/core_count/min_score。**

**③ 分點因子(net_breadth)尚未上線** —— 兩個未解的前提:(a) 分點 21:00 才發布,批次現在 ~16:00 跑,**得先把批次改到 21:30**;(b) 還沒做「把因子接進 stage-2 後重跑完整回測」的整合驗證。故 A 只上 ①②。

**驗證:** `compute_risk_gate` 用真實 TWII+期貨 OI 實測(今日=分歧、2022-10 空頭低點=正確判逆風、無期貨資料=優雅降級);網頁五種狀態渲染實測;email 模板 Jinja 渲染 + `fut_ok=None` 省略欄位實測。

---

## 20. 分點逆向因子接進選股(③,2026-07-18)

**① 批次時間 16:00 → 21:30(需你到 cron-job.org 手動改,程式端已備妥)**
券商分點 **21:00** 才發布,16:00 跑只吃得到「昨天」的籌碼。改 21:30 後分點因子才會真正生效,且三大法人(20:00)/融資券(21:00)/外資持股(21:00)也一併變成**當天**資料。`SETUP_PREMARKET_CRON.md` 已更新為 21:30(UTC 13:30)。
- **程式端保護**:查「當日」分點若尚未發布 → FinMind 回空 → 自動不加成(`branch_bonus` 形同關閉),**絕不用昨天的分點回填**。所以即使 cron 還沒改,開著也無副作用,只是不生效。

**② 因子實作** — `scripts/branch.py`:
- `breadth_ratio_from_rows()`:廣度比 =(買超家數−賣超家數)/總家數,範圍 −1~+1。**用比例不用絕對家數**(絕對值隨個股熱度浮動,跨股不可比)。
- `fetch_breadth_for(stock_ids, date)`:候選股各 1 次呼叫、平行(~30 檔數秒)。
- `branch_signal(breadth, risk_on, cfg)`:**直接編碼多空驗證結論** ——
  - 擁擠買(廣度高)→ **一律扣分**(Q4 在強弱盤都最差,是 regime-robust 的半邊);
  - 低廣度(分點在賣)→ **加分只在 risk-on 給**(弱盤時 Q1 執行淨 −1.66% 反而最差)。
- `scripts/main.py`:`_rank_core(..., risk_on=)` 多一項 `branch_bonus`(照 `chip_bonus` 同模式);risk_on 取自 `market_regime.risk_gate.state == 'risk_on'`。
- config `scoring.branch_bonus`:`enabled: true`、`weight: 10`。

**③ 驗證(`scratchpad/validate_branch_rerank.py`)—— 誠實說明測的是什麼:**
完整整合回測**做不到**:需要每天所有候選(非只被選中的)的分點 = 30 檔 × 1181 天 ≈ **35,000 次逐檔呼叫(~15 小時 + IP-ban 風險)**。故改測**對使用者最有意義的那件事**:他是「照排序由高到低逐檔人工分析」,所以測「**重排後的前 K 名是否優於重排前的前 K 名**」(4164 筆 / 629 天,用 `branch_cache` 既有資料)。

| | K=1 | K=3 | K=5 |
|---|---|---|---|
| 全部 | −0.09 → **+0.79** | +0.06 → +0.35 | +0.01 → +0.10 |
| 順風 | −0.41 → **+0.71** | +0.21 → +0.43 | +0.11 → +0.25 |
| 逆風 | +0.43 → +0.94 | −0.19 → +0.22 | −0.23 → −0.27 |

(單位:平均 5 日超額%;W=10 最佳,W=5 偏弱、W=15 未再改善)

**改善集中在名單最前面**(K=1 最大、K=5 幾乎沒差)= 分點主要在修正「誰該排第一」,正好對應使用者用法。
**逐年一致性:K=3 四年全改善;K=1 三年改善**(2024 變差但只有 18 天樣本,`branch_cache` 對 2024 僅覆蓋 12 月,不具代表性)。這是整輪下來一致性最好的結果。

**⚠️ 未測到的部分(誠實邊界):** 只測了「已被選中的股之間重排」,**沒測**「分點會不會把原本沒入選、分數較低的股拉進來」(資料量不可行)。

---

## 21. E:大戶/借券/費率三因子驗證 —— **結論:一個都不加**(2026-07-18)

`branch_validation/validate_e_factors.py`(逐日 bulk 快取、可續跑)。**冗餘檢查**(與 net_breadth 相關性)先做:`big_pct` −0.053、`lend_vol` −0.155、`lend_fee` +0.151 → **三個都不冗餘、是獨立維度**(推翻了「會像 govbank 一樣是同一根軸」的預期)。故純看有無 edge:

| 因子 | 結果 | 判定 |
|---|---|---|
| 大戶持股比例 | 強盤 Q4−Q1 +0.29pp / 弱盤 −0.54pp,兩邊都非單調 | ❌ 方向隨盤勢翻轉,不可靠 |
| 借券費率 | 全部 −0.52pp / 強盤 −0.66pp(**低費率反而好**,與「高費率=軋空燃料」假設相反) | ❌ edge 偏小且僅強盤,不值得多一個維度 |
| 借券賣出量 | 強盤 +0.91pp、弱盤 −2.01pp(看似最強) | ❌ **見下:是規模效應** |

**⚠️ 關鍵方法學檢查(差點就把假因子上線):** 借券量原始值與**成交量相關 r=0.401**,亦即「高借券量」很大程度只是「大型股」。改用**規模中性**的 `借券量/成交量` 重驗:

| | 原始借券量 | 規模中性後 |
|---|---|---|
| 強盤 Q4−Q1 | +0.91pp | **+0.19pp**(且非單調) |
| 全部 Q4−Q1 | +0.34pp | **−0.23pp**(翻負) |

→ 強盤那個漂亮的 edge **控制規模後幾乎消失**。原本已建議上線,經此檢查**收回建議**。

**通則(寫給未來的自己):任何以「量」為單位的因子(借券量、成交量、法人買賣量…),分位驗證前必須先做規模中性化**,否則測到的是市值/流動性效應。分點 net_breadth 用的是「家數比例」故無此問題。

**E 的價值:確認 `net_breadth` 是分點這條路上唯一該用的因子**,避免往系統塞三個假/弱訊號。

---

## 22. 我的實戰紀錄 — 量化「人工挑選 vs 全名單」(2026-07-19)

**動機(最大盲區):** 使用者不照單全收,而是拿核心+觀察清單**自己逐檔分析後挑一部分買**([[twse-user-hybrid-discretionary]])。系統原本**完全量不到他的人工判斷是加分還是扣分** —— 回測再怎麼準,都回答不了「我的用法到底行不行」。

**做法(`docs/index.html`,純前端):** 在「追蹤台帳」每列加「我的」欄,兩顆按鈕 **買 / 跳**(再按一次取消)。標記存 `localStorage['twse-mypicks']`(`{"選股日|代號": "bought"|"skipped"}`),**與持倉一樣不上傳、不進 GitHub、不進信件**。台帳上方自動顯示三組對照:**我買的 / 我跳過的 / 全部推薦**(平均報酬率 + 勝率 + 檔數)。

**刻意的防自我欺騙設計:**
- 標記未滿 **20 檔**時**不下任何結論**,只顯示「樣本太少時差異多半是運氣」。
- 結論句雙向誠實:加分會說加分,**扣分也會直說「你的人工篩選反而扣分,值得檢討挑選標準」**。
- 明寫「報酬率用台帳的『選股日成本→最新收盤』,與你實際進出價不同;這衡量的是**挑選能力**,不是真實損益」。

**驗證:** 用真實 166 筆台帳在瀏覽器實跑 —— 三組數字與獨立計算完全吻合(+11.76% / +0.05% / −15.45%);<20 檔時正確只顯示警告不下結論;按鈕 set/取消/切換/localStorage 持久化皆正確;332 顆按鈕(166 列×2)正常渲染;無 console 錯誤。

**核心精選卡片也能標記(2026-07-19 加):** 使用者每天第一個看的是「核心精選」,在那標記最自然。`mpCardBtns()` 加在卡片右上信心分下方,**key 與台帳共用同一組 `選股日|代號`**(核心卡取 `DATA.date`),所以兩邊標記互通、統計一致。`mpSyncBtns()` 就地更新同 key 的所有按鈕(不整段重繪、不跳掉捲動位置);`mpBindBtns()` 統一綁定並用 `_mpBound` 防重複綁。實測:5 檔核心→10 顆按鈕、key 格式正確、**點核心卡的「買」台帳同一檔同步亮起**、再點取消、無 console 錯誤。

---

## 23. 持倉多帳戶存檔(2026-07-19)

**需求:** 使用者有**兩個券商帳號**,需分開管理持倉。

**資料結構:** 新 key `localStorage['twse-portfolio-accounts']` = `{v, active, accounts:{帳戶名:[持倉...]}}`。
關鍵設計:**`PORT.positions` 永遠指向「目前帳戶」的陣列**,所以既有全部程式(`portGet`/`portUpsert`/`renderPortfolio`/截圖辨識/健檢/AI 總覽…)**完全不用改**。
- `portLoad()`:先讀新 key;沒有則從舊 key `twse-portfolio` **自動遷移成「預設」帳戶**,且**不刪舊 key**(可回溯)。
- `portSave()`:把 `PORT.positions` 寫回 `ACC.accounts[active]` 再存。
- `accSwitch/accAdd/accRename/accDelete`:切換時先存目前帳戶再切;刪除時擋「至少保留一個」;改名保持原順序。

**UI:** 持倉頁上方帳戶頁籤(名稱 + 持股檔數,目前帳戶高亮)+ ＋新增 / ✏️改名 / 🗑刪除(單一帳戶時自動隱藏刪除)。

**⚠️ 匯出/匯入改成全帳戶:** `portExport()` 匯出**整個 ACC**(不只目前帳戶)—— 否則使用者會以為備份了卻漏掉另一個帳號。`portImportFile()` 同時相容新版(多帳戶)與舊版(單一 positions,匯入到目前帳戶)。

**驗證:** 瀏覽器實測 —— 舊資料自動遷移(2 檔→預設帳戶、新 key 已寫、舊 key 保留)、新增第二帳戶、**切換後兩邊持倉完全隔離**(預設 2 檔 / 永豐 1 檔互不污染)、重新 load 持久化正確、匯出含全部帳戶、單一帳戶時隱藏刪除鈕。

---

## 24. 族群分類換成 FinMind 產業鏈(2026-07-19)

**動機:** 舊分類是 `docs/sector_map.json` 手工維護的 46 條 `_industry_map` + 306 筆 `_override`,底層是 TWSE 的粗類別(電子零組件業/其他電子業…),每次分類不準就得人工再補 override(見第 15 節那幾輪修正)。改用 **FinMind `TaiwanStockIndustryChain`**:**47 產業 / 483 細產業 / 2344 檔**,而且**一檔可屬於多條產業鏈** —— 鴻海同時在 電腦及週邊設備(伺服器/主機板/機殼/NB…)、通信網路、連接器、電動車輛。這才是台股實際在講的「族群」。

**新檔 `scripts/industry_chain.py`** — `build_sector_map(out_path)` 抓 FinMind 寫出 `docs/sector_map.json`,新 schema(舊的 `_industry_map`/`_override` 已整包淘汰):
```json
{"_source":"FinMind TaiwanStockIndustryChain","_built":"2026-07-19",
 "_stats":{"stocks":2344,"industries":47,"sub_industries":483},
 "chain":  {"2317":[["電腦及週邊設備","伺服器"],["連接器","連接器設計、組裝及製造"],...]},
 "primary":{"2317":["電腦及週邊設備","伺服器"]}}
```
- `chain` = 全部歸屬,**市場氛圍**用(一檔計入多個族群)。
- `primary` = 代表性那一條,**treemap** 用(一檔只能放一格,否則成交額重複計)。挑法:先取該檔細產業條數最多的產業(同分取全市場較大者),再取該產業內家數最多、名字非「其他…」的細產業(排「其他」是因為 FinMind 每個產業都有垃圾桶分類,不排的話 treemap 一大票擠在「其他電腦及週邊設備」)。
- 細產業名常帶一長串舉例(「網路設備(如數據機、網路卡…)」),建檔時把括號整段砍掉。
- **抓不到就回 None 不動既有檔案** —— 單次 API 掛掉不該讓整頁族群空白。
- 手動重建:`FINMIND_TOKEN=… python -m scripts.industry_chain`。

**`scripts/main.py`:** 寫完 `heatmap.json` 後呼叫 `build_sector_map(docs_dir/"sector_map.json")`,包在 try 內(失敗只 warning)。分類變動很慢,但跟著批次每天重抓最省事。

**`docs/index.html`:**
- `hmSecOf(s,sm)` 改讀 `primary`;新增 `hmSecsOf(s,sm)` 回全部歸屬給 `moodAgg` 用。**FinMind 沒收錄的退回 TWSE 原始產業名**(多為 KY 股,掃描池 860 檔中 18 檔,約 2%),不硬塞。
- `moodAgg`:一檔屬於幾條鏈就計入幾組(同組內去重)。**各組家數加總會大於總檔數,這是刻意的,族群本來就重疊。**
- 抽出 `_moodOf(arr,hasBreadth)`;**大盤氣氛改用全體個股直接算**,不再用各族群加權平均 —— 產業鏈重疊會讓跨鏈多的權值股(鴻海、台達電)被重複計到而放大。

**驗證(browser preview,7/17 資料 860 檔):** treemap **47 個產業、860 個 leaf(無重複計)**、18 檔 fallback;市場氛圍 47 列,被動元件 47 檔 −50.0、半導體/電腦及週邊設備/印刷電路板/連接器/智慧電網 等新族群正確出現;細產業層可用;無 console 錯誤。
⚠️ 現有 `heatmap.json` 是 7/17 產的,還沒有 `p`(股價)/`k`(K棒)欄位,所以成分股展開的股價欄顯示「—」、迷你K留白 —— **與本次改動無關**,下次批次跑完就有。

---

## 25. 即時報價層 + 全市場快照存檔(2026-07-19)

**背景:** 使用者開通 FinMind **Sponsor**(實測 level 3、6000 req/**hour**、訂閱 2026-07-17 ~ **08-17**,按月、隨時可能不續)。實測發現關鍵端點:

```
GET /api/v4/taiwan_stock_tick_snapshot        ← 不帶 data_id 就是「全市場」
→ 2852 檔、一次呼叫、0.7 秒、171 KB
```

帶了三個原本完全沒有的欄位:**均價 `average_price`(VWAP)、量比 `volume_ratio`、最佳一檔委買賣**。
(對照原本的盤中即時:只有核心 ~5 檔、一天兩個時點、靠 TWSE MIS 非官方 API。)

⚠️ **「盤中是否真即時、有沒有延遲」尚未驗證** —— 建置當天是週日,時間戳為上週五 14:30。**必須在交易日盤中實測一次**才能下結論。

### 瓶頸是儲存不是額度(實測)
| 存檔頻率 | API 用量 | 儲存 |
|---|---|---|
| 每天 1 次 | 0.02% | 3 MB/月 ✅ |
| 每天 7 檢查點 | 0.1% | **8 MB/月** ✅(實際比估算小,parquet 跨檢查點壓縮字串) |
| 每分鐘 | 4.5% | 900 MB/月 ❌ |
| 每 5 秒 | 54% | 10.8 GB/月 ❌ |

**「監控」可高頻(不存檔)、「存檔」一天 7 點。** 別因為額度夠就拉高存檔頻率。

### 三條鐵則(使用者明確要求)

**鐵則一:即時資料絕不進選股/回測層。** 訂閱到期時選股會**安靜地**壞掉(不 raise,只是少一個因子、分數整體偏移),回測結果更會永遠重現不了。
→ 用程式碼守,不是註解:**`scripts/check_realtime_isolation.py`** 用 AST 檢查 `scoring/indicators/backtest/screener/industry` 有沒有 import `quotes`/`snapshot_archive`,違規 exit 1。**已接進 `daily.yml` 的 `Check realtime isolation` step**。已做反向測試(故意加一行 import → 確實擋下並指出行號)。

**鐵則二:降級要看得見,不准靜默。** 每筆 `Quote` 帶 `source` + `ts`;`main.py` 把 `sponsor_status()` + `archive_stats()` 寫進 `data.json.realtime`;前端 `dataSourceBox()` 在盤中即時頁顯示「即時/降級」、訂閱剩幾天(**≤7 天轉警示色當續訂提醒**)。

**鐵則三:每天存檔。** 存下來的均價/量比在訂閱到期後沒有 API 補得回來。

### 新增檔案

**`scripts/quotes.py`** — 統一報價層,三段降級:
```
① Sponsor 全市場快照 2852 檔(含 vwap/量比/委買賣)
② TWSE MIS 逐檔(原本的作法,50 檔/批)
③ 本機 parquet 昨收(最後防線)
```
- `get_quotes(symbols)` → `{sid: Quote}`,**永遠回得到東西**。`vwap`/`volume_ratio`/`bid`/`ask` 只有 ① 有,呼叫端必須當「可能沒有」處理。
- `sponsor_status()` 查等級與到期日,查不到一律 `active=False`(保守降級)。
- `market_snapshot_source()` 給網頁用;**區分「訂閱到期」與「有訂閱但 API 暫時異常」**,免得誤判成該續訂。
- ⚠️ **踩到的坑:`NaN` 在 Python 是 truthy**,`row.get("close") or None` 擋不掉,停牌股會把 NaN 帶進前端。全部欄位改走 `_num()`。
- ⚠️ 快照的 `name` 欄實測**只有 3.2% 有值**,用本機月快取 `stock_info` 補(不額外打 API)。

**`scripts/snapshot_archive.py`** — `data/snapshots/YYYY-MM/YYYY-MM-DD.parquet`,一天一檔含當天所有檢查點(`snap_tag` 欄)。
- **同 tag 重跑會覆蓋該 tag 的列**(補跑安全,實測 5704 列重跑仍是 5704)。
- **時間戳守門**:快照日期 ≠ 今天(假日/颱風假/尚未更新)就不寫檔,避免污染歷史。
- `archive_stats()` 給網頁顯示累積量。

**`.github/workflows/snapshot.yml`** — `workflow_dispatch` 帶 `tag`,7 個檢查點見 `SETUP_PREMARKET_CRON.md` 新增章節(0900/0930/1000/1100/1200/1300/1330,只設一條就設 **1330**)。非交易日/無訂閱/API 失敗都乾淨跳過不變紅。

### 驗證
三層降級全部實跑且**三層價格一致**(2330 都是 2290):① sponsor 拿到 vwap 2351.58/量比 2.7/委買賣;② 模擬快照掛掉 → 落到 MIS(有價無 vwap);③ 再關掉 MIS → 落到本機昨收 `is_live=False`。存檔的 stale 守門、append、同 tag 去重都正確。前端四種狀態(無資料/正常/剩5天/已到期)文案皆正確、無 console 錯誤。
測試時產生的假存檔(同一份收盤資料被標成兩個檢查點)**已刪除**,不留造假的盤中歷史。

### 還沒做(下一步)
1. **盤中實測延遲** ← 前提,沒驗證前別把它當即時用
2. 盤中停損/停利實時觸發(出場層是目前唯一加分的環節,+12.83pp)
3. 跳空棄單當場決策
4. 移動停利改用當日盤中高點(現在取日收盤,停利點位一直被低估)
5. 等存夠資料後驗 VWAP/量比因子(**量比天生是比例,不用再做規模中性化**)

---

## 26. ⚠️ 選股層負超額診斷(2026-07-19)—— 目前專案最重要的一件事

**這一節比其他所有功能都重要。在讀懂它之前,不要再加新功能。**

### 結論

用真實台帳 166 筆(選股日 2026-06-17 ~ 07-17)對 TWII 逐筆算超額報酬:

**選股後第 N 個交易日的超額**(每檔各自對齊選股日,無共同終點偏誤):

| 第N日 | 平均超額 | 中位數 | 勝過大盤 | 樣本 |
|---|---|---|---|---|
| 1 日 | **−0.52pp** | −1.14 | 41.0% | 161 |
| 2 日 | −1.26pp | −2.04 | 39.1% | 156 |
| 3 日 | −2.46pp | −2.99 | 32.5% | 151 |
| 5 日 | −4.37pp | −4.61 | 29.4% | 143 |
| 10 日 | −5.80pp | −6.02 | 32.7% | 110 |
| 15 日 | −5.45pp | −7.30 | 23.3% | 60 |

**負超額從第 1 天就開始,單調惡化到第 10 天才打平。沒有任何一個持有期是正的。**
→ 這**不是出場/抱太久的問題**,是選股本身從進場當下就是負貢獻。

其他切法(結算到 7/17):平均超額 **−8.16pp**、中位數 −8.32、只有 **18%** 勝過大盤。
排除 7/17 崩盤日(指數單日 −6.47%)結算到 7/16 仍是 **−8.12pp** —— 且大盤 6/17→7/16
其實只有 **−0.55%**(幾乎持平)。**所以不是「大盤爛」也不是「高 beta 被崩盤放大」**:
就算 beta=2,持平的大盤也只能解釋 −0.55pp,剩下 7.6pp 與市場方向無關。

### 診斷細節

**1. 不是少數個股拖累** —— 中位數 ≈ 平均(−8.32 vs −8.16)。**18/21 個選股日平均超額為負。**

**2. 信心分對「相對表現」沒有鑑別力** —— 相關係數 **−0.056**(幾乎是 0,且方向錯):

| 分數四分位 | 平均分 | 平均超額 |
|---|---|---|
| 最低 25% | 66.3 | −6.6pp |
| 次低 | 71.4 | −7.6pp |
| 次高 | 75.6 | −10.6pp |
| 最高 25% | 85.9 | −7.9pp |

⚠️ 但這是**在已入選的核心股之間**比(分數範圍被截斷在 66~86),restriction of range
會壓低相關係數。所以它證明的是「**核心名單內部的排序是雜訊**」,不等於信心分整體無效。

**3. 動能 profile 最差** —— 動能 **−11.5pp**(40 檔)、均衡 −8.8(78)、品質 **−4.3**(48)。
與「這一個月是動能反轉月」一致,也與回測早先結論(動能是 beta 非 alpha)同向。

**4. 「90% 曾經有賺、平均峰值 +8.2%」是陷阱** —— 一度以為是「有波動沒抓到、該改停利」,
但事件時間曲線顯示第 1 天就是負的。個股波動大,持有 10 天以上幾乎必然在某個時點翻正,
**peak_gain 不是可捕捉的 edge 的證據**。差點據此得出錯誤結論,記錄下來免得下次再踩。

**5. 出場規則是有效的** —— signal −16.82% vs exec −3.99%,**delta +12.83pp**。
但它是靠「砍得快」省錢,不是靠「抓到漲幅」。與 [[twse-exit-rule-finding]] 的「出場自傷 0.8pp」
方向相反 —— 那是別的 regime 測的,動能反轉時機械停損確實救命。

### 誠實的統計保留

- **只有 1 個月、1 種 style regime**(動能反轉月),不是多年驗證。
- 166 筆**不是 166 個獨立觀察**:同一天選 10 檔、彼此高度相關,有效樣本更接近「21 天 × 幾個獨立方向」。
- 使 它可信的不是 166 這個數字,而是**它與離線回測(多年、超額 vs 大盤全負)獨立地得到同一結論**。
  兩個方法、兩種資料、同一個答案。

### 為什麼拖到現在才發現

`scripts/track.py` 與 `docs/index.html` 裡「超額 / benchmark」出現次數 = **0**。
只有離線 `scripts/backtest.py` 有大盤基準。**研究層用對的尺,儀表板用錯的尺** ——
網頁顯示「平均報酬 −4.84%」會讓人以為是大盤拖累,而大盤幾乎沒動。

### 下一步(依序)

1. **線上台帳加大盤基準**(半天)—— 每筆算同期 TWII 報酬與超額,績效頁主軸從「平均報酬」
   換成「超額報酬」。不新增 alpha,但**停止用錯的尺自我安慰**。「我的實戰紀錄」對照同樣要改。
2. **regime 閘門要能真的關機** —— 現在 `defensive` 仍出 3 檔、`min_score` 依設定註解等同不約束,
   系統從來沒有「今天不出手」這個輸出。可直接用現有回測驗證。
3. **開反轉軌道與動能軌道並行** —— 回測結論是「真 alpha 在反轉 + 擇時」,但信心分至今仍是
   趨勢25+相對強度25+短線時機量能25 的動能複合。與其微調動能權重撞天花板,不如做第二條邏輯對比。
4. 重驗信心分排序在**全市場**(非僅核心名單內)對超額的鑑別力 —— 解掉 restriction of range。

**再次強調:第 25 節的即時報價層、第 24 節的產業鏈分類,都沒有碰到這個問題。它們改善的是
看盤與執行,選股層的負超額原封不動。**

---

## 27. 盤中訊號掃描 + 一次性提醒(2026-07-19)

**動機:** 對標「起漲K線」那類付費軟體(NT$3,690/年)的核心 —— 全市場自動盯盤、發動時通知。
差別是規則全部寫在 `scripts/intraday_scan.py` 看得見,且每次觸發都存檔供事後驗證。

**使用者的關鍵要求:「價格會上下來回波動,所以若是有的話提醒一次就好。」**

### 三層防抖(這是本節重點)

1. **當日去重** —— key = `{stock_id}|{type}`,存 `data/alerts/YYYY-MM-DD.json`。
   **狀態存檔案不存記憶體**,因為每次輪詢都是新的 process(GitHub Actions run)。
2. **突破緩衝** —— 要超過前高 `BREAKOUT_BUFFER=0.3%` 才算,不是碰到就算。
3. **站上均價確認** —— VWAP 移動慢,`close > average_price` 濾掉衝一下就被打下來的假突破。

**實測:8 次輪詢、其中 4 次在突破區、價格反覆穿越 → 只寄 1 封。** 跨 process 狀態也驗過。

### 訊號

- **breakout(量增突破)**:突破 20 日高 ×0.3% 緩衝 · 量比 ≥1.5 · 站上均價 · 漲幅 1~8%(過熱不追)
- **pullback(回檔買點)**:多頭排列 5>20>60 · 距月線 ≤3% · 當日翻紅站回均價

參數全在檔案頂端常數區。**條件單獨失效的反向測試都驗過**(量比不足/跌破均價/漲太多/漲太少 皆不觸發)。

### 架構

- `--build-levels`(盤前 08:50)從本機 parquet 算均線/前高 → `data/levels.parquet`(1022 檔,62 KB)。
  **沒跑這條,盤中掃描會直接跳過** —— 即時快照只有當下價量,沒有均線/前高。
- `scan`(09:05–13:25 每 5 分鐘)= 1 次 API 呼叫(全市場快照)join levels。整盤約 54 次 = 額度 0.9%。
- 輸出:`data/alerts/*.json`(去重狀態 + 稽核紀錄)、`docs/alerts.json`(網頁)、Email(只寄本輪新觸發)。
- 沒有新訊號 → 不寄信、不 commit(不會產生每 5 分鐘一個 commit)。
- `.github/workflows/intraday.yml`,cron 設定見 `SETUP_PREMARKET_CRON.md`。

### 驗證

用「到 7/16 為止」的資料建 levels、對 7/17 快照掃描(模擬真實盤前建檔,不偷看當日高點):
**1026 檔比對 → 觸發 5 筆**(3 breakout / 2 pullback)。在一個大盤跌 6.47% 的日子只出 5 筆,數量合理不洗版。
前端:今日訊號列表、空狀態、**隔日守門**(alerts.json 停在昨天不會被當今天顯示)皆正確,無 console 錯誤。
示範用的 `docs/alerts.json` 已刪除(捏造資料不進 repo)。

### ⚠️ 必須記住的張力

**breakout/pullback 都是動能型訊號,而台帳實測動能 profile 平均超額 −11.5pp(三種風格最差)。**
Email 與網頁都寫了這句警語。做這個功能的價值是「免費驗證它到底行不行」,不是「它會賺」。
**下一步應該把 alerts 接進台帳追蹤超額**,三個月後就有答案 —— 見第 26 節。

### 分點(籌碼K線類)的替代方案

全市場掃分點要 2852 次呼叫 ≈ 71 分鐘,且 2330 單日就 16,067 列,**原始資料不可能存進 repo**。
結論:**不該全做**,用排除法 ——
只掃「核心+觀察+自選+持倉」(~40 檔 / 1 分鐘)或再加「當日量比>2 或漲跌幅>3% 的異動股」(~150–300 檔 / 4–8 分鐘),
且**只存聚合指標(主力買超集中度/前5大分點/隔日沖比例)不存原始列**。
盤中掃出訊號 → 當晚只對那些股票查分點,兩件事天然串起來。尚未實作。

---

## 28. 常駐盯盤 + 分點籌碼(2026-07-19)

### 28.1 從「每 5 分鐘一個 job」改成「常駐輪詢」

使用者:「五分鐘掃一次太少了不夠即時,額度有 6000。」**他是對的,但瓶頸不是額度。**

每 5 分鐘開一個新的 Actions run,光 checkout + setup-python + pip install 就要 40~90 秒,
**大部分時間在裝環境不是在掃描**,而且開太密會排隊。所以改成:

**`--loop`:一個 job 從 08:55 跑到 13:35(4.5 小時 < Actions 單一 job 6 小時上限),
環境只裝一次,之後純輪詢。** 公開 repo 標準 runner 不計費。

- `--interval` 預設 20 秒。4.5 小時 × 20 秒 = 810 次 = **180 次/小時 = 額度的 3%**。
- 單次掃描失敗會吞掉繼續跑(實測:第 3 次拋例外,迴圈未中斷、節奏不亂、errors 正確計數)。
- `no_sponsor` / `no_levels` 這兩種再輪也不會好 → 直接停,不空轉 4 小時。
- `--publish`:有新訊號當場 git commit/push,網頁不必等收盤。

**⚠️ 真正的上限沒量過:上游快照多久更新一次。**
若上游 60 秒才換一份,你每 5 秒問一次也只是拿到同一份 —— 快的是「你問的頻率」不是「資料」。
→ 新增 **`--measure-freshness SEC`**:盤中跑,記錄時間戳變化的中位間隔。
**交易日先跑這個再定 interval,別憑感覺設。** workflow 有 `mode=freshness`。

### 28.2 分點籌碼(範圍:核心+觀察+自選)

使用者選擇不掃全市場。`scripts/branch_chips.py`:

- **只存聚合指標不存原始列** —— 2330 單日 16,067 列(分點×成交價),全市場數百萬列,repo 直接爆。
  每檔壓成一列:`n_traders` / `buy_concentration`(前5大買超÷成交量)/ `concentration` /
  **`day_trade_ratio`(同一分點當日 min(buy,sell) 合計 ÷ 成交量 = 隔日沖嫌疑)** / top_buy / top_sell。
- **實測 30 檔 ≈ 1 分鐘、0.09 MB/日**。抓取沿用既有 `branch._fetch_one_day`,不另開第二條路徑。
- 實跑範例(7/17):華南金 分點765家、買超集中56.4%、隔日沖32.7%;凱基金 隔日沖 **56.9%**(對敲兇)。
- ⚠️ 分點**當晚 21:00 才發布**,只能盤後跑(建議 21:30)。
- ⚠️ **持倉在 localStorage 不上傳**(隱私設計),所以拿不到 → 要查請自己加進 `config/watchlist.json`。

### 28.3 ⚠️ 發現:分點加成已經在影響選股,且依賴訂閱

`main.py` 的 stage-2 已有 `branch_bonus`(2026-07-18 加),用 `branch.fetch_breadth_for` 算逆向廣度
餵進 `_rank_core` → **會改變核心名單**。而分點是 Sponsor 級資料。

也就是說 **訂閱到期後選股結果會改變**(降級成 bonus=0,不會壞掉但名單不同)。
現行 `check_realtime_isolation.py` 抓不到這條 —— 它只檢查 `scoring/indicators/backtest/screener/industry`,
而這條路徑是 `main.py → branch.py`。

**尚未處理。選項:(a) 把 branch 納入鐵則一檢查並移出選股層;(b) 維持現狀但在 data.json
明確標記「當天有沒有吃到分點加成」,讓事後回測知道每一天的選股是在哪種資料條件下產生的。**
建議 (b),因為分點加成本身是經過驗證的因子(見 [[twse-branch-factor-validated]]),
不像即時報價那樣純屬執行層。

---

## 29. 網頁真正即時 + 自選池網頁化 + 只點我買的(2026-07-19)

### 29.1 ⚠️ 先承認一件事:在這之前「網頁完全不即時」

即時報價層(第 25 節)做好了,但**沒有任何前端在用它**。網頁讀的是批次 commit 的靜態
JSON(data.json / premarket.json / alerts.json),永遠是上一次批次的快照;
持倉價格是點進去才現抓 `/api/detail`(yfinance 延遲報價),使用者實測要等 5~15 秒。

**即時只存在於伺服器端的掃描與 Email。使用者問「即時到底即時在哪裡」是完全正確的質疑。**

### 29.2 `api/quote.py` —— 讓網頁真的即時

`GET /api/quote?ids=2330,2317`(Vercel,maxDuration 15s)。走 `scripts/quotes.get_quotes()`,
即 Sponsor 全市場快照 —— **一次呼叫換整份,所以查 1 檔和查 50 檔一樣快**。
實測:8 檔 0.84s,同實例 TTL 內第二次 0.002s(不再打 API),payload 2.3 KB。

前端 `startLiveQuotes(20)` 每 20 秒輪詢一次,清單 = 自選池 ∪ 持倉 ∪ 今日核心(去重)。
`portPriceOf()` 改成**即時優先** → 批次收盤 → detail API。
回傳帶 `source`/`source_label`/`sponsor_days_left`(鐵則二)。
**拿不到就靜靜沿用靜態資料**(本機無 API 實測:`liveQuotes` 回 null、無 console 錯誤、頁面照常)。

⚠️ **Vercel 需設 `FINMIND_TOKEN` 環境變數**,否則這支只會回昨收(參考新聞分析當初漏設 API key 的前例)。

### 29.3 自選池改成網頁直接加

`localStorage['twse-watchlist']`。**加持倉會自動加入自選池**(`portUpsert` 內呼叫 `wlAdd`),
另有「匯入持倉」補既有部位、「複製設定檔」產生 `config/watchlist.json` 內容。

⚠️ **刻意的雙軌,不是 bug**:localStorage 不上傳(與持倉一致的隱私原則),
但雲端批次跑在 Actions 上、只看得到 repo 裡的 `config/watchlist.json`。所以
網頁自選池立即影響「網頁上的即時報價/顯示」,要讓**批次**(盤後籌碼、分點掃描)也認得,
必須按「複製設定檔」貼進 repo 再 commit。UI 上已寫明這件事。

實測:加入/自動從持倉加入/一鍵匯入/刪除/重載持久化 全部正確。

### 29.4 「只點我買的」

使用者:「降到只點我買的。」→ `mpGet()` 改成:
**當天只要點過任何一檔「買」,同一天其餘沒點的自動視為「跳過」**。

關鍵細節:**沒點過任何「買」的日子維持「未標記」** —— 不能把使用者根本沒看的日子
當成「全部跳過」,那會污染統計。實測五種情境皆正確,含「取消當天最後一個買 → 整天退回未標記」。

### 29.5 freshness 自動量測(使用者:「我會忘記」)

不做獨立 cron。`--loop` 啟動後**自動先量 90 秒**(每 3 秒一次)上游時間戳變化,
寫進 `docs/freshness.json`,並在 `interval < 中位間隔 × 0.8` 時警告
「多問的那幾次拿到的是同一份資料」。這是唯一能量的時機(只有盤中資料才會變),
所以綁在盯盤啟動最自然。

---

## 30. 完整即時欄位(2026-07-19)

使用者:「現在的即時資訊好少」,列出 17 個要的欄位。逐項落地結果:

### 直接來自快照(零額外成本,已上線)
成交價 / 漲跌 `change_price` / 漲跌幅 / **單量** `volume`(最後一筆)/ **總量** `total_volume` /
**成交金額** `total_amount` / 委買價量 / 委賣價量 / 當日最高 / 當日最低 / 均價 VWAP / 量比。

### 衍生(算法寫在 `api/quote.py:_enrich`,不做黑盒子)
- **委買賣比** = 委買量 ÷ (委買量+委賣量)。⚠️ 這是**掛單**失衡,**不是內外盤比**,兩者不同。
- **換手率** = 成交張數×1000 ÷ 流通股數。流通股數 = **市值 ÷ 收盤價**反推
  (`TaiwanStockMarketValue` 可 bulk,2717 檔一次呼叫)。2330 反推 259.3 億股與公開股本相符。
  **levels 覆蓋率 100%**。
- **是否突破** = 現價 > 20日高 × 1.003(與盤中掃描同一條規則,不會兩邊說法不一)
- **是否回測五日線** = |現價/MA5 − 1| ≤ 1.5% 且現價 ≥ MA5×0.985

### 靜態欄位併進 levels(盤前算一次,即時端點只 join)
**產業別**(FinMind 產業鏈 primary,覆蓋 97%)、流通股數(100%)、
**營收 YoY**(35%)、**EPS 近四季 TTM**(28%)。
⚠️ 營收/EPS 只有批次補抓過的股票才有(核心+自選),所以覆蓋率低是正常的 ——
**加進自選池後隔天批次就會補上**。

### 內外盤比 —— 需要逐筆資料,獨立管線
快照的 `TickType` **只是最後一筆的方向,不是全日累計**,算不出內外盤比。
真的要算得用 `TaiwanStockPriceTick`:**2330 一天 20,922 筆**,太重不能塞進 20 秒輪詢。
→ `deep_metrics()` 在盯盤迴圈裡**每 10 分鐘**對「自選池 + 今日核心」算一次,寫 `docs/deep.json`。

**驗證:2330 7/17 外盤 53,567 / 內盤 21,248 = 外盤比 71.6%,兩者相加 74,815 張
與快照 `total_volume` 完全一致** → 方向判定沒算錯。鴻海 74.8%。

### 還沒做 / 做不到
- **分點連買連賣天數**:算得出來,但需要分點的**歷史**。`branch_chips.py` 從今天起才開始累積,
  要立刻有得回補(30 檔 × 10 天 = 300 次呼叫 ≈ 10 分鐘)。**尚未實作。**
- **營收/EPS「預估」**:免費資料**沒有分析師預估值**。目前給的是已公布的
  營收 YoY 與 EPS TTM(實績,不是預估)。硬要做只能走「新聞轉述版」那條路
  (見 [[twse-branch-target-price-infeasible]] 的目標價作法),但那是轉述不是模型預估,
  **不該標成「預估」誤導自己**。

### 前端
自選池每列展開完整欄位表(`wlDetail`),缺值一律「—」不假裝有值。
換手率恆為正,不用 `fmt()` 的 +/− 號(YoY 才需要)。

---

## 31. 即時報價卡改版(2026-07-19)

使用者:「目前的即時資料我覺得會讓人看得眼花撩亂,都是單色而且字小密集複雜」,
要求參考美股/加密貨幣交易軟體而非台灣傳統陽春介面。

### 問題

第 30 節把 17 個欄位平鋪成同樣大小的 11px 灰字 —— **沒有階層,所有數字一樣重要
就等於都不重要**。而且狀態欄印「突破20日高✗ 回測5日線✗」,不成立的也佔版面。

### 改法(交易軟體的通用分層)

1. **Hero 價格** 28px(手機 24px),漲跌用 ▲▼ + 顏色;其餘一律降級到 10.5~14px
2. **兩個比率畫成 meter 長條** —— 委買/委賣、外盤/內盤。失衡程度用看的,不用讀數字
3. **KPI 格**(總量/成交額/換手率/量比):值 14px 粗體、標籤 10.5px 淡色
4. **狀態只在成立時出現 chip**,不成立就不佔版面
5. **`font-variant-numeric: tabular-nums`** —— 數字等寬才對得齊、跳動時不會抖
6. 產業別做成右上角 chip,不跟數字搶位置

### ⚠️ 配色是驗證過的,不是憑感覺

跑 `dataviz` skill 的 `validate_palette.js`:

| 模式 | 色 | CVD(deutan) | 正常視覺 |
|---|---|---|---|
| light | `#0f9d58` / `#e0383e` | **ΔE 4.3 FAIL** | 32.2 PASS |
| dark | `#34d27e` / `#ff5c63` | ΔE 6.3 WARN | 35.0 PASS |

**紅綠對紅綠色盲幾乎分不開。** 但漲跌用紅綠是金融慣例、且是專案既有 token,
不該為此改掉。依 skill 的規則,CVD 落在floor band **只有搭配次要編碼才合法** →
所以 meter **兩端一律附文字標籤**(「委買 450」/「532 委賣」)、中間留 **2px 縫**,
**絕不只靠顏色分辨買賣方**。這也是 mark spec 要求的分段間隙。

### 驗證(⚠️ 截圖工具在這次環境逾時,改用量測渲染結果)

| 項目 | 桌機 560px | 手機 375px |
|---|---|---|
| 價格 / KPI值 / KPI標籤 | 28 / 14 / 10.5 px | 24 / 14 / 10.5 px |
| KPI 欄數 | 4 | **2**(media query 生效) |
| 長條分段寬 | 委買243:委賣283、外盤396:內盤130 | — |
| 分段間距 | 2px | 2px |
| 等寬數字 | tabular-nums | tabular-nums |
| 橫向溢位 | 無 | 無 |
| 內容被截斷 | 無 | 無 |

暗色:頁底 `#0a0e17` / 卡底 `#121826` 分層明確,價格色隨主題切換(非自動反轉,兩套 token 各自定義)。

**沒有實際看過截圖** —— 這次環境的 screenshot 一直逾時,只做到量測幾何與計算樣式。
下次有截圖能力時應補看一眼(skill 步驟 7 要求「render it and look at it」)。

---

## 32. 卡片走勢線 + 漲跌停標註(2026-07-19)

### 32.1 走勢線(不是 K 線,這是刻意的)

卡片是**掃視**用的,一次看 5~10 檔。每張塞完整 K 線會比使用者原本抱怨的「字小密集」更糟。
掃視當下真正要判斷的只有兩件:**今天的形狀**、**現在站在均價之上還是之下** ——
一條價格線 + 一條均價線就答完了,34px 高。完整 K 線留給個股詳情頁(專心看一檔的地方)。

**資料來源選擇:用 `TaiwanStockKBar` 1分K,不用「自己每 20 秒累積」。**
自累的問題是盯盤 job 中途重啟或晚開就會缺一段;KBar 是回溯完整的,任何時候抓都有 09:00 至今全部。
2330 實測一天 266 筆完整 OHLCV,一檔一次呼叫。

`intraday_series()` 降頻到每 3 分鐘、上限 100 點(**最後一點一定保留 = 最新價**),
均價線用累計成交額÷累計量(等同當日 VWAP)。實測末值 2352.42 vs 快照官方均價 2351.58,
差 0.04% —— 夠畫線。輸出 `docs/series.json`,3 檔 6.5 KB。
在盯盤迴圈裡與 `deep_metrics` 同一個 10 分鐘節奏更新。

### 32.2 漲跌停:用檔位法,不用百分比門檻

台股漲跌停是「昨收 ×1.1 後**往下取到最近的檔位**」,檔位隨股價分級
(<10:0.01 / <50:0.05 / <100:0.1 / <500:0.5 / <1000:1 / ≥1000:5)。

**實測 7/17 差異很大:精算檔位法 = 漲停 8 檔、跌停 129 檔;用 ±9.8% 門檻 = 199 檔,
多出來的 62 檔全是誤判。** 所以照規則算,不用門檻。

驗證:3583 昨收 830 → 跌停價 747,現價 747 → 判定跌停 ✓;
8028 昨收 350 → 315 ✓;2330 −7.28%(跌停價 2225,現價 2290)→ 不判定 ✓。
檔位邊界 9.99/10/49.95/50/100/500/1000 全部正確。

**呈現:漲停綠底、跌停紅底(使用者指定),文字改白色**(不是把 --up 疊在 --up-bg 上,會糊)。
⚠️ **同時一定有「漲停/跌停」文字標籤** —— 紅綠色盲看不出綠底紅底的差別(deutan ΔE 4.3),
底色不能是唯一編碼。compact 版(核心/觀察/盤中)也會顯示標籤。

### 32.3 手機版修正

產業別原本擠在標題列、`max-width:46%`,窄螢幕會被截成「平面顯示器／生產製程及檢…」。
**產業別是判斷族群的關鍵資訊,不該為了排版被切掉** → ≤520px 時換到自己一行完整顯示。

### 驗證

| | 桌機 750px | 手機 375px |
|---|---|---|
| KPI 欄數 / 價格 | 4 / 28px | 2 / 24px |
| 走勢線 | 659×34,90 點,價格線+均價線+昨收基準線 | 319×34 |
| 產業別截斷 | 無 | 無(已修) |
| 橫向溢位 | 無 | 無 |

走勢線幾何實測:90 個點、Y 值範圍 2.0~32.0(填滿 34px 高度,不是平線)、
3583 因跌停鎖死只有 67 點(真實資料如此)。無 console 錯誤。
⚠️ 截圖工具仍逾時,只做到量測,未目視。

---

## 33. 分點連買連賣 + 自選池雲端同步 + 截圖檢查修正(2026-07-19)

### 33.1 截圖檢查發現的三個問題(使用者提供實際畫面)

1. **產業別分類錯** —— 旺宏標成「IC封裝測試」、華邦電標成「晶圓製造」,兩家其實是記憶體廠。
   根因:`industry_chain.py` 的 primary 挑「家數最多的細產業」,會偏向製程類的通用桶。
   **拿 10 檔已知答案實測三種規則:最多 6/10、最少 6/10、中位數 8/10** → 改用中位數。
   (最少會挑到冷門標籤:聯發科→光儲存控制IC、台達電→LED驅動IC。)
   修好:華邦電→記憶體IC、台達電→電源管理IC、緯創→筆記型電腦。
   仍會錯的:旺宏→晶圓製造、聯發科→網路通訊IC —— FinMind 分類本身沒有「IC設計」這種桶。
2. **空的 meter 佔版面** —— 跌停股(華邦電)委買賣與內外盤都無資料,卻各畫一條灰底長條。
   **空的視覺元素比沒有更糟**(會讓人以為「這裡有東西只是看不懂」)→ 無資料整條不畫。
3. 走勢線沒出現 —— `series.json` 只有盯盤迴圈會產生,非交易日沒有。**符合預期,非 bug。**

### 33.2 分點連買連賣天數

```
主力淨買超(當日) = 前 5 大買超分點淨買超 + 前 5 大賣超分點淨買超
連買 N 日 = 從最近交易日往回數,主力淨買超連續為正的天數
```
⚠️ **用「前 5 大」而不是全部分點,因為全部分點加總恆等於 0**(有買必有賣),算出來沒意義。

- `backfill(days)` 回補歷史(連買連賣要有歷史才算得出來),沿用 `branch.fetch_branch_daily`,
  已存在的日期會跳過。實測 4 檔 × 6 日約 13 秒;30 檔 × 15 日約 15 分鐘。
- `compute_streaks()` → `docs/branch_streak.json`,含 `net_series`(近 10 日淨買超)。
- 前端 chip 除了天數還畫 **近 10 日淨買超小柱狀** —— 「連 5 日但金額遞減」這種外強中乾
  只給天數看不出來。
- 實測:凱基金 2883 **連買 5 日**;永豐金 2890 連賣 5 日後翻正。
- 儲存:4 檔 × 6 日 = 76 KB。30 檔 × 15 日推估 ~1.4 MB,可接受但會長,日後要留意。

### 33.3 自選池雲端同步(`api/watchlist.py`)

使用者:「其實全部都上傳也可以,因為不會涉及到真錢,這只是網頁,而且這個網頁也只有我在用」,
但**明確保留:不要上傳總成本總損益**。

- `POST /api/watchlist` 用 GitHub Contents API 寫回 `config/watchlist.json`,批次即刻認得。
- **`_clean()` 只留「代號 → 備註」,任何 shares/cost/pnl 欄位一律丟棄** ——
  就算前端誤傳也不會進 repo。實測:含 `{shares:5,cost:4200,pnl:-12345}` 的輸入,
  輸出完全不含這些值;非代號 key 濾掉;備註截斷 60 字;上限 300 檔。
- **安全:沒設 `WATCHLIST_SECRET` 一律拒絕寫入(fail closed)**。
  這是公開端點,忘了設就變成任何人能改你的 repo,那比不能用嚴重得多。
  secret 存瀏覽器 localStorage,錯了會自動清掉重問。
- 需要的 Vercel 環境變數:`GITHUB_TOKEN`(repo 寫入權)、`GITHUB_REPO`、`WATCHLIST_SECRET`。
- 「複製設定檔」保留為離線備援(沒部署 Vercel 時仍可手動同步)。

### 驗證
連買 chip 與 6 根柱狀正確渲染、空 meter 已隱藏、走勢線 2 條、同步鈕存在、無 console 錯誤。

---

## 34. Vercel 環境變數要重新部署才生效(2026-07-19 踩到)

**症狀:** 使用者在 Vercel 設好 `WATCHLIST_SECRET` / `GITHUB_TOKEN` / `GITHUB_REPO`
(截圖確認三個都在、Production and Preview),網頁按「同步到雲端」仍回
「伺服器未設定 WATCHLIST_SECRET,拒絕寫入」。

**原因:Vercel 的環境變數只對「之後的部署」生效。** 變數是 1 分鐘前加的,
線上跑的還是加變數之前那份部署,函式 `os.environ.get()` 讀到空值。
→ **解法:Deployments 點最新一筆 Redeploy,或推任何一個 commit。**

**做了什麼避免下次再猜:**
- `GET /api/watchlist?check=1` 設定自檢,回 `{env:{各變數 true/false}, ready, hint}`。
  **只回布林,絕不回值**(實測輸出不含任何變數內容)。
- 403 的錯誤訊息直接寫明「環境變數要重新部署才生效」+ 指向自檢端點,
  不要只說「未設定」讓人以為是自己打錯。

## 34.1 同一輪的另一個教訓:改了規則沒重跑資料 = 沒改

第 33 節把 `industry_chain.py` 的 primary 規則改成中位數,但**沒有重新產生
`docs/sector_map.json` 與 `docs/levels.json`** —— 使用者截圖裡旺宏依然是「IC封裝測試」。
程式修好、線上沒變。

**凡是改了「產生資料的規則」,一定要在同一輪重跑對應的產生器並確認輸出。**
重跑後:華邦電→記憶體IC ✓、台達電→電源管理IC ✓、緯創→筆記型電腦 ✓、
仁寶→伺服器、旺宏→晶圓製造(FinMind 分類本身沒有「記憶體」以外更精確的桶,已知限制)。

## 34.2 ⚠️ secret 已在對話中外洩
使用者把 `WATCHLIST_SECRET` 的值貼進聊天視窗。已請他到 Vercel 換一組新值
(只保護自選池、不涉及金錢,但沒有理由留著)。網頁端 localStorage 存的舊值
在第一次同步失敗後會自動清除並重問。

---

## 35. 個股詳情頁:盤中完整分K(2026-07-19)

### 為什麼放這裡而不是卡片上
卡片是**掃視**用的(一次看 5~10 檔),放完整 K 線會比使用者原本抱怨的「字小密集」更糟 ——
所以卡片只放一條走勢線(第 32 節)。詳情頁是**專心看一檔**的地方,值得完整的圖。

### 後端:`/api/detail?stock=2330&intraday=1`

`compute_intraday()` 走 `TaiwanStockKBar`,**一檔一次呼叫**。實測 2330 → 266 根、
2.66 秒、payload 15 KB。回傳 `{bars:[[HH:MM,o,h,l,c,v]...], vwap:[...], prev_close}`。

**刻意做成獨立快路徑,不併進主 detail payload** —— 主 detail 已經要 5~15 秒
(多個 dataset + yfinance),再加一份會更慢,而且**分K只有點開「分K」頁籤時才需要,
沒看就不該付這個成本**。

- `vwap` 是**累計**均價(累計成交額÷累計量),不是移動平均 —— 台股看盤軟體的「均價線」就是這個。
- **當天沒資料會往回找最近 7 天**。不這樣做的話,週末點開分K 永遠空白,使用者會以為壞掉。
- `prev_close` 從本機日線 parquet 取,不另外打 API。

### 前端

日K / 分K 頁籤(`setDetailK`),**點了分K 才 fetch**,結果存 `INTRA_CACHE` 不重抓。
圖含三個系列:蠟燭 + 均價線(accent 色)+ 量,另有**昨收虛線 markLine** ——
沒有它只看得出形狀、看不出今天是紅盤還黑盤。dataZoom 兩種(滾輪 + 底部滑桿)。

### 驗證(本機無 Vercel,以快取灌入實資料)
- 切換:日K↔分K 顯示/隱藏、標題、按鈕 active 狀態皆正確
- 圖形:3 系列各 266 點、X 軸 09:00→13:30、昨收線 yAxis=2470、圖表 926×420、dataZoom 2 組
- 均價線首末 2385 → 2352.42(與快照官方均價 2351.58 差 0.04%)
- **換股自動回到日K**(不會停在上一檔的分K 檢視);**關閉後圖表確實 dispose**(不累積記憶體)
- 錯誤路徑:API 未部署時顯示「API 未部署或無回應(http 404)」而不是
  `Unexpected token '<'`(與 openDetail 用同一套處理)
- 無 console 錯誤

---

## 36. 盤中/盤後全自動化(2026-07-20)

**動機:** 使用者「我 8:50 跟 9:05 來不及用,能否自動執行」。

### 關鍵設計:讓 job 自己等,排程延遲就變成無害

專案原本的慣例是「外部 cron(cron-job.org)→ workflow_dispatch」,因為 GitHub 內建
schedule 會延遲 5~30 分鐘。但那個前提是「job 一啟動就要做事」。

改成 **job 自己 `_sleep_until('09:00')`** 之後,早觸發就等、晚觸發就直接開始 ——
**延遲不再影響結果,於是可以用內建 schedule,使用者零設定。**

`.github/workflows/intraday.yml`:`schedule: "20 0 * * 1-5"`(= 台北 08:20,留 40 分鐘給延遲)
`.github/workflows/chips.yml`(新):`"40 13 * * 1-5"`(= 台北 21:40,分點 21:00 發布後)

### 一條排程做完整天的事

`--start 09:00` 進 `loop()` 後依序:
1. **`_levels_fresh()` 檢查 levels 是不是今天的,不是就自動 `build_levels()`**
   —— 取代原本的 08:50 獨立排程。(跨日的均線/前高會讓突破判斷整個歪掉。)
2. 睡到 09:00
3. 每 20 秒掃描;每 10 分鐘更新內外盤比 + 走勢線
4. **在 7 個檢查點存全市場快照** —— 取代 `snapshot.yml` 的 7 條排程。
   用 `done_tags` 去重,實測模擬一整天輪詢:7 個檢查點各存一次、不重複、
   晚到的輪詢只補最後一個(不會一次補一串)。
5. 13:35 結束

### 驗證
- `_sleep_until` 已過時間 0.00s 返回;未到時間以模擬時鐘測試 → 睡 60s、30s 後在 09:00 返回
- `_levels_fresh()` 正確判定 7/19 建的 levels 對 7/20 而言已過期 → 會自動重建
- 檢查點去重:模擬 10 次不同時間的輪詢 → 恰好存 7 次,每個檢查點最多一次
- 5 個 workflow YAML 全部通過解析

### 仍需外部 cron 的
`daily`(選股主流程)與 `premarket` 維持原狀 —— 它們在乎準時,見 `SETUP_PREMARKET_CRON.md`。

---

## 37. 目前狀態總結(2026-07-20,等待第一次真實盤中驗證)

### 這幾輪做完的東西

| 功能 | 檔案 | 狀態 |
|---|---|---|
| FinMind 產業鏈分類(47/483) | `industry_chain.py` | 上線,primary 用中位數規則 8/10 |
| 全市場即時快照 + 三段降級 | `quotes.py` | 上線,**盤中未驗證** |
| 快照存檔(7 檢查點) | `snapshot_archive.py` | 併進盯盤迴圈 |
| 盤中訊號 + 當日去重 | `intraday_scan.py` | 上線,**盤中未驗證** |
| 走勢線(1分K) | `intraday_scan.intraday_series` | 上線,**盤中未驗證** |
| 內外盤比(逐筆) | `intraday_scan.deep_metrics` | 上線,**盤中未驗證** |
| 分點籌碼 + 連買連賣 | `branch_chips.py` | 上線,已回補 6 日 |
| 即時報價 API | `api/quote.py` | 上線 |
| 自選池雲端同步 | `api/watchlist.py` | **待使用者 Redeploy 後驗證** |
| 詳情頁分K | `api/detail.py?intraday=1` | 上線,**未經 Vercel 實測** |
| 報價卡改版 + 漲跌停 | `docs/index.html` | 上線 |

### ⚠️ 明天(2026-07-20 週一)開盤要看的
1. **上游快照更新頻率** —— `docs/freshness.json`(盯盤啟動自動量 90 秒)。
   若中位間隔遠大於 20 秒,`interval` 應調大;遠小於則可考慮調小。
2. **快照在盤中是不是真的即時** —— 這是整條即時路線的前提,至今沒驗證過。
3. 訊號一天實際觸發幾筆(7/17 模擬是 5 筆);去重是否真的只寄一次。
4. 走勢線 / 內外盤 / 分K 是否有資料。
5. 自選池同步(Redeploy 後 `/api/watchlist?check=1` 應三個 true)。

### 唯一還沒做的功能
**信心分全市場重驗**(見 [[twse-todo-score-revalidation]])—— 純離線分析,不受交易日限制。
目的是分辨「信心分整體有效、只是高分區失去鑑別力」vs「整套就是雜訊」,兩者修法相反。

### 沒有被這些功能改善的事(必須一直記得)
**選股層負超額 −8pp(第 26 節)原封不動。** 這幾輪做的都是執行層與看盤層。
盤中訊號是動能型的,而台帳實測動能 profile 超額 −11.5pp 最差。
做這些的價值是「免費驗證它到底行不行」,不是「它會賺」。

---

## 38. 第一次真實盤中驗證結果(2026-07-20)

### ✅ 核心問題有答案了:**快照是真即時,落後約 7 秒**

實測 12:35–12:39 輪詢:全市場 2851 檔、時間戳眾數落後 **0.1~0.2 分(6~12 秒)**,
逐檔查詢也是 0~6 秒。**即時這條路的前提成立。**

⚠️ 但我一度誤判成「延遲 95 分鐘」—— 因為 `snap['date'].iloc[0]` 是**第一列那檔股票的
最後成交時間**,冷門股可能停在 11:00。全市場有 **1321 種不同時間戳**。
→ 新增 `snapshot_date()` / `snapshot_lag_seconds()` 取**眾數**;
`snapshot_archive` 與 `quotes.market_snapshot_source` 的同一處一併修
(否則網頁會顯示「資料時間 11:00」而其實是即時的)。

### 發現並修掉的 4 個問題

**1. 訊號信裡個股名稱全是 "nan"(37/37)**
`row.get("name") or ""` —— **NaN 在 Python 是 truthy**,擋不掉。
**這個坑 `quotes.py` 已經踩過一次(第 25 節),7/20 又在 `intraday_scan` 踩第二次。**
→ 加 `_f()` / `_s()` 統一轉換,並用本機 `stock_info` 補名稱
(快照 name 欄實測只有 3.2% 有值)。

**2. 12:11 一次寄了 25 封信**
根因:**回檔買點完全沒有量能過濾**。原本的固定門檻 `MIN_VOL_RATIO=1.5` 只用在突破,
而且在 7/20 這種低量日形同虛設 —— 全市場量比中位數只有 **0.65**,
30 筆觸發裡 **27 筆量比 < 1.0、11 筆漲幅 < 0.5%**(等於沒動也沒量)。

→ 改成**相對門檻**:`量比 ≥ 全市場中位數 × 倍數`(突破 1.5、回檔 1.3,絕對下限 0.5),
外加回檔至少要漲 0.5%。低量日自動降門檻、爆量日自動升,同一套規則在不同盤況都成立。
**這與專案既有的「因子驗證要先規模中性化」是同一個原則。**
實測同一份資料:**30 筆 → 8 筆**(6 回檔 + 2 突破),且都是有量有漲幅的。

**3. 冷啟動洪水**
job 若在盤中才啟動(排程延遲/手動觸發/中途重啟),第一輪會把「整個上午累積、
當下仍符合條件」的股票一次全發 —— 那不是 25 個新機會,是 3 小時的存量。
→ `MAX_ALERTS_PER_POLL=8`,取漲幅最強的;**其餘仍寫入去重表**,
否則會在後續輪詢逐筆補寄(等於延遲洗版)。已驗證第 2 輪重複通知為 0。

**4. 每有訊號就 push = 每次觸發一次 Vercel 部署**
7/20 實測 20 分鐘內 8 次 push = 8 次部署。→ 發布節流 3 分鐘(`PUBLISH_EVERY`),
收盤前補推殘留;同時把 `deep.json` / `series.json` / `freshness.json` 也納入發布
(原本只有 alerts,所以 freshness 量測結果整天都沒上去)。

### 使用者端待處理:GitHub token 權限

自選池同步回 `403 Resource not accessible by personal access token` ——
token 有效但**缺 Contents 寫入權**。GitHub 的原文完全沒說要開哪個權限,
已把錯誤訊息改寫成可執行的指示:
- **Fine-grained token**:Repository access 選該 repo →
  Permissions → Repository permissions → **Contents: Read and write**
- **Classic token**:勾 **repo** scope
改好後更新到 Vercel 並 **Redeploy**(環境變數要重新部署才生效,見第 34 節)。

### 仍未驗證
- 修正後的訊號量在**完整一天**(09:00 起跑)是幾筆 —— 今天是 12:10 才啟動的。
- 走勢線 / 內外盤 / 分K 在網頁上的實際呈現(今天 job 啟動晚,且發布節流前只推 alerts)。

---

## 39. 名詞解釋 + 熱力圖即時化 + 委買賣/內外盤正名(2026-07-20 下午)

### 39.1 名詞解釋(使用者要求)

「在每個資訊上都要能讓使用者點下去或是將游標放在上面後,顯示該資訊的詳細意思」。

`TIP` 字典,每則寫**三段:是什麼 / 高代表什麼 / 低代表什麼** ——
只解釋名詞而不講高低意義等於沒幫助,使用者要的是「看到這個數字該怎麼想」。

- 桌機 hover、**手機點一下**(touch 沒有 hover,只做 hover 等於手機完全用不到);點外部或 Esc 關閉。
- 用 `position:absolute` + JS 定位,**不用 CSS ::after** —— 卡片有 overflow,純 CSS tooltip 會被裁掉。
- 空間不夠自動翻到下方;左右夾在畫面內。
- 事件用**委派**綁在 document:卡片每 5 秒重繪,逐一綁定會漏掉新元素又會累積監聽器。
- 內文支援 `**粗體**`:**先 escape 再轉標籤**(順序反了就是 XSS)。已實測 `<img onerror>` 被當文字。
- 已接:報價卡 12 個欄位 + 漲跌停/突破/回測/分點連買 chip + 詳情頁 5 個圖表標題。

### 39.2 熱力圖 / 市場氛圍即時化

使用者:「熱力圖和市場氛圍是否即時?目前看是沒有」—— 正確,它們讀盤後批次的 `heatmap.json`。

`hmLive()` 每 30 秒用 `/api/quote` 覆蓋:漲跌幅直接換;**廣度(站上月線/季線/多頭排列)
用即時價與 `levels.json` 的均線現算** —— 市場氛圍的權重 75% 在廣度,不更新等於整頁都是昨天的。
標籤改成「即時 N 檔・其餘為 YYYY-MM-DD 收盤」讓涵蓋範圍看得見。

**實測:市場氛圍 −31.5(批次)→ −39.7(即時),200 檔被更新,廣度驗算與 levels 一致。**
(目前一次只查 200 檔,是 `/api/quote` 的 `MAX_IDS` 上限;要全 860 檔需分批,尚未做。)

### 39.3 委買/委賣與內外盤:與券商軟體對照後的正名

使用者拿 CMoney 對照,數字對不上。查證結果:

| 欄位 | FinMind 給的 | CMoney 顯示的 | 結論 |
|---|---|---|---|
| 委買/委賣量 | **最佳一檔**的委託量 | **五檔加總**(2034/2879) | 不同指標,**不是錯誤** → 已正名為「買一/賣一」並在說明中註記 |
| 內盤/外盤 | 需逐筆資料 | 內盤46.11%/外盤53.89% | **逐筆資料當天盤中查不到(0 筆),盤後才發布** |

⚠️ **`TaiwanStockPriceTick` 今日資料在盤中回 0 筆,7/17 有 20,922 筆** ——
所以 `deep_metrics` 在盤中永遠算不出內外盤,這就是使用者整天看到「外盤/內盤 —」的原因。
`TaiwanStockStatisticsOfOrderBookAndTrade` 是**全市場**委託統計(不接受 data_id),無法用於個股。

**尚未解決:盤中內外盤。** 可行方向是從快照輪詢自行累計(每輪的 `total_volume` 增量,
依當下 `TickType` 歸給外盤或內盤)—— 是估算值,但可即時。**還沒實作。**

### 39.4 Gmail 容量:採用方案 D(使用者選擇)
不改程式。Gmail 建立過濾器自動清理,見下方使用者說明。

---

## 40. ⚠️ vercel.json 的 ignoreCommand 造成部署失敗(2026-07-20)

**症狀:** 加入 `ignoreCommand` 後,Vercel Production 部署開始失敗
(GitHub 的 checks 顯示 "Deployment has failed")。

**判定依據(⚠️ 是推論,不是從 Vercel 日誌確認的 —— 我讀不到建置日誌):**
- 失敗的 commit `ab6939ef4` **只改了 `docs/index.html` 與 `HANDOFF.md`**(純前端)
- 6 支 `api/*.py` 全部 `ast.parse` 通過
- 同期唯一的結構性改動就是 `vercel.json` 的 `ignoreCommand`

**兩個可能的原因,解法相同:**
1. `ignoreCommand` 不是 `vercel.json` 支援的鍵 → schema 驗證失敗
2. **Vercel 是淺層 clone**,`HEAD^` 不存在 → `git diff HEAD^ HEAD` 出錯

→ **已從 vercel.json 移除。**

### 正確作法:用 Vercel UI 的 Ignored Build Step

Settings → Git → **Ignored Build Step** → Custom,貼:

```bash
if echo "$VERCEL_GIT_COMMIT_MESSAGE" | grep -qE '^(intraday|snapshot|chips|watchlist):'; then exit 0; else exit 1; fi
```

(exit 0 = 略過建置)

**為什麼改用 commit message 比對而不是 git diff:**
機器人的 commit 訊息是固定前綴(`intraday:` / `snapshot:` / `chips:` / `watchlist:`),
比對字串**不需要 git 歷史**,淺層 clone 也能用 —— 這正是 `HEAD^` 那個作法的死穴。

### 教訓
**改動建置設定要單獨一個 commit 推,不要跟功能混在一起。**
這次 `ignoreCommand` 跟「內外盤修正 + 信件改版」同一個 commit 進去,
之後三個 commit 的部署全掛掉,而失敗訊號要到使用者截圖才發現。

---

## 41. 績效台帳即時化 + 心跳可視化 + 輪詢節流(2026-07-21)

使用者三個要求:①盤中到底該跑哪些排程 ②「歷史追蹤與績效」也要即時 ③整站體檢。

### 41.1 台帳即時化(`docs/index.html`)

**問題:** 績效分頁全部來自 `data.json` → 21:30 盤後批次才更新。盤中看「持有中」
那幾檔,`latest_close` 是**昨天的收盤**,報酬率等於落後一整天。

**做法:** 前端疊加,後端不動。
- `ledIsOpen(r)` — 只有 `exit_reason` 為空 / `持有中` / `待隔日進場` 算未出場。
- `ledLive(r)` — 未出場列改用 `LIVE[stock_id].price` 重算 `ret_pct`;
  `peak_gain_pct` 只在即時價**創新高**時才更新(否則沿用批次算的波段高)。
- **已模擬出場的列絕對不碰** —— 那是歷史成交價,重算會竄改台帳、破壞勝率統計。
- `liveIds()` 加入未出場的代號(實測 11 檔),`/api/quote` 是全市場一次呼叫,成本不變。
- `liveQuotes()` 回來時只呼叫 `drawLedger()`,**不重繪整個分頁** —— 整頁重繪會跳掉
  捲動位置、吃掉搜尋框焦點。
- `#ledtable` 頂端加 `ledLiveSummary()`:未出場部位的即時平均報酬 / 獲利檔數 / 檔數,
  並標明「上方勝率與已實現統計仍是批次結果」。
- 即時的列在「最新」欄有 `.led-dot` 綠點(hover 顯示報價時間戳)。

**驗證:** 本機注入模擬報價(每檔 +3%),6505 entry 66.2 → 即時 80.03 → 20.89%(數學正確),
已出場列數值不變,綠點 11 個。`node --check` 通過。

### 41.2 盯盤心跳搬到網頁上(`heartbeatBox()`)

`docs/freshness.json` 的 `heartbeat` 欄位本來只能去 GitHub 上開檔案看。
網頁上「今日尚無盤中訊號」和「job 根本沒跑」長得一模一樣 —— 這正是 7/20、7/21
兩天沒發現內建 schedule 沒觸發的原因。

現在「盤中即時」分頁頂端固定顯示:
- 心跳是今天且 10 分鐘內 → 綠點「盯盤程式運行中 · 已掃 N 輪 · 今日觸發 M 則」
- 心跳超過 10 分鐘 → ⚠️ 提示可能已結束或卡住
- **盤中時段卻完全沒有今日心跳 → 紅框警告 + 直接寫出補救步驟**(去 Actions 手動 run)
- 非盤中時段不顯示(不誤報)

⚠️ `freshness.json` 目前 repo 裡還沒有 —— 心跳寫入是上一個 commit(14268c4e8)才加的,
還沒真的跑過一次盤中。**第一個交易日跑完要確認這個檔案有被 commit 上去。**

### 41.3 即時報價輪詢節流

原本 `startLiveQuotes(20)` 一載入就每 20 秒打一次 `/api/quote`,**24 小時不停**,
分頁切到背景、半夜、週末照打。收盤後價格根本不會變,那些呼叫是純浪費
(Vercel 函式呼叫數 + FinMind 額度)。

現在 `tick()` 開頭兩道閘:`document.hidden` 就跳過;非盤中時段(台北週一~五 09:00–13:35)
且已經取得過一次報價就跳過。切回前景時 `visibilitychange` 立刻補一次,不會看到過期價格。
粗估省掉八成以上的呼叫。

### 41.4 體檢時發現、**尚未處理**的問題

1. **`docs/_qall.json`(384 KB)是誤 commit 的除錯 dump。**
   它在 14268c4e8 被加進來,但 repo 裡**沒有任何程式寫它、也沒有任何程式讀它**
   (`grep -rn _qall scripts/ api/ docs/index.html` 全空),內容是全市場報價快照。
   → 建議 `git rm docs/_qall.json`。留著每次 clone 都多背 384 KB。

2. **`data/snapshots/` 目前是空的(目錄不存在)。**
   盯盤迴圈的 7 個檢查點快照只在 **job 結束時**由 workflow 的最後一步 commit,
   迴圈內的 `_git_publish()` 並沒有 `git add data/snapshots`。
   → job 被取消 / timeout / runner 掛掉,**整天的快照就永遠沒了**(鐵則三說這種資料
   訂閱到期後補不回來)。建議把 `data/snapshots` 加進 `_git_publish` 的 add 清單,
   存一個就推一個。

3. **`docs/levels.json` 同樣沒在 `_git_publish` 的清單裡**(只在 workflow 最後一步),
   所以盤中自動重建的 levels 要等 job 結束網頁才拿得到。


---

## 42. FinMind 文件通讀 + 盤中大盤即時 + 取樣頻率修正(2026-07-21)

讀了 https://finmind.github.io/tutor/TaiwanMarket/RealTime/ 與資料集總覽,對照本專案現況。

### 42.1 三個舊坑已修(第 41.4 節列的)

1. **`docs/_qall.json`(384 KB)已 `git rm`** —— 誤 commit 的除錯 dump,無人寫也無人讀。
2. **`data/snapshots` 已加進 `_git_publish()`**,並且**存完快照就把節流計時器歸零**
   (`_pub_last[0] = 0.0`)讓它當場推上去。快照是鐵則三說的「不可重建資料」,
   不能讓它在 runner 磁碟上等 3 分鐘節流窗。
3. **`docs/levels.json` 也加進 `_git_publish()`** —— 盤中重建的 levels 不推上去,
   網頁整天拿不到產業別/換手率/突破判斷。

### 42.2 取樣頻率:20 秒 → 10 秒

FinMind 官方文件寫明 `taiwan_stock_tick_snapshot`「**約 10 秒更新一次**」,
本專案 `measure_freshness` 實測中位間隔約 11 秒 —— **兩邊對得上**。
原本 `intraday.yml` 預設 interval=20,等於只取樣一半,訊號白白晚 10 秒。

- `intraday.yml` interval 預設 **20 → 10**。額度:4.5 小時約 1620 次,上限 6000/hr,用不到 7%。
- `quotes._DEFAULT_TTL` **20 → 10**。原本網頁最壞拿到「20 秒快取 + 上游 10 秒」= 落後 30 秒;
  設 10 之後快取永遠不會比上游舊,而且**不會多打 API**(前端輪詢間隔比它長)。

**目前各層的更新頻率(2026-07-21 起):**

| 層 | 頻率 | 備註 |
|---|---|---|
| FinMind 上游快照 | ~10 秒 | 官方文件;實測時間戳落後真實時間 6~12 秒 |
| 盯盤掃描 | **10 秒** | 訊號 Email 在這個節奏上發出 |
| 網頁報價輪詢 `/api/quote` | 20 秒 | 分頁隱藏 / 非盤中會跳過(§41.3) |
| 走勢線 / 內外盤 | 10 分鐘 | 逐筆資料太重 |
| git push 發布 | 最快 3 分鐘 | 每次 push = 一次 Vercel 部署;快照存檔會插隊 |
| GitHub raw CDN | ~5 分鐘 | 網頁看訊號的實際下限 —— **Email 才是即時管道** |

### 42.3 新增:盤中大盤即時(`docs/pulse.json`)

**在這之前盤中完全沒有大盤資訊** —— 市場氛圍/位階全來自 21:30 盤後批次,盤中看的是昨天的。
而台帳早就顯示動能是 beta 不是 alpha,盤中最該先看的其實是大盤方向與廣度。

**零額外 API 呼叫** —— 全市場快照本來每輪就抓回來了:
- 指數:FinMind 文件寫明 `data_id` 除 4 碼個股外也支援 **91 個 3 碼指數代號**
  (`001`=加權指數、`101`=櫃買加權),不帶 data_id 的全市場回應裡本來就該有這幾列。
- 廣度:漲/跌/平家數、漲逾 5%、跌逾 5%、量比中位數、成交金額 —— 同一份 DataFrame 直接算。

網頁「盤中即時」分頁頂端新增大盤條。**左緣色帶用「上漲家數占比」而不是指數漲跌** ——
權值股拉指數但多數股票在跌是很常見的陷阱,只看指數會誤判(>55% 綠 / <45% 紅)。

⚠️ **指數列是否真的出現在「不帶 data_id」的回應裡,尚未實測**(收盤後寫的,本機無 token)。
沒有的話 `indices` 為空、廣度照常顯示,不會壞,但會 log 一次警告。
真的沒有就改接免費的 `TaiwanVariousIndicators5Seconds`(加權指數 5 秒級,**免 Sponsor**,
而且訂閱到期後還能用 —— 以降級韌性來說其實比指數列更好)。

**離線驗證過**:餵合成快照(2850 檔 + 001/101)→ 指數與廣度數字正確;
拿掉指數列 → `indices` 空、廣度仍在;空 DataFrame → 不炸。前端排序、降級、
昨日資料不冒充今日、色帶門檻皆已在瀏覽器實測。

⚠️ **前端踩到的雷**:指數用 `Object.keys(ix)` 迭代會讓**櫃買排在加權前面** ——
`'101'` 是合法陣列索引會被 JS 提前,`'001'` 有前導零不是。順序要寫死。

### 42.4 ⚠️ 疑似坑:TaiwanStockKBar 可能盤中不更新

FinMind 資料集總覽把 **`TaiwanStockKBar` 的「更新時間」列為平日 15:50**(收盤後)。
本專案的盤中走勢線(`intraday_series`)正是用它。若真是收盤後才更新,
**盤中的走勢線會整條停在前一天,而網頁上完全看不出來**(只會覺得線很短)。

專案記憶也記著「走勢線/內外盤/分K 在網頁的實際呈現還沒看到」—— 從沒驗證過。

→ 已加**自我驗證**:`intraday_series` 算出最後一根 K 與當下的落差,
超過 15 分鐘就 log 警告,並把 `lag_min` 寫進 `series.json`。
**第一個交易日跑完先看這個欄位**。若確認不更新,走勢線要改成自己累積快照
(缺點是 job 晚開就缺一段,但總比整條是昨天的好)。

### 42.5 還沒動的

- `_write_web()`(alerts.json)只在**有新訊號時**才呼叫,所以 `updated` 欄位在沒訊號的
  時段不會前進。心跳已能分辨 job 死活,所以不急,但這個欄位目前會誤導。
- `TaiwanStockEvery5SecondsIndex`(產業別指數 5 秒級,Backer/Sponsor)沒接 ——
  可以做「盤中最強族群」,但更新時間也寫 17:30,同樣要先驗。


---

## 43. 通知改推 Discord(2026-07-21)

使用者:「我不想用 Email 通知了」。評估 WhatsApp / Telegram / LINE / Discord 後選 **Discord**
(使用者本來就每天開著)。**盤後長報告(daily.yml)維持 Email** —— 多欄 HTML 表格在信箱的
可讀性遠勝聊天視窗,這是使用者自己選的。

### 43.1 為什麼不是 WhatsApp(使用者原本問的)

- Cloud API 要 **Meta business verification**(稅籍/公司登記文件,2~10 工作天)—— 個人散戶難過。
- **主動推的訊息按則計費**(2025/7/1 起 per-message;utility 約 USD 0.004~0.0456/則),
  且**必須用事先核准的範本**,變數只能填空格 —— 我們的訊號內容全是變動文字,塞不進去。
- 有免費路(使用者先發訊息開 24 小時視窗,視窗內可自由格式免費回),但忘記那天就靜默失效。
  對盯盤系統來說太脆。

Discord/Telegram 都免費免驗證。Discord 勝出的點:webhook 設定更簡單(只有一個 URL,
沒有 token 生命週期)、embed 排版更好、**PATCH 可以就地編輯已發出的訊息**、頻道可分流。

### 43.2 ⚠️ 最重要的設計限制:**編輯訊息不會推播**

原本的構想是「整天只維護一則就地更新的訊息 → 零洗版」。實作時才確認:
Discord 的 PATCH 會更新畫面但**不發手機通知** —— 那等於訊號全部靜音,對盯盤是致命的。

→ 改成**合併視窗**(`COALESCE_S = 150` 秒):
- 新訊號距上一則訊息超過 150 秒 → **發新訊息**(推播,這就是通知本身)
- 在視窗內 → **PATCH 上一則**把新的併進去(不推播,但你剛剛才被通知過)

這樣既保住通知,又不會 12:31/12:32/12:33 連噴三則。

⚠️ **合併時刻意不更新 `msg_ts`** —— 否則連續有訊號時視窗會無限往後延,
變成整個下午都在編輯同一則、完全不再推播。

也刻意**不做「今日總覽」常駐訊息** —— 全天清單網頁已經有(docs/alerts.json),
在 Discord 再維護一份只會兩處不一致。

### 43.3 只用 webhook,不做 bot

webhook = 一個 URL + POST JSON。沒有 token 生命週期、沒有簽章驗證、沒有 3 秒回應死線。
代價:**webhook 可以「顯示」按鈕但收不到點擊**,所以目前只發**連結型按鈕**(style 5)。

**「我買了/我跳過」按鈕是階段二**,要接 Interactions Endpoint + **Ed25519 驗簽**,
而且 Discord 會定期發假簽章測試,驗不過會直接拔掉 endpoint 並寄信警告。
值不值得那個複雜度再說 —— 但那是目前系統最大的量測盲區(人工挑選是加分還是扣分),
而且能把「買/跳」標記從 localStorage(換裝置就沒了)搬到 repo。

### 43.4 檔案地圖

- `scripts/notify.py` — 新增 `send_discord()` / `edit_discord()` / `link_buttons()` /
  `discord_enabled()`。⚠️ `send_discord` 的 URL **一定要帶 `?wait=true`**,
  否則 Discord 回 204 空 body、拿不到 message_id,整個就地編輯的設計就垮了。
  發 components 時還要帶 `with_components=true`,否則按鈕會被**靜默忽略**。
- `scripts/intraday_scan.py` — `_discord_notify()` + `_embed()`;`_notify()` 改成
  「先試 Discord,失敗才寄 Email」。訊息狀態存 `data/alerts/state-{day}.json`
  ⚠️ **不能塞進 `alerts/{day}.json`** —— 那是 `{訊號key: 紀錄}` 扁平 dict,
  `_write_web` 會直接迭代 `.values()`,混一個 meta 進去會變成一張壞掉的卡片。
- `scripts/premarket.py` — `_discord_preopen()` / `_discord_orb()`,同樣 Discord 優先、
  Email 當退路。這兩份是一天一次的定時報告,**不做合併視窗也不編輯**,本來就該推播。
- `scripts/config.py` — `DISCORD_WEBHOOK_URL`(沒設就整套自動退回 Email)。
- `intraday.yml` / `premarket.yml` — 加 `DISCORD_WEBHOOK_URL` secret。

### 43.5 使用者要做的事(一次性,約 3 分鐘)

1. Discord 開一個伺服器(或用現有的)→ 建一個頻道,例如 `#台股訊號`
2. 頻道右鍵 → **編輯頻道** → **整合** → **Webhook** → **新增 Webhook** → **複製 Webhook 網址**
3. GitHub → repo → Settings → Secrets and variables → Actions → **New repository secret**
   - Name: `DISCORD_WEBHOOK_URL`
   - Secret: 剛剛複製的網址
4. 驗證(**收盤也能跑**):Actions → Intraday Signal Scan → Run workflow → mode 選 `scan`
   —— 或本機 `DISCORD_WEBHOOK_URL=... python -m scripts.intraday_scan --test-discord`,
   會發一則假訊號卡到頻道。收到就是通了。

> ⚠️ Webhook 網址等同「知道就能在你頻道發文」的密鑰,**只放 GitHub secret,不要進 repo**。
> 另外記得確認該頻道**沒有被靜音**,否則手機不會響 —— Discord 是逐頻道設定的。

### 43.6 離線驗證過的路徑

用假的 requests 打樁跑過五條路:①第一批發新訊息(POST + `wait=true`)②視窗內併入
(PATCH 到 `/messages/{id}`,卡數累加)③視窗過期重發新訊息(卡數重置)
④訊息被手動刪除時 PATCH 收 404 → 自動改發新的並更新 msg_id ⑤沒設 webhook 回 False
讓呼叫端退回 Email。premarket 的兩種 embed 也都渲染驗證過。

**都還沒對真實 Discord 端點跑過** —— 使用者設好 secret 後用 `--test-discord` 確認。


---

## 44. Discord 通知附上個股背景 + 日K圖(2026-07-21)

使用者看到第一批真實 Discord 通知後反映:只有代號、價格、觸發理由 ——
「這是誰、在做什麼、基本面如何、最近有什麼新聞」全都得自己再查一次。

### 44.1 補了什麼、成本多少

| 內容 | 來源 | FinMind 額度 |
|---|---|---|
| 產業 / 細產業(**在做什麼**) | `docs/sector_map.json`(產業鏈,盤後算好) | **0** |
| 市值 / 均線 / 20日高 | `docs/levels.json`(盤前建好) | **0** |
| EPS(近四季)/ 月營收年增 | `levels.json` | **0** |
| 本益比 / 股價淨值比 / 殖利率 | `TaiwanStockPER` 現抓 | 1 次/檔 |
| 近 14 日新聞(3 則,標題可點) | Google News RSS | **0**(不吃 FinMind) |
| 日K 60 根 + MA5/20/60 + 量 | 本機 `data/prices/*.parquet` | **0** |

一天約 20 檔 = 20 次呼叫,對照 6000/hr 可忽略。

新檔:`scripts/alert_enrich.py`(背景資料)、`scripts/alert_chart.py`(日K圖)。
`requirements.txt` 加 `matplotlib`。

### 44.2 圖片一律走**附件上傳**,不要用圖床

第一直覺是「把 PNG commit 進 repo,embed 引用 raw.githubusercontent」——
**那是錯的**:raw 有約 5 分鐘 CDN 快取,訊號都過期了圖才出現。
改成隨訊息 multipart 上傳,embed 用 `attachment://<檔名>` 引用。
沒有 hosting、沒有快取延遲。

編輯(合併視窗)時要帶 `attachments` 陣列宣告「這則訊息現在有哪些附件」;
只列新的 = 舊的被移除,正好對應「整批重畫」的語意。所以 PATCH 時整批的圖會重傳。

### 44.3 ⚠️ 效能:補資料**一定要平行跑**

序列跑 8 檔實測 **7.5 秒**(每檔一次 Google News + 一次估值),
而輪詢間隔只有 10 秒 —— 一個批次就吃掉一整輪。
改用 `ThreadPoolExecutor` 後降到 **2.3 秒**。純 I/O 等待,GIL 不影響。

另外:
- **只給最強的前 `CHART_TOP=4` 檔畫圖**。一張圖約 0.3~0.6 秒、40 KB,
  8 張就是 5 秒 —— 瓶頸是時間不是 Discord 的 8 MB 附件上限。
- `alert_enrich._CACHE` 以「當天+代號」為 key。同一檔先觸發突破、稍後又觸發回檔買點
  不會重抓;實測第二次組卡從 2.3 秒降到 1.0 秒(只剩畫圖)。

### 44.4 踩到的雷

- **CI 沒有中文字型。** matplotlib 預設 DejaVu Sans 不含中日韓字,圖上寫中文會變一排
  豆腐方塊(本機實測「20日高」就是)。→ **圖上的文字一律只用英數**(`20D High`),
  中文資訊放在 embed 的文字欄位裡。
- **FinMind 細產業名稱本身可能是一長串列舉。** 鴻海的細產業叫
  「印表機、傳真機、掃瞄器、多功能事務機、投影機」,整串貼進通知會洗掉版面。
  → `_short()` 取第一項 +「等」,並限長 16 字。
- **parquet 最新只到昨天**(盤後批次才寫),所以日K圖預設看不到剛觸發的那一根 ——
  而那正是使用者最想看的。→ 用即時價現補今天這一根上去。
- KY 外國企業(如 6933 AMAX-KY)不在 FinMind 產業鏈裡、levels 的 TWSE 產業別也常缺,
  `business` 會是空字串。這是**預期行為**,卡片只是少一行,不要為此硬塞假分類。

### 44.5 驗證狀況

離線驗證:8 檔完整組卡(產業/估值/基本面/新聞/圖)8/8 有新聞與產業敘述、
4 張圖共 136 KB、POST 帶 2 個附件且 `attachments` 欄位正確、PATCH 重傳 3 張並指向原訊息、
缺檔股票(9999)回 None 不炸。日K圖已目視確認(20D High 虛線 + 當日突破那根)。

**估值那段是打樁測的**(本機無 FINMIND_TOKEN),`TaiwanStockPER` 的實際欄位名
(`PER`/`PBR`/`dividend_yield`)還沒對真實回應驗過 —— 第一個交易日看卡片上有沒有「估值」那行。


---

## 45. ⚠️ Discord 400:embed 總字數上限 6000(2026-07-21)

第一批帶背景資料的通知**全部退回 Email**。log 只有一行:

```
Discord 發送失敗:400 Client Error: Bad Request for url: ***?wait=true&with_components=true
```

### 根因

**Discord 一則訊息裡所有 embed 的文字加總不能超過 6000 字。** 實測 8 張卡 = **8583 字**。

真正的元兇是**新聞的超連結**:Google News RSS 的 `link` 是 base64 轉址網址,
**一條就約 380 字**。三條新聞光網址就 1150 字,乘以 8 張卡 = 9200 字 —— 卡片上看起來
只有三行標題,實際上超標的是看不見的網址。

### 修法

1. **新聞標題不再做超連結**,改成純標題 + 一條短的「更多 <代號> 新聞 →」搜尋連結。
   標題本來就是拿來掃的,想看內文再點。**8583 → 3540 字。**
2. `notify.fit_embeds()` 當最後一道保險:超過 `MAX_TOTAL_CHARS=5500`(對 6000 留餘裕)
   時**先砍新聞欄位、由後往前**(前面的卡是最強的訊號),真的還是超標才整張卡不送。
   驗證:9 張刻意灌大的卡 8253 字 → 壓到 5213 字,卡數全保留、現價欄全保留、
   後 5 張的新聞被砍。卡片被砍時對應的圖也不上傳。

### ⚠️ 這次真正的教訓:**沒印 response body**

`raise_for_status()` 只給「400 Bad Request」,而 **Discord 的回應 body 會明講是哪個欄位超限**。
沒有那行就只能猜。已在 `_post` 加 `log.warning(f"Discord {method} {r.status_code}:{r.text[:600]}")`。
**日後接任何 HTTP API,錯誤處理一律要印回應內容,不要只印狀態碼。**

另外 `_discord_notify` 送失敗時原本只是 `return False` 靜靜改寄 Email ——
現在會先 log 一行,不然「怎麼又變成 Email」得去翻 Actions。

### 45.1 怎麼測 Discord(使用者問的)

**webhook 密鑰只存在 GitHub secret,本機沒有** —— 所以必須有一個能從 Actions 點的入口。
之前只加了 CLI 的 `--test-discord` 卻沒接到 workflow,等於叫使用者用他沒有的東西測。已補:

**Actions → Intraday Signal Scan → Run workflow → mode 選 `test-discord`**

會走**完整的**卡片組裝路徑(產業/估值/基本面/新聞 + 日K圖)發一張 2330 的測試卡,
**收盤、假日都能跑**。收到就是通了;沒收到就看 log 裡 `Discord POST 4xx:` 那行。


### 45.2 首次真實 Discord 測試卡結果(2026-07-21 14:41)

**通道與版面全部正常。** 已確認的項目:

- ✅ **估值欄位可用** —— 這是 §44.5 列為「尚未對真實回應驗過」的那項。
  `TaiwanStockPER` 的 `PER`/`PBR`/`dividend_yield` 欄位名正確
  (實際顯示:本益比 31.2 · 股價淨值比 10.21 · 殖利率 0.95%)。
- ✅ 產業鏈「半導體 · 晶圓製造」、基本面(EPS 74.39 · 月營收年增 +67.9% · 市值)、
  近 14 日新聞 3 則 + 短連結、日K附件、連結按鈕 —— 全部正常顯示。
- ✅ 6000 字上限問題已解決(不再退回 Email)。

**發現一個真的 bug:日K圖多出一根假崩盤 K 棒。**

測試卡寫死 `price=1125`,但本機 2330 昨收是 2320 —— 補「今日K棒」的邏輯照單全收,
畫出一根從 2320 直插 1125 的長黑,整張圖的比例尺全毀。

→ 兩處都修了:
1. `alert_chart` 補今日K棒前先檢查合理性:**台股有 ±10% 漲跌幅限制**,
   偏離昨收超過 `LIMIT_UP_GUARD=11%` 就一定是壞資料(測試假價/對錯股票/未還原價),
   **不補這根並記 log**。寧可少畫今天,也不要畫一根騙人的。
   這在正式盤中也是有效的防線 —— 上游若給了未還原價會被擋下來。
2. 測試卡的價位改成**由 `levels.json` 的真實昨收推導**(昨收 × 1.0321),
   不再寫死。測試卡要長得像真的訊號才有測試價值。

**順帶查證(不是 bug):** 卡片顯示市值 29.17 兆是用測試假價 1125 算的;
用真實昨收 2320 算是 60.16 兆。對照 TWII 42,671 推估全市場約 140 兆,
台積電佔約 43% —— 偏高但與這份資料集自洽,不是計算錯誤。


---

## 46. 信心分全市場重驗 —— 結果與結論(2026-07-21)

欠使用者很久的那件事。工具:`scripts/score_validate.py`
(`python -m scripts.score_validate --every 5`,約 6 分鐘,純離線零 API)。

**樣本:** 2021-08-25 → 2026-07-16、238 個取樣交易日、**165,308 檔·日**(含 2022 空頭)。

### 46.1 為什麼要重寫一支而不是用 backtest

`data/signals/*.json` **只存核心 + 觀察 + 自選**,`scored_count: 866` 只是個數字 ——
那 866 檔的分數沒有留下來。所以必須**重放評分**。
`backtest._replay` 已有現成的因果切片引擎,但它只留 `trigger=True` 且進前 10 名的;
這支**完全不過濾**,每天所有能評分的股票全記錄。

### 46.2 ⚠️ 兩個把我差點導向錯誤結論的陷阱

1. **冒煙測試嚴重誤導。** 400 檔 / 近 2.5 個月(多頭)跑出 D10−D1 = **+2.13%**,
   看起來信心分很有效。**全期只有 +0.27%。** 那 2.13% 是多頭產物。
   → 任何因子驗證都不能只看近期切片,一定要含 2022 空頭。
2. **用 TWII 當對照會讓十組全變負的,那不是選股爛。** TWII 是**市值加權**、被權值股
   主導;這裡是「每檔一票」的等權平均。大盤靠少數大型股拉抬時,平均個股本來就輸指數,
   整張表被這個常數往下平移。
   → **量排序能力必須對照「當日全體評分股的等權平均」**(各組減完總和為零,
   剩下純橫斷面高低差)。TWII 版保留,但那回答的是另一個問題(能不能贏大盤)。
   → 同理「強盤/弱盤」那張表**不能照字面讀**,弱盤看起來好只是 TWII 跌更兇的假象。

### 46.3 結果(對照當日等權平均)

| 組 | 平均分 | 1日 | 3日 | 5日 | 10日 |
|---|---|---|---|---|---|
| D1 | 5.8 | +0.00% | −0.02% | −0.13% | **−0.34%** |
| D6 | 24.4 | +0.01% | +0.06% | +0.10% | **+0.21%** |
| D10 | 59.5 | +0.09% | +0.15% | +0.14% | **+0.27%** |

* 單調性 Spearman:0.248 / 0.37 / **0.758** / **0.903**
* **D10 − D1 = +0.61%(10日)**
* **D10 − D6 = +0.07%(10日)**

### 46.4 結論:**世界 A,但比預期弱得多,而且處方要修正**

1. **不是雜訊(排除世界 B)。** Spearman 0.76(5日)/0.90(10日),各天期 D10−D1 皆為正。
   評分邏輯不必整套換掉。
2. **但 89% 的鑑別力在下半部。** D1→D6 = **+0.547pp**,D6→D10 = **+0.067pp**。
   **中位以上完全沒有排序能力** —— 這正是「停止拿它排序」的直接證據。
3. **⚠️ 整個 D10−D1(0.614%)小於一趟來回交易成本(0.671%)。**
   就算能完美做多 D10、做空 D1,10 天賺的還不夠付一次手續費+證交稅+滑價。
4. **⚠️ 最頂端在較長天期是負的(新發現)。** 99~100 百分位:
   1日 **+0.39%**、3日 **+0.41%**、10日 **−0.21%**。
   **最高分那群是短命的動能爆發,10 天內回吐。**
   而線上核心平均分 73.1 正落在這一段 —— 選股排序**主動把人推進這個區域**。
5. **這完整解釋了台帳的 −0.056。** 核心選股全擠在前 1~5%,那是平的那一段;
   restriction of range 之外,那個區域本來就沒有梯度可測。

### 46.5 由此得到的可行動方向(尚未實作)

* **排序權重再怎麼調都是白工** —— D6 以上是平的。ABC 那輪(§43 之前)得到
  「三者相乘 +1.57pp 但超額全負」的天花板,現在有了機制解釋。
* **持有期嚴重不匹配。** edge 集中在 1~3 日,10 日就沒了,而 `exit.max_hold_days = 30`。
  這與 memory/twse-exit-rule-finding 的「出場在自傷」是同一件事的兩面。
* **一個可測的假設:stage-2 重排可能是有害的。** 本次重放(**純技術層**)的
  「線上核心前10」第 1 日是 **+0.28%**,但真實線上台帳第 1 日是 **−0.52pp**。
  差別就是 stage-2 的籌碼/財報/催化劑/產業加成(加上樣本期不同)。**值得單獨驗。**
* `min_score` 目前是 5(等於沒啟用,與 memory/twse-abc-shipped-and-ceiling 一致),
  所以線上篩選其實只有 `trigger=True` + 依分數取前 10。**真正把人推向頂端的是排序那一步。**

### 46.6 誠實的邊界

* **重疊樣本**:已用 `--every 5` 讓 1/3/5 日樣本接近獨立,但 10 日仍有部分重疊。
  未算 p 值 —— 方向可信,信心區間不可信。
* **未做 beta 中性化**:等權平均對照移除了大盤方向,但沒有控制個股 beta 差異。
* **倖存者偏誤**:universe = 今天還在的 1979 檔 parquet。
* **純技術層**:不含 stage-2 加成(無 point-in-time 歷史快照),與 backtest 同一邊界。


---

## 47. CMoney 類股熱力圖 + 盤中發布真兇(git add 原子性)+ 盤前雙送(2026-07-22)

### 47.1 熱力圖/市場氛圍改用 CMoney 產業類股(§46 之外的獨立工作)

使用者自抓 CMoney「產業類股總覽」(87 類股 / 6 大分類 / 一檔一類 / 掃描池 100%),
取代 FinMind 產業鏈。**使用者選「只用 87 類股一層,不要細產業」。**
- `data/cmoney_categories.json`(committed 來源)、`scripts/sector_cmoney.py`(builder)、
  main.py 改 import、前端 treemap 收合單層 + 隱藏細產業鈕。
- ⚠️「其他」跨 4 大分類 170 檔 → 大分類消歧義,避免併成巨桶。
- 已瀏覽器驗證、上線 7c7d1e942。細節見 memory/twse-heatmap-cmoney。

### 47.2 ⚠️⚠️ 盤中整天沒發布的真兇:`git add` 是原子的

**症狀:** 7/22(及回溯 7/20 加 freshness、7/21 加 snapshots 後)盤中 watch 掃描/Discord/
快照存檔全部正常,但 `docs/*.json` 整天推不上 main、網頁停在昨天。**而且一行錯誤都沒有。**

**兩層原因:**
1. **真兇:`git add A B C` 只要有任一 pathspec 對不到檔案就整批 fatal(rc=128)、
   一個檔都不 stage。** `data/snapshots` 在當日第一個檢查點(1200)前不存在、
   `docs/deep.json`/`series.json` 在第一次內外盤計算前不存在 → git add 全批失敗 →
   diff 空 → 靜默 early-return。**這個坑在 7/21 把 `data/snapshots` 加進清單那刻埋下**
   (7/20 能發是因為當時清單裡每個路徑都存在)。
2. **為什麼查了很久:** 我把 `_git_publish` 的 git 輸出 `capture_output` 起來、只在
   失敗時 log —— 結果連 rc=128 都被吞掉,完全無法診斷。**與 Discord 400 同一個教訓
   (§45.2):接任何外部命令/API,錯誤處理一律要把輸出印出來,不要只看有沒有例外。**

**修法(已上線 f45956c6e,並在 CI 上用 test-publish 驗證「發布成功」):**
- `_git_publish` 每輪先 `Path.exists()` 過濾掉不存在的路徑再 add;每一步 returncode+stderr
  都主動 log;成功印「發布成功」。
- workflow「Commit alerts」端步驟同樣的原子坑 → 改逐一 `[ -e ] && git add`。
- 發布邏輯本身(§46 的 fetch→reset --mixed→push)是對的,錯的是餵給 git add 的清單。

**診斷工具(留著):** `python -m scripts.intraday_scan --test-publish`(或 workflow
mode=test-publish)—— 寫個無害 heartbeat 到 freshness.json、跑一次 _git_publish,
**休市也能觸發**,直接看發布卡在哪一步。這次就是它一跑就抓到 rc=128。

**還原:** rebase 卡死那條(§43/§46 講的 pull --rebase 會 wedge)也一併解決了 ——
新版沒有 rebase。兩個問題(rebase wedge + git add 原子)是不同的;後者才是 7/22 的主因。

### 47.3 盤前快報 + ORB 改雙送(Email + Discord)

使用者要求。與盤中高頻訊號不同,盤前一天一次,兩邊並存不洗版 → 不再是「Discord 失敗
才退 Email」,而是**兩邊都送**。見 premarket.py run_preopen / run_orb。

### 47.4 今日(7/22)實際損失與狀態

- ✅ Discord 盤中訊號**有正常送達**(9 筆),使用者有收到。
- ✅ 快照存檔在 runner 磁碟(1200/1300),但因發布壞掉 + 我 cancel 了 salvage run →
  **今日快照沒進 repo**(可接受,早上本來就是 write-off)。
- ✅ 發布管道已修好並 CI 驗證;**明天 08:20 watch 會正常發布**(freshness heartbeat
  也終於會上線,網頁的心跳框才有資料)。
- levels.json 之前停在 07-19,明天正常發布後會恢復每日更新。


---

## 48. 技術訊號標籤 + K線型態辨識(2026-07-23)

與使用者討論後定案(研究過程見 memory/twse-pattern-recognition-research):
**只做 ① 指標訊號標籤 + ② TA-Lib K線型態;空方訊號與多方同等對待;圖表形態
(頭肩/雙底/三角)擱置;任何標籤都不進信心分** —— 先顯示+記錄,累積後用
score_validate 框架驗過才談。定位是幫人工混合型使用者省開圖時間,不是預測
(大樣本回測:型態單獨使用無統計 edge)。

### 檔案地圖
- `scripts/tech_signals.py` — 核心模組:
  - `indicator_tags(ind)`:布林(收斂/帶量突破上軌/沿上軌行走/跌破下軌)、KD(低檔金叉/
    高檔死叉/鈍化)、MACD 柱轉正負、均線(糾結/轉多頭排列/跌破月線季線)、量價
    (爆量長紅/長黑/長上影)、RSI 極端。**全部確定性規則**,布林規則依據文獻寫在檔頭
    (使用者剛學布林,規則由研究定:無量突破=雜訊所以不標、band walk=趨勢確認不是反轉)。
  - `candle_patterns(raw)`:TA-Lib 61 種全掃。**呈現**只用 CONSENSUS 24 種中文名;
    **記錄**全部(p_all,precision 格式 `+3INSIDE`/`-HIKKAKE`)供日後驗證。
    ⚠️ `SIDE_OVERRIDE`:TA-Lib 對十字線家族永遠回 +100(不判斷方向),照 sign 直翻
    會把墓碑十字標成偏多 —— 已按教科書慣例覆寫。
  - 型態用**原始 OHLC**(真實成交價形狀),指標用還原價指標(與全系統一致)。
- `main.py` — 評分迴圈內對每檔 scored 算 `tags_for(df, df_ind)` → 寫
  `docs/tech_tags.json` {date, tags:{sid:{i,p,p_all}}}(單一事實來源,~135KB,
  實測 1022 檔 26 秒)。TA-Lib 沒裝會自動略過不炸批次。
- `requirements.txt` — 加 `TA-Lib>=0.5`(0.5.0 起 PyPI 有預編 wheels,免裝 C 庫)。
- `alert_enrich.py` — `_load_static` 改回 3-tuple(levels, sector, tech);enrich 帶
  `tech_i`/`tech_p`。Discord 卡新增「技術狀態(昨收)」欄位(▲▼◆ 前綴)。
- `docs/index.html` — `techChips(id)` 讀 tech_tags.json,插在 `qBody` 尾端
  → **自選池/核心/觀察/盤中即時四處共用**。hover 顯示規則細節,標明「昨收基準」。

### 重要語意
- **所有標籤描述「最後一根完成日K」**。盤中看到 = 昨收狀態,呈現端都標了「昨收」。
- `p_all` 現在沒人看,但它就是日後做「台股版型態統計」的原始資料 —— 每天全市場
  61 種型態的觸發記錄,累積在 git 歷史裡,成本趨近零。
- 已生成第一份 docs/tech_tags.json(基準日 07-21 本機資料,949/1022 檔有標籤);
  明天 21:30 批次會用當日收盤覆蓋。

### 驗證
離線:5 檔實測(萬海抓到帶量突破上軌+爆量長紅,正好對應 7/21 真實訊號;威強電晨星)。
瀏覽器:自選池卡片 chips + tooltip 正常。Discord embed:技術狀態欄位正常。
**未驗:CI 上 TA-Lib wheels 安裝(理論上直接裝,看明天 21:30 批次)。**


---

## 49. Stage-2 重排驗證 —— 結案(2026-07-23)

工具:`scripts/stage2_validate.py`(檢驗1分項/檢驗2排序位移/檢驗3反事實,含
`score_cfg_override` 拆混淆)。資料:signals 檔存的 174 筆真實核心選股 / 23 天
(2026-05-08→07-22),每筆都有 score(純技術)與 rank_score(加成後)拆解。

### 結論:**stage-2 無罪(邊際小負),真兇是 07-18 前的舊技術權重**

反事實同日配對(k=當天實際核心數):

| 天期 | 實際核心 | 舊配技術top-k | 新配(v3)技術top-k | **stage-2 效應** | **config 效應** |
|---|---|---|---|---|---|
| 1日 | −1.25% | −0.03% | +0.04% | **−1.23pp** | −0.06pp |
| 3日 | −4.38% | −3.63% | −0.83% | −0.74pp | −2.80pp |
| 5日 | −6.79% | −6.02% | −0.75% | −0.76pp | −5.28pp |
| 10日 | −10.34% | −9.84% | −0.97% | **−0.51pp** | **−8.86pp** |

- stage-2 效應 = 實際 − 舊配技術(config 固定在當時線上版)= **−0.5~−1.2pp,小、
  且遠小於雜訊區間**。分項內證據(檢驗1/2)甚至偏正:chip_bonus 高的組全天期領先、
  被重排推升的組 3/5 日 +2.9/+4.1pp。
- config 效應 = 舊權重(rs +0.28 追強勢)vs v3(rs −0.176)= **10 日 −8.86pp,
  幾乎整個災難都是它**。重疊率佐證:實際核心與舊配技術 top-k 重疊 55.8%,
  與新配只有 22.2% —— 實際選股確實出自舊排序。
- **這同時解開了台帳負 alpha 之謎**(memory/twse-live-ledger-negative-alpha:
  第1日 −0.52pp、第10日 −5.80pp):主因是舊權重追強勢在這段行情被殺,不是 stage-2。

### ⚠️ 誠實邊界(寫進去,別讓數字被過度解讀)

1. **config 效應的 −8.86pp 有 in-sample 美化**:v3 權重是 07-18 依「包含這段樣本」的
   分析調出來的 —— 說「v3 會少虧 8.86pp」是部分後見之明。v3 的 out-of-sample 成績
   從 07-18 才開始累積。**但 stage-2 的歸因不受此影響**(那組比較把 config 固定在
   當時實際線上版)。
2. 「stage-2 效應」嚴格說是「技術排序之後所有步驟」的效應:含 regime 調整
   (prefer_pullback 懲罰/min_score)、新股名額替換,不只加成。
3. 23 天、同日群聚,幅度信心區間很寬;branch/industry/combo 加成樣本 <30 不下結論。
4. 第1日 −1.23pp 是 stage-2 最大的單點拖累(被推進來的股第1日 −1.63% vs 被擠出去
   −0.36%)—— 值得日後累積 v3 時代資料後複驗。

### 行動含義

- **stage-2 保留**,不動。無證據支持拆除;邊際小負在雜訊內。
- **v3(rs 轉負)方向獲強力佐證** —— 但真正的驗收是 v3 上線後的 live 數據。
- **2-3 個月後重跑一次**(工具已在 repo,幾分鐘的事):屆時樣本全是 v3 時代,
  stage-2 邊際效應可以乾淨複驗。輸出在 data/stage2_validation.json 與
  data/stage2_deconfound.json。


---

## 51. 個股健檢資料 Bug 修正 + 誠實覆蓋率(2026-07-24)

### 問題
健檢 `scripts/health/` 模組 2026-06-30 上線時，標註「未用真實 FinMind 驗證」：三個指標
（利息保障倍數/自由現金流/EV/EBITDA）長期顯示 0% 覆蓋率，且整體覆蓋率顯示值虛報（總是 100%）。

### A. 三個資料 Bug（已修）

**根因**：直接對線上 FinMind API 無 token 查詢確認，`_CF_TYPES` 的候選欄位名稱一個都沒命中：

| 指標 | 舊候選（全錯） | 實際欄位名 |
|------|------|------|
| 資本支出 | `AcquisitionOfPropertyPlantAndEquipment` | **`PropertyAndPlantAndEquipment`** |
| 折舊攤銷 | `DepreciationAmortizationExpense`（單欄） | **`Depreciation`** + `AmortizationExpense`（分兩列） |
| 利息費用 | 去損益表找（根本沒這欄） | **`InterestExpense`** 在**現金流量表** |

**關鍵陷阱**：現金流量表是 YTD **累計**，損益表是**單季**，相除基期不一致（Q4 差最多）。
- 新增 `scripts/health/quarterly.py`：`ttm()`（單季→滾動4季）、`ttm_flow()`（YTD去累計→滾動4季）、`_to_single_quarter()`。
- `financial_engine.py`：利息保障倍數、自由現金流改用 TTM。
- `value_engine.py`：EV/EBITDA 改 TTM；順帶修 DCF（capex 修好後會自動啟動，原「最新季×4」年化對累計數字嚴重高估）。
- `industry_benchmark.py`：同業利息保障倍數改從現金流表用 TTM 算（否則同業均值恆空）。
- `scripts/fetchers.py`：`_CF_TYPES` 補上實際欄位名、利息費用從損益表移到現金流表。

**沒動**：共用 `fetch_cashflow` 的 `op_cashflow`（餵 live 選股 `fundamental_bonus`，不可擾動）。

**煙霧測試結果（無 token，本機直打 FinMind raw API）**：
- 台積電：財務 14/14（100%）、利息保障 176 倍、FCF 1.056 兆（TTM）、EV/EBITDA 21.2。
- 鴻海 8.2 倍、台塑 −2.6 倍（營益虧損，正確）、中華電信折舊高（電信本色）。

⚠️ 本機無 token，欄位名以線上 raw API 驗証；**正式生效要等下次 daily.yml 跑（有 token 重抓現金流 parquet）**。

### B. 誠實覆蓋率（已修）

**問題**：舊 `covered_weight_pct` = 面向層級；只要面向有分數就顯示 100%，把「面向內一半指標其實抓不到」藏起來。

- `scripts/health/metric.py`：新增 `metric_coverage()`（指標層級，`not_applicable` 不計分母）。
- `scripts/health/engine.py`：每面向附 `coverage{present,total,pct}`，各風格加 `data_coverage_pct`（按面向權重加權的真實證據密度）。
- `docs/index.html`：每面向標題加「資料 N/M」chip（只在有缺口時顯示）+ 總分區「實際資料覆蓋率 XX%」說明。
- `docs/health/*.json`（15 檔）：已回填 `coverage` + `data_coverage_pct`，發布即生效。

**實測 2330**：short_term 覆蓋率 73.9%（舊顯示 100%）。風險面向 7/11（董監質押/違約/減資/重編無免費公開源）已誠實呈現。

### 已修改檔案（9 個）
`scripts/fetchers.py` / `scripts/health/quarterly.py` / `scripts/health/financial_engine.py` /
`scripts/health/value_engine.py` / `scripts/health/industry_benchmark.py` /
`scripts/health/metric.py` / `scripts/health/engine.py` / `docs/index.html` / `docs/health/*.json`（15 檔）

### 還沒做 / 已知限制
- **C（牛熊 regime 對應）**：把既有 `market_regime.risk_gate` 接進健檢分數（熊市降價值陷阱/動能加分）；使用者同意「先討論設計」，本次未實作。
- **籌碼大戶比/股東數**：`TaiwanStockHoldingSharesPer` 無 token 回 HTTP 400，疑需 Sponsor 階層；下次 daily 跑後驗。
- **Risk Tier2 四旗標**：董監質押/違約/減資/重編無免費源，誠實顯示即可。
- **正式驗收**：下次 daily.yml 跑完後看 `docs/health/` 幾檔，財務/估值面向是否真補到 100%。

---

## 50. 買/跳標記跨裝置同步(2026-07-23)

使用者:「買/跳不要只存本機,不同裝置用完全沒辦法統一數據。」

### 設計
- **儲存升級兩層**:`MYPICKF = {key:{v:'bought'|'skipped'|null, t:毫秒}}`(真格式,含墓碑)
  → 衍生出 `MYPICK = {key:'bought'|'skipped'}`(顯示用)。**既有顯示邏輯零改動**。
  舊純字串格式在 mpLoad 自動升級。
- **墓碑(v=null)是必要的**:沒有它,A 裝置取消的標記會被 B 裝置的舊資料復活。
  合併規則=每鍵比 t 新者勝;**前後端都做同一套**(伺服器端合併讓兩台幾乎同時 POST
  也不互蓋,並回合併後全量給前端採用)。過期墓碑 400 天清一次。
- **雲端檔** `config/my_marks.json`,走既有的 `api/watchlist.py`(GET `?what=marks` 公開讀,
  POST 帶 `marks` + 同一把 `WATCHLIST_SECRET`)。不加新的 Vercel function。
- **流程**:載入先 GET 合併(免密鑰)→ 本機較新且已存密鑰就靜默回推;
  每次點買/跳去抖 3 秒自動上傳;密鑰沒存過不打擾,狀態列顯示「未連雲端」點☁同步再設。
- **隱私界線不變**:標記只有「日期|代號|買或跳」,無張數/成本/損益(伺服器 `_clean_marks`
  再濾一次)。repo 公開 → 標記公開可見,與自選池同一條線(使用者先前已表示接受)。

### 驗證
伺服器:_clean_marks(非法鍵/值/非dict全丟、墓碑保留)、合併語意(新勝舊/雲端獨有保留/
過期墓碑清除)單元測過。前端(瀏覽器實測):舊格式遷移、取消=墓碑、合併三情境
(雲端新蓋本機/雲端舊被忽略/雲端獨有併入)、本機較新偵測、☁同步鈕+狀態列。
**未驗:Vercel 上的實際 POST(需部署後用兩台裝置互點確認)。**

### 使用者不用做任何新設定
密鑰與自選池共用(`WATCHLIST_SECRET` 已設過)、GitHub token 的 Contents 寫入權 7/20 已開。
部署自動跟著下一次 push。


---

## 52. 線上台帳接上大盤基準 —— 超額報酬(2026-07-30)

### 為什麼做這件
第 26 節(2026-07-19)把「線上台帳加大盤基準」列為**第一優先**,理由是
「`track.py` 與 `docs/index.html` 裡『超額 / benchmark』出現次數 = **0** —— 研究層用對的尺,
儀表板用錯的尺」。11 天後重新審視,發現**仍然是 0**:`build_report(index_close=...)` 這個參數
**宣告了但從頭到尾沒被使用**,而且 `main.py` 根本沒傳。等於帳面上有介面、實際完全沒接。

絕對報酬會把大盤自己的漲跌算到選股頭上。2026-06~07 大盤自己就跌了 12%,
網頁顯示「平均報酬 −23.86%」會讓人以為主要是大盤拖累 —— 實際上超額是 **−11.64pp**。

### 改了什麼
- **`scripts/track.py`**
  - `_bench_between(index_close, d0, d1)`:用 **`asof`(該日或之前最近一筆)以日期對齊**,不用位置差
    —— 個股與指數的交易日序列會有洞,位置差會飄掉(第 3 節「外資 30 日變化」踩過同一個坑)。
  - ⚠️ **`_bench_between` 必須擋區間外**:`asof` 對「晚於序列最後一筆」的日期會回傳最後一筆值,
    等於把大盤當成從此不再變動 → 基準恆 0 → **超額 = 原始報酬,而且畫面看起來完全正常**。
    第一版沒擋,實跑時 7/17 之後的選股全部顯示「超額 == 報酬」(6414 報酬 +7.78% / 超額 +7.78pp)才抓到。
    現在超出兩端一律回 `None`,前端顯示 `-`,**不以 0 充數**。
  - `_pick_perf(..., index_close)` 多回 `bench`/`excess`(逐天期)、`latest_bench`/`latest_excess`。
    每檔各自從**自己的選股日**起算(事件時間),無共同終點偏誤。
  - `overall`/`by_horizon`/`signal`/`exit_sim` 全部多 `avg_bench`/`avg_excess`/`beat_rate`/`n_excess`。
    `beat_rate` 與 `win_rate` **刻意分開命名**:前者是「贏大盤」,後者是「絕對正報酬」,混用會誤讀。
  - 已出場的單另外算**實際持有期間**(隔日開盤進場 → 出場當日)的大盤報酬 → `exit_sim.avg_excess`。
  - 報告多一個 `benchmark` 區塊(`available`/`name`/`note`),缺基準時前端/信件會明講而不是靜默留白。
- **`scripts/main.py`**:`build_perf_report(index_close=index_close, ...)` —— 把大盤真的傳進去。
- **`scripts/storage.py`**:新增 `load_index_cache()` / `upsert_index_cache()`(`data/meta/twii.parquet`)。
- **`scripts/main.py`**:每日抓完大盤就 `upsert_index_cache(index_df)`。
  ⚠️ **這修掉一個沒人發現的問題**:`data/meta/twii.parquet` **從來沒有任何流程在寫它**,
  停在 2026-07-17(最後一次手動補史),而 `backtest.py` 與 `score_validate.py` **只讀這份快取當基準**
  —— 研究層一直拿舊大盤在算超額。本次已補到 7/29。
- **`docs/index.html`**:超額卡片擺**第一張**(hero);天期表加「大盤同期 / 超額 / 勝過大盤」三欄;
  台帳加「超額」欄(hover 顯示同期大盤);出場模擬多一張「已實現超額」卡。
  ⚠️ **即時報價列的超額要複利今日大盤漲跌**:台帳的 `bench_ret_pct` 只到**昨收**,
  而 `ledLive()` 會用盤中價重算 `ret_pct` → 直接相減是拿兩個時點比。
  已改成 `(1+bench)×(1+PULSE.indices['001'].change_rate)−1`;沒有盤中大盤時沿用批次值(誤差僅一天)。
- **`templates/daily_email.html`**:同步加超額摘要列與天期表三欄。

### 驗證
- `python -m py_compile scripts/*.py api/*.py` 全過。
- `python -m scripts.track` 實跑 200 筆台帳,CLI 新增超額欄位。
- 抽 `<script>` 過 `node --check` OK(第 4 節慣例)。
- 用真實 `docs/data.json` 渲染 `daily_email.html`,確認三個新欄位都有值。
- 起 `python -m http.server` 實際載入網頁,確認 hero 卡 / 天期表 / 台帳欄 / 即時彙總都正確渲染。
- 嚴格 JSON 驗證(`parse_constant` 丟例外)通過,無裸 NaN。

### 結果(2026-07-30,200 筆,基準=加權指數)
| 天期 | 平均報酬 | 大盤同期 | 超額 | 勝過大盤 |
|---|---|---|---|---|
| 隔日 | −1.24% | −0.29% | −0.95pp | 42.1% |
| 3日 | −3.57% | −1.10% | −2.47pp | 36.1% |
| 5日 | −5.77% | −1.61% | −4.16pp | 32.2% |
| 10日 | −10.83% | −2.94% | **−7.89pp** | 26.3% |
| 20日 | −19.11% | −6.47% | **−12.64pp** | 16.9% |

已實現(扣成本後 vs 各自持有期間大盤):**−2.85pp**,勝過大盤 17.4%(n=149)。

⚠️ **這份數字幾乎全部來自 v3(07-18)之前的舊權重選股** —— v3 後的選股還沒滿 10 個交易日。
**別拿它當現行系統的成績單**,要驗收 v3 必須用 `date >= 2026-07-18` 切開單獨算(見第 53 節)。

---

## 53. 專案全面複審 + v3 真驗收(2026-07-30)

### ⚠️ 方法論陷阱:算 alpha 前先切 config
用**全部 200 筆台帳**算,10 日 beta 調整後 alpha **−6.27pp**(t=−7.4),
信心分四分位**單調反向**(最高分那組最慘 −8.17pp)。看起來像「比 07-19 診斷時更糟」。

**但這 148 筆全是 v3(07-18)上線前的舊權重選股** —— v3 後的選股還沒累積滿 10 個交易日的前推資料。
**把 pre/post-v3 混算會同時汙染兩個結論。** 報告任何績效數字前,先問「這批選股是哪個 config 產生的」。

### v3 真驗收(第 46/49 節要求的「上線後 live 數據」)
07-20~07-29,34 筆 / **8 個選股日**,基準=等權全市場:

| 持有 | n | 平均超額 | 逐個選股日為正 |
|---|---|---|---|
| 1 日 | 31 | **+0.71pp** | 6/7 日 |
| 2 日 | 28 | +0.69pp | 4/6 日 |
| 3 日 | 25 | **+1.47pp** | 4/5 日 |
| 5 日 | 15 | +1.35pp(beta 調整 +2.53pp) | 2/3 日 |

四個持有期**同號為正**、多數選股日為正、信心分相關由 **−0.152 翻成 +0.202**。
**專案史上第一次出現正超額。** 且這段大盤在跌、選股 beta≈1.9,高 beta 在跌市本該吃負超額,正號更難用 beta 解釋。

**但還不能宣稱有 edge**:只有 **7~8 個獨立選股日**(不是 34 個獨立觀察);
H=1 的 +0.71pp 幾乎等於來回成本 0.671%(H=3 才明顯超過);舊權重時期樣本小時也看不出負。
**要 2~3 個月、跨一次 style regime 才算驗收完成。這期間別再動排序權重(動了就重置樣本)。**

### 複審發現的其他問題(尚未修,依嚴重度)
1. **⚠️ 抓價失敗會靜默沿用舊快取,餵假分數** —— `main.py` 全市場迴圈:
   `df = upsert_prices(sid, inc) if not inc.empty else existing`,之後**沒有任何新鮮度檢查**就進 `compute_all`。
   實測 **89 檔停更、69 檔全凍在 07-09**。`6446` 藥華藥用 07-09 的資料(收 1285)算出 **62.8 分**,
   **連續 8 個交易日**掛在觀察清單、分數一字不變,而它 07-30 實際只剩 1,015。
   **不是停牌也不是上游沒資料**(本機直接抓回 14 列到 07-30);最可能是 CI 環境 yfinance 對部分 ticker
   間歇性回空,而 fallback 讓它永久隱形 —— workflow 永遠 success。
   修法:`compute_conviction` 前加新鮮度閘門,超過 N 個交易日沒更新就跳過並計入 `no_data`。
   ❌ 已排除的猜測:「`stock_info` 重複列導致 market_map 取到舊市場別」—— `dict(zip())` 是 **last-wins**,
   6446 拿到的是正確的 `twse`,且只 20% 停更股有 type 衝突。別再往這個方向查。
2. **候選池在跌市自己萎縮 15%** —— `scored_count` 從 960(06-23)單調掉到 **819**(07-29),
   同期 `scanned` 反而升到 1911。原因是流動性門檻是**絕對值**(收盤×20日均量 < 3000萬 淘汰):
   跌市量縮 → 池子萎縮,且倖存者偏向高周轉的散戶熱門股。改成**分位數門檻**可讓池子大小穩定。
3. `min_score` 顯示 5/8/12 **不是 bug** —— config 有註解說明是刻意設成不約束以隔離 v3 排序修正。
   但第 26 節「regime 閘門要能真的關機(輸出 0 檔)」仍未做,觀望日照樣出 3 檔。

### 網路比對(2026-07-30 查證)
- **台股被學術界明確記載為動能效應的例外市場**,原因是**散戶主導**的投資人結構;
  贏家組合高周轉率會抵銷動能利潤,**非持續型贏家/輸家在形成期後會反轉**。
  → 負 alpha 不是實作失誤,是**在台股用「近期強勢」選股本來就逆風**。
  離線回測、線上台帳、學術文獻三個獨立來源同一答案。
- **但文獻給了改法**:「**持續性(persistency)**」是關鍵調節變數,買**持續型贏家**在中期仍有顯著利潤。
  → 方向是**動能 + 持續性條件**(強勢要「久且穩」而非「近期噴」),不是把動能砍掉。值得當第二條軌道。
- **成本模型正確**:config(0.1425%×0.6×2 + 0.3% + 滑價 0.2% ≈ 0.671%)與公開費率完全吻合。
- **K線型態**:整體學術結論**無定論**,但**看多反轉型態、尤其 Piercing 穿刺線在台股顯著有利潤**。
  現行「只顯示不進分」是正確的保守選擇;若要進分,優先驗證看多反轉這一小類。
- 詳見專案記憶 [[twse-taiwan-momentum-literature]]。

---

## 54. 價格儲存分層 base + tail —— 止住 git 成長(2026-07-30)

### 問題(有到期日的基礎設施風險)
每檔一個 parquet、每天各補一根 K 棒 = **每個交易日重寫 1,888 個檔**。parquet 是 binary,
**git 無法 delta 壓縮** → 單一「daily update」commit 產生 **56 MB** 新物件。
實測 `.git` 已 **329 MB**、近 3 天新增 **138 MB(46 MB/天)**,約 **1.1 GB/月**
→ **兩週破 1 GB、3.5 個月破 5 GB**,免費自動化會撞牆。
(盤中那 ~97 個 commit/天反而無害:只改 `docs/freshness.json`/`pulse.json` 各 2 行。)

### 設計:分層,且**刻意不做資料遷移**
```
base  data/prices/{sid}.parquet         既有 1,979 檔原封不動,之後不再逐日重寫
tail  data/prices/tail/YYYY-MM.parquet  當月新增的 K 棒,全市場共一份
```
`load_prices()` = base ⊕ tail(同日以 tail 為準)。**tail 不存在時行為與改版前完全一致** ——
所以沒有「搬一半掛掉就毀了 5 年歷史」的風險,也不需要一次性的大遷移 commit。

**寫入是緩衝式的**:`upsert_prices()` 只更新記憶體,月檔在 `flush_prices()` 才落地。
否則一輪 1,900 次 upsert 會把同一個月檔重寫 1,900 次(比原本更慢)。
`daily_run` 在全市場迴圈後顯式呼叫,`storage.py` 另註冊 `atexit` 保險。

### 實測效果
對全部 1,979 檔各灌一根新 K 棒後 flush:**tail 總共 84.5 KB**(當月檔 70.7 KB / 1,975 列)。
月中 tail 會隨天數線性長大(月底約 1.4 MB),整月累計約 **15 MB**,對比原本 **1,100 MB/月**
—— **約 70 倍**。單日對比是 56 MB → 85 KB。

### 幾個非做不可的細節(踩過才知道)
- ⚠️ **不能只用日期排除既有列**。第一版寫 `inc = inc[~inc.index.isin(base.index)]`,
  但原本 `_upsert` 是 `keep="last"`,**新抓的資料會覆蓋既有日期** —— yfinance 會事後修正
  已發布的 K 棒(未定收盤補上就是一例)。只看日期整列丟掉 = 默默放棄修正。
  現在用 `_row_differs()` **比對內容**:相同才跳過,值不同照樣進 tail。
  (NaN vs NaN 要視為相等,否則含 NaN 的列每天都判定成有異動、天天寫進 tail。)
- **`save_prices()` 必須清掉該檔的 tail**。它是減資/分割的「整段重抓覆蓋」路徑,
  base 已是完整正確序列;殘留的舊尺度 tail 會疊回來,把剛修好的序列再弄壞一次。
- **`alert_chart.py` 原本繞過 `load_prices` 直接 `pd.read_parquet(price_path(...))`** —— 已改掉。
  **任何地方都不可以直接讀 base 檔**,會少掉當月 K 棒。`intraday_scan.py` 的 `price_path` import 是死的(未使用)。
- **`backfill.py` 是「整段抓」,要走 `save_prices` 而非 `upsert_prices`** ——
  補史一次幾百根 K 棒,走 upsert 會把當月檔撐大幾十倍(tail 的前提是每天只加一根)。
- `daily.yml` 是 `git add data/ docs/`(遞迴),新目錄自動涵蓋,workflow 不用改。

### 維護
tail 月檔會愈積愈多(不影響正確性,只是讀取要多合併幾份)。
半年~一年跑一次 `python -m scripts.storage --compact` 把 tail 併回 base 並清空
(那一次會有一個 ~56 MB 的 commit,之後 tail 重新從 0 長)。

### 驗證
- 28 項儲存層單元測試全過(tmp 目錄,不碰 `data/`):冷啟動合併、重複日期、修正既有 K 棒、
  save_prices 清 tail、多檔共用月檔不串檔、NaN 過濾安全網、查無此檔。
- **真實資料回歸**:1,979 檔逐一比對 `load_prices()` vs 直接讀 base,**差異 0 檔**。
- **離線完整 `daily_run`**(HANDOFF 第 4 節)跑通:Scored 827 / 核心 3 / 觀察 20,
  `價格 tail 已落地:0 個月檔`(fetch 回空 → 正確地沒產生 tail)。
- 測試產物已精準清除(`data/performance.json` `docs/{data,dates,heatmap,sector_map,tech_tags}.json`
  + `data/signals/2026-07-30.json` + `docs/history/2026-07-30.json`)。

---

## 55. 價格新鮮度閘門 —— 擋掉停更股餵假分數(2026-07-30)

### 問題
全市場評分迴圈裡這一行:
```python
df = upsert_prices(sid, inc) if not inc.empty else existing   # ← 增量抓不到就沿用舊快取
...
df_ind = compute_all(df)    # 沒有任何「這份資料是不是今天的」檢查
```
增量抓失敗時**靜默沿用舊快取**,把任意舊的最後一根 K 棒當成今日 →
均線/RSI/量比/信心分全是假的,照樣進排序、照樣入榜,前端毫無警示,**workflow 永遠 success**。

實測 2026-07-30:`data/prices/` 有 **89 檔停更**,其中 **69 檔全部凍在 2026-07-09**。
`6446` 藥華藥用 07-09 的資料(收 1285)算出 **62.8 分**,**連續 8 個交易日**(07-20~07-29)
掛在觀察清單、分數一字不變 —— 而它 07-30 實際只剩 1,015(真跌約 21%)。

**不是停牌、也不是上游沒資料**:本機直接 `fetch_price_history('6446','twse',days=15)` 回 14 列到 07-30。
最可能是 CI 環境 yfinance 對部分 ticker 間歇性回空(1900 檔連續抓,Actions IP 易被限流)。
❌ **已排除的猜測**:「`stock_info` 重複列導致 `market_map` 取到舊市場別」——
`dict(zip())` 是 **last-wins**,6446 拿到的是正確的 `twse`,且只 20% 停更股有 type 衝突。別再往這查。

### 改法
- `config/screeners.yaml` → `ranking.max_stale_days: 7`(日曆日;0 或負數關閉)。
- `scripts/main.py` 迴圈內、`compute_all` 之前:最後一根 K 棒落後基準日超過門檻 → `continue`,計入 `stale`。
- ⚠️ **基準用「市場最後交易日」(`index_close.index[-1]`)而非 `today`** ——
  離線測試與 `--date` 歷史模式下牆上時間會遠離資料日期,用 `today` 會把**全市場**誤殺成停更。
  指數抓不到時才退回 `today`。
- **要吵**:log 出 `⚠ 價格停更 N 檔` + 最後日期分布 + 範例代號;
  `stale_count`/`stale_stocks` 寫進 `data.json` / `signals/{date}.json` / `history/{date}.json`;
  前端副標顯示「⚠ N 檔資料停更已排除」,hover 可看個別代號與最後日期。
  (這類失敗不會讓流程紅燈,不主動露出就等於又回到「靜默用舊資料算分」。)

### 驗證(離線完整 daily_run)
```
⚠ 價格停更 51 檔(落後市場基準日 2026-07-29 逾 7 天),已排除不評分。
  最後日期分布 top3:[('2026-07-09', 38), ('2026-07-21', 3), ('2026-07-03', 1)]
Scored: 816 | 核心 3 / 觀察 20 / 自選 8
```
`6446` 在停更清單 = True、**已不在觀察清單** = True。評分數 827 → 816(11 檔停更股原本通過流動性門檻)。
前端實測副標顯示「⚠ 51 檔資料停更已排除」。`node --check` OK。測試產物已精準清除。

### 還沒做的
新鮮度閘門只是**擋住污染**,沒有解決「為什麼抓不到」。若停更檔數長期居高,
下一步是查 CI 的 yfinance 失敗率(加重試/降速/換資料源)。`no_data` 目前只收「完全沒快取」的,
與 `stale` 是兩類,別混。

---

## 56. 買/跳標記跨裝置不一致 —— 診斷與「未同步」可見化(2026-08-06)

### 使用者回報
手機與電腦的「我的實戰紀錄」數字完全不同:
電腦 我買的 **4 檔** / 我跳過的 14 檔;手機 我買的 **10 檔** / 我跳過的 21 檔。

### 診斷(不是合併邏輯壞了)
- 雲端 `config/my_marks.json` 實際只有 **11 筆**明確標記(4 bought / 7 skipped),最後同步 **08-01**。
- **電腦的「我買的 4 檔」與雲端完全一致 → 電腦是正常的。**
- 手機畫面顯示「**未連雲端 — 點「同步」設定**」→ 該裝置 localStorage **沒有 `twse-wl-secret`**,
  所以 `mpQueueSync()` 只會留在 dirty 狀態、從不上傳。手機那 6 筆多出來的買進標記從未離開過手機。
- 「跳過」數字差更多是因為 `mpGet()` 的**推論規則**:某天只要點過任一檔「買」,
  同一天其餘未點的自動視為「跳過」。所以每多一個買進日,跳過數就跟著放大 ——
  兩台的 explicit 標記差 6 筆,顯示出來會差到 13 檔。

**資料沒有遺失**:合併是「每鍵比時間戳、新者勝」,手機接上密鑰後 push 就會全部併進雲端。

### 真正的問題是「靜默失敗」
唯一的警示是卡片右上角一行 **10.5px 的灰字**(`var(--muted-2)`)。
使用者在手機上標了 6 筆買進、橫跨兩週,完全沒察覺沒上傳,兩台統計長期不一致。
這與第 55 節的停更股是同一類 bug:**不會報錯、不會紅燈,只會安靜地給出不一致的數字。**

### 改法(docs/index.html)
- 記住上次拉到的雲端快照 `_MP_REMOTE`,`mpUnsyncedCount()` 算出「本機比雲端新或雲端沒有」的筆數。
- `mpSyncBanner()`:卡片內顯示紅色橫幅
  「⚠ 本機有 N 筆標記還沒上雲端(這台裝置沒設定過同步密鑰)—— 這台看到的統計會與其他裝置**不一致**」
  + 一顆「立即上傳」按鈕(直接呼叫 `mpCloudPush(false)`,會跳出密鑰輸入框)。
  有密鑰時文案改成「尚未上傳完成」。
- `mpCloudLoad()` / `mpCloudPush()` / `mpQueueSync()` 完成後都重畫台帳,橫幅即時出現或消失。
- ⚠️ **順手修一個潛在的合併 bug**:`mpLoad()` 把舊格式(純字串)升級成 `{v,t}` 時給 `t=Date.now()`,
  但**沒有存回**。舊格式裝置每次載入都重新取得 now → 這些標記永遠比雲端新 → 每次合併都贏 →
  **其他裝置取消掉的標記會一直被復活**。現已在升級後立刻 `mpSave()` 把 t 定住。

### 驗證
本機起 http server,注入三種狀態實測:
無密鑰 → 「⚠ 本機有 4 筆…(這台裝置沒設定過同步密鑰)」+ 立即上傳鈕;
有密鑰未完成 → 文案改為「尚未上傳完成」;
雲端已含全部 → 橫幅消失、`mpUnsyncedCount()` 歸 0。`node --check` OK。

### 使用者要做的一次性動作
**在手機上按「☁ 同步」→ 輸入 Vercel 的 `WATCHLIST_SECRET`** → 31 筆標記會全部上雲,
電腦下次載入自動合併。之後兩台都會自動同步。

---

## 57. ⚠️ 同步鈕的 inline onclick 被 mpBindBtns 覆蓋(2026-08-06)

### 症狀
使用者:「我手機點同步的時候沒有跳出視窗」(iOS Safari)。

### 根因(不是 Safari 的問題)
```html
<button class="mp-b" onclick="mpCloudPush(false)">☁ 同步</button>
```
```js
$('#ledtable').innerHTML = ledLiveSummary(...) + mpSummaryHtml(LED) + ...;   /* 同步鈕在 #ledtable 內 */
mpBindBtns($('#ledtable'));
function mpBindBtns(root){
  root.querySelectorAll('.mp-b').forEach(b=>{ b.onclick = e=>{ ... mpSet(b.dataset.mk,b.dataset.mv); }; });
}
```
**同步鈕與買/跳鈕共用 `.mp-b` 這個 class**(為了外觀一致),而它就被塞在 `#ledtable` 裡
(`mpSummaryHtml` 的輸出)。`b.onclick = fn` 會**覆蓋 inline onclick 屬性** →
按下去執行的是 `mpSet(undefined, undefined)`,不但不跳密鑰輸入框,還會在 MYPICKF 塞一個
`"undefined"` 垃圾鍵(值是墓碑 `{v:null}`),而且會被 push 到雲端。

**為什麼電腦沒事**:自選池的同步鈕是 `class="refresh" id="wl-sync"`,**class 不同名、不會被掃到**。
使用者當初在電腦按過那顆、存下密鑰,所以標記走「有密鑰就靜默自動 push」的路徑一直正常。
手機從沒按過自選池同步,而標記頁那顆同步鈕是壞的 → 永遠拿不到密鑰 → 標記永遠留在本機。
**這就是第 56 節「兩台數字不一致」的真正上游原因。**

### 改法
1. **`mpBindBtns` 選擇器改成 `.mp-b[data-mk]`** —— 只綁真正的標記鈕。(核心修正)
2. 同步/上傳類按鈕一律改用 `class="refresh"`,不再共用 `.mp-b`。(雙保險)
3. `mpSet(k,v)` 開頭 `if(!k||!v)return;` —— 防呆,誤綁也寫不進垃圾鍵。
4. `mpLoad()` 丟掉不含 `|` 的 key —— 清掉既有的垃圾鍵(雲端目前乾淨,只是保險)。
5. **密鑰輸入不再用 `prompt()`** —— iOS Safari 一旦按過「不再顯示」就會**靜默封鎖所有對話框**,
   使用者按了沒反應又查不出原因。改成橫幅內建 `<input type="password">` + 「連線並上傳」按鈕
   (`mpSaveSecretAndPush()`),在哪個瀏覽器都能用。密鑰錯誤時也重畫,把輸入框換回來讓人重試。

### 驗證(靜態檢查,可重跑)
抽出所有 `class="mp-b"` 的 `<button>`,斷言**每一個都帶 `data-mk`**;
斷言 `mpBindBtns` 的選擇器含 `[data-mk]`;斷言所有 `onclick="mpCloudPush(...)"` /
`mpSaveSecretAndPush()` 的按鈕 class **不含 mp-b**。結果:4 個 .mp-b 全是標記鈕、
3 個同步類按鈕全為 refresh。`node --check` OK。

### 教訓
**用 class 同時當「樣式」與「行為選擇器」很容易出這種事** —— 有人為了長得一樣借用了 class,
行為就跟著被綁上去。之後要綁行為,選擇器一律加上該行為專屬的 data 屬性條件。

---

## 58. 分點缺口會無聲累積 —— 夜間跑完自動補洞(2026-08-07)

### 起因
2026-08-06 15:22Z 起 **GitHub Actions / Pages 大規模故障**(`Failed to resolve action download
info: Service Unavailable`),當晚 `Branch Chips (nightly)` 連 checkout 都還沒跑就掛了,
pages build 也失敗。這部分**不是我們的 bug**,平台恢復後重跑即可。

但查缺料時發現真問題:**近 20 個交易日缺了 `2026-07-27` / `2026-08-03` / `2026-08-06` 三天**。
也就是說夜間 job 每掛一次,那天的分點就**永久缺一格,而且沒有任何地方會發現** ——
`chips.yml` 的設計是「沒有檔案變動就乾淨跳過不讓 job 變紅」,失敗與「非交易日」長得一樣。

### 為什麼缺口比想像嚴重
`compute_streaks()` 只是把所有 parquet 併起來**按日期排序數連續同號**,
**中間缺一天是隱形的** —— 跨過缺口的「主力連買 5 日」其實根本不連續。
缺口不只少一天資料,是**污染連買連賣這個欄位本身**。

### 改法([scripts/branch_chips.py](scripts/branch_chips.py))
預設路徑(`python -m scripts.branch_chips`,即 workflow 每晚跑的那條)在 `run()` 之後、
`compute_streaks()` 之前,自動補 `CATCHUP_DAYS = 10` 個交易日內的缺口:

- 直接重用既有的 `backfill()` —— 它第 167 行本來就有 `have = {p.stem for p in OUT_DIR.glob(...)}`,
  **會跳過已存在的日期**,天生就是補洞工具,只是排程從來沒叫過它。
- **沒缺口時零成本**:`todo` 為空 → 迴圈第一圈就 `break` → 一次 API 都不打。
- `CATCHUP_DAYS = 10` 取這個數是配合 `net_series` 長度,也讓「某天真的補不到」的情況
  **最多重試 10 個交易日就自然停手**,不會永遠每晚重試。
- 新增 `--no-catchup` 可關掉。`chips.yml` **不用改**(commit 步驟已經 `git add data/chips_branch`)。
- catchup **不依賴 `run()` 成功**,寫在 `run()` 之外 —— 當晚沒發布 / 非交易日照樣補前幾天的洞。

### 驗證(離線 monkeypatch,可重跑)
patch 層是 **`scripts.branch`**:`backfill()` 內是函式內 from-import,
但 `_fetch_one_day` 是 **module-level** `from .branch import`,所以 runpy 前先 patch
`scripts.branch` 兩個函式才攔得到(from-import 綁定陷阱,同 §347 那次)。

1. 有缺口 → 鎖定的正是 `('2026-07-27','2026-08-03','2026-08-06')` 三天。
2. 無缺口(`_trading_days` 改回傳已有檔的日期)→ **API 呼叫 0 次**,assert 通過。
3. 端到端跑 `python -m scripts.branch_chips --limit 2`:`run()` 抓 2 檔 → 回 `no_data`
   (01:26 分點還沒發布,正確)→ catchup 照樣觸發、目標日正確 → `compute_streaks()`。

### 未做 / 誠實邊界
- **那三天的缺口還沒真的補回來**:本機沒有 `FINMIND_TOKEN`(`.env.local` 只有 Vercel 的)。
  平台恢復後跑一次 `Branch Chips (nightly)` 的 `workflow_dispatch` 就會自動補上,
  或手動帶 `backfill=10`。補完前所有「連買 N 日」跨到那三天的都不可信。
- 缺口的**根因沒查**(只知道 08-06 是平台故障,07-27 / 08-03 為何缺不明)。
  這次做的是止血:不管什麼原因掛掉,隔天自己補回來。

---

## 59. 「我跳過的」只認明確按下的那一下(2026-08-07)

### 起因
使用者問「買跟跳,不同天出現相同標的會打架嗎」。查下來分三層,答案不一樣:

1. **儲存層 — 不打架。** key = `` `${r.date}|${r.stock_id}` ``,不同天是不同鍵、各自獨立。
   互斥只發生在同一天同一檔(買/跳共用一鍵,再點同一顆=取消並留墓碑)。
2. **統計層 — 已經在打。** 3231 緯創 `07-22 買` + `07-23 買` 被算成兩筆獨立交易
   (+15.55% / +9.22%),但那是同一個決策。n 只有 13,一檔重複就佔 8%。
3. **推論層 — 埋著未爆彈(本次主因)。**

### 舊規則錯在哪
舊 `mpGet()`:「當天只要標過任何一檔買 → 同日其餘沒標的自動視為跳過」。省操作成本,
但把三種心理狀態混成同一格 —— (a) 看過明確不要 (b) **沒把握、刻意不表態** (c) 根本沒看到。
使用者原話:「有些我不太確定,我就不會按買也不會按跳過」。(b)(c) 灌進對照組後,
「你跳過的」就不再是他的判斷,量化人工選股失去意義。

更硬的錯:推論**按日期**算,所以「X 日買進、Y 日又被推薦但沒重標」的股票會在 Y 日被記成
「看過沒買」——**實際上正抱著**。台帳 140 檔有 **39 檔(28%)跨多天重複出現**。
當時實測 0 筆純屬運氣(標買的 2377/3022/2425 剛好都是最後一次出現;
最接近的 2867 只因 07-20 標的是「跳」才沒事)。

### 改法([docs/index.html](docs/index.html))
- `mpGet(r)` 簡化成 `return MYPICK[mpKey(r)]||null;` —— 只認明確標記。
- 刪掉 `mpDaysWithBuy()` / `_mpBuyDays` 快取,以及 `mpSet` / 雲端合併裡兩處
  `_mpBuyDays=null` 失效賦值(留著會變隱式全域)。
- 面板加一行說明:「我跳過的」只算真的按過跳的,沒表態的不列入。
- 推論層拿掉後,上面第 3 點的未爆彈**一併消失**(不再有任何自動判定)。

### 數字變化(真實資料,191 筆台帳 × 26 筆標記)
| | 舊(含自動推論) | 新(只認明確) |
|---|---|---|
| 我買的 | n=13 +1.62% 勝率31% | n=13 **+1.62%** 勝率31% |
| 我跳過的 | n=25 +0.59% 勝率44% | n=13 **+1.39%** 勝率**54%** |
| 差距 | +1.03% | **+0.23%** |

**代價:對照組 25→13,累積變慢。這是使用者知情下的取捨** —— 寧可樣本少,
不要對照組混進根本不是他判斷的東西。順帶一提新數字誠實得多:
他真的跳過的那 13 檔勝率 54%,比他買的 31% 高。

### 驗證(真實 localStorage 資料 + 本機 http.server,可重跑)
`node --check` 抽出的 inline JS 過;注入真實 26 筆標記後載入頁面:
面板渲染 `13檔/31%`、`13檔/54%`、`191檔/25%`,與 Python 獨立算出的完全一致;
`typeof _mpBuyDays === 'undefined'`(無殘留全域);
點一顆未標記的「跳」→ 27 筆且 `on-s` 亮 → 再點取消 → 回 26 筆(§57 迴歸測試同時通過,
`.mp-b` 沒有任何一顆缺 `data-mk`)。

### 3231 重複計入 —— 已裁示:維持現狀(2026-08-07)
使用者確認 `07-22` / `07-23` 那兩筆是**加碼**,不是同部位重標。
所以兩筆各自是「在該建議日做了一次決策」,分開計入是對的,**不去重**。
通則:標記衡量的是「每個建議日你當下的判斷」,同一檔在不同建議日各算一次。

---

## 60. Vercel Active CPU 燒到 88% —— 熱力圖輪詢停不下來(2026-08-12)

### 起因
Vercel 寄信「已用掉免費額度 75%」,實際 **Fluid Active CPU 3h33s / 4h(88%)**。
**超過 100% 整個專案會被自動暫停**,不是降速。

### 先確立計費規則(推翻了直覺答案)
[官方文件](https://vercel.com/docs/functions/usage-and-pricing)明講 Active CPU **不計 I/O 等待**,
而且直接點名 AI 呼叫:「only billed during actual code execution and not during I/O
operations (database queries, like AI model calls, etc.)」。
→ 所以**不是** AI 總覽/健檢的 LLM 呼叫在燒(那些全是 I/O),是 Python 真的在算。
(Provisioned Memory 才是連 I/O 一起算,但那項只用了 35.6/360 GB-Hrs,不是瓶頸。)

### 實測:CPU 幾乎全是冷啟動,不是運算
| 項目 | 實測 |
|---|---|
| `/api/quote` 真正的工作(2852 檔 isin + to_dict 180 檔) | **2.2 ms** |
| `import pandas` 冷啟動 | **846 ms** |
| 實際平均(12,780 秒 ÷ 45,000 次呼叫) | **284 ms/次** |

284/846 ≈ **34% 的呼叫在付冷啟動代價**,真正的工作只佔 2ms。
**不是為運算付錢,是為「一直重新啟動 Python」付錢。** 所以省 CPU 的槓桿在
**減少函式呼叫次數**,不在優化演算法。

### 根因:`_hmLiveTimer` 是全檔唯一沒人管的計時器
| | `_liveTimer` | `liveTimer` | **`_hmLiveTimer`** |
|---|---|---|---|
| `clearInterval` | ✅ | ✅ | **❌ 全檔沒有** |
| `document.hidden` | ✅ | — | **❌** |
| `inSession()` | ✅ | — | **❌** |

**2026-07-21 就修過一模一樣的 bug**(見 `startLiveQuotes` 註解:「原本開著網頁就每 20 秒
打一次 /api/quote,24 小時不停」),但**熱力圖那條漏掉了**。
規模:755 檔 ÷ CH=180 → 每 30 秒 **5 個並行** `/api/quote` = **600 次/小時**,
開過一次就永遠停不下來(切分頁、收盤、半夜、週末照打)。

**最諷刺的**:舊版把「熱力圖分頁是否開著」的檢查放在 `hmLive()` **最後面** ——
打完 5 個請求才決定要不要重畫,貴的部分早就花掉了。

算術對得上:600 × 34% × 0.846 = 170 秒/小時 ≈ **2.8 分/小時**;
開 10 小時 = **28 分/天**,與用量圖上 7/27、7/28、8/12 的 **~29 分**尖峰完全吻合。
低的日子 1~3 分 = 沒開熱力圖。4 小時額度只夠這樣開 **85 小時/月**。

### 兩刀
**第一刀 — 三道閘門 + 生命週期**([docs/index.html](docs/index.html))
- `hmLive()` 開頭加 `document.hidden` / 分頁是否 active / `inSession()&&_hmLiveN` 三道閘門,
  **一律放最前面**(舊版放最後是主要的錯)。
- 拆出 `startHmLive()` / `stopHmLive()`,`activateTab` 改
  `if(p==='heatmap'){initHeatmap();startHmLive();}else stopHmLive();`。
- ⚠️ **坑**:`initHeatmap()` 開頭有 `if(_hmReady)return;`,所以計時器**不能**留在它裡面 ——
  否則加了 clearInterval 後重進分頁永遠不會重啟。必須由 `startHmLive()` 另外起。
- 加 `visibilitychange` 切回前景立刻補一次,避免看到過期價格。

**第二刀 — 扇出 5→1**([api/quote.py](api/quote.py) `MAX_IDS` 200→1000、前端 `CH` 180→900)
快照本身就是全市場 2852 檔一次抓回,回 180 檔和回 755 檔差不到 10ms,
但每多切一批就多一次冷啟動(846ms)。755 檔的 ids 參數 3.8KB
(encodeURIComponent 後 5.3KB),離 Vercel URL 上限 ~14KB 仍有餘裕。

### 效果
| 情境 | 修前 | 修後 |
|---|---|---|
| 看著熱力圖(盤中) | 2.8 分/小時 | **0.6 分/小時** |
| 切到別的分頁 | 2.8 分/小時 | **0** |
| 背景 / 收盤 / 半夜 | 2.8 分/小時 | **0** |

4 小時額度:85 小時/月 → **423 小時/月**,且只在真的盯著熱力圖時才計。

### 驗證(本機 http.server + 攔截 fetch,可重跑)
`node --check` 與 `py_compile` 都過。攔截 `window.fetch` 計數:
1. 進熱力圖 → 計時器 running;切走 → **stopped**;再進 → **restarted**(`_hmReady` 那個坑已避開)
2. 不在熱力圖分頁時直呼 `hmLive()` → **0 次請求**
3. 偽裝前景後跑一輪 → **1 個請求**(舊版 5 個),URL 5,297 字元
4. `_hmLiveN>0` 且非盤中 → **0 次**;`document.hidden=true` → **0 次**
5. 伺服器端:送 755 檔 → `MAX_IDS=1000` 保留 755 檔,未被截斷

### 追加:前端不再假設伺服器的 MAX_IDS(同日補)
推完之後實測發現 **desync 是真的會發生的,而且當下就在發生**:
GitHub Pages 已經是新前端(`CH=900`),Vercel 卻還是舊的 `MAX_IDS=200` ——
輪詢 20 次、送 250 檔一直只回 200 檔。**前端在 Pages、後端在 Vercel,是兩套分開的部署,
版本不同步的空窗必然存在**,不是這次的意外。

而舊伺服器超過上限是**靜默截斷**:沒有錯誤、沒有旗標,755 檔只回 200 檔 →
一部分即時、一部分昨收,使用者完全看不出差別。這正是 2026-07-21 認定的
「半新半舊比全舊更糟」,等於我為了省 CPU 把那個 bug 又放回去了。

**改法**:新增 `quoteAll(ids)` 取代原本的 `Promise.all(batches...)` ——
少拿到的就把缺的再要一次,直到沒有進展為止(`guard<8` 上限、
「這輪一檔都沒補到」就停,避免真的查無資料時無限迴圈)。
**不再對伺服器上限做任何假設**:伺服器夠新時只打 1 次,還舊時自己補到齊,
兩邊誰先部署都不會壞。

驗證(假伺服器可設定上限並靜默截斷,完全複製舊 Vercel 行為):
| 情境 | 結果 |
|---|---|
| 舊伺服器 200 | 請求序列 755→555→355→155,**補齊 755/755** |
| 新伺服器 1000 | **1 個請求** |
| 有 2 檔真的查無資料 | 第 2 輪無進展即停,**不無限迴圈**(753/755) |
| 端點回 500 | 立刻返回,不卡死 |

閘門迴歸同時重測:進入 running / 離開 stopped / 重進 restarted、
背景 0 次、非盤中已有資料 0 次、非熱力圖分頁 0 次、一輪 1 個請求、`_hmLiveN=755`。

### 未做
- **沒有實際驗證修後的 Vercel 用量**(要等部署後累積幾天才看得出來)。
- **Vercel 至今沒有部署這次的 commit**(head 就是它、不符合 ignoreCommand 的跳過規則,
  原因不明)。因為前端已改成自癒,這件事不再影響正確性,只影響省下多少呼叫 ——
  Vercel 部署前是 4 次/輪,部署後才是 1 次/輪。
- 沒查 7/27~7/29 那三天為何連續高;推論是熱力圖分頁連開三天,但無法從這端證實。
- 使用者可到 Vercel 用量圖的 **Type / Runtime** 分頁按端點拆,直接驗證
  `/api/quote` 是否佔壓倒性多數。

---

## 61. 一次 GitHub 500 就白跑一整輪 —— push 加重試(2026-08-13)

### 起因
`Branch Chips (nightly)` 失敗。日誌看下去**不是我們的 bug**:

```
remote: Internal Server Error
 ! [remote rejected]     main -> main (Internal Server Error)
```

分點抓完、`data/chips_branch/2026-08-13.parquet` 寫好、commit `4394ae89e` 也做了,
**最後 `git push` 被 GitHub 以 500 拒絕**,runner 一銷毀當天資料就沒了。
沒有重試 = 一次隨機的平台抖動白跑 40 分鐘。

### 順帶驗證:§58 的 catchup 在正式環境確實有效
近 20 個交易日只缺 `2026-08-13`(今天這次),
而 §58 當時列的 `07-27` / `08-03` / `08-06` **全部已被自動補回**
(commit `e49f11711`,2026-08-07 那次夜間跑順手補的)。缺口累積問題確認解決。
所以 08-13 明天也會自己補回來 —— 但那是「事後補救」,不該取代「當下就推成功」。

### 改法([.github/workflows/chips.yml](.github/workflows/chips.yml))
`git push` 改成最多 3 次、退避 5/10/15 秒的重試迴圈:
- 每次重試前先 `git pull --rebase origin main`(機器人 commit 很頻繁,落後是常態)
- **rebase 撞衝突就 `rebase --abort`** —— 寧可這輪失敗(隔天 catchup 會補),
  也不要把半套 rebase 結果推上去
- 3 次都失敗才 `exit 1`,並在日誌寫明「隔天 catchup 會自動補回」

### 驗證(假 git + 假 sleep,可重跑)
`yaml.safe_load` 解析 OK、`bash -n` 語法 OK,四種情境實跑:
| 情境 | 結果 |
|---|---|
| 第 1 次就成功 | exit 0,不重試 |
| 前 2 次失敗第 3 次成功(**今天的狀況**) | 退避 5→10 秒,exit 0 |
| 3 次全失敗 | exit 1 + 明確訊息 |
| push 失敗且 rebase 撞衝突 | `rebase --abort`,不卡死,exit 1 |

`set -e` 不會被 `if git push` 裡的失敗誤殺(if 條件不觸發 -e)。

### 追加:抽成共用腳本,六支全部套用(同日)
使用者裁示「一併套上去」。與其把同一段重試複製六次,抽成
**[.github/scripts/push-with-retry.sh](.github/scripts/push-with-retry.sh)**,
六支 workflow(chips / daily / intraday / premarket / snapshot / backfill)全部改成:

```
git commit -m "..."
bash .github/scripts/push-with-retry.sh "這輪失敗的後果說明"
```

參數是「失敗後果的一句話」,寫進失敗日誌 —— 每支的後果不同
(chips 隔天 catchup 會補、daily 是網頁停在昨天要手動重跑、
snapshot 是該時點永久缺一格、intraday 5 分鐘後自己重掃、backfill 冪等可直接重跑)。

**與最初 chips 版的兩點差異:**
1. `git pull --rebase` 移到**迴圈內、每次嘗試前都做**(原本第一次 push 前沒有,
   落後 main 時要浪費一次嘗試才會 pull)。機器人 commit 盤中每分鐘都有,落後是常態。
2. 最後一次失敗後**不再 sleep**(`if [ "$i" -lt 3 ]`),不白等 15 秒。

同時清掉了六支裡的 `git pull --rebase origin main || true` —— 那個 `|| true`
會在 rebase 撞衝突時吞掉錯誤、留下「rebase 進行中」的狀態,接著的 push 就推出半套結果。
改用 `|| { git rebase --abort || true; }`。

### 驗證(假 git + 假 sleep,可重跑)
六支 `yaml.safe_load` 全過、`bash -n` 過、`grep` 確認沒有殘留的裸 `git push`。
四情境實跑:
| 情境 | 呼叫順序 | exit |
|---|---|---|
| 第 1 次就成功 | `pull push` | 0 |
| 前 2 次失敗第 3 次成功 | `pull push ×3`,退避 5→10 | 0 |
| 三次全失敗 | `pull push ×3`,最後不多睡 | 1 |
| push 失敗 + rebase 撞衝突 | `pull abort push ×3`,不卡死 | 1 |

⚠️ **測試踩到的坑(給未來的自己)**:第一次寫測試時用
`PATH="C:/Users/.../fakebin:$PATH"`,**Git Bash 的 PATH 不吃 Windows 路徑格式**,
假 git 沒生效 → 腳本拿真 git 對真 remote 跑了 3 次 push。
所幸全被 `fetch first` 拒絕、`pull --rebase` 也因有未提交改動而拒絕啟動,沒有損害。
**要用 `cygpath -u` 轉路徑,而且跑之前先 `which git` 斷言含 `fakebin` 才繼續。**

### 08-13 的資料
使用者裁示「現在補」→ `gh workflow run chips.yml -f backfill=10`,
commit `18eff1b97` 補回 `data/chips_branch/2026-08-13.parquet`(14,451 bytes)。

### (已解決)其餘 4 支 workflow 仍是舊寫法
`daily.yml` / `intraday.yml` / `premarket.yml` / `snapshot.yml` 的 push 段
與 chips 出事前**一模一樣**(`git pull --rebase origin main || true` + 裸 `git push`),
同一個 GitHub 500 打到它們也會一樣白跑。本次只動了實際出事的 chips,
**其餘 4 支未改,待裁示**。(`backfill.yml` 是手動一次性工具,只有裸 `git push`,優先度低。)

注意 `|| true` 那段本身也有問題:rebase 撞衝突時它會吞掉錯誤、留下 rebase 進行中的狀態。
2026-07-22 那次盤中卡死修的是 `git add A B C` 的 fatal(§intraday 註解),**push 這段從沒被加固過**。

---

## 62. 8/14 盤中健檢報告對帳 —— 找出 10 個問題,修掉 8 個(2026-08-14)

### 起因
使用者把 2449 / 2344 / 2337 / 2303 四份**盤中**健檢報告丟過來,要求「跟實際盤面數據比對」。
逐項對帳的結論先講:**算術層沒有錯,籌碼與財報數字都忠實反映 FinMind 原始值**,
但有 10 個會讓人做出錯誤判讀的問題,其中多數是可解釋性/方法論的瑕疵而非計算 bug。

### 對帳方法(可重跑)
- 籌碼:`fetch_chips_history(sid, 7/20, 8/14)` 現抓,逐項比對今日/近5日/外資/持股%/融資融券
- 財報:`data/financials|balance|cashflow/*.parquet`(2337/2344 有本機資料)+ FinMind 現抓(2303/2449)
- 技術:`storage.load_prices()` + `compute_all()` 用**完整 K 棒(到 8/13)**重算,對照報告值
- 四檔籌碼**全部逐位吻合**(例:2303 `inst_total` −469,674 股 → 報告 −470 張)

### 已修(8 項)

| # | 問題 | 檔案 |
|---|---|---|
| 1 | **負基期年增率**:`yoy()` 只擋 `prev == 0` 沒擋 `prev < 0`,除以 `abs(負數)` 造出假成長 | [quarterly.py](scripts/health/quarterly.py) |
| 2 | **業外一次性獲利被當本業** | [quarterly.py](scripts/health/quarterly.py) + financial/growth/value |
| 3 | **DCF 沒擋負 FCF** → 印出「合理價 −231.3 元」 | [value_engine.py](scripts/health/value_engine.py) |
| 4 | **月營收在線上健檢永遠抓不到** | [engine.py](scripts/health/engine.py) |
| 5 | **「連續營收衰退月數 0 個月」是沒資料時捏造的好消息** | [risk_engine.py](scripts/health/risk_engine.py) |
| 6 | **YTD 累計當成單季 → 營業現金流的 ↑ 是假趨勢** | [financial_engine.py](scripts/health/financial_engine.py) |
| 7 | **單位標籤錯 1000 倍**(`unit="千元"` 但 FinMind 給的是「元」) | [financial_engine.py](scripts/health/financial_engine.py) |
| 8 | **Swing Score 飽和**:固定上限讓當沖/隔日沖恆為 100 | [scoring.py](scripts/health/scoring.py) |

#### 1. 負基期年增率
2337 去年同季營業利益 **−10.79 億**、2344 **−12.95 億**(都在虧),舊寫法算出
**+931.1%** / **+2337.7%**,再一路污染 PEG(0.05 / 0.01,兩檔都排進「優點」第一列)
與成長分(94.9 / 99.4)。`yoy()` 改擋 `prev <= 0` 並移除 `abs()`;新增 `yoy_basis()`
回傳 `turnaround`/`still_negative`/`no_base`。growth_engine 負基期改顯示
**「由虧轉盈(年增率不適用)」**(附去年同季實際數字),方向照給 good/bad
但**不進 momentum 分**。value_engine 的 PEG 改標 `not_applicable`(原本會落到
`api_unavailable`,讓人以為重試就有)。

#### 2. 業外一次性獲利
2303 聯電 2026Q2:營收 687.3 億、營業利益 149.5 億、**淨利 422.2 億** → 業外 **+272.7 億**。
淨利率 61.43% > 毛利率 32.48%,本業結構上不可能達成,但舊版把它當獲利能力算進財務體質
(96.1 分),又用含這筆一次性的 TTM EPS 推出 PE 18.72 / EPS年增率 377.5% / PEG 0.05。

新增 `quarterly.nonoperating_dominant()` 作**單一事實來源**,三種命中方式:
- `structural` 淨利率 > 毛利率(結構上不可能)
- `loss_cover` 營業利益 ≤ 0 但淨利 > 0(本業虧損,獲利全靠業外)
- `ratio` |淨利 − 營業利益| ≥ 營業利益(業外規模不小於本業)

命中後三個 Engine 一起降級:財務體質的**淨利率/ROE/ROA 不進獲利能力子分**(改記 0.3 折扣)、
成長的**淨利/EPS 年增率轉 neutral 且不計分**、估值的 **PE 不計分 + PEG 停用**,
並多出一條 `nonoperating_dominant` 指標(rating=bad)把差額金額攤開。
全市場 308 檔命中 46 檔(14.9%:structural 23 / loss_cover 8 / ratio 15),
抽查 ratio 那組是友達 8.71×、鴻準 1.56× 這類真實案例,沒有明顯誤報。

⚠️ **踩到的坑**:`kind_txt` 原本寫成 dict 查表,但 dict 的三個 value 會**全部先算完**再取一個,
而 `nonop_ratio` / margin 在非對應 kind 下可能是 None → f-string 直接 `TypeError`。
改成 if/elif 才對。全市場掃描才抓到,四檔測試沒踩到。

#### 3. DCF 負 FCF
2449 近12個月 FCF **−130.9 億** → 五年複合成長後折現全是負的 → 每股 **−231.3 元**、
「高估 192.7%」。`_compute_dcf` 加 `fcf_ttm <= 0` 的 guard,回 `available: False` 並說明
「兩階段 DCF 的『未來現金流為正』前提不成立…重資本支出擴產期屬正常,建議改看 EV/EBITDA」。
前端本來就有 not-available 分支,只把文案「資料不足:」改成「**不適用:**」。

#### 4. 月營收永遠抓不到 —— 根因是部署設定,不是 API
`build_ctx_batch` 對 financials / balance / cashflow / per / chips / holder_dist **都有
「過期就現抓」的 fallback,唯獨 `revenue_df` 是直接 pass-through**;而 `api/health.py` 用
`load_revenue()` 讀本機 parquet,`.vercelignore` 又排除整個 `data/` → **線上健檢的月營收
四項指標永遠是「資料不足」**,與 FinMind 無關。

**沒有改 `.vercelignore`** —— 把 `data/`(數百 MB)塞進 bundle 會撞 serverless size 上限,
`api/health.py` 檔頭註解早就在擔心這件事。改成補上 fallback,與其他六個資料集一致。
另加 `_stale_month_index()`:revenue 的 index 是 `'YYYY-MM'` **字串**不是 DatetimeIndex,
`_stale()` 會丟例外落到 True(剛好能動,但是靠例外),獨立判斷比較誠實。
驗證:2449 原本 0 筆 → 抓到 26 筆,月營收 YoY **+36.7%**、連續正成長 **14 個月**、**創新高**
(原報告這三項全是「資料不足」)。

#### 5. 捏造的「連續營收衰退 0 個月」
`decline_streak = 0` 初始化後,沒資料時**不走 `missing_metric`,照樣以 rating="good" 輸出**。
四份報告都把它列進「優點」,而同一份報告的月營收 YoY 卻標「資料不足」—— 自相矛盾。
改成沒資料就 `missing_metric`,而且**不把該條規則加進 `rules`**(不計分也不假裝沒命中)。

#### 6. YTD 累計 → 假趨勢
現金流量表是 YTD 累計,`financial_engine` 的 `op_cashflow` 直接取 `q.last()` 未去累計,
再跟前一季比 → **Q1→Q2 必然 ↑**,四檔全部標 improving。「近期趨勢」那行更明顯:
2449 顯示 `2025Q1:9.8億 → 2025Q4:131.5億`,那是 3M 累計跟 12M 累計並排。
同檔案的自由現金流、利息保障倍數本來就已用 `ttm_flow` 去累計,**只有這一項漏掉**。

新增 `quarterly.flow_at()` / `flow_trend()`(包既有的 `_to_single_quarter`),
營業現金流、現金流穩定度、`risk_engine` 的淨利/現金流背離檢查全改單季。
- 2449:63.7 億 **↑improving** → 26.3 億 **↓worsening**(Q1 37.4 億 → Q2 26.3 億,單季確實在下滑)
- 2337 現金流穩定度:YTD 算 62.5% 判「不穩定」→ 單季 6/8 = **恰好 75%** 判「穩定」(後者才對)

#### 7. 單位
`unit="千元"` 但 FinMind 給的是「元」。2303 顯示「營業現金流 55678697000.00 千元」=
55.7 **兆**元,實際 556.8 億元。全 repo 已無 `千元` 殘留。

#### 8. Swing Score 飽和
`liq_factor = min(成交額, 300百萬)/300`、`vol_factor = min(ATR%, 6)/6`。
2026-08-14 實測 1,977 檔的橫斷面分布:

| | P10 | P25 | P50 | P75 | P90 | P95 | P99 |
|---|---|---|---|---|---|---|---|
| 日均成交額(百萬) | 0.6 | 2.3 | 14.1 | 107.5 | 734.7 | 2,180.9 | 11,250.7 |
| ATR% | 1.59 | 2.36 | 3.71 | 5.37 | 6.88 | 7.59 | 9.25 |

**跨近四個數量級**,固定上限根本沒救:16.5% 的股票流動性頂天、18.5% 波動度頂天,
四檔的當沖/隔日沖**全是 100/100**,零鑑別力。波段也一樣(`liq_factor2` 上限 200 百萬,
20% 頂天)→ 實際公式退化成 `技術面分×0.7 + 30`,**「流動性納入30%權重」那句話是假的**,
等於固定送 30 分地板。

改成**全市場橫斷面百分位**(`_DOLLAR_VOL_PCTL` / `_ATR_PCT_PCTL` 線性內插,
末端用對數收斂到 100 避免超大型股並列)。修正後:

| | 當沖 | 隔日沖 | 波段 | (舊版) |
|---|---|---|---|---|
| 2303 | 92 | 94 | 54 | 100 / 100 / 54 |
| 2337 | 96 | 97 | 81 | 100 / 100 / 81 |
| 2344 | 96 | 97 | 85 | 100 / 100 / 85 |
| 2449 | 92 | 93 | 70 | 100 / 100 / 71 |
| 冷門小型(ATR 2.1 / 3百萬) | 23 | 24 | 43 | — |
| 中型活潑(ATR 5.4 / 110百萬) | 75 | 75 | 65 | — |
| 大型牛皮(ATR 1.8 / 2500百萬) | 47 | 59 | 71 | — |

reasons 文案也改成附百分位(「ATR波動度 6.58%(全市場第 87 百分位)」)。

⚠️ **為什麼校準表寫死在程式碼裡而不是存成資料檔**:`.vercelignore` 排除 `data/`,
即時健檢在 Vercel 上讀不到任何資料檔。存成 `data/meta/*.json` 會變成
**批次有、即時沒有 → 同一檔股票兩條路徑拿到不同分數**。寫死是唯一能一致的做法。
重算工具:`tools/refresh_swing_calibration.py`(印出常數,手動貼回),建議**每半年跑一次**。

### 未修(2 項,待裁示)

**⑨ 盤中未收盤 K 棒被當完整日 K**
yfinance 盤中回傳當日部分 K 棒,`compute_all` 照收 → 所有技術指標與量能均線都吃到半天的量。
用完整 K 棒(到 8/13)重算對照:

| | 量能結構 報告→實際 | 日均成交額 報告→實際 |
|---|---|---|
| 2303 | 0.66 → **0.72** | 23,125 → **24,713** 百萬 |
| 2449 | 1.03 → 1.01 | 5,314 → **5,756** 百萬(−7.7%) |
| 2344 | 1.37 → 1.39 | 25,729 → 24,292 百萬 |

從報告的日均成交額反推 8/14 當下的量:**2303 約 8.3 萬張、2449 約 9,612 張,都只有近20日
均量的 42%**(2344 反推 138%,代表那份是盤中較晚抓的)。2303 因此被寫成「短期量能萎縮至
0.66 倍」列為缺點 —— 講公道話,用完整 K 棒 0.72 一樣會踩到 ≤0.85 的 bad 門檻,
**旗標不是假的,但幅度被放大**。

建議改法(**標記而非丟棄** —— 丟掉當日 K 棒會讓盤中健檢完全看不到今天,反而更糟):
`api/health.py` 判斷最後一根是否為當日且早於 13:35 → `ctx["partial_last_bar"]`;
technical_engine 的 asof 標「(盤中未收盤)」;**量能結構與日均成交額改用不含當日的視窗**
(這兩個才是被半天成交量直接扭曲的,RSI/KD/MACD/ATR 用當前價是合理的);前端加一行提示。

**⑩「近90天新聞」其實等於「近30天」**
`fetch_news(stock_id, name, 60)` 來源只有 60 天,實際 Google News RSS 只回到約 3 週前。
2449 近30天 15 則 = 近90天 15 則,淨情緒 7/30/90 天**全是 0.44**;2344 是 13/13、0.88/0.88。
三個視窗在加權(0.5/0.3/0.2)裡被當獨立資訊,實際上 30/90 是同一組新聞算兩次。
改法:`fetch_news` 天數拉到 90;`news_engine` 若 30 天與 90 天**則數相同**就把 90 天那格
權重併回 30 天,並標 `not_applicable` + 顯示「新聞來源回溯僅至 {最舊日期}」。

**順帶**:融券回補的 5% 門檻沒有絕對量下限 —— 2449 融券 227 張 → 209 張(**差 18 張**,
該檔日成交 2 萬多張)被判「回補中」還列進優點。`chip_engine` 應改成
「比例 ≥ 5% **且** 減少 ≥ 500 張」。

### 順帶發現的批次資料問題(不影響線上報告,線上會現抓)
- `data/chips/2303.parquet` **停在 2026-05-07**(41 列,且缺 margin/short/foreign_holding 三欄)
- `data/financials/2344.parquet` 的 **2026Q2 那列 revenue/gross/op/net 全是 NaN**,只有 EPS 5.40
  (線上因 `len<20` 觸發重抓而躲過,批次會直接用到破的那列)
- `data/prices/2449.parquet` 只有 410 根 K 棒(其他三檔 1,266 根),MA240 剛好跨過門檻

### 驗證
- 全市場 **308 檔**(有本機財報的)跑完四個 Engine + swing_scores,**例外 0**
- 四檔逐項對照修正前後(見上方各節表格)
- 邊界:`swing_scores(None, None, ...)`、ATR=0 / 成交額=0、空 revenue、`_stale_month_index(None)`
