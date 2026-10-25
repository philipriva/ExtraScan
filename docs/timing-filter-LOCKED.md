# Timing filter — LOCKED stack（2026-10-24）

**Authorized:** Philip「寫入」2026-10-24 in 早鳥板塊.  
**Rule:** 任何公式／概念變更 → **必須通知 Market Pulse**（Sheet Ops / Pine Craft）.

## 叠序（定稿）

### 弱→強（上行；唔叫「早警」）
1. **初動** — 日主力上穿（`zhuli_cross_up_D`）／偶發白圓≥50（`white_ball` + `ff_state.ft_ge_50`）；黃燭＝次要附註（`yellow_candle`／`ff_state.yellow`），**唔當硬閘**
2. **加固** — 週主力上穿（`zhuli_cross_up_W`）**或** FF **綠佔優**（Pulse 用 `ff_state.color` 自算窗％；Sheet **唔**預聚合）
3. **確認** — **Pulse 閘硬確認**（RS + 50DMA ≥15）— 唔由本 Sheet 算
4. **信心加分** — MCDX 金叉（`mcdx_golden_D`）

### 強→弱
1. **早警** — 日主力下穿（`zhuli_cross_down_D`）；FF 高位白≥50→紅 由 Pulse 用 `ff_state` 判
2. **確認** — Pulse 閘／價格確認（本表唔替代）
3. **晚確認** — MCDX 死叉（`mcdx_death_D`）；週主力下穿（`zhuli_cross_down_W`）可作輔助

## 見底預示（⑤⑥ · bottoms-only · Philip「寫入」2026-10-26）

| signal | 層 | 定義 | 邊界 |
|--------|----|------|------|
| `pulse_bottom_5` | ⑤ Pulse見底讀 | RS(ticker/SPY) stop_ll onset（60/10）＋＋10TD 內市寬 RSP/SPY 20d slope>0 | **無見頂**；唔係③硬閘 |
| `whale_hit` | ⑥ Whale | 昨紅＋今黃＋今黃高度&lt;昨紅高度（Tongdaxin 能量柱） | **無見頂**；Banker L1 仍唔用 |

叠序啓示：`⑤→(⑥)→①→(④)→③`。Live `pulse_bottom_5`＝**RS stop_ll False→True onset**＋＋10TD 市寬確認日 emit（`signal_date`＝onset）。研究表有時喺 T−40 窗左緣標⑤（結構早已止LL、窗內無新 onset）——daily emit **唔**重放呢種窗緣標籤，只打真 onset；屬有意近似。

## 唔用
- **Banker Entry ≤3** — 全年樣本幾乎零；唔 emit、唔當閘
- 黃燭／Banker≤3 唔做 hard gate

## 分工
| 角色 | 負責 |
|------|------|
| **Sheet Ops** | Fund Flow 計算；`events` + `ff_state` 寫入 Sheet／GH |
| **Market Pulse** | 解讀叠層、綠佔優窗、RS 閘、獨立分析 |
| **Pine Craft** | 主力／MCDX script 與公式欄 |

## Sheet
- URL: https://docs.google.com/spreadsheets/d/11ew_PpHdAnFDn6TWTsGp8qX7GXoYxQOtpvIt_DHmqZ8/edit
- Tabs: `events` | `ff_state` | `universe`
- Detail: `docs/timing-filter-ff.md`
