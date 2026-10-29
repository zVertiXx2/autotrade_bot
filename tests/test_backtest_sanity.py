"""Uji kejujuran backtest: tanpa sinyal, strategi harusnya rugi kira-kira sebesar fee.

Kalau backtest bisa "untung" di random walk, berarti ada lookahead bias
atau simulasi fill yang terlalu optimis.
"""

import random

import pytest

import backtest


def random_walk(n, seed, start=100.0, vol=0.004):
    rnd = random.Random(seed)
    out, px, t = [], start, 0
    for _ in range(n):
        o = px
        c = o * (1 + rnd.gauss(0, vol))
        h = max(o, c) * (1 + abs(rnd.gauss(0, vol / 2)))
        l = min(o, c) * (1 - abs(rnd.gauss(0, vol / 2)))
        out.append([t, o, h, l, c, 1000.0])
        px, t = c, t + 300_000
    return out


@pytest.fixture
def synthetic(monkeypatch):
    data = {f'SYN{i}/USDT:USDT': random_walk(3000, seed=i) for i in range(6)}
    monkeypatch.setattr(backtest, 'SYMBOLS', list(data))
    monkeypatch.setattr(backtest, 'load', lambda sym: data[sym])
    return data


def test_random_walk_tidak_menghasilkan_edge(synthetic):
    res = backtest.run_backtest()
    m = backtest.metrics(res)
    if m is None or m['n'] < 20:
        pytest.skip('trade terlalu sedikit di data sintetis')
    # Tanpa informasi arah, profit factor harus di bawah 1.3 (fee bikin rugi).
    assert m['pf'] < 1.3
