# 台股技術選股系統 (twse)

長線選股 + 自選股監控 + Email 通知,跑在 GitHub Actions + GitHub Pages,**完全免費**。

> ⚠️ **本系統提供的是「符合特定條件的股票清單」,不是投資建議。**
> 條件觸發 ≠ 應該買進。

## 它做什麼

- 每個交易日 16:30 (台北時間),GitHub Actions 自動:
  1. 抓全市場(上市 + 上櫃約 1800 檔)當日資料
  2. 計算所有技術指標
  3. 跑 12 條長線策略 + 4 個多訊號交集組合
  4. 對你的自選池檢查觸發條件
  5. 寄 Email 給你看選股結果 + 自選池訊號

- GitHub Pages 顯示:選股清單、個股 K 線、公司資訊、近期新聞、自選池管理

## 架構

```
GitHub Actions (cron)
    ↓ Python
    ├─ 抓資料 (TWSE / TPEX / FinMind / Google News RSS)
    ├─ 算指標 (pandas-ta)
    ├─ 跑選股策略 (12 條 + 4 個交集組合)
    ├─ 對自選池檢查觸發 → 寄 Email
    └─ 寫 JSON 回 repo
        ↓
GitHub Pages 前端 (階段二)
    ├─ 看選股清單
    ├─ 看個股 K 線 + 指標 + 公司資訊 + 新聞
    └─ 編輯自選池 (Firebase Firestore)
```

## 開發階段

- [ ] **階段一(MVP)**:Actions cron + 抓資料 + 算指標 + 篩選 + Email
- [ ] **階段二**:GitHub Pages 前端 + Firebase 自選股管理 + 個股 K 線/新聞
- [ ] **階段三**:策略回測引擎、訊號歷史勝率統計

## 規格文件

詳細規格請看 [`specs/`](specs/):

- [01_strategies.md](specs/01_strategies.md) — 選股策略清單與多訊號交集
- [02_indicators.md](specs/02_indicators.md) — 技術指標公式與計算方式
- [03_data_sources.md](specs/03_data_sources.md) — 各資料源 API 與排程
- [04_email_template.md](specs/04_email_template.md) — Email 內容範本

## 環境設定(初次啟用)

### 1. GitHub Secrets

在 `Settings → Secrets and variables → Actions` 設定:

| Secret 名稱 | 用途 |
|---|---|
| `FINMIND_TOKEN` | FinMind API token([註冊](https://finmindtrade.com)) |
| `GMAIL_USER` | 寄信用 Gmail(例:`xxx@gmail.com`) |
| `GMAIL_APP_PASSWORD` | Gmail App Password(16 碼,**不是登入密碼**) |
| `MAIL_TO` | 收件 Email |

### 2. Gmail App Password 取得方式

1. Google 帳號 → 安全性
2. 開啟兩階段驗證(若還沒開)
3. 進「應用程式密碼」(App Passwords)
4. 建立一組,App 選 Mail → 複製 16 碼

### 3. 啟用 GitHub Actions

進 repo `Actions` 頁籤,點 `I understand my workflows, go ahead and enable them`。

## 本機開發

```bash
git clone https://github.com/ray319129/twse.git
cd twse

python -m venv .venv
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # macOS/Linux

pip install -r requirements.txt

# 設環境變數(本機測試)
set FINMIND_TOKEN=your_token
set GMAIL_USER=your@gmail.com
set GMAIL_APP_PASSWORD=your_app_password
set MAIL_TO=your@gmail.com

# 跑一次每日流程
python -m scripts.main
```

## 風險警告

長線選股工具給新手用容易被誤導:

1. **看到訊號 ≠ 該買**。技術指標訊號歷史勝率約 50~60%,單一訊號不可靠。
2. **長線吃飯的是基本面 + 產業 + 籌碼**,技術面是進場時機輔助。
3. **必須懂得部位管理 / 停損紀律**,不然選股再準也會虧。

**建議使用方式**:前 3~6 個月當「學習工具」,**訊號 → 觀察 → 不下單 → 累積對訊號品質的直覺**,確認有 edge 後再實盤。

## License

僅供個人研究使用,不對任何投資虧損負責。
