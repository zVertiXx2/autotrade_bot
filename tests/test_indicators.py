import pytest

from core import indicators as ind


def candles_from_closes(closes, spread=1.0):
    """Candle sintetis: open = close sebelumnya, high/low = close +/- spread."""
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append([i, prev, max(prev, c) + spread, min(prev, c) - spread, c, 1.0])
        prev = c
    return out


class TestEma:
    def test_data_kurang_balik_kosong(self):
        assert ind.ema_series([1, 2], 3) == []
        assert ind.ema_last([1, 2], 3) is None

    def test_nilai_pertama_adalah_sma(self):
        assert ind.ema_series([2, 4, 6], 3) == [4.0]

    def test_deret_konstan_tetap_konstan(self):
        assert ind.ema_series([5.0] * 30, 10) == pytest.approx([5.0] * 21)

    def test_ema_last_sama_dengan_elemen_terakhir_deret(self):
        vals = [1, 3, 2, 5, 4, 6, 8, 7, 9, 10]
        assert ind.ema_last(vals, 4) == ind.ema_series(vals, 4)[-1]

    def test_rumus_manual(self):
        # period 2 -> k = 2/3. SMA awal (1+3)/2 = 2. Lalu 5*2/3 + 2*1/3 = 4.
        assert ind.ema_series([1, 3, 5], 2) == pytest.approx([2.0, 4.0])


class TestRsi:
    def test_data_kurang(self):
        assert ind.rsi(list(range(14)), 14) is None

    def test_naik_terus_100(self):
        assert ind.rsi(list(range(1, 40)), 14) == 100.0

    def test_turun_terus_mendekati_0(self):
        assert ind.rsi(list(range(40, 1, -1)), 14) == pytest.approx(0.0)

    def test_naik_turun_seimbang_sekitar_50(self):
        closes = [100 + (1 if i % 2 else -1) for i in range(60)]
        assert ind.rsi(closes, 14) == pytest.approx(50, abs=5)

    def test_selalu_di_rentang_0_100(self):
        import random
        rnd = random.Random(7)
        closes = [100.0]
        for _ in range(200):
            closes.append(closes[-1] * (1 + rnd.uniform(-0.03, 0.03)))
        assert 0 <= ind.rsi(closes, 14) <= 100


class TestAtr:
    def test_data_kurang(self):
        assert ind.atr(candles_from_closes([1.0] * 14), 14) is None
        assert ind.atr_sma(candles_from_closes([1.0] * 14), 14) is None

    def test_range_konstan(self):
        # Harga datar, high-low selalu 2 -> ATR = 2 di kedua rumus.
        c = candles_from_closes([100.0] * 40, spread=1.0)
        assert ind.atr(c, 14) == pytest.approx(2.0)
        assert ind.atr_sma(c, 14) == pytest.approx(2.0)

    def test_gap_ikut_dihitung(self):
        # True range harus memperhitungkan gap dari close sebelumnya.
        c = [[0, 10, 10, 10, 10, 1], [1, 20, 20, 20, 20, 1]]
        assert ind.atr(c, 1) == pytest.approx(10.0)

    def test_wilder_vs_sma_beda_setelah_lonjakan(self):
        # Satu lonjakan volatilitas lama: SMA 14 terakhir sudah melupakannya,
        # Wilder masih membawa sisanya. Ini beda yang dulu bikin backtest
        # dan bot live nggak sejalan.
        closes = [100.0] * 20 + [130.0, 100.0] + [100.0] * 30
        c = candles_from_closes(closes)
        assert ind.atr(c, 14) > ind.atr_sma(c, 14)
