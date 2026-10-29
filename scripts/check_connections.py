"""Cek koneksi ke Bitget dan Anthropic sebelum menjalankan bot.

Jalankan dari root repo:  python scripts/check_connections.py
Tidak pernah mencetak isi API key — cuma status sukses/gagal.
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv()

REQUIRED = ["BITGET_API_KEY", "BITGET_API_SECRET", "BITGET_PASSPHRASE", "ANTHROPIC_API_KEY"]


def check_env():
    missing = [k for k in REQUIRED if not os.getenv(k)]
    if missing:
        print(f"❌ Variabel .env belum diisi: {', '.join(missing)}")
        print("   Salin .env.example jadi .env lalu isi nilainya.")
        return False
    print("✅ Semua variabel .env terisi")
    return True


def check_bitget():
    import ccxt
    exchange = ccxt.bitget({
        "apiKey": os.getenv("BITGET_API_KEY"),
        "secret": os.getenv("BITGET_API_SECRET"),
        "password": os.getenv("BITGET_PASSPHRASE"),
        "enableRateLimit": True,
        "timeout": 20000,
        "options": {"defaultType": "swap"},
    })
    try:
        balance = exchange.fetch_balance()
        print(f"✅ Bitget terhubung — saldo USDT tersedia: {balance['USDT']['free']}")
        return True
    except ccxt.AuthenticationError as e:
        print(f"❌ Bitget: autentikasi gagal (cek key/secret/passphrase) — {type(e).__name__}")
    except Exception as e:
        print(f"❌ Bitget: {type(e).__name__}: {e}")
    return False


def check_anthropic():
    from anthropic import Anthropic
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"), timeout=60.0)
    try:
        msg = client.messages.create(
            model=os.getenv("CHECK_MODEL", "claude-sonnet-5"),
            max_tokens=1000,
            messages=[{"role": "user", "content": "Tes koneksi. Balas singkat: 'Siap'."}],
        )
        text = " ".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        print(f"✅ Anthropic terhubung — {msg.model}: {text.strip()[:60]}")
        return True
    except Exception as e:
        print(f"❌ Anthropic: {type(e).__name__}: {e}")
    return False


if __name__ == "__main__":
    ok = check_env()
    if ok:
        ok = check_bitget() & check_anthropic()
    sys.exit(0 if ok else 1)
