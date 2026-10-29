"""Metrik evaluasi strategi.

Net profit doang menyesatkan di sampel kecil. Yang dipakai di proyek ini:

- breakeven win rate = rata2 kalah / (rata2 kalah + rata2 menang)
  -> win rate minimum supaya strategi impas, dari bentuk menang/kalahnya sendiri
- margin = win rate - breakeven win rate (poin persen). Positif = ada edge.
- profit factor = total menang / total kalah

Trade dengan PnL <= 0 dihitung kalah (sama seperti backtest.py).
"""

from __future__ import annotations

from typing import Iterable, Optional


def summarize(pnls: Iterable[float], start_balance: Optional[float] = None) -> Optional[dict]:
    """Ringkas daftar PnL per trade (urut waktu). None kalau kosong."""
    pnls = list(pnls)
    if not pnls:
        return None
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    win_rate = len(wins) / len(pnls) * 100
    breakeven = avg_loss / (avg_loss + avg_win) * 100 if (avg_loss + avg_win) else 0.0
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    out = {
        'trades': len(pnls),
        'wins': len(wins),
        'win_rate': win_rate,
        'breakeven': breakeven,
        'margin': win_rate - breakeven,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'profit_factor': (gross_win / gross_loss) if gross_loss else float('inf'),
        'total': sum(pnls),
        'expectancy': sum(pnls) / len(pnls),
    }

    if start_balance:
        bal = peak = start_balance
        max_dd = 0.0
        for p in pnls:
            bal += p
            peak = max(peak, bal)
            max_dd = max(max_dd, (peak - bal) / peak * 100)
        out['max_drawdown_pct'] = max_dd
    return out
