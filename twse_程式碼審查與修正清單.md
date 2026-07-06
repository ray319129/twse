# twse 專案審查報告與修正清單

> 審查日期:2026-07-06
> 審查範圍:全部 scripts/、config/screeners.yaml、.github/workflows/、docs/ 實際輸出(11 天歷史)、data/ 快取
> 結論:工程品質整體良好,但有 4 個確定性 bug(其一使 combo 系統從未運作)、1 個急迫的資料完整性風險、以及實測績效數據指向的 2 個結構性問題

---

## 零、先看你自己的實測數據(performance.json, as of 2026-07-03)

| 指標 | 數值 | 解讀 |
|---|---|---|
| 出場模擬已平倉 | 67 筆 | 樣本還小但方向明確 |
| 已實現勝率 | **14.9%** | 嚴重偏低 |
| 平均淨報酬 | **-2.97%**(扣成本前 -2.39%) | 負期望 |
| 平均持有 | **1.2 天** | 幾乎全數隔天就被踢出 |
| 出場原因 | 均線停損 53.7% + 硬停損 35.8% | 九成是停損出場 |
| 3 日平均報酬 vs 平均最高漲幅 | -1.65% vs **+6.81%** | 票有噴,但你不在車上 |
| 5 日平均報酬 vs 平均最高漲幅 | -2.75% vs **+8.49%** | 同上 |
| signal vs exec delta | -1.8% | 執行層在扣分 |

診斷:選出的票在 3~5 日內確實有 +7~8% 的高點,但(a)momentum 出場規則(收盤破 5MA)太緊,突破股正常回測一天就被洗掉;(b)進場買點偏追高(見結構性問題)。**出場規則是目前績效最大的單一殺手,優先級高於任何選股邏輯調整。**

---

## 一、確定性 Bug(P0,有證據)

### Bug 1:screen_stock 在籌碼/財報資料抓取之前被呼叫 → D/E 策略與全部 combos 從未觸發

**位置**:`scripts/main.py` `_enrich_pick()`
- 第 183 行:`scr = screen_stock(df_ind, screen_cfg, valuation=pick.get("valuation"))` — 只傳 valuation
- 第 194 行:`chips_df = _update_chips(sid, today)` — 在 screen_stock 之後才抓
- 第 200 行:`revenue_df = _update_revenue(sid)` — 同上
- `_update_eps`(第 99 行)只有定義,**從未被呼叫**(data/eps 的 397 個 parquet 是舊版遺留)

**證據**:掃描 docs/history/ 全部 11 天、355 個 picks:
- `inst_consecutive_buy / foreign_holding_increase / short_cover_with_buy / monthly_revenue_growth / eps_positive_high_yield / chip_accumulation` 命中次數:**0**
- combos 非空的 picks:**0**(四個 combo 全部 requires 至少一個籌碼/基本面策略)

**影響**:email 和網頁顯示的 hits 缺整類訊號;combo 系統形同不存在;F4 籌碼吸籌也死。

**修法**:
```python
# _enrich_pick 內,把資料抓取移到 screen_stock 之前:
chips_df = _update_chips(sid, today)
revenue_df = _update_revenue(sid) if fundamentals else None
fin, bal, cf = update_fundamentals(sid) if fundamentals else (None, None, None)
# eps 直接取自 fin(financials parquet 已含 eps 欄),不必另外呼叫 _update_eps 打 API:
eps_df = fin[["eps"]].dropna() if (fin is not None and not fin.empty and "eps" in fin.columns) else None

scr = screen_stock(df_ind, screen_cfg,
                   chips_df=chips_df, revenue_df=revenue_df, eps_df=eps_df,
                   valuation=pick.get("valuation"))
```
並刪除或標註 `_update_eps` 為棄用。修完後跑一次 `--date` 歷史測試,確認 hits 開始出現 D/E 標籤、combos 開始觸發。

---

### Bug 2:量比(vol_ratio)分母包含今日成交量 → 爆量被系統性低估

**位置**:`scripts/indicators.py` 第 78-80 行
```python
out["vol_ma5"] = sma(out["volume"], 5)      # 含今日
out["vol_ratio"] = out["volume"] / out["vol_ma5"]
```

**影響**:
- 真實 3 倍量顯示為 2.14;真實 2 倍量顯示為 1.67;vol_ratio 數學上限 = 5.0
- 現行 1.5 門檻實際等於「今日量 ≥ 前 4 日均量 × 1.71」;2.0 門檻實際等於 × 2.67
- 影響範圍:breakout 觸發、C1 量價齊揚、setup 分數的 volx 項、catalyst_chase 的量能確認

**修法**:
```python
out["vol_ma5_prev"] = sma(out["volume"], 5).shift(1)   # 前 5 日均量(不含今日)
out["vol_ratio"] = out["volume"] / out["vol_ma5_prev"]
```
注意:修正後同一門檻的語意變寬(1.5 的新定義比舊的 1.5 鬆),原本 breakout_vol_ratio 1.5 大致可沿用,但建議修完用 --date 回放幾天對照觸發數量變化。setup 的 vol_ratio_hi 2.0 可考慮上調到 2.5~3.0(修正後分辨率變高,不再被 5.0 上限壓縮)。

---

### Bug 3:籌碼增量抓取起點造成永久 NaN 洞 + 外資持股 30 日窗飄移

**位置**:`scripts/main.py` `_update_chips()` 第 77-89 行

**機制**:三大法人約 16:00 出、融資券約 21:00 出、外資持股隔日出。當天 16:30 跑的時候,last 那天的 margin/short/holding 是 NaN;隔天增量從 `last + timedelta(days=1)` 開始 → **last 那天的缺值永遠補不回來**。

**證據**:`data/chips/3141.parquet`:
```
2026-06-30  inst=995897   margin=4416   short=57   holding=11.25
2026-07-01  inst=50120    margin=NaN    short=NaN  holding=NaN   ← 永久洞
2026-07-02  inst=-413113  margin=4396   short=55   holding=10.94
2026-07-03  inst=1435265  margin=NaN    short=NaN  holding=NaN   ← 下一個洞
```

**連鎖影響**:`_foreign_holding_up(period=30)` 與 `foreign_holding_change_30d` 都用 `dropna()` 後的 iloc 位置當「30 日」— 序列有洞時,實際窗口飄移到 40~60 個日曆日,D2 外資加碼的判定不穩定。

**修法**:
1. 增量起點改重疊回補:`start = last - timedelta(days=4)`
2. `storage.py` 對 chips 的合併改 combine_first 語意(新值優先、新 NaN 不覆蓋舊有值):
```python
combined = new_df.combine_first(cur)   # 取代 concat + duplicated keep="last"
```
3. 外資持股變化改用「日期差」而非「位置差」:
```python
target = s.index[-1] - pd.Timedelta(days=30)
base = s.loc[:target]
delta = float(s.iloc[-1]) - float(base.iloc[-1]) if not base.empty else None
```
4. 跑一次 backfill 把既有快取的洞補掉(重抓近 60 天 chips 合併回去)。

---

### Bug 4:config 宣告但完全未實作的過濾條件

**位置**:`config/screeners.yaml` global 區塊 vs `scripts/fetchers.py` `filter_tradable_stocks()`

grep 全 scripts/ 的結果:
- `min_market_cap`(10 億市值):**無任何實作**(min_dollar_volume 間接頂著,可接受)
- `exclude_full_cash`(全額交割股):**無任何實作**
- `leading.chip_accumulation.max_scan: 120`(安靜股掃描):**無任何實作**(且 F4 因 Bug 1 本來就死)

**風險**:全額交割/處置股沒被排除。處置股採分盤撮合(5 或 20 分鐘一次),流動性瞬間歸零,對隔日沖是實務大忌,而系統目前可能把它選進核心。

**修法(擇一)**:
- 實作:接 TWSE/TPEX 處置股票公告(TWSE 有公開 announcement API),每日抓一次處置名單,在 universe 過濾;或 FinMind `TaiwanStockDispositionSecuritiesPeriod`(需確認免費版可用性)
- 或至少:把 config 中未實作的三行刪除/註明「未實作」,消除假安全感

---

## 二、急迫的資料完整性風險(P0.5,七月除權息旺季)

### yfinance 原始價(auto_adjust=False)的兩個坑

**位置**:`scripts/fetchers.py` `fetch_price_history()` + `scripts/storage.py` `upsert_prices()`

**坑 1 — 除息假跳空**:除息日價格自然下跳(殖利率 4% 的股就是 -4%),污染 ret5 / ret20 / mom / RSI / KD / pullback 判定。**現在是 7 月初,台股除權息旺季即將大量發生**,mom 和趨勢分會被系統性扣分、可能觸發假的回測支撐訊號。

**坑 2 — 減資/分割的混合序列(更嚴重)**:yfinance 遇股票分割或減資會**回溯調整整條序列**,但增量只抓最近 10 天 → 快取是舊尺度、新增量是新尺度,concat 之後均線/動能/一切指標全毀,且不會自我修復(除非快取被刪)。台股減資不罕見。

**最小修法(必做,約十行)** — upsert_prices 加尺度偏移偵測:
```python
overlap = cur.index.intersection(new_df.index)
if len(overlap) > 0:
    ratio = (new_df.loc[overlap, "close"] / cur.loc[overlap, "close"]).dropna()
    if len(ratio) and (abs(ratio - 1) > 0.03).any():
        # 重疊日收盤差異 > 3% → 疑似回溯調整(減資/分割),整段重抓
        full = fetch_price_history(stock_id, market, days=400)
        if not full.empty:
            save_prices(stock_id, full)
            return full
```

**進階修法(選做)**:雙軌價格 — close(原始,用於漲停判定、停損價、實際成交價位)+ adj_close(還原,用於均線/動能/RSI 等指標)。yfinance 的 Adj Close 已含除息還原,`compute_all` 對指標改吃 adj_close。工程量中等,但一勞永逸解掉除息雜訊。

---

## 三、結構性問題(實測數據佐證)

### 3.1 出場規則與進場型態打架(績效最大殺手)

**現況**:momentum 風格 `ma_stop: 5` — 進場當日起,收盤破 5MA 即出場。突破股觸發日收盤通常在 5MA 上方 5~10%,隔日開盤進場後正常回測一天就跌破 5MA。

**證據**:均線停損佔出場 53.7%、平均持有 1.2 天、3 日 maxgain +6.81% 完全沒吃到。

**修正方向(需回測驗證,列選項)**:
1. 進場後前 2 個交易日只用初始停損(結構低),第 3 日起才啟用均線停損(給洗盤空間)
2. momentum 的 ma_stop 從 5 放寬到 10
3. 均線停損改「連續 2 日收盤破線」才出場(過濾單日洗盤)
4. 在 performance 報表加 by_trigger(breakout vs pullback_turn)與 by_style 拆分統計,看哪種進場型態配哪種出場規則——現在的報表看不出這個分層

### 3.2 漲停處理的反效果(v2 規格第 5 點,程式碼與輸出雙重證實)

**現況**:`scoring.py` — `limit_up_today`(漲幅 ≥ 9.5%)單獨即觸發 exhausted → 總分 ×0.55 且 trigger 強制 False。

**實證(docs/history 核心最大漲幅)**:06-30 = 8.79%、07-01 = 9.25%、**07-02 = 9.48%**、07-03 = 7.52%。真正鎖死的漲停(惜售、隔天續攻機率最高)被排除;「衝到 9.48% 沒鎖住」(尾盤被打開、賣壓出籠、恰恰是隔天最易開低的型態)反而進核心。**現行規則精準地留下了錯的那種。**

**修法**:依 v2 規格 — 漲停改複合條件才罰:
```
exhausted_limit = limit_up_today AND (ret5 > 0.22 OR ext_ma20 > 0.18 OR 連續大漲 >= 3 日)
```
盤整區帶量突破的第一根漲停不罰(可另設「首板」標籤加分);同時把「衝高未鎖(漲幅 7~9.4% 但收盤位置 < 0.7)」列入警示,這才是該防的型態。搭配 3.4 的收盤位置指標一起做。

### 3.3 大盤閘門是純裝飾

**證據**:grep `index_below_ma20` — 只出現在 log、email 文案、JSON 欄位,不影響 core_count / min_score / 任何決策。premarket 有美股隔夜閘門,但盤後選股本身無任何盤性調節。

**修法**:index_close 已經抓了,直接照 v2 規格第 1.5 關實作:用加權 vs MA20、(可加抓)騰落與漲跌停家數,產出積極度 → 動態調整 core_count(10 / 7 / 3~5 / 0~3)與 min_score(45 / 50 / 55 / 60),弱盤模式下 trigger 偏好 pullback_turn 而非 breakout。最小版本(只用 index vs MA20 二檔調節)一小時能上線,先求有再求細。

### 3.4 隔日沖偵測完全缺席(v2 第一優先缺口)

現況零偵測。免費資料可先上兩個代理指標(v2 規格第四關):
1. **收盤相對位置** `(close - low) / (high - low)`:> 0.8 加分、< 0.5 扣分(衝高收中段 = 尾盤有人在賣)。零額外 API,high/low 都在 parquet 裡,建議與 3.2 一起實作
2. **當沖比**:FinMind 免費有「當日沖銷交易標的及成交量值」;當沖比 > 40% 扣分/警示

分點旗標留到 Sponsor 階段,依 v2 規格。

### 3.5 其餘 v2 落差(照規格文件執行,此處僅列狀態)

| v2 項目 | 現況 |
|---|---|
| 籌碼進基礎分 25% | 未做(仍為 stage-2 加成,candidate_count 15) |
| 品質估值降至 5% | 未做(仍 15%) |
| 產業加成一級因子 | 未做(industry_bonus enabled: false, weight 4;程式已寫好,先開起來調 weight 即可) |
| combo 共振加分 | 未做(且 combo 因 Bug 1 從未觸發) |
| 新股獨立軌道 | 未做(min_history 120 一刀切) |

---

## 四、值得保留的好設計(不要在重構時弄丟)

- signal vs exec 拆分 + 跳空棄單機會成本分析(skip_cost by_gap/by_horizon)— 這是很成熟的自我審計設計
- 交易成本模擬誠實:手續費折數、滑價、證交稅,且當沖減半稅率只在 hold_days=0 套用
- 到處都是優雅降級(FinMind 死 dataset 短路、Vercel 唯讀檔案系統容錯、NaN 到 JSON 的 _json_safe)
- --date 歷史測試模式可重現、不污染線上資料
- 同日雙觸保守假設先觸停損(出場模擬不自欺)

---

## 五、修正優先順序(可直接當 Claude Code 任務清單)

**P0 — 本週**
1. Bug 1:_enrich_pick 資料抓取順序(修完 --date 回放驗證 hits/combos 開始出現)
2. Bug 5 最小版:upsert_prices 尺度偏移偵測(除權息旺季前必上)
3. 出場規則 3.1:先上「前 2 日只用初始停損」+ 報表加 by_trigger/by_style 拆分,讓後續調參有依據

**P1 — 下週**
4. Bug 2:vol_ratio 分母改 shift(1),回放對照觸發量
5. Bug 3:chips 重疊回補 + combine_first + 日期窗;跑一次 backfill 補洞
6. 3.2 + 3.4-1:漲停複合條件 + 收盤相對位置(同一個 PR,都在 scoring.py)
7. 3.3:大盤閘門最小版(index vs MA20 兩檔調節 core_count/min_score)

**P2 — 之後**
8. Bug 4:處置股過濾(或清掉 config 死參數)
9. industry_bonus 打開 + weight 調 8~10
10. 3.4-2:當沖比接入
11. v2 其餘:籌碼進基礎分、combo 加分、品質降權、新股軌道
12. 進階:雙軌價格(close + adj_close)

**每一步修完的驗證方式**:用 `--date` 對 2026-06-19 ~ 07-03 這 11 天回放,對照 docs/history 舊輸出,確認變化符合預期(例如 Bug 1 修完 hits 應出現 D/E 標籤;閘門上線後弱盤日核心檔數應下降)。

---

## 附:本次審查未發現問題、確認無虞的部分

- 出場模擬無前視偏誤:init_stop 只用訊號日以前的低點;均線/ATR 逐日取值;同日雙觸保守處理(移動停利段內 trail level 使用當日 ATR 有極輕微的日內資訊,影響可忽略)
- breakout 的 20 日高定義(含今日收盤)語意正確
- FinMind 三大法人時效:實測選股日當天 16:30 前已可取得當日資料(3141 07-03 有值),無延遲問題
- _is_trading_day、歷史測試截斷、TPEX 估值快照合併邏輯皆正確
