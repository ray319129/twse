# 台股短線選股系統 — 交接文件 (HANDOFF)

> 給新對話的冷啟動說明。讀完這份就能接續開發。最後更新 commit:`5ae52ec5`（之後以 `git log` 為準）。
> repo: github.com/ray319129/twse · 分支 main · 平台 Windows + Python(CI 用 3.11)

---

## 0. 一句話定位
盤後在 **GitHub Actions** 自動跑的台股**短線**(隔日沖/隔週/月內)選股系統:全市場用免費資料算 0~100 信心分 → 排序出「核心 10 + 觀察 20」→ 寄 Email + 更新互動網頁(GitHub Pages),並**自動追蹤每檔選股後續績效**(含止盈止損出場模擬)。完全免費、無自有伺服器。

使用者(Ray)是**短線交易者**,用富邦證券。重點訴求:不要追高、要能驗證勝率、視覺要現代化。

---

## 1. 架構與資料流

```
GitHub Actions cron 16:30 台北 (.github/workflows/daily.yml)
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
- `scripts/track.py` — **出場模擬 + 績效**:`compute_entry_plan()`(停損/TP1/A·B·C價位線)、`_simulate_exit()`(隔日開盤進場+跳空保護)、`build_report()`(台帳/勝率/各天期/出場統計)。可 `python -m scripts.track` 單獨跑。
- `scripts/indicators.py` — 手刻指標(MA/KD/MACD/RSI/ATR/布林/bb_width)、`compute_relative_strength`(rs_line/rs_ratio,需大盤)。
- `scripts/screener.py` — 舊 12 策略 + 4 combo + 4 領先訊號(現降為自選池標籤用)。
- `scripts/fetchers.py` — yfinance 價格/指數(**已 dropna(close)**)、FinMind 籌碼/財報、TWSE 估值、Google News。
- `scripts/storage.py` — parquet 讀寫;`load_prices` **讀取時忽略 NaN 收盤列**。
- `scripts/{config,industry,notify,utils}.py`、`templates/daily_email.html`、`docs/index.html`(SPA)。
- `config/screeners.yaml` — 所有可調參數(見下)。
- `data/` — prices/、signals/{date}.json、performance.json、meta/。`docs/` — data.json、dates.json、history/{date}.json。

### config/screeners.yaml 可調區塊
- `ranking`: core_count 10 / watch_count 20 / min_score 45 / min_dollar_volume 3000萬 / enrich_top_n 30
- `entry`: max_chase 0.03(隔日開盤 ±3% 以上跳空棄單)
- `exit`: hard_stop 0.07 / r_multiple 2.0 / max_hold_days 30 / momentum{struct_lookback 2, ma_stop 5, trail_ma 5} / swing{10,20,10} / trail{atr_mult 1.5, min_pct .03, max_pct .07}

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

## 4. 離線測試法(關鍵,無需網路/API)
本機有 ~1976 個 `data/prices/*.parquet`(到約 6/18)。可 monkeypatch 網路函式跑完整 `daily_run`:
```python
import scripts.main as M
M.fetch_stock_info=lambda *a,**k: <cached info df>
M.fetch_valuation_snapshot=lambda *a,**k: {}
M.fetch_index_history=lambda *a,**k: <equal-weight proxy from closes>
M.fetch_price_history=lambda *a,**k: pd.DataFrame()   # 不動 data 檔
M.fetch_chips_history / fetch_monthly_revenue / fetch_eps_quarterly = lambda: pd.DataFrame()
M.fetch_news=lambda *a,**k: []
M.send_email=lambda *a,**k: None
M.daily_run(test_mode=True)
# 之後務必 git checkout -- data/ ; rm 測試產生的 docs/*.json 與 data/signals/<today>.json
```
驗證 JSON:`json.loads(text, parse_constant=lambda c:(_ for _ in()).throw(ValueError(c)))`。
驗證網頁 JS:抽出 `<script>` 內容 `node --check`。

---

## 5. 目前狀態 / 立即待辦(使用者動作)
- ⏳ **線上 docs/data.json 還是 6/18(bug 期間)那份,核心 0 檔**。需到 GitHub **Actions → Daily Screener → Run workflow** 觸發一次,才會用修好的程式產生 核心10 + 決策卡 + 乾淨 JSON;Pages 重部署後網頁才完整。
- GitHub Pages 已啟用(/docs),網頁可載入。

## 6. 下一步任務(已規劃、尚未做)
1. **【優先】交易成本納入出場模擬**:手續費(0.1425%×折扣,買賣各一)+ 證交稅 0.15%(賣出)+ 滑價。讓已實現勝率/報酬接近真實到手 → 這是驗證「有沒有 edge」最關鍵的一塊。
2. **累積 1~2 個月真實數據後,用績效回頭調評分權重**(哪種 profile / 分數區間 / 觸發型態真有 edge)。
3. **盤中執行層(獨立大專案)**:富邦 API 即時報價 + 開盤區間/VWAP/帶量吞噬判讀 + 提醒或半自動下單。需盤中持續運行的程式(非 Actions)+ 資安考量。日線測不出盤中順序,這層才能真正執行 A/B/C。
4. (選配)自選池/觀察層也納入績效追蹤;大盤/產業過濾進階規則。

## 7. 必記前提(誠實風險)
- **策略尚未驗證有 edge**:樣本太少、過去只經歷多頭、回測未扣成本。**先紙上跟單、讓【五】歷史追蹤累積真實已實現勝率再說**,別急著實盤或斷言穩賺。
- 短線扣 0.15% 證交稅 + 手續費後要穩定贏 0050 非常難。系統價值在「縮小該盯的範圍 + 擋追高 + 客觀記錄績效」,不是印鈔機。

---

## 8. 慣例
- commit 訊息用繁中,結尾加 `Co-Authored-By: Claude ...`;改完先 py_compile + 離線跑一次驗證再 push;push 前常需 `git pull --rebase`(每日 workflow 會 commit data)。
- 別把使用者根目錄的「新增 文字文件.txt」(他貼的郵件原文)commit 進去。
