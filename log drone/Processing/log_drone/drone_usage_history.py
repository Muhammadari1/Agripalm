"""
Drone Usage History — rekap Hari/Flight/Durasi per Nama/Lokasi Kerja/Kode
Drone per Bulan, dipakai untuk halaman "Rekap Pemakaian Drone".

SENGAJA pakai SQLite lokal (bukan tabel Postgres seperti drone_report_uploads
di sekitarnya) karena ini masih rekap SEMENTARA -- kalau nanti mau dipindah
jadi tabel Postgres beneran, tinggal export isi file .db ini.

Tiap upload yang selesai diproses (lihat drone_report_worker.py) menambah
SATU baris baru di sini (source='auto') -- bukan update/akumulasi baris lama
-- supaya riwayat tiap upload tetap tercatat apa adanya (histori, bukan
cuma angka akumulasi). Rekap per Nama/Bulan dihitung dengan SUM saat
ditampilkan (lihat get_recap_data()).
"""
import os
import sqlite3
from datetime import date

DB_PATH = os.environ.get(
    "AGRIPALM_DRONE_HISTORY_DB",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "drone_report_data", "drone_usage_history.db",
    ),
)

INDO_MONTHS = ["Januari", "Februari", "Maret", "April", "Mei", "Juni",
               "Juli", "Agustus", "September", "Oktober", "November", "Desember"]

# Data awal periode Mei-Juni 2026, dibaca manual dari rekap Excel yang sudah
# ada sebelumnya (screenshot dikirim user saat minta fitur ini) -- angka
# Hari/Flight/Durasi APA ADANYA dari sana, cek ulang & benarkan langsung di
# tabel `usage_history` (source='seed') kalau ada yang meleset.
_SEED_ROWS = [
    # (nama, lokasi_kerja, kode_drone, bulan, hari, flight, durasi_menit)
    ("Gilang Wicaksono", "Re-planting Mentaya", "NEO SQA12", "2026-05", 11, 37, 261),
    ("Gilang Wicaksono", "Re-planting Mentaya", "NEO SQA12", "2026-06", 11, 18, 139),
    ("Ahmad Fajar Aji", "Re-planting Pundu", "NEO SQA16", "2026-05", 19, 41, 250),
    ("Ahmad Fajar Aji", "Re-planting Pundu", "NEO SQA16", "2026-06", 20, 47, 337),
    ("Syaeful Arifin", "Region 1 & 2", "NEO SQA02", "2026-05", 5, 13, 102),
    ("Syaeful Arifin", "Region 1 & 2", "NEO SQA02", "2026-06", 12, 15, 148),
    ("Erwan Setiadi", "Region 3 & 4", "NEO SQA04", "2026-05", 3, 5, 20),
    ("Erwan Setiadi", "Region 3 & 4", "NEO SQA04", "2026-06", 2, 4, 20),
    ("Sofyan Assidiq", "Region 5", "NEO SQA09", "2026-05", 5, 18, 103),
    ("Sofyan Assidiq", "Region 5", "NEO SQA09", "2026-06", 6, 12, 73),
    ("Arman Ichwaizah", "Region 6", "NEO SQA10", "2026-05", 2, 3, 11),
    ("Arman Ichwaizah", "Region 6", "NEO SQA10", "2026-06", 4, 6, 24),
    ("Lukman A", "Region 7A & 7B", "NEO SQA06", "2026-05", 2, 5, 27),
    ("Lukman A", "Region 7A & 7B", "NEO SQA06", "2026-06", 5, 11, 96),
    ("Tutut Pujiarto", "Region 8A, 8B & 10", "NEO SQA07", "2026-05", 17, 54, 544),
    ("Tutut Pujiarto", "Region 8A, 8B & 10", "NEO SQA07", "2026-06", 13, 21, 196),
    ("Ahmad Karim", "Region Kalteng", "NEO SQA05", "2026-05", 4, 17, 126),
    ("Ahmad Karim", "Region Kalteng", "NEO SQA05", "2026-06", 3, 6, 25),
    ("Verry Kurnia S", "Region Kalbar", "NEO SQA11", "2026-05", 4, 8, 32),
    ("Verry Kurnia S", "Region Kalbar", "NEO SQA11", "2026-06", 3, 6, 42),
    ("Sony Widodo", "Region Kalteng", "NEO SQA15", "2026-05", 8, 16, 113),
    ("Sony Widodo", "Region Kalteng", "NEO SQA15", "2026-06", 12, 23, 182),
]


def _connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = _connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS usage_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nama TEXT NOT NULL,
                lokasi_kerja TEXT,
                kode_drone TEXT,
                bulan TEXT NOT NULL,
                hari INTEGER DEFAULT 0,
                flight INTEGER DEFAULT 0,
                durasi_menit REAL DEFAULT 0,
                source TEXT DEFAULT 'manual',
                upload_id INTEGER,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
    finally:
        conn.close()


def seed_if_empty():
    conn = _connect()
    try:
        count = conn.execute("SELECT COUNT(*) FROM usage_history WHERE source='seed'").fetchone()[0]
        if count > 0:
            return
        conn.executemany(
            "INSERT INTO usage_history (nama, lokasi_kerja, kode_drone, bulan, hari, flight, durasi_menit, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'seed')",
            _SEED_ROWS,
        )
        conn.commit()
        print(f"[DRONE HISTORY] Seed data {len(_SEED_ROWS)} baris (Mei-Juni 2026) dimasukkan ke {DB_PATH}")
    finally:
        conn.close()


def record_usage(nama, lokasi_kerja, kode_drone, bulan, hari, flight, durasi_menit, upload_id=None):
    """Dipanggil worker setelah 1 batch upload SELESAI diproses -- 1 baris baru,
    bukan update baris lama, supaya histori per-upload tetap terjaga."""
    if not nama or not bulan:
        return
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO usage_history (nama, lokasi_kerja, kode_drone, bulan, hari, flight, durasi_menit, source, upload_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'auto', ?)",
            (nama, lokasi_kerja, kode_drone, bulan, hari or 0, flight or 0, durasi_menit or 0, upload_id),
        )
        conn.commit()
    except Exception as e:
        print(f"[DRONE HISTORY] Gagal simpan riwayat: {e}")
    finally:
        conn.close()


def bulan_label(bulan_key):
    """'2026-05' -> 'Mei 2026'"""
    try:
        y, m = bulan_key.split("-")
        return f"{INDO_MONTHS[int(m) - 1]} {y}"
    except Exception:
        return bulan_key


def bulan_options(months_back=13, months_forward=1):
    """List (value, label) 'YYYY-MM' utk dropdown input Bulan di form upload,
    dari `months_back` bulan lalu s/d `months_forward` bulan depan dari sekarang."""
    today = date.today()
    idx = today.year * 12 + (today.month - 1)
    options = []
    for offset in range(months_back, -months_forward - 1, -1):
        total = idx - offset
        y, m = divmod(total, 12)
        m += 1
        key = f"{y:04d}-{m:02d}"
        options.append((key, bulan_label(key)))
    return options


def get_recap_data():
    """Rekap per Nama, pivot per Bulan. Return (bulan_list, entities):
      bulan_list: ['2026-05', '2026-06', ...] terurut kronologis
      entities: list of dict {"nama", "lokasi_kerja", "kode_drone",
                "bulan": {bulan: {"hari","flight","durasi"}}, "average": {...},
                "variasi": {...} | None}
    """
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT nama, lokasi_kerja, kode_drone, bulan, hari, flight, durasi_menit "
            "FROM usage_history ORDER BY nama, bulan, created_at"
        ).fetchall()
    finally:
        conn.close()

    bulan_set = sorted({r[3] for r in rows})
    entities = {}
    order = []
    for nama, lokasi, drone, bulan, hari, flight, durasi in rows:
        if nama not in entities:
            entities[nama] = {"nama": nama, "lokasi_kerja": lokasi, "kode_drone": drone, "bulan": {}}
            order.append(nama)
        e = entities[nama]
        b = e["bulan"].setdefault(bulan, {"hari": 0, "flight": 0, "durasi": 0})
        b["hari"] += hari or 0
        b["flight"] += flight or 0
        b["durasi"] += durasi or 0
        # Lokasi Kerja/Kode Drone dipakai dari baris paling baru (rows sudah urut ASC per nama)
        e["lokasi_kerja"] = lokasi or e["lokasi_kerja"]
        e["kode_drone"] = drone or e["kode_drone"]

    result = []
    for nama in order:
        e = entities[nama]
        months_present = [m for m in bulan_set if m in e["bulan"]]
        n = len(months_present)
        avg = {"hari": 0, "flight": 0, "durasi": 0}
        if n:
            avg = {
                "hari": sum(e["bulan"][m]["hari"] for m in months_present) / n,
                "flight": sum(e["bulan"][m]["flight"] for m in months_present) / n,
                "durasi": sum(e["bulan"][m]["durasi"] for m in months_present) / n,
            }
        variasi = None
        if n >= 2:
            m_last, m_prev = months_present[-1], months_present[-2]
            variasi = {k: e["bulan"][m_last][k] - e["bulan"][m_prev][k] for k in ("hari", "flight", "durasi")}
        e["average"] = avg
        e["variasi"] = variasi
        result.append(e)
    return bulan_set, result
