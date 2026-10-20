# ExtraScan（Fund Flow 黃燭 / 白圓）

收市後 **+15 分鐘** 自動篩選 **日線** Banker Fund Flow，寫入 Google Sheet **ExtraScan** 四個 tab（覆寫）。

| Tab | 宇宙 | 條件 |
|-----|------|------|
| `yellowALL` | 全美股（Nasdaq Trader），**剔 ETF／fund** | 黃燭 (yellow) |
| `whiteballALL` | 同上 | 白圓 (white_circle) |
| `yellowETF` | `etf_list.txt` 固定名單（已去重 ICLN／CNRG） | 黃燭 |
| `whiteETF` | 同上 | 白圓 |

與 [us-fundflow-tips-screener](https://github.com/philipriva/us-fundflow-tips-screener) 不同：此 repo **分開** 黃燭／白圓，ALL 宇宙 **不含** ETF／fund，並寫入 Sheets（tips 只出 artifact `.txt`）。

## Google Sheet

- 檔案：[ExtraScan](https://docs.google.com/spreadsheets/d/1FSOmey7ALHFuYXxZSgjZnjWA5zpmbiv48PWAJD_yYRk/edit)
- 資料夾：[processing](https://drive.google.com/drive/folders/1N41jCmxOvOzNQXRtE6DwYojyU5kskqZM)
- 寫入用 spreadsheet **file id**（`sheet_ids.json` → `open_by_key`），搬 folder 唔影響

每 tab 欄位 A/B/C：`symbol` / `signal` / `asof_et`（**無 header 行**；A1 即第一隻股）

## 公式（與 tips / banker_entry 一致）

| 條件 | 說明 |
|------|------|
| 黃燭 (yellow) | fundtrend 上穿 bullbear，且 bullbear &lt; 25（is_banker） |
| 白圓 (white_circle) | 昨 status=藍 + 今=綠 + 今非黃燭 |

未完成日線 bar 喺美東 **16:15** 前會丟掉。

## GitHub Actions

- **排程**：`cron: 15 20 * * 1-5`（UTC）  
  - ≈ **Mon–Fri 16:15 EDT**（夏令；美股現金收市 16:00 ET +15m）  
  - 冬令 EST 請改為 `15 21 * * 1-5`（GitHub Actions cron **只支援 UTC**，無 `CRON_TZ`）
- **手動**：Actions → *Daily ExtraScan* → Run workflow  
  - 可勾 `etf_only`：只掃固定 ETF 名單（煙測／省時間）
- **Artifact**：`extrascan_out`（四個 CSV + `download_stats.txt`），保留 14 日

### Secret

| Name | 內容 |
|------|------|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Google Cloud service account **整份 JSON** |

設定步驟：

1. GCP 建 service account，開 **Google Sheets API** + **Drive API**
2. 下載 JSON key
3. 把 SA 電郵（`...@....iam.gserviceaccount.com`）加做 **ExtraScan** spreadsheet（或整個 `processing` folder）嘅 **Editor**
4. Repo → Settings → Secrets → Actions → `GOOGLE_SERVICE_ACCOUNT_JSON` = 整份 JSON

未設 secret 時 Action 仍會跑篩選並 upload CSV artifact，只係 **跳過 Sheets 寫入**。

## 本地／Smoke

```bash
pip install -r requirements.txt
# 少數 symbols（四個 tab 都會寫同一批 hits）
python run_extrascan.py --symbols AAPL,MSFT,SPY,QQQ
# 只掃固定 ETF 名單
python run_extrascan.py --etf-only
# 完整（全美 ex-ETF + ETF 名單；需時很長）
export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat /path/to/sa.json)"
python run_extrascan.py
```

## 檔案

| 檔 | 用途 |
|----|------|
| `run_extrascan.py` | 主程式 |
| `sheets_writer.py` | gspread 覆寫 tab |
| `sheet_ids.json` | spreadsheet / folder id |
| `etf_list.txt` | 固定 ETF 名單 |

## QQscan tab

Separate workflow [`.github/workflows/qqscan.yml`](.github/workflows/qqscan.yml) (`QQscan to Sheet`):

- **Schedule:** Mon–Fri `0 22 * * 1-5` UTC ≈ **18:00 America/New_York** during EDT (US cash close + 2h). In EST use `0 23 * * 1-5`.
- **Column A:** latest `Strong_Stocks_V3_YYYYMMDD.txt` from [`philipriva/my-stock-scanner`](https://github.com/philipriva/my-stock-scanner) (one symbol per row; clear+overwrite).
- **Column J:** latest `Maggiestocks_YYYYMMDD.txt` from [`philipriva/Q-scanner`](https://github.com/philipriva/Q-scanner) (same).
- Uses existing secret `GOOGLE_SERVICE_ACCOUNT_JSON` (no new API keys). Manual: Actions → **QQscan to Sheet** → Run workflow.

```bash
python write_qqscan.py --dry-run
GOOGLE_SERVICE_ACCOUNT_JSON='...' python write_qqscan.py
```

