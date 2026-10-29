"""
[ARSIP] Bot Trading AI v1 - Bitget Futures (BTC/USDT saja)

Versi pertama, disimpan sebagai pembanding. Pengembangan aktif ada di
bot_scalp.py. SL/TP di sini persentase tetap, bukan dari volatilitas pair.
Jalankan dari root repo: python legacy/bot_v1.py

Versi perbaikan: stop loss wajib, position tracking, anti-stacking,
leverage eksplisit, one-way mode, precision handling.

PENTING: jalankan dengan DRY_RUN = True dulu sampai lo yakin logikanya benar.
"""

import ccxt
import os
import re
import time
import json
import logging
from datetime import datetime, timezone
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# KONFIGURASI
# ============================================================

# --- SAFETY SWITCH ---
# DRY_RUN = True  -> semua keputusan dihitung & dicatat, TAPI order tidak dikirim.
# DRY_RUN = False -> order beneran dikirim ke exchange.
DRY_RUN = True

# USE_DEMO = True -> pakai demo trading Bitget.
# Catatan: demo Bitget butuh API key terpisah dari akun demo, bukan key live lo.
USE_DEMO = False

# --- MARKET ---
PAIR = 'BTC/USDT:USDT'
TIMEFRAME = '15m'
CANDLE_LIMIT = 100          # naik dari 20; 100 candle 15m = ~25 jam konteks
SLEEP_TIME = 300

# --- POSISI & RISIKO ---
LEVERAGE = 30               # di-set eksplisit ke exchange saat startup
MARGIN_MODE = 'isolated'    # 'isolated' atau 'cross'
POSITION_MODE_HEDGED = False  # False = one-way mode (wajib untuk logika bot ini)

NOTIONAL_USD = 12.0         # nilai posisi (bukan margin). Margin = NOTIONAL / LEVERAGE

# SL/TP dihitung dari PERGERAKAN HARGA, bukan dari margin. Leverage nggak
# mengubah seberapa cepat level ini kesentuh — yang mengubah cuma jaraknya.
# Angka di bawah disetel untuk leverage tinggi: SL harus jauh di dalam jarak
# likuidasi, TP diperpendek biar lebih sering kena.
STOP_LOSS_PCT = 1.0         # % pergerakan harga dari entry
TAKE_PROFIT_PCT = 2.0       # % pergerakan harga dari entry (RR 1:2)

# Maintenance margin rate Bitget untuk BTC perpetual (~0.4%). Dipakai buat
# ngitung jarak likuidasi yang realistis, bukan sekadar 100/leverage.
MAINTENANCE_MARGIN_PCT = 0.5

# SL nggak boleh lebih dari sekian persen dari jarak likuidasi. Di bawah 50%
# artinya masih ada ruang buat wick dan slippage sebelum posisi hangus.
MAX_SL_VS_LIQ_RATIO = 0.5

# Fee taker Bitget per sisi (%). Dipakai buat ngitung RR bersih.
TAKER_FEE_PCT = 0.06

# Precision exchange bikin ukuran order nggak bisa persis NOTIONAL_USD.
# Kalau melesetnya lebih dari angka ini, order ditolak daripada jalan diam-diam
# dengan ukuran yang beda jauh dari yang lo kira.
MAX_NOTIONAL_DEVIATION_PCT = 20.0

# --- FILE ---
LOG_FILE = 'bot.log'
JOURNAL_FILE = 'journal.jsonl'

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-7s | %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger('bot')


def journal(event, **data):
    """Catat setiap keputusan & aksi ke file. Nanti dipakai buat feedback loop."""
    record = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'event': event,
        **data
    }
    try:
        with open(JOURNAL_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record) + '\n')
    except Exception as e:
        log.warning(f"Gagal nulis journal: {e}")


# ============================================================
# SETUP EXCHANGE
# ============================================================

exchange = ccxt.bitget({
    'apiKey': os.getenv('BITGET_API_KEY'),
    'secret': os.getenv('BITGET_API_SECRET'),
    'password': os.getenv('BITGET_PASSPHRASE'),
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
})

if USE_DEMO:
    exchange.set_sandbox_mode(True)
    log.info("Mode DEMO aktif (butuh API key akun demo Bitget)")

client = Anthropic(
    api_key=os.getenv("ANTHROPIC_API_KEY"),
    timeout=90.0,
)

market = None       # diisi di setup_exchange()
SIM_POSITION = None  # posisi simulasi, hanya dipakai saat DRY_RUN


def setup_exchange():
    """Load market, set position mode / margin mode / leverage secara eksplisit.

    FIX #4 & #5: leverage dan position mode nggak lagi diserahkan ke setting
    default akun. Kalau one-way mode gagal di-set, bot berhenti — karena logika
    open/close di bawah mengasumsikan one-way.
    """
    global market

    exchange.load_markets()
    market = exchange.market(PAIR)
    log.info(f"Market loaded: {PAIR}")
    log.info(f"  min amount : {market['limits']['amount']['min']}")
    log.info(f"  min cost   : {market['limits']['cost']['min']}")
    log.info(f"  precision  : {market['precision']['amount']}")

    # One-way mode itu syarat mutlak buat logika bot ini
    try:
        exchange.set_position_mode(hedged=POSITION_MODE_HEDGED, symbol=PAIR)
        log.info(f"Position mode: {'hedge' if POSITION_MODE_HEDGED else 'one-way'}")
    except Exception as e:
        msg = str(e).lower()
        if 'not modified' in msg or 'no change' in msg or 'same' in msg:
            log.info("Position mode sudah sesuai")
        else:
            log.error(f"GAGAL set position mode: {e}")
            log.error("Bot berhenti. Set manual ke One-Way Mode di app Bitget.")
            raise SystemExit(1)

    try:
        exchange.set_margin_mode(MARGIN_MODE, PAIR)
        log.info(f"Margin mode: {MARGIN_MODE}")
    except Exception as e:
        log.warning(f"Set margin mode dilewati: {e}")

    try:
        exchange.set_leverage(LEVERAGE, PAIR)
        log.info(f"Leverage: {LEVERAGE}x")
    except Exception as e:
        log.warning(f"Set leverage dilewati: {e} — cek manual di akun!")


def liquidation_distance_pct():
    """Perkiraan jarak likuidasi dalam % pergerakan harga (isolated margin).

    Rumus kasarnya: (100 / leverage) - maintenance margin rate.
    Ini estimasi, bukan angka pasti dari exchange — makanya dipakai konservatif.
    """
    return (100.0 / LEVERAGE) - MAINTENANCE_MARGIN_PCT


def validate_risk_config():
    """Tolak jalan kalau SL kelewat dekat ke titik likuidasi.

    Ini penting di leverage tinggi. Kalau SL lebih jauh dari likuidasi, posisi
    hangus duluan sebelum SL sempat kepanggil — dan itu artinya rugi seluruh
    margin, bukan rugi terkendali sebesar STOP_LOSS_PCT.
    """
    liq_dist = liquidation_distance_pct()
    max_safe_sl = liq_dist * MAX_SL_VS_LIQ_RATIO

    round_trip_fee = TAKER_FEE_PCT * 2
    net_tp = TAKE_PROFIT_PCT - round_trip_fee
    net_sl = STOP_LOSS_PCT + round_trip_fee
    net_rr = net_tp / net_sl if net_sl > 0 else 0

    log.info("=" * 55)
    log.info("VALIDASI RISIKO")
    log.info(f"  Leverage         : {LEVERAGE}x ({MARGIN_MODE})")
    log.info(f"  Jarak likuidasi  : ~{liq_dist:.2f}% pergerakan harga")
    log.info(f"  Stop loss        : {STOP_LOSS_PCT:.2f}%")
    log.info(f"  Buffer ke liq    : {liq_dist - STOP_LOSS_PCT:.2f}%")
    log.info(f"  Take profit      : {TAKE_PROFIT_PCT:.2f}%")
    log.info(f"  RR kotor         : 1:{TAKE_PROFIT_PCT / STOP_LOSS_PCT:.2f}")
    log.info(f"  RR bersih (fee)  : 1:{net_rr:.2f} "
             f"(fee pp {round_trip_fee:.2f}%)")

    if liq_dist <= 0:
        log.error(f"Leverage {LEVERAGE}x terlalu tinggi — likuidasi instan.")
        raise SystemExit(1)

    if STOP_LOSS_PCT >= liq_dist:
        log.error(f"FATAL: SL {STOP_LOSS_PCT}% LEBIH JAUH dari likuidasi "
                  f"{liq_dist:.2f}%. Posisi bakal hangus sebelum SL jalan.")
        log.error(f"Turunkan STOP_LOSS_PCT di bawah {max_safe_sl:.2f}% "
                  f"atau turunkan LEVERAGE.")
        raise SystemExit(1)

    if STOP_LOSS_PCT > max_safe_sl:
        log.error(f"SL {STOP_LOSS_PCT}% terlalu dekat ke likuidasi "
                  f"{liq_dist:.2f}%. Wick atau slippage bisa bikin lo "
                  f"keliduidasi duluan.")
        log.error(f"Maksimum yang aman di {LEVERAGE}x: {max_safe_sl:.2f}%")
        raise SystemExit(1)

    if net_rr < 1.0:
        log.warning(f"RR bersih cuma 1:{net_rr:.2f} — fee makan terlalu banyak. "
                    f"Perlebar TP atau kurangi frekuensi trade.")

    # Pengingat: SL dalam % harga itu tetap, tapi porsinya terhadap margin
    # membesar seiring leverage. Kerugian USD-nya sendiri nggak berubah.
    sl_vs_margin = STOP_LOSS_PCT * LEVERAGE
    log.info(f"  SL vs margin     : {sl_vs_margin:.0f}% dari margin terpakai")
    if sl_vs_margin > 50:
        log.warning(f"Sekali kena SL, {sl_vs_margin:.0f}% margin posisi hilang. "
                    f"Nominal USD-nya kecil, tapi butuh win rate tinggi.")
    log.info("=" * 55)


def show_account_info():
    try:
        balance = exchange.fetch_balance()
        usdt_free = float(balance.get('USDT', {}).get('free', 0) or 0)
        usdt_total = float(balance.get('USDT', {}).get('total', 0) or 0)

        log.info("=" * 55)
        log.info("INFORMASI AKUN")
        log.info(f"  USDT total     : {usdt_total:.2f}")
        log.info(f"  USDT available : {usdt_free:.2f}")

        # Hitung dari ukuran order yang BENERAN bakal dieksekusi di harga
        # sekarang, bukan dari NOTIONAL_USD mentah. Precision exchange bikin
        # dua angka itu beda, dan log yang nampilin target doang itu menyesatkan.
        try:
            price = exchange.fetch_ticker(PAIR)['last']
            amount, actual_notional, err = calculate_amount(price)
            if err:
                log.warning(f"  Sizing bermasalah: {err}")
            else:
                log.info(f"  Harga sekarang : {price:.2f}")
                log.info(f"  Target notional: {NOTIONAL_USD:.2f} USDT")
                log.info(f"  Notional aktual: {actual_notional:.2f} USDT "
                         f"(amount {amount})")
                log.info(f"  Margin dipakai : {actual_notional / LEVERAGE:.2f} USDT "
                         f"(leverage {LEVERAGE}x)")
                log.info(f"  Max rugi/trade : "
                         f"{actual_notional * STOP_LOSS_PCT / 100:.3f} USDT "
                         f"(SL {STOP_LOSS_PCT}%)")
                log.info(f"  Max untung     : "
                         f"{actual_notional * TAKE_PROFIT_PCT / 100:.3f} USDT "
                         f"(TP {TAKE_PROFIT_PCT}%)")
                log.info(f"  Risiko vs saldo: "
                         f"{actual_notional * STOP_LOSS_PCT / 100 / usdt_total * 100:.2f}% "
                         f"per trade" if usdt_total > 0 else "")
        except Exception as e:
            log.warning(f"  Gagal hitung sizing: {e}")

        log.info(f"  DRY_RUN        : {DRY_RUN}")
        log.info("=" * 55)

        return usdt_free
    except Exception as e:
        log.error(f"Gagal ambil info akun: {e}")
        return 0.0


# ============================================================
# POSITION TRACKING  (FIX #2 & #3)
# ============================================================

def get_open_position():
    """Return dict posisi terbuka, atau None kalau flat.

    Ini yang bikin bot berhenti numpuk posisi. Dipanggil SEBELUM setiap
    keputusan, jadi bot selalu tau kondisi sebenarnya di exchange.
    """
    # Saat dry run, posisi nyata nggak pernah kebuka. Tanpa simulasi ini,
    # bot bakal terus keliatan flat dan jalur manage (HOLD/CLOSE) plus logika
    # anti-stacking nggak pernah teruji.
    if DRY_RUN:
        return SIM_POSITION

    try:
        positions = exchange.fetch_positions([PAIR])
        for p in positions:
            contracts = float(p.get('contracts') or 0)
            if contracts > 0:
                return {
                    'side': p.get('side'),                       # 'long' / 'short'
                    'contracts': contracts,
                    'entry': float(p.get('entryPrice') or 0),
                    'notional': float(p.get('notional') or 0),
                    'pnl': float(p.get('unrealizedPnl') or 0),
                    'pnl_pct': float(p.get('percentage') or 0),
                    'liq': p.get('liquidationPrice'),
                }
        return None
    except Exception as e:
        log.error(f"Gagal fetch position: {e}")
        # Fail-safe: anggap ADA posisi biar bot nggak buka order baru
        # saat kondisi exchange nggak jelas.
        return {'side': 'unknown', 'contracts': 0, 'entry': 0,
                'notional': 0, 'pnl': 0, 'pnl_pct': 0, 'liq': None}


def has_protective_orders():
    """Cek apakah posisi sekarang punya SL/TP terpasang."""
    if DRY_RUN:
        return SIM_POSITION is not None and SIM_POSITION.get('sl') is not None
    try:
        orders = exchange.fetch_open_orders(PAIR, params={'stop': True})
        return len(orders) > 0
    except Exception:
        try:
            orders = exchange.fetch_open_orders(PAIR)
            return len(orders) > 0
        except Exception as e:
            log.warning(f"Gagal cek protective order: {e}")
            return False


# ============================================================
# SIZING  (FIX #6)
# ============================================================

def calculate_amount(price):
    """Hitung ukuran order dengan precision & minimum exchange.

    Precision amount BTC di Bitget = 0.0001, yang di harga sekarang setara
    ~6.5 USDT per step. Artinya NOTIONAL_USD hampir nggak pernah bisa dipenuhi
    persis. Dua hal penting di sini:
      1. Bulatkan ke step TERDEKAT, bukan ke bawah. amount_to_precision()
         defaultnya truncate — itu bikin order 12 USDT jadi 6.5 USDT diam-diam.
      2. Laporkan notional yang SEBENARNYA, biar angka risiko di log jujur.

    Return (amount, actual_notional, error_message).
    """
    raw_amount = NOTIONAL_USD / price
    step = market['precision']['amount']

    try:
        if step:
            amount = round(raw_amount / float(step)) * float(step)
            amount = float(exchange.amount_to_precision(PAIR, amount))
        else:
            amount = float(exchange.amount_to_precision(PAIR, raw_amount))
    except Exception as e:
        return None, 0.0, f"Gagal apply precision: {e}"

    if amount <= 0:
        return None, 0.0, (f"Amount jadi 0 setelah rounding (raw {raw_amount:.8f}). "
                           f"Naikkan NOTIONAL_USD.")

    min_amount = market['limits']['amount']['min']
    if min_amount and amount < float(min_amount):
        return None, 0.0, (f"Amount {amount} di bawah minimum {min_amount}. "
                           f"Naikkan NOTIONAL_USD ke minimal "
                           f"{float(min_amount) * price * 1.05:.2f} USDT")

    actual_notional = amount * price

    min_cost = market['limits']['cost']['min']
    if min_cost and actual_notional < float(min_cost):
        return None, 0.0, (f"Nilai order {actual_notional:.2f} USDT di bawah "
                           f"minimum {min_cost} USDT")

    deviation = (actual_notional - NOTIONAL_USD) / NOTIONAL_USD * 100
    if abs(deviation) > MAX_NOTIONAL_DEVIATION_PCT:
        return None, 0.0, (
            f"Notional aktual {actual_notional:.2f} USDT meleset {deviation:+.1f}% "
            f"dari target {NOTIONAL_USD:.2f} USDT (step {step} = "
            f"{float(step) * price:.2f} USDT). Set NOTIONAL_USD ke kelipatan "
            f"terdekat, misal {round(raw_amount / float(step)) * float(step) * price:.2f}"
        )

    if abs(deviation) > 2:
        log.info(f"Notional aktual {actual_notional:.2f} USDT ({deviation:+.1f}% "
                 f"dari target) — dibatasi step precision {step}")

    return amount, actual_notional, None


# ============================================================
# EKSEKUSI  (FIX #1 & #5)
# ============================================================

def open_position(side, price):
    """Buka posisi dengan SL & TP menempel.

    side: 'buy' (long) atau 'sell' (short)

    Aturan keras: kalau SL gagal terpasang, posisi langsung ditutup.
    Bot ini tidak akan pernah memegang posisi tanpa proteksi.
    """
    global SIM_POSITION

    amount, actual_notional, err = calculate_amount(price)
    if err:
        log.warning(f"Order dibatalkan: {err}")
        journal('order_rejected', reason=err, side=side, price=price)
        return False

    if side == 'buy':
        sl_price = price * (1 - STOP_LOSS_PCT / 100)
        tp_price = price * (1 + TAKE_PROFIT_PCT / 100)
    else:
        sl_price = price * (1 + STOP_LOSS_PCT / 100)
        tp_price = price * (1 - TAKE_PROFIT_PCT / 100)

    sl_price = float(exchange.price_to_precision(PAIR, sl_price))
    tp_price = float(exchange.price_to_precision(PAIR, tp_price))

    risk_usd = actual_notional * (STOP_LOSS_PCT / 100)
    reward_usd = actual_notional * (TAKE_PROFIT_PCT / 100)

    log.info(f"OPEN {'LONG' if side == 'buy' else 'SHORT'} | "
             f"amount {amount} | notional {actual_notional:.2f} USDT | "
             f"entry ~{price:.2f}")
    log.info(f"     SL {sl_price:.2f} (-{risk_usd:.3f} USDT) | "
             f"TP {tp_price:.2f} (+{reward_usd:.3f} USDT)")

    if DRY_RUN:
        log.info("[DRY_RUN] Order tidak dikirim — posisi dicatat sebagai simulasi.")
        SIM_POSITION = {
            'side': 'long' if side == 'buy' else 'short',
            'contracts': amount,
            'entry': price,
            'notional': actual_notional,
            'pnl': 0.0,
            'pnl_pct': 0.0,
            'liq': None,
            'sl': sl_price,
            'tp': tp_price,
        }
        journal('dry_run_open', side=side, amount=amount, notional=actual_notional,
                price=price, sl=sl_price, tp=tp_price)
        return True

    # reduceOnly=False memastikan ini OPEN, bukan nutup posisi lawan.
    # Ini fix untuk masalah create_market_sell_order yang ambigu di Bitget.
    params = {
        'reduceOnly': False,
        'stopLoss': {'triggerPrice': sl_price},
        'takeProfit': {'triggerPrice': tp_price},
    }

    try:
        order = exchange.create_order(PAIR, 'market', side, amount, None, params)
        log.info(f"Order terkirim. ID: {order.get('id')}")
    except Exception as e:
        log.error(f"Gagal kirim order: {e}")
        journal('order_failed', side=side, error=str(e))
        return False

    # Verifikasi: posisi kebuka & SL beneran nempel
    time.sleep(2)
    pos = get_open_position()
    if not pos or pos['contracts'] == 0:
        log.error("Order terkirim tapi posisi tidak terdeteksi. Cek manual!")
        journal('open_unverified', side=side)
        return False

    if not has_protective_orders():
        log.warning("SL/TP tidak terdeteksi. Mencoba pasang manual...")
        if not attach_stop_loss(pos, sl_price, tp_price):
            log.error("SL TIDAK BISA DIPASANG. Menutup posisi sekarang juga.")
            close_position(pos, reason='sl_attach_failed')
            return False

    journal('opened', side=side, amount=amount, entry=pos['entry'],
            sl=sl_price, tp=tp_price)
    return True


def attach_stop_loss(pos, sl_price, tp_price):
    """Pasang SL & TP sebagai trigger order reduce-only terpisah."""
    close_side = 'sell' if pos['side'] == 'long' else 'buy'
    ok = False
    try:
        exchange.create_order(
            PAIR, 'market', close_side, pos['contracts'], None,
            {'reduceOnly': True, 'triggerPrice': sl_price, 'stopLossPrice': sl_price}
        )
        log.info(f"SL terpasang di {sl_price:.2f}")
        ok = True
    except Exception as e:
        log.error(f"Gagal pasang SL: {e}")

    try:
        exchange.create_order(
            PAIR, 'market', close_side, pos['contracts'], None,
            {'reduceOnly': True, 'triggerPrice': tp_price, 'takeProfitPrice': tp_price}
        )
        log.info(f"TP terpasang di {tp_price:.2f}")
    except Exception as e:
        log.warning(f"Gagal pasang TP: {e} (tidak fatal)")

    return ok


def close_position(pos, reason='manual'):
    """Tutup posisi dengan reduceOnly. FIX #2 — sebelumnya fungsi ini nggak ada."""
    if not pos or pos['contracts'] <= 0:
        return False

    close_side = 'sell' if pos['side'] == 'long' else 'buy'
    log.info(f"CLOSE {pos['side'].upper()} | {pos['contracts']} | "
             f"PnL {pos['pnl']:.4f} USDT | alasan: {reason}")

    if DRY_RUN:
        globals()['SIM_POSITION'] = None
        log.info("[DRY_RUN] Close tidak dikirim — posisi simulasi dihapus.")
        journal('dry_run_close', side=pos['side'], pnl=pos['pnl'], reason=reason)
        return True

    try:
        exchange.cancel_all_orders(PAIR)
    except Exception as e:
        log.warning(f"Gagal cancel order lama: {e}")

    try:
        order = exchange.create_order(
            PAIR, 'market', close_side, pos['contracts'], None,
            {'reduceOnly': True}
        )
        log.info(f"Posisi ditutup. ID: {order.get('id')}")
        journal('closed', side=pos['side'], pnl=pos['pnl'], reason=reason)
        return True
    except Exception as e:
        log.error(f"GAGAL TUTUP POSISI: {e} — TUTUP MANUAL SEKARANG!")
        journal('close_failed', side=pos['side'], error=str(e))
        return False


# ============================================================
# AI
# ============================================================

def get_market_data():
    ohlcv = exchange.fetch_ohlcv(PAIR, TIMEFRAME, limit=CANDLE_LIMIT)
    lines = ["Time(UTC) | Open | High | Low | Close | Volume"]
    for c in ohlcv:
        ts = datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc).strftime('%m-%d %H:%M')
        lines.append(f"{ts} | {c[1]} | {c[2]} | {c[3]} | {c[4]} | {c[5]}")
    return "\n".join(lines)


def parse_decision(text, valid_options):
    """Ekstrak keputusan dari balasan AI. FIX #7.

    Sebelumnya pakai .strip().upper() — kalau AI balas 'BUY.' atau '**BUY**',
    nggak match branch manapun dan bot diam tanpa error.
    """
    if not text:
        return None
    upper = text.upper()
    found = [opt for opt in valid_options if re.search(rf'\b{opt}\b', upper)]
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        log.warning(f"AI sebut beberapa opsi: {found}. Diabaikan.")
    return None


def call_ai(prompt):
    try:
        message = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        parts = []
        for block in getattr(message, 'content', []) or []:
            if isinstance(block, dict):
                if block.get('type') == 'text':
                    parts.append(block.get('text', ''))
            elif getattr(block, 'type', None) == 'text':
                parts.append(getattr(block, 'text', ''))
        return "\n".join(parts).strip()
    except Exception as e:
        log.error(f"AI error: {e}")
        return ""


def ask_ai_entry(market_data):
    prompt = f"""Kamu seorang Quant Trading Analyst.

Data candlestick {PAIR} timeframe {TIMEFRAME} ({CANDLE_LIMIT} candle terakhir):

{market_data}

Saat ini TIDAK ADA posisi terbuka. Tentukan aksi:
- BUY  : struktur bullish, ada alasan konkret untuk long
- SELL : struktur bearish, ada alasan konkret untuk short
- WAIT : tidak ada setup yang jelas

Balas HANYA satu kata: BUY, SELL, atau WAIT."""

    raw = call_ai(prompt)
    decision = parse_decision(raw, ['BUY', 'SELL', 'WAIT'])
    if decision is None:
        log.warning(f"Tidak bisa parse jawaban AI: '{raw[:120]}' -> default WAIT")
        return 'WAIT', raw
    return decision, raw


def ask_ai_manage(market_data, pos):
    prompt = f"""Kamu seorang Quant Trading Analyst.

Data candlestick {PAIR} timeframe {TIMEFRAME}:

{market_data}

POSISI TERBUKA SAAT INI:
- Arah        : {pos['side'].upper()}
- Entry price : {pos['entry']}
- PnL         : {pos['pnl']:.4f} USDT ({pos['pnl_pct']:.2f}%)

Stop loss dan take profit sudah terpasang otomatis di exchange.
Tentukan aksi:
- HOLD  : biarkan posisi jalan, tesis awal masih valid
- CLOSE : tutup sekarang, struktur sudah berbalik melawan posisi

Balas HANYA satu kata: HOLD atau CLOSE."""

    raw = call_ai(prompt)
    decision = parse_decision(raw, ['HOLD', 'CLOSE'])
    if decision is None:
        log.warning(f"Tidak bisa parse jawaban AI: '{raw[:120]}' -> default HOLD")
        return 'HOLD', raw
    return decision, raw


# ============================================================
# SIKLUS UTAMA
# ============================================================

def run_cycle():
    pos = get_open_position()

    # Fail-safe dari get_open_position saat fetch error
    if pos and pos['side'] == 'unknown':
        log.warning("Status posisi tidak jelas. Skip siklus ini.")
        return

    ticker = exchange.fetch_ticker(PAIR)
    price = ticker['last']

    # Simulasi SL/TP saat dry run: cek apakah harga sudah menyentuh level.
    if DRY_RUN and pos:
        direction = 1 if pos['side'] == 'long' else -1
        pos['pnl'] = (price - pos['entry']) * direction * pos['contracts']
        pos['pnl_pct'] = (price - pos['entry']) / pos['entry'] * 100 * direction

        hit = None
        if pos['side'] == 'long':
            if price <= pos['sl']:
                hit = 'stop_loss'
            elif price >= pos['tp']:
                hit = 'take_profit'
        else:
            if price >= pos['sl']:
                hit = 'stop_loss'
            elif price <= pos['tp']:
                hit = 'take_profit'

        if hit:
            log.info(f"[DRY_RUN] {hit.upper()} tersentuh di {price:.2f}")
            close_position(pos, reason=hit)
            return

    data = get_market_data()

    if pos:
        # ADA POSISI -> kelola, jangan buka baru. Ini yang mencegah stacking.
        log.info(f"Posisi aktif: {pos['side'].upper()} @ {pos['entry']} | "
                 f"PnL {pos['pnl']:.4f} USDT ({pos['pnl_pct']:.2f}%) | "
                 f"liq {pos['liq']}")

        if not has_protective_orders() and not DRY_RUN:
            log.warning("Posisi terbuka TANPA proteksi. Memasang SL darurat...")
            if pos['side'] == 'long':
                sl = pos['entry'] * (1 - STOP_LOSS_PCT / 100)
                tp = pos['entry'] * (1 + TAKE_PROFIT_PCT / 100)
            else:
                sl = pos['entry'] * (1 + STOP_LOSS_PCT / 100)
                tp = pos['entry'] * (1 - TAKE_PROFIT_PCT / 100)
            if not attach_stop_loss(pos, float(exchange.price_to_precision(PAIR, sl)),
                                    float(exchange.price_to_precision(PAIR, tp))):
                close_position(pos, reason='no_protection')
                return

        decision, raw = ask_ai_manage(data, pos)
        log.info(f"Keputusan AI: {decision}")
        journal('decision_manage', decision=decision, price=price,
                side=pos['side'], pnl=pos['pnl'], raw=raw[:200])

        if decision == 'CLOSE':
            close_position(pos, reason='ai_signal')
    else:
        # FLAT -> boleh cari entry
        log.info(f"Tidak ada posisi. Harga sekarang: {price:.2f}")

        decision, raw = ask_ai_entry(data)
        log.info(f"Keputusan AI: {decision}")
        journal('decision_entry', decision=decision, price=price, raw=raw[:200])

        if decision == 'BUY':
            open_position('buy', price)
        elif decision == 'SELL':
            open_position('sell', price)
        else:
            log.info("WAIT — tidak ada eksekusi.")


if __name__ == "__main__":
    log.info("=" * 55)
    log.info(f"BOT TRADING AI — {PAIR} {TIMEFRAME}")
    if DRY_RUN:
        log.info(">>> MODE DRY RUN — tidak ada order yang dikirim <<<")
    else:
        log.info(">>> MODE LIVE — UANG ASLI <<<")
    log.info("=" * 55)

    setup_exchange()
    validate_risk_config()
    show_account_info()

    # Cek posisi nyangkut dari sesi sebelumnya
    existing = get_open_position()
    if existing and existing['contracts'] > 0:
        log.warning(f"Ada posisi terbuka dari sesi lalu: {existing['side'].upper()} "
                    f"@ {existing['entry']} | PnL {existing['pnl']:.4f} USDT")
        log.warning("Bot akan mengelola posisi ini, bukan membuka yang baru.")

    consecutive_errors = 0

    while True:
        try:
            log.info("-" * 55)
            run_cycle()
            consecutive_errors = 0
            log.info(f"Tidur {SLEEP_TIME} detik...")
            time.sleep(SLEEP_TIME)

        except KeyboardInterrupt:
            log.info("Bot dihentikan manual.")
            pos = get_open_position()
            if pos and pos['contracts'] > 0:
                log.warning(f"PERHATIAN: masih ada posisi {pos['side'].upper()} "
                            f"terbuka. SL tetap aktif di exchange.")
            break

        except Exception as e:
            consecutive_errors += 1
            log.error(f"Error di loop utama ({consecutive_errors}): {e}")
            if consecutive_errors >= 5:
                log.error("5 error berturut-turut. Bot berhenti demi keamanan.")
                log.error("Cek posisi terbuka lo di Bitget secara manual.")
                break
            time.sleep(60)