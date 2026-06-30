# 個股健檢(Stock Health)v1

## 為什麼做

既有系統只服務「短線排序」(核心10+觀察20),回答的是「現在哪幾檔可以進場」。使用者要的是另一個問題:
「這家公司現在到底值不值得投資」——不是一個分數,是**可解釋、可回推、可換投資風格**的完整健檢報告。

程式見 [`scripts/health/`](../scripts/health/),orchestrator 是 [`engine.py`](../scripts/health/engine.py),
參數在 `config/screeners.yaml` 的 `health:` 區塊。

## 可解釋性契約(所有 Engine 共用)

每個指標一律是同一種 `Metric` dict(`scripts/health/metric.py`):`value`、`unit`、`trend`(歷史序列)、
`industry_avg`(同業平均)、`status`(improving/worsening/stable)、`rating`(good/neutral/bad)、`formula`(怎麼算的)、
`source`(資料源)、`asof`(資料時點)、`updated_at`、`missing_reason`(缺資料就誠實標原因,不是沉默留白)。
前端用**一個泛型 renderer**(`metricRow()`)吃所有 Engine,新增指標/新增 Engine 不必改前端。

每個 Engine 回傳 `{score: 0~100|None, metrics: [...], notes: [...]}`。`score=None` 代表該面向資料不足,
**不計入總分**(不是硬湊 0 分),沿用 `fundamentals.fundamental_score()` 既有「缺幾項就算幾項」的精神。

## 七個面向 + 1 個診斷層

| Engine | 檔案 | 涵蓋 |
|---|---|---|
| 財務體質 | `financial_engine.py` | 毛利/營益/淨利率、ROE/ROA(近似)、流動比/速動比、負債比、利息保障倍數、營業現金流/自由現金流/現金流穩定度、淨利現金流背離 |
| 成長能力 | `growth_engine.py` | 月營收YoY+連續成長月數、季營收/營益/淨利/EPS YoY、5年CAGR(需≥20季財報)、營收/獲利是否創新高 |
| 估值分析 | `value_engine.py` | PE/PB/殖利率、PE/PB歷史百分位(自身5年序列)、PEG、EV/EBITDA(市值用PB×權益近似)、現價是否低於PE均值回歸估算合理價、**選配 DCF**(不計分,假設攤開) |
| 風險分析 | `risk_engine.py` | Tier1(連續虧損/連續營收衰退/現金流背離/負債比年增/月營收暴跌/應收存貨異常)規則式,任一 Critical 組合規則命中 → 總分強制封頂;Tier2(質押/違約/減資/重編)誠實標「無免費資料源,僅靠新聞」 |
| 技術面 | `technical_engine.py` | 均線排列、ADX趨勢強度(新指標)、RSI、MACD、KD、量能結構、ATR波動度、支撐壓力(複用既有 `reference_levels()`)、現況站上/跌破關鍵區間 |
| 籌碼分析 | `chip_engine.py` | 三大法人連買天數/今日淨買/近5日淨買、外資持股趨勢、融資5日變化、融券回補、大戶持股比例、股東人數、日均成交金額 |
| 新聞分析 | `news_engine.py` | 沿用 `catalyst.py` 零幻覺呼叫模式,**單次** Haiku 呼叫逐則新聞標記(sentiment/durability/impact/confidence+evidence原文引用),本地依已知發布日期分桶成7/30/90天三視窗,零額外LLM成本 |
| AI解讀 | `ai_summary.py` | **規則先產生所有事實句**(數字→if/else→固定句型),LLM 只負責潤飾語句(prompt強制不能新增數字/結論),規則句永遠保留可切換查看 |

Final Scoring(`scoring.py`):三組可切換投資風格權重(財務/成長/估值/風險/技術/籌碼/新聞):

| 面向 | 價值投資 | 成長投資 | 短線交易 |
|---|---|---|---|
| 財務體質 | 30 | 20 | 5 |
| 成長能力 | 10 | 30 | 5 |
| 估值分析 | 30 | 10 | 0 |
| 風險分析 | 20 | 15 | 15 |
| 技術面 | 5 | 15 | 35 |
| 籌碼分析 | 0 | 5 | 25 |
| 新聞分析 | 5 | 5 | 15 |

任一面向缺資料 → 從加權分母剔除、剩餘權重等比例重分配(不是偷偷補0或補中性值掩蓋)。
Risk 命中 Critical 規則時總分強制封頂(預設 40 分),不被其他面向稀釋——這是唯一不走加權平均的例外。
星等診斷(★1~5 + 文字標籤)是純規則對照表,**不是 LLM 生成**。

Swing Score(短線評估):當沖/隔日沖/波段/中長線四個 0~100 適合度,純規則組合 ATR波動度+日均成交額+
技術面分數+財務體質分數,不是新邏輯,也不重打任何 API。

## 雙路徑架構(為什麼需要)

GitHub Pages 是純靜態,沒有地方執行「使用者剛打代號→即時抓資料→算分」。設計成兩條路徑共用同一套引擎:

- **路徑 A(批次,免費,核心+自選池)**:`main.py daily_run()` 在既有 stage-2 enrichment 之後跑健檢,
  寫 `docs/health/{代號}.json`,瞬開、零延遲、零額外 API 預算風險(沿用既有「只對入榜名單補抓 FinMind」紀律)。
- **路徑 B(即時,選用,任意代號)**:`api/health.py`(Vercel Python Serverless Function),`vercel.json` +
  `VERCEL_SETUP.md`。預設關閉,需額外部署。詳見 HANDOFF.md 第 10 節與 VERCEL_SETUP.md。

## 同業平均怎麼來的(零額外成本)

全市場 ~1900 檔不可能天天對每檔抓 FinMind 財報。`industry_benchmark.py` 吃「核心榜每天輪動」這個
**本來就會發生**的副產品:掃描本地已累積的 `data/financials|balance/*.parquet`,依產業彙總平均值,
樣本數 < 3 的產業整個跳過(不產生假裝有同業平均的數字)。寫 `data/health/industry_benchmarks.json`,
隨 `docs/` 一起發布,即時路徑直接讀這份靜態檔,不必自己重新聚合全市場。

## 老實話(現況邊界,別假裝做完了)

- 這次只做到本機合成資料驗證,沒有真實 FinMind token 跑過一次完整流程。
- `_BS_TYPES`/`_CF_TYPES` 新增的財報欄位候選名稱、`fetch_holder_distribution()` 解析的集保分散表欄位,
  都**未經真實 API 回應驗證**——命中失敗只會讓對應指標顯示「資料不足」,不會讓流程當掉,但不代表已經調好。
- 董監質押/重大違約/重大減資/財報重編四項風險,**沒有確認可行的免費資料源**,僅靠新聞最佳努力涵蓋。
- 三組投資風格權重、Tier1 風險規則的 penalty 數字,都是設計時的合理猜測,沒有用真實結果驗證過。
- Vercel 即時路徑從沒真的部署測試過,serverless function 體積(reuse 含 yfinance 的整包 scripts/)有沒有
  超過平台上限也沒驗證過,見 VERCEL_SETUP.md 的風險清單。
