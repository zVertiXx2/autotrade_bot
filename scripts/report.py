"""Laporan performa dari journal bot (dry run atau live).

Jalankan dari root repo:
    python scripts/report.py                       # journal_scalp.jsonl
    python scripts/report.py journal_lain.jsonl    # file lain
    python scripts/report.py --by-symbol           # rincian per pair

Ingat: di bawah ~30 trade, angka di sini belum bisa disimpulkan apa pun.
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.metrics import summarize  # noqa: E402

CLOSE_EVENTS = {'dry_run_close', 'close', 'position_closed'}
MIN_SAMPLE = 30


def load_closes(path):
    closes, events, bad = [], Counter(), 0
    with open(path, encoding='utf-8') as f:
        for line in f:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            ev = rec.get('event', '?')
            events[ev] += 1
            if ev in CLOSE_EVENTS and isinstance(rec.get('pnl'), (int, float)):
                closes.append(rec)
    return closes, events, bad


def fmt(s):
    pf = '∞' if s['profit_factor'] == float('inf') else f"{s['profit_factor']:.2f}"
    return (f"  trade            : {s['trades']}\n"
            f"  win rate         : {s['win_rate']:.1f}%  ({s['wins']} menang)\n"
            f"  breakeven        : {s['breakeven']:.1f}%\n"
            f"  margin           : {s['margin']:+.1f} poin   <- angka utama, positif = ada edge\n"
            f"  profit factor    : {pf}\n"
            f"  rata2 menang     : +{s['avg_win']:.2f} USDT\n"
            f"  rata2 kalah      : -{s['avg_loss']:.2f} USDT\n"
            f"  ekspektasi/trade : {s['expectancy']:+.3f} USDT\n"
            f"  total PnL        : {s['total']:+.2f} USDT")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('journal', nargs='?', default='journal_scalp.jsonl')
    ap.add_argument('--by-symbol', action='store_true')
    args = ap.parse_args()

    if not os.path.exists(args.journal):
        sys.exit(f"File {args.journal} nggak ada.")

    closes, events, bad = load_closes(args.journal)
    print(f"Journal: {args.journal}")
    if closes:
        print(f"Periode: {closes[0].get('ts', '?')[:16]}  ->  {closes[-1].get('ts', '?')[:16]} UTC")
    print("Event  : " + ", ".join(f"{k} {v}" for k, v in events.most_common(6)))
    if bad:
        print(f"Baris rusak dilewati: {bad}")

    s = summarize([c['pnl'] for c in closes])
    if not s:
        print("\nBelum ada trade yang ditutup.")
        return
    print("\n" + fmt(s))

    reasons = Counter(c.get('reason', '?') for c in closes)
    print("\n  alasan tutup     : " + ", ".join(f"{k} {v}" for k, v in reasons.most_common()))

    if args.by_symbol:
        groups = defaultdict(list)
        for c in closes:
            groups[c.get('symbol', '?')].append(c['pnl'])
        print("\n  per pair:")
        for sym, pnls in sorted(groups.items(), key=lambda kv: sum(kv[1])):
            print(f"    {sym.split('/')[0]:10} {len(pnls):>3} trade  PnL {sum(pnls):+8.2f}")

    if s['trades'] < MIN_SAMPLE:
        print(f"\n⚠ Baru {s['trades']} trade — butuh minimal {MIN_SAMPLE} sebelum menyimpulkan apa pun.")


if __name__ == '__main__':
    main()
