"""
Bot Scalping AI - Bitget Futures
================================

Beda mendasar dari bot.py:

1. SCANNER      - nyari pair yang lagi rame & gerak, bukan maksa BTC terus
2. LEVEL ADAPTIF- SL/TP dihitung dari ATR pair itu sendiri, bukan persen tetap
3. LIMIT ENTRY  - nunggu harga datang ke zona, bukan market order asal masuk
4. LEVERAGE ADAPTIF - nyesuain SL biar selalu aman dari likuidasi

Poin nomor 2 dan 3 datang dari cara trader di screenshot nyusun plan:
entry di zona, SL di balik struktur, TP di level berikutnya.

--- REVISI (setelah dry run pertama) ---
a. API resmi Anthropic, bukan router pihak ketiga.
b. Parsing JSON diperketat: ekstraksi kurung berimbang + retry + batas
   panjang di prompt. Sebelumnya 2 dari 3 kandidat hilang cuma gara-gara
   JSON kepotong di max_tokens.
c. Circuit breaker: AI mati nggak lagi keliatan sama kayak AI bilang WAIT.
d. Dry run jujur: fee dihitung, limit order harus kesentuh harga dulu baru
   fill, dan PnL closing dipatok di level SL/TP — bukan di harga saat dicek.
e. ATR scanner disamain ke timeframe entry. Dulu nyaring pakai ATR 15m tapi
   trading pakai ATR 5m, jadi filternya nggak nyambung.
f. SL nggak dilebarin paksa lagi. Struktur nggak cukup ruang -> skip pair.
g. Manage loop di-throttle biar nggak bakar token buat jawaban HOLD berulang.

PENTING: biarkan DRY_RUN = True sampai statistiknya cukup dan masuk akal.
"""

import ccxt
import os
import re
import time
import json
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone
from anthropic import Anthropic
from dotenv import load_dotenv

from core import fees
from core import indicators as ind

load_dotenv()

# ============================================================
# KONFIGURASI
# ============================================================

DRY_RUN = True

# --- AI ---
AI_MODEL = "claude-sonnet-5"

# Model ini punya proses berpikir internal, dan token berpikir itu DIHITUNG
# ke dalam max_tokens yang sama dengan jawaban akhirnya. Kalau jatahnya mepet,
# jatah habis di tahap mikir dan teks jawaban nggak pernah keluar — persis
# yang bikin balasan kosong dan JSON kepotong sebelum ini.
# Jangan turunin di bawah ~2000 buat ngirit; yang kepotong bukan cuma
# panjangnya, tapi ada-nya jawaban sama sekali.
AI_MAX_TOKENS = 6000

# Opsional: kalau model mendukung, ini nekan porsi mikir biar lebih murah &
# cepat. Isi 'low' / 'medium' / 'high', atau None buat nggak dikirim sama
# sekali. Kalau API-nya nolak parameter ini, bot otomatis mundur ke mode
# tanpa parameter — bukan mati.
AI_EFFORT = "low"

AI_MAX_CONSECUTIVE_ERRORS = 5   # circuit breaker: bot berhenti kalau AI mati
MAX_LOOP_ERRORS = 8             # error beruntun sebelum bot nyerah (backoff naik)

# --- SCANNER ---
QUOTE = 'USDT'
SCAN_TOP_VOLUME = 40        # ambil N pair teratas by volume 24h
SCAN_CANDIDATES = 5         # dari N itu, sisakan berapa buat dianalisis AI.
                            # Sempat 10 buat mempercepat pengumpulan data, tapi
                            # dibalikin ke 5 karena kredit API terbatas —
                            # separuh biaya, konsekuensinya trade lebih jarang.
                            # Naikin lagi kalau dana sudah longgar; ini satu-
                            # satunya cara nambah trade tanpa melonggarkan
                            # ambang kualitas apa pun.
MIN_VOLUME_USD = 5_000_000  # volume 24h minimum, buang pair sepi
BLACKLIST = set()           # isi kalau ada pair yang mau dihindari

# --- TIMEFRAME ---
TIMEFRAME = '5m'            # scalping
CANDLE_LIMIT = 120
SCAN_TIMEFRAME = TIMEFRAME  # HARUS sama dengan TIMEFRAME. Kalau beda, filter
                            # ATR di scanner nyaring pakai angka yang bukan
                            # angka yang dipakai validate_plan.
SLEEP_TIME = 300            # satu siklus per candle 5m. Dari 180 detik —
                            # cek lebih rapat dari kecepatan candle itu sendiri
                            # cuma bikin AI ngeliat data yang sama dua kali.
                            # Motong sekitar 40% biaya.
MANAGE_EVERY_CYCLES = 2     # panggil AI buat kelola posisi tiap N siklus
MANAGE_MOVE_PCT = 0.30      # ...atau lebih cepat kalau harga gerak sejauh ini
THESIS_BREAK_MIN_CONF = 55  # keyakinan minimum sebelum posisi ditutup karena
                            # tesis entry dinyatakan patah

# --- POSISI ---
# --- POSISI PARALEL ---
MAX_CONCURRENT_POSITIONS = 3   # berapa pair boleh dipegang barengan.
                               # 3 x 0.5% = 1.5%, pas di plafon MAX_TOTAL_RISK_PCT,
                               # jadi pagar risikonya tetap yang menentukan.
MAX_TOTAL_RISK_PCT = 1.5       # total risiko semua posisi terbuka, % saldo.
                               # Pagar ini yang mencegah "2 posisi" pelan-pelan
                               # jadi 5 posisi searah = satu taruhan besar yang
                               # nyamar jadi diversifikasi.

MARGIN_MODE = 'isolated'
# --- SIZING ---
# Ukuran posisi TIDAK lagi angka tetap. Lo tentuin mau risiko berapa persen
# dari saldo, ukurannya yang ngikutin SL:
#     notional = (RISK_PCT% x saldo) / (SL% )
# Efeknya: tiap trade risikonya sama persis berapapun lebar SL-nya, jadi
# statistik win rate lo akhirnya bisa dibandingin antar trade.
#
# INI SATU-SATUNYA ANGKA YANG PERLU LO PUTUSIN. Konvensi umum 0.5-2%.
# Default sengaja di bawah itu — naikin sendiri kalau lo udah yakin.
RISK_PCT = 0.5              # persen saldo yang direlakan hilang kalau kena SL

# Pagar pengaman, biar salah ketik nol nggak langsung jadi bencana
MAX_NOTIONAL_PCT = 300.0    # notional maksimal, persen dari saldo
MIN_NOTIONAL_USD = 5.0      # di bawah ini nggak usah trading
BALANCE_REFRESH_CYCLES = 10 # saldo di-refresh tiap N siklus

# Dipakai cuma kalau saldo gagal dibaca
NOTIONAL_FALLBACK_USD = 15.0
MAX_LEVERAGE = 20           # batas atas; bot nurunin sendiri kalau SL lebar

# --- LEVEL (ATR-based) ---
ATR_PERIOD = 14
SL_ATR_MULT = 2.0           # SL minimal = 2.0x ATR pair itu.
                            # Backtest 30 hari, 10 pair: win rate naik monoton
                            # seiring nilai ini (1.2 -> 20%, 2.0 -> 22%,
                            # 2.5 -> 26%, 3.0 -> 29%), tapi jumlah trade anjlok
                            # (110 -> 90 -> 61 -> 35). 2.0 dipilih sebagai
                            # kompromi: cukup lebar buat lolos noise, masih
                            # cukup sering trading buat ngumpulin data.
                            # Naikin ke 2.5 kalau lo lebih milih kualitas.
TP_ATR_MULT = 2.4           # TP = 2.4x ATR  -> RR kotor 1:2
MIN_SL_PCT = 0.70           # SL lebih sempit dari ini bikin fee 0.12% makan
                            # RR sampai nggak lolos filter. Plan kayak gitu
                            # DITOLAK, bukan dilebarin — melebarkan SL berarti
                            # balik ke persentase karangan dan ninggalin
                            # struktur yang jadi alasan entry.
MAX_SL_PCT = 3.50           # plafon SL.
                            # Dulu 2.50 waktu notional masih tetap, karena SL
                            # lebar = rugi lebih besar. Sekarang sizing berbasis
                            # risiko: SL lebar otomatis mengecilkan posisi, jadi
                            # kerugian tetap RISK_PCT% berapapun SL-nya. Plafon
                            # ketat malah bikin pair ber-ATR tinggi mustahil
                            # dapat SL yang valid (jendelanya nol).

# --- ENTRY ---
ENTRY_MODE = 'market'       # 'limit' (nunggu zona) atau 'market' (langsung)
                            #
                            # Backtest 110 vs 149 trade: market unggul 7.8 poin
                            # margin walau bayar taker fee + slippage. Sebabnya
                            # adverse selection — limit di zona pullback cuma
                            # kefill pas pullback-nya jadi pembalikan, sementara
                            # tren yang lanjut mulus nggak pernah kepegang.
MAX_ENTRY_DRIFT_PCT = 0.35  # market entry dibatalkan kalau harga udah lari
                            # sejauh ini dari zona rencana AI
ENTRY_OFFSET_ATR = 0.3      # limit ditaruh 0.3x ATR lebih baik dari harga now
ENTRY_TIMEOUT_CYCLES = 5    # batal kalau nggak kefill dalam N siklus

# --- FILTER ---
MIN_CONFIDENCE = 55

# --- FILTER TAMBAHAN ---
RSI_MIN = 25                # di luar rentang ini, pair udah kejauhan larinya
RSI_MAX = 72                # dan AI hampir pasti jawab WAIT
COOLDOWN_CYCLES = 10        # istirahatin pair sekian siklus setelah kalah
                            # (10 x 180s = 30 menit; yang 5 kependekan, flip
                            #  BICO kemarin jaraknya 17 menit)
MIN_SL_WINDOW_PP = 0.40     # jendela SL minimal yang harus tersedia di suatu
                            # pair. Kalau lebih sempit dari ini, AI hampir pasti
                            # meleset — buang pair-nya sebelum bayar call.
JOURNAL_LOOKBACK = 3        # berapa trade terakhir yang diceritain ke AI
MIN_RR = 1.3                # RR bersih minimum setelah fee.
                            # Ini bukan angka keramat — dia cuma sisi lain dari
                            # win rate. Breakeven = 1 / (1 + MIN_RR):
                            #   1.5 -> butuh menang 40% dari semua trade
                            #   1.3 -> butuh menang 43.5%
                            #   1.0 -> butuh menang 50%
                            # Turunin = lebih banyak trade, tapi standar menang
                            # naik. Jangan turun di bawah 1.0: itu artinya TP
                            # lebih deket dari SL, dan lo harus lebih sering
                            # bener daripada salah cuma buat impas.
# Bitget USDT-M Futures VIP0: maker 0.02% / taker 0.06% (dikonfirmasi dari
# halaman fee akun). Entry & TP pakai limit order -> MAKER. SL -> TAKER.
# Asumsi lama "semua taker 0.12%" bikin bot nolak setup yang sebenarnya layak.
TAKER_FEE_PCT = 0.06
MAKER_FEE_PCT = 0.02
MAINTENANCE_MARGIN_PCT = 0.5
MAX_SL_VS_LIQ_RATIO = 0.5   # SL maksimal 50% dari jarak likuidasi

# --- FILE ---
LOG_FILE = 'scalp.log'
JOURNAL_FILE = 'journal_scalp.jsonl'

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-7s | %(message)s',
    handlers=[
        # Rotasi: aman ditinggal berjam-jam tanpa log-nya membengkak.
        RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3,
                            encoding='utf-8'),
        logging.StreamHandler(),
    ],
)

for _noisy in ('httpx', 'httpcore', 'anthropic', 'urllib3'):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

log = logging.getLogger('scalp')


def journal(event, **data):
    rec = {'ts': datetime.now(timezone.utc).isoformat(), 'event': event, **data}
    try:
        with open(JOURNAL_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec) + '\n')
    except Exception as e:
        log.warning(f"Gagal nulis journal: {e}")


# ============================================================
# EXCHANGE
# ============================================================

exchange = ccxt.bitget({
    'timeout': 20000,          # 20 detik; jangan biarkan panggilan menggantung
    'apiKey': os.getenv('BITGET_API_KEY'),
    'secret': os.getenv('BITGET_API_SECRET'),
    'password': os.getenv('BITGET_PASSPHRASE'),
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
})

# Timeout eksplisit. Tanpa ini, satu request yang menggantung bisa membekukan
# seluruh siklus tanpa batas — di log pernah kejadian jeda 17 menit di tengah
# siklus 180 detik, dan bot cuma diam tanpa pesan apa pun.
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"),
                   timeout=90.0, max_retries=2)

SIM_POSITIONS = {}      # symbol -> posisi simulasi yang lagi terbuka
PENDING_ORDERS = {}     # symbol -> limit order live yang belum kefill
SIM_PENDINGS = {}       # symbol -> limit order simulasi yang belum kefill
AI_ERRORS = 0           # error beruntun; direset tiap call yang sukses
USAGE = {'calls': 0, 'in': 0, 'out': 0, 'started': None}
CYCLE = 0
LAST_MANAGE = {}        # symbol -> {'cycle': n, 'price': harga}
BALANCE = {'usdt': 0.0, 'cycle': -99}
COOLDOWN = {}           # symbol -> cycle sampai kapan pair itu diistirahatin
PLANNED_RISK = {}       # symbol -> USDT yang dipertaruhkan. Dicatat sendiri
                        # karena fetch_positions() nggak ngasih tau SL-nya di
                        # mana, jadi plafon risiko total nggak bisa dihitung
                        # dari data exchange.


# ============================================================
# INDIKATOR
# ============================================================

def ema(values, period):
    return ind.ema_series(values, period)


def rsi(closes, period=14):
    return ind.rsi(closes, period)


def atr(ohlcv, period=ATR_PERIOD):
    """Average True Range (Wilder). Ini yang bikin SL/TP nyesuain volatilitas tiap pair."""
    return ind.atr(ohlcv, period)


# ============================================================
# SCANNER
# ============================================================

def scan_markets():
    """Cari pair yang lagi rame DAN lagi gerak.

    Dua tahap biar hemat API call:
      1. fetch_tickers() sekali -> ranking volume 24h
      2. dari top volume, ambil OHLCV -> ranking volatilitas (ATR%)

    Volume doang nggak cukup: BTC selalu volume tertinggi tapi bisa diam
    seharian. Yang dicari adalah irisan rame + bergerak.
    """
    log.info("Scanning market...")
    try:
        tickers = exchange.fetch_tickers()
    except Exception as e:
        log.error(f"Gagal fetch tickers: {e}")
        return []

    rows = []
    for sym, t in tickers.items():
        if not sym.endswith(f':{QUOTE}') or sym in BLACKLIST:
            continue
        vol = t.get('quoteVolume') or 0
        last = t.get('last') or 0
        if vol < MIN_VOLUME_USD or last <= 0:
            continue
        rows.append({'symbol': sym, 'volume': float(vol), 'last': float(last),
                     'change': float(t.get('percentage') or 0)})

    rows = [r for r in rows if COOLDOWN.get(r['symbol'], -1) < CYCLE]
    rows.sort(key=lambda r: r['volume'], reverse=True)
    rows = rows[:SCAN_TOP_VOLUME]
    log.info(f"  {len(rows)} pair lolos filter volume (>{MIN_VOLUME_USD/1e6:.0f}M)")

    scored = []
    for r in rows:
        try:
            # SCAN_TIMEFRAME sengaja disamain dengan TIMEFRAME. ATR yang
            # dipakai nyaring di sini harus ATR yang sama dengan yang dipakai
            # validate_plan nanti, kalau nggak pair lolos scanner terus
            # ketolak di eksekusi.
            oh = exchange.fetch_ohlcv(r['symbol'], SCAN_TIMEFRAME, limit=ATR_PERIOD + 5)
            if len(oh) < ATR_PERIOD + 1:
                continue
            a = atr(oh)
            if not a:
                continue
            atr_pct = a / r['last'] * 100

            # Buang yang terlalu diam (nggak bisa ditradingin) dan terlalu
            # liar (SL-nya bakal kelewat lebar buat leverage berapapun).
            if atr_pct < MIN_SL_PCT / SL_ATR_MULT:
                continue
            # Jendela SL yang tersedia di pair ini: antara 1.2x ATR (biar
            # nggak kesenggol noise) dan MAX_SL_PCT (biar leverage nggak anjlok).
            # Kalau jendelanya terlalu sempit, AI nggak punya ruang buat naruh
            # SL di struktur — plan-nya bakal ditolak terus. Buang di sini.
            sl_floor = max(MIN_SL_PCT, atr_pct * SL_ATR_MULT)
            if MAX_SL_PCT - sl_floor < MIN_SL_WINDOW_PP:
                log.debug(f"  skip {r['symbol']}: jendela SL cuma "
                          f"{MAX_SL_PCT - sl_floor:.2f}pp")
                continue

            closes = [c[4] for c in oh]
            rs = rsi(closes, 14)

            # Buang yang udah kejauhan larinya. Skor scanner = volatilitas x
            # volume, dan volatilitas paling tinggi justru pas harga lagi
            # parabolic — kondisi yang persis bikin AI jawab WAIT. Nyaring di
            # sini artinya nggak perlu bayar call AI buat jawaban yang udah
            # ketebak.
            if rs is not None and not (RSI_MIN <= rs <= RSI_MAX):
                log.debug(f"  skip {r['symbol']}: RSI {rs:.1f} di luar "
                          f"{RSI_MIN}-{RSI_MAX}")
                continue

            r['atr'] = a
            r['atr_pct'] = atr_pct
            r['rsi'] = rs
            # Skor: volatilitas dibobot log volume. Rame saja tidak cukup.
            import math
            r['score'] = atr_pct * math.log10(r['volume'])
            scored.append(r)
        except Exception as e:
            log.debug(f"  skip {r['symbol']}: {e}")
            continue

    scored.sort(key=lambda r: r['score'], reverse=True)
    top = scored[:SCAN_CANDIDATES]

    log.info(f"  Top {len(top)} kandidat:")
    for r in top:
        log.info(f"    {r['symbol']:22} vol {r['volume']/1e6:7.1f}M  "
                 f"ATR {r['atr_pct']:5.2f}%  24h {r['change']:+6.2f}%")
    return top


# ============================================================
# LEVERAGE ADAPTIF
# ============================================================

def safe_leverage(sl_pct):
    """Leverage tertinggi yang masih bikin SL aman dari likuidasi.
    Pair yang volatil otomatis dapat leverage lebih rendah."""
    return fees.safe_leverage(sl_pct, MAX_LEVERAGE, MAX_SL_VS_LIQ_RATIO,
                              MAINTENANCE_MARGIN_PCT)


def net_rr(sl_pct, tp_pct):
    return fees.net_rr(sl_pct, tp_pct, ENTRY_MODE, MAKER_FEE_PCT, TAKER_FEE_PCT)


def entry_fee_pct():
    """Limit order = maker. Market order = taker."""
    return fees.entry_fee_pct(ENTRY_MODE, MAKER_FEE_PCT, TAKER_FEE_PCT)


def worst_fee_pct():
    """Fee bolak-balik kasus terburuk: keluar via SL (taker).
    Dipakai buat sizing dan filter RR — perencanaan selalu pakai asumsi pahit."""
    return fees.worst_fee_pct(ENTRY_MODE, MAKER_FEE_PCT, TAKER_FEE_PCT)


def round_trip_fee(notional, exit_kind='stop_loss'):
    """Fee masuk + keluar dalam USDT. Entry limit/TP -> maker, SL -> taker."""
    return fees.round_trip_fee(notional, ENTRY_MODE, exit_kind,
                               MAKER_FEE_PCT, TAKER_FEE_PCT)


# ============================================================
# DATA & AI
# ============================================================

def candles_after(symbol, since_ms, limit=6):
    """High/low HANYA dari candle yang dibuka setelah `since_ms`.

    Tanpa filter ini, simulasi ngeliat pergerakan harga yang terjadi SEBELUM
    order dipasang, terus nyimpulin SL/TP udah kesentuh. Efeknya posisi bisa
    kefill dan langsung ditutup di siklus yang sama — hasil yang nggak mungkin
    kejadian di exchange beneran.

    Balikin (high, low, jumlah_candle). jumlah_candle 0 artinya belum ada
    candle baru sejak order dipasang, jadi belum ada yang bisa dinilai.
    """
    try:
        oh = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=limit)
    except Exception as e:
        log.warning(f"Gagal ambil candle {symbol}: {e}")
        return None, None, 0
    fresh = [c for c in oh if c[0] >= since_ms]
    if not fresh:
        return None, None, 0
    return max(c[2] for c in fresh), min(c[3] for c in fresh), len(fresh)


def market_snapshot(symbol):
    """Ringkasan pair buat dikirim ke AI: candle + indikator."""
    oh = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=CANDLE_LIMIT)
    closes = [c[4] for c in oh]
    a = atr(oh)
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    r = rsi(closes, 14)

    recent = oh[-40:]
    lines = ["Time(UTC) | Open | High | Low | Close | Volume"]
    for c in recent:
        ts = datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc).strftime('%m-%d %H:%M')
        lines.append(f"{ts} | {c[1]} | {c[2]} | {c[3]} | {c[4]} | {c[5]}")

    highs = [c[2] for c in oh[-60:]]
    lows = [c[3] for c in oh[-60:]]

    info = {
        'symbol': symbol,
        'last': closes[-1],
        'atr': a,
        'atr_pct': a / closes[-1] * 100 if a else 0,
        'ema20': e20[-1] if e20 else None,
        'ema50': e50[-1] if e50 else None,
        'rsi': r,
        'high60': max(highs),
        'low60': min(lows),
        'candles': "\n".join(lines),
    }
    return info


class AIDown(Exception):
    """AI nggak bisa dihubungi berkali-kali berturut-turut."""


def call_ai(prompt, system=None):
    """Balikin (teks, ok). ok=False artinya API-nya yang bermasalah — beda
    dengan AI yang jawab tapi isinya nggak kepakai.

    Perbedaan itu penting: dulu dua-duanya balik string kosong, jadi bot
    nggak bisa bedain 'AI bilang WAIT' sama 'AI mati'.
    """
    global AI_ERRORS, AI_EFFORT
    try:
        kwargs = {
            'model': AI_MODEL,
            'max_tokens': AI_MAX_TOKENS,
            'messages': [{"role": "user", "content": prompt}],
        }
        if system:
            # Prompt caching: bagian statis (aturan, format, definisi) identik
            # di setiap call, jadi cukup dibayar penuh sekali lalu dipakai ulang
            # selama cache masih hidup (~5 menit; siklus bot 180 detik, muat).
            kwargs['system'] = [{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }]
        if AI_EFFORT:
            kwargs['output_config'] = {"effort": AI_EFFORT}

        try:
            msg = client.messages.create(**kwargs)
        except Exception as e:
            # Parameter effort nggak didukung model ini -> matikan, jangan
            # bikin bot berhenti gara-gara opsi yang cuma buat ngirit.
            if AI_EFFORT and ('output_config' in str(e) or 'effort' in str(e)):
                log.warning("Parameter effort ditolak API — dimatikan, lanjut tanpa itu.")
                AI_EFFORT = "low"
                kwargs.pop('output_config', None)
                msg = client.messages.create(**kwargs)
            else:
                raise

        parts, n_think = [], 0
        for b in getattr(msg, 'content', []) or []:
            btype = b.get('type') if isinstance(b, dict) else getattr(b, 'type', None)
            if btype == 'text':
                parts.append(b.get('text', '') if isinstance(b, dict)
                             else getattr(b, 'text', ''))
            elif btype in ('thinking', 'redacted_thinking'):
                n_think += 1
        text = "\n".join(parts).strip()

        if getattr(msg, 'stop_reason', None) == 'max_tokens':
            u = getattr(msg, 'usage', None)
            out = getattr(u, 'output_tokens', '?') if u else '?'
            log.warning(f"  Kepotong di max_tokens (output {out}, "
                        f"{n_think} blok mikir). Naikin AI_MAX_TOKENS.")

        u = getattr(msg, 'usage', None)
        if u:
            USAGE['calls'] += 1
            USAGE['in'] += getattr(u, 'input_tokens', 0) or 0
            USAGE['out'] += getattr(u, 'output_tokens', 0) or 0
            USAGE['cache_w'] = USAGE.get('cache_w', 0) + (getattr(u, 'cache_creation_input_tokens', 0) or 0)
            USAGE['cache_r'] = USAGE.get('cache_r', 0) + (getattr(u, 'cache_read_input_tokens', 0) or 0)

        AI_ERRORS = 0
        return text, True
    except Exception as e:
        AI_ERRORS += 1
        log.error(f"AI error ({AI_ERRORS}/{AI_MAX_CONSECUTIVE_ERRORS}): {e}")
        journal('ai_error', error=str(e)[:300], streak=AI_ERRORS)
        if AI_ERRORS >= AI_MAX_CONSECUTIVE_ERRORS:
            raise AIDown(str(e))
        return "", False


def extract_json(text):
    """Ambil objek JSON pertama yang kurungnya berimbang.

    Regex greedy `\\{.*\\}` yang lama gagal total kalau jawabannya kepotong,
    dan bisa nyaplok kelebihan kalau ada teks setelahnya.
    """
    if not text:
        return None
    cleaned = re.sub(r'```(?:json)?', '', text)
    start = cleaned.find('{')
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(cleaned)):
        c = cleaned[i]
        if in_str:
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return cleaned[start:i + 1]
    return None


def parse_json(text):
    raw = extract_json(text)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def ask_ai(prompt, label, retries=1, system=None):
    """Panggil AI + parse. Retry sekali kalau JSON-nya rusak.

    Balikin (obj, raw, api_ok).
    """
    raw = ""
    for attempt in range(retries + 1):
        raw, ok = call_ai(prompt, system=system)
        if not ok:
            return None, raw, False
        obj = parse_json(raw)
        if obj:
            return obj, raw, True
        log.warning(f"  {label}: JSON rusak (percobaan {attempt + 1}) — "
                    f"potongan: {raw[:160]!r}")
    journal('parse_failed', label=label, raw=raw[:500])
    return None, raw, True


SYSTEM_ENTRY = """Kamu Quant Trading Analyst yang menilai setup trading jangka pendek.

CARA MENYUSUN PLAN:
- Stop loss ditaruh di BALIK STRUKTUR terdekat — level yang kalau tembus
  berarti tesismu salah. Bukan angka persentase asal.
- Take profit di level teknikal berikutnya (swing high/low, resistance).
- Kalau tidak ada struktur yang jelas, jawab WAIT. Jangan dipaksakan.
- Jangan menggeser stop loss menjauh dari struktur cuma supaya lolos syarat
  jarak minimum; lebih baik lewati pair-nya.

FORMAT JAWABAN — balas HANYA JSON, tanpa teks lain:
{"decision":"BUY|SELL|WAIT",
 "confidence":0-100,
 "entry":harga_entry_angka,
 "stop_loss":harga_sl_angka,
 "take_profit":harga_tp_angka,
 "reason":"maksimal 120 karakter",
 "structure":"maksimal 150 karakter"}

Patuhi batas karakter. Untuk WAIT, isi entry/stop_loss/take_profit dengan 0.
Karakter pertama jawabanmu harus { dan karakter terakhir harus }."""


SYSTEM_MANAGE = """Kamu Quant Trading Analyst yang mengelola posisi terbuka.

TUGASMU: nilai apakah tesis entry yang diberikan MASIH BERLAKU sekarang.

Tesis dianggap PATAH kalau kondisi spesifik yang jadi alasan masuk sudah
tidak ada lagi. Contoh konkret:
- Masuk SHORT karena "harga di bawah EMA20/50" -> sekarang EMA20 > EMA50
  dan harga di atasnya = PATAH.
- Masuk LONG karena "higher-low intact" -> sekarang sudah bikin lower-low
  = PATAH.
- Masuk karena "breakout" -> harga balik ke dalam range = PATAH.

Yang BUKAN tesis patah:
- Harga bergerak melawan tapi struktur yang jadi alasan masuk masih utuh.
- PnL sedang negatif. Itu normal, bukan bukti apa pun.

PENTING: "SL belum tersentuh" BUKAN alasan untuk menahan posisi. SL adalah
batas kerugian, bukan bukti bahwa tesismu masih benar. Kalau alasan kamu
masuk sudah tidak berlaku, katakan begitu — walaupun harga belum menyentuh
SL, dan walaupun posisinya sedang untung.

FORMAT JAWABAN — balas HANYA JSON, tanpa teks lain:
{"thesis_valid": true atau false,
 "decision":"HOLD|CLOSE",
 "confidence":0-100,
 "reason":"maksimal 120 karakter — sebutkan kondisi spesifiknya",
 "structure":"maksimal 150 karakter"}
Karakter pertama jawabanmu harus { dan karakter terakhir harus }."""


def recent_trades(symbol, n=None):
    """Baca balik journal buat ambil N trade terakhir di pair ini.

    Ini "learning system" yang selama ini kosong. Datanya udah dicatat sejak
    awal, cuma nggak pernah dibaca lagi. Tanpa ini, AI bisa short BICO, kena
    SL, terus 16 menit kemudian long di pair yang sama — bukan karena dia
    salah baca chart, tapi karena dia nggak tau itu pernah kejadian.
    """
    n = n or JOURNAL_LOOKBACK
    out = []
    try:
        with open(JOURNAL_FILE, encoding='utf-8') as f:
            lines = f.readlines()
    except FileNotFoundError:
        return out

    opens = {}
    for ln in lines:
        try:
            r = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if r.get('symbol') != symbol:
            continue
        if r.get('event') == 'dry_run_open':
            opens = r
        elif r.get('event') in ('dry_run_close', 'position_closed'):
            out.append({
                'side': opens.get('side') or r.get('side'),
                'pnl': r.get('pnl'),
                'reason': r.get('reason'),
                'ts': r.get('ts', ''),
            })
    return out[-n:]


def history_block(symbol):
    """Ringkasan riwayat pair, siap ditempel ke prompt."""
    hist = recent_trades(symbol)
    if not hist:
        return "Belum ada riwayat trade di pair ini."
    baris = []
    for h in hist:
        arah = str(h.get('side', '?')).upper()
        pnl = h.get('pnl')
        hasil = 'MENANG' if (pnl or 0) > 0 else 'KALAH'
        baris.append(f"- {arah} -> {hasil} ({h.get('reason', '?')}, "
                     f"pnl {pnl:+.4f} USDT)")
    return ("Riwayat trade bot ini di pair yang sama (terbaru di bawah):\n"
            + "\n".join(baris)
            + "\nPertimbangkan ini. Kalau arah yang sama baru saja gagal dan "
              "strukturnya belum berubah, jangan ulangi setup yang sama.")


def ask_ai_entry(info):
    """AI mengusulkan arah + level. Kode yang memvalidasi, bukan AI."""
    # Aturan entry HARUS cocok dengan ENTRY_MODE. Kalau prompt minta rencana
    # pullback sementara eksekusinya market, AI ngasih entry jauh dari harga
    # sekarang dan kode langsung nolak karena jauh dari harga sekarang —
    # dua bagian sistem yang saling bertentangan, dan nol trade hasilnya.
    if ENTRY_MODE == 'market':
        entry_rule = (
            "Entry HARUS di harga sekarang ({last}), bukan di zona pullback. "
            "Order dieksekusi seketika. Kalau harga sekarang bukan tempat yang "
            "layak untuk masuk, jawab WAIT — jangan mengusulkan level yang "
            "lebih baik dan berharap harga datang ke situ."
        ).format(last=info['last'])
    else:
        entry_rule = ("Entry di ZONA, bukan harga sekarang. "
                      "Tunggu harga datang ke level.")

    prompt = f"""Kamu Quant Trading Analyst yang fokus scalping.

PAIR: {info['symbol']}  timeframe {TIMEFRAME}

Kondisi sekarang:
- Harga    : {info['last']}
- ATR({ATR_PERIOD}) : {info['atr']:.8f}  ({info['atr_pct']:.2f}% dari harga)
- EMA20    : {info['ema20']}
- EMA50    : {info['ema50']}
- RSI(14)  : {info['rsi']:.1f}
- High 60 candle : {info['high60']}
- Low 60 candle  : {info['low60']}

40 candle terakhir:
{info['candles']}

{history_block(info['symbol'])}

ATURAN PENYUSUNAN PLAN:
1. {entry_rule}
2. Stop loss di BALIK STRUKTUR terdekat — level yang kalau tembus berarti
   tesis kamu salah. Bukan angka persentase asal.
3. Take profit di level teknikal berikutnya (swing high/low, resistance).
4. Kalau tidak ada struktur yang jelas, jawab WAIT. Jangan dipaksakan.
5. Jarak entry ke stop loss minimal {max(MIN_SL_PCT, info['atr_pct'] * SL_ATR_MULT):.2f}%
   dari harga entry (= {SL_ATR_MULT}x ATR pair ini). Stop yang lebih rapat dari
   satu candle normal akan kena noise, bukan kena tesis yang salah.
   Kalau struktur terdekat lebih rapat dari itu, setup ini tidak layak —
   jawab WAIT. Jangan menggeser stop loss menjauh dari struktur cuma supaya
   lolos syarat ini; lebih baik lewati pair-nya.

Balas HANYA JSON, tanpa teks lain:
{{"decision":"BUY|SELL|WAIT",
  "confidence":0-100,
  "entry":harga_entry_angka,
  "stop_loss":harga_sl_angka,
  "take_profit":harga_tp_angka,
  "reason":"maksimal 120 karakter",
  "structure":"maksimal 150 karakter"}}

Patuhi batas karakter itu. Untuk WAIT, isi entry/stop_loss/take_profit dengan 0.
Karakter pertama jawabanmu harus {{ dan karakter terakhir harus }}. Jangan menulis apa pun di luar JSON."""

    return ask_ai(prompt, info['symbol'], system=SYSTEM_ENTRY)


def ask_ai_manage(info, pos):
    """Tanya AI soal posisi yang lagi jalan.

    Perubahan penting dari versi lama: AI diingetin APA tesis entry-nya, dan
    diminta menilai secara eksplisit apakah tesis itu masih berlaku.

    Versi lama cuma nanya HOLD/CLOSE sambil bilang "biarkan SL/TP bekerja" —
    tiga kalimat yang semuanya condong ke HOLD, tanpa pernah mendefinisikan
    apa itu "invalid". Hasilnya: bot masuk short CAP karena "downtrend break
    EMA20/50", lalu sebelas kali berturut-turut melaporkan "EMA20>EMA50
    uptrend" dan tetap HOLD sampai SL kena. AI-nya sadar tesisnya patah;
    dia cuma nggak punya cara menyatakannya.
    """
    thesis = pos.get('thesis') or '(tidak tercatat)'
    struktur = pos.get('thesis_structure') or '(tidak tercatat)'
    arah = pos['side'].upper()

    prompt = f"""Kamu Quant Trading Analyst yang mengelola posisi terbuka.

PAIR: {info['symbol']}  timeframe {TIMEFRAME}
Harga sekarang : {info['last']}
ATR            : {info['atr_pct']:.2f}%
RSI            : {info['rsi']:.1f}
EMA20/EMA50    : {info['ema20']} / {info['ema50']}

POSISI TERBUKA:
- Arah  : {arah}
- Entry : {pos['entry']}
- SL    : {pos.get('sl')}
- TP    : {pos.get('tp')}
- PnL   : {pos['pnl']:.4f} USDT ({pos['pnl_pct']:.2f}%)

ALASAN POSISI INI DIBUKA (tesis entry):
"{thesis}"
Struktur yang dibaca saat itu: "{struktur}"

40 candle terakhir:
{info['candles']}

TUGASMU: nilai apakah tesis entry di atas MASIH BERLAKU sekarang.

Tesis dianggap PATAH kalau kondisi spesifik yang jadi alasan masuk sudah
tidak ada lagi. Contoh konkret:
- Masuk SHORT karena "harga di bawah EMA20/50" -> sekarang EMA20 > EMA50
  dan harga di atasnya = PATAH.
- Masuk LONG karena "higher-low intact" -> sekarang sudah bikin lower-low
  = PATAH.
- Masuk karena "breakout" -> harga balik ke dalam range = PATAH.

Yang BUKAN tesis patah:
- Harga bergerak melawan tapi struktur yang jadi alasan masuk masih utuh.
- PnL sedang negatif. Itu normal, bukan bukti apa pun.

PENTING: "SL belum tersentuh" BUKAN alasan untuk menahan posisi. SL adalah
batas kerugian, bukan bukti bahwa tesismu masih benar. Kalau alasan kamu
masuk sudah tidak berlaku, katakan begitu — walaupun harga belum menyentuh
SL, dan walaupun posisinya sedang untung.

Jawab jujur. Kalau tesisnya patah, bilang patah.

Balas HANYA JSON, tanpa teks lain:
{{"thesis_valid": true atau false,
  "decision":"HOLD|CLOSE",
  "confidence":0-100,
  "reason":"maksimal 120 karakter — sebutkan kondisi spesifiknya",
  "structure":"maksimal 150 karakter"}}
Karakter pertama jawabanmu harus {{ dan karakter terakhir harus }}. Jangan menulis apa pun di luar JSON."""

    return ask_ai(prompt, f"{info['symbol']} manage", system=SYSTEM_MANAGE)


# ============================================================
# VALIDASI PLAN
# ============================================================

def validate_plan(plan, info):
    """AI mengusulkan, kode memutuskan.

    Level dari AI tidak pernah dipercaya mentah — dicek arah, jarak, dan RR-nya.
    Kalau AI ngasih SL di sisi yang salah atau TP kekecilan, plan ditolak.
    """
    side = plan.get('decision')
    if side not in ('BUY', 'SELL'):
        return None, 'bukan sinyal entry'

    conf = int(plan.get('confidence') or 0)
    if conf < MIN_CONFIDENCE:
        return None, f'confidence {conf} < {MIN_CONFIDENCE}'

    try:
        entry = float(plan.get('entry') or 0)
        sl = float(plan.get('stop_loss') or 0)
        tp = float(plan.get('take_profit') or 0)
    except (ValueError, TypeError):
        return None, 'level bukan angka'

    if entry <= 0 or sl <= 0 or tp <= 0:
        return None, 'level kosong'

    last = info['last']
    a = info['atr']

    # Di mode market, entry digeser ke harga sekarang di place_entry, jadi
    # pemeriksaan jarak ini nggak relevan — MAX_ENTRY_DRIFT_PCT yang jaga.
    # Entry nggak boleh kejauhan dari harga sekarang
    if abs(entry - last) / last * 100 > info['atr_pct'] * 2:
        if ENTRY_MODE != 'market':
            return None, f'entry {entry} kejauhan dari harga {last}'

    # Arah SL/TP harus benar
    if side == 'BUY' and not (sl < entry < tp):
        return None, f'BUY tapi urutan level salah (sl {sl}, entry {entry}, tp {tp})'
    if side == 'SELL' and not (tp < entry < sl):
        return None, f'SELL tapi urutan level salah (sl {sl}, entry {entry}, tp {tp})'

    sl_pct = abs(entry - sl) / entry * 100
    tp_pct = abs(tp - entry) / entry * 100

    # SL yang terlalu sempit DITOLAK, bukan dilebarkan.
    #
    # Versi lama melebarkan SL ke 0.70% biar RR lolos. Efeknya: SL yang tadinya
    # ditaruh persis di balik struktur berubah jadi persentase karangan — persis
    # penyakit yang bikin bot lama nggak pernah kena level. Kalau struktur pair
    # ini terlalu rapat buat nutup fee, jawabannya bukan geser SL, tapi cari
    # pair lain.
    if sl_pct < MIN_SL_PCT:
        return None, (f'SL {sl_pct:.2f}% terlalu rapat (min {MIN_SL_PCT}%) — '
                      f'struktur nggak cukup ruang buat nutup fee')

    # SL harus lebih lebar dari noise normal pair ini.
    #
    # Ini yang bikin 4 trade pertama kalah semua: SL-nya 0.44-0.76x ATR,
    # padahal SL_ATR_MULT di config bilang 1.2x. Angka itu ternyata cuma
    # dipakai buat ngitung leverage, nggak pernah buat validasi level dari AI.
    # SL yang lebih sempit dari satu candle normal itu bukan stop loss —
    # itu cuma nunggu digampar noise.
    min_sl_atr = info['atr_pct'] * SL_ATR_MULT
    if sl_pct < min_sl_atr:
        return None, (f'SL {sl_pct:.2f}% cuma {sl_pct / info["atr_pct"]:.2f}x ATR '
                      f'(minimal {SL_ATR_MULT}x = {min_sl_atr:.2f}%) — kesenggol noise')

    if sl_pct > MAX_SL_PCT:
        return None, f'SL {sl_pct:.2f}% melebihi plafon {MAX_SL_PCT}%'

    rr = net_rr(sl_pct, tp_pct)
    if rr < MIN_RR:
        return None, f'RR bersih 1:{rr:.2f} < minimum 1:{MIN_RR}'

    lev = safe_leverage(sl_pct)

    return {
        'side': 'buy' if side == 'BUY' else 'sell',
        'entry': entry, 'sl': sl, 'tp': tp,
        'sl_pct': sl_pct, 'tp_pct': tp_pct,
        'rr': rr, 'leverage': lev, 'confidence': conf,
        'reason': str(plan.get('reason', ''))[:300],
        'structure': str(plan.get('structure', ''))[:300],
    }, None


# ============================================================
# EKSEKUSI
# ============================================================

def fetch_usdt_balance(force=False):
    """Saldo USDT di dompet futures. Di-cache biar nggak nembak API tiap siklus."""
    if not force and (CYCLE - BALANCE['cycle']) < BALANCE_REFRESH_CYCLES and BALANCE['usdt']:
        return BALANCE['usdt']
    try:
        bal = exchange.fetch_balance()
        usdt = float(bal.get('USDT', {}).get('free') or 0)
        if usdt > 0:
            BALANCE['usdt'] = usdt
            BALANCE['cycle'] = CYCLE
    except Exception as e:
        log.warning(f"Gagal baca saldo: {e}")
    return BALANCE['usdt']


def target_notional(sl_pct):
    """Notional supaya kerugian saat SL kena = RISK_PCT% dari saldo.

    risk_usd = RISK_PCT% x saldo
    notional = risk_usd / (SL% /100)

    Fee ikut diperhitungkan supaya kerugian TOTAL (termasuk fee masuk-keluar)
    yang pas di angka risiko, bukan cuma selisih harganya.
    """
    bal = fetch_usdt_balance()
    if bal <= 0:
        log.warning(f"Saldo nggak kebaca — pakai fallback {NOTIONAL_FALLBACK_USD} USDT")
        return NOTIONAL_FALLBACK_USD, 0.0

    risk_usd = bal * RISK_PCT / 100
    notional = fees.risk_notional(bal, RISK_PCT, sl_pct, worst_fee_pct())

    cap = bal * MAX_NOTIONAL_PCT / 100
    if notional > cap:
        log.info(f"  Notional dibatasi pagar: {notional:.0f} -> {cap:.0f} USDT")
        notional = cap
    return notional, risk_usd


def calc_amount(symbol, price, notional_usd):
    m = exchange.market(symbol)
    step = m['precision']['amount']
    raw = notional_usd / price
    try:
        if step:
            amt = round(raw / float(step)) * float(step)
            amt = float(exchange.amount_to_precision(symbol, amt))
        else:
            amt = float(exchange.amount_to_precision(symbol, raw))
    except Exception as e:
        return None, 0, f'precision error: {e}'
    if amt <= 0:
        return None, 0, 'amount jadi 0'
    min_a = m['limits']['amount']['min']
    if min_a and amt < float(min_a):
        return None, 0, f'amount {amt} < min {min_a}'
    notional = amt * price
    min_c = m['limits']['cost']['min']
    if min_c and notional < float(min_c):
        return None, 0, f'notional {notional:.2f} < min cost {min_c}'
    return amt, notional, None


def get_position(symbol):
    if DRY_RUN:
        return SIM_POSITIONS.get(symbol)
    try:
        for p in exchange.fetch_positions([symbol]):
            c = float(p.get('contracts') or 0)
            if c > 0:
                return {'symbol': symbol, 'side': p.get('side'), 'contracts': c,
                        'entry': float(p.get('entryPrice') or 0),
                        'pnl': float(p.get('unrealizedPnl') or 0),
                        'pnl_pct': float(p.get('percentage') or 0)}
        return None
    except Exception as e:
        log.error(f"Gagal fetch position {symbol}: {e}")
        return {'symbol': symbol, 'side': 'unknown', 'contracts': 0,
                'entry': 0, 'pnl': 0, 'pnl_pct': 0}


def all_positions():
    """Semua posisi terbuka, sebagai list.

    Dulu bot cuma boleh pegang satu posisi, jadi cukup "ada apa nggak".
    Sekarang bisa beberapa sekaligus, jadi yang dipakai jumlah dan isinya.
    """
    if DRY_RUN:
        return list(SIM_POSITIONS.values())
    out = []
    try:
        for p in exchange.fetch_positions():
            c = float(p.get('contracts') or 0)
            if c <= 0:
                continue
            entry = float(p.get('entryPrice') or 0)
            lev = float(p.get('leverage') or 1) or 1
            notional = c * entry
            out.append({'symbol': p.get('symbol'), 'side': p.get('side'),
                        'contracts': c, 'entry': entry, 'notional': notional,
                        'leverage': lev, 'margin': notional / lev,
                        'pnl': float(p.get('unrealizedPnl') or 0),
                        'pnl_pct': float(p.get('percentage') or 0)})
    except Exception as e:
        log.error(f"Gagal fetch positions: {e}")
        return [{'symbol': '?', 'side': 'unknown', 'contracts': 0,
                 'entry': 0, 'pnl': 0, 'pnl_pct': 0}]
    return out


def busy_symbols():
    """Pair yang lagi dipegang atau lagi nunggu limit kefill."""
    syms = {p['symbol'] for p in all_positions()}
    syms |= set(SIM_PENDINGS.keys())
    syms |= set(PENDING_ORDERS.keys())
    return syms


def open_risk_pct():
    """Total risiko posisi terbuka + order nunggu, dalam % saldo."""
    bal = BALANCE['usdt'] or fetch_usdt_balance()
    if not bal:
        return 0.0
    return sum(PLANNED_RISK.values()) / bal * 100


def setup_pair(symbol, leverage):
    """Set mode posisi, margin, dan leverage sebelum kirim order.
    Dipanggil per pair karena bot pindah-pindah pair dan leverage-nya adaptif."""
    try:
        exchange.set_position_mode(hedged=False, symbol=symbol)
    except Exception:
        pass
    try:
        exchange.set_margin_mode(MARGIN_MODE, symbol)
    except Exception:
        pass
    try:
        exchange.set_leverage(leverage, symbol)
        log.info(f"  Leverage {symbol} -> {leverage}x")
    except Exception as e:
        log.warning(f"  Gagal set leverage {symbol}: {e}")


def place_entry(plan, symbol, last=None):

    # MARKET ENTRY: harga sekarang jarang sama persis dengan zona yang
    # direncanakan AI. Kalau SL/TP dibiarkan di level lama, jarak SL berubah
    # dan risiko nggak lagi RISK_PCT — geser semua level sejauh selisihnya
    # supaya bentuk plan-nya utuh.
    if ENTRY_MODE == 'market' and last:
        shift = last - plan['entry']
        if abs(shift) / plan['entry'] * 100 > MAX_ENTRY_DRIFT_PCT:
            msg = (f"harga udah gerak {abs(shift) / plan['entry'] * 100:.2f}% "
                   f"dari zona rencana (maks {MAX_ENTRY_DRIFT_PCT}%)")
            log.warning(f"Entry dibatalkan: {msg}")
            journal('entry_rejected', symbol=symbol, reason=msg)
            return False
        plan = {**plan,
                'entry': last,
                'sl': plan['sl'] + shift,
                'tp': plan['tp'] + shift}
        log.info(f"  Market entry: level digeser {shift:+.6f} "
                 f"(SL {plan['sl']:.6f} / TP {plan['tp']:.6f})")

    want_notional, risk_usd = target_notional(plan['sl_pct'])
    if want_notional < MIN_NOTIONAL_USD:
        log.warning(f"Notional {want_notional:.2f} di bawah minimum "
                    f"{MIN_NOTIONAL_USD} — entry dilewati.")
        journal('entry_rejected', symbol=symbol, reason='notional terlalu kecil')
        return False

    amt, notional, err = calc_amount(symbol, plan['entry'], want_notional)
    if err:
        log.warning(f"Entry dibatalkan: {err}")
        journal('entry_rejected', symbol=symbol, reason=err)
        return False

    sl = float(exchange.price_to_precision(symbol, plan['sl']))
    tp = float(exchange.price_to_precision(symbol, plan['tp']))
    entry = float(exchange.price_to_precision(symbol, plan['entry']))

    # Fee ikut dihitung, biar angka di sini cocok sama PnL yang nanti tercatat
    # saat posisi ditutup. Tanpa ini, "Kalau SL kena -3.503" ternyata jadi
    # -3.7065 di akhir — selisihnya persis fee bolak-balik.
    fee_rt = round_trip_fee(notional)
    risk = notional * plan['sl_pct'] / 100 + fee_rt
    reward = notional * plan['tp_pct'] / 100 - fee_rt

    log.info(f"PLAN {plan['side'].upper()} {symbol}")
    log.info(f"  Entry {entry} | SL {sl} ({plan['sl_pct']:.2f}%) | "
             f"TP {tp} ({plan['tp_pct']:.2f}%)")
    bal = BALANCE['usdt'] or 0
    lev = max(plan['leverage'], 1)
    margin = notional / lev
    PLANNED_RISK[symbol] = risk
    log.info(f"  Ukuran posisi {notional:.2f} USDT ({amt} kontrak) @ {lev}x")
    log.info(f"  Modal ketahan (margin) {margin:.2f} USDT")
    log.info(f"  Kalau SL kena : -{risk:.3f} USDT  (ROI margin -{risk / margin * 100:.1f}%) "
             f"[fee {fee_rt:.3f} sudah termasuk]")
    log.info(f"  Kalau TP kena : +{reward:.3f} USDT  (ROI margin +{reward / margin * 100:.1f}%)")
    log.info(f"  RR bersih 1:{plan['rr']:.2f}")
    if bal:
        log.info(f"  Risiko {risk / bal * 100:.2f}% dari saldo {bal:.2f} USDT "
                 f"(target {RISK_PCT}%)")
    log.info(f"  Confidence {plan['confidence']} | {plan['reason']}")

    if DRY_RUN:
        if ENTRY_MODE == 'limit':
            # Limit order TIDAK langsung kefill. Versi lama nganggep setiap
            # limit pasti kena, padahal harga bisa aja nggak pernah balik ke
            # zona itu — bikin hasil simulasi kelewat bagus.
            SIM_PENDINGS[symbol] = {'symbol': symbol, 'side': plan['side'],
                           'contracts': amt, 'entry': entry, 'notional': notional,
                           'sl': sl, 'tp': tp, 'cycles': 0, 'plan': plan,
                           'leverage': lev, 'margin': margin, 'rr': plan['rr'],
                           'thesis': plan.get('reason'),
                           'thesis_structure': plan.get('structure'),
                           'placed_ts': exchange.milliseconds()}
            log.info("[DRY_RUN] Limit dipasang — nunggu harga nyentuh zona.")
            journal('dry_run_limit_placed', symbol=symbol, side=plan['side'],
                    entry=entry, sl=sl, tp=tp, notional=notional,
                    leverage=plan['leverage'], rr=plan['rr'],
                    confidence=plan['confidence'], reason=plan['reason'],
                    structure=plan['structure'])
            return True

        fill = last or entry
        SIM_POSITIONS[symbol] = {'symbol': symbol,
                        'side': 'long' if plan['side'] == 'buy' else 'short',
                        'contracts': amt, 'entry': fill, 'notional': amt * fill,
                        'pnl': 0.0, 'pnl_pct': 0.0, 'sl': sl, 'tp': tp,
                        'leverage': lev, 'margin': amt * fill / lev,
                        'rr': plan['rr'],
                        'thesis': plan.get('reason'),
                        'thesis_structure': plan.get('structure'),
                        'opened_ts': exchange.milliseconds()}
        log.info("[DRY_RUN] Market order disimulasikan kefill di harga sekarang.")
        journal('dry_run_open', symbol=symbol, side=plan['side'], amount=amt,
                notional=amt * fill, entry=fill, sl=sl, tp=tp,
                leverage=plan['leverage'], rr=plan['rr'],
                confidence=plan['confidence'], reason=plan['reason'],
                structure=plan['structure'], mode='market')
        return True

    setup_pair(symbol, plan['leverage'])

    params = {'reduceOnly': False,
              'stopLoss': {'triggerPrice': sl},
              'takeProfit': {'triggerPrice': tp}}

    try:
        if ENTRY_MODE == 'limit':
            order = exchange.create_order(symbol, 'limit', plan['side'], amt,
                                          entry, params)
            PENDING_ORDERS[symbol] = {'id': order.get('id'), 'symbol': symbol,
                             'cycles': 0, 'plan': plan}
            log.info(f"  Limit order dipasang. ID {order.get('id')}")
        else:
            order = exchange.create_order(symbol, 'market', plan['side'], amt,
                                          None, params)
            log.info(f"  Market order terkirim. ID {order.get('id')}")
    except Exception as e:
        log.error(f"Gagal kirim order: {e}")
        journal('order_failed', symbol=symbol, error=str(e))
        return False

    journal('opened', symbol=symbol, side=plan['side'], amount=amt,
            notional=notional, entry=entry, sl=sl, tp=tp,
            leverage=plan['leverage'], rr=plan['rr'],
            confidence=plan['confidence'], reason=plan['reason'],
            structure=plan['structure'], mode=ENTRY_MODE)
    return True


def pos_metrics(pos):
    """Balikin (margin, roi_margin_pct, gerak_harga_pct) buat sebuah posisi.

    Tiga angka ini sering ketuker:
      - PnL USDT     : yang beneran nambah/ngurangin saldo
      - ROI margin   : PnL dibagi modal yang ketahan -> ini yang Bitget tampilin
      - gerak harga  : selisih harga doang, tanpa leverage
    Leverage cuma nentuin berapa modal yang ketahan, bukan besar PnL USDT-nya.
    """
    notional = pos.get('notional') or (pos.get('contracts', 0) * pos.get('entry', 0))
    lev = pos.get('leverage') or 1
    margin = pos.get('margin') or (notional / lev if lev else 0)
    pnl = pos.get('pnl') or 0
    roi = (pnl / margin * 100) if margin else 0

    # Gerak harga dihitung dari HARGA, bukan dari PnL. PnL udah dipotong fee,
    # jadi kalau dibagi notional hasilnya selalu meleset sebesar fee — bikin
    # SL 2.06% kelihatan jadi 2.18% di log dan nggak cocok sama chart.
    entry = pos.get('entry') or 0
    px = pos.get('exit') or pos.get('last_price') or 0
    if entry and px:
        d = 1 if pos.get('side') == 'long' else -1
        move = (px - entry) / entry * 100 * d
    else:
        move = 0
    return margin, roi, move


def close_position(pos, reason='manual'):
    if not pos or pos['contracts'] <= 0:
        return False
    symbol = pos['symbol']
    side = 'sell' if pos['side'] == 'long' else 'buy'
    margin, roi, move = pos_metrics(pos)
    log.info(f"CLOSE {pos['side'].upper()} {symbol} | PnL {pos['pnl']:+.4f} USDT "
             f"| ROI margin {roi:+.1f}% | gerak harga {move:+.2f}% "
             f"| alasan: {reason}")
    log.info(f"  Posisi {pos.get('notional', 0):.2f} USDT, margin {margin:.2f} USDT")

    # Habis kalah, istirahatin pair ini. Struktur yang barusan bikin rugi
    # jarang berubah dalam hitungan menit — balik lagi ke situ langsung
    # biasanya cuma bayar fee dua kali buat tesis yang sama.
    if (pos.get('pnl') or 0) < 0:
        COOLDOWN[symbol] = CYCLE + COOLDOWN_CYCLES
        log.info(f"  {symbol} diistirahatin {COOLDOWN_CYCLES} siklus.")

    PLANNED_RISK.pop(symbol, None)
    if DRY_RUN:
        SIM_POSITIONS.pop(symbol, None)
        LAST_MANAGE.pop(symbol, None)
        journal('dry_run_close', symbol=symbol, side=pos['side'],
                pnl=pos['pnl'], roi_margin_pct=round(roi, 2),
                price_move_pct=round(move, 3),
                notional=pos.get('notional'), margin=round(margin, 4),
                leverage=pos.get('leverage'), rr=pos.get('rr'), reason=reason)
        return True

    try:
        exchange.cancel_all_orders(symbol)
    except Exception as e:
        log.warning(f"Gagal cancel order: {e}")
    try:
        exchange.create_order(symbol, 'market', side, pos['contracts'], None,
                              {'reduceOnly': True})
        journal('closed', symbol=symbol, side=pos['side'], pnl=pos['pnl'],
                reason=reason)
        return True
    except Exception as e:
        log.error(f"GAGAL TUTUP POSISI {symbol}: {e} — TUTUP MANUAL!")
        journal('close_failed', symbol=symbol, error=str(e))
        return False


def check_pending():
    """Limit order yang nggak kefill dibatalkan setelah N siklus.

    Tanpa ini, order nyangkut di harga yang udah nggak relevan dan bot
    nggak bisa cari setup lain.
    """

    if DRY_RUN:
        # Beberapa limit bisa nunggu barengan sekarang, jadi diurus satu-satu.
        for sym in list(SIM_PENDINGS.keys()):
            pend = SIM_PENDINGS[sym]
            pend['cycles'] += 1
            entry = pend['entry']

            # Cuma candle yang dibuka SETELAH order dipasang yang dihitung.
            hi, lo, n = candles_after(sym, pend['placed_ts'])
            if n == 0:
                log.info(f"[DRY_RUN] Belum ada candle baru sejak order {sym} "
                         f"dipasang ({pend['cycles']}/{ENTRY_TIMEOUT_CYCLES})")
                continue

            # Limit buy kefill kalau harga sempat TURUN sampai level entry;
            # limit sell kalau sempat NAIK.
            touched = (lo <= entry) if pend['side'] == 'buy' else (hi >= entry)

            if touched:
                SIM_POSITIONS[sym] = {
                    'symbol': sym,
                    'side': 'long' if pend['side'] == 'buy' else 'short',
                    'contracts': pend['contracts'], 'entry': entry,
                    'notional': pend['notional'], 'pnl': 0.0, 'pnl_pct': 0.0,
                    'sl': pend['sl'], 'tp': pend['tp'],
                    'leverage': pend.get('leverage', 1),
                    'margin': pend.get('margin', 0),
                    'rr': pend.get('rr'),
                    'thesis': pend.get('thesis'),
                    'thesis_structure': pend.get('thesis_structure'),
                    'opened_ts': exchange.milliseconds()}
                log.info(f"[DRY_RUN] Limit {sym} kefill di {entry}")
                journal('dry_run_open', symbol=sym, side=pend['side'],
                        amount=pend['contracts'], notional=pend['notional'],
                        entry=entry, sl=pend['sl'], tp=pend['tp'],
                        cycles_waited=pend['cycles'], mode='limit')
                SIM_PENDINGS.pop(sym, None)
                continue

            if pend['cycles'] >= ENTRY_TIMEOUT_CYCLES:
                log.info(f"[DRY_RUN] Limit {sym} nggak kesentuh dalam "
                         f"{ENTRY_TIMEOUT_CYCLES} siklus — dibatalkan.")
                journal('dry_run_limit_timeout', symbol=sym, entry=entry)
                SIM_PENDINGS.pop(sym, None)
                PLANNED_RISK.pop(sym, None)
                continue

            log.info(f"[DRY_RUN] Nunggu {sym} nyentuh {entry} "
                     f"({pend['cycles']}/{ENTRY_TIMEOUT_CYCLES})")
        return False

    for sym in list(PENDING_ORDERS.keys()):
        po = PENDING_ORDERS[sym]
        po['cycles'] += 1
        try:
            o = exchange.fetch_order(po['id'], sym)
            status = o.get('status')
            if status == 'closed':
                log.info(f"Limit order kefill di {sym}")
                journal('limit_filled', symbol=sym, order_id=po['id'])
                PENDING_ORDERS.pop(sym, None)
                continue
            if status == 'canceled':
                PENDING_ORDERS.pop(sym, None)
                PLANNED_RISK.pop(sym, None)
                continue
        except Exception as e:
            log.warning(f"Gagal cek order {sym}: {e}")

        if po['cycles'] >= ENTRY_TIMEOUT_CYCLES:
            log.info(f"Limit order {sym} nggak kefill dalam "
                     f"{ENTRY_TIMEOUT_CYCLES} siklus — dibatalkan.")
            try:
                exchange.cancel_order(po['id'], sym)
            except Exception as e:
                log.warning(f"Gagal cancel {sym}: {e}")
            journal('limit_timeout', symbol=sym)
            PENDING_ORDERS.pop(sym, None)
            PLANNED_RISK.pop(sym, None)
            continue

        log.info(f"Nunggu limit order {sym} kefill "
                 f"({po['cycles']}/{ENTRY_TIMEOUT_CYCLES})")
    return False


# ============================================================
# SIKLUS
# ============================================================

def manage_position(pos):
    """Urus satu posisi terbuka. Balikin True kalau posisinya ditutup."""
    symbol = pos['symbol']
    info = market_snapshot(symbol)

    if DRY_RUN:
        price = info['last']
        d = 1 if pos['side'] == 'long' else -1
        fee = round_trip_fee(pos['notional'])
        pos['pnl'] = (price - pos['entry']) * d * pos['contracts'] - fee
        pos['pnl_pct'] = (price - pos['entry']) / pos['entry'] * 100 * d
        pos['last_price'] = price

        # Deteksi sentuhan cuma dari candle yang terbentuk SETELAH posisi
        # kebuka. Kalau belum ada candle baru, belum ada apa pun yang bisa
        # nyentuh SL/TP — posisi dibiarkan.
        hi, lo, n = candles_after(symbol, pos.get('opened_ts', 0))
        if n == 0:
            log.info(f"  {symbol}: belum ada candle baru sejak entry.")
            return False

        hit, fill = None, None
        if pos['side'] == 'long':
            if lo <= pos['sl']:
                hit, fill = 'stop_loss', pos['sl']
            elif hi >= pos['tp']:
                hit, fill = 'take_profit', pos['tp']
        else:
            if hi >= pos['sl']:
                hit, fill = 'stop_loss', pos['sl']
            elif lo <= pos['tp']:
                hit, fill = 'take_profit', pos['tp']

        if hit:
            # PnL dihitung DI level SL/TP, bukan di harga saat dicek.
            pos['pnl'] = (fill - pos['entry']) * d * pos['contracts'] - fee
            pos['pnl_pct'] = (fill - pos['entry']) / pos['entry'] * 100 * d
            pos['exit'] = fill
            log.info(f"[DRY_RUN] {hit.upper()} {symbol} kena di {fill} "
                     f"(fee {fee:.4f} USDT sudah dipotong)")
            close_position(pos, reason=hit)
            return True

    _m, _roi, _mv = pos_metrics(pos)
    log.info(f"Posisi aktif: {pos['side'].upper()} {symbol} @ {pos['entry']} "
             f"| {pos.get('notional', 0):.2f} USDT | PnL {pos['pnl']:+.4f} USDT "
             f"| ROI margin {_roi:+.1f}% | harga {_mv:+.2f}%")

    # SL/TP udah nempel di exchange, jadi nggak perlu tanya AI tiap siklus.
    # Throttle-nya per pair, bukan global — kalau nggak, posisi kedua bakal
    # ikut kena jatah skip gara-gara posisi pertama baru saja dicek.
    lm = LAST_MANAGE.setdefault(symbol, {'cycle': -99, 'price': None})
    moved = MANAGE_MOVE_PCT + 1
    if lm['price']:
        moved = abs(info['last'] - lm['price']) / lm['price'] * 100
    due = (CYCLE - lm['cycle']) >= MANAGE_EVERY_CYCLES
    if not due and moved < MANAGE_MOVE_PCT:
        log.info(f"  Skip cek AI {symbol} (gerak {moved:.2f}% < "
                 f"{MANAGE_MOVE_PCT}%). SL/TP tetap jaga posisi.")
        return False
    lm['cycle'], lm['price'] = CYCLE, info['last']

    obj, raw, api_ok = ask_ai_manage(info, pos)
    if not api_ok:
        log.warning("AI nggak bisa dihubungi — posisi dibiarkan, SL/TP "
                    "tetap aktif di exchange.")
        return False
    if not obj:
        log.warning(f"Gagal parse AI: '{raw[:120]}' -> default HOLD")
        return False

    dec = str(obj.get('decision', 'HOLD')).upper()
    valid = obj.get('thesis_valid')
    conf = obj.get('confidence') or 0

    log.info(f"Keputusan AI {symbol}: {dec} (conf {conf}) | "
             f"tesis {'MASIH BERLAKU' if valid is not False else 'PATAH'}")
    log.info(f"  Alasan: {obj.get('reason', '')}")
    journal('decision_manage', symbol=symbol, decision=dec, thesis_valid=valid,
            confidence=conf, reason=obj.get('reason'),
            structure=obj.get('structure'), price=info['last'],
            side=pos['side'], entry=pos['entry'], pnl=pos['pnl'])

    # AI mengusulkan, kode memutuskan — sama seperti validate_plan.
    # Tesis patah = alasan masuk sudah tidak ada. Menahan posisi setelah itu
    # bukan lagi mengikuti rencana, cuma berharap.
    if valid is False and conf >= THESIS_BREAK_MIN_CONF:
        log.info(f"  Tesis entry patah (conf {conf}) -> tutup posisi.")
        close_position(pos, reason='thesis_invalid')
        return True

    if dec == 'CLOSE':
        close_position(pos, reason='ai_signal')
        return True
    return False


def report_usage():
    """Pemakaian token nyata, bukan perkiraan.

    Yang dicetak volume mentahnya — tarif per token ada di halaman pricing
    Anthropic dan bisa berubah, jadi kali sendiri dengan tarif akun lo.
    """
    if not USAGE['calls'] or not USAGE['started']:
        return
    jam = max((time.time() - USAGE['started']) / 3600, 1 / 60)
    log.info(f"PEMAKAIAN: {USAGE['calls']} call | in {USAGE['in']:,} tok | "
             f"out {USAGE['out']:,} tok | {jam:.2f} jam jalan")
    log.info(f"  laju     : {USAGE['calls']/jam:.0f} call/jam | "
             f"{USAGE['in']/jam:,.0f} in/jam | {USAGE['out']/jam:,.0f} out/jam")
    if USAGE.get('cache_r'):
        log.info(f"  cache    : tulis {USAGE.get('cache_w',0):,} | "
                 f"baca {USAGE['cache_r']:,} tok (baca jauh lebih murah)")
    log.info(f"  proyeksi 24 jam: {USAGE['in']/jam*24:,.0f} input + "
             f"{USAGE['out']/jam*24:,.0f} output token")


def run_cycle():
    global CYCLE
    CYCLE += 1
    if CYCLE % 10 == 0:
        report_usage()

    check_pending()

    positions = all_positions()
    if any(p.get('side') == 'unknown' for p in positions):
        log.warning("Status posisi nggak jelas. Skip siklus.")
        return

    # --- kelola semua posisi yang lagi jalan ---
    for pos in list(positions):
        try:
            manage_position(pos)
        except Exception as e:
            log.error(f"Gagal kelola {pos.get('symbol')}: {e}")

    # --- masih ada slot buat posisi baru? ---
    busy = busy_symbols()
    slots = MAX_CONCURRENT_POSITIONS - len(busy)
    if slots <= 0:
        log.info(f"Slot penuh ({len(busy)}/{MAX_CONCURRENT_POSITIONS}: "
                 f"{', '.join(sorted(s for s in busy if s))}). Nggak nyari setup baru.")
        return

    risk_now = open_risk_pct()
    if risk_now >= MAX_TOTAL_RISK_PCT:
        log.info(f"Risiko terbuka {risk_now:.2f}% udah nyentuh plafon "
                 f"{MAX_TOTAL_RISK_PCT}%. Nggak nambah posisi.")
        return
    if busy:
        log.info(f"Slot kosong {slots} | risiko terbuka {risk_now:.2f}% "
                 f"dari plafon {MAX_TOTAL_RISK_PCT}%")

    # --- cari setup buat slot yang kosong ---
    candidates = scan_markets()
    if not candidates:
        log.info("Nggak ada kandidat yang lolos filter. Skip.")
        return

    opened = 0
    for c in candidates:
        sym = c['symbol']

        # Jangan dobel di pair yang sama. Dua posisi di pair yang sama bukan
        # dua taruhan — itu satu taruhan ukuran dobel.
        if sym in busy:
            log.info(f"  {sym} udah dipegang / nunggu fill, dilewati.")
            continue

        try:
            info = market_snapshot(sym)
        except Exception as e:
            log.warning(f"Gagal ambil data {sym}: {e}")
            continue

        plan_raw, raw, api_ok = ask_ai_entry(info)
        if not api_ok:
            log.error("AI nggak bisa dihubungi — siklus dihentikan. "
                      "Ini BUKAN 'nggak ada setup'.")
            return
        if not plan_raw:
            log.warning(f"{sym}: JSON dari AI nggak kebaca, pair dilewati")
            continue

        dec = str(plan_raw.get('decision', '')).upper()
        conf = plan_raw.get('confidence')
        reason = str(plan_raw.get('reason', ''))[:200]
        log.info(f"{sym}: {dec} (conf {conf}) — {reason}")

        journal('decision_entry', symbol=sym, decision=dec, confidence=conf,
                reason=reason, structure=str(plan_raw.get('structure', ''))[:200],
                price=info['last'], atr_pct=info['atr_pct'],
                entry=plan_raw.get('entry'), sl=plan_raw.get('stop_loss'),
                tp=plan_raw.get('take_profit'))

        if dec == 'WAIT':
            continue

        plan, err = validate_plan(plan_raw, info)
        if err:
            log.info(f"  Plan ditolak: {err}")
            journal('plan_rejected', symbol=sym, reason=err)
            continue

        if place_entry(plan, sym, last=info['last']):
            busy.add(sym)
            opened += 1
            risk_now = open_risk_pct()
            if opened >= slots:
                log.info(f"Slot habis ({MAX_CONCURRENT_POSITIONS} posisi). "
                         f"Berhenti nyari setup siklus ini.")
                return
            if risk_now >= MAX_TOTAL_RISK_PCT:
                log.info(f"Risiko terbuka {risk_now:.2f}% nyentuh plafon "
                         f"{MAX_TOTAL_RISK_PCT}%. Berhenti nambah posisi.")
                return

    if opened:
        log.info(f"{opened} setup dipasang siklus ini.")
    else:
        log.info("Nggak ada setup yang lolos validasi siklus ini.")


if __name__ == "__main__":
    log.info("=" * 60)
    log.info("BOT SCALPING AI — multi-pair scanner")
    log.info(">>> DRY RUN <<<" if DRY_RUN else ">>> LIVE — UANG ASLI <<<")
    log.info(f"Timeframe {TIMEFRAME} | risiko {RISK_PCT}%/trade | "
             f"max leverage {MAX_LEVERAGE}x")
    log.info(f"SL {SL_ATR_MULT}x ATR | TP {TP_ATR_MULT}x ATR | "
             f"RR minimum 1:{MIN_RR} -> breakeven win rate "
             f"{100 / (1 + MIN_RR):.1f}%")
    log.info(f"Model {AI_MODEL} | siklus {SLEEP_TIME}s | "
             f"{SCAN_CANDIDATES} kandidat per scan")
    log.info(f"Maks {MAX_CONCURRENT_POSITIONS} posisi barengan | "
             f"total risiko dibatasi {MAX_TOTAL_RISK_PCT}% saldo")
    log.info("=" * 60)

    USAGE['started'] = time.time()

    if not os.getenv("ANTHROPIC_API_KEY"):
        log.error("ANTHROPIC_API_KEY nggak ada di .env. Bot berhenti.")
        raise SystemExit(1)

    exchange.load_markets()

    try:
        bal = exchange.fetch_balance()
        log.info(f"USDT available: "
                 f"{float(bal.get('USDT', {}).get('free', 0) or 0):.2f}")
    except Exception as e:
        log.error(f"Gagal ambil saldo: {e}")

    existing = (all_positions() or [None])[0]
    if existing and existing.get('contracts', 0) > 0:
        log.warning(f"Ada posisi terbuka: {existing['side'].upper()} "
                    f"{existing['symbol']} @ {existing['entry']}")

    errors = 0
    while True:
        try:
            log.info("-" * 60)
            run_cycle()
            errors = 0
            time.sleep(SLEEP_TIME)
        except AIDown as e:
            log.error("=" * 60)
            log.error(f"AI GAGAL {AI_MAX_CONSECUTIVE_ERRORS}x BERTURUT-TURUT — "
                      f"BOT BERHENTI.")
            log.error(f"Penyebab terakhir: {e}")
            log.error("Bot yang jalan tanpa AI cuma keliatan sibuk, bukan "
                      "kerja. Cek API key / kuota dulu.")
            log.error("=" * 60)
            journal('bot_halted', reason='ai_down', error=str(e)[:300])
            for p in all_positions():
                if p.get('contracts', 0) > 0:
                    log.warning(f"PERHATIAN: posisi {p['symbol']} masih terbuka. "
                                f"SL/TP tetap aktif di exchange.")
            break
        except KeyboardInterrupt:
            log.info("Bot dihentikan manual.")
            p = (all_positions() or [None])[0]
            if p and p.get('contracts', 0) > 0:
                log.warning(f"PERHATIAN: posisi {p['symbol']} masih terbuka. "
                            f"SL tetap aktif di exchange.")
            break
        except Exception as e:
            errors += 1
            log.error(f"Error loop ({errors}/{MAX_LOOP_ERRORS}): {e}")
            if errors >= MAX_LOOP_ERRORS:
                log.error(f"{MAX_LOOP_ERRORS} error berturut-turut. Bot berhenti. "
                          f"Cek posisi manual.")
                journal('bot_halted', reason='loop_errors', error=str(e)[:300])
                break
            # Backoff naik: koneksi putus sebentar (VPN drop, exchange lag)
            # nggak langsung ngabisin jatah error.
            wait = min(30 * errors, 300)
            log.info(f"  Nunggu {wait}s sebelum coba lagi.")
            time.sleep(wait)