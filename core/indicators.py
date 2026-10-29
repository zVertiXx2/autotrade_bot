"""Indikator teknikal yang dipakai bareng oleh bot live dan backtest.

Satu sumber kebenaran. Sebelum modul ini ada, bot_scalp.py dan backtest.py
punya salinan masing-masing — dan ATR-nya ternyata beda rumus (Wilder vs
rata-rata biasa), padahal komentarnya bilang "sama persis".

Semua fungsi murni: input list angka / candle OHLCV, output angka.
Candle OHLCV mengikuti format ccxt: [timestamp, open, high, low, close, volume].
"""

from __future__ import annotations

from typing import Optional, Sequence

Candle = Sequence[float]


def ema_series(values: Sequence[float], period: int) -> list[float]:
    """Deret EMA. Elemen pertama = SMA dari `period` nilai pertama.

    Balikin list kosong kalau data kurang dari `period`.
    """
    if period <= 0 or len(values) < period:
        return []
    k = 2 / (period + 1)
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def ema_last(values: Sequence[float], period: int) -> Optional[float]:
    """Nilai EMA terakhir saja, atau None kalau data kurang."""
    s = ema_series(values, period)
    return s[-1] if s else None


def rsi(closes: Sequence[float], period: int = 14) -> Optional[float]:
    """RSI dengan smoothing Wilder. None kalau data kurang dari period + 1."""
    if period <= 0 or len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 100.0
    return 100 - (100 / (1 + ag / al))


def _true_ranges(candles: Sequence[Candle]) -> list[float]:
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i][2], candles[i][3], candles[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return trs


def atr(candles: Sequence[Candle], period: int = 14) -> Optional[float]:
    """Average True Range dengan smoothing Wilder (definisi standar).

    Ini yang dipakai bot live buat nentuin lebar SL/TP per pair.
    """
    if period <= 0 or len(candles) < period + 1:
        return None
    trs = _true_ranges(candles)
    val = sum(trs[:period]) / period
    for t in trs[period:]:
        val = (val * (period - 1) + t) / period
    return val


def atr_sma(candles: Sequence[Candle], period: int = 14) -> Optional[float]:
    """ATR versi rata-rata biasa dari `period` true range terakhir.

    Ini rumus lama backtest.py. Disimpan supaya hasil backtest lama bisa
    direproduksi persis (lihat ATR_METHOD di backtest.py).
    """
    if period <= 0 or len(candles) < period + 1:
        return None
    trs = _true_ranges(candles)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period
