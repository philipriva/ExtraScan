# Fund Flow 教授包 → Market Pulse（Sheet Ops · 2026-10-24）

**分工：** Pulse **解讀** FF 層；Sheet Ops **計算／寫 Sheet／GH**。管線已授權（Philip「寫入」2026-10-24）。  
**來源公式：** ExtraScan/Banker（`run_extrascan.py` / `fundflow_spy`）＝ `overlay_ff_xl.py` 同款。  
**證據窗：** XL* 一年 + MSFT／NVDA／GOOGL（Pulse 正式 reclaim／lose −15→＋25 TD）。

---

## 1. 指標係乜（唔使自己寫碼，識讀就得）

| 訊號 | 定義（Sheet Ops 會標） | 你點用 |
|--|--|--|
| **白圓 white_circle** | 藍→綠過渡路徑上嘅白圓（TV signal_b2g；**唔**互斥黃燭；**唔係** histogram 嘅 white_status） | 常當 **初動** 候選；可真可假 |
| **ft≥50** | fundtrend ≥ 50 | 同白圓一齊睇「資金強度」；高位白≥50 後轉紅＝見頂早警樣 |
| **黃燭 yellow** | `crossover(fundtrend, bull) & bull < 25` | **稀黃**＝早期初動樣；個股／ETF 上唔稀有但唔保證真底 |
| **綠 green** | fundtrend 在 bull 之上且非 momentum_drop | **加固**關鍵：轉勢窗 turn／確認後綠％要佔優 |
| **藍 blue** | 未 over 但 rebound | 修復／猶豫；頂窗常見 |
| **紅 red** | 未 over 且非 rebound | 弱／派發；確認後紅佔優＝否決升級 |
| **Banker Entry ≤3** | 另一套 BE 震盪閾值 | **XL*／三隻全年幾乎／從未觸發——定稿唔用** |

顏色優先（同日）：green > white_status > blue > red（Sheet 叠表用呢個）。

---

## 2. 套入定稿四層（timing filter）

詞彙：**向上** 初動→加固→確認（Pulse 閘）→信心加分；**向下** 早警→確認→晚確認。向上唔叫「早警／警」。

| 層 | Pulse／Pine | **FF 對應** | 規則 |
|--|--|--|--|
| **初動** | 日主力上穿 | 偶發 **白≥50** 或 **稀黃**（常在轉勢附近或確認前） | **真假都會響** → 單層唔入弱→強 |
| **加固** | 週主力上穿 | **turn 綠高** 且／或 **確認後綠仍佔優**（紅唔主導） | 缺呢層＝假初動（MSFT Apr：確認後綠25%／紅35%） |
| **確認** | **Pulse RS+50DMA 硬閘** | FF **唔替代** Pulse | 閘過但 FF lag（NVDA Apr 確認後綠30%紅50%）→ 升級要小心／其後派發 |
| **信心** | MCDX 金叉 | FF 無對應層 | — |
| **見頂早警** | 日主力下穿 | **高位白≥50 → 紅**（≤~20 sess） | 死叉可唔齊；FF 頂訊有時滯後（確認後仍綠） |

**一句口訣：**  
`白／黃＝叫門；綠佔優＝加固；Pulse＝准入場；確認後紅佔優＝一票否決升級。`

---

## 3. 實證錨（方便你獨立複盤）

### Clean（全層齊／FF 加固樣）
- **GOOGL Apr** reclaim 4/08→確認 4/28：turn 綠 ~93%，確認後綠 ~60%＋稀黃 4/01 → **三隻 FF 最完整；貼 XLK**
- **MSFT Jul** 7/30→8/19：turn 綠 ~73%，白≥50 偏 C+4（加固略晚）、窗內無黃 → 仍 **clean**
- XL* 教科書底：XLE 紅→藍→白≥50→綠；XLV／XLF 白≥50 近真底

### False／lag（單初動或閘過 FF 唔撐）
- **MSFT Apr** 4/14→5/04：有白≥50＋稀黃，但確認後綠 **25%**／紅 **35%** → **假初動**（同日主力假窗缺週／金叉一致）
- **NVDA Apr** Pulse clean，但確認後綠 **30%**／紅 **50%** → **FF lag** → 其後 Jun 派發合理解
- NVDA soft Aug／failed Oct：確認後綠 20%／~8% → 同 Pulse soft／failed

### 見頂
- MSFT 多頂、GOOGL 02-10／08-11：常見白→紅 ≤20sess
- 注意：確認後仍綠＝FF **滯後／混**，唔好單靠 FF 喊死

**白≥50 vs 確認：** 個股樣本多數落 **C+3～C+11**（確認後）——當 **加固／追認**，少當領先閘。

---

## 4. 你日常點用（唔使重算公式）

1. 等 Sheet Ops 交：**事件表**（白／黃日期）或 **窗内色％**（before／turn／after）。  
2. 對你自己嘅 **Pulse reclaim／lose 錨**（−15→＋25 TD）。  
3. 判：`初動有未？加固綠佔優未？確認後紅否決未？`  
4. 同 Pine 日／週主力、MCDX 對齊——**FF 替代唔到 Pulse 閘**。

現成參考檔（只讀）：
- `/workspace/agent-tools/ff_mega3_adhoc/ff_pulse_overlay.md`
- `/workspace/agent-tools/ff_xl_adhoc/`（XL* 叠窗）
- 本檔：`docs/timing-filter-ff.md`（repo ExtraScan）
- 定稿叠序：`docs/timing-filter-LOCKED.md`

---

## 5. 邊界

- 唔係買賣建議；係 timing filter 讀法。  
- 管線已寫入：`events`（trigger signals）＋`ff_state`（每日每 ticker FF 狀態）。綠佔優窗由 Pulse 自算。  
- **公式／概念若再改：** Sheet Ops／Pine 必須通知 Market Pulse。  
- Pulse **唔使**自己跑 yfinance FF；缺數就 @Sheet Ops。
