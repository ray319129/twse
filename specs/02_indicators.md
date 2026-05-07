# 指標計算清單

## 實作框架

**主 library**: [`pandas-ta`](https://github.com/twopirllc/pandas-ta)
- 純 Python,`pip install pandas-ta` 即可
- 不需要 build TA-Lib C library,**GitHub Actions 部署無痛**
- 主流指標都有

**輔助**: `pandas`, `numpy`

**為什麼不用 TA-Lib**:TA-Lib 是 C library,Actions 上設定麻煩。`pandas-ta` 純 Python 雖然慢一點,但全市場每日跑 < 1 分鐘,可接受。

---

## 1. 移動平均 MA / EMA

### 簡單移動平均 SMA
$$ SMA_n = \frac{P_1 + P_2 + ... + P_n}{n} $$

**預設週期**:5, 10, 20, 60, 120, 240

對應週期:
- 5 = 1 週
- 10 = 2 週
- 20 = 月線
- 60 = 季線
- 120 = 半年線
- 240 = 年線

```python
df.ta.sma(length=20, append=True)
# 產生欄位: SMA_20
```

### 指數移動平均 EMA
較快反映近期價格變化,長線較少用。

```python
df.ta.ema(length=12, append=True)
```

---

## 2. KD (Stochastic)

```
RSV_n = (今日收盤 − n日最低) / (n日最高 − n日最低) × 100
K_t   = K_{t-1} × 2/3 + RSV_n × 1/3
D_t   = D_{t-1} × 2/3 + K_t × 1/3
```

**預設參數**:(9, 3, 3) — 9 日 RSV、K 平滑 3、D 平滑 3

```python
df.ta.stoch(k=9, d=3, smooth_k=3, append=True)
# 產生: STOCHk_9_3_3, STOCHd_9_3_3
```

**解讀**:
- K > 80:超買
- K < 20:超賣
- K 上穿 D:黃金交叉
- K 下穿 D:死亡交叉
- KD 鈍化(K 連續多日 > 80):強勢股特徵,不要逆勢操作

---

## 3. MACD

```
DIF       = EMA(close, 12) − EMA(close, 26)
DEA       = EMA(DIF, 9)
柱狀體    = (DIF − DEA) × 2
```

**預設參數**:(12, 26, 9)

```python
df.ta.macd(fast=12, slow=26, signal=9, append=True)
# 產生: MACD_12_26_9, MACDh_12_26_9, MACDs_12_26_9
```

**解讀**:
- 柱狀體由負轉正:動能翻多
- 柱狀體由正轉負:動能翻空
- DIF 在零軸上方:中期多頭格局

---

## 4. RSI

```
RS  = n 日平均上漲幅度 / n 日平均下跌幅度
RSI = 100 − 100/(1 + RS)
```

**預設週期**:6, 12, 14

```python
df.ta.rsi(length=14, append=True)
# 產生: RSI_14
```

**解讀**:
- > 70:超買
- < 30:超賣
- 50:多空分界

---

## 5. 布林通道 BBands

```
中軸 = SMA(close, 20)
上軌 = 中軸 + 2 × σ(20 日)
下軌 = 中軸 − 2 × σ(20 日)
```

**預設**:(20, 2)

```python
df.ta.bbands(length=20, std=2, append=True)
# 產生: BBL_20_2.0, BBM_20_2.0, BBU_20_2.0, BBB_20_2.0, BBP_20_2.0
```

---

## 6. ATR(平均真實區間)

用來衡量波動性,**做停損用**。

```
TR  = max(high − low, |high − prev_close|, |low − prev_close|)
ATR = SMA(TR, 14)
```

```python
df.ta.atr(length=14, append=True)
# 產生: ATRr_14
```

**長線停損常見公式**:`進場價 − 2 × ATR`

---

## 7. 季線扣抵值

```
扣抵值 = 60 個交易日前的收盤價
```

**意義**:明天計算 60MA 時會「扣掉」的那個價格,告訴你季線即將上揚還是下彎。

- 扣抵值低 → 即將扣掉的數字小,新進的數字若大於它,60MA 會上揚 → 季線多頭續攻
- 扣抵值高 → 季線即將下彎,壓力增加

```python
df['discount_60'] = df['close'].shift(60)
df['ma60_slope_estimate'] = df['close'] - df['discount_60']  # 正號表示明天季線上揚
```

---

## 8. 量能指標

### 5/20 日均量
```python
df['vol_ma5']  = df['volume'].rolling(5).mean()
df['vol_ma20'] = df['volume'].rolling(20).mean()
```

### 量比
```python
df['vol_ratio'] = df['volume'] / df['vol_ma5']
```

### 爆量
`vol_ratio > 2.0` 即視為爆量。

---

## 9. 籌碼指標

### 三大法人合計買賣超
```python
df['inst_total'] = df['foreign'] + df['investment_trust'] + df['dealer_proprietary']
```
單位:股(或張,看來源)

### 連續買超天數
```python
streak = 0
streaks = []
for v in df['inst_total']:
    streak = streak + 1 if v > 0 else 0
    streaks.append(streak)
df['inst_buy_streak'] = streaks
```

### 外資持股比 30 日變化
```python
df['foreign_pct_change_30d'] = df['foreign_holding_pct'] - df['foreign_holding_pct'].shift(30)
```

### 券資比
```
券資比 = 融券餘額 / 融資餘額 × 100%
```
> 30% 通常代表空頭氣氛濃,軋空機率高(逆向思維指標)

---

## 計算流程與儲存

每個交易日的計算流程:

1. 載入歷史日 K(至少 **250 個交易日**,以免年線、季線扣抵不夠)
2. Append 今日資料
3. 計算所有指標(append 到 DataFrame)
4. 寫入 `data/prices/{stock_id}.parquet`(parquet 比 csv 小很多)
5. 提取「今日訊號」寫入 `data/signals/{date}.json`

**效能估計**:1800 檔 × 250 日 × 全指標 約 30 ~ 60 秒(GitHub Actions runner)

---

## 為什麼用 parquet 而不是 csv

- 檔案小 5~10 倍(全市場 1800 檔 × 5 年歷史 csv 約 200MB,parquet 約 25MB)
- 讀寫快 5~20 倍
- 保留型別資訊(date 不會變字串)

讀:`pd.read_parquet(path)` / 寫:`df.to_parquet(path)`

需要 `pip install pyarrow`。
