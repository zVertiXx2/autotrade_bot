import pytest

from core import fees
from core.metrics import summarize


class TestFee:
    def test_limit_maker_market_taker(self):
        assert fees.entry_fee_pct('limit') == 0.02
        assert fees.entry_fee_pct('market') == 0.06
        assert fees.entry_fee_pct('limit', assume_maker=False) == 0.06

    def test_worst_fee_keluar_via_sl(self):
        assert fees.worst_fee_pct('limit') == pytest.approx(0.08)
        assert fees.worst_fee_pct('market') == pytest.approx(0.12)

    def test_round_trip_tp_lebih_murah_dari_sl(self):
        tp = fees.round_trip_fee(1000, 'market', 'take_profit')
        sl = fees.round_trip_fee(1000, 'market', 'stop_loss')
        assert tp == pytest.approx(0.8)   # 0.06% + 0.02%
        assert sl == pytest.approx(1.2)   # 0.06% + 0.06%


class TestNetRR:
    def test_fee_menurunkan_rr(self):
        # RR kotor 2.0 harus turun setelah fee.
        assert fees.net_rr(1.0, 2.0, 'market') < 2.0

    def test_rumus(self):
        # market: kalah = 1.0 + 0.12, menang = 2.0 - (0.06 + 0.02)
        assert fees.net_rr(1.0, 2.0, 'market') == pytest.approx(1.92 / 1.12)

    def test_sl_nol(self):
        assert fees.net_rr(0, 2.0, 'market', maker=0, taker=0) == 0.0


class TestLeverage:
    def test_sl_lebar_leverage_turun(self):
        assert fees.safe_leverage(0.5, 50) > fees.safe_leverage(3.0, 50)

    def test_dibatasi_max(self):
        assert fees.safe_leverage(0.1, 20) == 20

    def test_minimal_satu(self):
        assert fees.safe_leverage(0, 20) == 1
        assert fees.safe_leverage(-1, 20) == 1
        assert fees.safe_leverage(500, 20) == 1

    def test_sl_tetap_di_dalam_jarak_likuidasi(self):
        # Dengan leverage hasil fungsi, SL harus <= separuh jarak likuidasi.
        for sl in (0.7, 1.0, 1.5, 2.5, 3.5):
            lev = fees.safe_leverage(sl, 125)
            liq_distance = 100 / lev - 0.5
            assert sl <= liq_distance * 0.5 + 1e-9


class TestSizing:
    def test_kerugian_di_sl_sama_dengan_risiko(self):
        bal, risk, sl, fee = 1000, 0.5, 1.5, 0.12
        notional = fees.risk_notional(bal, risk, sl, fee)
        loss = notional * (sl + fee) / 100
        assert loss == pytest.approx(bal * risk / 100)

    def test_input_tidak_valid(self):
        assert fees.risk_notional(0, 0.5, 1, 0.1) == 0.0
        assert fees.risk_notional(1000, 0.5, 0, 0) == 0.0


class TestMetrics:
    def test_kosong(self):
        assert summarize([]) is None

    def test_angka_dasar(self):
        s = summarize([10, -5, -5, 10])
        assert s['win_rate'] == 50
        assert s['breakeven'] == pytest.approx(5 / 15 * 100)
        assert s['margin'] == pytest.approx(50 - 100 / 3)
        assert s['profit_factor'] == pytest.approx(2.0)
        assert s['total'] == 10

    def test_pnl_nol_dihitung_kalah(self):
        assert summarize([0, 5])['wins'] == 1

    def test_drawdown(self):
        s = summarize([100, -50, -50], start_balance=1000)
        assert s['max_drawdown_pct'] == pytest.approx(100 / 1100 * 100)
