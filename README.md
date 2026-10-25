# ExtraScan（Fund Flow 黃燭 / 白圓）

收市後 **+15 分鐘** 自動篩選 **日線** Banker Fund Flow，寫入 Google Sheet **ExtraScan** 四個 tab（覆寫）。

| Tab | 宇宙 | 條件 |
|-----|------|------|
| `yellowALL` | 全美股（Nasdaq Trader），**剔 ETF／fund** | 黃燭 (yellow) |
| `whiteballALL` | 同上 | 白圓 (white_circle) |
| `yellowETF` | 預設 `etf_list.txt`；可選 `--all-etf-fund` 用全 Nasdaq ETF／fund | 黃燭 |
| `whiteETF` | 同上 | 白圓 |

與 [us-fundflow-tips-screener](https://github.com/philipriva/us-fundflow-tips-screener) 不同：此 repo **分開** 黃燭／白圓；ALL 宇宙 **不含** ETF／fund；四個 tab 係 **四次獨立掃描**：ALL = 全美股剔 ETF／fund；ETF = **預設** curated `etf_list.txt`（可選 `--all-etf-fund` 先用全 Nasdaq ETF／fund）。唔好混宇宙。寫入 Sheets（tips 只出 artifact `.txt`）。

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
  - 可勾 `etf_only`：只掃 ETF tabs（預設 curated `etf_list.txt`；跳過 ALL 股票；唔清 ALL tabs）
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
# 少數 symbols（smoke：四個 tab 都會寫同一批 hits）
python run_extrascan.py --symbols AAPL,MSFT,SPY,QQQ
# 只掃 ETF tabs（預設 curated etf_list.txt；唔改 ALL tabs）
python run_extrascan.py --etf-only
# 可選：ETF tabs 用全 Nasdaq ETF/fund（~6k；唔影響 ALL）
python run_extrascan.py --etf-only --all-etf-fund
# 完整（ALL=全美 ex-ETF + ETF=curated；需時很長）
export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat /path/to/sa.json)"
python run_extrascan.py
```

## 檔案

| 檔 | 用途 |
|----|------|
| `run_extrascan.py` | 主程式 |
| `sheets_writer.py` | gspread 覆寫 tab |
| `write_qqscan.py` | Strong_Stocks + Maggiestocks → QQscan A/J |
| `write_etf_leaders.py` | yellowETF/whiteETF → holdings leaders → QQscan G; yellowALL∪whiteballALL → QQscan H |
| `write_zhuli_lm.py` | 主力能量 weekly→QQscan L, daily→QQscan M (US ex-ETF universe) |
| `write_pulse_events.py` | MS145 Pulse `events` + `ff_state` → Sheet |
| `docs/timing-filter-LOCKED.md` | Locked timing-filter stack (Philip 寫入) |
| `docs/timing-filter-ff.md` | Fund Flow teach pack for Pulse |
| `sheet_ids.json` | spreadsheet / folder id |
| `etf_list.txt` | 預設 curated ETF 名單（`--all-etf-fund` 可改全宇宙） |

## QQscan tab

Separate workflow [`.github/workflows/qqscan.yml`](.github/workflows/qqscan.yml) (`QQscan to Sheet`):

- **Schedule:** `30 2 * * 2-6` UTC ≈ **10:30 HKT** Tue–Sat (after Maggiestocks / Strong_Stocks publish).
- **Column A:** latest `Strong_Stocks_V3_YYYYMMDD.txt` from [`philipriva/my-stock-scanner`](https://github.com/philipriva/my-stock-scanner) (one symbol per row; clear+overwrite).
- **Column J:** latest `Maggiestocks_YYYYMMDD.txt` from [`philipriva/Q-scanner`](https://github.com/philipriva/Q-scanner) (same).
- Uses existing secret `GOOGLE_SERVICE_ACCOUNT_JSON` (no new API keys). Manual: Actions → **QQscan to Sheet** → Run workflow.

```bash
python write_qqscan.py --dry-run
GOOGLE_SERVICE_ACCOUNT_JSON='...' python write_qqscan.py
```

## QQscan columns G + H — ETF holdings leaders + ALL Fund Flow union

Separate workflow [`.github/workflows/etf_leaders.yml`](.github/workflows/etf_leaders.yml) (`ETF Leaders to QQscan G+H`):

- **Triggers:** `workflow_run` after Daily ExtraScan success; crons `0 3` / `30 5` UTC Tue–Sat; `workflow_dispatch`.
- **Column G:** union of `yellowETF` + `whiteETF` → Schwab holdings (Playwright) → yfinance filter (`price > EMA200` **and** `EMA50 > EMA200` **and** `price >= 0.90 * ATH`, Philip's script). Sorted unique → `QQscan!G` (clear+overwrite). Does **not** mix ALL tabs into G.
- **Column H:** union of `yellowALL` + `whiteballALL` (stock Fund Flow hits) → `QQscan!H` (clear+overwrite). Sheets-only; no Schwab. Does **not** mix ETF tabs into H.
- **Column F:** no longer written (prior F values left uncleared).
- **Row 1:** bilingual/Chinese source labels; symbols from row 2.
- **Secret:** reuses `GOOGLE_SERVICE_ACCOUNT_JSON`. Four ExtraScan tabs stay separate universes.

```bash
pip install -r requirements.txt && playwright install chromium
GOOGLE_SERVICE_ACCOUNT_JSON='...' python write_etf_leaders.py --dry-run
GOOGLE_SERVICE_ACCOUNT_JSON='...' python write_etf_leaders.py
# H only / G only:
GOOGLE_SERVICE_ACCOUNT_JSON='...' python write_etf_leaders.py --skip-g
GOOGLE_SERVICE_ACCOUNT_JSON='...' python write_etf_leaders.py --skip-h --etfs XBI,XPH --dry-run
```

## QQscan columns L + M — 主力能量做多上穿做空

Workflow [`.github/workflows/zhuli_lm.yml`](.github/workflows/zhuli_lm.yml) (`Zhuli L+M to QQscan`):

- **Schedule:** `45 21 * * 1-5` UTC ≈ **17:45 EDT** Mon–Fri (US close+~1h45); backup `30 0 * * 2-6` UTC.
- **Universe:** Nasdaq Trader US equities **ex ETF/fund** (same as `yellowALL` / `universe_us_ex_etf_fund`). Not `etf_list.txt`.
- **Column L:** weekly Tongdaxin 主力能量 — 做多線上穿做空線 (Banker 主力能量 tab / `zhuli_cross_up` on W-FRI bars).
- **Column M:** same crossover on **daily** bars.
- **Shared filters (daily):** market cap > 100M, volume > 300k, close > $5, close > EMA200.
- **Row 1:** source labels for A/G/H/J/L/M; symbols from row 2.
- One download pass for both columns. Secret: existing `GOOGLE_SERVICE_ACCOUNT_JSON` (no new secrets).
- Does **not** mix ETF tabs; does **not** overwrite A/G/H/J data (only refreshes row-1 labels).

```bash
python write_zhuli_lm.py --dry-run --symbols AAPL,MSFT
GOOGLE_SERVICE_ACCOUNT_JSON='...' python write_zhuli_lm.py
```

## Pulse events (Market Pulse theme timing)

Daily post-close screener over **MS145 proxy + radar patches + macro sleeve** → Google Sheet **[MS145 Pulse events](https://docs.google.com/spreadsheets/d/11ew_PpHdAnFDn6TWTsGp8qX7GXoYxQOtpvIt_DHmqZ8/edit)**.

| Tab | Contents |
|-----|----------|
| `events` | Header + trigger-day rows: `asof | ticker | signal | signal_date | tf` |
| `ff_state` | One row per universe ticker (last settled bar): `asof | ticker | fundtrend | bull | color | ft_ge_50 | white_circle | yellow | layer` |
| `universe` | `pulse_universe.csv` (ticker, layer, …) |

Signals (trigger-day only — not ongoing state):

| signal | 定稿層 | 方向 |
|--------|--------|------|
| `zhuli_cross_up_D` | ①初動 | 弱→強 |
| `white_ball` | ①初動（偶發／可假） | 弱→強 |
| `yellow_candle` | ①次要提示（樣本唔加分；唔當硬閘） | 弱→強 |
| `zhuli_cross_up_W` | ②加固 | 弱→強 |
| `mcdx_golden_D` | ④信心加分 | 弱→強 |
| `pulse_bottom_5` | ⑤見底讀（RS止LL＋市寬） | 見底預示 only |
| `whale_hit` | ⑥Whale（昨紅今黃較短） | 見底預示 only |
| `zhuli_cross_down_D` | ①早警 | 強→弱 |
| `zhuli_cross_down_W` | 輔助 | 強→弱 |
| `mcdx_death_D` | ③晚確認 | 強→弱 |

③確認硬閘＝Market Pulse RS＋50DMA≥15（唔由本表計算）。FF **綠佔優**＝②加固（Pulse 用 `ff_state.color` 自算窗％；Sheet **唔**預聚合）。**Banker Entry ≤3 唔用／唔 emit。** ⑤⑥ **只有見底預示、無見頂 emit**（Philip「寫入」2026-10-26）。

叠序：弱→強 **初動→加固→Pulse閘→信心加分**；強→弱 **早警→確認→晚確認**。上行早期唔叫「早警」。

Docs: [`docs/timing-filter-LOCKED.md`](docs/timing-filter-LOCKED.md) · [`docs/timing-filter-ff.md`](docs/timing-filter-ff.md)  
**公式／概念若再改 → 必須通知 Market Pulse。** Pipeline authorized Philip「寫入」2026-10-24.

- Script: `write_pulse_events.py`
- Universe: `pulse_universe.csv` (layer = ms145 / radar / macro; USD = **UUP**)
- Workflow: `.github/workflows/pulse_events.yml` — cron `50 21 * * 1-5` UTC (+ backup `40 0 * * 2-6`)
- Reuses secret `GOOGLE_SERVICE_ACCOUNT_JSON`
- Manual: Actions → *Pulse events to Sheet* → Run workflow (optional refresh universe tab)
- Local CSVs: `out/pulse_events.csv` + `out/pulse_ff_state.csv`

Formulas: Fund Flow = ExtraScan `fund_flow_state` (color green/blue/red/white_status); zhuli = `write_zhuli_lm` (no stock filters on this ETF universe); MCDX = Pine Craft (`SMA10(PC)` cross `SMA10(LC)`; PC=`fmcdx(C×0.96)`, LC=`100−fmcdx(C×1.04)`, LEN=100). ⑥ `whale_hit` = `pulse_layers_56`（同 banker `whale_hit`）. ⑤ `pulse_bottom_5` = RS stop_ll onset + RSP/SPY slope20>0 within +10TD（live emit on breadth-confirm day; `signal_date`=onset）.
