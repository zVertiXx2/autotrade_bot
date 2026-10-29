"""
Teacher Bot — CLI buat "ngajarin" AI trading dari teks dan gambar.

Teks buku/artikel atau screenshot chart/flowchart trading plan dikirim ke
Claude, diringkas jadi "Hukum Trading", lalu ditambahkan ke file pengetahuan
(default: knowledge.md).

Catatan: bot_scalp.py BELUM membaca file ini. Pipeline-nya sengaja masih
terpisah sampai dry run punya cukup data pembanding (lihat README).

Jalankan:  python teacher_bot.py
"""

import base64
import os

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

AI_MODEL = os.getenv("TEACHER_MODEL", "claude-sonnet-5")
AI_MAX_TOKENS = 3000   # token "thinking" ikut makan jatah ini, jangan terlalu kecil
KNOWLEDGE_FILE = os.getenv("KNOWLEDGE_FILE", "knowledge.md")

MEDIA_TYPES = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "gif": "image/gif", "webp": "image/webp",
}

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"), timeout=90.0)


def load_knowledge():
    """Memuat pengetahuan yang sudah tersimpan."""
    if os.path.exists(KNOWLEDGE_FILE):
        with open(KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip() or "Belum ada pelajaran."
    return "Belum ada pelajaran."


def save_knowledge(new_knowledge):
    """Menambahkan pengetahuan baru ke file."""
    with open(KNOWLEDGE_FILE, "a", encoding="utf-8") as f:
        f.write("\n\n" + new_knowledge.strip())
    print(f"✅ Pengetahuan baru disimpan ke {KNOWLEDGE_FILE}")


def _ask(content):
    message = client.messages.create(
        model=AI_MODEL,
        max_tokens=AI_MAX_TOKENS,
        messages=[{"role": "user", "content": content}],
    )
    # Respons bisa berisi blok "thinking" sebelum teks jawaban — ambil teksnya saja.
    return "\n".join(b.text for b in message.content if getattr(b, "type", "") == "text").strip()


def teach_with_image(image_path, instruction):
    """Kirim screenshot/flowchart ke AI untuk dipelajari."""
    ext = image_path.rsplit(".", 1)[-1].lower()
    media_type = MEDIA_TYPES.get(ext)
    if not media_type:
        print(f"❌ Format .{ext} nggak didukung. Pakai: {', '.join(MEDIA_TYPES)}")
        return
    with open(image_path, "rb") as f:
        image_data = base64.b64encode(f.read()).decode("utf-8")

    text = _ask([
        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_data}},
        {"type": "text", "text": (
            f"Instruksi dari Guru: {instruction}\n\n"
            "Analisis gambar chart/flowchart trader ini. Ekstrak aturan main, pola yang "
            "dicari, dan manajemen risikonya. Tulis dalam poin-poin tegas sebagai "
            "'Hukum Trading' baru."
        )},
    ])
    print("\n🧠 Hasil analisis:\n" + text)
    if text:
        save_knowledge(text)


def teach_with_text(text):
    """Kirim teks buku/rangkuman untuk dipelajari."""
    result = _ask(f"Pelajari teks dari buku trader ini dan rangkum menjadi 'Hukum Trading' yang tegas:\n\n{text}")
    print("\n🧠 Hasil analisis:\n" + result)
    if result:
        save_knowledge(result)


if __name__ == "__main__":
    print("📚 TEACHER BOT - Latih AI Trading")
    print("1. Kirim Teks (Buku/Artikel)")
    print("2. Kirim Gambar (Screenshot/Flowchart)")
    print("3. Lihat Pengetahuan Saat Ini")
    print("4. Reset Pengetahuan")

    while True:
        choice = input("\nPilih menu (1/2/3/4) atau 'q' untuk keluar: ").strip()

        if choice == "1":
            teach_with_text(input("Tempel teks buku/artikel di sini: "))
        elif choice == "2":
            path = input("Path file gambar: ").strip().strip('"')
            if os.path.exists(path):
                teach_with_image(path, input("Instruksi untuk gambar ini: "))
            else:
                print("❌ File tidak ditemukan.")
        elif choice == "3":
            print("\n=== PENGETAHUAN SAAT INI ===")
            print(load_knowledge())
        elif choice == "4":
            if input("Yakin hapus semua pengetahuan? (ketik 'ya'): ").strip().lower() == "ya":
                open(KNOWLEDGE_FILE, "w", encoding="utf-8").close()
                print("🧹 Pengetahuan dihapus.")
        elif choice.lower() == "q":
            break
