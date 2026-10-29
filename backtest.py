#!/usr/bin/env python3
"""
BACKTEST HARNESS buat bot_scalp.py
===================================

Masalah yang dipecahin: dry run cuma ngasih ~0.9 trade/jam. Buat 50 trade
butuh 54 jam. Tiap kali satu parameter diubah, hitungannya balik nol.

Di sini logika yang sama dijalanin di atas candle historis. Sebulan data
x 10 pair = ratusan trade dalam hitungan detik.

PENTING — apa yang diuji dan apa yang TIDAK:

  DIUJI  : semua yang deterministik. Ukuran posisi, aturan SL vs ATR,
           MIN_RR, filter RSI, cooldown, timeout limit, fee, cara SL/TP
           kesentuh. Ini yang selama ini lo tune sambil nebak.

  TIDAK  : kualitas penilaian AI. Manggil AI ratusan ribu kali buat
           backtest itu nggak masuk akal biayanya.

Gantinya, planner di sini pakai aturan eksplisit yang niru cara AI nyusun
plan: entry di pullback ke EMA20, SL di balik swing terdekat, TP di swing
berikutnya. Itu jadi GRUP KONTROL.

Gunanya: kalau bot AI lo nggak ngalahin planner bodoh ini, berarti AI-nya
belum nambah nilai apa-apa dan biayanya nggak kebayar. Kalau ngalahin,
selisihnya itu kontribusi asli si AI — dan lo punya angkanya.

Cara pakai:
    python backtest.py fetch                # unduh & simpan data
    python backtest.py run                  # backtest config sekarang
    python backtest.py sweep MIN_RR         # cari nilai terbaik
    python backtest.py sweep SL_ATR_MULT
"""

import os
import sys
import csv
import json
import math
import time
import itertools
from datetime import datetime, timezone

from core import fees
from core import indicators as ind

# Dipatok ke folder tempat backtest.py berada, bukan folder tempat perintah
# dijalankan. Kalau relatif ke cwd, `fetch` dari satu folder dan `run` dari
# folder lain bakal nyari data di tempat yang beda.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')

# ============================================================
# KONFIGURASI — cerminan bot_scalp.py. Ubah di sini buat nguji.
# ============================================================
SYMBOLS = [
    'BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT',
    'DOGE/USDT:USDT', 'XRP/USDT:USDT', 'LINK/USDT:USDT',
    'AVAX/USDT:USDT', 'SUI/USDT:USDT', 'WLD/USDT:USDT',
    'ARB/USDT:USDT',
]
TIMEFRAME = '5m'
DAYS = 30

# Risiko & ukuran
BALANCE_START = 1000.0
RISK_PCT = 0.5
MAX_LEVERAGE = 20
# Fee dipisah maker/taker. Waktu sistemnya nyaris impas, asumsi "semua taker"
# bukan pembulatan — selisihnya seukuran hasilnya sendiri.
#   masuk  : limit order di zona -> MAKER
#   TP     : limit order         -> MAKER
#   SL     : market/stop         -> TAKER
# CEK TIER FEE AKUNMU SENDIRI DI BITGET dan sesuaikan dua angka ini.
TAKER_FEE_PCT = 0.06
MAKER_FEE_PCT = 0.02
ASSUME_MAKER_ENTRY = True

# Aturan level
ATR_PERIOD = 14
# 'sma'    = rumus lama backtest (rata-rata 14 true range terakhir).
# 'wilder' = rumus yang dipakai bot_scalp.py live. Pakai ini kalau mau
#            backtest benar-benar sejalan dengan bot.
ATR_METHOD = 'sma'
SL_ATR_MULT = 1.2
TP_ATR_MULT = 2.4
MIN_SL_PCT = 0.70
MAX_SL_PCT = 3.50
MIN_RR = 1.3
MIN_SL_WINDOW_PP = 0.40

# Filter
RSI_MIN = 25
RSI_MAX = 72
# Lantai ATR diturunkan drastis.
#
# Nilai lama 0.58 itu turunan MIN_SL_PCT/SL_ATR_MULT, dengan asumsi "pair
# terlalu diam nggak bisa ditradingin". Asumsi itu keliru: di pair ber-ATR
# rendah, SL yang dipatok di lantai fee 0.70% justru jadi 3-7x ATR — jauh
# LEBIH aman dari noise ketimbang pair liar yang cuma dapat 1.2x.
# Efek nilai lama: 83.828 dari 84.790 peluang (98.9%) dibuang, dan yang
# tersisa cuma altcoin ekstrem.
MIN_ATR_PCT = 0.12
MAX_ATR_PCT = 2.92

# Eksekusi
# --- EKSPERIMEN: cara masuk ---
# 'limit'  : pasang order di EMA20, tunggu harga datang.
# 'market' : langsung masuk di harga sekarang.
#
# Hipotesis yang mau diuji: limit buy di uptrend cuma kefill kalau harga
# BENERAN turun ke situ. Kalau trennya lanjut tanpa pullback — skenario
# terbaik — kita nggak pernah masuk. Jadi yang kefill condong ke pullback
# yang berubah jadi pembalikan. Fill-nya sendiri jadi sinyal buruk.
ENTRY_MODE = 'limit'

# Market order nggak kefill di harga penutupan bar. Ada spread dan slippage,
# dan di backtest hal itu gratis kalau nggak dimodelkan — bikin mode 'market'
# kelihatan lebih bagus dari kenyataannya. Stop loss juga kena: dia jadi
# market order pas kesentuh.
# Isi sesuai spread pair yang lo tradingin. 0.02-0.05% wajar buat pair likuid,
# lebih besar buat altcoin tipis.
SLIPPAGE_PCT = 0.03

# Kalau struktur terdekat lebih rapat dari SL_ATR_MULT x ATR:
#   False = tolak setup (perilaku sekarang)
#   True  = lebarkan SL sampai memenuhi syarat, walau lewat struktur
#
# Ini pertanyaan nyata, bukan teori. Di tren kuat, struktur invalidasi
# (swing terakhir) justru RAPAT sementara ATR BESAR — jadi aturan tolak
# memblokir persis setup tren yang paling kuat. GRVT ditolak 10x berturut
# dengan alasan identik sementara harganya turun 10%.
ALLOW_SL_WIDEN = False

# Balik arah semua sinyal. Bukan buat dipakai — buat diagnosa. Kalau versi
# terbalik hasilnya jauh lebih baik, berarti logika arahnya yang salah,
# bukan eksekusinya.
REVERSE = False

ENTRY_TIMEOUT_BARS = 5          # 5 siklus x 180s ~ 15 menit ~ 3 bar 5m
MAX_CONCURRENT = 2
COOLDOWN_BARS = 10
SWING_LOOKBACK = 20             # buat nyari struktur SL
TARGET_LOOKBACK = 40            # buat nyari struktur TP

# Asumsi konservatif: kalau satu candle nyentuh SL dan TP dua-duanya,
# anggap SL yang duluan. Tanpa data tick, ini satu-satunya asumsi jujur —
# yang sebaliknya bakal bikin hasil backtest kelewat bagus.
SL_WINS_TIES = True


# ============================================================
# INDIKATOR — implementasinya di core/indicators.py (dipakai bareng bot)
# ============================================================
def ema(vals, period):
    return ind.ema_last(vals, period)


def rsi(closes, period=14):
    return ind.rsi(closes, period)


def atr(candles, period=ATR_PERIOD):
    fn = ind.atr if ATR_METHOD == 'wilder' else ind.atr_sma
    return fn(candles, period)


def round_trip_fee(notional, exit_kind='stop_loss'):
    return fees.round_trip_fee(notional, ENTRY_MODE, exit_kind, MAKER_FEE_PCT, TAKER_FEE_PCT, ASSUME_MAKER_ENTRY)


def worst_fee_pct():
    return fees.worst_fee_pct(ENTRY_MODE, MAKER_FEE_PCT, TAKER_FEE_PCT, ASSUME_MAKER_ENTRY)


def net_rr(sl_pct, tp_pct):
    return fees.net_rr(sl_pct, tp_pct, ENTRY_MODE, MAKER_FEE_PCT, TAKER_FEE_PCT, ASSUME_MAKER_ENTRY)


def safe_leverage(sl_pct):
    return fees.safe_leverage(sl_pct, MAX_LEVERAGE)


# ============================================================
# DATA
# ============================================================
def fetch_all():
    """Unduh candle historis dan simpan ke CSV. Perlu koneksi + ccxt."""
    import ccxt
    ex = ccxt.bitget({'enableRateLimit': True,
                      'options': {'defaultType': 'swap'}})
    ex.load_markets()
    os.makedirs(DATA_DIR, exist_ok=True)

    ms = ex.parse_timeframe(TIMEFRAME) * 1000
    # Bitget menolak permintaan lebih dari 200 dan diam-diam memotongnya.
    BATCH_LIMIT = 200
    since_start = ex.milliseconds() - DAYS * 24 * 60 * 60 * 1000

    for sym in SYMBOLS:
        path = os.path.join(DATA_DIR, sym.replace('/', '_').replace(':', '_') + '.csv')
        rows, since = [], since_start
        now = ex.milliseconds()
        print(f"unduh {sym} ...", end=' ', flush=True)

        # Bitget cuma ngasih maksimal 200 candle per request, berapapun yang
        # diminta. Jadi JANGAN berhenti waktu batch lebih kecil dari `limit` —
        # itu kondisi yang bikin loop mati setelah satu batch dan tiap pair
        # cuma dapet 200 bar. Yang dipakai: berhenti kalau waktunya nggak maju
        # lagi, atau udah nyampe sekarang.
        guard = 0
        while since < now and guard < 500:
            guard += 1
            try:
                batch = ex.fetch_ohlcv(sym, TIMEFRAME, since=since, limit=BATCH_LIMIT)
            except Exception as e:
                print(f"gagal: {e}")
                break
            if not batch:
                break
            rows.extend(batch)
            next_since = batch[-1][0] + ms
            if next_since <= since:      # nggak maju -> hentikan
                break
            since = next_since
            print('.', end='', flush=True)
            time.sleep(ex.rateLimit / 1000)
        print(' ', end='')

        if len(rows) < 200:
            print(f"CUMA {len(rows)} bar — terlalu sedikit, kemungkinan pair "
                  f"ini nggak ada di Bitget swap atau namanya beda.")
        if rows:
            seen, uniq = set(), []
            for r in rows:
                if r[0] not in seen:
                    seen.add(r[0])
                    uniq.append(r)
            uniq.sort(key=lambda r: r[0])
            with open(path, 'w', newline='', encoding='utf-8') as f:
                csv.writer(f).writerows(uniq)
            hari = (uniq[-1][0] - uniq[0][0]) / 86_400_000
            print(f"{len(uniq)} candle ({hari:.1f} hari) -> {os.path.basename(path)}")
        else:
            print("kosong")


def load(sym):
    path = os.path.join(DATA_DIR, sym.replace('/', '_').replace(':', '_') + '.csv')
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding='utf-8') as f:
        for row in csv.reader(f):
            if len(row) >= 6:
                out.append([int(float(row[0]))] + [float(x) for x in row[1:6]])
    return out


# ============================================================
# PLANNER BERBASIS ATURAN (grup kontrol menggantikan AI)
# ============================================================
def make_plan(candles, i):
    """Susun plan pakai candle 0..i saja.

    Cuma pakai data sampai indeks i — nggak boleh ngintip masa depan.
    Lookahead bias itu cara paling gampang bikin backtest yang indah tapi
    bohong, jadi batas ini dijaga ketat.
    """
    window = candles[max(0, i - 120):i + 1]
    if len(window) < 60:
        return None, 'data kurang'

    closes = [c[4] for c in window]
    price = closes[-1]
    a = atr(window)
    if not a:
        return None, 'atr kosong'
    atr_pct = a / price * 100

    if not (MIN_ATR_PCT <= atr_pct <= MAX_ATR_PCT):
        return None, 'atr di luar rentang'

    sl_floor = max(MIN_SL_PCT, atr_pct * SL_ATR_MULT)
    if MAX_SL_PCT - sl_floor < MIN_SL_WINDOW_PP:
        return None, 'jendela SL sempit'

    rs = rsi(closes, 14)
    if rs is None or not (RSI_MIN <= rs <= RSI_MAX):
        return None, 'rsi di luar rentang'

    e20, e50 = ema(closes, 20), ema(closes, 50)
    if not e20 or not e50:
        return None, 'ema kosong'

    swing_lo = min(c[3] for c in window[-SWING_LOOKBACK:])
    swing_hi = max(c[2] for c in window[-SWING_LOOKBACK:])
    target_lo = min(c[3] for c in window[-TARGET_LOOKBACK:])
    target_hi = max(c[2] for c in window[-TARGET_LOOKBACK:])

    # Arah dari struktur EMA, entry nunggu pullback ke EMA20 —
    # niru pola "pullback ke EMA20/50 dalam uptrend" yang dipakai AI.
    if e20 > e50 and price > e50:
        side = 'buy'
        entry = e20
        sl = swing_lo * (1 - 0.001)
        tp = max(target_hi, entry * (1 + TP_ATR_MULT * a / entry))
    elif e20 < e50 and price < e50:
        side = 'sell'
        entry = e20
        sl = swing_hi * (1 + 0.001)
        tp = min(target_lo, entry * (1 - TP_ATR_MULT * a / entry))
    else:
        return None, 'tidak ada tren jelas'

    if entry <= 0:
        return None, 'entry invalid'

    # urutan level harus benar
    if side == 'buy' and not (sl < entry < tp):
        return None, 'urutan level salah'
    if side == 'sell' and not (tp < entry < sl):
        return None, 'urutan level salah'

    sl_pct = abs(entry - sl) / entry * 100
    tp_pct = abs(tp - entry) / entry * 100

    if sl_pct < sl_floor:
        if not ALLOW_SL_WIDEN:
            return None, 'SL lebih rapat dari syarat ATR'
        # Lebarkan SL ke syarat ATR, geser menjauh dari struktur.
        sl_pct = sl_floor
        sl = (entry * (1 - sl_pct / 100) if side == 'buy'
              else entry * (1 + sl_pct / 100))
    if sl_pct > MAX_SL_PCT:
        return None, 'SL lewat plafon'

    rr = net_rr(sl_pct, tp_pct)
    if rr < MIN_RR:
        return None, 'RR kurang'

    if REVERSE:
        side = 'sell' if side == 'buy' else 'buy'
        sl, tp = entry + (entry - sl), entry + (entry - tp)
        if side == 'buy' and not (sl < entry < tp):
            return None, 'reverse invalid'
        if side == 'sell' and not (tp < entry < sl):
            return None, 'reverse invalid'
        sl_pct = abs(entry - sl) / entry * 100
        tp_pct = abs(tp - entry) / entry * 100
        rr = net_rr(sl_pct, tp_pct)
        if rr < MIN_RR:
            return None, 'RR kurang (reverse)'

    return {'side': side, 'entry': entry, 'sl': sl, 'tp': tp,
            'sl_pct': sl_pct, 'tp_pct': tp_pct, 'rr': rr,
            'leverage': safe_leverage(sl_pct), 'atr_pct': atr_pct,
            'rsi': rs}, None


# ============================================================
# MESIN BACKTEST
# ============================================================
OOS_SPLIT = 0.70   # 70% pertama buat tuning, 30% terakhir buat ujian


def run_backtest(symbols=None, verbose=False, portion='all'):
    """portion: 'all' | 'train' | 'test'.

    'train' = bagian awal data, tempat parameter boleh dipas-pasin.
    'test'  = bagian akhir, TIDAK BOLEH dipakai buat milih parameter.
              Ini satu-satunya angka yang berarti buat mutusin.
    """
    symbols = symbols or SYMBOLS
    raw = {s: load(s) for s in symbols}
    data = {s: d for s, d in raw.items() if len(d) > 200}

    if not data:
        print(f"Nggak ada data yang kepakai. Nyari di: {DATA_DIR}")
        if not os.path.isdir(DATA_DIR):
            print("  Foldernya nggak ada. Jalanin: python backtest.py fetch")
            return None
        found = sorted(f for f in os.listdir(DATA_DIR) if f.endswith('.csv'))
        if not found:
            print("  Folder ada tapi kosong. Jalanin: python backtest.py fetch")
            return None
        print(f"  Ketemu {len(found)} file CSV, tapi nggak ada yang lebih dari 200 bar:")
        for f in found[:12]:
            try:
                n = sum(1 for _ in open(os.path.join(DATA_DIR, f), encoding='utf-8'))
            except Exception as e:
                n = f'gagal dibaca ({e})'
            print(f"    {f:36} {n} baris")
        print("  Kemungkinan fetch-nya kepotong. Ulangi: python backtest.py fetch")
        return None

    kurang = [s for s, d in raw.items() if 0 < len(d) <= 200]
    hilang = [s for s, d in raw.items() if not d]
    if kurang:
        print(f"  ({len(kurang)} pair dilewati karena datanya kurang dari 200 bar)")
    if hilang:
        print(f"  ({len(hilang)} pair nggak ada filenya: "
              f"{', '.join(x.split('/')[0] for x in hilang[:6])})")

    balance = BALANCE_START
    trades, rejects = [], {}
    open_pos = {}       # symbol -> posisi
    pending = {}        # symbol -> limit order
    cooldown = {}       # symbol -> index sampai kapan

    n = min(len(d) for d in data.values())
    split = int(n * OOS_SPLIT)
    if portion == 'train':
        n = split
        start = 120
    elif portion == 'test':
        start = max(120, split)
    else:
        start = 120

    for i in range(start, n):
        # --- 1. urus limit yang nunggu ---
        for sym in list(pending):
            p = pending[sym]
            bar = data[sym][i]
            hi, lo = bar[2], bar[3]
            touched = (lo <= p['entry']) if p['side'] == 'buy' else (hi >= p['entry'])
            if touched:
                notional = p['notional']
                open_pos[sym] = {**p, 'opened_at': i,
                                 'contracts': notional / p['entry']}
                del pending[sym]
            else:
                p['bars'] += 1
                if p['bars'] >= ENTRY_TIMEOUT_BARS:
                    del pending[sym]

        # --- 2. urus posisi terbuka ---
        for sym in list(open_pos):
            pos = open_pos[sym]
            if i <= pos['opened_at']:
                continue
            bar = data[sym][i]
            hi, lo = bar[2], bar[3]
            d = 1 if pos['side'] == 'buy' else -1
            hit_sl = (lo <= pos['sl']) if d == 1 else (hi >= pos['sl'])
            hit_tp = (hi >= pos['tp']) if d == 1 else (lo <= pos['tp'])

            fill = reason = None
            if hit_sl and hit_tp:
                fill, reason = (pos['sl'], 'stop_loss') if SL_WINS_TIES else (pos['tp'], 'take_profit')
            elif hit_sl:
                fill, reason = pos['sl'], 'stop_loss'
            elif hit_tp:
                fill, reason = pos['tp'], 'take_profit'

            if fill:
                if reason == 'stop_loss':
                    # stop jadi market order -> ikut kena slippage
                    fill *= (1 - SLIPPAGE_PCT / 100) if d == 1 else (1 + SLIPPAGE_PCT / 100)
                fee = round_trip_fee(pos['notional'], reason)
                pnl = (fill - pos['entry']) * d * pos['contracts'] - fee
                balance += pnl
                trades.append({
                    'symbol': sym, 'side': pos['side'], 'entry': pos['entry'],
                    'exit': fill, 'pnl': pnl, 'reason': reason,
                    'rr': pos['rr'], 'sl_pct': pos['sl_pct'],
                    'tp_pct': pos['tp_pct'], 'atr_pct': pos['atr_pct'],
                    'bars_held': i - pos['opened_at'],
                    'ts': data[sym][i][0], 'balance': balance,
                })
                if pnl < 0:
                    cooldown[sym] = i + COOLDOWN_BARS
                del open_pos[sym]

        # --- 3. cari setup baru buat slot kosong ---
        busy = set(open_pos) | set(pending)
        if len(busy) >= MAX_CONCURRENT:
            continue

        for sym in data:
            if len(busy) >= MAX_CONCURRENT:
                break
            if sym in busy or cooldown.get(sym, -1) > i:
                continue
            plan, err = make_plan(data[sym], i)
            if err:
                rejects[err] = rejects.get(err, 0) + 1
                continue
            notional = fees.risk_notional(balance, RISK_PCT, plan['sl_pct'],
                                          worst_fee_pct())
            if ENTRY_MODE == 'market':
                # Masuk di harga penutupan bar ini — geser level supaya
                # jarak SL/TP-nya tetap sama seperti yang direncanakan.
                px = data[sym][i][4]
                # bayar spread: beli sedikit lebih mahal, jual sedikit lebih murah
                px *= (1 + SLIPPAGE_PCT / 100) if plan['side'] == 'buy' else (1 - SLIPPAGE_PCT / 100)
                shift = px - plan['entry']
                open_pos[sym] = {**plan, 'entry': px,
                                 'sl': plan['sl'] + shift,
                                 'tp': plan['tp'] + shift,
                                 'notional': notional, 'opened_at': i,
                                 'contracts': notional / px}
            else:
                pending[sym] = {**plan, 'notional': notional, 'bars': 0}
            busy.add(sym)

    return {'trades': trades, 'rejects': rejects, 'portion': portion,
            'balance': balance, 'bars': n - start,
            'symbols': list(data)}


# ============================================================
# STATISTIK
# ============================================================
def stats(res, label=''):
    t = res['trades']
    if not t:
        print(f"{label}: nol trade")
        return {}
    wins = [x for x in t if x['pnl'] > 0]
    losses = [x for x in t if x['pnl'] <= 0]
    wr = len(wins) / len(t) * 100
    aw = sum(x['pnl'] for x in wins) / len(wins) if wins else 0
    al = abs(sum(x['pnl'] for x in losses) / len(losses)) if losses else 0
    be = al / (al + aw) * 100 if (al + aw) else 0
    total = sum(x['pnl'] for x in t)

    peak = BALANCE_START
    dd = 0
    for x in t:
        peak = max(peak, x['balance'])
        dd = max(dd, (peak - x['balance']) / peak * 100)

    print(f"\n{'=' * 58}")
    if label:
        print(label)
        print('=' * 58)
    print(f"  trade            : {len(t)}")
    print(f"  win rate         : {wr:.1f}%")
    print(f"  breakeven        : {be:.1f}%   <- ambang dari rata2 menang/kalah")
    print(f"  margin           : {wr - be:+.1f} poin")
    print(f"  rata2 menang     : +{aw:.2f} USDT")
    print(f"  rata2 kalah      : -{al:.2f} USDT")
    print(f"  total PnL        : {total:+.2f} USDT ({total / BALANCE_START * 100:+.1f}%)")
    print(f"  saldo akhir      : {res['balance']:.2f}")
    print(f"  max drawdown     : {dd:.1f}%")
    print(f"  ekspektasi/trade : {total / len(t):+.3f} USDT")
    print(f"  rata2 bar ditahan: {sum(x['bars_held'] for x in t) / len(t):.1f}")
    return {'trades': len(t), 'wr': wr, 'be': be, 'total': total, 'dd': dd,
            'exp': total / len(t)}


def show_rejects(res):
    print("\n  alasan setup nggak jadi (top 8):")
    for k, v in sorted(res['rejects'].items(), key=lambda x: -x[1])[:8]:
        print(f"    {k:32} {v:>8,}")


def metrics(res):
    t = res['trades']
    if not t:
        return None
    wins = [x for x in t if x['pnl'] > 0]
    losses = [x for x in t if x['pnl'] <= 0]
    aw = sum(x['pnl'] for x in wins) / len(wins) if wins else 0
    al = abs(sum(x['pnl'] for x in losses) / len(losses)) if losses else 0
    wr = len(wins) / len(t) * 100
    be = al / (al + aw) * 100 if (al + aw) else 0
    gross_w = sum(x['pnl'] for x in wins)
    gross_l = abs(sum(x['pnl'] for x in losses)) or 1e-9
    peak, dd = BALANCE_START, 0
    for x in t:
        peak = max(peak, x['balance'])
        dd = max(dd, (peak - x['balance']) / peak * 100)
    return {'n': len(t), 'wr': wr, 'be': be, 'margin': wr - be,
            'pnl': sum(x['pnl'] for x in t), 'pf': gross_w / gross_l, 'dd': dd}


def validate(param, values):
    """Cari nilai terbaik di data TRAIN, lalu uji di data TEST.

    Ini pengaman anti-overfitting. Milih parameter dan menilai hasilnya di
    data yang sama itu cara paling gampang menipu diri sendiri: dengan
    cukup banyak percobaan, SELALU ada nilai yang kelihatan bagus di data
    yang sudah kita lihat.

    Angka yang menentukan cuma satu: kolom TEST.
    """
    orig = globals()[param]
    print(f"\nVALIDASI {param}  (train {OOS_SPLIT:.0%} / test {1-OOS_SPLIT:.0%})\n")
    print(f"{'nilai':>8} | {'--- TRAIN ---':^26} | {'--- TEST (ujian) ---':^26}")
    print(f"{'':>8} | {'trade':>6} {'margin':>7} {'PF':>5} {'PnL':>6} | "
          f"{'trade':>6} {'margin':>7} {'PF':>5} {'PnL':>6}")
    print('-' * 74)
    rows = []
    for v in values:
        globals()[param] = v
        tr = metrics(run_backtest(portion='train'))
        te = metrics(run_backtest(portion='test'))
        def fmt(m):
            if not m:
                return f"{'0':>6} {'-':>7} {'-':>5} {'-':>6}"
            return f"{m['n']:>6} {m['margin']:>+7.1f} {m['pf']:>5.2f} {m['pnl']:>+6.0f}"
        print(f"{v:>8} | {fmt(tr)} | {fmt(te)}")
        if tr and te:
            rows.append((v, tr, te))
    globals()[param] = orig

    # Cuma pertimbangkan nilai yang punya cukup trade di TRAIN. Tanpa ini,
    # pemenangnya selalu nilai paling ketat yang kebetulan menang 1 dari 1.
    MIN_TRAIN_TRADES = 30
    layak = [r for r in rows if r[1]['n'] >= MIN_TRAIN_TRADES]
    if not layak:
        print(f"\nNggak ada nilai dengan >={MIN_TRAIN_TRADES} trade di TRAIN. "
              f"Semua terlalu ketat buat dinilai.")
        return rows

    if layak:
        best = max(layak, key=lambda r: r[1]['margin'])
        v, tr, te = best
        print(f"\n(hanya nilai dengan >={MIN_TRAIN_TRADES} trade di TRAIN yang dipertimbangkan)")
        print(f"\nTerbaik di TRAIN: {param} = {v} (margin {tr['margin']:+.1f})")
        print(f"Nilai itu di TEST: margin {te['margin']:+.1f}, PF {te['pf']:.2f}, "
              f"{te['n']} trade")
        if te['margin'] > 0 and te['n'] >= 20:
            print("-> BERTAHAN di data yang belum pernah dilihat.")
        elif te['n'] < 20:
            print("-> Trade di TEST terlalu sedikit buat menyimpulkan apa pun.")
        else:
            print("-> AMBYAR di luar sampel. Yang tadi kelihatan bagus cuma kebetulan.")
    return rows


def sweep(param, values):
    print(f"\nSWEEP {param}")
    print(f"{'nilai':>8} | {'trade':>6} | {'win%':>6} | {'be%':>6} | "
          f"{'margin':>7} | {'PnL':>9} | {'DD%':>6}")
    print('-' * 66)
    orig = globals()[param]
    rows = []
    for v in values:
        globals()[param] = v
        res = run_backtest()
        t = res['trades']
        if not t:
            print(f"{v:>8} |      0 |      - |      - |       - |         - |      -")
            continue
        wins = [x for x in t if x['pnl'] > 0]
        losses = [x for x in t if x['pnl'] <= 0]
        wr = len(wins) / len(t) * 100
        aw = sum(x['pnl'] for x in wins) / len(wins) if wins else 0
        al = abs(sum(x['pnl'] for x in losses) / len(losses)) if losses else 1
        be = al / (al + aw) * 100 if (al + aw) else 0
        total = sum(x['pnl'] for x in t)
        peak, dd = BALANCE_START, 0
        for x in t:
            peak = max(peak, x['balance'])
            dd = max(dd, (peak - x['balance']) / peak * 100)
        print(f"{v:>8} | {len(t):>6} | {wr:>5.1f}% | {be:>5.1f}% | "
              f"{wr - be:>+6.1f} | {total:>+8.2f} | {dd:>5.1f}%")
        rows.append((v, len(t), wr, be, total, dd))
    globals()[param] = orig
    return rows


def atr_profile(symbols=None):
    """Sebaran ATR tiap pair — buat lihat filter mana yang sebenernya menggigit."""
    symbols = symbols or SYMBOLS
    print(f"\n{'pair':10} | {'ATR median':>11} | {'p10':>6} | {'p90':>6} | "
          f"{'% lolos filter':>14}")
    print('-' * 60)
    for sym in symbols:
        d = load(sym)
        if len(d) < 200:
            continue
        vals = []
        for i in range(120, len(d), 5):
            w = d[i - 120:i + 1]
            a = atr(w)
            if a:
                vals.append(a / w[-1][4] * 100)
        if not vals:
            continue
        vals.sort()
        med = vals[len(vals) // 2]
        p10 = vals[int(len(vals) * 0.1)]
        p90 = vals[int(len(vals) * 0.9)]
        lolos = sum(1 for v in vals if MIN_ATR_PCT <= v <= MAX_ATR_PCT) / len(vals) * 100
        print(f"{sym.split('/')[0]:10} | {med:>10.3f}% | {p10:>5.2f}% | "
              f"{p90:>5.2f}% | {lolos:>13.1f}%")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'run'

    if cmd == 'fetch':
        fetch_all()
        return

    if cmd == 'validate':
        param = sys.argv[2] if len(sys.argv) > 2 else 'SL_ATR_MULT'
        grids = {
            'MIN_RR': [1.0, 1.1, 1.2, 1.3, 1.5, 1.7, 2.0],
            'SL_ATR_MULT': [1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 4.0],
            'TP_ATR_MULT': [2.0, 2.4, 3.0, 4.0, 5.0, 6.0],
            'MIN_ATR_PCT': [0.05, 0.10, 0.15, 0.25, 0.40],
            'ENTRY_MODE': ['limit', 'market'],
            'ALLOW_SL_WIDEN': [False, True],
            'MIN_SL_PCT': [0.4, 0.55, 0.7, 0.9, 1.2],
        }
        if param not in grids:
            print(f"pilih: {', '.join(grids)}")
            return
        validate(param, grids[param])
        return

    if cmd == 'atr':
        atr_profile()
        return

    if cmd == 'sweep':
        param = sys.argv[2] if len(sys.argv) > 2 else 'MIN_RR'
        grids = {
            'MIN_RR': [1.0, 1.1, 1.2, 1.3, 1.5, 1.7, 2.0],
            'SL_ATR_MULT': [1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 4.0],
            'TP_ATR_MULT': [2.0, 2.4, 3.0, 4.0, 5.0, 6.0, 8.0],
            'RSI_MAX': [60, 65, 72, 80, 100],
            'MAX_SL_PCT': [2.5, 3.5, 4.5, 6.0, 8.0],
            'ENTRY_TIMEOUT_BARS': [2, 3, 5, 8, 12],
            'TARGET_LOOKBACK': [20, 40, 60, 90],
            'SWING_LOOKBACK': [10, 20, 30, 50],
            'COOLDOWN_BARS': [0, 5, 10, 20],
            'MIN_ATR_PCT': [0.05, 0.10, 0.15, 0.25, 0.40, 0.58],
            'MIN_SL_PCT': [0.4, 0.55, 0.7, 0.9, 1.2],
            'MAX_CONCURRENT': [1, 2, 3, 5],
            'MAKER_FEE_PCT': [0.0, 0.01, 0.02, 0.04, 0.06],
            'ENTRY_MODE': ['limit', 'market'],
            'REVERSE': [False, True],
            'SLIPPAGE_PCT': [0.0, 0.02, 0.03, 0.05, 0.10],
            'ALLOW_SL_WIDEN': [False, True],
        }
        if param not in grids:
            print(f"parameter tak dikenal. pilih: {', '.join(grids)}")
            return
        sweep(param, grids[param])
        print("\nCatatan: nilai terbaik di tabel ini belum tentu terbaik di masa")
        print("depan. Makin banyak parameter yang lo pas-pasin ke data yang sama,")
        print("makin besar kemungkinan lo cuma ngepasin ke kebetulan.")
        return

    res = run_backtest(verbose=True)
    if not res:
        return
    print(f"\nData: {len(res['symbols'])} pair, {res['bars']:,} bar {TIMEFRAME}")
    stats(res, 'HASIL BACKTEST')
    show_rejects(res)

    t = res['trades']
    if t:
        print("\n  per pair:")
        agg = {}
        for x in t:
            a = agg.setdefault(x['symbol'], [0, 0, 0.0])
            a[0] += 1
            a[1] += 1 if x['pnl'] > 0 else 0
            a[2] += x['pnl']
        for sym, (c, w, p) in sorted(agg.items(), key=lambda x: -x[1][2]):
            print(f"    {sym.split('/')[0]:10} {c:>4} trade  "
                  f"menang {w / c * 100:>5.1f}%  PnL {p:>+8.2f}")

        with open('backtest_trades.json', 'w', encoding='utf-8') as f:
            json.dump(t, f, indent=1)
        print("\n  detail tiap trade -> backtest_trades.json")


if __name__ == '__main__':
    main()