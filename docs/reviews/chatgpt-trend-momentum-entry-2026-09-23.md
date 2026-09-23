Saya sudah bedah struktur repo, `history.db`, pipeline scoring, `features.py`, `regime.py`, `vaf.py`, `analyzer.py`, `collect.py`, `model_audit.py`, `regime_backfill.py`, `vaf_overrides.json`, serta catatan validasinya.

**Kesimpulan utamanya:** problem terbesar sekarang bukan kekurangan indikator. Justru model mulai **terlalu banyak menggabungkan sinyal yang secara empiris belum terbukti punya edge**, sementara beberapa komponen baru memiliki sampel sangat pendek.

Ada tiga area yang paling layak dioptimalkan:

1. **Trend Score → ubah dari “predictor” menjadi state/confirmation engine.**
2. **Momentum → jangan dihidupkan kembali sebagai ranking; ubah menjadi “acceleration / transition” layer.**
3. **Entry Grade → pisahkan kualitas setup, timing, dan confidence. Saat ini ketiganya terlalu tercampur.**

---

# 1. Temuan paling penting dari audit

Repo sekarang punya **38.542 snapshot**, 292 hari, tetapi model v3 baru benar-benar mulai sekitar **29 Agustus 2026**.

Dari history:

| Komponen | Kondisi |
|---|---:|
| Trend Score overall | IC −0.0229 |
| Cross-sectional momentum | sudah rejected |
| v3 flow rotation | baru 11 IC-days |
| v3 participation | baru 11 IC-days |
| v3 confirmation | baru 11 IC-days |
| OTF/Entry | baru 35 IC-days |
| Fundamental/VaF | masih pending |
| Regime reconstructed | jauh lebih lengkap daripada stored regime |

Jadi saya **tidak akan mengubah bobot v3 berdasarkan 11 hari data**. Itu terlalu mudah menjadi overfit.

Yang menarik justru: pada 11 hari terakhir yang sudah memakai v3, sinyal terlihat lebih baik secara *in-sample/OOS pendek*:

- Trend ≈ **+0.138 IC**
- Flow rotation ≈ **+0.106**
- Participation ≈ **+0.064**
- Confirmation ≈ **+0.253**
- OTF ≈ **+0.269**

Tetapi ini **belum cukup untuk menyebut edge**. Ini hanya menunjukkan bahwa arsitektur baru layak dipertahankan untuk diuji.

---

# 2. Masalah terbesar Trend Score sekarang

Current v3:

```text
Flow Rotation       28%
Participation       28%
Relative Strength   24%
Compression         12%
Confirmation         8%
```

Secara konseptual ini jauh lebih baik daripada model lama karena macro constant sudah dikeluarkan.

Tetapi ada **3 masalah struktural**.

---

## A. Relative Strength 24% terlalu besar

Ini menurut saya kandidat nomor satu untuk dikurangi.

`IR_7d vs BTC` adalah inti dari:

```python
rel_strength
```

Tetapi audit sebelumnya sudah menunjukkan bahwa cross-sectional momentum / relative price strength **tidak memiliki edge stabil**.

Bahkan validasi 9 tahun menunjukkan momentum:

- BULL: negatif
- SIDEWAYS: hanya sedikit positif pada beberapa horizon
- BEAR: negatif
- probability transformation juga tidak memperbaiki hasil

Jadi memberi **24%** kepada `rel_strength` membuat trend score masih terlalu dekat dengan model momentum yang sudah ditolak.

### Saya akan ubah menjadi:

```text
Flow Rotation       30%
Participation       25%
Price Acceleration  15%
Compression         10%
Confirmation        20%
```

Tetapi ada satu syarat:

**Price Acceleration bukan momentum klasik.**

Jangan:

```text
return_7d
return_14d
return_30d
```

lalu diranking.

---

# 3. Momentum sebaiknya tidak dibuang — tetapi diubah definisinya

Ini bagian yang menurut saya paling menarik untuk SFC/Altcoin Terminal.

Momentum klasik bertanya:

> "Coin mana yang sudah naik paling banyak?"

Saya justru ingin model bertanya:

> **"Apakah tekanan yang menyebabkan harga bergerak sedang menguat sebelum harga melakukan repricing penuh?"**

Ini berbeda.

### Momentum baru = Acceleration / Transition

Gunakan empat keadaan:

```text
FLOW
   ↓
PARTICIPATION
   ↓
PRICE RESPONSE
   ↓
TREND CONFIRMATION
```

Model mencari **ketidakseimbangan antar-layer**.

Contoh:

### Case A — ideal

```text
Flow            ↑↑
Volume          ↑↑
Price           ↑
Trend           →
```

Ini jauh lebih menarik daripada:

```text
Flow            →
Volume          →
Price           ↑↑↑
Trend           ↑↑
```

Karena yang kedua sudah mengalami repricing.

---

# 4. Saya sarankan membuat `Acceleration Score`

Bukan momentum.

Misalnya:

```text
Acceleration =
    35% Flow Acceleration
    25% Participation Acceleration
    20% Price Response
    20% Trend Confirmation
```

### Flow acceleration

Sekarang:

```python
flow_trend = buy_share_3d / buy_share_7d
```

Ini terlalu pendek dan agak noisy.

Saya akan ubah menjadi:

```text
flow_z_3
flow_z_7
flow_slope
flow_persistence
```

Contoh:

```text
Buy share 7d = 50.2%
Buy share 3d = 52.1%
Buy share 1d = 54.0%
```

lebih informatif daripada hanya:

```text
flow_trend = 1.038
```

Karena kita ingin tahu:

> apakah tekanan beli benar-benar sedang meningkat secara konsisten?

---

# 5. Ada masalah penting pada `flow_rotation`

Saat ini:

```python
strength = (buy_share_3d - 0.5) * 150
momentum = (flow_trend - 1.0) * 60
score = 50 + strength + momentum
```

Saya kurang suka struktur ini.

Karena `flow_trend` adalah **rasio**, sehingga ketika denominator kecil atau buy-share baseline berubah, sensitivitasnya bisa tidak linear.

Saya lebih suka:

```text
Flow Score =
    60% directional pressure
    25% acceleration
    15% persistence
```

Misalnya:

```text
directional_pressure =
    tanh((buy_share_3d - 0.50) / σ_flow)

acceleration =
    normalized(flow_3d - flow_7d)

persistence =
    percentage of positive-flow days
```

Hasilnya lebih robust.

---

# 6. Participation sekarang terlalu mudah menjadi 100

Ini salah satu masalah yang paling jelas dari data aktual.

Current:

```python
vol_trend
vol_ratio
```

dan scoring:

```python
(vol_trend - 0.9) / 0.4
(vol_ratio - 0.8) / 1.2
```

Akibatnya banyak coin sekarang:

```text
participation = 100
```

Saya menemukan distribusi latest snapshot:

```text
median participation ≈ 50
75th percentile ≈ 76.6
max = 100
```

dan banyak top-ranked coins memiliki:

```text
participation = 100
```

Ini membuat komponen **cepat jenuh**.

### Solusi:

Gunakan percentile atau robust z-score.

Misalnya:

```text
volume acceleration percentile
volume anomaly percentile
```

daripada hard clipping.

Contoh:

```text
Pctl 50 → 50
Pctl 70 → 70
Pctl 85 → 85
Pctl 95 → 95
Pctl 99 → 99
```

bukan:

```text
volume ratio > 2 → 100
```

Dengan demikian model masih bisa membedakan:

```text
2.1x
2.7x
3.5x
5.0x
```

---

# 7. Compression juga terlalu binary

Sekarang:

```python
compression_setup = bbw_pct < 20 and vol_2d_ratio > 1.5
```

Kemudian:

```text
True  → 85
False → 60 atau 40
```

Ini terlalu kasar.

Compression sebenarnya bukan:

> compressed / not compressed

Tetapi sebuah **continuum**.

Saya akan membuat:

```text
Compression Score
```

berdasarkan:

```text
BB width percentile
+
volume expansion
+
ATR contraction
+
price range contraction
```

Contoh:

```text
BB percentile       8
ATR percentile     14
volume acceleration 1.6x
```

→ compression tinggi.

Sedangkan:

```text
BB percentile       19
ATR percentile      35
volume acceleration 1.1x
```

→ compression moderat.

Ini akan jauh lebih berguna untuk **early-entry detection**.

---

# 8. Confirmation 8% justru menurut saya terlalu kecil

Ini agak kontra-intuitif.

Audit pendek saat ini menunjukkan:

```text
confirmation IC ≈ +0.253
```

Tetapi sample hanya 11 hari.

Jadi saya **belum akan menaikkan bobot berdasarkan angka tersebut**.

Namun secara arsitektur, confirmation harus menjadi **gate**, bukan sekadar 8% score.

Saya lebih suka:

```text
CORE SCORE
+
CONFIRMATION FILTER
```

daripada:

```text
CORE SCORE + 8% confirmation
```

Misalnya:

```text
Trend Core = 76
Confirmation = weak
```

jangan otomatis menghasilkan:

```text
Trend = 70
```

Tetapi:

```text
Trend = 76
State = UNCONFIRMED
```

Ini jauh lebih informatif.

---

# 9. Ini membawa kita ke redesign Trend Score

Saya akan membangun:

## `Trend Core`

```text
Flow Rotation       30%
Participation       25%
Acceleration        15%
Compression         10%
Relative Strength   10%
```

Total:

```text
90%
```

Kemudian:

## `Confirmation Layer`

```text
Structure
EMA alignment
Breakout quality
ATR expansion
RSI regime
```

Tidak langsung dicampur.

Output:

```json
{
  "trend_core": 74,
  "confirmation": 82,
  "trend_state": "BUILDING",
  "trend_confidence": 0.71
}
```

Ini lebih bersih daripada satu angka 0–100 yang mencoba menjelaskan semuanya.

---

# 10. Regime: sekarang sudah lebih baik setelah `regime_backfill.py`

Ini bagian repo yang menurut saya **jangan dirombak besar-besaran**.

`regime_backfill.py` sudah melakukan hal yang benar:

```text
stored market_regime
        ↓
reconstructed regime
```

tanpa overwrite historical truth.

Itu bagus secara research methodology.

Saat ini reconstructed distribution:

```text
SIDEWAYS               14,037
BULL_TREND             11,006
RISK_OFF                7,687
BEAR_TREND              4,207
CAPITULATION_RECOVERY   1,190
```

Ini jauh lebih berguna daripada historical dataset yang sebelumnya mayoritas `UNKNOWN`.

---

# 11. Tetapi Regime sekarang terlalu BTC-centric

Saat ini regime terutama:

```text
BTC 30d return
BTC realized volatility percentile
repo stress
GLF
```

Masalahnya:

> **Market regime crypto ≠ hanya regime BTC.**

Untuk Altcoin Terminal saya akan menambahkan:

### Crypto breadth

```text
% alts above MA20
% alts outperform BTC
median alt return 7d
median alt return 30d
```

### Rotation

```text
ALT/BTC breadth
ETH/BTC
SOL/BTC
altcoin dispersion
```

### Liquidity

```text
GLF
stablecoin growth
repo stress
```

### Volatility

```text
BTC vol
ETH vol
cross-sectional dispersion
```

Lalu regime menjadi:

```text
LIQUIDITY
+ MARKET TREND
+ BREADTH
+ VOLATILITY
+ ROTATION
```

---

# 12. Jangan langsung pakai HMM

Ini penting.

Repo sendiri sudah benar untuk menahan HMM.

Dengan data sekarang:

```text
~292 daily observations
```

dan regime 5-state, HMM masih mudah mengalami:

- state instability
- label switching
- overfitting
- historical reclassification
- look-ahead melalui Viterbi

Saya akan tetap menggunakan deterministic regime sebagai baseline.

Tetapi buat:

```text
Regime Score
```

bukan hanya state.

Misalnya:

```text
Liquidity      +1
Breadth        +1
Trend          +1
Volatility     0
Rotation       +1
----------------
Regime score   4/5
```

Kemudian:

```text
BULL_EXPANSION
BULL_SELECTIVE
SIDEWAYS_ROTATION
RISK_OFF
CAPITULATION
```

Ini lebih berguna untuk entry.

---

# 13. Entry Grade adalah bagian yang paling perlu diubah

Saat ini:

```text
OTF
├── Market structure 12
├── Relative strength 10
├── Catalyst timing 10
├── Supply timing 8
└── Execution quality 10
```

Kemudian:

```text
41 → A+
35 → A
28 → B
<28 → Avoid
```

Masalahnya ada dua.

---

## Problem 1 — OTF sangat berkorelasi dengan Trend Score

Dokumentasi sendiri mencatat:

> entry_grade / OTF is not independent

dan cross-sectional correlation dengan trend score sekitar:

```text
0.687
```

Artinya:

```text
Trend Score tinggi
       ↓
OTF tinggi
       ↓
Entry Grade tinggi
```

Jadi dashboard terlihat memiliki dua sinyal berbeda, padahal sebagian besar membawa informasi yang sama.

Itu **double counting**.

---

# 14. Entry Grade seharusnya bukan "berapa bagus coin"

Entry Grade harus menjawab:

> **"Apakah sekarang merupakan lokasi entry yang memiliki risk/reward dan confirmation yang masuk akal?"**

Itu pertanyaan yang berbeda dari Trend.

Saya akan pecah menjadi:

### Trend

> Direction

### Momentum / Acceleration

> Transition

### Entry

> Location

### Regime

> Environment

### Fundamentals

> Thesis

Ini jauh lebih clean.

---

# 15. Entry Score baru

Saya sarankan:

```text
ENTRY SCORE

30% Location
25% Flow Confirmation
20% Participation Confirmation
15% Structure
10% Risk/Reward
```

### Location

Bukan sekadar:

```python
prox_30d_high
```

Tetapi:

```text
distance_to_breakout
distance_to_support
ATR normalized distance
```

Contoh:

```text
Price = 0.97 × resistance
ATR = 3%
```

berbeda dengan:

```text
Price = 0.97 × resistance
ATR = 12%
```

Entry risk keduanya tidak sama.

---

# 16. Buat `Entry Quality` berbasis R:R

Ini menurut saya salah satu upgrade terbesar.

Model harus tahu:

```text
entry
stop/invalidation
target
ATR
```

Misalnya:

```text
Entry      = 1.00
Support    = 0.94
Resistance = 1.08
ATR        = 0.025
```

Kemudian:

```text
Risk = 6%
Reward = 8%
R:R = 1.33
```

Bandingkan dengan:

```text
Risk = 3%
Reward = 12%
R:R = 4.0
```

Trend Score bisa sama-sama 80.

Tetapi kualitas entry jelas berbeda.

Jadi Entry Grade harus menangkap **location asymmetry**, bukan sekadar trend.

---

# 17. Saya juga akan menghapus `relative_strength` dari OTF atau menurunkan bobotnya

Sekarang:

```python
score_relative_strength()
```

menggunakan:

```text
IR_7d vs BTC
```

Padahal ini juga sudah masuk Trend.

Jadi terjadi:

```text
Trend
  └── relative strength

OTF
  └── relative strength
```

Ini double counting langsung.

Lebih baik OTF memakai:

```text
flow
location
risk/reward
structure
volatility
```

sedangkan relative strength tetap di Trend/rotation.

---

# 18. Coverage problem lebih serius daripada kelihatannya

Ini sangat penting.

Untuk non-DeFi:

```text
OTF available:
market_structure = 12
relative_strength = 10
execution_quality = 10

total = 32/50 = 64%
```

Kemudian `build_pillar()` melakukan:

```python
scaled = raw * 50 / cov_w
```

Artinya tiga komponen tersebut **di-rescale kembali seolah-olah tersedia 50/50**.

Ini mathematically valid untuk missing-data normalization.

Tetapi secara decision-making:

> **OTF 40 dengan coverage 64% tidak seharusnya dibaca sama dengan OTF 40 dengan coverage 100%.**

Sekarang keduanya bisa menghasilkan grade yang sama.

---

# 19. Solusi: Score + Confidence, jangan hanya Score

Contoh:

```text
Entry Score       40.2
Coverage          64%
Confidence        0.58
Grade             B
```

sedangkan:

```text
Entry Score       40.2
Coverage          100%
Confidence        0.86
Grade             A
```

Ini jauh lebih jujur.

Bahkan saya akan mengganti:

```text
A+
A
B
Avoid
```

menjadi dua dimensi:

```text
ENTRY QUALITY
A / B / C / Avoid

CONFIDENCE
High / Medium / Low
```

---

# 20. Jangan biarkan manual override menentukan entry terlalu besar

`vaf_overrides.json` saat ini sangat berguna untuk fundamental.

Tetapi untuk entry:

```text
catalyst_timing
supply_timing
```

manual score bisa membuat entry grade berubah meskipun market structure tidak berubah.

Saya akan pisahkan:

```text
Fundamental Override
```

dari:

```text
Market Timing Override
```

Dan market timing override sebaiknya:

**tidak boleh menaikkan score secara langsung.**

Hanya boleh:

```text
risk flag
confidence adjustment
```

Misalnya:

```text
unlock < 30 days
→ confidence −15%
```

bukan:

```text
supply timing = 8
→ Entry Score +8
```

---

# 21. Fundamental/VaF justru jangan terlalu banyak disentuh sekarang

Ini penting.

Fundamental dataset baru sekitar:

```text
792 snapshot rows
```

dan sekitar 16 DeFi/infra coins.

Jadi:

**jangan optimize VaF berdasarkan hasil sekarang.**

Yang perlu dilakukan adalah memperbaiki **measurement quality**, bukan weights.

Terutama:

```text
point-in-time
fees
revenue
holders revenue
TVL
market cap
dilution
```

sudah dipersist dengan benar.

Biarkan history bertambah.

Target minimal:

```text
≥60 days
≥2 regimes
≥15 coins
```

baru lakukan recalibration.

---

# 22. `model_audit.py` perlu ditingkatkan

Saat ini audit sudah bagus untuk:

```text
IC
era split
regime split
component redundancy
coverage
```

Saya akan menambah empat hal.

### A. IC by quantile

Bukan hanya:

```text
mean IC
```

tetapi:

```text
Top 10%
Top 20%
Bottom 20%
```

dan forward:

```text
1d
3d
7d
14d
30d
```

Karena mungkin Trend Score tidak bisa memilih "winner", tetapi bisa membantu menghindari losers.

---

### B. Precision by state

Misalnya:

```text
Trend > 70
AND
Flow > 55
AND
Regime = BULL
```

berapa:

```text
median forward return?
hit rate?
tail loss?
```

Ini jauh lebih relevan untuk sistem trading daripada satu IC global.

---

### C. Conditional value

Cari:

```text
P(forward +x | setup)
```

misalnya:

```text
P(+5% within 7d)
P(+10% within 14d)
P(-5% before +5%)
```

Yang terakhir sangat penting.

Karena entry model bukan hanya mencari upside.

Ia harus tahu:

> **berapa sering setup gagal sebelum thesis bekerja?**

---

# 23. Backtest Entry Grade harus memakai path, bukan hanya terminal return

Ini upgrade besar.

Misalnya:

```text
Entry = 100

Day 2 = 92
Day 5 = 110
Day 7 = 115
```

Forward return:

```text
+15%
```

Model sekarang akan menyukai setup itu.

Tetapi untuk entry:

```text
MAE = -8%
```

yang mungkin sebenarnya sudah stop-loss.

Jadi entry test perlu:

```text
MFE
MAE
time-to-MFE
time-to-MAE
```

Contoh:

```text
Grade A
MFE  +11%
MAE  -2%
```

vs

```text
Grade A
MFE  +18%
MAE -14%
```

Keduanya sama-sama +forward return, tetapi kualitas entry sangat berbeda.

---

# 24. Arsitektur yang saya rekomendasikan

Saya akan mengubah pipeline menjadi:

```text
                 ┌──────────────┐
                 │    REGIME    │
                 │ liquidity    │
                 │ breadth      │
                 │ BTC trend    │
                 │ volatility   │
                 └──────┬───────┘
                        │
                        ▼
┌──────────────┐   ┌───────────────┐
│ FUNDAMENTAL  │   │  MARKET STATE │
│              │   │               │
│ VaF          │   │ Flow          │
│ value        │   │ Participation │
│ growth       │   │ Acceleration  │
│ dilution     │   │ Structure     │
└──────┬───────┘   └───────┬───────┘
       │                   │
       │                   ▼
       │            ┌──────────────┐
       │            │ TREND CORE   │
       │            └──────┬───────┘
       │                   │
       │                   ▼
       │            ┌──────────────┐
       │            │ ENTRY ENGINE │
       │            │ location     │
       │            │ R:R          │
       │            │ confirmation │
       │            │ MAE risk     │
       │            └──────┬───────┘
       │                   │
       └──────────┬────────┘
                  ▼
          ┌───────────────┐
          │ DECISION MAP  │
          │               │
          │ Direction     │
          │ Timing        │
          │ Thesis        │
          │ Confidence    │
          └───────────────┘
```

Ini jauh lebih bersih daripada satu `trend_score` yang mencoba menjadi semuanya.

---

# 25. Bentuk output ideal

Saya tidak akan lagi membuat dashboard terutama berbicara:

```text
Trend Score = 82
Entry Grade = A
VaF = 108
```

Saya ingin:

```text
SOLUSDT

REGIME
BULL / EXPANSION

DIRECTION
74 / 100
BUILDING

ACCELERATION
81 / 100
FLOW LEADING PRICE

ENTRY
68 / 100
PULLBACK / GOOD LOCATION

THESIS
VaF 101
Strong

CONFIDENCE
72%

RISK
Medium

STATE
ACCUMULATION → EXPANSION
```

Ini jauh lebih dekat dengan cara discretionary macro/flow trader membaca market.

---

# 26. Prioritas optimasi saya

Saya akan urutkan seperti ini:

| Prioritas | Modul | Perubahan | Dampak |
|---|---|---|---|
| **P0** | `features.py` | pisahkan Direction / Acceleration / Confirmation | Sangat tinggi |
| **P0** | `vaf.py` | redesign Entry OTF | Sangat tinggi |
| **P0** | `collect.py` | jangan campur legacy/v2/v3 dalam ranking live | Sangat tinggi |
| **P1** | `features.py` | percentile-based participation | Tinggi |
| **P1** | `analyzer.py` | flow acceleration/persistence | Tinggi |
| **P1** | `regime.py` | breadth + rotation + liquidity | Tinggi |
| **P1** | `model_audit.py` | MFE/MAE + quantile tests | Sangat tinggi |
| **P2** | `vaf_overrides.json` | pisahkan thesis vs timing override | Medium |
| **P2** | fundamentals | jangan recalibrate weights dulu | Medium |
| **P2** | `backfill.py` | tambah feature history baru | Tinggi untuk research |
| **P3** | `index.html` | ubah UX menjadi state map | Medium |
| **P3** | `validation.html` | tampilkan confidence/coverage/path metrics | Medium |

---

# 27. Satu masalah yang harus diperbaiki sebelum optimasi apa pun

Ini sangat penting dari data aktual.

Latest universe masih memiliki campuran:

```text
v3-coresignal
v2-features
v1-legacy
```

Contoh aktual:

```text
GRASS → v3
CHR   → v3
XEC   → v3
JOE   → v2
RLC   → v2
IQ    → v2
MARS  → v1
```

Dan yang paling mengkhawatirkan:

**MARSCOINUSDT mendapat trend score 81.81 dari v1-legacy meskipun komponen v3 tidak tersedia.**

Sementara GRASS:

```text
Trend = 86.1
Coverage = 72%
Source = OKX
```

Artinya angka:

```text
86.1 vs 81.8
```

**tidak sepenuhnya comparable.**

Ini harus dibereskan sebelum membicarakan ranking.

Saya sarankan:

```text
production_rankable = true
```

hanya apabila:

```text
model_version == current
coverage >= threshold
data_source_quality >= threshold
```

Legacy tetap boleh tampil, tetapi:

```text
LEGACY
NOT RANKABLE
```

---

# 28. Dan saya tidak akan menaikkan bobot Flow menjadi lebih besar sekarang

Walaupun secara konsep flow sangat menarik.

Dokumentasi validation sendiri menyebut volume-flow sebelumnya hanya menghasilkan:

```text
IC ≈ 0
```

dengan improvement conditional sekitar:

```text
+0.06 hit-rate
```

Dan flow rotation v3 baru mempunyai **11 IC-days**.

Jadi posisi saya:

> **Flow layak dipertahankan sebagai early-warning layer, tetapi belum layak dianggap proven alpha.**

Ini justru membuat model lebih sehat.

---

# 29. Formula yang saya ingin uji sebagai `v4`

Bukan langsung mengganti production. Buat eksperimen paralel:

```text
TREND CORE v4

Flow acceleration       25%
Participation            20%
Price acceleration       15%
Compression              10%
Relative strength         5%
Structure confirmation   25%
```

Kemudian:

```text
REGIME = external context
```

dan:

```text
ENTRY = independent engine
```

Jadi **Entry tidak mengambil Trend Score sebagai input langsung**.

Itu penting untuk mengurangi circularity.

---

## Entry v4

```text
Location                  25%
Flow confirmation         25%
Participation             15%
Structure                 15%
Risk/Reward               20%
```

Kemudian:

```text
Entry Quality
+
Entry Confidence
```

bukan hanya A+/A/B/Avoid.

---

# 30. Target akhir model

Saya kira arah optimalisasi SFC Altcoin Terminal bukan:

> **"mencari satu skor yang paling akurat."**

Tetapi:

> **membangun mesin yang tahu kapan sinyal sedang terbentuk, kapan sinyal sudah terlambat, dan kapan lingkungan makro/regime membuat sinyal tersebut layak dipercaya.**

Secara praktis:

```text
REGIME
   ↓
DIRECTION
   ↓
ACCELERATION
   ↓
CONFIRMATION
   ↓
LOCATION
   ↓
RISK/REWARD
   ↓
CONFIDENCE
```

Dan **fundamental/VaF berada paralel sebagai thesis layer**, bukan dipaksa menjadi timing signal.

Itu menurut saya merupakan evolusi paling penting dari repo ini.

**Kalau harus memilih hanya tiga perubahan untuk dikerjakan sekarang:**  
**(1)** pisahkan Trend vs Acceleration vs Entry, **(2)** hilangkan double-counting `IR_7d` dari Trend + OTF, dan **(3)** jadikan coverage/model-version sebagai bagian dari rankability/confidence, bukan hanya metadata. Ini akan memperbaiki kualitas mesin jauh lebih besar daripada sekadar mengubah angka bobot.

---

If you want, I can:

- Jelaskan lebih detail tentang ‘Acceleration Score’ dan komponennya
- Jelaskan implementasi ‘Entry Quality’ berbasis risk/reward ratio
- Bahas prioritas optimasi modul dan dampaknya secara rinci