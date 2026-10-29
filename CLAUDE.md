# Konteks Proyek — Trading Bot Bitget

File ini buat dibaca Claude Code di awal sesi. Isinya temuan yang udah dibayar
mahal (waktu dan uang) selama tiga hari. **Baca dulu sebelum ngusulin perubahan** —
banyak ide yang kelihatan bagus di sini udah diuji dan gagal.

---

## Isi folder

| File | Fungsi |
|---|---|
| `bot_scalp.py` | Bot utama. Scanner multi-pair + AI planner + eksekusi. Ini yang aktif. |
| `backtest.py` | Harness backtest. Ngetes aturan deterministik di data historis, tanpa call AI. |
| `core/` | Indikator, matematika fee/leverage/sizing, dan metrik. Dipakai bareng bot & backtest — ubah di sini, bukan disalin. |
| `teacher_bot.py` | CLI buat ngisi `knowledge.md` dari teks/gambar. Belum dibaca bot. |
| `scripts/report.py` | Statistik dari journal (win rate, breakeven, margin, PF). |
| `scripts/check_connections.py` | Cek `.env`, koneksi Bitget, dan Anthropic. |
| `legacy/bot_v1.py` | Versi lama (BTC-only, SL/TP persentase tetap). Disimpan sebagai pembanding, jangan dikembangin. |
| `tests/` | `python -m pytest -q`. Wajib lulus sebelum commit. |
| `journal_scalp.jsonl` | **Jangan dihapus.** Semua trade, keputusan, dan penolakan. Ini sumber data satu-satunya. |
| `scalp.log` | Log berjalan, rotasi 5MB x 3. |
| `data/*.csv` | Candle historis buat backtest. |
| `.env` | `BITGET_API_KEY`, `BITGET_API_SECRET`, `BITGET_PASSPHRASE`, `ANTHROPIC_API_KEY`. Jangan pernah dibaca isinya atau ditampilkan. |

## Cara jalanin

```bash
python bot_scalp.py                      # bot (DRY_RUN, nggak kirim order)
python backtest.py fetch                 # unduh 30 hari candle
python backtest.py run                   # backtest config sekarang
python backtest.py atr                   # sebaran ATR per pair
python backtest.py sweep <PARAM>         # cari nilai terbaik (SATU sampel)
python backtest.py validate <PARAM>      # train 70% / test 30% <- INI yang dipakai
python scripts/report.py --by-symbol     # statistik journal live/dry run
python -m pytest -q                      # unit test
```

---

## Kondisi sekarang (11 Agustus 2026)

- `DRY_RUN = True`. **Belum pernah live.** Jangan diubah tanpa diminta eksplisit.
- Risiko 0.5% saldo per trade. Config disetel hemat biaya API:
  `AI_EFFORT="low"`, 5 kandidat, siklus 300 detik.
- Data live terkumpul: **12 trade**. Menang 3 (25%), breakeven 38%,
  margin −13.0, profit factor 0.54, total −13.72 USDT.

**Rencana yang disepakati: jalanin tanpa diubah sampai 40–50 trade.**
Jangan tuning parameter sebelum itu.

---

## Yang SUDAH diuji dan GAGAL — jangan diulang

1. **Sinyal EMA20/50 di timeframe 5m nggak punya daya prediksi arah.**
   Dibuktikan dua arah: versi normal rugi, versi sinyal-dibalik juga rugi.
   110 trade, z ≈ −4.2. Ini bukan soal parameter.

2. **Tuning parameter nggak nolong.** `validate` (train/test) nunjukin setiap
   nilai yang membaik di TRAIN justru memburuk di TEST. Termasuk temuan
   "market entry lebih baik dari limit" — itu ambyar di luar sampel.

3. **Ganti model (Opus/Fable) bukan solusi.** Masalahnya bukan kualitas
   penalaran; AI-nya konsisten baca arah dengan benar. Yang kosong sinyalnya.

4. **MA crossover, TradingView, MCP server, framework multi-agent** — semuanya
   pernah dipertimbangkan. Nggak satupun nambah informasi baru; cuma cara lain
   ngolah data yang sama.

---

## Pelajaran teknis yang mahal (jangan sampai terulang)

- **Simulasi harus pesimis.** Bug-bug ini sempat bikin hasil dry run kelihatan
  jauh lebih bagus dari kenyataan: limit order dianggap pasti kefill; PnL
  dihitung di harga saat dicek bukan di level SL/TP; fee diabaikan; candle
  sebelum order dipasang ikut dinilai. Semua udah dibenerin — jangan
  diregresikan.
- **Token "thinking" makan jatah `max_tokens` yang sama** dengan jawaban.
  Jangan turunin `AI_MAX_TOKENS` di bawah ~3000; yang kepotong bukan panjang
  jawaban tapi ada-nya jawaban.
- **Indikator & rumus fee cuma boleh ada di `core/`.** Dulu bot dan backtest
  punya salinan masing-masing, dan ATR-nya diam-diam beda rumus (Wilder vs
  SMA). Backtest sekarang pilih lewat `ATR_METHOD` (default `sma` biar hasil
  lama bisa direproduksi; `wilder` = sama persis dengan bot).
- **Bitget batesin OHLCV 200 per request.** Paginasi jangan berhenti waktu
  batch lebih kecil dari `limit`.
- **Fee: maker 0.02% / taker 0.06%** (USDT-M VIP0, dikonfirmasi dari akun).
  Limit entry = maker, TP = maker, SL = taker.
- **Prompt harus cocok sama `ENTRY_MODE`.** Kalau prompt minta rencana pullback
  sementara eksekusinya market, semua plan ditolak karena entry jauh dari harga.
- **Sizing berbasis risiko:** `notional = (RISK_PCT% x saldo) / (SL% + fee)`.
  SL lebar otomatis mengecilkan posisi. Leverage nggak ngaruh ke PnL USDT —
  cuma nentuin berapa margin yang ketahan.

---

## Cara nilai hasil (penting)

Jangan pakai net profit doang. Yang dipakai:

- **breakeven win rate** = rata2 kalah / (rata2 kalah + rata2 menang)
- **margin** = win rate − breakeven. Positif = ada edge. Ini angka utamanya.
- **profit factor** = total menang / total kalah
- Di bawah 30 trade, jangan simpulkan apa pun.
- Parameter dipilih di TRAIN, dinilai di TEST. Jangan pernah nilai di data
  yang dipakai buat milih.

**Pembanding (grup kontrol, planner mekanis tanpa AI, out-of-sample):**
margin ≈ −26, profit factor ≈ 0.25.

Pertanyaan terbuka satu-satunya: **apakah bot AI ngalahin angka itu secara
meyakinkan?** Indikasi awal iya (PF 0.54 vs 0.25) tapi sampelnya baru 12.

---

## Ide yang belum diuji (kalau nanti mau lanjut)

Urut dari yang paling menjanjikan:

1. **Data khas perp yang belum dipakai sama sekali**: funding rate, open
   interest, long/short ratio, data likuidasi, kedalaman orderbook. Ini
   kategori informasi baru, bukan indikator yang sama dengan nama lain.
2. **Timeframe lebih besar (1h/4h).** Satu baris config, backtest 2 detik.
   Kompetisi jauh lebih longgar dari 5m, dan fee jadi kecil relatif ke gerakan.
3. **`knowledge.md` dari Teacher Bot belum pernah dibaca `bot_scalp.py`.**
   Seluruh materi training (buku, screenshot trading plan) nggak pernah nyampe
   ke keputusan trading. Pipeline-nya terputus.
4. **Feedback loop journal masih per-pair**, padahal scanner muter pair terus —
   68% permintaan plan dapat "belum ada riwayat". Harusnya per-POLA
   ("setup pullback EMA20 udah 5x dicoba, menang 1"), bukan per-pair.

---

## Batasan

- Jangan pernah nampilin atau nge-log isi `.env`.
- Jangan ubah `DRY_RUN` ke `False` tanpa permintaan eksplisit dan pembahasan
  terpisah. Jalur live belum pernah dieksekusi sekali pun — order asli,
  `setup_pair`, penanganan fill parsial semuanya belum teruji.
- Jangan hapus atau timpa `journal_scalp.jsonl`. Kalau perlu reset, ganti nama.
- Perubahan yang naikin jumlah call AI harus disebutin biayanya di depan.
