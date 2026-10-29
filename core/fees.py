"""Matematika fee, risk-reward, dan leverage untuk Bitget USDT-M futures.

Semua fungsi murni dan menerima parameter eksplisit, jadi bot live dan
backtest bisa pakai config masing-masing tanpa saling nempel lewat global.

Konvensi fee (VIP0): limit order = maker, market order & stop loss = taker,
take profit (limit) = maker. Semua persen ditulis sebagai angka persen
(0.06 artinya 0.06%).
"""

from __future__ import annotations

TAKER_FEE_PCT = 0.06
MAKER_FEE_PCT = 0.02


def entry_fee_pct(entry_mode: str, maker: float = MAKER_FEE_PCT,
                  taker: float = TAKER_FEE_PCT, assume_maker: bool = True) -> float:
    """Fee masuk: limit -> maker, market -> taker."""
    return maker if (assume_maker and entry_mode == 'limit') else taker


def worst_fee_pct(entry_mode: str, maker: float = MAKER_FEE_PCT,
                  taker: float = TAKER_FEE_PCT, assume_maker: bool = True) -> float:
    """Fee bolak-balik kasus terburuk (keluar via SL = taker).

    Dipakai buat sizing dan filter RR, supaya perencanaan selalu pakai
    asumsi paling pahit.
    """
    return entry_fee_pct(entry_mode, maker, taker, assume_maker) + taker


def round_trip_fee(notional: float, entry_mode: str, exit_kind: str = 'stop_loss',
                   maker: float = MAKER_FEE_PCT, taker: float = TAKER_FEE_PCT,
                   assume_maker: bool = True) -> float:
    """Total fee masuk + keluar dalam USDT. TP keluar sebagai maker, selain itu taker."""
    exit_fee = maker if exit_kind == 'take_profit' else taker
    return notional * (entry_fee_pct(entry_mode, maker, taker, assume_maker) + exit_fee) / 100


def net_rr(sl_pct: float, tp_pct: float, entry_mode: str,
           maker: float = MAKER_FEE_PCT, taker: float = TAKER_FEE_PCT,
           assume_maker: bool = True) -> float:
    """Risk-reward bersih setelah fee.

    Kalah = jarak SL + fee terburuk. Menang = jarak TP - (fee masuk + fee TP maker).
    """
    denom = sl_pct + worst_fee_pct(entry_mode, maker, taker, assume_maker)
    win_fee = entry_fee_pct(entry_mode, maker, taker, assume_maker) + maker
    return ((tp_pct - win_fee) / denom) if denom > 0 else 0.0


def safe_leverage(sl_pct: float, max_leverage: int, liq_ratio: float = 0.5,
                  maintenance_pct: float = 0.5) -> int:
    """Leverage tertinggi yang masih bikin SL aman dari likuidasi.

    Turunan dari: sl_pct <= (100 / lev - maintenance_pct) * liq_ratio.
    Pair yang volatil (SL lebar) otomatis dapat leverage lebih rendah.
    """
    if sl_pct <= 0:
        return 1
    denom = sl_pct / liq_ratio + maintenance_pct
    if denom <= 0:
        return 1
    return max(1, min(int(max_leverage), int(100.0 / denom)))


def risk_notional(balance: float, risk_pct: float, sl_pct: float, fee_pct: float) -> float:
    """Ukuran posisi berbasis risiko.

    notional = (risk_pct% x saldo) / (sl% + fee%). SL lebar otomatis
    mengecilkan posisi; leverage nggak ngaruh ke PnL USDT.
    """
    loss_pct = sl_pct + fee_pct
    if loss_pct <= 0 or balance <= 0:
        return 0.0
    risk_usd = balance * risk_pct / 100
    return risk_usd / (loss_pct / 100)
