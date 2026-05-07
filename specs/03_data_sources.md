# 資料源對照表

## 一覽

| 資料類別 | 主要來源 | 備援 | 更新頻率 | 免費 |
|---|---|---|---|---|
| 上市日成交 | TWSE 官方 | FinMind | 每日 14:30 後 | ✅ |
| 上櫃日成交 | TPEX 官方 | FinMind | 每日 14:30 後 | ✅ |
| 三大法人 | TWSE / TPEX | FinMind | 每日 16:00 後 | ✅ |
| 融資融券 | TWSE / TPEX | FinMind | 每日 17:00 後 | ✅ |
| 月營收 | MOPS | FinMind | 每月 10 號 | ✅ |
| 季財報 | MOPS | FinMind | 每季法定日 | ✅ |
| 公司基本資料 | TWSE 上市清單 + MOPS | FinMind | 每月更新 | ✅ |
| 重大訊息 | MOPS | - | 即時(每日抓 1 次) | ✅ |
| 個股新聞 | Google News RSS | - | 即時 | ✅ |
| 殖利率/EPS/PE | TWSE BWIBBU | FinMind | 每日 | ✅ |

---

## 1. TWSE 證交所(上市)

**根網址**:`https://www.twse.com.tw`

### 全市場日成交(推薦,一次抓完)
```
GET https://www.twse.com.tw/exchangeReport/MI_INDEX
?response=json&date=YYYYMMDD&type=ALL
```
回傳:當日所有上市股票的 OHLCV、漲跌、成交筆數

### 個股歷史日 K(補歷史用)
```
GET https://www.twse.com.tw/exchangeReport/STOCK_DAY
?response=json&date=YYYYMMDD&stockNo=2330
```

### 三大法人買賣超(個股)
```
GET https://www.twse.com.tw/fund/T86
?response=json&date=YYYYMMDD&selectType=ALL
```

### 融資融券
```
GET https://www.twse.com.tw/exchangeReport/MI_MARGN
?response=json&date=YYYYMMDD&selectType=ALL
```

### 個股 PE / 殖利率 / 股價淨值比
```
GET https://www.twse.com.tw/exchangeReport/BWIBBU_d
?response=json&date=YYYYMMDD&selectType=ALL
```

**Rate limit**:沒明文,但連打太快會被 ban IP 5 ~ 10 分鐘。**建議每次 API 間隔 ≥ 3 秒**。

**注意**:
- date 是 YYYYMMDD 西元
- 假日不開盤,要先檢查交易日曆
- JSON 結構不算友善,需要自己解析

---

## 2. TPEX 櫃買中心(上櫃)

**根網址**:`https://www.tpex.org.tw`

API 結構跟 TWSE 不一樣,要分開寫。**日期格式是「民國年/MM/DD」**(例如 `115/05/08`)。

### 上櫃日成交
```
GET https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php
?l=zh-tw&d=YYY/MM/DD
```

### 三大法人(上櫃)
```
GET https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php
?l=zh-tw&se=AL&t=D&d=YYY/MM/DD
```

### 融資融券(上櫃)
```
GET https://www.tpex.org.tw/web/stock/margin_trading/margin_balance/margin_bal_result.php
?l=zh-tw&d=YYY/MM/DD
```

> ⚠️ TPEX 的 URL 結構偶爾改版,實作時遇到 404 要去官網查最新。

---

## 3. MOPS 公開資訊觀測站

**根網址**:`https://mops.twse.com.tw`

MOPS 是 ASP.NET WebForms 做的網站,**不是友善的 API**,常見方式是 POST form data。

### 月營收(每月 10 號公告)
要解析 HTML table 或用第三方包裝。

### 重大訊息
```
GET https://mops.twse.com.tw/mops/web/t05st01
```
要 POST 篩選條件。

### 公司基本資料
```
GET https://mops.twse.com.tw/mops/web/ajax_t05st03
```

**建議**:**MOPS 的東西全部用 FinMind 抓,不要自己打 MOPS,會浪費生命**。

---

## 4. FinMind ⭐ (主要資料源)

**根網址**:`https://api.finmindtrade.com`

**註冊**:[finmindtrade.com](https://finmindtrade.com)(免費版每小時 600 次)

**Token**:在 GitHub Secrets 設 `FINMIND_TOKEN`

### 常用端點

| dataset | 用途 |
|---|---|
| `TaiwanStockPrice` | 個股日 K |
| `TaiwanStockInfo` | 上市櫃股票清單 |
| `TaiwanStockInstitutionalInvestorsBuySell` | 三大法人 |
| `TaiwanStockMarginPurchaseShortSale` | 融資融券 |
| `TaiwanStockHoldingSharesPer` | 外資持股比 |
| `TaiwanStockMonthRevenue` | 月營收 |
| `TaiwanStockFinancialStatements` | 財報 |
| `TaiwanStockDividend` | 股利 |
| `TaiwanStockPER` | 個股 PE/殖利率 |

**範例**:
```python
import requests

def fetch_finmind(dataset: str, **params):
    url = "https://api.finmindtrade.com/api/v4/data"
    params = {"dataset": dataset, "token": FINMIND_TOKEN, **params}
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()["data"]

data = fetch_finmind(
    "TaiwanStockPrice",
    data_id="2330",
    start_date="2024-01-01",
)
```

**策略**:**優先用 FinMind**(穩定、結構乾淨),官方 API 當備援。

---

## 5. Google News RSS(新聞)

```
https://news.google.com/rss/search?q={QUERY}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant
```

QUERY 範例:`2330+台積電`(代號 + 公司名 URL-encoded)

回傳 RSS XML,Python 用 `feedparser` 解析。

**為什麼選 Google News**:
- ✅ 沒 CORS 問題
- ✅ 涵蓋鉅亨、經濟日報、工商時報、Yahoo 股市、ETtoday、自由時報、聯合新聞網
- ✅ 免費、無 token、穩定
- ✅ 自動排序

**範例**:
```python
import feedparser
import urllib.parse

def fetch_news(stock_id: str, name: str, limit: int = 20):
    q = urllib.parse.quote(f"{stock_id} {name}")
    url = f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    feed = feedparser.parse(url)
    return [
        {"title": e.title, "link": e.link, "published": e.published, "source": e.source.title}
        for e in feed.entries[:limit]
    ]
```

---

## 6. 公司基本資料(產業、產品)

每月抓一次即可。

### TWSE 上市/上櫃公司清單
```
https://isin.twse.com.tw/isin/C_public.jsp?strMode=2  # 上市
https://isin.twse.com.tw/isin/C_public.jsp?strMode=4  # 上櫃
```
回傳 HTML table,解析後得到代號、名稱、上市日、產業別。

### MOPS 公司詳細(英文簡介、產品線、董事長、官網)
直接用 FinMind `TaiwanStockInfo`,簡單很多。

---

## 排程建議(台北時間)

| 時間 | 任務 |
|---|---|
| 14:35 | 抓上市/上櫃當日成交、PE/殖利率 |
| 16:05 | 抓三大法人、融資券、外資持股 |
| 16:30 | 計算指標、跑選股、寫 JSON |
| 16:35 | 抓自選股新聞(RSS) |
| 16:40 | 寄 Email |

每月 11 號早上多一次:抓月營收、更新公司基本資料。

> 由於 GitHub Actions cron 用 UTC,且 runner 不保證準時(可能延遲幾分鐘),實際 cron 會設早一點。

---

## 假日 / 例外

- **週末、國定假日不開盤** → cron 先檢查 TWSE 交易日曆,沒開盤就跳過
- **颱風假停市** → 同上
- **跌停 / 除權息日** 的量能異常 → 在篩選邏輯標記但**不過濾**(讓你看到)

### 交易日曆來源
TWSE: `https://www.twse.com.tw/holidaySchedule/holidaySchedule?response=csv&queryYear=YYYY`
或在 Python 用 `twstock` / `mcalendar` 套件。

---

## CORS 與前端能不能直接打 TWSE?

**不行**。TWSE / MOPS 都沒設 `Access-Control-Allow-Origin`,瀏覽器會擋。

**解法**:
- 主架構:**GitHub Actions 預先抓**,前端只讀 repo 裡的 JSON
- 例外:Google News RSS 雖然技術上瀏覽器也會擋,但前端可以走 `https://api.allorigins.win/` 之類的 CORS proxy(免費,但不保證 SLA)。建議還是 Actions 抓完寫進 JSON

---

## 資料正確性的最後一道防線

- **每天比對**:今日抓的全市場數量是否 ≥ 1700 檔(突然剩 200 檔 = 抓爆了)
- **價格範圍檢查**:任何單日漲跌 > ±10.5% 視為異常(台股漲跌停板 10%),需要人工確認
- **日期檢查**:資料日期是否 = 今日(週一抓的資料如果還是上週五的 = 沒更新)

驗證失敗時 Email 多寄一封 `[警告] 資料抓取異常`,讓你不會用錯資料下單。
