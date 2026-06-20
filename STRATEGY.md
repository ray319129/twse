# 台股短線選股策略 — 現行實作說明(供檢查與優化)

> 本文描述「**實際在跑的程式邏輯**」(以 code 為準,非舊 spec 理想)。對照 commit 以 `git log` 為準。
> 數值若與 `config/screeners.yaml` 不同,**以 config 為準**;標「(硬寫)」者代表寫死在 `.py`、目前不可由 config 調(見第 10 節)。

---

## 0. 一句話與總流程

盤後在 GitHub Actions 對全市場(約 1900 檔)用**免費資料**算一個 **0~100 信心分**,排序後只取「核心 10 + 觀察 20」,再對核心補抓籌碼/財報、算進場決策卡與歷史出場績效。

```
全市場 → 過濾可交易 → 逐檔(本機 parquet 增量)算指標 + 相對強度
      → compute_conviction 信心分(只用價格+估值)
      → 排序:核心=「今天觸發 且 分≥門檻」前10;觀察=「醞釀中」前20
      → 只對核心+自選補 FinMind 籌碼/財報 + 算 A/B/C 進場卡
      → 出場模擬(隔日開盤進場+移動停利)→ 績效台帳
```
主檔:[scripts/main.py](scripts/main.py) `daily_run()`。

---

## 1. 選股宇宙與過濾

| 步驟 | 規則 | 位置 |
|---|---|---|
| 來源 | FinMind `TaiwanStockInfo`(本機快取,月更) | [fetchers.py:47](scripts/fetchers.py) |
| 代號 | 只留 **4 位數字**;排除 `00xx`(ETF)、`91xx`(TDR) | [fetchers.py:75](scripts/fetchers.py) |
| 板別 | 只留 `twse`(上市)/`tpex`(上櫃) | 同上 |
| 板塊 | 排除含「創新版/創新板」 | [main.py:38](scripts/main.py) |
| 資料長度 | 價格 K 棒 **< 120 根直接跳過** | [main.py:283](scripts/main.py) |
| 流動性 | 日均成交額 = 收盤 × 20日均量 **< 3000 萬元 → 淘汰** | [scoring.py:49](scripts/scoring.py) |

---

## 2. 信心分(核心)— 五面向加權 + 過熱重罰

函式 [scoring.py:27 `compute_conviction`](scripts/scoring.py)。資料不足或流動性不足回 `None`(直接出局)。

**總分公式**（[scoring.py:151](scripts/scoring.py)）:
```
raw = 100 × (0.25·趨勢 + 0.25·相對強度 + 0.25·時機量能 + 0.15·品質 + 0.10·流動性)
若 exhausted(過熱):raw × 0.55
```
五個面向各自是 0~1。

### 2.1 流動性門檻 + 流動性分(權重 10)
- 門檻:`日均成交額 < min_dollar_volume(3000萬)` → 淘汰。
- 分數:`clip01( log10(額/3000萬) / log10(50) )` → 3000萬≈0 分、約 15 億≈1 分(對數)。([scoring.py:49-54](scripts/scoring.py))

### 2.2 趨勢健康(權重 25)
5 個布林條件各 1/5,全中 = 1.0([scoring.py:57-64](scripts/scoring.py)):
1. 收盤 > 5MA
2. 20MA > 60MA
3. 收盤 > 60MA
4. 60MA > 120MA
5. 60MA > 21 個交易日前的 60MA(季線上揚)

### 2.3 相對強度(權重 25)
- 主分:`rs_ratio`(個股60日報酬 ÷ 大盤60日報酬)線性映射 **0.95→0、1.30→1**(硬寫)。
- 加成:若 `rs_line`(個股/大盤比值線)今日 > 11 日前(仍在轉強),分數拉抬 `rs×0.8+0.2`。([scoring.py:66-76](scripts/scoring.py))
- 大盤 = yfinance `^TWII`,整個 run 抓一次。

### 2.4 短線時機 / 量能(權重 25)
`setup = 0.45·volx + 0.25·vbias + 0.30·mom`([scoring.py:79-89](scripts/scoring.py)):
- `volx` = 今日量比 `vol_ratio`(量/5日均量)映射 0.8→0、2.0→1
- `vbias` = `vol_ma5/vol_ma20` 映射 0.8→0、1.6→1(量能結構轉強)
- `mom` = 近 **10 日報酬 / 15%**(10日漲15%→1)

### 2.5 品質估值(權重 15)
用 TWSE 估值快照(本益比/殖利率/股價淨值比)平均([scoring.py:92-103](scripts/scoring.py)):
- PE:`0<PE≤25→1`、`≤40→0.5`、`>40→0.1`
- 殖利率:`clip01(殖利率/5%)`
- PB:`0<PB≤3→1`、`≤6→0.5`、其餘 0.2
- **沒有估值資料 → 給中性 0.5**(注意:估值快照只涵蓋上市;上櫃股常拿 0.5)。

### 2.6 過熱重罰(exhausted)
任一成立即 `raw × 0.55`([scoring.py:106-119](scripts/scoring.py)):
- 近 5 日漲 > **22%**,或
- 乖離 20MA > **18%**,或
- RSI14 > **88**,或
- 今日漲停(漲幅 ≥ 9.5%)

### 2.7 觸發 / 醞釀 旗標(決定進核心或觀察)
- **breakout(帶量突破)**:收盤 ≥ 近20日收盤高 **且** 量比 ≥ **1.5** **且** 收 > 開。([scoring.py:122-128](scripts/scoring.py))
- **pullback_turn(回測轉強)**:季線上揚 + 距20MA ≤4% + 收盤 ≥ 60MA×0.98 +（收紅 或 K≥D）。([scoring.py:129-135](scripts/scoring.py))
- **trigger = (breakout 或 pullback_turn) 且 非過熱**。
- **coiling(收斂蓄勢)**:布林帶寬 ≤ 近120日20%分位 **或** ≤ 0.06。
- **brewing(醞釀)= 非觸發 且 非過熱 且 收盤>60MA 且（coiling 或 rs 轉強)**。([scoring.py:139-148](scripts/scoring.py))

### 2.8 風格分類 profile([scoring.py:155-160](scripts/scoring.py))
- `rs + setup > trend + quality + 0.30` → **動能**
- `quality≥0.70 且 trend≥0.60` → **品質**
- 其餘 → **均衡**
（profile 同時決定出場走「動能流/波段流」,見第 5 節。）

### 2.9 權重與門檻速查表

| 面向 | 權重 | 0 分 | 1 分 | 可調? |
|---|---|---|---|---|
| 趨勢健康 | 25 | 5 條件全不中 | 全中 | `scoring.weights.trend` |
| 相對強度 | 25 | rs_ratio 0.95 | rs_ratio 1.30 | `scoring.weights.rs` / `scoring.rs.*` |
| 時機量能 | 25 | — | — | `scoring.weights.setup` / `scoring.setup.*` |
| 品質估值 | 15 | 貴/無資料 0.5 | 便宜+高息 | `scoring.weights.quality` / `scoring.quality.*` |
| 流動性 | 10 | 3000萬 | ~15億 | `scoring.weights.liquidity` / `ranking.min_dollar_volume` |
| 過熱罰 | ×0.55 | — | — | `scoring.exhausted.penalty` |

> ✅ **所有信心分門檻/權重已抽進 `config/screeners.yaml` 的 `scoring:` 區塊**(預設值 = 上述原值);改 config 即可調參,不必動 `.py`。

---

## 3. 排序 → 候選 →(籌碼 stage-2 重排)→ 核心 / 觀察

[main.py](scripts/main.py)、參數 `config/screeners.yaml: ranking` + `scoring.chip_bonus`:
1. **候選池** = `trigger 且 score ≥ min_score(45)` 依基礎信心分降序取前 `candidate_count(15)`(略多於核心數,受 `enrich_top_n` 上限保護 API)。
2. **enrich**:對候選補抓 FinMind 籌碼/財報 + 算決策卡。
3. **籌碼 stage-2 重排**([`_rank_core`](scripts/main.py)):`rank_score = 信心分 + chip_bonus`,`chip_bonus = weight(10) × chip_signal`。
   - `chip_signal`(0~1)= 法人連買天數 / 30日外資持股變化 / 今日法人淨買 / 融券回補 四項平均(有幾項算幾項)。
   - **無籌碼資料 → bonus 0(中性、不扣分)**,避免資料覆蓋偏差。
   - `score`(信心分)語意不變;只有排名與顯示的「籌碼 +X」用 `rank_score`/`chip_bonus`。
4. **核心** = 候選依 `rank_score` 降序取前 `core_count(10)`(籌碼可改變誰進核心)。
5. **觀察** = `brewing 且 非 trigger 且 score ≥ 45 且 不在核心`,取前 `watch_count(20)`(觀察層不抓籌碼、不加成)。
6. 熱門產業標記:產業近況排序取前 5,核心/觀察標 🔥。

> 為何用 stage-2 而非全市場評分:FinMind 免費額度無法對 ~1900 檔抓籌碼,故先用免費資料選出候選,再用「已抓到的籌碼」決定誰進核心。`scoring.chip_bonus.enabled: false` 可關閉、回到純信心分排序。

---

## 4. 進場決策卡(A/B/C)

只對核心(+自選)算,函式 [track.py:89 `compute_entry_plan`](scripts/track.py)。以昨收 `ref` 為基準:

| 欄位 | 算法 |
|---|---|
| `init_stop` 初始停損 | `max(近 N 根結構低, ref×(1−hard_stop 7%))`;若風險太小退回 7% 底 |
| `R`(風險) | `ref − init_stop` |
| `tp1` 第一停利 | `ref + r_multiple(2.0) × R`(即 1:2) |
| `max_entry` 進場上限 | `ref × (1 + max_chase 3%)` |
| `flat_lo/hi` 平盤帶 | `ref×0.99 / ref×1.01` |
| `gap_up_line` 開高線 | `ref×1.015` |
| `gap_dn_line` 開低線 | `ref×0.99` |
| `tp1_resistance` | TP1 上方 5% 內有近 60 日高點壓力則標記 |

隔日開盤分 **A 平盤 / B 開高 / C 開低** 三劇本,盤中照表手動執行(盤前自動看盤會用這些線分類,見第 8 節)。

---

## 5. 出場模擬(R 倍數 + 移動停利)

函式 [track.py:117 `_simulate_exit`](scripts/track.py),參數 `config: exit/entry`。算「真實已實現勝率」用:

1. **進場 = 隔日開盤**(非選股日收盤),做跳空保護:
   - 開盤較選股收盤高 > `max_chase(3%)` → **跳空開高棄單**
   - 開盤低於 −max_chase,或開盤已 ≤ 初始停損 → **棄單/作廢**
2. 進場後,**未達 TP1 前**:盤中破初始停損 → 止損;收盤跌破均線(動能 5MA / 波段 20MA)→ 均線停損。
3. **觸 TP1(高點 ≥ tp1)後啟動移動停利**:回檔幅度用 **ATR 自適應**(`1.5×ATR%`,夾在 3%~7%);收盤跌破移動均線(動能 5MA / 波段 10MA)出清。
4. 同日同時觸停損與 TP1 → **保守假設先觸停損**(寧可低估獲利)。
5. 風格分流:`profile=動能 或 breakout` → 動能流(停損緊、看 5MA);否則波段流(看 20MA)。
6. 最長持有 `max_hold_days(30)`,到期以收盤出場。

> ⚠️ **目前出場模擬未扣交易成本**(手續費/證交稅/滑價)— 這是 HANDOFF 列的第一優先待辦。

---

## 6. 技術指標公式([scripts/indicators.py](scripts/indicators.py))

全部手刻(避免 pandas-ta 在 Actions 出包):
- 均線 SMA:5/10/20/60/120/240
- KD:`kd(9,3,3)`,RSV 的 EMA 平滑(alpha=1/3)
- MACD:`(12,26,9)`,`hist=(DIF−DEA)×2`
- RSI:14,Wilder EMA(alpha=1/14)
- ATR:14,TR 的 N 日均
- 布林:`(20, 2.0)`;`bb_width=(上−下)/中軌`
- 量:`vol_ma5/vol_ma20`、`vol_ratio=量/vol_ma5`
- 相對強度:`rs_line=個股/大盤`、`rs_ratio=個股n日報酬/大盤n日報酬`(n=60)([indicators.py:87](scripts/indicators.py))

---

## 7. 自選池標籤 / 領先訊號 / combo(**非**核心選股,僅標籤用)

[scripts/screener.py](scripts/screener.py) 的 12 策略 + 4 領先訊號 + 4 combo **已被信心分取代**,目前只用來對「自選池」與核心補上文字標籤。enabled 由 `config/screeners.yaml` 控:

- A 趨勢:多頭排列 / 黃金交叉 / 突破季線 / 站上年線
- B 動能:KD 低檔黃金交叉 / MACD 翻紅 / RSI 突破 50
- C 量價:量價齊揚 / N 日新高
- D 籌碼:法人連買 / 外資加碼 / 融券回補+主力買超
- E 基本面:月營收連續成長 / EPS 正+高殖利率（殖利率已修為可退用估值快照,[screener.py:268](scripts/screener.py)）
- F 領先/醞釀:盤底蓄勢 / 回測支撐 / 相對強勢領頭羊 / 籌碼吸籌([screener.py:286+](scripts/screener.py))
- combo(交集):主升段啟動 / 底部反轉 / 強勢續攻 / 保守存股

---

## 8. 盤前自動看盤(premarket)— 詳見 [HANDOFF.md](HANDOFF.md) 第 9 節

[scripts/premarket.py](scripts/premarket.py):
- **大盤閘門**:美股隔夜 SOX / NASDAQ期 / S&P期 / VIX 投票 → risk-on / 中性 / risk-off([premarket.py `compute_gate`](scripts/premarket.py))。
- **個股分類**:用 MIS 試撮價套第 4 節的決策卡線 → A平盤 / B開高 / C開低 / ❌棄單(過進場上限)/ ❌作廢(破停損)。
- **ORB**:對 A 股抓開盤 15 分 1分K,判 09:15 後**帶量突破開盤區間高**(`premarket.orb.volume_filter`)。
- ADR 佐證(2330→TSM 等)。

---

## 9. 所有可調參數一覽(`config/screeners.yaml`)

| 區塊 | 關鍵參數(預設) |
|---|---|
| `ranking` | core_count 10 / watch_count 20 / **min_score 45** / min_dollar_volume 3000萬 / enrich_top_n 30 |
| `scoring` | **(新)** weights 五面向 / rs / setup / quality / exhausted / trigger / brewing / profile 全部門檻 |
| `scoring.chip_bonus` | **(新)** enabled / candidate_count 15 / weight 10 / streak_full / foreign_full / short_cover_thr — 核心候選的籌碼 stage-2 重排 |
| `entry` | max_chase **3%**(隔日開盤追價上限) |
| `exit` | hard_stop **7%** / r_multiple **2.0** / max_hold_days 30 / momentum{struct_lookback 2, ma_stop 5, trail_ma 5} / swing{10,20,10} / trail{atr_mult 1.5, min 3%, max 7%} |
| `leading` | 四個領先訊號的 lookback/門檻(僅標籤用) |
| `premarket` | gate 門檻 / orb(15分,09:30,帶量) / adr 對照表 |

---

## 10. 已知限制與「可優化點」清單(給你檢查 — 尚未改動)

**A. ✅ 已完成:信心分參數已全部抽進 `config/screeners.yaml` 的 `scoring:` 區塊**(權重、相對強度映射、動能、量能、過熱門檻、突破/回測、醞釀、風格)。預設值 = 原硬寫值(已驗證 12/12 檔分數不變)。現在可純靠改 config 系統化調參。

**B. 品質面向偏弱**:無估值就給 0.5;上櫃股估值快照常缺 → 大量股票品質固定 0.5,15% 權重等於半失效。可考慮上櫃估值來源,或無資料時降權而非給 0.5。

**C. 面向之間可能重疊/雙算**:`setup` 的 mom(10日漲幅)與「過熱罰」(5日22%)方向相反互相拉扯;trend 與 rs 也部分相關。可檢查是否造成「漲多的分數先被加再被罰」。

**D. 出場未扣成本**:勝率/報酬偏樂觀(HANDOFF 第一待辦)。在驗證 edge 前,所有勝率都要打折看。

**E. breakout 用「收盤」近20日高、量比對的是 5日均量**:可考慮改用「最高價」突破與對 20 日均量,定義更嚴謹。

**F. 大盤只用 ^TWII 月線位階(index_below_ma20)當背景**,未進信心分;可考慮把大盤環境納入分數或核心檔數調節。

**G. min_score 45 是否合適**:多頭期容易塞滿 10 檔,空頭期可能湊不到;可隨大盤環境動態調。

**H. 樣本與市況偏誤**:回測只經歷多頭、樣本少 → 任何權重優化都要等真實已實現績效累積(至少 1~2 個月)再做,否則是過擬合。

---

### 建議的優化順序(個人觀點)
1. ~~把 scoring 的硬寫參數抽進 config~~ ✅ 已完成。
2. **出場加交易成本** → 拿到「接近真實」的勝率基準。
3. 累積 1~2 個月實單/紙上績效後,用「哪種 profile / 分數區間 / 觸發型態真有 edge」回調權重(A、C、G)。
4. 再處理品質面向(B)與大盤環境納入(F)。
