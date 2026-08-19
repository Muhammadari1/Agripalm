"""
Agripalm Vision — License & Auth API Server
=============================================
Jalankan di server BGAWKS-GIS6:
    python api_server.py

Endpoints:
    POST /api/auth/token          → Dapat JWT token (app secret)
    POST /api/license/verify      → Verifikasi lisensi + MAC
    POST /api/license/activate    → Simpan MAC ke DB
    POST /api/auth/ldap           → Validasi LDAP
    POST /api/auth/verify-user    → Cek username di DB
    POST /api/log/login           → Log login
    POST /api/log/logout          → Log logout
    POST /api/log/activity        → Increment aktivitas
    POST /api/user/last-open      → Update last_open
    POST /api/location            → Get location (proxy ipinfo)
"""

import os
import re
import socket
import shutil
import subprocess
import json
import glob
import threading
import uuid
import zipfile
import tempfile
import secrets
from datetime import datetime, timedelta
from functools import wraps
from werkzeug.utils import secure_filename

from flask import Flask, request, jsonify, render_template, redirect, url_for, Response, session, abort
import jwt
import psycopg2
from psycopg2 import Error

app = Flask(__name__)


def _load_or_create_session_secret():
    """Kunci penanda-tangan cookie session dashboard.

    Sebelumnya nilai ini punya default HARDCODED di source — artinya siapa pun yang
    bisa baca file ini (atau salinan/backup-nya) bisa memalsukan cookie session dan
    masuk sebagai admin mana pun tanpa perlu password. Sekarang: pakai env var kalau
    ada; kalau tidak, generate acak SEKALI lalu simpan ke file lokal (mode dibatasi)
    supaya nilainya tetap konsisten antar-restart — kalau di-random tiap start, semua
    admin akan ke-logout paksa tiap kali service restart.

    Catatan sengaja TIDAK diterapkan ke JWT_SECRET/APP_SECRET di bawah: dua nilai itu
    dipakai bersama oleh aplikasi desktop Agripalm Vision yang sudah ter-deploy ke
    banyak laptop — mengubahnya sepihak di server akan langsung memutus login semua
    client itu. Rotasi keduanya harus barengan dengan update client (lihat peringatan
    startup di bawah)."""
    env_secret = os.environ.get("FLASK_SECRET_KEY")
    if env_secret:
        return env_secret
    secret_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".flask_session_secret")
    try:
        if os.path.isfile(secret_path):
            with open(secret_path, "r", encoding="utf-8") as f:
                saved = f.read().strip()
            if saved:
                return saved
        generated = secrets.token_urlsafe(64)
        with open(secret_path, "w", encoding="utf-8") as f:
            f.write(generated)
        try:
            os.chmod(secret_path, 0o600)
        except Exception:
            pass  # Windows: ACL default folder service sudah cukup ketat
        print(f"[SECURITY] FLASK_SECRET_KEY belum di-set — secret acak dibuat & disimpan di {secret_path}")
        return generated
    except Exception as e:
        # Jangan sampai server gagal start cuma gara-gara file secret tidak bisa ditulis.
        print(f"[SECURITY WARNING] Gagal simpan session secret ({e}) — pakai secret acak "
              f"sementara. Semua admin akan perlu login ulang setiap service restart.")
        return secrets.token_urlsafe(64)


app.secret_key = _load_or_create_session_secret()

# ── Config ────────────────────────────────────────────────────────────────────
# Ganti secret ini di production! Jangan pakai default.
JWT_SECRET = os.environ.get("AGRIPALM_JWT_SECRET", "AgripalmVision-JWT-Secret-2026-GPN")
JWT_EXPIRY_HOURS = int(os.environ.get("AGRIPALM_JWT_EXPIRY_HOURS", "24"))
APP_SECRET = os.environ.get("AGRIPALM_APP_SECRET", "AgripalmVision-App-Secret-BGA-2026")

# Set AGRIPALM_HTTPS=1 kalau dashboard sudah diakses lewat https:// (lihat blok TLS di
# nginx/agripalm.conf). Kalau ON, cookie session ditandai Secure sehingga browser TIDAK
# akan pernah mengirimkannya lewat koneksi http polos yang bisa disadap.
HTTPS_ENABLED = os.environ.get("AGRIPALM_HTTPS", "0").strip().lower() in ("1", "true", "yes", "on")
SESSION_HOURS = float(os.environ.get("AGRIPALM_SESSION_HOURS", "12"))

app.config.update(
    # HttpOnly: cookie tidak bisa dibaca JavaScript (membatasi dampak kalau suatu saat
    # ada celah XSS). SameSite=Lax: browser tidak mengirim cookie ini pada request POST
    # lintas-situs — ini mitigasi utama CSRF untuk semua form dashboard, tanpa perlu
    # menyisipkan token ke puluhan form yang sudah ada (risiko ada form kelewat).
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=HTTPS_ENABLED,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=SESSION_HOURS),
    # Sliding window: selama admin masih aktif meng-klik, masa berlaku diperpanjang —
    # yang ke-logout hanya sesi yang benar-benar ditinggal diam (mis. browser lupa
    # ditutup di komputer bersama).
    SESSION_REFRESH_EACH_REQUEST=True,
    MAX_CONTENT_LENGTH=None,  # upload citra batch bisa sangat besar — jangan dibatasi di sini
)

DASHBOARD_PREFIX_DEFAULT = os.environ.get("AGRIPALM_DASHBOARD_PREFIX", "/gpn-admin")
DASHBOARD_SEED_ADMIN = os.environ.get("AGRIPALM_SEED_ADMIN", "muhammad.aji")

# Menu yang bisa dipilih untuk role "user" (di luar ini selalu admin-only: admins, settings)
DASHBOARD_MENUS = ["overview", "users", "licenses", "logs", "batch", "custom", "tbm", "tbm_poor_class_tool", "treecounting", "homogenitas", "homogenitas_rekap", "rekap_data", "raw", "drone", "database", "thematic_mapping"]
SUPER_ADMIN = "muhammad.aji"

DB_HOST = os.environ.get("AGRIPALM_DB_HOST", "BGAWKS-GIS6")
DB_USER = os.environ.get("AGRIPALM_DB_USER", "muhammad.aji")
DB_PASS = os.environ.get("AGRIPALM_DB_PASS", "Ari230498")
DB_NAME = os.environ.get("AGRIPALM_DB_NAME", "gpnserver")

LDAP_HOST = os.environ.get("AGRIPALM_LDAP_HOST", "ldap.bumitama.com")
LDAP_PORT = int(os.environ.get("AGRIPALM_LDAP_PORT", "389"))


# ── Keamanan: peringatan secret default ───────────────────────────────────────
def _warn_default_secrets():
    """Cetak peringatan JELAS di log kalau server masih jalan pakai secret bawaan
    yang tertulis di source code. Sengaja cuma memperingatkan (bukan menolak start
    atau mengacak sendiri) karena JWT/APP secret dipakai bersama aplikasi desktop
    yang sudah ter-deploy — mengubahnya sepihak akan memutus login semua client."""
    defaults = {
        "AGRIPALM_JWT_SECRET": (JWT_SECRET, "AgripalmVision-JWT-Secret-2026-GPN"),
        "AGRIPALM_APP_SECRET": (APP_SECRET, "AgripalmVision-App-Secret-BGA-2026"),
        "AGRIPALM_DB_PASS": (DB_PASS, "Ari230498"),
    }
    masih_default = [name for name, (current, bawaan) in defaults.items() if current == bawaan]
    if masih_default:
        print("=" * 78)
        print("[SECURITY WARNING] Masih memakai nilai BAWAAN yang tertulis di source code")
        print("                   untuk: " + ", ".join(masih_default))
        print("                   Siapa pun yang bisa membaca api_server.py (atau backup-nya)")
        print("                   tahu nilai ini. Set lewat env var service, contoh:")
        print('                     nssm set AgripalmAPIServer AppEnvironmentExtra AGRIPALM_JWT_SECRET=<acak>')
        print("                   CATATAN: JWT/APP secret dipakai juga oleh aplikasi desktop —")
        print("                   ganti keduanya BERSAMAAN dengan update client, jangan sendiri.")
        print("=" * 78)
    if not HTTPS_ENABLED:
        print("[SECURITY WARNING] AGRIPALM_HTTPS belum di-set — cookie session dikirim tanpa "
              "flag Secure. Aktifkan TLS di nginx (lihat nginx/agripalm.conf) lalu set "
              "AGRIPALM_HTTPS=1 supaya cookie tidak pernah lewat http polos.")


_warn_default_secrets()


def _client_ip():
    """IP asli client. Karena dashboard berjalan di belakang reverse proxy nginx,
    request.remote_addr SELALU 127.0.0.1 — IP sebenarnya ada di X-Forwarded-For yang
    di-set nginx (lihat nginx/agripalm.conf). Ambil entri paling kiri (client asli)."""
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "-"


# ── Keamanan: pembatasan percobaan login (anti brute-force) ───────────────────
# Disimpan di memori proses ini saja (bukan DB) — cukup untuk memperlambat tebak
# password otomatis, dan otomatis bersih saat service restart. Tidak perlu library
# tambahan supaya tidak menambah dependency baru ke requirements.txt.
LOGIN_MAX_ATTEMPTS = int(os.environ.get("AGRIPALM_LOGIN_MAX_ATTEMPTS", "8"))
LOGIN_LOCKOUT_MINUTES = float(os.environ.get("AGRIPALM_LOGIN_LOCKOUT_MINUTES", "10"))
_login_attempts = {}
_login_attempts_lock = threading.Lock()


def _login_throttle_status(key):
    """Return (boleh_coba, sisa_detik_lockout)."""
    with _login_attempts_lock:
        record = _login_attempts.get(key)
        if not record:
            return True, 0
        gagal, terakhir = record
        if gagal < LOGIN_MAX_ATTEMPTS:
            return True, 0
        sisa = (terakhir + timedelta(minutes=LOGIN_LOCKOUT_MINUTES) - datetime.now()).total_seconds()
        if sisa <= 0:
            _login_attempts.pop(key, None)
            return True, 0
        return False, int(sisa)


def _login_record_failure(key):
    with _login_attempts_lock:
        gagal, _ = _login_attempts.get(key, (0, datetime.now()))
        _login_attempts[key] = (gagal + 1, datetime.now())


def _login_record_success(key):
    with _login_attempts_lock:
        _login_attempts.pop(key, None)


@app.after_request
def _security_headers(resp):
    """Header pengaman standar untuk SEMUA response.

    Sengaja TIDAK memasang Content-Security-Policy yang ketat: beberapa halaman
    (batch_analyze, drone_log_detail, custom_batch_analyze) masih memuat Leaflet dari
    unpkg.com dan tile peta dari mt1.google.com — CSP ketat akan mematikan peta di
    halaman-halaman itu. Yang dipasang di sini semuanya aman terhadap fitur yang ada."""
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    if HTTPS_ENABLED:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp


AGRIPALM_INSTALL_PATH = os.environ.get("AGRIPALM_INSTALL_PATH", r"C:\Program Files\Agripalm Vision")

BATCH_ROOT = os.environ.get("AGRIPALM_BATCH_ROOT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "batch_data"))
BATCH_INCOMING = os.path.join(BATCH_ROOT, "incoming")
BATCH_OUTPUT_DEFAULT = os.path.join(BATCH_ROOT, "output")
os.makedirs(BATCH_INCOMING, exist_ok=True)
os.makedirs(BATCH_OUTPUT_DEFAULT, exist_ok=True)
ALLOWED_BATCH_EXTENSIONS = {".tif", ".tiff", ".ecw"}

# Deteksi Custom — folder TERPISAH dari Batch Deteksi TM (batch_data/) di atas,
# supaya model & hasil deteksi custom tidak tercampur dengan pipeline TM.
CUSTOM_ROOT = os.environ.get("AGRIPALM_CUSTOM_ROOT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "custom_batch_data"))
CUSTOM_INCOMING = os.path.join(CUSTOM_ROOT, "incoming")
CUSTOM_MODELS = os.path.join(CUSTOM_ROOT, "models")
CUSTOM_OUTPUT_DEFAULT = os.path.join(CUSTOM_ROOT, "output")
os.makedirs(CUSTOM_INCOMING, exist_ok=True)
os.makedirs(CUSTOM_MODELS, exist_ok=True)
os.makedirs(CUSTOM_OUTPUT_DEFAULT, exist_ok=True)
ALLOWED_MODEL_EXTENSIONS = {".pt"}

# Deteksi TBM — sama seperti Deteksi Custom (satu tabel/worker: custom_batch_jobs),
# tapi modelnya TETAP (bukan upload user) & tidak ditampilkan ke user, mirip Deteksi TM.
TBM_MODEL_PATH = os.environ.get(
    "AGRIPALM_TBM_MODEL_PATH",
    os.path.join(AGRIPALM_INSTALL_PATH, "geoprocessing", "Immature", "immature.pt"),
)

# TreeCounting — sama seperti Custom Detection (splitter estate opsional, dsb), tapi
# modelnya TETAP (bukan upload user) & tidak ditampilkan ke user, mirip Deteksi TBM.
TREECOUNTING_MODEL_PATH = os.environ.get(
    "AGRIPALM_TREECOUNTING_MODEL_PATH",
    os.path.join(AGRIPALM_INSTALL_PATH, "geoprocessing", "nutripalm", "Sgpar", "treecounting.pt"),
)

# Homogenitas Kanopi — logika klasifikasinya diport dari desktop app (SAHI.py,
# tool_tag="homogenitas"), TAPI model deteksinya SENGAJA beda dari desktop.
# Desktop app aslinya memakai immature.pt (2 kelas: TBM Sakit/TBM Sehat, model yang
# sama dengan Deteksi TBM). Di web ini dipakai treecounting.pt (1 kelas "Tanam KS",
# model yang sama dengan menu TreeCounting) — divalidasi (jumlah/nama kelas dicek
# lewat ultralytics.YOLO) atas permintaan user: yang diinginkan cuma deteksi kanopi
# murni, tanpa embel-embel kelas kesehatan. Aman dipakai karena klasifikasi
# homogenitas (_classify_canopy_homogeneity di custom_batch_worker.py) hanya membaca
# kolom radius/DIAMETER hasil deteksi, tidak pernah membaca class_name/class_id sama
# sekali — jadi jumlah kelas model sumbernya tidak memengaruhi cara hitungnya.
# Konsekuensi: hasil web TIDAK bisa lagi dibandingkan apple-to-apple dengan desktop
# app (beda model sumber) — ini disetujui user, bukan kealpaan.
HOMOGENITAS_MODEL_PATH = os.environ.get("AGRIPALM_HOMOGENITAS_MODEL_PATH", TREECOUNTING_MODEL_PATH)

# Kelas hasil klasifikasi kanopi (bukan kelas mentah dari immature.pt) — inilah yang
# ditampilkan di halaman Analyze, karena yang dilihat user adalah tingkat homogenitas
# kanopi, bukan kelas deteksi mentahnya. Urutan & warna: baik → sedang → bermasalah.
HOMOGENITAS_CLASSES = ["well canopy", "normal canopy", "abnormal canopy"]
HOMOGENITAS_CLASS_STYLES = {
    "well canopy": "#27ae60",
    "normal canopy": "#f1c40f",
    "abnormal canopy": "#e74c3c",
}

# Deteksi RAW Foto — foto drone biasa (PNG/JPG/TIF, umumnya tidak berkoordinat presisi),
# model TETAP (pakai model TM yang sama), tidak ada splitter/tile besar.
RAW_ROOT = os.environ.get("AGRIPALM_RAW_ROOT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "raw_photo_data"))
RAW_INCOMING = os.path.join(RAW_ROOT, "incoming")
os.makedirs(RAW_INCOMING, exist_ok=True)
ALLOWED_RAW_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}

# Automatic Thematic Mapping (ATM) — Excel (Blok_ID + kolom tematik) di-join ke
# poligon blok Aresta.shp (lihat Processing/thematic_mapping/), hasilnya peta
# tematik yang bisa distyle & di-print. Sesi disimpan permanen (thematic_mapping_sessions)
# supaya bisa dibuka/diedit lagi, beda dari batch/custom yang pakai job-queue+worker —
# di sini prosesnya ringan (parse Excel + join atribut, bukan GPU), jadi sinkron saja.
THEMATIC_ROOT = os.environ.get("AGRIPALM_THEMATIC_ROOT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "thematic_mapping_data"))
THEMATIC_INCOMING = os.path.join(THEMATIC_ROOT, "incoming")
THEMATIC_WORK = os.path.join(THEMATIC_ROOT, "work")
os.makedirs(THEMATIC_INCOMING, exist_ok=True)
os.makedirs(THEMATIC_WORK, exist_ok=True)
ALLOWED_THEMATIC_EXTENSIONS = {".xlsx", ".xls"}

# Cache token sementara buat /thematic/print-frame (halaman cetak yang di-render ulang
# oleh Chromium headless-nya Playwright lalu di-screenshot server-side -- lihat
# dashboard_thematic_export_png()). Bukan tabel DB, sengaja cuma hidup di memori
# proses ini beberapa detik per export (dipakai sekali lalu langsung dihapus) --
# volume rendah (1 export = 1 klik user), tidak butuh persistensi.
_PRINT_EXPORT_CACHE = {}
_PRINT_EXPORT_TTL_SECONDS = 60


def _print_export_put(payload):
    now = datetime.now().timestamp()
    for k in [k for k, (_, exp) in _PRINT_EXPORT_CACHE.items() if exp < now]:
        del _PRINT_EXPORT_CACHE[k]
    token = uuid.uuid4().hex
    _PRINT_EXPORT_CACHE[token] = (payload, now + _PRINT_EXPORT_TTL_SECONDS)
    return token


def _print_export_get(token):
    entry = _PRINT_EXPORT_CACHE.get(token)
    if not entry:
        return None
    payload, exp = entry
    if exp < datetime.now().timestamp():
        del _PRINT_EXPORT_CACHE[token]
        return None
    return payload

# Log Drone — flight log DJI (.txt), diproses lewat app.opendronelog.com (Playwright,
# lihat Processing/log_drone/). 1 upload bisa banyak file .txt sekaligus, semuanya
# digabung jadi SATU rekap (drone_report_uploads = batch header + hasil gabungan,
# drone_report_files = 1 baris per file, tracking status parsing tiap file).
DRONE_ROOT = os.environ.get("AGRIPALM_DRONE_ROOT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "drone_report_data"))
DRONE_INCOMING = os.path.join(DRONE_ROOT, "incoming")
os.makedirs(DRONE_INCOMING, exist_ok=True)
ALLOWED_DRONE_EXTENSIONS = {".txt"}

# 2 server berbagi 1 DB tapi TIDAK berbagi disk -- hasil Log Drone (gpx/xlsx/shp/
# peta/pptx) tersimpan lokal di server yang worker-nya kebetulan memproses batch itu
# (lihat kolom worker_host di drone_report_uploads). Supaya user tidak perlu tahu/
# nebak server mana yang punya filenya, kalau request masuk ke server yang BUKAN
# pemroses, kita
# redirect otomatis ke server yang benar -- perlu tahu alamat server satunya, isi
# lewat env var ini (URL dasar server SATUNYA, misal http://agripalm2.bumitama).
# Kosongkan/tidak usah diisi kalau cuma 1 server, atau kalau belum sempat diatur
# (pesan error di halaman akan tetap jelas menyebut server mana yang harus dipakai).
THIS_HOSTNAME = socket.gethostname()
AGRIPALM_PEER_URL = os.environ.get("AGRIPALM_PEER_URL", "").rstrip("/")

try:
    from ldap3 import Server, Connection, ALL
    LDAP_AVAILABLE = True
except ImportError:
    LDAP_AVAILABLE = False
    print("WARNING: ldap3 not installed. LDAP endpoints will return error.")


# ── Database Helper ───────────────────────────────────────────────────────────
def get_db():
    try:
        conn = psycopg2.connect(
            host=DB_HOST, user=DB_USER, password=DB_PASS, database=DB_NAME
        )
        return conn
    except Error as e:
        print(f"[DB ERROR] {e}")
        return None


def _ensure_admin_table():
    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS dashboard_admins (
                username VARCHAR(100) PRIMARY KEY,
                added_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # Migrasi aman untuk tabel yang sudah ada dari sebelumnya (role & filter per user)
        cur.execute("ALTER TABLE dashboard_admins ADD COLUMN IF NOT EXISTS role VARCHAR(20) DEFAULT 'admin'")
        cur.execute("ALTER TABLE dashboard_admins ADD COLUMN IF NOT EXISTS allowed_menus TEXT")
        cur.execute("ALTER TABLE dashboard_admins ADD COLUMN IF NOT EXISTS allowed_estates TEXT DEFAULT 'ALL'")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS dashboard_settings (
                key VARCHAR(100) PRIMARY KEY,
                value TEXT
            )
        """)
        cur.execute("SELECT COUNT(*) FROM dashboard_admins")
        if cur.fetchone()[0] == 0:
            cur.execute(
                "INSERT INTO dashboard_admins (username, role, allowed_estates) "
                "VALUES (%s, 'admin', 'ALL') ON CONFLICT DO NOTHING",
                (DASHBOARD_SEED_ADMIN,))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"[ADMIN TABLE] {e}")


def _get_admin_list():
    conn = get_db()
    if not conn:
        return [DASHBOARD_SEED_ADMIN]
    try:
        cur = conn.cursor()
        cur.execute("SELECT username FROM dashboard_admins ORDER BY username")
        admins = [r[0] for r in cur.fetchall()]
        cur.close(); conn.close()
        return admins if admins else [DASHBOARD_SEED_ADMIN]
    except Exception:
        return [DASHBOARD_SEED_ADMIN]


def _csv_to_list(text):
    if not text:
        return []
    return [v.strip() for v in text.split(",") if v.strip()]


def _get_admin_full_list():
    """Daftar admin lengkap dengan role/menu/estate — dipakai halaman Admins."""
    conn = get_db()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT username, role, allowed_menus, allowed_estates FROM dashboard_admins ORDER BY username"
        )
        rows = cur.fetchall()
        cur.close(); conn.close()
        result = []
        for username, role, allowed_menus, allowed_estates in rows:
            result.append({
                "username": username,
                "role": role or "admin",
                "allowed_menus": _csv_to_list(allowed_menus),
                "allowed_estates": _csv_to_list(allowed_estates) or ["ALL"],
            })
        return result
    except Exception as e:
        print(f"[ADMIN FULL LIST ERROR] {e}")
        return []


def _get_admin_info(username):
    conn = get_db()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT role, allowed_menus, allowed_estates FROM dashboard_admins WHERE username = %s",
            (username,)
        )
        row = cur.fetchone()
        cur.close(); conn.close()
        if not row:
            return None
        role, allowed_menus, allowed_estates = row
        return {
            "role": role or "admin",
            "allowed_menus": _csv_to_list(allowed_menus),
            "allowed_estates": _csv_to_list(allowed_estates) or ["ALL"],
        }
    except Exception as e:
        print(f"[ADMIN INFO ERROR] {e}")
        return None


def _get_allowed_estates():
    """None = tidak ada batasan (admin, atau allowed_estates='ALL'). List = daftar estate yang diizinkan."""
    if session.get("admin_role", "admin") == "admin":
        return None
    estates = session.get("admin_estates") or []
    if not estates or "ALL" in estates:
        return None
    return estates


def _is_admin(username):
    return username in _get_admin_list()


def _get_dashboard_prefix():
    conn = get_db()
    if not conn:
        return DASHBOARD_PREFIX_DEFAULT
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM dashboard_settings WHERE key = 'prefix'")
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    return DASHBOARD_PREFIX_DEFAULT


def _set_dashboard_prefix(new_prefix):
    conn = get_db()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO dashboard_settings (key, value) VALUES ('prefix', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (new_prefix,),
        )
        conn.commit(); cur.close(); conn.close()
        return True
    except Exception:
        return False


def _ensure_batch_jobs_table():
    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS batch_jobs (
                id SERIAL PRIMARY KEY,
                created_by VARCHAR(100),
                created_at TIMESTAMP DEFAULT NOW(),
                input_path TEXT NOT NULL,
                estate_splitter VARCHAR(100),
                output_folder TEXT NOT NULL,
                status VARCHAR(20) DEFAULT 'pending',
                current_stage VARCHAR(20),
                stage_progress INT DEFAULT 0,
                tiles_total INT,
                tiles_done INT DEFAULT 0,
                result_sick INT,
                result_healthy INT,
                result_shapefile_path TEXT,
                error_message TEXT,
                started_at TIMESTAMP,
                finished_at TIMESTAMP,
                stop_requested BOOLEAN DEFAULT FALSE,
                rotasi VARCHAR(50),
                bulan VARCHAR(50),
                tahun VARCHAR(10),
                result_rekap_shapefile_path TEXT,
                result_rekap_excel_path TEXT,
                do_rekap BOOLEAN DEFAULT TRUE
            )
        """)
        # Migrasi aman untuk tabel yang sudah ada dari sebelumnya (sebelum kolom ini ditambahkan)
        cur.execute("ALTER TABLE batch_jobs ADD COLUMN IF NOT EXISTS stop_requested BOOLEAN DEFAULT FALSE")
        cur.execute("ALTER TABLE batch_jobs ADD COLUMN IF NOT EXISTS rotasi VARCHAR(50)")
        cur.execute("ALTER TABLE batch_jobs ADD COLUMN IF NOT EXISTS bulan VARCHAR(50)")
        cur.execute("ALTER TABLE batch_jobs ADD COLUMN IF NOT EXISTS tahun VARCHAR(10)")
        cur.execute("ALTER TABLE batch_jobs ADD COLUMN IF NOT EXISTS result_rekap_shapefile_path TEXT")
        cur.execute("ALTER TABLE batch_jobs ADD COLUMN IF NOT EXISTS result_rekap_excel_path TEXT")
        cur.execute("ALTER TABLE batch_jobs ADD COLUMN IF NOT EXISTS stage_detail VARCHAR(200)")
        cur.execute("ALTER TABLE batch_jobs ADD COLUMN IF NOT EXISTS do_rekap BOOLEAN DEFAULT TRUE")
        # Penanda tile/checkpoint mentah di work_dir sudah dibersihkan permanen (lihat
        # _purge_job_work_dir di batch_worker.py) — sama seperti custom_batch_jobs.
        cur.execute("ALTER TABLE batch_jobs ADD COLUMN IF NOT EXISTS raw_cleaned BOOLEAN DEFAULT FALSE")
        # Nama mesin (hostname) yang benar-benar memproses job ini — dipakai kalau ada
        # lebih dari 1 server worker (lihat catatan desain multi-server), supaya kelihatan
        # job mana ditangani di server mana.
        cur.execute("ALTER TABLE batch_jobs ADD COLUMN IF NOT EXISTS worker_host VARCHAR(100)")
        # Referensi ke batch_splitter_uploads kalau splitter job ini dari SHP upload user
        # (bukan dari splitter_estate.geojson) — lihat _ensure_batch_splitter_uploads_table().
        cur.execute("ALTER TABLE batch_jobs ADD COLUMN IF NOT EXISTS splitter_upload_id VARCHAR(64)")
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"[BATCH JOBS TABLE] {e}")


def _ensure_batch_splitter_uploads_table():
    """SHP splitter yang diupload user sendiri (bukan dari splitter_estate.geojson) —
    disimpan sementara sebagai bytea di DB (bukan file lokal) supaya bisa diakses worker
    manapun yang klaim job ini, karena 2 server tidak berbagi disk. Dihapus otomatis oleh
    _purge_job_work_dir() di batch_worker.py begitu job selesai/dibersihkan."""
    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS batch_splitter_uploads (
                upload_id VARCHAR(64) PRIMARY KEY,
                uploaded_by VARCHAR(100),
                original_filename TEXT,
                zip_data BYTEA NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"[BATCH SPLITTER UPLOADS TABLE] {e}")


def _ensure_custom_batch_jobs_table():
    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS custom_batch_jobs (
                id SERIAL PRIMARY KEY,
                created_by VARCHAR(100),
                created_at TIMESTAMP DEFAULT NOW(),
                job_name VARCHAR(200),
                model_path TEXT NOT NULL,
                model_filename VARCHAR(255),
                class_names TEXT,
                class_styles TEXT,
                input_path TEXT NOT NULL,
                estate_splitter VARCHAR(100),
                confidence_threshold REAL DEFAULT 0.25,
                slice_size INT DEFAULT 1900,
                output_folder TEXT NOT NULL,
                status VARCHAR(20) DEFAULT 'pending',
                current_stage VARCHAR(20),
                stage_progress INT DEFAULT 0,
                stage_detail VARCHAR(200),
                tiles_total INT,
                tiles_done INT DEFAULT 0,
                result_total_detections INT,
                result_class_counts TEXT,
                result_geojson_path TEXT,
                result_shapefile_path TEXT,
                result_excel_path TEXT,
                error_message TEXT,
                stop_requested BOOLEAN DEFAULT FALSE,
                started_at TIMESTAMP,
                finished_at TIMESTAMP,
                raw_cleaned BOOLEAN DEFAULT FALSE,
                do_rekap BOOLEAN DEFAULT FALSE,
                result_rekap_shapefile_path TEXT,
                result_rekap_excel_path TEXT
            )
        """)
        # Migrasi aman untuk tabel yang sudah ada dari sebelumnya (sebelum kolom ini ditambahkan)
        cur.execute("ALTER TABLE custom_batch_jobs ADD COLUMN IF NOT EXISTS do_rekap BOOLEAN DEFAULT FALSE")
        cur.execute("ALTER TABLE custom_batch_jobs ADD COLUMN IF NOT EXISTS result_rekap_shapefile_path TEXT")
        cur.execute("ALTER TABLE custom_batch_jobs ADD COLUMN IF NOT EXISTS result_rekap_excel_path TEXT")
        # job_type: 'custom' (model diupload user) atau 'tbm' (model tetap Immature) —
        # dua-duanya tetap satu tabel & satu worker, cuma beda sumber model.
        cur.execute("ALTER TABLE custom_batch_jobs ADD COLUMN IF NOT EXISTS job_type VARCHAR(20) DEFAULT 'custom'")
        # Ukuran grid splitter (px) khusus job_type='tbm' — NULL = pakai default GRID_TILE_SIZE_PX
        cur.execute("ALTER TABLE custom_batch_jobs ADD COLUMN IF NOT EXISTS grid_tile_px INT")
        cur.execute("ALTER TABLE custom_batch_jobs ADD COLUMN IF NOT EXISTS worker_host VARCHAR(100)")
        # job_type='rekap_only' — "Rekap Data": user sudah punya SHP deteksi/rekap jadi,
        # skip splitter+deteksi sama sekali, langsung rekap (lihat custom_batch_worker.py
        # ::_process_rekap_only_job). kelas_col/rekap_mode/class_remap_json cuma dipakai
        # job_type ini; estate_splitter (kolom yang sudah ada) dipakai sebagai HINT estate
        # dari user — nilai sebenarnya tetap diselaraskan ke Aresta oleh blok_resolver.py.
        cur.execute("ALTER TABLE custom_batch_jobs ADD COLUMN IF NOT EXISTS kelas_col VARCHAR(100)")
        cur.execute("ALTER TABLE custom_batch_jobs ADD COLUMN IF NOT EXISTS rekap_mode VARCHAR(20) DEFAULT 'raw'")
        cur.execute("ALTER TABLE custom_batch_jobs ADD COLUMN IF NOT EXISTS class_remap_json TEXT")
        cur.execute("ALTER TABLE custom_batch_jobs ADD COLUMN IF NOT EXISTS rotasi VARCHAR(50)")
        cur.execute("ALTER TABLE custom_batch_jobs ADD COLUMN IF NOT EXISTS bulan VARCHAR(50)")
        cur.execute("ALTER TABLE custom_batch_jobs ADD COLUMN IF NOT EXISTS tahun VARCHAR(10)")
        # batch_group_id: sama untuk semua job yang dibuat dari 1 klik "Jalankan Rekap"
        # (lihat dashboard_rekap_data_submit) -- dipakai buat tahu job-job mana yang
        # boleh digabung Bad Image-nya sekali (lihat _maybe_merge_estate_group di
        # custom_batch_worker.py, dan merge_and_finalize.py).
        cur.execute("ALTER TABLE custom_batch_jobs ADD COLUMN IF NOT EXISTS batch_group_id VARCHAR(64)")
        # resolved_estate: estate SEBENARNYA hasil blok_resolver.py (beda dari
        # estate_splitter yang cuma hint user) -- ini kunci pengelompokan buat merge,
        # bukan estate_splitter yang bisa salah ketik.
        cur.execute("ALTER TABLE custom_batch_jobs ADD COLUMN IF NOT EXISTS resolved_estate VARCHAR(100)")
        # target_gsd_m: ukuran piksel (meter) yang diinginkan sebelum deteksi, khusus
        # job_type='homogenitas'. NULL = pakai resolusi asli citra (tanpa resample) —
        # ini default-nya, supaya setara dengan desktop app yang juga tidak me-resample.
        # Tipe job lain SENGAJA tidak membaca kolom ini (perilaku lamanya dipertahankan).
        cur.execute("ALTER TABLE custom_batch_jobs ADD COLUMN IF NOT EXISTS target_gsd_m REAL")
        # splitter_upload_id: SHP splitter upload user sendiri (bukan dari daftar estate
        # bawaan) -- sama persis polanya dengan batch_jobs.splitter_upload_id, reuse
        # tabel batch_splitter_uploads yang sama (sudah generic, tidak terikat 1 tabel
        # job tertentu). Berlaku untuk semua job_type di tabel ini (custom/tbm/
        # treecounting/homogenitas) -- lihat process_custom_job() di custom_batch_worker.py.
        cur.execute("ALTER TABLE custom_batch_jobs ADD COLUMN IF NOT EXISTS splitter_upload_id VARCHAR(64)")
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"[CUSTOM BATCH JOBS TABLE] {e}")


def _ensure_rekap_estate_merges_table():
    """Hasil GABUNGAN per estate (+rotasi/bulan/tahun) dalam 1 batch_group_id --
    dibuat setelah SEMUA job custom_batch_jobs (job_type='rekap_only') di grup itu
    selesai, lihat custom_batch_worker.py::_maybe_merge_estate_group(). UNIQUE
    constraint di bawah dipakai juga sebagai kunci klaim atomic (INSERT ...
    ON CONFLICT DO NOTHING) supaya 2 job yang selesai nyaris bersamaan tidak
    sama-sama trigger merge dobel."""
    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rekap_estate_merges (
                id SERIAL PRIMARY KEY,
                batch_group_id VARCHAR(64) NOT NULL,
                estate VARCHAR(100) NOT NULL,
                -- NOT NULL DEFAULT '' (bukan NULL) SENGAJA -- constraint UNIQUE di
                -- bawah menganggap tiap NULL beda satu sama lain (tidak dianggap
                -- duplikat), jadi ON CONFLICT DO NOTHING (klaim atomic merge) TIDAK
                -- akan bekerja kalau rotasi/bulan/tahun kosong dibiarkan NULL.
                rotasi VARCHAR(50) NOT NULL DEFAULT '',
                bulan VARCHAR(50) NOT NULL DEFAULT '',
                tahun VARCHAR(10) NOT NULL DEFAULT '',
                status VARCHAR(20) DEFAULT 'pending',
                result_shapefile_path TEXT,
                result_excel_path TEXT,
                error_message TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                finished_at TIMESTAMP,
                UNIQUE (batch_group_id, estate, rotasi, bulan, tahun)
            )
        """)
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"[REKAP ESTATE MERGES TABLE] {e}")


def _ensure_raw_photo_jobs_table():
    """Deteksi RAW Foto — foto drone biasa (PNG/JPG/TIF, tidak berkoordinat presisi),
    1x deteksi langsung (tanpa splitter/tile besar), beda bentuk data dari batch_jobs/
    custom_batch_jobs makanya tabel terpisah."""
    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS raw_photo_jobs (
                id SERIAL PRIMARY KEY,
                created_by VARCHAR(100),
                created_at TIMESTAMP DEFAULT NOW(),
                job_name VARCHAR(200),
                input_path TEXT NOT NULL,
                status VARCHAR(20) DEFAULT 'pending',
                error_message TEXT,
                started_at TIMESTAMP,
                finished_at TIMESTAMP,
                result_image_path TEXT,
                result_total_detections INT,
                result_class_counts TEXT,
                class_names TEXT,
                class_styles TEXT,
                gps_lat REAL,
                gps_lon REAL,
                bounds_json TEXT,
                worker_host VARCHAR(100)
            )
        """)
        cur.execute("ALTER TABLE raw_photo_jobs ADD COLUMN IF NOT EXISTS worker_host VARCHAR(100)")
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"[RAW PHOTO JOBS TABLE] {e}")


def _ensure_thematic_mapping_table():
    """Automatic Thematic Mapping (ATM) — bukan job-queue (tidak ada status/progress
    kolom seperti batch_jobs), tabel CRUD biasa untuk riwayat sesi (Step 1/2/3
    tersimpan semua supaya bisa dibuka/diedit lagi lewat halaman Riwayat)."""
    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS thematic_mapping_sessions (
                id SERIAL PRIMARY KEY,
                created_by VARCHAR(100),
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW(),

                title VARCHAR(200) NOT NULL DEFAULT 'Untitled',
                excel_source_path TEXT NOT NULL,
                excel_source_filename VARCHAR(255),
                sheet_name VARCHAR(200),
                blok_id_column VARCHAR(200) NOT NULL DEFAULT 'Blok_ID',
                thematic_column VARCHAR(200) NOT NULL,

                join_matched_count INT,
                join_total_count INT,
                join_unmatched_blok_ids TEXT,

                thematic_type VARCHAR(20) NOT NULL DEFAULT 'warna',
                category_styles TEXT,
                polygon_opacity REAL DEFAULT 0.75,
                polygon_line_color VARCHAR(20) DEFAULT '#333333',
                polygon_line_width INT DEFAULT 1,

                region_scope TEXT,
                show_label BOOLEAN DEFAULT TRUE,
                label_size INT DEFAULT 12,
                show_area_terpilih BOOLEAN DEFAULT FALSE,
                show_tahun_tanam BOOLEAN DEFAULT FALSE,
                map_scale INT,
                description TEXT
            )
        """)
        # Migrasi aman untuk sesi yang sudah ada dari sebelum kolom-kolom ini ditambahkan.
        # label_overrides: override teks PER BLOK di label peta (JSON {blok_id: {text1, text2}}),
        # terpisah dari category_styles/label pengaturan global yang sudah ada duluan.
        cur.execute("ALTER TABLE thematic_mapping_sessions ADD COLUMN IF NOT EXISTS label_overrides TEXT")
        cur.execute("ALTER TABLE thematic_mapping_sessions ADD COLUMN IF NOT EXISTS orientation_override TEXT")
        cur.execute("ALTER TABLE thematic_mapping_sessions ADD COLUMN IF NOT EXISTS scale_override TEXT")
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"[THEMATIC MAPPING TABLE] {e}")


def _ensure_drone_log_tables():
    """Log Drone — 1 upload bisa berisi banyak file .txt sekaligus, semuanya digabung
    jadi SATU rekap (2 tabel: drone_report_uploads = batch header + hasil gabungan
    GPX/Excel/SHP/PNG/PPTX, drone_report_files = 1 baris per file .txt, tracking
    status parsing tiap file yang nanti dijumlah/digabung)."""
    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS drone_report_uploads (
                id SERIAL PRIMARY KEY,
                created_by VARCHAR(100),
                created_at TIMESTAMP DEFAULT NOW(),
                batch_name VARCHAR(200),
                files_total INT DEFAULT 0,
                files_done INT DEFAULT 0,
                files_failed INT DEFAULT 0
            )
        """)
        for ddl in (
            "ALTER TABLE drone_report_uploads ADD COLUMN IF NOT EXISTS status VARCHAR(20) DEFAULT 'pending'",
            "ALTER TABLE drone_report_uploads ADD COLUMN IF NOT EXISTS error_message TEXT",
            "ALTER TABLE drone_report_uploads ADD COLUMN IF NOT EXISTS started_at TIMESTAMP",
            "ALTER TABLE drone_report_uploads ADD COLUMN IF NOT EXISTS finished_at TIMESTAMP",
            "ALTER TABLE drone_report_uploads ADD COLUMN IF NOT EXISTS result_gpx_path TEXT",
            "ALTER TABLE drone_report_uploads ADD COLUMN IF NOT EXISTS result_xlsx_path TEXT",
            "ALTER TABLE drone_report_uploads ADD COLUMN IF NOT EXISTS result_map_path TEXT",
            "ALTER TABLE drone_report_uploads ADD COLUMN IF NOT EXISTS result_pptx_path TEXT",
            "ALTER TABLE drone_report_uploads ADD COLUMN IF NOT EXISTS result_shp_path TEXT",
            "ALTER TABLE drone_report_uploads ADD COLUMN IF NOT EXISTS pilot_name TEXT",
            "ALTER TABLE drone_report_uploads ADD COLUMN IF NOT EXISTS drone_code TEXT",
            "ALTER TABLE drone_report_uploads ADD COLUMN IF NOT EXISTS drone_sn VARCHAR(200)",
            "ALTER TABLE drone_report_uploads ADD COLUMN IF NOT EXISTS duration_s REAL",
            "ALTER TABLE drone_report_uploads ADD COLUMN IF NOT EXISTS distance_m REAL",
            "ALTER TABLE drone_report_uploads ADD COLUMN IF NOT EXISTS worker_host VARCHAR(100)",
        ):
            cur.execute(ddl)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS drone_report_files (
                id SERIAL PRIMARY KEY,
                upload_id INT REFERENCES drone_report_uploads(id),
                original_filename VARCHAR(255),
                input_path TEXT NOT NULL,
                status VARCHAR(20) DEFAULT 'pending',
                error_message TEXT,
                started_at TIMESTAMP,
                finished_at TIMESTAMP,
                result_json_path TEXT,
                result_gpx_path TEXT,
                result_xlsx_path TEXT,
                result_map_path TEXT,
                drone_sn VARCHAR(100),
                duration_s REAL,
                distance_m REAL,
                worker_host VARCHAR(100)
            )
        """)
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"[DRONE LOG TABLES] {e}")


def _find_qgis_bat():
    roots = glob.glob(r"C:\Program Files\QGIS*")
    roots = sorted(roots, key=lambda p: ("ltr" not in p.lower(), p), reverse=True)
    for qdir in roots:
        bat = os.path.join(qdir, "bin", "python-qgis-ltr.bat")
        if os.path.exists(bat):
            return bat
        bat = os.path.join(qdir, "bin", "python-qgis.bat")
        if os.path.exists(bat):
            return bat
    return None


def _get_estate_list_via_qgis():
    """Panggil get_estate_list_local() Agripalm (tidak diubah) via QGIS Python subprocess."""
    qgis_bat = _find_qgis_bat()
    if not qgis_bat:
        return []
    code = (
        "import sys, json; "
        f"sys.path.insert(0, r'{AGRIPALM_INSTALL_PATH}'); "
        "from geoprocessing.nutripalm.estate_pilihan import get_estate_list_local; "
        "print(json.dumps(get_estate_list_local()))"
    )
    try:
        result = subprocess.run([qgis_bat, "-c", code], capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip().splitlines()[-1])
    except Exception as e:
        print(f"[ESTATE LIST ERROR] {e}")
    return []


_ensure_admin_table()
_ensure_batch_jobs_table()
_ensure_batch_splitter_uploads_table()
_ensure_custom_batch_jobs_table()
_ensure_rekap_estate_merges_table()
_ensure_raw_photo_jobs_table()
_ensure_thematic_mapping_table()
_ensure_drone_log_tables()
DASHBOARD_PREFIX = _get_dashboard_prefix()


# ── JWT Helper ────────────────────────────────────────────────────────────────
def create_token(payload_extra=None):
    payload = {
        "iat": datetime.utcnow(),
        "exp": datetime.utcnow() + timedelta(hours=JWT_EXPIRY_HOURS),
    }
    if payload_extra:
        payload.update(payload_extra)
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Token tidak ditemukan"}), 401
        token = auth_header[7:]
        try:
            decoded = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
            request.jwt_payload = decoded
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Token sudah expired"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "Token tidak valid"}), 401
        return f(*args, **kwargs)
    return decorated


# ══════════════════════════════════════════════════════════════════════════════
#  ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

# ── 1. Get JWT Token ──────────────────────────────────────────────────────────
@app.route("/api/auth/token", methods=["POST"])
def auth_token():
    data = request.get_json(silent=True) or {}
    if data.get("app_secret") != APP_SECRET:
        return jsonify({"error": "App secret tidak valid"}), 403
    token = create_token({"source": "agripalm_client"})
    return jsonify({"token": token, "expires_in": JWT_EXPIRY_HOURS * 3600})


# ── 2. Verify License ────────────────────────────────────────────────────────
@app.route("/api/license/verify", methods=["POST"])
@token_required
def license_verify():
    data = request.get_json(silent=True) or {}
    username = data.get("username")
    license_key = data.get("license_key")
    mac_address = data.get("mac_address")

    if not all([username, license_key, mac_address]):
        return jsonify({"valid": False, "reason": "missing_fields"}), 400

    conn = get_db()
    if not conn:
        return jsonify({"valid": False, "reason": "db_unavailable"}), 503

    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT mac_address, expiration_date, status FROM licenses "
            "WHERE username = %s AND license_key = %s",
            (username, license_key),
        )
        result = cur.fetchone()
        cur.close()
        conn.close()

        if not result:
            return jsonify({"valid": False, "reason": "not_found"})

        mac_in_db, expiration_date, status = result

        if not status:
            return jsonify({"valid": False, "reason": "inactive"})

        if mac_in_db != "Licensed" and mac_in_db != mac_address:
            return jsonify({"valid": False, "reason": "mac_mismatch"})

        today = datetime.now().date()
        if expiration_date < today:
            return jsonify({"valid": False, "reason": "expired"})

        return jsonify({
            "valid": True,
            "expires": expiration_date.isoformat(),
            "mac_status": "new" if mac_in_db == "Licensed" else "bound",
        })

    except Exception as e:
        print(f"[LICENSE VERIFY ERROR] {e}")
        return jsonify({"valid": False, "reason": "server_error"}), 500


# ── 3. Activate License (save MAC) ───────────────────────────────────────────
@app.route("/api/license/activate", methods=["POST"])
@token_required
def license_activate():
    data = request.get_json(silent=True) or {}
    username = data.get("username")
    license_key = data.get("license_key")
    mac_address = data.get("mac_address")

    if not all([username, license_key, mac_address]):
        return jsonify({"success": False, "message": "missing_fields"}), 400

    conn = get_db()
    if not conn:
        return jsonify({"success": False, "message": "db_unavailable"}), 503

    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE licenses SET mac_address = %s WHERE username = %s AND license_key = %s",
            (mac_address, username, license_key),
        )
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True, "message": "MAC address disimpan"})
    except Exception as e:
        print(f"[LICENSE ACTIVATE ERROR] {e}")
        return jsonify({"success": False, "message": str(e)}), 500


# ── 4. LDAP Validation ───────────────────────────────────────────────────────
@app.route("/api/auth/ldap", methods=["POST"])
@token_required
def auth_ldap():
    if not LDAP_AVAILABLE:
        return jsonify({"valid": False, "message": "ldap3 tidak tersedia di server"}), 503

    data = request.get_json(silent=True) or {}
    username = data.get("username")
    password = data.get("password")

    if not username or not password:
        return jsonify({"valid": False, "message": "missing_fields"}), 400

    user_upn = f"{username}@bumitama.com"
    server = Server(LDAP_HOST, port=LDAP_PORT, get_info=ALL)
    try:
        ldap_conn = Connection(server, user=user_upn, password=password, auto_bind=False)
        if ldap_conn.bind():
            ldap_conn.unbind()
            return jsonify({"valid": True})
        else:
            return jsonify({"valid": False, "message": "Username atau password tidak valid"})
    except Exception as e:
        print(f"[LDAP ERROR] {e}")
        return jsonify({"valid": False, "message": f"LDAP error: {e}"}), 500


# ── 5. Verify Username ───────────────────────────────────────────────────────
@app.route("/api/auth/verify-user", methods=["POST"])
@token_required
def auth_verify_user():
    data = request.get_json(silent=True) or {}
    username = data.get("username")
    if not username:
        return jsonify({"exists": False}), 400

    conn = get_db()
    if not conn:
        return jsonify({"exists": False, "reason": "db_unavailable"}), 503

    try:
        cur = conn.cursor()
        cur.execute("SELECT username FROM users WHERE username = %s", (username,))
        result = cur.fetchone()
        cur.close()
        conn.close()
        return jsonify({"exists": result is not None})
    except Exception as e:
        print(f"[VERIFY USER ERROR] {e}")
        return jsonify({"exists": False}), 500


# ── 6. Log Login ──────────────────────────────────────────────────────────────
@app.route("/api/log/login", methods=["POST"])
@token_required
def log_login():
    data = request.get_json(silent=True) or {}
    username = data.get("username")
    latitude = data.get("latitude")
    longitude = data.get("longitude")

    conn = get_db()
    if not conn:
        return jsonify({"log_id": None, "reason": "db_unavailable"}), 503

    try:
        cur = conn.cursor()
        login_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        hostname = data.get("hostname", "unknown")
        cur.execute(
            "INSERT INTO login_logs (username, login_time, latitude, longitude, ip_address) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (username, login_time, latitude, longitude, hostname),
        )
        conn.commit()
        log_id = cur.fetchone()[0]
        cur.close()
        conn.close()
        return jsonify({"log_id": log_id})
    except Exception as e:
        print(f"[LOG LOGIN ERROR] {e}")
        return jsonify({"log_id": None}), 500


# ── 7. Log Logout ─────────────────────────────────────────────────────────────
@app.route("/api/log/logout", methods=["POST"])
@token_required
def log_logout():
    data = request.get_json(silent=True) or {}
    log_id = data.get("log_id")
    if not log_id:
        return jsonify({"success": False}), 400

    conn = get_db()
    if not conn:
        return jsonify({"success": False}), 503

    try:
        cur = conn.cursor()
        logout_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur.execute(
            "UPDATE login_logs SET logout_time=%s, duration=AGE(%s, login_time) WHERE id=%s",
            (logout_time, logout_time, log_id),
        )
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        print(f"[LOG LOGOUT ERROR] {e}")
        return jsonify({"success": False}), 500


# ── 8. Increment Activity ────────────────────────────────────────────────────
@app.route("/api/log/activity", methods=["POST"])
@token_required
def log_activity():
    data = request.get_json(silent=True) or {}
    log_id = data.get("log_id")
    activity = data.get("activity")

    if not log_id or not activity:
        return jsonify({"success": False}), 400

    ALLOWED_ACTIVITIES = {
        "image_inputs", "successful_predictions", "charts_shown",
        "excel_downloaded", "shapefile_downloaded",
    }
    if activity not in ALLOWED_ACTIVITIES:
        return jsonify({"success": False, "reason": "activity_not_allowed"}), 400

    conn = get_db()
    if not conn:
        return jsonify({"success": False}), 503

    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE login_logs SET {activity} = {activity} + 1 WHERE id = %s",
            (log_id,),
        )
        koordinat = data.get("koordinat")
        if koordinat and "," in koordinat:
            lat, lng = koordinat.split(",", 1)
            cur.execute(
                "UPDATE login_logs SET latitude = %s, longitude = %s WHERE id = %s",
                (lat.strip(), lng.strip(), log_id),
            )
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        print(f"[LOG ACTIVITY ERROR] {e}")
        return jsonify({"success": False}), 500


# ── 9. Update Last Open ──────────────────────────────────────────────────────
@app.route("/api/user/last-open", methods=["POST"])
@token_required
def user_last_open():
    data = request.get_json(silent=True) or {}
    username = data.get("username")
    days = data.get("days")

    if not username:
        return jsonify({"success": False}), 400

    conn = get_db()
    if not conn:
        return jsonify({"success": False}), 503

    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE licenses SET last_open = %s WHERE username = %s",
            (f"{days} hari" if days is not None else "0 hari", username),
        )
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        print(f"[LAST OPEN ERROR] {e}")
        return jsonify({"success": False}), 500


# ── 10. Health Check ──────────────────────────────────────────────────────────
@app.route("/api/health", methods=["GET"])
def health():
    db_ok = False
    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
            conn.close()
            db_ok = True
        except Exception:
            pass
    return jsonify({
        "status": "ok",
        "db": "connected" if db_ok else "disconnected",
        "ldap": "available" if LDAP_AVAILABLE else "unavailable",
        "server_time": datetime.now().isoformat(),
    })


# ── 11. Chart Data (for dashboard) ────────────────────────────────────────────
@app.route(DASHBOARD_PREFIX + "/chart-data")
def dashboard_chart_data():
    if not session.get("admin_user"):
        return jsonify({"error": "unauthorized"}), 401

    conn = get_db()
    if not conn:
        return jsonify({"error": "db_unavailable"}), 503

    try:
        cur = conn.cursor()

        # 1. Login per hari (30 hari terakhir)
        cur.execute("""
            SELECT login_time::date AS day, COUNT(*) AS total
            FROM login_logs
            WHERE login_time >= CURRENT_DATE - INTERVAL '30 days'
            GROUP BY day ORDER BY day
        """)
        logins_daily = cur.fetchall()

        # 2. Top 10 users paling aktif bulan ini
        cur.execute("""
            SELECT username, COUNT(*) AS total
            FROM login_logs
            WHERE EXTRACT(MONTH FROM login_time) = EXTRACT(MONTH FROM CURRENT_DATE)
              AND EXTRACT(YEAR FROM login_time) = EXTRACT(YEAR FROM CURRENT_DATE)
            GROUP BY username ORDER BY total DESC LIMIT 10
        """)
        top_users = cur.fetchall()

        # 3. Prediksi & Images per hari (30 hari terakhir)
        cur.execute("""
            SELECT login_time::date AS day,
                   COALESCE(SUM(successful_predictions), 0) AS predictions,
                   COALESCE(SUM(image_inputs), 0) AS images
            FROM login_logs
            WHERE login_time >= CURRENT_DATE - INTERVAL '30 days'
            GROUP BY day ORDER BY day
        """)
        usage_daily = cur.fetchall()

        cur.close(); conn.close()

        return jsonify({
            "logins_daily": {
                "labels": [str(r[0]) for r in logins_daily],
                "data": [r[1] for r in logins_daily],
            },
            "top_users": {
                "labels": [r[0] for r in top_users],
                "data": [r[1] for r in top_users],
            },
            "usage_daily": {
                "labels": [str(r[0]) for r in usage_daily],
                "predictions": [r[1] for r in usage_daily],
                "images": [r[2] for r in usage_daily],
            },
        })
    except Exception as e:
        print(f"[CHART DATA ERROR] {e}")
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
#  DASHBOARD WEB UI
# ══════════════════════════════════════════════════════════════════════════════

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_user"):
            return redirect(url_for("dashboard_login"))
        return f(*args, **kwargs)
    return decorated


# ── Gate akses menu & data per role (jalan di setiap request dashboard) ────────
# Role "admin" selalu full akses. Role "user" dibatasi ke allowed_menus (session)
# dan, untuk halaman batch/custom, ke job yang estate-nya ada di allowed_estates.
_MENU_PATH_MAP = [
    ("/chart-data", "overview"),
    ("/admins", "admins"),
    ("/settings", "settings"),
    ("/database", "database"),
    ("/thematic", "thematic_mapping"),
    ("/tools/tbm-poor-class", "tbm_poor_class_tool"),
    ("/tbm", "tbm"),
    ("/treecounting", "treecounting"),
    # Dicek SEBELUM "/homogenitas" di bawah -- "/homogenitas-rekap" juga startswith
    # "/homogenitas", jadi kalau urutannya kebalik, halaman ini salah kena gate
    # permission 'homogenitas' padahal seharusnya 'homogenitas_rekap'.
    ("/homogenitas-rekap", "homogenitas_rekap"),
    ("/homogenitas", "homogenitas"),
    ("/custom", "custom"),
    ("/batch", "batch"),
    ("/raw", "raw"),
    ("/drone", "drone"),
    ("/users", "users"),
    ("/licenses", "licenses"),
    ("/logs", "logs"),
]

_JOB_PATH_RE = re.compile(
    re.escape(DASHBOARD_PREFIX) + r"/(batch|custom|tbm|treecounting|homogenitas)/(?:download|delete|stop|resume|analyze)/(\d+)"
)


@app.before_request
def _dashboard_access_gate():
    path = request.path
    if not path.startswith(DASHBOARD_PREFIX):
        return  # bukan route dashboard (mis. API desktop app) — tidak diatur di sini
    if path in (DASHBOARD_PREFIX + "/login", DASHBOARD_PREFIX + "/logout"):
        return
    if not session.get("admin_user"):
        return  # biar admin_required per-route yang redirect ke login

    if session.get("admin_role", "admin") == "admin":
        return  # admin selalu full akses

    allowed_menus = session.get("admin_menus") or []

    if path == DASHBOARD_PREFIX or path == DASHBOARD_PREFIX + "/":
        key = "overview"
    else:
        key = next((k for suffix, k in _MENU_PATH_MAP if path.startswith(DASHBOARD_PREFIX + suffix)), None)

    if key in ("admins", "settings"):
        return redirect(url_for("dashboard_overview"))
    # "overview" TIDAK ikut diblokir di sini: halaman itu adalah tujuan redirect di
    # bawah ini sendiri, jadi kalau ikut diblokir hasilnya redirect ke dirinya sendiri
    # berulang-ulang (browser menampilkan ERR_TOO_MANY_REDIRECTS dan admin yang menu
    # 'overview'-nya tidak dicentang jadi terkunci total, tidak bisa masuk sama sekali).
    # Aman dibiarkan terbuka karena isi sensitifnya (tabel Login Terbaru + IP) sudah
    # dibatasi ke admin penuh di dashboard_overview(); role terbatas cuma lihat angka.
    if key and key != "overview" and key not in allowed_menus:
        return redirect(url_for("dashboard_overview"))

    # Cegah akses langsung ke job orang lain lewat URL (id ditebak/diketik manual)
    m = _JOB_PATH_RE.match(path)
    if m:
        allowed_estates = _get_allowed_estates()
        if allowed_estates is not None:
            table = "batch_jobs" if m.group(1) == "batch" else "custom_batch_jobs"
            job_id = int(m.group(2))
            conn = get_db()
            if not conn:
                abort(403)
            row = None
            try:
                cur = conn.cursor()
                cur.execute(f"SELECT estate_splitter FROM {table} WHERE id = %s", (job_id,))
                row = cur.fetchone()
                cur.close(); conn.close()
            except Exception as e:
                print(f"[JOB ACCESS GATE ERROR] {e}")
            if not row or row[0] not in allowed_estates:
                abort(403)


def _try_ldap_login(username, password):
    if not LDAP_AVAILABLE:
        return False
    try:
        user_upn = f"{username}@bumitama.com"
        server = Server(LDAP_HOST, port=LDAP_PORT, get_info=ALL)
        ldap_conn = Connection(server, user=user_upn, password=password, auto_bind=False)
        if ldap_conn.bind():
            ldap_conn.unbind()
            return True
    except Exception as e:
        print(f"[DASHBOARD LDAP] {e}")
    return False


@app.route(DASHBOARD_PREFIX + "/login", methods=["GET", "POST"])
def dashboard_login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        throttle_key = f"{username.lower()}|{_client_ip()}"
        boleh, sisa_detik = _login_throttle_status(throttle_key)

        if not boleh:
            error = (f"Terlalu banyak percobaan login gagal. Coba lagi dalam "
                     f"{max(1, sisa_detik // 60)} menit.")
            print(f"[LOGIN THROTTLE] '{username}' dari {_client_ip()} diblokir sementara")
        elif not password:
            error = "Password wajib diisi."
        elif not _try_ldap_login(username, password):
            # Gagal di LDAP langsung ditolak di sini, SEBELUM cek status admin —
            # supaya orang yang belum tahu password LDAP asli tidak bisa dipakai
            # buat mengecek/menebak-nebak username mana saja yang terdaftar admin.
            _login_record_failure(throttle_key)
            error = "Login gagal. Username atau password tidak cocok."
        elif not _is_admin(username):
            # LDAP-nya benar tapi bukan admin dashboard — jangan dihitung sebagai
            # percobaan brute-force (orangnya memang karyawan sah, cuma tidak berhak).
            error = "Anda tidak terdaftar sebagai admin dashboard."
        else:
            _login_record_success(throttle_key)
            session.clear()  # cegah session fixation — mulai sesi baru dari nol
            session.permanent = True
            session["admin_user"] = username
            info = _get_admin_info(username) or {"role": "admin", "allowed_menus": [], "allowed_estates": ["ALL"]}
            session["admin_role"] = info["role"]
            session["admin_menus"] = info["allowed_menus"]
            session["admin_estates"] = info["allowed_estates"]
            print(f"[LOGIN OK] '{username}' (role={info['role']}) dari {_client_ip()}")
            return redirect(url_for("dashboard_overview"))
    return render_template("login.html", error=error)


@app.route(DASHBOARD_PREFIX + "/logout")
def dashboard_logout():
    # Bersihkan SELURUH isi session, bukan cuma admin_user — kalau cuma key itu yang
    # dihapus, admin_role/admin_menus/admin_estates milik user sebelumnya masih
    # tertinggal di cookie dan bisa terbawa ke sesi berikutnya di browser yang sama.
    session.clear()
    return redirect(url_for("dashboard_login"))


# ── Overview ──────────────────────────────────────────────────────────────────
@app.route(DASHBOARD_PREFIX)
@admin_required
def dashboard_overview():
    # Overview adalah halaman pendaratan SEMUA role (termasuk role "user" yang aksesnya
    # dibatasi ke beberapa menu saja) — jadi tabel "Login Terbaru" di sini dulu bocor ke
    # semua orang, lengkap dengan username + IP/hostname mesin tiap orang. Sekarang
    # bagian itu hanya diambil untuk role admin penuh; role terbatas tetap melihat
    # kartu ringkasan (angka agregat, tidak mengidentifikasi siapa pun).
    is_full_admin = session.get("admin_role", "admin") == "admin"
    conn = get_db()
    data = {"total_users": 0, "active_licenses": 0, "expired_licenses": 0,
            "logins_today": 0, "recent_logs": [], "db_ok": False, "ldap_ok": LDAP_AVAILABLE,
            "can_see_recent_logs": is_full_admin}
    if conn:
        data["db_ok"] = True
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM users")
            data["total_users"] = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM licenses WHERE status = true AND expiration_date >= CURRENT_DATE")
            data["active_licenses"] = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM licenses WHERE expiration_date < CURRENT_DATE")
            data["expired_licenses"] = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM login_logs WHERE login_time::date = CURRENT_DATE")
            data["logins_today"] = cur.fetchone()[0]
            if is_full_admin:
                cur.execute("SELECT id, username, login_time, logout_time, duration, ip_address FROM login_logs ORDER BY id DESC LIMIT 10")
                data["recent_logs"] = cur.fetchall()
            cur.close(); conn.close()
        except Exception as e:
            print(f"[DASHBOARD ERROR] {e}")
    return render_template("overview.html", active="overview", **data)


# ── Users ─────────────────────────────────────────────────────────────────────
@app.route(DASHBOARD_PREFIX + "/users")
@admin_required
def dashboard_users():
    conn = get_db()
    users = []
    if conn:
        try:
            cur = conn.cursor()
            # Kolom password SENGAJA tidak diambil di sini -- halaman ini cuma perlu
            # tampilkan daftar user, dan menyertakan password (walau cuma buat lewat
            # ke template) berisiko ke-render balik ke HTML kalau ada yang lupa/khilaf
            # nanti. Form Tambah/Edit tetap bisa SET password baru lewat POST terpisah
            # (dashboard_users_add/_edit) -- itu tidak butuh baca nilai lama.
            cur.execute("SELECT username, nama_lengkap, email, jabatan, estate, wilayah FROM users ORDER BY username")
            users = cur.fetchall()
            cur.close(); conn.close()
        except Exception as e:
            print(f"[USERS ERROR] {e}")
    return render_template("users.html", active="users", users=users)


@app.route(DASHBOARD_PREFIX + "/users/add", methods=["POST"])
@admin_required
def dashboard_users_add():
    f = request.form
    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO users (username, password, nama_lengkap, email, jabatan, estate, wilayah) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (username) DO UPDATE SET password=EXCLUDED.password, nama_lengkap=EXCLUDED.nama_lengkap, "
                "email=EXCLUDED.email, jabatan=EXCLUDED.jabatan, estate=EXCLUDED.estate, wilayah=EXCLUDED.wilayah",
                (f["username"], f["password"], f.get("nama_lengkap"), f.get("email"),
                 f.get("jabatan"), f.get("estate"), f.get("wilayah")),
            )
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[USER ADD ERROR] {e}")
            conn.close()
            return redirect(url_for("dashboard_users", flash_error=f"Gagal menambah user: {e}"))
    return redirect(url_for("dashboard_users", flash_success="User berhasil ditambahkan"))


@app.route(DASHBOARD_PREFIX + "/users/edit", methods=["POST"])
@admin_required
def dashboard_users_edit():
    f = request.form
    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            if f.get("password"):
                cur.execute(
                    "UPDATE users SET username=%s, password=%s, nama_lengkap=%s, email=%s, jabatan=%s, estate=%s, wilayah=%s WHERE username=%s",
                    (f["username"], f["password"], f.get("nama_lengkap"), f.get("email"),
                     f.get("jabatan"), f.get("estate"), f.get("wilayah"), f["original_username"]),
                )
            else:
                cur.execute(
                    "UPDATE users SET username=%s, nama_lengkap=%s, email=%s, jabatan=%s, estate=%s, wilayah=%s WHERE username=%s",
                    (f["username"], f.get("nama_lengkap"), f.get("email"),
                     f.get("jabatan"), f.get("estate"), f.get("wilayah"), f["original_username"]),
                )
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[USER EDIT ERROR] {e}")
            conn.close()
            return redirect(url_for("dashboard_users", flash_error=f"Gagal update user: {e}"))
    return redirect(url_for("dashboard_users", flash_success="User berhasil diupdate"))


@app.route(DASHBOARD_PREFIX + "/users/delete/<username>")
@admin_required
def dashboard_users_delete(username):
    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM users WHERE username = %s", (username,))
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[USER DELETE ERROR] {e}")
    return redirect(url_for("dashboard_users"))


# ── Licenses ──────────────────────────────────────────────────────────────────
@app.route(DASHBOARD_PREFIX + "/licenses")
@admin_required
def dashboard_licenses():
    conn = get_db()
    licenses = []
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("SELECT id, username, license_key, expiration_date, status, mac_address, last_open FROM licenses ORDER BY id")
            licenses = cur.fetchall()
            cur.close(); conn.close()
        except Exception as e:
            print(f"[LICENSES ERROR] {e}")
    return render_template("licenses.html", active="licenses", licenses=licenses)


@app.route(DASHBOARD_PREFIX + "/licenses/detail/<int:lid>")
@admin_required
def dashboard_licenses_detail(lid):
    """Ambil 1 baris lisensi LENGKAP (termasuk license_key utuh) — dipanggil hanya saat
    tombol Edit diklik.

    Sebelumnya seluruh license_key semua baris ikut tercetak di atribut onclick tabel,
    jadi walaupun kolomnya sudah sengaja disamarkan jadi 12 karakter di tampilan, key
    utuh SEMUA lisensi tetap bisa dipanen sekaligus lewat "View Page Source". Sekarang
    key utuh hanya dikirim satu per satu, saat memang mau diedit."""
    conn = get_db()
    if not conn:
        return jsonify({"success": False, "message": "DB tidak tersedia"}), 503
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, username, license_key, expiration_date, status, mac_address "
                    "FROM licenses WHERE id = %s", (lid,))
        row = cur.fetchone()
        cur.close(); conn.close()
    except Exception as e:
        print(f"[LICENSE DETAIL ERROR] {e}")
        return jsonify({"success": False, "message": str(e)}), 500

    if not row:
        return jsonify({"success": False, "message": "Lisensi tidak ditemukan"}), 404
    return jsonify({
        "success": True,
        "id": row[0],
        "username": row[1],
        "license_key": row[2],
        "expiration_date": str(row[3]) if row[3] else "",
        "status": bool(row[4]),
        "mac_address": row[5] or "",
    })


@app.route(DASHBOARD_PREFIX + "/licenses/add", methods=["POST"])
@admin_required
def dashboard_licenses_add():
    f = request.form
    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO licenses (username, license_key, expiration_date, status, mac_address) VALUES (%s, %s, %s, %s, %s)",
                (f["username"], f["license_key"], f["expiration_date"], f["status"] == "true", "Licensed"),
            )
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[LICENSE ADD ERROR] {e}")
            conn.close()
            return redirect(url_for("dashboard_licenses", flash_error=f"Gagal menambah lisensi: {e}"))
    return redirect(url_for("dashboard_licenses", flash_success="Lisensi berhasil ditambahkan"))


@app.route(DASHBOARD_PREFIX + "/licenses/edit", methods=["POST"])
@admin_required
def dashboard_licenses_edit():
    f = request.form
    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE licenses SET username=%s, license_key=%s, expiration_date=%s, status=%s, mac_address=%s WHERE id=%s",
                (f["username"], f["license_key"], f["expiration_date"],
                 f["status"] == "true", f.get("mac_address") or "Licensed", f["id"]),
            )
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[LICENSE EDIT ERROR] {e}")
            conn.close()
            return redirect(url_for("dashboard_licenses", flash_error=f"Gagal update lisensi: {e}"))
    return redirect(url_for("dashboard_licenses", flash_success="Lisensi berhasil diupdate"))


@app.route(DASHBOARD_PREFIX + "/licenses/delete/<int:lid>")
@admin_required
def dashboard_licenses_delete(lid):
    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM licenses WHERE id = %s", (lid,))
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[LICENSE DELETE ERROR] {e}")
    return redirect(url_for("dashboard_licenses"))


@app.route(DASHBOARD_PREFIX + "/licenses/reset-mac/<int:lid>")
@admin_required
def dashboard_licenses_reset_mac(lid):
    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("UPDATE licenses SET mac_address = 'Licensed' WHERE id = %s", (lid,))
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[MAC RESET ERROR] {e}")
    return redirect(url_for("dashboard_licenses"))


# ── Login Logs ────────────────────────────────────────────────────────────────
@app.route(DASHBOARD_PREFIX + "/logs")
@admin_required
def dashboard_logs():
    now = datetime.now()
    month = request.args.get("month", now.month, type=int)
    year = request.args.get("year", now.year, type=int)

    conn = get_db()
    logs = []
    total_logs = 0
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM login_logs")
            total_logs = cur.fetchone()[0]
            cur.execute(
                "SELECT id, username, login_time, logout_time, duration, "
                "latitude, longitude, ip_address, successful_predictions, image_inputs, charts_shown "
                "FROM login_logs "
                "WHERE EXTRACT(MONTH FROM login_time) = %s AND EXTRACT(YEAR FROM login_time) = %s "
                "ORDER BY id DESC",
                (month, year),
            )
            logs = cur.fetchall()
            cur.close(); conn.close()
        except Exception as e:
            print(f"[LOGS ERROR] {e}")
    return render_template("logs.html", active="logs", logs=logs, total_logs=total_logs,
                           current_month=month, current_year=year)


@app.route(DASHBOARD_PREFIX + "/logs/export")
@admin_required
def dashboard_logs_export():
    month = request.args.get("month", datetime.now().month, type=int)
    year = request.args.get("year", datetime.now().year, type=int)

    conn = get_db()
    if not conn:
        return "DB tidak tersedia", 503

    try:
        import io
        import csv

        cur = conn.cursor()
        cur.execute(
            "SELECT id, username, login_time, logout_time, duration, "
            "latitude, longitude, ip_address, successful_predictions, image_inputs, charts_shown "
            "FROM login_logs "
            "WHERE EXTRACT(MONTH FROM login_time) = %s AND EXTRACT(YEAR FROM login_time) = %s "
            "ORDER BY id DESC",
            (month, year),
        )
        rows = cur.fetchall()
        cur.close(); conn.close()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["ID", "Username", "Login Time", "Logout Time", "Duration",
                         "Latitude", "Longitude", "IP", "Predictions", "Images", "Charts"])
        for row in rows:
            writer.writerow(row)

        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=login_logs_{year}_{month:02d}.csv"},
        )
    except Exception as e:
        print(f"[EXPORT ERROR] {e}")
        return f"Error: {e}", 500


# ── Admin Management ──────────────────────────────────────────────────────────
def _confirm_current_password(password):
    admin_user = session.get("admin_user", "")
    return _try_ldap_login(admin_user, password)


def _admins_page(error=None, success=None):
    # Estate diambil lewat AJAX (endpoint /batch/estates yang sudah ada) supaya halaman
    # Admins tidak ikut menunggu proses QGIS subprocess tiap kali dibuka.
    return render_template("admins.html", active="admins", admins=_get_admin_full_list(),
                           all_menus=DASHBOARD_MENUS, error=error, success=success)


def _role_menus_estates_from_form(f):
    role = f.get("role", "user").strip()
    if role not in ("admin", "user"):
        role = "user"
    menus = ",".join(f.getlist("menus")) if role == "user" else ""
    if role == "user" and f.get("estate_mode", "all") == "custom":
        estates = ",".join(f.getlist("estates")) or "ALL"
    else:
        estates = "ALL"
    return role, menus, estates


@app.route(DASHBOARD_PREFIX + "/admins")
@admin_required
def dashboard_admins():
    return _admins_page()


@app.route(DASHBOARD_PREFIX + "/admins/add", methods=["POST"])
@admin_required
def dashboard_admins_add():
    f = request.form
    if not _confirm_current_password(f.get("confirm_password", "")):
        return _admins_page(error="Password konfirmasi salah. Gagal menambahkan admin.")
    new_admin = f.get("username", "").strip()
    if not new_admin:
        return redirect(url_for("dashboard_admins"))
    role, menus, estates = _role_menus_estates_from_form(f)
    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO dashboard_admins (username, role, allowed_menus, allowed_estates) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (username) DO UPDATE SET "
                "role=EXCLUDED.role, allowed_menus=EXCLUDED.allowed_menus, allowed_estates=EXCLUDED.allowed_estates",
                (new_admin, role, menus, estates))
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[ADMIN ADD ERROR] {e}")
    return redirect(url_for("dashboard_admins"))


@app.route(DASHBOARD_PREFIX + "/admins/edit", methods=["POST"])
@admin_required
def dashboard_admins_edit():
    f = request.form
    if not _confirm_current_password(f.get("confirm_password", "")):
        return _admins_page(error="Password konfirmasi salah. Gagal mengubah admin.")
    target = f.get("username", "").strip()
    if not target:
        return redirect(url_for("dashboard_admins"))
    role, menus, estates = _role_menus_estates_from_form(f)
    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE dashboard_admins SET role=%s, allowed_menus=%s, allowed_estates=%s WHERE username=%s",
                (role, menus, estates, target))
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[ADMIN EDIT ERROR] {e}")
    if target == session.get("admin_user"):
        # Update session langsung supaya perubahan berlaku tanpa perlu logout/login lagi
        session["admin_role"] = role
        session["admin_menus"] = _csv_to_list(menus)
        session["admin_estates"] = _csv_to_list(estates) or ["ALL"]
    return redirect(url_for("dashboard_admins"))


@app.route(DASHBOARD_PREFIX + "/admins/delete", methods=["POST"])
@admin_required
def dashboard_admins_delete():
    f = request.form
    if not _confirm_current_password(f.get("confirm_password", "")):
        return _admins_page(error="Password konfirmasi salah. Gagal menghapus admin.")
    target = f.get("username", "").strip()
    admin_user = session.get("admin_user", "")
    if target == admin_user:
        return _admins_page(error="Tidak bisa menghapus diri sendiri.")
    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM dashboard_admins WHERE username = %s", (target,))
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[ADMIN DELETE ERROR] {e}")
    return redirect(url_for("dashboard_admins"))


# ── Settings (super admin only) ───────────────────────────────────────────────
@app.route(DASHBOARD_PREFIX + "/settings")
@admin_required
def dashboard_settings():
    if session.get("admin_user") != SUPER_ADMIN:
        return redirect(url_for("dashboard_overview"))
    return render_template("settings.html", active="settings",
                           current_prefix=DASHBOARD_PREFIX, super_admin=SUPER_ADMIN)


@app.route(DASHBOARD_PREFIX + "/settings/change-url", methods=["POST"])
@admin_required
def dashboard_settings_change_url():
    if session.get("admin_user") != SUPER_ADMIN:
        return redirect(url_for("dashboard_overview"))
    f = request.form
    confirm_pass = f.get("confirm_password", "")
    if not _try_ldap_login(SUPER_ADMIN, confirm_pass):
        return render_template("settings.html", active="settings",
                               current_prefix=DASHBOARD_PREFIX, super_admin=SUPER_ADMIN,
                               error="Password salah.")
    new_prefix = f.get("new_prefix", "").strip()
    if not new_prefix.startswith("/"):
        new_prefix = "/" + new_prefix
    new_prefix = "/" + new_prefix.strip("/")
    if _set_dashboard_prefix(new_prefix):
        return render_template("settings.html", active="settings",
                               current_prefix=DASHBOARD_PREFIX, super_admin=SUPER_ADMIN,
                               success=f"URL berhasil diubah ke {new_prefix}. Restart server untuk menerapkan.")
    return render_template("settings.html", active="settings",
                           current_prefix=DASHBOARD_PREFIX, super_admin=SUPER_ADMIN,
                           error="Gagal menyimpan ke database.")


# ── Batch Deteksi TM ───────────────────────────────────────────────────────────
def _safe_batch_path(user_path, root):
    """Cegah path traversal — pastikan path hasil tetap di dalam root."""
    candidate = os.path.abspath(os.path.join(root, user_path or ""))
    root_abs = os.path.abspath(root)
    if os.path.commonpath([candidate, root_abs]) != root_abs:
        return None
    return candidate


@app.route(DASHBOARD_PREFIX + "/batch")
@admin_required
def dashboard_batch():
    conn = get_db()
    jobs = []
    if conn:
        try:
            cur = conn.cursor()
            allowed_estates = _get_allowed_estates()
            base_sql = """
                SELECT id, created_by, created_at, input_path, estate_splitter, output_folder,
                       status, current_stage, stage_progress, tiles_total, tiles_done,
                       result_sick, result_healthy, result_shapefile_path, error_message,
                       rotasi, bulan, tahun, result_rekap_shapefile_path, result_rekap_excel_path,
                       started_at, finished_at
                FROM batch_jobs
            """
            if allowed_estates is None:
                cur.execute(base_sql + " ORDER BY id DESC")
            else:
                cur.execute(base_sql + " WHERE estate_splitter = ANY(%s) ORDER BY id DESC", (allowed_estates,))
            jobs = cur.fetchall()
            cur.close(); conn.close()
        except Exception as e:
            print(f"[BATCH LIST ERROR] {e}")
    return render_template("batch.html", active="batch", jobs=jobs, output_default=BATCH_OUTPUT_DEFAULT)


@app.route(DASHBOARD_PREFIX + "/batch/estates")
@admin_required
def dashboard_batch_estates():
    return jsonify({"estates": _get_estate_list_via_qgis()})


@app.route(DASHBOARD_PREFIX + "/batch/upload", methods=["POST"])
@admin_required
def dashboard_batch_upload():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"success": False, "message": "Tidak ada file"}), 400
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_BATCH_EXTENSIONS:
        return jsonify({"success": False, "message": f"Ekstensi {ext} tidak diizinkan"}), 400
    filename = secure_filename(file.filename)
    save_path = os.path.join(BATCH_INCOMING, filename)
    file.save(save_path)
    return jsonify({"success": True, "path": save_path})


MAX_SPLITTER_ZIP_MB = int(os.environ.get("AGRIPALM_MAX_SPLITTER_ZIP_MB", "20"))


def _safe_extract_zip(zip_path, dest_dir):
    """Extract zip ke dest_dir dengan guard zip-slip — tolak entry mana pun yang hasil
    join-nya keluar dari dest_dir (pola sama dengan _safe_batch_path di atas)."""
    dest_abs = os.path.abspath(dest_dir)
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            target = os.path.abspath(os.path.join(dest_abs, member))
            if os.path.commonpath([target, dest_abs]) != dest_abs:
                raise ValueError(f"Entry zip tidak aman: {member}")
        zf.extractall(dest_abs)


@app.route(DASHBOARD_PREFIX + "/batch/upload_splitter", methods=["POST"])
@app.route(DASHBOARD_PREFIX + "/custom/upload_splitter", methods=["POST"], endpoint="dashboard_custom_upload_splitter")
@app.route(DASHBOARD_PREFIX + "/tbm/upload_splitter", methods=["POST"], endpoint="dashboard_tbm_upload_splitter")
@app.route(DASHBOARD_PREFIX + "/treecounting/upload_splitter", methods=["POST"], endpoint="dashboard_treecounting_upload_splitter")
@app.route(DASHBOARD_PREFIX + "/homogenitas/upload_splitter", methods=["POST"], endpoint="dashboard_homogenitas_upload_splitter")
@admin_required
def dashboard_batch_upload_splitter():
    """Upload SHP splitter milik user sendiri (zip berisi .shp/.shx/.dbf/.prj) — dipakai
    sekali pakai untuk 1 job, bukan splitter_estate.geojson global. Disimpan sementara
    sebagai bytea di batch_splitter_uploads (tabel generic, dipakai bareng oleh Batch TM
    DAN custom_batch_jobs -- lihat splitter_upload_id di _ensure_custom_batch_jobs_table),
    dihapus otomatis begitu job yang memakainya selesai/dibersihkan."""
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"success": False, "message": "Tidak ada file"}), 400
    if os.path.splitext(file.filename)[1].lower() != ".zip":
        return jsonify({"success": False, "message": "File harus berformat .zip (berisi .shp/.shx/.dbf/.prj)"}), 400

    zip_bytes = file.read()
    if len(zip_bytes) > MAX_SPLITTER_ZIP_MB * 1024 * 1024:
        return jsonify({"success": False, "message": f"Ukuran file melebihi {MAX_SPLITTER_ZIP_MB}MB"}), 400

    tmp_dir = tempfile.mkdtemp(prefix="splitter_upload_")
    try:
        tmp_zip_path = os.path.join(tmp_dir, "upload.zip")
        with open(tmp_zip_path, "wb") as f:
            f.write(zip_bytes)

        extract_dir = os.path.join(tmp_dir, "extracted")
        try:
            _safe_extract_zip(tmp_zip_path, extract_dir)
        except (zipfile.BadZipFile, ValueError) as e:
            return jsonify({"success": False, "message": f"Zip tidak valid: {e}"}), 400

        shp_candidates = glob.glob(os.path.join(extract_dir, "**", "*.shp"), recursive=True)
        if not shp_candidates:
            return jsonify({"success": False, "message": "Tidak ada file .shp di dalam zip"}), 400

        try:
            import geopandas as gpd
            gdf = gpd.read_file(shp_candidates[0])
        except Exception as e:
            return jsonify({"success": False, "message": f"Gagal membaca shapefile: {e}"}), 400

        if len(gdf) == 0:
            return jsonify({"success": False, "message": "Shapefile kosong (tidak ada polygon)"}), 400
        geom_types = set(gdf.geometry.geom_type.unique())
        if not geom_types.issubset({"Polygon", "MultiPolygon"}):
            return jsonify({"success": False, "message": f"Shapefile harus berisi Polygon/MultiPolygon, ditemukan: {', '.join(geom_types)}"}), 400

        upload_id = uuid.uuid4().hex
        conn = get_db()
        if not conn:
            return jsonify({"success": False, "message": "Tidak bisa konek ke database"}), 500
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO batch_splitter_uploads (upload_id, uploaded_by, original_filename, zip_data) "
                "VALUES (%s, %s, %s, %s)",
                (upload_id, session.get("admin_user"), secure_filename(file.filename), psycopg2.Binary(zip_bytes)),
            )
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[SPLITTER UPLOAD ERROR] {e}")
            return jsonify({"success": False, "message": str(e)}), 500

        return jsonify({"success": True, "upload_id": upload_id, "block_count": len(gdf)})
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# Drive lokal di server ini sebenarnya adalah network drive yang di-map dari server FTP
# terpisah — tampilkan alamat network aslinya di file browser, bukan cuma huruf drive,
# supaya user tahu file itu fisiknya ada di mana. Drive yang belum terdaftar di sini
# diberi label fallback "data" (bukan alamat IP spesifik).
DRIVE_UNC_MAP = {
    "Y": r"\\192.168.7.250",
    "Z": r"\\192.168.7.252",
    # D: di server penyimpanan (BGAWKS-GIS6/Server 1) -- share BIASA bernama "d" yang
    # sudah dibagikan ke akun non-admin (mis. muhammad.aji), BUKAN admin share bawaan
    # Windows (D$). Sempat salah tebak pakai D$ (lihat riwayat _localize_output_path
    # di bawah) -- job 51/52 (2026-08-17/18) gagal cepat & bersih dengan
    # PermissionError karena akun servicenya memang bukan admin di server 1, admin
    # share SELALU menolak akun non-admin apapun pun password-nya benar. Pakai
    # hostname (bukan IP) atas permintaan eksplisit user karena tidak pernah berubah.
    "D": r"\\BGAWKS-GIS6\d",
}
DRIVE_UNC_FALLBACK = "data"


def _to_unc_path(full_path):
    """Konversi path lokal (mis. Y:\\Estate1\\foto.tif) ke alamat network aslinya
    (mis. \\\\192.168.7.250\\Estate1\\foto.tif) berdasarkan DRIVE_UNC_MAP."""
    drive, rest = os.path.splitdrive(full_path)
    drive_letter = drive.rstrip(":\\").upper()
    prefix = DRIVE_UNC_MAP.get(drive_letter, DRIVE_UNC_FALLBACK)
    rest = rest.strip("\\/")
    return os.path.join(prefix, rest) if rest else prefix


def _localize_output_path(path):
    """Pastikan output_folder job SELALU berupa UNC path yang bisa ditulis dari server
    manapun (server 1 ATAU server 2), bukan path lokal yang cuma valid di server yang
    kebetulan me-render file browser (selalu server 1).

    - Path UNC (\\\\server\\...) dibiarkan apa adanya -- sudah network-reachable.
    - Drive yang terdaftar di DRIVE_UNC_MAP (Y:/Z:/D:) pakai UNC aslinya.
    - Drive lain yang BELUM terdaftar: sengaja dibiarkan apa adanya (path lokal),
      TIDAK ditebak lagi ke admin share ($) seperti sebelumnya -- itu asumsi yang
      terbukti salah (service account bukan admin di server manapun). Kalau ada
      drive baru yang perlu ditulis lintas-server, daftarkan share network-nya
      dulu di DRIVE_UNC_MAP di atas, jangan andalkan tebakan otomatis di sini."""
    if path.startswith("\\\\"):
        return path
    drive, rest = os.path.splitdrive(path)
    drive_letter = drive.rstrip(":\\").upper()
    rest = rest.strip("\\/")
    if drive_letter not in DRIVE_UNC_MAP:
        return path
    prefix = DRIVE_UNC_MAP[drive_letter]
    return os.path.join(prefix, rest) if rest else prefix


# Network share yang di-browse langsung via UNC path (BUKAN lewat huruf drive Y:/Z:) —
# drive letter itu hasil "Map Network Drive" di sesi login interaktif user, dan Windows
# TIDAK mewariskan mapping itu ke proses service (AgripalmAPIServer jalan di sesi
# terpisah/Session 0), jadi GetLogicalDrives() di service tidak akan pernah melihatnya
# walau di File Explorer kelihatan ada. UNC path tidak tergantung sesi/login siapapun,
# jadi selalu bisa dibaca oleh service asal akun service-nya punya akses ke share ini.
NETWORK_BROWSE_ROOTS = [
    {"name": "FTP_Data (\\\\192.168.7.250\\data\\FTP_Data)", "full_path": r"\\192.168.7.250\data\FTP_Data"},
    {"name": "gis (\\\\192.168.7.252)", "full_path": r"\\192.168.7.252"},
]

# _ensure_network_share_connected() SEKARANG didefinisikan di batch_worker.py
# (bukan di sini) — worker (bukan cuma file browser ini) yang benar-benar buka
# file raster lewat GDAL dari UNC path, jadi 1 titik definisi dipakai bersama
# lewat import lazy di dashboard_batch_browse() di bawah supaya cache koneksinya
# (_connected_share_servers) juga ke-share, tidak connect dobel per modul.


@app.route(DASHBOARD_PREFIX + "/batch/browse")
@admin_required
def dashboard_batch_browse():
    """Browse seluruh filesystem server (admin only). Path kosong = tampilkan daftar drive."""
    target_path = request.args.get("path", "").strip()

    if not target_path:
        drives = []
        try:
            import ctypes
            bitmask = ctypes.windll.kernel32.GetLogicalDrives()
            for i in range(26):
                if bitmask & (1 << i):
                    drive = f"{chr(65 + i)}:\\"
                    drives.append({"name": drive, "is_dir": True, "full_path": drive, "alamat_asli": _to_unc_path(drive)})
        except Exception as e:
            print(f"[DRIVE LIST ERROR] {e}")
        # Network share (Y:/Z: dkk) tidak akan pernah muncul dari GetLogicalDrives() di atas
        # (lihat komentar NETWORK_BROWSE_ROOTS) — tambahkan manual via UNC path langsung.
        for share in NETWORK_BROWSE_ROOTS:
            drives.append({"name": share["name"], "is_dir": True, "full_path": share["full_path"],
                           "alamat_asli": share["full_path"]})
        return jsonify({"path": "", "full_path": "", "parent_path": None, "entries": drives})

    import batch_worker
    batch_worker._ensure_network_share_connected(target_path)

    if not os.path.isdir(target_path):
        return jsonify({"error": "Folder tidak ditemukan"}), 400

    # Filter ekstensi bisa dioverride lewat ?ext=shp,xlsx (dipisah koma) — default raster
    # (dipakai file browser pilih citra input di TM/Custom/TBM/TreeCounting/RAW Foto).
    ext_param = request.args.get("ext", "").strip()
    allowed_ext = {"." + e.strip(".").lower() for e in ext_param.split(",") if e.strip()} \
        if ext_param else ALLOWED_BATCH_EXTENSIONS

    entries = []
    try:
        names = sorted(os.listdir(target_path), key=lambda n: (not os.path.isdir(os.path.join(target_path, n)), n.lower()))
        for name in names:
            full = os.path.join(target_path, name)
            is_dir = os.path.isdir(full)
            if not is_dir and os.path.splitext(name)[1].lower() not in allowed_ext:
                continue
            entries.append({"name": name, "is_dir": is_dir, "full_path": full, "alamat_asli": _to_unc_path(full)})
    except PermissionError:
        return jsonify({"error": "Akses ditolak ke folder ini"}), 403
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    normalized = os.path.normpath(target_path)
    parent = os.path.dirname(normalized)
    parent_path = "" if parent == normalized else parent

    return jsonify({"path": target_path, "full_path": target_path, "full_path_unc": _to_unc_path(target_path),
                     "parent_path": parent_path, "entries": entries})


@app.route(DASHBOARD_PREFIX + "/batch/submit", methods=["POST"])
@admin_required
def dashboard_batch_submit():
    f = request.form
    input_path = f.get("input_path", "").strip()
    estate = f.get("estate_splitter", "").strip() or None
    splitter_upload_id = f.get("splitter_upload_id", "").strip() or None
    output_folder = _localize_output_path(f.get("output_folder", "").strip() or BATCH_OUTPUT_DEFAULT)
    rotasi = f.get("rotasi", "").strip() or None
    bulan = f.get("bulan", "").strip() or None
    tahun = f.get("tahun", "").strip() or None
    do_rekap = f.get("do_rekap", "1").strip() not in ("0", "false", "False", "")

    if not input_path or not os.path.exists(input_path):
        return jsonify({"success": False, "message": "File input tidak ditemukan"}), 400

    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            if splitter_upload_id:
                cur.execute("SELECT 1 FROM batch_splitter_uploads WHERE upload_id = %s", (splitter_upload_id,))
                if not cur.fetchone():
                    cur.close(); conn.close()
                    return jsonify({"success": False, "message": "Upload SHP splitter tidak ditemukan/sudah kedaluwarsa — upload ulang."}), 400
            cur.execute(
                "INSERT INTO batch_jobs (created_by, input_path, estate_splitter, splitter_upload_id, "
                "output_folder, rotasi, bulan, tahun, do_rekap, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')",
                (session.get("admin_user"), input_path, estate, splitter_upload_id, output_folder,
                 rotasi, bulan, tahun, do_rekap),
            )
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[BATCH SUBMIT ERROR] {e}")
            return jsonify({"success": False, "message": str(e)}), 500
    return jsonify({"success": True})


@app.route(DASHBOARD_PREFIX + "/batch/status")
@admin_required
def dashboard_batch_status():
    conn = get_db()
    jobs = []
    if conn:
        try:
            cur = conn.cursor()
            allowed_estates = _get_allowed_estates()
            base_sql = """
                SELECT id, input_path, estate_splitter, status, current_stage, stage_progress,
                       tiles_total, tiles_done, result_sick, result_healthy,
                       result_shapefile_path, error_message,
                       result_rekap_shapefile_path, result_rekap_excel_path, stage_detail,
                       started_at, finished_at
                FROM batch_jobs
            """
            if allowed_estates is None:
                cur.execute(base_sql + " ORDER BY id DESC")
            else:
                cur.execute(base_sql + " WHERE estate_splitter = ANY(%s) ORDER BY id DESC", (allowed_estates,))
            for row in cur.fetchall():
                jobs.append({
                    "id": row[0], "input_path": row[1], "estate_splitter": row[2],
                    "status": row[3], "current_stage": row[4], "stage_progress": row[5],
                    "tiles_total": row[6], "tiles_done": row[7],
                    "result_sick": row[8], "result_healthy": row[9],
                    "result_shapefile_path": row[10], "error_message": row[11],
                    "result_rekap_shapefile_path": row[12], "result_rekap_excel_path": row[13],
                    "stage_detail": row[14],
                    "started_at": row[15].isoformat() if row[15] else None,
                    "finished_at": row[16].isoformat() if row[16] else None,
                })
            cur.close(); conn.close()
        except Exception as e:
            print(f"[BATCH STATUS ERROR] {e}")
    return jsonify({"jobs": jobs})


@app.route(DASHBOARD_PREFIX + "/batch/download/<int:job_id>")
@admin_required
def dashboard_batch_download(job_id):
    """type=merged (default, vektor gabungan) | rekap_shp (vektor rekap+peningkatan) | rekap_excel"""
    file_type = request.args.get("type", "merged")
    column_map = {
        "merged": "result_shapefile_path",
        "rekap_shp": "result_rekap_shapefile_path",
        "rekap_excel": "result_rekap_excel_path",
    }
    column = column_map.get(file_type)
    if not column:
        return "Jenis file tidak dikenal", 400

    conn = get_db()
    result_path = None
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT {column} FROM batch_jobs WHERE id = %s AND status = 'done'", (job_id,))
            row = cur.fetchone()
            cur.close(); conn.close()
            if row:
                result_path = row[0]
        except Exception as e:
            print(f"[BATCH DOWNLOAD ERROR] {e}")
    if not result_path or not os.path.exists(result_path):
        return "Hasil tidak ditemukan", 404

    if file_type == "rekap_excel":
        return Response(
            open(result_path, "rb").read(),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={os.path.basename(result_path)}"},
        )

    import io
    import zipfile
    base = os.path.splitext(result_path)[0]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
            part = base + suffix
            if os.path.exists(part):
                zf.write(part, arcname=os.path.basename(part))
    buf.seek(0)
    return Response(
        buf.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename=batch_job_{job_id}_{file_type}.zip"},
    )


@app.route(DASHBOARD_PREFIX + "/batch/delete/<int:job_id>", methods=["POST"])
@admin_required
def dashboard_batch_delete(job_id):
    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM batch_jobs WHERE id = %s AND status != 'processing'", (job_id,))
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[BATCH DELETE ERROR] {e}")
    return redirect(url_for("dashboard_batch"))


@app.route(DASHBOARD_PREFIX + "/batch/stop/<int:job_id>", methods=["POST"])
@admin_required
def dashboard_batch_stop(job_id):
    """Minta worker berhenti di checkpoint terdekat (antar tile/tahap)."""
    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE batch_jobs SET stop_requested = TRUE WHERE id = %s AND status = 'processing'",
                (job_id,),
            )
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[BATCH STOP ERROR] {e}")
    return redirect(url_for("dashboard_batch"))


@app.route(DASHBOARD_PREFIX + "/batch/force-fail/<int:job_id>", methods=["POST"])
@admin_required
def dashboard_batch_force_fail(job_id):
    """Paksa job 'processing' langsung jadi 'failed', TANPA butuh worker hidup --
    lihat penjelasan lengkap di dashboard_custom_force_fail (pola identik, tabel beda)."""
    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE batch_jobs SET status = 'failed', "
                "error_message = 'Dipaksa gagal oleh admin (worker tidak merespons)' "
                "WHERE id = %s AND status = 'processing'",
                (job_id,),
            )
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[BATCH FORCE FAIL ERROR] {e}")
    return redirect(url_for("dashboard_batch"))


@app.route(DASHBOARD_PREFIX + "/batch/resume/<int:job_id>", methods=["POST"])
@admin_required
def dashboard_batch_resume(job_id):
    """Kembalikan job 'stopped'/'failed' jadi 'pending' — worker akan lanjutkan
    dari tile yang belum selesai (tile yang sudah ada hasilnya otomatis di-skip)."""
    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE batch_jobs SET status = 'pending', stop_requested = FALSE, error_message = NULL "
                "WHERE id = %s AND status IN ('stopped', 'failed')",
                (job_id,),
            )
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[BATCH RESUME ERROR] {e}")
    return redirect(url_for("dashboard_batch"))


@app.route(DASHBOARD_PREFIX + "/batch/analyze/<int:job_id>")
@admin_required
def dashboard_batch_analyze(job_id):
    conn = get_db()
    job = None
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, created_by, created_at, input_path, estate_splitter,
                       status, rotasi, bulan, tahun, result_sick, result_healthy,
                       result_shapefile_path, result_rekap_shapefile_path,
                       result_rekap_excel_path, tiles_total, finished_at
                FROM batch_jobs WHERE id = %s AND status = 'done'
            """, (job_id,))
            row = cur.fetchone()
            cur.close(); conn.close()
            if row:
                job = {
                    "id": row[0], "created_by": row[1], "created_at": row[2],
                    "input_path": row[3], "estate_splitter": row[4],
                    "status": row[5], "rotasi": row[6], "bulan": row[7], "tahun": row[8],
                    "result_sick": row[9], "result_healthy": row[10],
                    "result_shapefile_path": row[11],
                    "result_rekap_shapefile_path": row[12],
                    "result_rekap_excel_path": row[13],
                    "tiles_total": row[14], "finished_at": row[15],
                }
        except Exception as e:
            print(f"[ANALYZE ERROR] {e}")
    if not job:
        return redirect(url_for("dashboard_batch"))
    return render_template("batch_analyze.html", active="batch", job=job)


@app.route(DASHBOARD_PREFIX + "/batch/analyze/<int:job_id>/data")
@admin_required
def dashboard_batch_analyze_data(job_id):
    conn = get_db()
    row = None
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT result_rekap_excel_path, result_sick, result_healthy,
                       estate_splitter, rotasi, bulan, tahun, tiles_total
                FROM batch_jobs WHERE id = %s AND status = 'done'
            """, (job_id,))
            row = cur.fetchone()
            cur.close(); conn.close()
        except Exception as e:
            print(f"[ANALYZE DATA ERROR] {e}")
    if not row or not row[0] or not os.path.exists(row[0]):
        return jsonify({"error": "Data rekap Excel tidak tersedia"}), 404

    excel_path, result_sick, result_healthy, estate, rotasi, bulan, tahun, tiles_total = row
    try:
        import pandas as pd

        # Auto-detect header row: cari baris yang mengandung 'No_Blok' atau 'KESEHATAN'
        # (generate_rekap_kesehatan lama menulis 3 baris metadata sebelum header)
        df_raw = pd.read_excel(excel_path, header=None)
        header_row = 0
        for i, row in df_raw.iterrows():
            vals = [str(v).strip() for v in row.values]
            if "No_Blok" in vals or "KESEHATAN" in vals:
                header_row = i
                break
        df = pd.read_excel(excel_path, header=header_row)
        df.columns = [str(c).upper().strip() for c in df.columns]
        # Buang kolom unnamed (artefak index Excel)
        df = df.loc[:, ~df.columns.str.startswith("UNNAMED")]

        all_cols = list(df.columns)

        # ── Flexible column matching ─────────────────────────────────────
        def _find_col(*candidates, contains=None):
            for c in candidates:
                if c in all_cols:
                    return c
            if contains:
                return next((c for c in all_cols if contains in c), None)
            return None

        kesehatan_col   = _find_col("KESEHATAN", "KELAS", "STATUS_KESEHATAN", contains="KESEHATAN")
        ha_col          = _find_col("HA", "LUAS_HA", "TOTAL_HA", "LUAS", "LUAS_TANAM",
                                    "LUAS_KEBUN", "LUAS_BLOK", contains="HA")
        if not ha_col:
            ha_col = next((c for c in all_cols if "LUAS" in c), None)
        divisi_col      = _find_col("DIVISI", "DIV", contains="DIVISI")
        pkk_col         = _find_col("PKK", "TOTAL_PKK", contains="PKK")
        jenis_tanah_col = _find_col("JENIS_TANAH", "TNH_GNR", "TANAH", "SOIL", contains="TANAH")
        tahun_tanam_col = _find_col("TAHUN_TANAM", "TAHUN_TANA", "THN_TANAM", "TAHUN", contains="TANAM")

        # ── Aggregasi utama ───────────────────────────────────────────────
        def _agg(col_grp, col_sum):
            if not col_grp or not col_sum:
                return {}
            try:
                grp = df.groupby(col_grp)[col_sum].sum()
                return {str(k): round(float(v), 2) for k, v in grp.items() if str(k) not in ("nan", "")}
            except Exception:
                return {}

        kesehatan_dist = _agg(kesehatan_col, ha_col)
        divisi_data    = dict(list({
            k: v for k, v in sorted(
                _agg(divisi_col, ha_col).items(), key=lambda x: x[1], reverse=True
            )
        }.items())[:15])

        # ── Kesehatan per Jenis Tanah (stacked bar) ───────────────────────
        jenis_tanah_data = {}
        if kesehatan_col and ha_col and jenis_tanah_col:
            try:
                pivot = df.groupby([jenis_tanah_col, kesehatan_col])[ha_col]\
                           .sum().unstack(fill_value=0).round(2)
                jenis_tanah_data = {
                    "soil_types":           [str(s) for s in pivot.index if str(s) not in ("nan", "")],
                    "kesehatan_categories": [str(k) for k in pivot.columns],
                    "matrix": {
                        str(soil): {str(kes): round(float(val), 2) for kes, val in row_s.items()}
                        for soil, row_s in pivot.iterrows() if str(soil) not in ("nan", "")
                    },
                }
            except Exception:
                pass

        # ── Need Improvement Soon per Umur Tanaman ───────────────────────
        nis_per_umur = {}
        if kesehatan_col and ha_col and tahun_tanam_col:
            try:
                from datetime import date
                cur_year = date.today().year
                nis_df = df[df[kesehatan_col].astype(str).str.upper().str.contains("SOON", na=False)].copy()
                nis_df["_UMUR"] = cur_year - pd.to_numeric(nis_df[tahun_tanam_col], errors="coerce")
                nis_df = nis_df.dropna(subset=["_UMUR"])
                nis_df["_UMUR"] = nis_df["_UMUR"].astype(int)
                grp = nis_df.groupby("_UMUR")[ha_col].sum().sort_index()
                nis_per_umur = {f"{k} thn": round(float(v), 2) for k, v in grp.items()}
            except Exception:
                pass

        # ── Need Improvement (bukan Soon) per Umur Tanaman ───────────────
        ni_per_umur = {}
        if kesehatan_col and ha_col and tahun_tanam_col:
            try:
                from datetime import date as _date
                cur_year = _date.today().year
                mask_ni = (
                    df[kesehatan_col].astype(str).str.upper().str.contains("IMPROVEMENT", na=False) &
                    ~df[kesehatan_col].astype(str).str.upper().str.contains("SOON", na=False)
                )
                ni_df = df[mask_ni].copy()
                ni_df["_UMUR"] = cur_year - pd.to_numeric(ni_df[tahun_tanam_col], errors="coerce")
                ni_df = ni_df.dropna(subset=["_UMUR"])
                ni_df["_UMUR"] = ni_df["_UMUR"].astype(int)
                grp = ni_df.groupby("_UMUR")[ha_col].sum().sort_index()
                ni_per_umur = {f"{k} thn": round(float(v), 2) for k, v in grp.items()}
            except Exception:
                pass

        # ── Turunan kesehatan_dist: green/ni/nis/total HA ─────────────────
        green_ha, ni_ha, nis_ha = 0.0, 0.0, 0.0
        for kes, val in kesehatan_dist.items():
            ku = kes.upper()
            if "SOON" in ku:
                nis_ha += val
            elif "IMPROVEMENT" in ku:
                ni_ha += val
            elif "GREEN" in ku:
                green_ha += val
        total_ha = sum(kesehatan_dist.values())

        # ── Kesehatan per divisi (matrix untuk mini pie per divisi) ───────
        divisi_health_data = {}
        if divisi_col and kesehatan_col and ha_col:
            try:
                piv = df.groupby([divisi_col, kesehatan_col])[ha_col].sum().unstack(fill_value=0).round(2)
                divisi_health_data = {
                    "divisions": [str(d) for d in piv.index if str(d) not in ("nan", "")],
                    "kesehatan_categories": [str(k) for k in piv.columns],
                    "matrix": {
                        str(d): {str(k): round(float(v), 2) for k, v in row_d.items()}
                        for d, row_d in piv.iterrows() if str(d) not in ("nan", "")
                    },
                }
            except Exception:
                pass

        # ── Perlu perhatian (PKK sum) ─────────────────────────────────────
        total_det = (result_sick or 0) + (result_healthy or 0)
        perlu_perhatian = result_sick or 0
        if pkk_col and kesehatan_col:
            try:
                ni_df = df[df[kesehatan_col].astype(str).str.upper().str.contains("IMPROVEMENT", na=False)]
                perlu_perhatian = int(ni_df[pkk_col].fillna(0).astype(float).sum())
                if total_det == 0:
                    total_det = int(df[pkk_col].fillna(0).astype(float).sum())
            except Exception:
                pass

        table_rows = df.fillna("").astype(str).to_dict(orient="records")
        return jsonify({
            "total_det":          total_det,
            "perlu_perhatian":    perlu_perhatian,
            "result_sick":        result_sick or 0,
            "result_healthy":     result_healthy or 0,
            "tiles_total":        tiles_total,
            "estate": estate, "rotasi": rotasi, "bulan": bulan, "tahun": tahun,
            "green_ha":           round(green_ha, 2),
            "ni_ha":              round(ni_ha, 2),
            "nis_ha":             round(nis_ha, 2),
            "total_ha":           round(total_ha, 2),
            "columns":            all_cols,
            "kesehatan_dist":     kesehatan_dist,
            "jenis_tanah_data":   jenis_tanah_data,
            "nis_per_umur":       nis_per_umur,
            "ni_per_umur":        ni_per_umur,
            "divisi_health_data": divisi_health_data,
            "table_rows":         table_rows,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route(DASHBOARD_PREFIX + "/batch/analyze/<int:job_id>/geojson")
@admin_required
def dashboard_batch_analyze_geojson(job_id):
    # Cari SHP yang bisa dipakai — rekap_shp (polygon per blok) atau fallback ke Excel path / merged detection
    conn2 = get_db()
    shp_to_use = None
    opsi_used = None
    if conn2:
        try:
            cur2 = conn2.cursor()
            cur2.execute(
                "SELECT result_rekap_shapefile_path, result_rekap_excel_path, result_shapefile_path "
                "FROM batch_jobs WHERE id = %s AND status = 'done'", (job_id,)
            )
            r2 = cur2.fetchone()
            cur2.close(); conn2.close()
            if r2:
                rekap_shp, rekap_xlsx, merged_shp = r2
                print(f"[GEOJSON job={job_id}] rekap_shp={rekap_shp!r}")
                print(f"[GEOJSON job={job_id}] rekap_xlsx={rekap_xlsx!r}")
                print(f"[GEOJSON job={job_id}] merged_shp={merged_shp!r}")
                # Opsi 1: rekap SHP tersimpan dan ada
                if rekap_shp:
                    exists1 = os.path.exists(rekap_shp)
                    print(f"[GEOJSON job={job_id}] Opsi1 exists={exists1}: {rekap_shp!r}")
                    if exists1:
                        shp_to_use = rekap_shp; opsi_used = "1-rekap_shp"
                # Opsi 2: derive dari Excel path (nama sama, ekstensi .shp)
                if not shp_to_use and rekap_xlsx:
                    derived = os.path.splitext(rekap_xlsx)[0] + ".shp"
                    exists2 = os.path.exists(derived)
                    print(f"[GEOJSON job={job_id}] Opsi2 exists={exists2}: {derived!r}")
                    if exists2:
                        shp_to_use = derived; opsi_used = "2-derived"
                # Opsi 3: merged detection SHP (titik deteksi)
                if not shp_to_use and merged_shp:
                    exists3 = os.path.exists(merged_shp)
                    print(f"[GEOJSON job={job_id}] Opsi3 exists={exists3}: {merged_shp!r}")
                    if exists3:
                        shp_to_use = merged_shp; opsi_used = "3-merged_det"
            else:
                print(f"[GEOJSON job={job_id}] Job tidak ditemukan di DB (status bukan done?)")
        except Exception as e:
            print(f"[GEOJSON FALLBACK ERROR] {e}")
    print(f"[GEOJSON job={job_id}] shp_to_use={shp_to_use!r} opsi={opsi_used}")
    if not shp_to_use:
        return Response('{"type":"FeatureCollection","features":[]}', mimetype="application/json")
    try:
        import geopandas as gpd
        gdf = gpd.read_file(shp_to_use)
        if gdf.crs is None:
            # CRS tidak dikenali dari .prj — detect dari rentang koordinat
            bounds = gdf.total_bounds  # minx, miny, maxx, maxy
            if abs(bounds[0]) > 180 or abs(bounds[2]) > 180:
                # Koordinat besar → projected (UTM). Indonesia zona 49 paling umum.
                # Deteksi North/South dari nilai Y: UTM South Y > 9_000_000
                epsg = 32749 if bounds[1] > 1_000_000 else 32649
                gdf = gdf.set_crs(epsg=epsg, allow_override=True)
        if gdf.crs and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)
        return Response(gdf.to_json(), mimetype="application/json")
    except Exception as e:
        print(f"[GEOJSON READ ERROR job={job_id}] {e}")
        return jsonify({"error": str(e)}), 500


@app.route(DASHBOARD_PREFIX + "/batch/analyze/<int:job_id>/debug")
@admin_required
def dashboard_batch_analyze_debug(job_id):
    """Debug endpoint: tampilkan path SHP/Excel dari DB dan apakah file ada."""
    conn = get_db()
    if not conn:
        return jsonify({"error": "DB tidak tersambung"}), 500
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT result_rekap_shapefile_path, result_rekap_excel_path, result_shapefile_path, status "
            "FROM batch_jobs WHERE id = %s", (job_id,)
        )
        row = cur.fetchone(); cur.close(); conn.close()
        if not row:
            return jsonify({"error": f"Job {job_id} tidak ditemukan"}), 404
        rekap_shp, rekap_xlsx, merged_shp, status = row
        def check(p):
            if not p:
                return {"path": None, "exists": False}
            return {"path": p, "exists": os.path.exists(p)}
        # Opsi 2: derived SHP dari Excel path
        derived_shp = (os.path.splitext(rekap_xlsx)[0] + ".shp") if rekap_xlsx else None
        return jsonify({
            "job_id": job_id,
            "status": status,
            "opsi1_rekap_shp": check(rekap_shp),
            "opsi2_derived_shp": check(derived_shp),
            "opsi3_merged_shp": check(merged_shp),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route(DASHBOARD_PREFIX + "/batch/log")
@admin_required
def dashboard_batch_log():
    log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "batch_worker.log")
    if not os.path.exists(log_file):
        return Response("Log file belum ada — worker belum pernah jalan.", mimetype="text/plain")
    try:
        n_lines = request.args.get("lines", 300, type=int)
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        content = "".join(lines[-n_lines:])
        return Response(content or "(log kosong)", mimetype="text/plain")
    except Exception as e:
        return Response(f"Gagal membaca log: {e}", mimetype="text/plain"), 500


# ── Rekap Data — batch rekap dari SHP deteksi/rekap yang SUDAH ada (tidak perlu ulang
#    proses deteksi/splitter), sejajar Batch Deteksi TM & Deteksi TBM. Upgrade dari
#    "Rekap Manual" lama: multi-file, job queue background (bukan sinkron 1 file),
#    Blok_ID/Estate diselaraskan ke Aresta (blok_resolver.py — bukan dipercaya dari file
#    atau pilihan user), mode Rekap Ulang untuk data yang sudah di-dissolve
#    (rekap_ulang.py), Bad Image (bad_image.py), validasi/remap kelas asing
#    (class_utils.py). Semua modul itu di-port dari tool desktop "Rekap Batch"
#    (01. Agripalm 1.2\Rekap Batch\) — logikanya identik, sudah diuji di sana. Job-nya
#    lewat tabel & worker yang sama dengan Custom/TBM (custom_batch_jobs,
#    job_type='rekap_only') — lihat custom_batch_worker.py::_process_rekap_only_job. ──


@app.route(DASHBOARD_PREFIX + "/rekap-data")
@admin_required
def dashboard_rekap_data():
    jobs = _custom_family_jobs("rekap_only")
    return render_template("rekap_data.html", active="rekap_data", jobs=jobs,
                           output_default=CUSTOM_OUTPUT_DEFAULT)


@app.route(DASHBOARD_PREFIX + "/rekap-data/status")
@admin_required
def dashboard_rekap_data_status():
    return jsonify({"jobs": _custom_family_status("rekap_only")})


@app.route(DASHBOARD_PREFIX + "/rekap-data/merges/status")
@admin_required
def dashboard_rekap_data_merges_status():
    """Hasil GABUNGAN per estate (lihat merge_and_finalize.py /
    _maybe_merge_estate_group di custom_batch_worker.py) — ini yang totalnya benar,
    beda dari hasil per-file/per-job mentah di /rekap-data/status."""
    conn = get_db()
    merges = []
    if conn:
        try:
            cur = conn.cursor()
            allowed_estates = _get_allowed_estates()
            base_sql = (
                "SELECT id, batch_group_id, estate, rotasi, bulan, tahun, status, "
                "result_shapefile_path, result_excel_path, error_message, "
                "created_at, finished_at FROM rekap_estate_merges"
            )
            conditions, params = [], []
            if allowed_estates is not None:
                conditions.append("estate = ANY(%s)")
                params.append(allowed_estates)
            if conditions:
                base_sql += " WHERE " + " AND ".join(conditions)
            cur.execute(base_sql + " ORDER BY id DESC", params)
            for row in cur.fetchall():
                merges.append({
                    "id": row[0], "batch_group_id": row[1], "estate": row[2],
                    "rotasi": row[3], "bulan": row[4], "tahun": row[5], "status": row[6],
                    "result_shapefile_path": row[7], "result_excel_path": row[8],
                    "error_message": row[9],
                    "created_at": row[10].isoformat() if row[10] else None,
                    "finished_at": row[11].isoformat() if row[11] else None,
                })
            cur.close(); conn.close()
        except Exception as e:
            print(f"[REKAP ESTATE MERGES STATUS ERROR] {e}")
    return jsonify({"merges": merges})


@app.route(DASHBOARD_PREFIX + "/rekap-data/merges/download/<int:merge_id>")
@admin_required
def dashboard_rekap_data_merges_download(merge_id):
    """type=shp (default, di-zip) | excel"""
    file_type = request.args.get("type", "shp")
    column = "result_excel_path" if file_type == "excel" else "result_shapefile_path"

    conn = get_db()
    result_path = None
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                f"SELECT {column} FROM rekap_estate_merges WHERE id = %s AND status = 'done'",
                (merge_id,),
            )
            row = cur.fetchone()
            cur.close(); conn.close()
            if row:
                result_path = row[0]
        except Exception as e:
            print(f"[REKAP ESTATE MERGES DOWNLOAD ERROR] {e}")
    if not result_path or not os.path.exists(result_path):
        return "Hasil tidak ditemukan", 404

    if file_type == "excel":
        return Response(
            open(result_path, "rb").read(),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={os.path.basename(result_path)}"},
        )

    import io
    import zipfile
    base = os.path.splitext(result_path)[0]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
            part = base + suffix
            if os.path.exists(part):
                zf.write(part, arcname=os.path.basename(part))
    buf.seek(0)
    return Response(
        buf.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename=rekap_gabungan_{merge_id}.zip"},
    )


@app.route(DASHBOARD_PREFIX + "/rekap-data/peek")
@admin_required
def dashboard_rekap_data_peek():
    """Dipanggil tiap kali 1 file ditambahkan ke tabel antrian — baca daftar kolomnya
    saja (cepat, 1 baris) supaya frontend bisa isi dropdown Kolom Kelas + tebak Mode
    (Deteksi Mentah / Rekap Ulang) otomatis, sama seperti class_utils.py di desktop."""
    from Processing.rekap_data import class_utils

    path = request.args.get("path", "").strip()
    if not path or not os.path.isfile(path):
        return jsonify({"error": "File tidak ditemukan"}), 400
    columns = class_utils.peek_columns(path)
    mode = class_utils.guess_mode(columns)
    kelas_col = class_utils.guess_class_column(columns, mode)
    return jsonify({"columns": columns, "mode": mode, "kelas_col": kelas_col})


@app.route(DASHBOARD_PREFIX + "/rekap-data/scan-classes", methods=["POST"])
@admin_required
def dashboard_rekap_data_scan_classes():
    """Pre-flight: scan nilai kelas di semua file yang mau diantrikan, kembalikan yang
    di luar kamus dikenal (GREEN/NI/NIS utk mode raw, Green/Need Improvement/dst utk
    mode ulang) supaya frontend bisa tampilkan modal remap SEBELUM submit final —
    setara ClassReviewDialog di tool desktop, versi web."""
    from Processing.rekap_data import class_utils

    rows = request.get_json(silent=True) or {}
    files = rows.get("files") or []
    unknown_counts = {}
    for row in files:
        file_path = (row.get("file") or "").strip()
        kelas_col = (row.get("kelas_col") or "").strip()
        mode = row.get("mode") or "raw"
        if not file_path or not os.path.isfile(file_path) or not kelas_col:
            continue
        found = class_utils.scan_unknown_classes(file_path, kelas_col, mode)
        for value, count in found.items():
            unknown_counts[value] = unknown_counts.get(value, 0) + count
    return jsonify({"unknown_classes": unknown_counts})


@app.route(DASHBOARD_PREFIX + "/rekap-data/submit", methods=["POST"])
@admin_required
def dashboard_rekap_data_submit():
    """Terima daftar baris (file + estate hint + kolom kelas + mode) + parameter
    bersama (rotasi/bulan/tahun/output_folder/class_remap) dari halaman Rekap Data,
    buat 1 row custom_batch_jobs (job_type='rekap_only') per file — diproses satu-satu
    di background oleh worker yang sama dengan Custom/TBM."""
    payload = request.get_json(silent=True) or {}
    rows = payload.get("rows") or []
    rotasi = (payload.get("rotasi") or "").strip()
    bulan = (payload.get("bulan") or "").strip()
    tahun = (payload.get("tahun") or "").strip()
    output_folder = _localize_output_path((payload.get("output_folder") or "").strip() or BATCH_OUTPUT_DEFAULT)
    class_remap = payload.get("class_remap") or {}

    if not rows:
        return jsonify({"success": False, "message": "Belum ada file yang ditambahkan"}), 400

    class_remap_json = json.dumps(class_remap) if class_remap else None
    # 1 batch_group_id per klik "Jalankan Rekap" -- dipakai worker buat tahu job-job
    # mana yang boleh digabung Bad Image-nya sekali (lihat merge_and_finalize.py).
    # rotasi/bulan/tahun SENGAJA disimpan '' (bukan NULL) -- dipakai sebagai kunci
    # pencocokan grup lewat "=" di SQL nanti, dan NULL tidak pernah "=" NULL di SQL.
    batch_group_id = uuid.uuid4().hex
    created = 0
    conn = get_db()
    if not conn:
        return jsonify({"success": False, "message": "Gagal konek DB"}), 500
    try:
        cur = conn.cursor()
        for row in rows:
            file_path = (row.get("file") or "").strip()
            estate_hint = (row.get("estate") or "").strip()
            kelas_col = (row.get("kelas_col") or "").strip()
            mode = row.get("mode") or "raw"
            job_name = (row.get("job_name") or os.path.splitext(os.path.basename(file_path))[0])
            if not file_path or not os.path.isfile(file_path):
                continue
            if not kelas_col:
                continue
            cur.execute(
                "INSERT INTO custom_batch_jobs (created_by, job_name, model_path, "
                "input_path, estate_splitter, output_folder, kelas_col, rekap_mode, "
                "class_remap_json, rotasi, bulan, tahun, batch_group_id, job_type, status) "
                "VALUES (%s, %s, '', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'rekap_only', 'pending')",
                (session.get("admin_user"), job_name, file_path, estate_hint or None,
                 output_folder, kelas_col, mode, class_remap_json,
                 rotasi, bulan, tahun, batch_group_id),
            )
            created += 1
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"[REKAP DATA SUBMIT ERROR] {e}")
        return jsonify({"success": False, "message": str(e)}), 500

    if not created:
        return jsonify({"success": False, "message": "Tidak ada baris valid (file tidak ditemukan / kolom kelas kosong)"}), 400
    return jsonify({"success": True, "created": created})


# ── Deteksi Custom — model & jumlah kelas bebas, diupload sendiri oleh user ────
DEFAULT_CLASS_PALETTE = ["#e67e22", "#3498db", "#27ae60", "#e74c3c", "#9b59b6",
                         "#1abc9c", "#f1c40f", "#95a5a6", "#e84393", "#00b894"]


def _read_model_classes(model_path):
    """Baca daftar kelas dari checkpoint YOLO (.pt) — kelas & jumlahnya milik model itu sendiri,
    jadi jumlah/nama kelas otomatis menyesuaikan model apa pun yang diupload user."""
    from ultralytics import YOLO
    names = YOLO(model_path).names  # {int: str}
    return {str(k): v for k, v in names.items()}


def _custom_family_jobs(job_type):
    """List job dari custom_batch_jobs untuk 1 job_type ('custom' atau 'tbm'),
    sudah difilter estate sesuai role (dipakai bareng oleh Deteksi Custom & Deteksi TBM)."""
    conn = get_db()
    jobs = []
    if conn:
        try:
            cur = conn.cursor()
            allowed_estates = _get_allowed_estates()
            base_sql = """
                SELECT id, job_name, model_filename, class_names, input_path, output_folder,
                       status, current_stage, stage_progress, tiles_total, tiles_done,
                       result_total_detections, result_class_counts, result_geojson_path,
                       result_shapefile_path, result_excel_path, error_message,
                       started_at, finished_at, result_rekap_shapefile_path, result_rekap_excel_path
                FROM custom_batch_jobs
            """
            conditions = ["job_type = %s"]
            params = [job_type]
            if allowed_estates is not None:
                conditions.append("estate_splitter = ANY(%s)")
                params.append(allowed_estates)
            cur.execute(base_sql + " WHERE " + " AND ".join(conditions) + " ORDER BY id DESC", params)
            jobs = cur.fetchall()
            cur.close(); conn.close()
        except Exception as e:
            print(f"[{job_type.upper()} LIST ERROR] {e}")
    return jobs


@app.route(DASHBOARD_PREFIX + "/custom")
@admin_required
def dashboard_custom():
    jobs = _custom_family_jobs("custom")
    return render_template("custom_batch.html", active="custom_batch", jobs=jobs,
                           output_default=CUSTOM_OUTPUT_DEFAULT)


@app.route(DASHBOARD_PREFIX + "/tbm")
@admin_required
def dashboard_tbm():
    jobs = _custom_family_jobs("tbm")
    return render_template("tbm_batch.html", active="tbm", jobs=jobs,
                           output_default=CUSTOM_OUTPUT_DEFAULT)


@app.route(DASHBOARD_PREFIX + "/treecounting")
@admin_required
def dashboard_treecounting():
    jobs = _custom_family_jobs("treecounting")
    return render_template("treecounting.html", active="treecounting", jobs=jobs,
                           output_default=CUSTOM_OUTPUT_DEFAULT)


@app.route(DASHBOARD_PREFIX + "/homogenitas")
@admin_required
def dashboard_homogenitas():
    jobs = _custom_family_jobs("homogenitas")
    return render_template("homogenitas.html", active="homogenitas", jobs=jobs,
                           output_default=CUSTOM_OUTPUT_DEFAULT)


@app.route(DASHBOARD_PREFIX + "/custom/estates", endpoint="dashboard_custom_estates")
@app.route(DASHBOARD_PREFIX + "/tbm/estates", endpoint="dashboard_tbm_estates")
@app.route(DASHBOARD_PREFIX + "/treecounting/estates", endpoint="dashboard_treecounting_estates")
@app.route(DASHBOARD_PREFIX + "/homogenitas/estates", endpoint="dashboard_homogenitas_estates")
@admin_required
def dashboard_custom_estates():
    return jsonify({"estates": _get_estate_list_via_qgis()})


@app.route(DASHBOARD_PREFIX + "/custom/upload-model", methods=["POST"])
@admin_required
def dashboard_custom_upload_model():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"success": False, "message": "Tidak ada file"}), 400
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_MODEL_EXTENSIONS:
        return jsonify({"success": False, "message": f"Ekstensi {ext} tidak diizinkan, harus .pt"}), 400
    filename = secure_filename(file.filename)
    save_path = os.path.join(CUSTOM_MODELS, f"{int(datetime.now().timestamp())}_{filename}")
    file.save(save_path)
    try:
        classes = _read_model_classes(save_path)
    except Exception as e:
        os.remove(save_path)
        return jsonify({"success": False, "message": f"Gagal membaca model: {e}"}), 400
    return jsonify({
        "success": True, "path": save_path, "filename": filename,
        "classes": classes,
        "palette": {cid: DEFAULT_CLASS_PALETTE[i % len(DEFAULT_CLASS_PALETTE)] for i, cid in enumerate(classes)},
    })


@app.route(DASHBOARD_PREFIX + "/custom/upload", methods=["POST"], endpoint="dashboard_custom_upload")
@app.route(DASHBOARD_PREFIX + "/tbm/upload", methods=["POST"], endpoint="dashboard_tbm_upload")
@app.route(DASHBOARD_PREFIX + "/treecounting/upload", methods=["POST"], endpoint="dashboard_treecounting_upload")
@app.route(DASHBOARD_PREFIX + "/homogenitas/upload", methods=["POST"], endpoint="dashboard_homogenitas_upload")
@admin_required
def dashboard_custom_upload():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"success": False, "message": "Tidak ada file"}), 400
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_BATCH_EXTENSIONS:
        return jsonify({"success": False, "message": f"Ekstensi {ext} tidak diizinkan"}), 400
    filename = secure_filename(file.filename)
    save_path = os.path.join(CUSTOM_INCOMING, filename)
    file.save(save_path)
    return jsonify({"success": True, "path": save_path})


@app.route(DASHBOARD_PREFIX + "/custom/browse", endpoint="dashboard_custom_browse")
@app.route(DASHBOARD_PREFIX + "/tbm/browse", endpoint="dashboard_tbm_browse")
@app.route(DASHBOARD_PREFIX + "/treecounting/browse", endpoint="dashboard_treecounting_browse")
@app.route(DASHBOARD_PREFIX + "/homogenitas/browse", endpoint="dashboard_homogenitas_browse")
@admin_required
def dashboard_custom_browse():
    """Reuse browser filesystem yang sama dengan Batch Deteksi TM (admin only)."""
    return dashboard_batch_browse()


def _validate_splitter_upload_id(splitter_upload_id):
    """Cek upload_id (SHP splitter sendiri) masih ada di batch_splitter_uploads sebelum
    job disimpan -- dipakai bareng oleh semua submit route custom_batch_jobs (custom/
    tbm/treecounting/homogenitas), pola & tabel sama persis dengan dashboard_batch_submit.
    Return None kalau valid/tidak dipakai, atau pesan error kalau upload_id tidak ketemu."""
    if not splitter_upload_id:
        return None
    conn = get_db()
    if not conn:
        return "Tidak bisa konek ke database"
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM batch_splitter_uploads WHERE upload_id = %s", (splitter_upload_id,))
        found = cur.fetchone()
        cur.close(); conn.close()
    except Exception as e:
        return str(e)
    if not found:
        return "Upload SHP splitter tidak ditemukan/sudah kedaluwarsa — upload ulang."
    return None


@app.route(DASHBOARD_PREFIX + "/custom/submit", methods=["POST"])
@admin_required
def dashboard_custom_submit():
    f = request.form
    job_name = f.get("job_name", "").strip() or "Custom Detection"
    model_path = f.get("model_path", "").strip()
    model_filename = f.get("model_filename", "").strip()
    class_names = f.get("class_names", "").strip() or "{}"
    input_path = f.get("input_path", "").strip()
    estate = f.get("estate_splitter", "").strip() or None
    splitter_upload_id = f.get("splitter_upload_id", "").strip() or None
    confidence = f.get("confidence_threshold", "").strip() or "0.25"
    slice_size = f.get("slice_size", "").strip() or "1900"
    output_folder = _localize_output_path(f.get("output_folder", "").strip() or CUSTOM_OUTPUT_DEFAULT)
    do_rekap = f.get("do_rekap", "0").strip() not in ("0", "false", "False", "")

    if not model_path or not os.path.exists(model_path):
        return jsonify({"success": False, "message": "Model belum diupload / tidak ditemukan"}), 400
    if not input_path or not os.path.exists(input_path):
        return jsonify({"success": False, "message": "File input tidak ditemukan"}), 400
    splitter_err = _validate_splitter_upload_id(splitter_upload_id)
    if splitter_err:
        return jsonify({"success": False, "message": splitter_err}), 400

    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO custom_batch_jobs (created_by, job_name, model_path, model_filename, "
                "class_names, input_path, estate_splitter, splitter_upload_id, confidence_threshold, "
                "slice_size, output_folder, do_rekap, job_type, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'custom', 'pending')",
                (session.get("admin_user"), job_name, model_path, model_filename, class_names,
                 input_path, estate, splitter_upload_id, float(confidence), int(slice_size),
                 output_folder, do_rekap),
            )
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[CUSTOM SUBMIT ERROR] {e}")
            return jsonify({"success": False, "message": str(e)}), 500
    return jsonify({"success": True})


_TBM_CLASSES_CACHE = None


def _get_tbm_model_classes():
    """Baca nama kelas model TBM sekali saja lalu cache — model-nya tetap/tidak pernah ganti."""
    global _TBM_CLASSES_CACHE
    if _TBM_CLASSES_CACHE is None:
        _TBM_CLASSES_CACHE = _read_model_classes(TBM_MODEL_PATH)
    return _TBM_CLASSES_CACHE


_TREECOUNTING_CLASSES_CACHE = None


def _get_treecounting_model_classes():
    """Baca nama kelas model TreeCounting sekali saja lalu cache — model-nya tetap."""
    global _TREECOUNTING_CLASSES_CACHE
    if _TREECOUNTING_CLASSES_CACHE is None:
        _TREECOUNTING_CLASSES_CACHE = _read_model_classes(TREECOUNTING_MODEL_PATH)
    return _TREECOUNTING_CLASSES_CACHE


@app.route(DASHBOARD_PREFIX + "/tbm/submit", methods=["POST"])
@admin_required
def dashboard_tbm_submit():
    """Sama seperti /custom/submit, tapi model TIDAK diupload user — sudah tetap (mirip Deteksi TM)."""
    f = request.form
    job_name = f.get("job_name", "").strip() or "Deteksi TBM"
    input_path = f.get("input_path", "").strip()
    estate = f.get("estate_splitter", "").strip() or None
    splitter_upload_id = f.get("splitter_upload_id", "").strip() or None
    confidence = f.get("confidence_threshold", "").strip() or "0.25"
    slice_size = f.get("slice_size", "").strip() or "1900"
    output_folder = _localize_output_path(f.get("output_folder", "").strip() or CUSTOM_OUTPUT_DEFAULT)
    do_rekap = f.get("do_rekap", "0").strip() not in ("0", "false", "False", "")

    grid_tile_px_raw = f.get("grid_tile_px", "").strip()
    grid_tile_px = None
    if grid_tile_px_raw:
        try:
            grid_tile_px = max(1000, min(20000, int(grid_tile_px_raw)))
        except ValueError:
            return jsonify({"success": False, "message": "Ukuran grid harus berupa angka"}), 400

    if not os.path.exists(TBM_MODEL_PATH):
        return jsonify({"success": False, "message": f"Model TBM tidak ditemukan di server: {TBM_MODEL_PATH}"}), 500
    if not input_path or not os.path.exists(input_path):
        return jsonify({"success": False, "message": "File input tidak ditemukan"}), 400
    splitter_err = _validate_splitter_upload_id(splitter_upload_id)
    if splitter_err:
        return jsonify({"success": False, "message": splitter_err}), 400

    try:
        class_names = json.dumps(_get_tbm_model_classes())
    except Exception as e:
        return jsonify({"success": False, "message": f"Gagal membaca model TBM: {e}"}), 500

    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO custom_batch_jobs (created_by, job_name, model_path, model_filename, "
                "class_names, input_path, estate_splitter, splitter_upload_id, confidence_threshold, "
                "slice_size, output_folder, do_rekap, job_type, grid_tile_px, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'tbm', %s, 'pending')",
                (session.get("admin_user"), job_name, TBM_MODEL_PATH, "immature.pt", class_names,
                 input_path, estate, splitter_upload_id, float(confidence), int(slice_size),
                 output_folder, do_rekap, grid_tile_px),
            )
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[TBM SUBMIT ERROR] {e}")
            return jsonify({"success": False, "message": str(e)}), 500
    return jsonify({"success": True})


@app.route(DASHBOARD_PREFIX + "/treecounting/submit", methods=["POST"])
@admin_required
def dashboard_treecounting_submit():
    """Sama seperti /custom/submit (termasuk splitter estate opsional), tapi model
    TIDAK diupload user — sudah tetap (treecounting.pt), mirip Deteksi TBM."""
    f = request.form
    job_name = f.get("job_name", "").strip() or "TreeCounting"
    input_path = f.get("input_path", "").strip()
    estate = f.get("estate_splitter", "").strip() or None
    splitter_upload_id = f.get("splitter_upload_id", "").strip() or None
    confidence = f.get("confidence_threshold", "").strip() or "0.25"
    slice_size = f.get("slice_size", "").strip() or "1900"
    output_folder = _localize_output_path(f.get("output_folder", "").strip() or CUSTOM_OUTPUT_DEFAULT)
    do_rekap = f.get("do_rekap", "0").strip() not in ("0", "false", "False", "")

    if not os.path.exists(TREECOUNTING_MODEL_PATH):
        return jsonify({"success": False,
                        "message": f"Model TreeCounting tidak ditemukan di server: {TREECOUNTING_MODEL_PATH}"}), 500
    if not input_path or not os.path.exists(input_path):
        return jsonify({"success": False, "message": "File input tidak ditemukan"}), 400
    splitter_err = _validate_splitter_upload_id(splitter_upload_id)
    if splitter_err:
        return jsonify({"success": False, "message": splitter_err}), 400

    try:
        class_names = json.dumps(_get_treecounting_model_classes())
    except Exception as e:
        return jsonify({"success": False, "message": f"Gagal membaca model TreeCounting: {e}"}), 500

    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO custom_batch_jobs (created_by, job_name, model_path, model_filename, "
                "class_names, input_path, estate_splitter, splitter_upload_id, confidence_threshold, "
                "slice_size, output_folder, do_rekap, job_type, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'treecounting', 'pending')",
                (session.get("admin_user"), job_name, TREECOUNTING_MODEL_PATH, "treecounting.pt", class_names,
                 input_path, estate, splitter_upload_id, float(confidence), int(slice_size),
                 output_folder, do_rekap),
            )
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[TREECOUNTING SUBMIT ERROR] {e}")
            return jsonify({"success": False, "message": str(e)}), 500
    return jsonify({"success": True})


@app.route(DASHBOARD_PREFIX + "/homogenitas/submit", methods=["POST"])
@admin_required
def dashboard_homogenitas_submit():
    """Homogenitas Kanopi — port dari desktop app. Alurnya sama seperti TreeCounting
    (model tetap, splitter estate opsional), bedanya:
      - model yang dipakai immature.pt (sama dengan Deteksi TBM),
      - ada isian GSD opsional (kosong = resolusi asli citra),
      - setelah deteksi, worker menambah tahap klasifikasi kanopi per blok
        (lihat _classify_canopy_homogeneity di custom_batch_worker.py).
    """
    f = request.form
    job_name = f.get("job_name", "").strip() or "Homogenitas Kanopi"
    input_path = f.get("input_path", "").strip()
    estate = f.get("estate_splitter", "").strip() or None
    splitter_upload_id = f.get("splitter_upload_id", "").strip() or None
    confidence = f.get("confidence_threshold", "").strip() or "0.25"
    slice_size = f.get("slice_size", "").strip() or "1900"
    output_folder = _localize_output_path(f.get("output_folder", "").strip() or CUSTOM_OUTPUT_DEFAULT)

    # Kosong = pakai resolusi asli citra (tanpa resample) — default, dan inilah yang
    # setara dengan desktop app. Diisi = tiap tile di-resample ke ukuran piksel itu dulu.
    target_gsd_raw = f.get("target_gsd_m", "").strip()
    target_gsd = None
    if target_gsd_raw:
        try:
            target_gsd = float(target_gsd_raw.replace(",", "."))
        except ValueError:
            return jsonify({"success": False, "message": "Ukuran piksel (GSD) harus berupa angka, contoh: 0.07"}), 400
        if target_gsd <= 0 or target_gsd > 10:
            return jsonify({"success": False, "message": "Ukuran piksel (GSD) harus di antara 0 dan 10 meter"}), 400

    if not os.path.exists(HOMOGENITAS_MODEL_PATH):
        return jsonify({"success": False,
                        "message": f"Model Homogenitas tidak ditemukan di server: {HOMOGENITAS_MODEL_PATH}"}), 500
    if not input_path or not os.path.exists(input_path):
        return jsonify({"success": False, "message": "File input tidak ditemukan"}), 400
    splitter_err = _validate_splitter_upload_id(splitter_upload_id)
    if splitter_err:
        return jsonify({"success": False, "message": splitter_err}), 400

    # Kelas yang disimpan adalah kelas KANOPI hasil klasifikasi, bukan kelas mentah
    # immature.pt — inilah yang ditampilkan di peta & ringkasan halaman Analyze.
    class_names = json.dumps(HOMOGENITAS_CLASSES)
    class_styles = json.dumps(HOMOGENITAS_CLASS_STYLES)

    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO custom_batch_jobs (created_by, job_name, model_path, model_filename, "
                "class_names, class_styles, input_path, estate_splitter, splitter_upload_id, "
                "confidence_threshold, slice_size, output_folder, do_rekap, target_gsd_m, job_type, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, %s, 'homogenitas', 'pending')",
                (session.get("admin_user"), job_name, HOMOGENITAS_MODEL_PATH,
                 os.path.basename(HOMOGENITAS_MODEL_PATH), class_names, class_styles,
                 input_path, estate, splitter_upload_id, float(confidence), int(slice_size),
                 output_folder, target_gsd),
            )
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[HOMOGENITAS SUBMIT ERROR] {e}")
            return jsonify({"success": False, "message": str(e)}), 500
    return jsonify({"success": True})


# ── Rekap Homogenitas Kanopi — rekap LUAS (Ha) per blok x kelas kanopi (Well/Normal/
# Abnormal Canopy), menu terpisah di bawah Homogenitas Kanopi. Reuse TOTAL job_type=
# 'rekap_only' yang sudah ada (mesin sama dengan menu "Rekap Data" -- generate_rekap_
# kesehatan(), engine asli, tidak diubah) -- bedanya cuma kelas_col dikunci ke 'CANOPY'
# (bukan dipilih user macam-macam kayak Rekap Data), dan tidak ada UI review/remap kelas
# sama sekali karena nama 3 kelasnya sudah pasti dari _classify_canopy_homogeneity().
# Job jenis ini dibedakan dari job Rekap Data biasa (job_type SAMA 'rekap_only') lewat
# kelas_col = 'CANOPY' di kondisi WHERE setiap query di bawah.
def _homogenitas_rekap_jobs():
    conn = get_db()
    jobs = []
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, job_name, status, current_stage, stage_progress,
                       result_rekap_shapefile_path, result_rekap_excel_path,
                       error_message, started_at, finished_at, resolved_estate
                FROM custom_batch_jobs
                WHERE job_type = 'rekap_only' AND kelas_col = 'CANOPY'
                ORDER BY id DESC
            """)
            jobs = cur.fetchall()
            cur.close(); conn.close()
        except Exception as e:
            print(f"[HOMOGENITAS REKAP LIST ERROR] {e}")
    return jobs


@app.route(DASHBOARD_PREFIX + "/homogenitas-rekap")
@admin_required
def dashboard_homogenitas_rekap():
    jobs = _homogenitas_rekap_jobs()

    # Daftar job Homogenitas yang SUDAH selesai & punya hasil klasifikasi kanopi --
    # ditampilkan sebagai pilihan cepat (dropdown) supaya user tidak perlu browse file
    # manual untuk kasus yang paling umum (rekap dari job yang baru saja selesai).
    homogenitas_options = []
    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, job_name, result_rekap_shapefile_path
                FROM custom_batch_jobs
                WHERE job_type = 'homogenitas' AND status = 'done'
                      AND result_rekap_shapefile_path IS NOT NULL
                ORDER BY id DESC
            """)
            homogenitas_options = [
                {"id": r[0], "job_name": r[1], "shapefile_path": r[2]} for r in cur.fetchall()
            ]
            cur.close(); conn.close()
        except Exception as e:
            print(f"[HOMOGENITAS REKAP OPTIONS ERROR] {e}")

    return render_template("homogenitas_rekap.html", active="homogenitas_rekap", jobs=jobs,
                           output_default=CUSTOM_OUTPUT_DEFAULT, homogenitas_options=homogenitas_options)


@app.route(DASHBOARD_PREFIX + "/homogenitas-rekap/submit", methods=["POST"])
@admin_required
def dashboard_homogenitas_rekap_submit():
    f = request.form
    job_name = f.get("job_name", "").strip() or "Rekap Homogenitas"
    input_path = f.get("input_path", "").strip()
    estate = f.get("estate_splitter", "").strip() or None
    rotasi = f.get("rotasi", "").strip()
    bulan = f.get("bulan", "").strip()
    tahun = f.get("tahun", "").strip()
    output_folder = _localize_output_path(f.get("output_folder", "").strip() or CUSTOM_OUTPUT_DEFAULT)

    if not input_path or not os.path.exists(input_path):
        return jsonify({"success": False, "message": "File SHP hasil Homogenitas tidak ditemukan"}), 400

    # batch_group_id WAJIB diisi (bahkan untuk 1 file/job saja) -- ini kunci yang dibaca
    # _maybe_merge_estate_group() di custom_batch_worker.py untuk memicu tahap gabungan
    # (merge_and_finalize.py) begitu job ini selesai, yang baru menghitung Bad Image dan
    # membuat total luas benar-benar sama dengan luas estate di Aresta. Tanpa ini, hasil
    # rekap MENTAH -- cuma luas dari titik yang benar-benar terdeteksi, area yang tidak
    # ter-cover tidak ikut terhitung. Sama persis pola /rekap-data/submit.
    batch_group_id = uuid.uuid4().hex

    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO custom_batch_jobs (created_by, job_name, model_path, "
                "input_path, estate_splitter, output_folder, kelas_col, rekap_mode, "
                "rotasi, bulan, tahun, batch_group_id, job_type, status) "
                "VALUES (%s, %s, '', %s, %s, %s, 'CANOPY', 'raw', %s, %s, %s, %s, 'rekap_only', 'pending')",
                (session.get("admin_user"), job_name, input_path, estate,
                 output_folder, rotasi, bulan, tahun, batch_group_id),
            )
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[HOMOGENITAS REKAP SUBMIT ERROR] {e}")
            return jsonify({"success": False, "message": str(e)}), 500
    return jsonify({"success": True})


@app.route(DASHBOARD_PREFIX + "/homogenitas-rekap/status")
@admin_required
def dashboard_homogenitas_rekap_status():
    conn = get_db()
    jobs = []
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, job_name, status, current_stage, stage_progress,
                       result_rekap_shapefile_path, result_rekap_excel_path,
                       error_message, started_at, finished_at, resolved_estate
                FROM custom_batch_jobs
                WHERE job_type = 'rekap_only' AND kelas_col = 'CANOPY'
                ORDER BY id DESC
            """)
            for row in cur.fetchall():
                jobs.append({
                    "id": row[0], "job_name": row[1], "status": row[2], "current_stage": row[3],
                    "stage_progress": row[4], "result_rekap_shapefile_path": row[5],
                    "result_rekap_excel_path": row[6], "error_message": row[7],
                    "started_at": row[8].isoformat() if row[8] else None,
                    "finished_at": row[9].isoformat() if row[9] else None,
                    "resolved_estate": row[10],
                })
            cur.close(); conn.close()
        except Exception as e:
            print(f"[HOMOGENITAS REKAP STATUS ERROR] {e}")
    return jsonify({"jobs": jobs})


@app.route(DASHBOARD_PREFIX + "/homogenitas-rekap/merges/status")
@admin_required
def dashboard_homogenitas_rekap_merges_status():
    """Hasil GABUNGAN per estate (lihat merge_and_finalize.py / _maybe_merge_estate_group
    di custom_batch_worker.py) — ini yang totalnya benar (sudah termasuk Bad Image), beda
    dari hasil per-job mentah di /homogenitas-rekap/status. Dibedakan dari hasil gabungan
    milik menu Rekap Data biasa (tabel rekap_estate_merges dipakai bersama, tidak ada
    kolom pembeda job_type) lewat EXISTS ke custom_batch_jobs yang kelas_col='CANOPY'."""
    conn = get_db()
    merges = []
    if conn:
        try:
            cur = conn.cursor()
            allowed_estates = _get_allowed_estates()
            base_sql = (
                "SELECT m.id, m.batch_group_id, m.estate, m.rotasi, m.bulan, m.tahun, m.status, "
                "m.result_shapefile_path, m.result_excel_path, m.error_message, "
                "m.created_at, m.finished_at "
                "FROM rekap_estate_merges m "
                "WHERE EXISTS (SELECT 1 FROM custom_batch_jobs j WHERE j.batch_group_id = "
                "m.batch_group_id AND j.kelas_col = 'CANOPY')"
            )
            params = []
            if allowed_estates is not None:
                base_sql += " AND m.estate = ANY(%s)"
                params.append(allowed_estates)
            base_sql += " ORDER BY m.id DESC"
            cur.execute(base_sql, params)
            for row in cur.fetchall():
                merges.append({
                    "id": row[0], "batch_group_id": row[1], "estate": row[2], "rotasi": row[3],
                    "bulan": row[4], "tahun": row[5], "status": row[6],
                    "result_shapefile_path": row[7], "result_excel_path": row[8],
                    "error_message": row[9],
                    "created_at": row[10].isoformat() if row[10] else None,
                    "finished_at": row[11].isoformat() if row[11] else None,
                })
            cur.close(); conn.close()
        except Exception as e:
            print(f"[HOMOGENITAS REKAP MERGES STATUS ERROR] {e}")
    return jsonify({"merges": merges})


@app.route(DASHBOARD_PREFIX + "/homogenitas-rekap/merges/download/<int:merge_id>")
@admin_required
def dashboard_homogenitas_rekap_merges_download(merge_id):
    """type=shp (default, di-zip) | excel"""
    file_type = request.args.get("type", "shp")
    column = "result_excel_path" if file_type == "excel" else "result_shapefile_path"

    conn = get_db()
    result_path = None
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                f"SELECT {column} FROM rekap_estate_merges WHERE id = %s AND status = 'done' "
                f"AND EXISTS (SELECT 1 FROM custom_batch_jobs j WHERE j.batch_group_id = "
                f"rekap_estate_merges.batch_group_id AND j.kelas_col = 'CANOPY')",
                (merge_id,),
            )
            row = cur.fetchone()
            cur.close(); conn.close()
            if row:
                result_path = row[0]
        except Exception as e:
            print(f"[HOMOGENITAS REKAP MERGES DOWNLOAD ERROR] {e}")
    if not result_path or not os.path.exists(result_path):
        return "Hasil tidak ditemukan", 404

    if file_type == "excel":
        return Response(
            open(result_path, "rb").read(),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={os.path.basename(result_path)}"},
        )

    import io
    import zipfile
    base = os.path.splitext(result_path)[0]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
            part = base + suffix
            if os.path.exists(part):
                zf.write(part, arcname=os.path.basename(part))
    buf.seek(0)
    return Response(
        buf.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename=rekap_homogenitas_gabungan_{merge_id}.zip"},
    )


def _custom_family_status(job_type):
    conn = get_db()
    jobs = []
    if conn:
        try:
            cur = conn.cursor()
            allowed_estates = _get_allowed_estates()
            base_sql = """
                SELECT id, job_name, model_filename, class_names, status, current_stage, stage_progress,
                       tiles_total, tiles_done, result_total_detections, result_class_counts,
                       result_geojson_path, result_shapefile_path, result_excel_path,
                       error_message, stage_detail, started_at, finished_at,
                       do_rekap, result_rekap_shapefile_path, result_rekap_excel_path
                FROM custom_batch_jobs
            """
            conditions = ["job_type = %s"]
            params = [job_type]
            if allowed_estates is not None:
                conditions.append("estate_splitter = ANY(%s)")
                params.append(allowed_estates)
            cur.execute(base_sql + " WHERE " + " AND ".join(conditions) + " ORDER BY id DESC", params)
            for row in cur.fetchall():
                jobs.append({
                    "id": row[0], "job_name": row[1], "model_filename": row[2], "class_names": row[3],
                    "status": row[4], "current_stage": row[5], "stage_progress": row[6],
                    "tiles_total": row[7], "tiles_done": row[8],
                    "result_total_detections": row[9], "result_class_counts": row[10],
                    "result_geojson_path": row[11], "result_shapefile_path": row[12],
                    "result_excel_path": row[13], "error_message": row[14], "stage_detail": row[15],
                    "started_at": row[16].isoformat() if row[16] else None,
                    "finished_at": row[17].isoformat() if row[17] else None,
                    "do_rekap": row[18],
                    "result_rekap_shapefile_path": row[19], "result_rekap_excel_path": row[20],
                })
            cur.close(); conn.close()
        except Exception as e:
            print(f"[{job_type.upper()} STATUS ERROR] {e}")
    return jobs


@app.route(DASHBOARD_PREFIX + "/custom/status")
@admin_required
def dashboard_custom_status():
    return jsonify({"jobs": _custom_family_status("custom")})


@app.route(DASHBOARD_PREFIX + "/tbm/status")
@admin_required
def dashboard_tbm_status():
    return jsonify({"jobs": _custom_family_status("tbm")})


@app.route(DASHBOARD_PREFIX + "/treecounting/status")
@admin_required
def dashboard_treecounting_status():
    return jsonify({"jobs": _custom_family_status("treecounting")})


@app.route(DASHBOARD_PREFIX + "/homogenitas/status")
@admin_required
def dashboard_homogenitas_status():
    return jsonify({"jobs": _custom_family_status("homogenitas")})


def _job_family_from_request():
    """Tentukan 'tbm'/'treecounting'/'homogenitas'/'rekap_data'/'homogenitas_rekap'/'custom'
    dari path URL yang diakses sekarang — dipakai route yang dibagi beberapa menu sekaligus
    untuk tahu mau redirect/render ke mana."""
    if request.path.startswith(DASHBOARD_PREFIX + "/rekap-data"):
        return "rekap_data"
    # Dicek SEBELUM loop "homogenitas" di bawah -- /homogenitas-rekap tidak akan pernah
    # match f"/homogenitas/" (beda karakter setelah "homogenitas"), tapi tetap ditaruh di
    # sini biar jelas dan tidak rawan salah urutan kalau nanti ada penambahan lagi.
    if request.path.startswith(DASHBOARD_PREFIX + "/homogenitas-rekap"):
        return "homogenitas_rekap"
    for name in ("tbm", "treecounting", "homogenitas"):
        if request.path.startswith(DASHBOARD_PREFIX + f"/{name}/") or request.path == DASHBOARD_PREFIX + f"/{name}":
            return name
    return "custom"


@app.route(DASHBOARD_PREFIX + "/custom/download/<int:job_id>", endpoint="dashboard_custom_download")
@app.route(DASHBOARD_PREFIX + "/tbm/download/<int:job_id>", endpoint="dashboard_tbm_download")
@app.route(DASHBOARD_PREFIX + "/treecounting/download/<int:job_id>", endpoint="dashboard_treecounting_download")
@app.route(DASHBOARD_PREFIX + "/homogenitas/download/<int:job_id>", endpoint="dashboard_homogenitas_download")
@app.route(DASHBOARD_PREFIX + "/rekap-data/download/<int:job_id>", endpoint="dashboard_rekap_data_download")
@app.route(DASHBOARD_PREFIX + "/homogenitas-rekap/download/<int:job_id>", endpoint="dashboard_homogenitas_rekap_download")
@admin_required
def dashboard_custom_download(job_id):
    """type=shp (default) | excel | geojson | rekap_shp | rekap_excel"""
    file_type = request.args.get("type", "shp")
    column_map = {
        "shp": "result_shapefile_path",
        "excel": "result_excel_path",
        "geojson": "result_geojson_path",
        "rekap_shp": "result_rekap_shapefile_path",
        "rekap_excel": "result_rekap_excel_path",
    }
    column = column_map.get(file_type)
    if not column:
        return "Jenis file tidak dikenal", 400

    conn = get_db()
    result_path = None
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT {column} FROM custom_batch_jobs WHERE id = %s AND status = 'done'", (job_id,))
            row = cur.fetchone()
            cur.close(); conn.close()
            if row:
                result_path = row[0]
        except Exception as e:
            print(f"[CUSTOM DOWNLOAD ERROR] {e}")
    if not result_path or not os.path.exists(result_path):
        return "Hasil tidak ditemukan", 404

    if file_type in ("excel", "rekap_excel"):
        return Response(
            open(result_path, "rb").read(),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={os.path.basename(result_path)}"},
        )
    if file_type == "geojson":
        return Response(
            open(result_path, "rb").read(),
            mimetype="application/geo+json",
            headers={"Content-Disposition": f"attachment; filename={os.path.basename(result_path)}"},
        )

    import io
    import zipfile
    base = os.path.splitext(result_path)[0]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
            part = base + suffix
            if os.path.exists(part):
                zf.write(part, arcname=os.path.basename(part))
    buf.seek(0)
    return Response(
        buf.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename=custom_job_{job_id}_{file_type}.zip"},
    )


@app.route(DASHBOARD_PREFIX + "/tools/tbm-poor-class")
@admin_required
def dashboard_tbm_poor_class_page():
    """Tools admin: konversi manual SHP hasil Deteksi TBM LAMA (dibuat sebelum fitur
    Poor-class ada di pipeline, lihat apply_poor_class_by_radius di batch_worker.py)
    dari 2 kelas (TBM Sehat/TBM Sakit) jadi 3 kelas (+ Poor), tanpa perlu proses ulang
    deteksi YOLO dari awal -- cukup baca SHP hasil deteksi yang sudah ada."""
    return render_template("tbm_poor_class_tool.html", active="tbm_poor_class_tool")


@app.route(DASHBOARD_PREFIX + "/tools/tbm-poor-class/convert", methods=["POST"])
@admin_required
def dashboard_tbm_poor_class_convert():
    input_path = request.form.get("input_path", "").strip()
    if not input_path or not os.path.isfile(input_path):
        return jsonify({"success": False, "message": "File SHP tidak ditemukan"}), 400
    if os.path.splitext(input_path)[1].lower() != ".shp":
        return jsonify({"success": False, "message": "File harus berformat .shp"}), 400

    import batch_worker
    batch_worker._ensure_network_share_connected(input_path)

    tmp_dir = tempfile.mkdtemp(prefix="poor_class_tool_")
    try:
        gdf, poor_count, error_reason = batch_worker.apply_poor_class_by_radius(input_path, tmp_dir)
        if gdf is None:
            return jsonify({"success": False, "message": error_reason or "Gagal memproses (tidak diketahui alasannya)."}), 400

        base_dir = os.path.dirname(input_path)
        base_name = os.path.splitext(os.path.basename(input_path))[0]
        output_path = os.path.join(base_dir, f"{base_name}_dengan_poor.shp")
        gdf.to_file(output_path, driver="ESRI Shapefile")

        total_sakit_asli = int((gdf["class_name"].astype(str).str.lower() == "tbm sakit").sum()) + poor_count
        return jsonify({
            "success": True,
            "output_path": output_path,
            "poor_count": poor_count,
            "total_sakit_asli": total_sakit_asli,
            "total_titik": len(gdf),
        })
    except Exception as e:
        print(f"[TBM POOR CLASS CONVERT ERROR] {e}")
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.route(DASHBOARD_PREFIX + "/tools/tbm-poor-class/download")
@admin_required
def dashboard_tbm_poor_class_download():
    output_path = request.args.get("path", "").strip()
    if not output_path or not os.path.isfile(output_path) or not output_path.lower().endswith(".shp"):
        return "File tidak ditemukan", 404

    import io
    import zipfile
    base = os.path.splitext(output_path)[0]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
            part = base + suffix
            if os.path.exists(part):
                zf.write(part, arcname=os.path.basename(part))
    buf.seek(0)
    return Response(
        buf.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename={os.path.basename(base)}.zip"},
    )


@app.route(DASHBOARD_PREFIX + "/custom/delete/<int:job_id>", methods=["POST"], endpoint="dashboard_custom_delete")
@app.route(DASHBOARD_PREFIX + "/tbm/delete/<int:job_id>", methods=["POST"], endpoint="dashboard_tbm_delete")
@app.route(DASHBOARD_PREFIX + "/treecounting/delete/<int:job_id>", methods=["POST"], endpoint="dashboard_treecounting_delete")
@app.route(DASHBOARD_PREFIX + "/homogenitas/delete/<int:job_id>", methods=["POST"], endpoint="dashboard_homogenitas_delete")
@app.route(DASHBOARD_PREFIX + "/rekap-data/delete/<int:job_id>", methods=["POST"], endpoint="dashboard_rekap_data_delete")
@app.route(DASHBOARD_PREFIX + "/homogenitas-rekap/delete/<int:job_id>", methods=["POST"], endpoint="dashboard_homogenitas_rekap_delete")
@admin_required
def dashboard_custom_delete(job_id):
    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM custom_batch_jobs WHERE id = %s AND status != 'processing'", (job_id,))
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[CUSTOM DELETE ERROR] {e}")
    return redirect(url_for(f"dashboard_{_job_family_from_request()}"))


@app.route(DASHBOARD_PREFIX + "/custom/stop/<int:job_id>", methods=["POST"], endpoint="dashboard_custom_stop")
@app.route(DASHBOARD_PREFIX + "/tbm/stop/<int:job_id>", methods=["POST"], endpoint="dashboard_tbm_stop")
@app.route(DASHBOARD_PREFIX + "/treecounting/stop/<int:job_id>", methods=["POST"], endpoint="dashboard_treecounting_stop")
@app.route(DASHBOARD_PREFIX + "/homogenitas/stop/<int:job_id>", methods=["POST"], endpoint="dashboard_homogenitas_stop")
@app.route(DASHBOARD_PREFIX + "/rekap-data/stop/<int:job_id>", methods=["POST"], endpoint="dashboard_rekap_data_stop")
@app.route(DASHBOARD_PREFIX + "/homogenitas-rekap/stop/<int:job_id>", methods=["POST"], endpoint="dashboard_homogenitas_rekap_stop")
@admin_required
def dashboard_custom_stop(job_id):
    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE custom_batch_jobs SET stop_requested = TRUE WHERE id = %s AND status = 'processing'",
                (job_id,),
            )
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[CUSTOM STOP ERROR] {e}")
    return redirect(url_for(f"dashboard_{_job_family_from_request()}"))


@app.route(DASHBOARD_PREFIX + "/custom/force-fail/<int:job_id>", methods=["POST"], endpoint="dashboard_custom_force_fail")
@app.route(DASHBOARD_PREFIX + "/tbm/force-fail/<int:job_id>", methods=["POST"], endpoint="dashboard_tbm_force_fail")
@app.route(DASHBOARD_PREFIX + "/treecounting/force-fail/<int:job_id>", methods=["POST"], endpoint="dashboard_treecounting_force_fail")
@app.route(DASHBOARD_PREFIX + "/homogenitas/force-fail/<int:job_id>", methods=["POST"], endpoint="dashboard_homogenitas_force_fail")
@app.route(DASHBOARD_PREFIX + "/rekap-data/force-fail/<int:job_id>", methods=["POST"], endpoint="dashboard_rekap_data_force_fail")
@app.route(DASHBOARD_PREFIX + "/homogenitas-rekap/force-fail/<int:job_id>", methods=["POST"], endpoint="dashboard_homogenitas_rekap_force_fail")
@admin_required
def dashboard_custom_force_fail(job_id):
    """Paksa job 'processing' langsung jadi 'failed' di DB, TANPA butuh worker hidup --
    beda dari Stop (cuma titip flag stop_requested yang baru kebaca kalau worker aktif
    sempat singgah ke checkpoint berikutnya). Dipakai kalau worker yang mengklaim job
    ini sudah mati/hang total (proses macet di server, service di-restart paksa, dst)
    sehingga baris job nyangkut di status processing selamanya dan Stop tidak pernah
    direspons. Sebelum ini satu-satunya jalan adalah admin edit manual lewat SQL."""
    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE custom_batch_jobs SET status = 'failed', "
                "error_message = 'Dipaksa gagal oleh admin (worker tidak merespons)' "
                "WHERE id = %s AND status = 'processing'",
                (job_id,),
            )
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[CUSTOM FORCE FAIL ERROR] {e}")
    return redirect(url_for(f"dashboard_{_job_family_from_request()}"))


@app.route(DASHBOARD_PREFIX + "/custom/resume/<int:job_id>", methods=["POST"], endpoint="dashboard_custom_resume")
@app.route(DASHBOARD_PREFIX + "/tbm/resume/<int:job_id>", methods=["POST"], endpoint="dashboard_tbm_resume")
@app.route(DASHBOARD_PREFIX + "/treecounting/resume/<int:job_id>", methods=["POST"], endpoint="dashboard_treecounting_resume")
@app.route(DASHBOARD_PREFIX + "/homogenitas/resume/<int:job_id>", methods=["POST"], endpoint="dashboard_homogenitas_resume")
@app.route(DASHBOARD_PREFIX + "/rekap-data/resume/<int:job_id>", methods=["POST"], endpoint="dashboard_rekap_data_resume")
@app.route(DASHBOARD_PREFIX + "/homogenitas-rekap/resume/<int:job_id>", methods=["POST"], endpoint="dashboard_homogenitas_rekap_resume")
@admin_required
def dashboard_custom_resume(job_id):
    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE custom_batch_jobs SET status = 'pending', stop_requested = FALSE, error_message = NULL, "
                "raw_cleaned = FALSE WHERE id = %s AND status IN ('stopped', 'failed')",
                (job_id,),
            )
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[CUSTOM RESUME ERROR] {e}")
    return redirect(url_for(f"dashboard_{_job_family_from_request()}"))


@app.route(DASHBOARD_PREFIX + "/custom/analyze/<int:job_id>", endpoint="dashboard_custom_analyze")
@app.route(DASHBOARD_PREFIX + "/tbm/analyze/<int:job_id>", endpoint="dashboard_tbm_analyze")
@app.route(DASHBOARD_PREFIX + "/treecounting/analyze/<int:job_id>", endpoint="dashboard_treecounting_analyze")
@app.route(DASHBOARD_PREFIX + "/homogenitas/analyze/<int:job_id>", endpoint="dashboard_homogenitas_analyze")
@admin_required
def dashboard_custom_analyze(job_id):
    conn = get_db()
    job = None
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, job_name, model_filename, class_names, class_styles, input_path,
                       status, result_total_detections, result_class_counts, result_geojson_path,
                       result_shapefile_path, result_excel_path, finished_at, job_type
                FROM custom_batch_jobs WHERE id = %s AND status = 'done'
            """, (job_id,))
            row = cur.fetchone()
            cur.close(); conn.close()
            if row:
                job = {
                    "id": row[0], "job_name": row[1], "model_filename": row[2],
                    "class_names": row[3], "class_styles": row[4], "input_path": row[5],
                    "status": row[6], "result_total_detections": row[7],
                    "result_class_counts": row[8], "result_geojson_path": row[9],
                    "result_shapefile_path": row[10], "result_excel_path": row[11],
                    "finished_at": row[12], "job_type": row[13] or "custom",
                }
        except Exception as e:
            print(f"[CUSTOM ANALYZE ERROR] {e}")
    if not job:
        return redirect(url_for(f"dashboard_{_job_family_from_request()}"))
    active = job["job_type"] if job["job_type"] in ("tbm", "treecounting", "homogenitas") else "custom_batch"
    return render_template("custom_batch_analyze.html",
                           active=active, job=job, default_palette=DEFAULT_CLASS_PALETTE)


@app.route(DASHBOARD_PREFIX + "/custom/analyze/<int:job_id>/data", endpoint="dashboard_custom_analyze_data")
@app.route(DASHBOARD_PREFIX + "/tbm/analyze/<int:job_id>/data", endpoint="dashboard_tbm_analyze_data")
@app.route(DASHBOARD_PREFIX + "/treecounting/analyze/<int:job_id>/data", endpoint="dashboard_treecounting_analyze_data")
@app.route(DASHBOARD_PREFIX + "/homogenitas/analyze/<int:job_id>/data", endpoint="dashboard_homogenitas_analyze_data")
@admin_required
def dashboard_custom_analyze_data(job_id):
    conn = get_db()
    row = None
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT result_excel_path, result_total_detections, result_class_counts,
                       class_names, class_styles
                FROM custom_batch_jobs WHERE id = %s AND status = 'done'
            """, (job_id,))
            row = cur.fetchone()
            cur.close(); conn.close()
        except Exception as e:
            print(f"[CUSTOM ANALYZE DATA ERROR] {e}")
    if not row:
        return jsonify({"error": "Job tidak ditemukan"}), 404

    excel_path, total_det, class_counts_json, class_names_json, class_styles_json = row
    class_names = json.loads(class_names_json) if class_names_json else {}
    class_counts = json.loads(class_counts_json) if class_counts_json else {}
    class_styles = json.loads(class_styles_json) if class_styles_json else {}

    # Susun style final (default palette untuk kelas yang belum pernah diatur manual)
    styles_out = {}
    for i, (cid, cname) in enumerate(class_names.items()):
        saved = class_styles.get(cid, {})
        styles_out[cid] = {
            "name": cname,
            "color": saved.get("color") or DEFAULT_CLASS_PALETTE[i % len(DEFAULT_CLASS_PALETTE)],
            "size": saved.get("size") or 6,
        }

    table_rows = []
    avg_confidence = None
    if excel_path and os.path.exists(excel_path):
        try:
            import pandas as pd
            df = pd.read_excel(excel_path)
            if "confidence" in df.columns:
                avg_confidence = round(float(df["confidence"].astype(float).mean()), 3)
            table_rows = df.fillna("").astype(str).to_dict(orient="records")
        except Exception as e:
            print(f"[CUSTOM ANALYZE DATA EXCEL ERROR] {e}")

    return jsonify({
        "total_detections": total_det or 0,
        "class_counts": class_counts,
        "class_styles": styles_out,
        "avg_confidence": avg_confidence,
        "table_rows": table_rows,
    })


@app.route(DASHBOARD_PREFIX + "/custom/analyze/<int:job_id>/geojson", endpoint="dashboard_custom_analyze_geojson")
@app.route(DASHBOARD_PREFIX + "/tbm/analyze/<int:job_id>/geojson", endpoint="dashboard_tbm_analyze_geojson")
@app.route(DASHBOARD_PREFIX + "/treecounting/analyze/<int:job_id>/geojson", endpoint="dashboard_treecounting_analyze_geojson")
@app.route(DASHBOARD_PREFIX + "/homogenitas/analyze/<int:job_id>/geojson", endpoint="dashboard_homogenitas_analyze_geojson")
@admin_required
def dashboard_custom_analyze_geojson(job_id):
    conn = get_db()
    geojson_path = None
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT result_geojson_path FROM custom_batch_jobs WHERE id = %s AND status = 'done'",
                (job_id,),
            )
            row = cur.fetchone()
            cur.close(); conn.close()
            if row:
                geojson_path = row[0]
        except Exception as e:
            print(f"[CUSTOM GEOJSON ERROR] {e}")
    if not geojson_path or not os.path.exists(geojson_path):
        return Response('{"type":"FeatureCollection","features":[]}', mimetype="application/json")
    return Response(open(geojson_path, "rb").read(), mimetype="application/json")


@app.route(DASHBOARD_PREFIX + "/custom/analyze/<int:job_id>/style", methods=["POST"], endpoint="dashboard_custom_analyze_style")
@app.route(DASHBOARD_PREFIX + "/tbm/analyze/<int:job_id>/style", methods=["POST"], endpoint="dashboard_tbm_analyze_style")
@app.route(DASHBOARD_PREFIX + "/treecounting/analyze/<int:job_id>/style", methods=["POST"], endpoint="dashboard_treecounting_analyze_style")
@app.route(DASHBOARD_PREFIX + "/homogenitas/analyze/<int:job_id>/style", methods=["POST"], endpoint="dashboard_homogenitas_analyze_style")
@admin_required
def dashboard_custom_analyze_style(job_id):
    """Simpan simbologi (warna + ukuran) per kelas yang diatur user di halaman hasil,
    supaya tampilan peta/chart yang sama muncul lagi saat dibuka ulang."""
    styles = request.get_json(silent=True) or {}
    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE custom_batch_jobs SET class_styles = %s WHERE id = %s",
                (json.dumps(styles), job_id),
            )
            conn.commit(); cur.close(); conn.close()
            return jsonify({"success": True})
        except Exception as e:
            print(f"[CUSTOM STYLE SAVE ERROR] {e}")
            return jsonify({"success": False, "message": str(e)}), 500
    return jsonify({"success": False, "message": "db_unavailable"}), 503


@app.route(DASHBOARD_PREFIX + "/custom/log", endpoint="dashboard_custom_log")
@app.route(DASHBOARD_PREFIX + "/tbm/log", endpoint="dashboard_tbm_log")
@app.route(DASHBOARD_PREFIX + "/treecounting/log", endpoint="dashboard_treecounting_log")
@app.route(DASHBOARD_PREFIX + "/homogenitas/log", endpoint="dashboard_homogenitas_log")
@admin_required
def dashboard_custom_log():
    log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "custom_batch_worker.log")
    if not os.path.exists(log_file):
        return Response("Log file belum ada — worker belum pernah jalan.", mimetype="text/plain")
    try:
        n_lines = request.args.get("lines", 300, type=int)
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        content = "".join(lines[-n_lines:])
        return Response(content or "(log kosong)", mimetype="text/plain")
    except Exception as e:
        return Response(f"Gagal membaca log: {e}", mimetype="text/plain"), 500


# ══════════════════════════════════════════════════════════════════════════════
#  DATABASE KESEHATAN — Overview & Edit Data
# ══════════════════════════════════════════════════════════════════════════════

KESEHATAN_TABLE = "database_kesehatan"
ARESTA_TABLE = "database_aresta"

EDITABLE_COLUMNS = [
    "ESTATE", "REGION", "WILAYAH", "DIVISI", "NO_BLOK",
    "ROTASI", "TAHUN", "KESEHATAN", "PKK", "HA", "PERSEN",
]

FILTER_COLUMNS = {
    "region": "REGION",
    "wilayah": "WILAYAH",
    "estate": "ESTATE",
    "divisi": "DIVISI",
    "tahun": "TAHUN",
    "rotasi": "ROTASI",
}

ROTASI_ORDER = ["R1", "R1 SELECTED", "R2", "R2 SELECTED", "R3"]

# Urutan dari paling sehat ke paling buruk — dikonfirmasi via /agripalm/database/inspect.
# Data asli di DB tidak konsisten (Green/GREEN, NI/Need Improvement, NIS/Need Improvement
# Soon, dll) — makanya pakai KESEHATAN_NORMALIZE_SQL di query untuk menggabungkan varian ini.
KESEHATAN_ORDER = ["GREEN", "MODERATE GREEN", "NEED IMPROVEMENT", "NEED IMPROVEMENT SOON", "BAD IMAGE"]

# Normalisasi nilai KESEHATAN yang berantakan (case beda, singkatan NI/NIS) jadi 1 kategori baku.
KESEHATAN_NORMALIZE_SQL = """CASE
    WHEN UPPER(TRIM("KESEHATAN")) = 'GREEN' THEN 'Green'
    WHEN UPPER(TRIM("KESEHATAN")) = 'MODERATE GREEN' THEN 'Moderate Green'
    WHEN UPPER(TRIM("KESEHATAN")) IN ('NEED IMPROVEMENT', 'NI') THEN 'Need Improvement'
    WHEN UPPER(TRIM("KESEHATAN")) IN ('NEED IMPROVEMENT SOON', 'NIS') THEN 'Need Improvement Soon'
    WHEN UPPER(TRIM("KESEHATAN")) = 'BAD IMAGE' THEN 'Bad Image'
    ELSE COALESCE(NULLIF(TRIM("KESEHATAN"), ''), '(Tidak diketahui)')
END"""

# Normalisasi JENIS_TANAH — buang sampah "_x0000_" hasil korup Excel + samakan case/typo.
JENIS_TANAH_NORMALIZE_SQL = """CASE
    WHEN UPPER(TRIM(SPLIT_PART("JENIS_TANAH", '_x0000_', 1))) = 'MINERAL' THEN 'Mineral'
    WHEN UPPER(TRIM(SPLIT_PART("JENIS_TANAH", '_x0000_', 1))) = 'PASIR' THEN 'Pasir'
    WHEN UPPER(TRIM(SPLIT_PART("JENIS_TANAH", '_x0000_', 1))) = 'SULFAT MASAM' THEN 'Sulfat Masam'
    WHEN UPPER(TRIM(SPLIT_PART("JENIS_TANAH", '_x0000_', 1))) IN ('RENDAHAN', 'RENDAHHAN') THEN 'Rendahan'
    WHEN UPPER(TRIM(SPLIT_PART("JENIS_TANAH", '_x0000_', 1))) = 'LEMPUNG' THEN 'Lempung'
    ELSE COALESCE(NULLIF(TRIM(SPLIT_PART("JENIS_TANAH", '_x0000_', 1)), ''), '(Tidak diketahui)')
END"""


def rotasi_sort_key(value):
    if not value:
        return -1
    v = str(value).strip().upper()
    try:
        return ROTASI_ORDER.index(v)
    except ValueError:
        return 999  # rotasi tak dikenal ditaruh di akhir


def kesehatan_sort_key(value):
    if not value:
        return 999
    v = str(value).strip().upper()
    try:
        return KESEHATAN_ORDER.index(v)
    except ValueError:
        return 998  # kategori tak dikenal ditaruh di akhir (sebelum kosong)


def safe_int(value, default=-1):
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _parse_database_filters(args):
    filters = {}
    for level, col in FILTER_COLUMNS.items():
        val = args.get(level, "").strip()
        if val:
            filters[level] = val
    return filters


def _build_where_clause(filters, extra_conditions=None):
    conditions = list(extra_conditions or [])
    params = []
    for level, val in filters.items():
        col = FILTER_COLUMNS[level]
        if level == "tahun":
            conditions.append(f'"{col}"::text = %s')
        else:
            conditions.append(f'"{col}" = %s')
        params.append(val)

    allowed_estates = _get_allowed_estates()
    if allowed_estates is not None:
        conditions.append('"ESTATE" = ANY(%s)')
        params.append(allowed_estates)

    where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    return where_clause, params


@app.route(DASHBOARD_PREFIX + "/database")
@admin_required
def dashboard_database_overview():
    return render_template("database_overview.html", active="database")


@app.route(DASHBOARD_PREFIX + "/database/edit")
@admin_required
def dashboard_database_edit_page():
    return render_template("database_edit.html", active="database")


@app.route(DASHBOARD_PREFIX + "/database/inspect")
@admin_required
def dashboard_database_inspect():
    """Diagnostik sementara — lihat isi kolom asli sebelum desain chart final."""
    conn = get_db()
    if not conn:
        return jsonify({"error": "db_unavailable"}), 503
    try:
        cur = conn.cursor()
        result = {}

        cur.execute(f'SELECT "KESEHATAN", COUNT(*) FROM {KESEHATAN_TABLE} GROUP BY "KESEHATAN" ORDER BY COUNT(*) DESC')
        result["kesehatan_values"] = [{"value": r[0], "count": r[1]} for r in cur.fetchall()]

        cur.execute(f'SELECT "JENIS_TANAH", COUNT(*) FROM {KESEHATAN_TABLE} GROUP BY "JENIS_TANAH" ORDER BY COUNT(*) DESC')
        result["jenis_tanah_values"] = [{"value": r[0], "count": r[1]} for r in cur.fetchall()]

        cur.execute(
            f'SELECT column_name, data_type FROM information_schema.columns '
            f"WHERE table_name = '{KESEHATAN_TABLE}' ORDER BY ordinal_position"
        )
        result["columns"] = [{"name": r[0], "type": r[1]} for r in cur.fetchall()]

        cur.execute(
            f'SELECT column_name FROM information_schema.columns '
            f"WHERE table_name = '{KESEHATAN_TABLE}' AND column_name ILIKE '%id%'"
        )
        result["possible_id_columns"] = [r[0] for r in cur.fetchall()]

        cur.execute(f'SELECT * FROM {KESEHATAN_TABLE} LIMIT 3')
        sample_colnames = [desc[0] for desc in cur.description]
        sample_rows = cur.fetchall()
        result["sample_rows"] = [dict(zip(sample_colnames, row)) for row in sample_rows]

        cur.close(); conn.close()
        return jsonify(result)
    except Exception as e:
        print(f"[DATABASE INSPECT ERROR] {e}")
        return jsonify({"error": str(e)}), 500


@app.route(DASHBOARD_PREFIX + "/database/filters")
@admin_required
def dashboard_database_filters():
    level = request.args.get("level", "")
    column = FILTER_COLUMNS.get(level)
    if not column:
        return jsonify({"options": []}), 400

    parent_filters = {k: v for k, v in _parse_database_filters(request.args).items() if k != level}
    where_clause, params = _build_where_clause(parent_filters, extra_conditions=[f'"{column}" IS NOT NULL'])

    conn = get_db()
    if not conn:
        return jsonify({"options": []})
    try:
        cur = conn.cursor()
        cur.execute(f'SELECT DISTINCT "{column}" FROM {KESEHATAN_TABLE}{where_clause}', params)
        options = [r[0] for r in cur.fetchall() if r[0] is not None]
        cur.close(); conn.close()

        if level == "rotasi":
            options = sorted(set(options), key=rotasi_sort_key)
        elif level == "tahun":
            options = sorted(set(options), key=lambda v: safe_int(v), reverse=True)
        else:
            options = sorted(set(options), key=lambda v: str(v))

        return jsonify({"options": options})
    except Exception as e:
        print(f"[DATABASE FILTERS ERROR] {e}")
        return jsonify({"options": [], "error": str(e)}), 500


@app.route(DASHBOARD_PREFIX + "/database/latest")
@admin_required
def dashboard_database_latest():
    conn = get_db()
    if not conn:
        return jsonify({"tahun": None, "rotasi": None})
    try:
        cur = conn.cursor()
        cur.execute(
            f'SELECT DISTINCT "TAHUN", "ROTASI" FROM {KESEHATAN_TABLE} '
            f'WHERE "TAHUN" IS NOT NULL AND "ROTASI" IS NOT NULL'
        )
        rows = cur.fetchall()
        cur.close(); conn.close()

        if not rows:
            return jsonify({"tahun": None, "rotasi": None})

        rows_sorted = sorted(rows, key=lambda r: (safe_int(r[0]), rotasi_sort_key(r[1])), reverse=True)
        latest_tahun, latest_rotasi = rows_sorted[0]
        return jsonify({"tahun": latest_tahun, "rotasi": latest_rotasi})
    except Exception as e:
        print(f"[DATABASE LATEST ERROR] {e}")
        return jsonify({"tahun": None, "rotasi": None, "error": str(e)}), 500


@app.route(DASHBOARD_PREFIX + "/database/data")
@admin_required
def dashboard_database_data():
    filters = _parse_database_filters(request.args)
    page = request.args.get("page", 1, type=int)
    per_page = 50

    where_clause, params = _build_where_clause(filters)

    conn = get_db()
    if not conn:
        return jsonify({"error": "db_unavailable"}), 503

    try:
        cur = conn.cursor()

        cur.execute(f'SELECT COUNT(*) FROM {KESEHATAN_TABLE}{where_clause}', params)
        total_rows = cur.fetchone()[0]

        cur.execute(
            f'SELECT {KESEHATAN_NORMALIZE_SQL}, COUNT(DISTINCT ("ESTATE","DIVISI","NO_BLOK")), SUM("HA"::numeric) '
            f'FROM {KESEHATAN_TABLE}{where_clause} GROUP BY 1',
            params,
        )
        summary_rows = cur.fetchall()

        offset = (page - 1) * per_page
        cur.execute(
            f'SELECT ctid::text, "ESTATE", "REGION", "WILAYAH", "DIVISI", "NO_BLOK", '
            f'"ROTASI", "TAHUN", "KESEHATAN", "PKK", "HA", "PERSEN" '
            f'FROM {KESEHATAN_TABLE}{where_clause} '
            f'ORDER BY "ESTATE", "DIVISI", "NO_BLOK" LIMIT %s OFFSET %s',
            params + [per_page, offset],
        )
        data_rows = cur.fetchall()
        cur.close(); conn.close()

        total_blok = sum(r[1] for r in summary_rows)
        total_ha = sum(float(r[2] or 0) for r in summary_rows)

        def is_sehat(kesehatan_val):
            # Hanya "Green" murni dihitung Sehat — "Moderate Green" dianggap perlu perhatian
            return (kesehatan_val or "").strip().lower() == "green"

        sehat_ha = sum(float(r[2] or 0) for r in summary_rows if is_sehat(r[0]))
        pct_sehat = round((sehat_ha / total_ha * 100), 1) if total_ha else 0
        pct_perlu = round(100 - pct_sehat, 1) if total_ha else 0

        rows_json = [
            {
                "row_id": r[0], "estate": r[1], "region": r[2], "wilayah": r[3],
                "divisi": r[4], "no_blok": r[5], "rotasi": r[6], "tahun": r[7],
                "kesehatan": r[8], "pkk": r[9], "ha": r[10], "persen": r[11],
            }
            for r in data_rows
        ]

        return jsonify({
            "rows": rows_json,
            "total_rows": total_rows,
            "page": page,
            "per_page": per_page,
            "total_pages": max(1, (total_rows + per_page - 1) // per_page),
            "summary": {
                "total_blok": total_blok,
                "total_ha": total_ha,
                "pct_sehat": pct_sehat,
                "pct_perlu_perhatian": pct_perlu,
            },
        })
    except Exception as e:
        print(f"[DATABASE DATA ERROR] {e}")
        return jsonify({"error": str(e)}), 500


@app.route(DASHBOARD_PREFIX + "/database/chart-data")
@admin_required
def dashboard_database_chart_data():
    filters = _parse_database_filters(request.args)
    where_clause, params = _build_where_clause(filters)

    conn = get_db()
    if not conn:
        return jsonify({"error": "db_unavailable"}), 503

    try:
        cur = conn.cursor()

        # 1. Distribusi Kesehatan berdasarkan Luasan (HA) — horizontal bar (nilai dinormalisasi)
        cur.execute(
            f'SELECT {KESEHATAN_NORMALIZE_SQL} AS kesehatan_norm, SUM("HA"::numeric) '
            f'FROM {KESEHATAN_TABLE}{where_clause} GROUP BY 1',
            params,
        )
        kesehatan_dist = cur.fetchall()
        kesehatan_dist_sorted = sorted(kesehatan_dist, key=lambda r: kesehatan_sort_key(r[0]))

        # 2. Top 10 Estate Terburuk — HA "Need Improvement Soon" terluas (termasuk varian "NIS")
        nis_conditions, nis_params = _build_where_clause(filters)
        nis_having = f'HAVING {KESEHATAN_NORMALIZE_SQL} = \'Need Improvement Soon\''
        cur.execute(
            f'SELECT "ESTATE", SUM("HA"::numeric) AS total_ha FROM {KESEHATAN_TABLE}{nis_conditions} '
            f'GROUP BY "ESTATE", {KESEHATAN_NORMALIZE_SQL} {nis_having} '
            f'ORDER BY total_ha DESC NULLS LAST LIMIT 10',
            nis_params,
        )
        top_estates = cur.fetchall()

        # 3. Kesehatan per Jenis Tanah — stacked horizontal bar (kedua kolom dinormalisasi)
        cur.execute(
            f'SELECT {JENIS_TANAH_NORMALIZE_SQL} AS jenis_norm, {KESEHATAN_NORMALIZE_SQL} AS kesehatan_norm, SUM("HA"::numeric) '
            f'FROM {KESEHATAN_TABLE}{where_clause} GROUP BY 1, 2',
            params,
        )
        soil_rows = cur.fetchall()
        cur.close(); conn.close()

        soil_types = sorted({r[0] for r in soil_rows})
        kesehatan_categories = sorted({r[0] for r in kesehatan_dist}, key=kesehatan_sort_key)
        soil_matrix = {soil: {k: 0.0 for k in kesehatan_categories} for soil in soil_types}
        for jenis_tanah, kesehatan, ha in soil_rows:
            soil_matrix[jenis_tanah][kesehatan] = float(ha or 0)

        return jsonify({
            "kesehatan_distribution": {
                "labels": [r[0] for r in kesehatan_dist_sorted],
                "data": [float(r[1] or 0) for r in kesehatan_dist_sorted],
            },
            "top_estates_terburuk": {
                "labels": [r[0] or "(kosong)" for r in top_estates],
                "data": [float(r[1] or 0) for r in top_estates],
            },
            "kesehatan_per_jenis_tanah": {
                "soil_types": soil_types,
                "kesehatan_categories": kesehatan_categories,
                "matrix": soil_matrix,
            },
        })
    except Exception as e:
        print(f"[DATABASE CHART ERROR] {e}")
        return jsonify({"error": str(e)}), 500


@app.route(DASHBOARD_PREFIX + "/database/export")
@admin_required
def dashboard_database_export():
    filters = _parse_database_filters(request.args)
    where_clause, params = _build_where_clause(filters)

    conn = get_db()
    if not conn:
        return "Database tidak tersedia", 503

    try:
        import io
        import pandas as pd

        cur = conn.cursor()
        cur.execute(f'SELECT * FROM {KESEHATAN_TABLE}{where_clause} ORDER BY "ESTATE", "DIVISI", "NO_BLOK"', params)
        colnames = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
        cur.close(); conn.close()

        df = pd.DataFrame(rows, columns=colnames)
        buf = io.BytesIO()
        df.to_excel(buf, index=False, engine="openpyxl")
        buf.seek(0)

        return Response(
            buf.getvalue(),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=database_kesehatan_export.xlsx"},
        )
    except Exception as e:
        print(f"[DATABASE EXPORT ERROR] {e}")
        return f"Gagal export: {e}", 500


@app.route(DASHBOARD_PREFIX + "/database/row/add", methods=["POST"])
@admin_required
def dashboard_database_row_add():
    f = request.form
    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            col_list = ", ".join(f'"{c}"' for c in EDITABLE_COLUMNS)
            placeholders = ", ".join(["%s"] * len(EDITABLE_COLUMNS))
            values = [f.get(c.lower()) or None for c in EDITABLE_COLUMNS]
            cur.execute(f'INSERT INTO {KESEHATAN_TABLE} ({col_list}) VALUES ({placeholders})', values)
            conn.commit(); cur.close(); conn.close()
            return jsonify({"success": True})
        except Exception as e:
            print(f"[DATABASE ADD ERROR] {e}")
            return jsonify({"success": False, "message": str(e)}), 500
    return jsonify({"success": False, "message": "db_unavailable"}), 503


@app.route(DASHBOARD_PREFIX + "/database/row/update", methods=["POST"])
@admin_required
def dashboard_database_row_update():
    f = request.form
    row_id = f.get("row_id")
    if not row_id:
        return jsonify({"success": False, "message": "row_id wajib diisi"}), 400

    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            set_clause = ", ".join(f'"{c}" = %s' for c in EDITABLE_COLUMNS)
            values = [f.get(c.lower()) or None for c in EDITABLE_COLUMNS] + [row_id]
            cur.execute(f'UPDATE {KESEHATAN_TABLE} SET {set_clause} WHERE ctid = %s::tid', values)
            conn.commit(); cur.close(); conn.close()
            return jsonify({"success": True})
        except Exception as e:
            print(f"[DATABASE UPDATE ERROR] {e}")
            return jsonify({"success": False, "message": str(e)}), 500
    return jsonify({"success": False, "message": "db_unavailable"}), 503


@app.route(DASHBOARD_PREFIX + "/database/row/delete", methods=["POST"])
@admin_required
def dashboard_database_row_delete():
    row_id = request.form.get("row_id")
    if not row_id:
        return jsonify({"success": False, "message": "row_id wajib diisi"}), 400

    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(f'DELETE FROM {KESEHATAN_TABLE} WHERE ctid = %s::tid', (row_id,))
            conn.commit(); cur.close(); conn.close()
            return jsonify({"success": True})
        except Exception as e:
            print(f"[DATABASE DELETE ERROR] {e}")
            return jsonify({"success": False, "message": str(e)}), 500
    return jsonify({"success": False, "message": "db_unavailable"}), 503


# ── Automatic Thematic Mapping (ATM) ────────────────────────────────────────────
# Wizard 3 langkah (Sumberdata Excel → Tematik & Metadata → Peta & Print), meniru UX
# fitur sejenis di gismaps.bumitama.com. Beda dari Batch/Custom/dst: TIDAK ada worker
# thread/job-queue — Excel parse + join atribut Blok_ID ke Aresta ringan (bukan GPU),
# jadi semua route di bawah ini sinkron (baca/hitung/balas dalam 1 request).
# Phase 1: upload + peek kolom + preview join. Styling/map/history menyusul.
def _thematic_safe_path(path):
    """Path Excel yang diproses di sini SELALU dari THEMATIC_INCOMING (hasil upload
    lewat /thematic/upload) — bukan file browser bebas seperti /batch/browse, jadi
    confine ke folder itu (beda dari /rekap-data/peek yang memang sengaja menerima
    path server manapun)."""
    if not path:
        return None
    candidate = os.path.abspath(path)
    root_abs = os.path.abspath(THEMATIC_INCOMING)
    if os.path.commonpath([candidate, root_abs]) != root_abs:
        return None
    return candidate if os.path.isfile(candidate) else None


@app.route(DASHBOARD_PREFIX + "/thematic")
@admin_required
def dashboard_thematic_new():
    return render_template("thematic_mapping.html", active="thematic_mapping")


@app.route(DASHBOARD_PREFIX + "/thematic/upload", methods=["POST"])
@admin_required
def dashboard_thematic_upload():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"success": False, "message": "Tidak ada file"}), 400
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_THEMATIC_EXTENSIONS:
        return jsonify({"success": False, "message": f"Ekstensi {ext} tidak diizinkan"}), 400
    filename = secure_filename(file.filename)
    save_path = os.path.join(THEMATIC_INCOMING, f"{int(datetime.now().timestamp())}_{filename}")
    file.save(save_path)
    return jsonify({"success": True, "path": save_path, "filename": filename})


@app.route(DASHBOARD_PREFIX + "/thematic/sheets")
@admin_required
def dashboard_thematic_sheets():
    from Processing.thematic_mapping import excel_io

    path = _thematic_safe_path(request.args.get("path", "").strip())
    if not path:
        return jsonify({"error": "File tidak ditemukan"}), 400
    try:
        return jsonify({"sheets": excel_io.list_sheets(path)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route(DASHBOARD_PREFIX + "/thematic/peek")
@admin_required
def dashboard_thematic_peek():
    from Processing.thematic_mapping import excel_io

    path = _thematic_safe_path(request.args.get("path", "").strip())
    sheet = request.args.get("sheet", "").strip()
    if not path or not sheet:
        return jsonify({"error": "File/sheet tidak ditemukan"}), 400
    try:
        columns = excel_io.peek_columns(path, sheet)
        blok_guess = next((c for c in columns if c.strip().lower() == "blok_id"), columns[0] if columns else "")
        return jsonify({"columns": columns, "blok_id_guess": blok_guess})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route(DASHBOARD_PREFIX + "/thematic/join-preview", methods=["POST"])
@admin_required
def dashboard_thematic_join_preview():
    from Processing.thematic_mapping import excel_io, blok_join
    import batch_worker

    payload = request.get_json(silent=True) or {}
    path = _thematic_safe_path((payload.get("path") or "").strip())
    sheet = (payload.get("sheet") or "").strip()
    blok_col = (payload.get("blok_col") or "").strip()
    if not path or not sheet or not blok_col:
        return jsonify({"error": "path/sheet/blok_col wajib diisi"}), 400
    if not batch_worker.ARESTA_PATH or not os.path.isfile(batch_worker.ARESTA_PATH):
        return jsonify({"error": "Aresta.shp tidak ditemukan di server ini"}), 500

    try:
        df = excel_io.read_sheet(path, sheet)
        _joined, stats = blok_join.join_excel_to_aresta(df, blok_col, batch_worker.ARESTA_PATH, THEMATIC_WORK)
        return jsonify(stats)
    except Exception as e:
        print(f"[THEMATIC JOIN PREVIEW ERROR] {e}")
        return jsonify({"error": str(e)}), 500


@app.route(DASHBOARD_PREFIX + "/thematic/categories")
@admin_required
def dashboard_thematic_categories():
    """Nilai unik kolom tematik terpilih -- dipakai Step 2 buat auto-isi daftar
    kategori "Warna" (tiap nilai dapat 1 baris color-picker)."""
    from Processing.thematic_mapping import excel_io

    path = _thematic_safe_path(request.args.get("path", "").strip())
    sheet = request.args.get("sheet", "").strip()
    column = request.args.get("column", "").strip()
    if not path or not sheet or not column:
        return jsonify({"error": "path/sheet/column wajib diisi"}), 400
    try:
        categories, total_unique = excel_io.distinct_values(path, sheet, column)
        return jsonify({"categories": categories, "total_unique": total_unique,
                         "default_palette": DEFAULT_CLASS_PALETTE})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route(DASHBOARD_PREFIX + "/thematic/save", methods=["POST"])
@admin_required
def dashboard_thematic_save():
    """Simpan 1 sesi ATM (Step 1 + Step 2) sebagai riwayat permanen -- bukan
    job-queue, jadi langsung INSERT/UPDATE sinkron di request ini."""
    payload = request.get_json(silent=True) or {}
    session_id = payload.get("id")

    title = (payload.get("title") or "").strip() or "Untitled"
    excel_source_path = (payload.get("excel_source_path") or "").strip()
    excel_source_filename = (payload.get("excel_source_filename") or "").strip()
    sheet_name = (payload.get("sheet_name") or "").strip()
    blok_id_column = (payload.get("blok_id_column") or "Blok_ID").strip()
    thematic_column = (payload.get("thematic_column") or "").strip()
    join_matched_count = payload.get("join_matched_count")
    join_total_count = payload.get("join_total_count")
    join_unmatched_blok_ids = json.dumps((payload.get("join_unmatched_blok_ids") or [])[:50])
    thematic_type = (payload.get("thematic_type") or "warna").strip()
    category_styles = json.dumps(payload.get("category_styles") or {})
    polygon_opacity = payload.get("polygon_opacity", 0.75)
    polygon_line_color = (payload.get("polygon_line_color") or "#333333").strip()
    polygon_line_width = payload.get("polygon_line_width", 1)
    # description membawa JSON titleLines (baris judul bebas) dari frontend -- lihat
    # collectSessionPayload() di thematic_mapping.html, bukan teks bebas biasa.
    description = payload.get("description") or ""
    label_overrides = json.dumps(payload.get("label_overrides") or {})
    orientation_override = payload.get("orientation_override") or ""
    scale_override = payload.get("scale_override") or ""

    if not excel_source_path or not thematic_column:
        return jsonify({"success": False, "message": "excel_source_path/thematic_column wajib diisi"}), 400

    conn = get_db()
    if not conn:
        return jsonify({"success": False, "message": "Gagal konek DB"}), 500
    try:
        cur = conn.cursor()
        if session_id:
            cur.execute(
                "UPDATE thematic_mapping_sessions SET title=%s, excel_source_path=%s, "
                "excel_source_filename=%s, sheet_name=%s, blok_id_column=%s, thematic_column=%s, "
                "join_matched_count=%s, join_total_count=%s, join_unmatched_blok_ids=%s, "
                "thematic_type=%s, category_styles=%s, polygon_opacity=%s, polygon_line_color=%s, "
                "polygon_line_width=%s, description=%s, label_overrides=%s, orientation_override=%s, "
                "scale_override=%s, updated_at=NOW() WHERE id=%s",
                (title, excel_source_path, excel_source_filename, sheet_name, blok_id_column,
                 thematic_column, join_matched_count, join_total_count, join_unmatched_blok_ids,
                 thematic_type, category_styles, polygon_opacity, polygon_line_color,
                 polygon_line_width, description, label_overrides, orientation_override,
                 scale_override, session_id),
            )
        else:
            cur.execute(
                "INSERT INTO thematic_mapping_sessions (created_by, title, excel_source_path, "
                "excel_source_filename, sheet_name, blok_id_column, thematic_column, "
                "join_matched_count, join_total_count, join_unmatched_blok_ids, thematic_type, "
                "category_styles, polygon_opacity, polygon_line_color, polygon_line_width, "
                "description, label_overrides, orientation_override, scale_override) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (session.get("admin_user"), title, excel_source_path, excel_source_filename,
                 sheet_name, blok_id_column, thematic_column, join_matched_count, join_total_count,
                 join_unmatched_blok_ids, thematic_type, category_styles, polygon_opacity,
                 polygon_line_color, polygon_line_width, description, label_overrides,
                 orientation_override, scale_override),
            )
            session_id = cur.fetchone()[0]
        conn.commit(); cur.close(); conn.close()
        return jsonify({"success": True, "id": session_id})
    except Exception as e:
        print(f"[THEMATIC SAVE ERROR] {e}")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route(DASHBOARD_PREFIX + "/thematic/estates")
@admin_required
def dashboard_thematic_estates():
    from Processing.thematic_mapping import blok_join
    import batch_worker

    if not batch_worker.ARESTA_PATH or not os.path.isfile(batch_worker.ARESTA_PATH):
        return jsonify({"error": "Aresta.shp tidak ditemukan di server ini"}), 500
    try:
        return jsonify({"estates": blok_join.list_estates(batch_worker.ARESTA_PATH)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route(DASHBOARD_PREFIX + "/thematic/geojson", methods=["POST"])
@admin_required
def dashboard_thematic_geojson():
    """Sinkron (bukan job-queue) -- Excel di-parse + di-join ulang ke Aresta tiap
    panggilan (tidak ada file GeoJSON tersimpan seperti Custom/TBM), karena input
    Excel-nya kecil dan prosesnya ringan. Dibatasi ke `estates` yang dipilih user di
    "Pilih region" supaya payload tidak membengkak kalau Aresta.shp cakupannya luas."""
    from Processing.thematic_mapping import excel_io, blok_join
    import batch_worker

    payload = request.get_json(silent=True) or {}
    path = _thematic_safe_path((payload.get("path") or "").strip())
    sheet = (payload.get("sheet") or "").strip()
    blok_col = (payload.get("blok_col") or "").strip()
    thematic_col = (payload.get("thematic_col") or "").strip()
    estates = payload.get("estates") or None
    if not path or not sheet or not blok_col or not thematic_col:
        return jsonify({"error": "path/sheet/blok_col/thematic_col wajib diisi"}), 400
    if not batch_worker.ARESTA_PATH or not os.path.isfile(batch_worker.ARESTA_PATH):
        return jsonify({"error": "Aresta.shp tidak ditemukan di server ini"}), 500

    try:
        df = excel_io.read_sheet(path, sheet)
        geojson_str, _stats = blok_join.build_map_geojson(
            df, blok_col, thematic_col, batch_worker.ARESTA_PATH, THEMATIC_WORK, estates=estates
        )
        return Response(geojson_str, mimetype="application/json")
    except Exception as e:
        print(f"[THEMATIC GEOJSON ERROR] {e}")
        return jsonify({"error": str(e)}), 500


@app.route(DASHBOARD_PREFIX + "/thematic/export-png", methods=["POST"])
@admin_required
def dashboard_thematic_export_png():
    """Render halaman cetak di Chromium headless (Playwright) lalu screenshot elemen
    petanya server-side -- BUKAN html2canvas di browser user. html2canvas terbukti
    tidak bisa diandalkan untuk tile Leaflet (transform CSS-nya sering salah terbaca,
    hasilnya "pecah"/geser dari yang ditampilkan) -- screenshot browser sungguhan tidak
    kena masalah itu sama sekali karena yang di-capture adalah render asli, bukan
    simulasi ulang DOM ke canvas. Payload berisi SEMUA state yang sudah diselesaikan
    di sisi client (judul, override teks, style, posisi/zoom peta persis) supaya
    /thematic/print-frame tidak perlu menebak ulang apa pun."""
    payload = request.get_json(silent=True) or {}
    if not payload.get("path") or not payload.get("thematic_col"):
        return jsonify({"error": "Data peta tidak lengkap"}), 400

    token = _print_export_put(payload)
    print_url = f"http://127.0.0.1:{PORT}{DASHBOARD_PREFIX}/thematic/print-frame?token={token}"

    try:
        # Env var browser path HARUS di-set SEBELUM import playwright -- service jalan
        # sebagai akun Local System (nssm), profile-nya beda dari akun yang dipakai
        # instal browsernya, pola identik dengan Processing/log_drone/opendronelog_client.py.
        os.environ.setdefault(
            "PLAYWRIGHT_BROWSERS_PATH",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "ms-playwright"),
        )
        from playwright.sync_api import sync_playwright
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                # device_scale_factor 3.125 = 300/96 -- halaman cetak di-desain di
                # thematic_print_frame.html dalam skala ~96 DPI (gampang dipikirkan,
                # sama kayak layar biasa), Playwright yang mengalikannya ke resolusi
                # cetak ~300 DPI saat screenshot (elemen #mapWrapper 1587x1122px
                # jadi ~4960x3507px, mendekati A3 sungguhan 420x297mm di 300 DPI).
                page = browser.new_page(viewport={"width": 1650, "height": 1180}, device_scale_factor=3.125)
                page.goto(print_url, timeout=30000, wait_until="load")
                try:
                    page.wait_for_function("window.__mapReady === true", timeout=20000)
                except PlaywrightTimeoutError:
                    pass  # tile lambat/situs OSM sibuk -- tetap lanjut screenshot apa adanya, lebih baik daripada gagal total
                page.wait_for_timeout(300)
                png_bytes = page.locator("#mapWrapper").screenshot()
            finally:
                browser.close()
    except Exception as e:
        print(f"[THEMATIC EXPORT PNG ERROR] {e}")
        return jsonify({"error": f"Gagal render PNG: {e}"}), 500
    finally:
        _PRINT_EXPORT_CACHE.pop(token, None)

    return Response(
        png_bytes, mimetype="image/png",
        headers={"Content-Disposition": "attachment; filename=peta-tematik.png"},
    )


@app.route(DASHBOARD_PREFIX + "/thematic/print-frame")
def dashboard_thematic_print_frame():
    """TANPA @admin_required -- ini dibuka oleh Chromium headless-nya Playwright
    sendiri (browser context baru, sama sekali tidak punya cookie sesi admin), bukan
    browser user. Token sekali-pakai (dari dashboard_thematic_export_png(), kadaluarsa
    60 detik) yang jadi gerbang aksesnya, bukan session cookie -- _dashboard_access_gate
    tidak memblokir ini karena request tanpa session tetap diloloskan ke route (lihat
    komentar di _dashboard_access_gate), dan route ini sengaja tidak dipasangi
    admin_required."""
    token = request.args.get("token", "")
    payload = _print_export_get(token)
    if not payload:
        abort(404)

    from Processing.thematic_mapping import excel_io, blok_join
    import batch_worker

    # Sama seperti /thematic/geojson (validasi path, biar tidak keluar dari folder
    # upload) -- sebelumnya route ini pakai payload.get("path") mentah tanpa divalidasi.
    safe_path = _thematic_safe_path((payload.get("path") or "").strip())

    geojson_str = "{}"
    geojson_error = None
    try:
        if not safe_path:
            raise ValueError(f"Path tidak valid/tidak ditemukan: {payload.get('path')!r}")
        df = excel_io.read_sheet(safe_path, payload.get("sheet"))
        geojson_str, _stats = blok_join.build_map_geojson(
            df, payload.get("blok_col"), payload.get("thematic_col"),
            batch_worker.ARESTA_PATH, THEMATIC_WORK, estates=payload.get("estates") or None,
        )
    except Exception as e:
        # Sebelumnya cuma di-print ke log server (kalau service-nya tidak dipantau,
        # gagalnya jadi SENYAP -- hasil PNG cuma kelihatan "peta kosong" tanpa
        # petunjuk kenapa). Sekarang pesan errornya juga ditanam ke halaman cetak
        # sendiri (lihat thematic_print_frame.html) supaya kelihatan langsung di
        # hasil export kalau ini terulang.
        geojson_error = str(e)
        print(f"[PRINT FRAME GEOJSON ERROR] {e}")

    # payload berisi teks bebas dari user (judul, override label, dll) yang ditanam
    # langsung ke dalam <script> -- "</" di-escape supaya tidak ada nilai yang bisa
    # menutup tag script lebih awal dan menyuntik HTML/JS lain (XSS lewat isi Excel/teks
    # yang diketik user, walau ini fitur admin-only, tetap dijaga aman by default).
    return render_template(
        "thematic_print_frame.html", payload=payload, geojson_error=geojson_error,
        geojson_json=geojson_str.replace("</", "<\\/"),
        payload_json=json.dumps(payload).replace("</", "<\\/"),
    )


# ── Deteksi RAW Foto — foto drone biasa (PNG/JPG/TIF), model tetap, tanpa splitter ──
def _raw_photo_jobs():
    conn = get_db()
    jobs = []
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, job_name, status, error_message, result_total_detections,
                       result_class_counts, gps_lat, created_at, finished_at
                FROM raw_photo_jobs ORDER BY id DESC
            """)
            jobs = cur.fetchall()
            cur.close(); conn.close()
        except Exception as e:
            print(f"[RAW LIST ERROR] {e}")
    return jobs


@app.route(DASHBOARD_PREFIX + "/raw")
@admin_required
def dashboard_raw():
    return render_template("raw_photo.html", active="raw", jobs=_raw_photo_jobs())


@app.route(DASHBOARD_PREFIX + "/raw/upload", methods=["POST"])
@admin_required
def dashboard_raw_upload():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"success": False, "message": "Tidak ada file"}), 400
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_RAW_EXTENSIONS:
        return jsonify({"success": False, "message": f"Ekstensi {ext} tidak diizinkan"}), 400
    filename = secure_filename(file.filename)
    save_path = os.path.join(RAW_INCOMING, f"{int(datetime.now().timestamp())}_{filename}")
    file.save(save_path)
    return jsonify({"success": True, "path": save_path})


@app.route(DASHBOARD_PREFIX + "/raw/browse")
@admin_required
def dashboard_raw_browse():
    """Reuse browser filesystem yang sama dengan menu lain (admin only)."""
    return dashboard_batch_browse()


@app.route(DASHBOARD_PREFIX + "/raw/submit", methods=["POST"])
@admin_required
def dashboard_raw_submit():
    f = request.form
    job_name = f.get("job_name", "").strip() or "Deteksi RAW Foto"
    input_path = f.get("input_path", "").strip()
    if not input_path or not os.path.exists(input_path):
        return jsonify({"success": False, "message": "File input tidak ditemukan"}), 400

    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO raw_photo_jobs (created_by, job_name, input_path, status) "
                "VALUES (%s, %s, %s, 'pending')",
                (session.get("admin_user"), job_name, input_path),
            )
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[RAW SUBMIT ERROR] {e}")
            return jsonify({"success": False, "message": str(e)}), 500
    return jsonify({"success": True})


@app.route(DASHBOARD_PREFIX + "/raw/status")
@admin_required
def dashboard_raw_status():
    jobs = []
    for row in _raw_photo_jobs():
        jobs.append({
            "id": row[0], "job_name": row[1], "status": row[2], "error_message": row[3],
            "result_total_detections": row[4], "result_class_counts": row[5],
            "gps_lat": row[6],
            "created_at": row[7].isoformat() if row[7] else None,
            "finished_at": row[8].isoformat() if row[8] else None,
        })
    return jsonify({"jobs": jobs})


@app.route(DASHBOARD_PREFIX + "/raw/delete/<int:job_id>", methods=["POST"])
@admin_required
def dashboard_raw_delete(job_id):
    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM raw_photo_jobs WHERE id = %s AND status != 'processing'", (job_id,))
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[RAW DELETE ERROR] {e}")
    return redirect(url_for("dashboard_raw"))


@app.route(DASHBOARD_PREFIX + "/raw/result/<int:job_id>")
@admin_required
def dashboard_raw_result(job_id):
    conn = get_db()
    job = None
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, job_name, status, result_image_path, result_total_detections,
                       result_class_counts, class_names, gps_lat, gps_lon, bounds_json, finished_at
                FROM raw_photo_jobs WHERE id = %s AND status = 'done'
            """, (job_id,))
            row = cur.fetchone()
            cur.close(); conn.close()
            if row:
                job = {
                    "id": row[0], "job_name": row[1], "status": row[2], "result_image_path": row[3],
                    "result_total_detections": row[4], "result_class_counts": row[5],
                    "class_names": row[6], "gps_lat": row[7], "gps_lon": row[8],
                    "bounds_json": row[9], "finished_at": row[10],
                }
        except Exception as e:
            print(f"[RAW RESULT ERROR] {e}")
    if not job:
        return redirect(url_for("dashboard_raw"))
    return render_template("raw_photo_result.html", active="raw", job=job)


@app.route(DASHBOARD_PREFIX + "/raw/result/<int:job_id>/image")
@admin_required
def dashboard_raw_result_image(job_id):
    conn = get_db()
    image_path = None
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("SELECT result_image_path FROM raw_photo_jobs WHERE id = %s AND status = 'done'", (job_id,))
            row = cur.fetchone()
            cur.close(); conn.close()
            if row:
                image_path = row[0]
        except Exception as e:
            print(f"[RAW IMAGE ERROR] {e}")
    if not image_path or not os.path.exists(image_path):
        return "Gambar tidak ditemukan", 404
    return Response(open(image_path, "rb").read(), mimetype="image/jpeg")


@app.route(DASHBOARD_PREFIX + "/raw/log")
@admin_required
def dashboard_raw_log():
    log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "raw_photo_worker.log")
    if not os.path.exists(log_file):
        return Response("Log file belum ada — worker belum pernah jalan.", mimetype="text/plain")
    try:
        n_lines = request.args.get("lines", 300, type=int)
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        content = "".join(lines[-n_lines:])
        return Response(content or "(log kosong)", mimetype="text/plain")
    except Exception as e:
        return Response(f"Gagal membaca log: {e}", mimetype="text/plain"), 500


# ── Log Drone — flight log DJI (.txt) lewat app.opendronelog.com ──────────────
# 1 upload bisa berisi banyak file .txt sekaligus, semuanya digabung jadi SATU
# rekap (GPX/Excel/SHP gabungan, durasi & jarak dijumlah) -- lihat
# Processing/log_drone/drone_log_worker.py. Peta (PNG) & Report (PPTX) SENGAJA
# tidak dibuat worker -- baru dirender LAZY saat pertama kali di-download (lihat
# _build_drone_map()/_build_drone_pptx() di bawah), supaya batch cepat "selesai".
def _drone_log_upload_rows():
    conn = get_db()
    rows = []
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, batch_name, status, error_message, drone_sn, duration_s,
                       distance_m, created_at, worker_host, files_total
                FROM drone_report_uploads
                ORDER BY id DESC
            """)
            rows = cur.fetchall()
            cur.close(); conn.close()
        except Exception as e:
            print(f"[DRONE LIST ERROR] {e}")
    return rows


@app.route(DASHBOARD_PREFIX + "/drone")
@admin_required
def dashboard_drone():
    return render_template("drone_log.html", active="drone", uploads=_drone_log_upload_rows())


@app.route(DASHBOARD_PREFIX + "/drone/upload", methods=["POST"])
@admin_required
def dashboard_drone_upload():
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        return jsonify({"success": False, "message": "Tidak ada file dipilih"}), 400

    invalid = [f.filename for f in files if os.path.splitext(f.filename)[1].lower() not in ALLOWED_DRONE_EXTENSIONS]
    if invalid:
        return jsonify({"success": False, "message": f"Bukan file .txt: {', '.join(invalid)}"}), 400

    conn = get_db()
    if not conn:
        return jsonify({"success": False, "message": "Database tidak tersedia"}), 500
    try:
        cur = conn.cursor()
        batch_name = request.form.get("batch_name", "").strip() or f"Upload {datetime.now().strftime('%d %b %Y %H:%M')}"
        pilot_name = request.form.get("nama_user", "").strip() or session.get("admin_user")
        drone_code = request.form.get("kode_drone", "").strip() or None
        cur.execute(
            "INSERT INTO drone_report_uploads (created_by, batch_name, files_total, pilot_name, drone_code) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (session.get("admin_user"), batch_name, len(files), pilot_name, drone_code),
        )
        upload_id = cur.fetchone()[0]

        ts_ms = int(datetime.now().timestamp() * 1000)
        for i, f in enumerate(files):
            filename = secure_filename(f.filename)
            save_path = os.path.join(DRONE_INCOMING, f"{ts_ms}_{i}_{filename}")
            f.save(save_path)
            cur.execute(
                "INSERT INTO drone_report_files (upload_id, original_filename, input_path, status) "
                "VALUES (%s, %s, %s, 'pending')",
                (upload_id, f.filename, save_path),
            )
        conn.commit(); cur.close(); conn.close()
        return jsonify({"success": True, "upload_id": upload_id, "files": len(files)})
    except Exception as e:
        print(f"[DRONE UPLOAD ERROR] {e}")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route(DASHBOARD_PREFIX + "/drone/status")
@admin_required
def dashboard_drone_status():
    uploads = []
    for row in _drone_log_upload_rows():
        uploads.append({
            "id": row[0], "batch_name": row[1], "status": row[2], "error_message": row[3],
            "drone_sn": row[4], "duration_s": row[5], "distance_m": row[6],
            "created_at": row[7].isoformat() if row[7] else None,
            "worker_host": row[8], "files_total": row[9],
        })
    return jsonify({"uploads": uploads})


@app.route(DASHBOARD_PREFIX + "/drone/delete/<int:upload_id>", methods=["POST"])
@admin_required
def dashboard_drone_delete(upload_id):
    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM drone_report_files WHERE upload_id = %s AND upload_id IN "
                "(SELECT id FROM drone_report_uploads WHERE id = %s AND status != 'processing')",
                (upload_id, upload_id),
            )
            cur.execute(
                "DELETE FROM drone_report_uploads WHERE id = %s AND status != 'processing'",
                (upload_id,),
            )
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[DRONE DELETE ERROR] {e}")
    return redirect(url_for("dashboard_drone"))


def _get_drone_upload(upload_id):
    conn = get_db()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, batch_name, status, error_message, drone_sn, duration_s,
                   distance_m, result_gpx_path, result_xlsx_path, result_map_path,
                   finished_at, worker_host, files_total, result_pptx_path, created_by,
                   result_shp_path, pilot_name, drone_code
            FROM drone_report_uploads
            WHERE id = %s
        """, (upload_id,))
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close()
            return None
        cur.execute(
            "SELECT original_filename FROM drone_report_files WHERE upload_id = %s ORDER BY id ASC",
            (upload_id,),
        )
        filenames = [r[0] for r in cur.fetchall()]
        cur.close(); conn.close()
    except Exception as e:
        print(f"[DRONE GET ERROR] {e}")
        return None
    if not row:
        return None
    return {
        "id": row[0], "batch_name": row[1], "status": row[2], "error_message": row[3],
        "drone_sn": row[4], "duration_s": row[5], "distance_m": row[6],
        "result_gpx_path": row[7], "result_xlsx_path": row[8], "result_map_path": row[9],
        "finished_at": row[10], "worker_host": row[11], "files_total": row[12],
        "result_pptx_path": row[13], "created_by": row[14], "result_shp_path": row[15],
        "pilot_name": row[16], "drone_code": row[17], "filenames": filenames,
    }


def _drone_peer_redirect(f):
    """2 server berbagi 1 DB tapi TIDAK berbagi disk -- kalau batch ini diproses server
    LAIN (worker_host beda dari hostname server yang sedang melayani request ini),
    hasilnya (gpx/xlsx/shp/peta/pptx) cuma ada di disk server itu, bukan di sini.
    Return URL redirect ke server yang benar (pakai AGRIPALM_PEER_URL + path+query
    yang sama persis) kalau memang beda server & peer URL sudah dikonfigurasi, else
    None (artinya: lanjut proses di server ini seperti biasa / batch memang lokal)."""
    worker_host = (f or {}).get("worker_host")
    if not worker_host or worker_host == THIS_HOSTNAME:
        return None
    if not AGRIPALM_PEER_URL:
        return None
    return AGRIPALM_PEER_URL + request.full_path


@app.route(DASHBOARD_PREFIX + "/drone/<int:upload_id>")
@admin_required
def dashboard_drone_detail(upload_id):
    f = _get_drone_upload(upload_id)
    if not f:
        return redirect(url_for("dashboard_drone"))
    peer_url = _drone_peer_redirect(f)
    if peer_url:
        return redirect(peer_url)
    if f["worker_host"] and f["worker_host"] != THIS_HOSTNAME:
        # Beda server tapi AGRIPALM_PEER_URL belum diatur -- daripada diam-diam
        # gagal, kasih tahu jelas server mana yang harus dipakai.
        return render_template(
            "drone_log_detail.html", active="drone", file=f,
            wrong_server_host=f["worker_host"],
        )
    return render_template("drone_log_detail.html", active="drone", file=f)


@app.route(DASHBOARD_PREFIX + "/drone/<int:upload_id>/geojson")
@admin_required
def dashboard_drone_geojson(upload_id):
    f = _get_drone_upload(upload_id)
    if not f:
        return jsonify({"error": "Data belum tersedia"}), 404
    peer_url = _drone_peer_redirect(f)
    if peer_url:
        return redirect(peer_url)
    if f["worker_host"] and f["worker_host"] != THIS_HOSTNAME:
        return jsonify({"error": f"Batch ini diproses di server '{f['worker_host']}', buka Log Drone dari server itu."}), 409
    if f["status"] != "done":
        return jsonify({"error": "Data belum tersedia"}), 404
    conn = get_db()
    if not conn:
        return jsonify({"error": "Database tidak tersedia"}), 500
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT result_json_path, original_filename FROM drone_report_files "
            "WHERE upload_id = %s AND status = 'done' ORDER BY id ASC",
            (upload_id,),
        )
        file_rows = cur.fetchall()
        cur.close(); conn.close()

        from Processing.log_drone.drone_export import load_rows
        from Processing.log_drone.drone_map import build_track_geojson_multi, build_aresta_geojson_multi

        all_rows = []
        work_dir = None
        for track_idx, (json_path, filename) in enumerate(file_rows):
            if not json_path or not os.path.exists(json_path):
                continue
            rows, _ = load_rows(json_path)
            if work_dir is None:
                work_dir = os.path.dirname(json_path)
            for r in rows:
                all_rows.append({**r, "track_idx": track_idx, "source_file": filename})

        if not all_rows:
            return jsonify({"error": "Data belum tersedia"}), 404

        return jsonify({
            "track": build_track_geojson_multi(all_rows),
            "blocks": build_aresta_geojson_multi(all_rows, work_dir=work_dir),
        })
    except Exception as e:
        print(f"[DRONE GEOJSON ERROR] {e}")
        return jsonify({"error": str(e)}), 500


def _load_drone_rows(upload_id):
    conn = get_db()
    if not conn:
        return [], {}
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT original_filename, result_json_path FROM drone_report_files "
            "WHERE upload_id = %s AND status = 'done' ORDER BY id ASC",
            (upload_id,),
        )
        file_rows = cur.fetchall()
        cur.close(); conn.close()
    except Exception as e:
        print(f"[DRONE LOAD ROWS ERROR] {e}")
        return [], {}

    from Processing.log_drone.drone_export import load_rows

    all_rows = []
    file_metas = {}
    for track_idx, (filename, json_path) in enumerate(file_rows):
        if not json_path or not os.path.exists(json_path):
            continue
        rows, flight_meta = load_rows(json_path)
        for r in rows:
            all_rows.append({**r, "track_idx": track_idx, "source_file": filename})
        file_metas[track_idx] = (flight_meta, filename)
    return all_rows, file_metas


def _build_drone_map(upload_id):
    all_rows, file_metas = _load_drone_rows(upload_id)
    if not all_rows:
        return None

    conn = get_db()
    created_by = None
    out_dir = None
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("SELECT created_by FROM drone_report_uploads WHERE id = %s", (upload_id,))
            row = cur.fetchone()
            created_by = row[0] if row else None
            cur.execute(
                "SELECT result_json_path FROM drone_report_files "
                "WHERE upload_id = %s AND status = 'done' AND result_json_path IS NOT NULL LIMIT 1",
                (upload_id,),
            )
            path_row = cur.fetchone()
            out_dir = os.path.dirname(path_row[0]) if path_row else None
            cur.close(); conn.close()
        except Exception as e:
            print(f"[DRONE MAP BUILD ERROR] {e}")

    if not out_dir:
        return None

    from Processing.log_drone.drone_map import render_flight_map_png_multi

    png_path = os.path.join(out_dir, f"batch_{upload_id}_peta.png")
    try:
        render_flight_map_png_multi(
            all_rows, png_path, work_dir=out_dir, file_metas=file_metas, uploader_name=created_by,
        )
    except Exception as e:
        print(f"[DRONE MAP RENDER ERROR] {e}")
        return None

    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("UPDATE drone_report_uploads SET result_map_path = %s WHERE id = %s", (png_path, upload_id))
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[DRONE MAP SAVE ERROR] {e}")
    return png_path


def _build_drone_pptx(upload_id):
    u = _get_drone_upload(upload_id)
    if not u:
        return None
    map_path = u.get("result_map_path")
    if not map_path or not os.path.exists(map_path):
        map_path = _build_drone_map(upload_id)
    if not map_path:
        return None

    all_rows, file_metas = _load_drone_rows(upload_id)

    from Processing.log_drone.drone_export import export_pptx_report
    from Processing.log_drone.drone_map import (
        compute_cluster_recaps, render_flight_map_png_compact,
        compute_estate_names, compute_flight_period, compute_flight_days_count,
    )

    out_dir = os.path.dirname(map_path)

    compact_map_path = os.path.join(out_dir, f"batch_{upload_id}_peta_compact.png")
    try:
        render_flight_map_png_compact(all_rows, compact_map_path, work_dir=out_dir, file_metas=file_metas)
    except Exception as e:
        print(f"[DRONE PPTX MAP ERROR] {e}")
        compact_map_path = None

    cluster_rows = compute_cluster_recaps(all_rows, file_metas, u.get("pilot_name"))
    estate_names = compute_estate_names(all_rows, work_dir=out_dir)
    flight_start, flight_end = compute_flight_period(file_metas)
    flight_days_count = compute_flight_days_count(file_metas)

    pptx_path = os.path.join(out_dir, f"batch_{upload_id}_report.pptx")
    try:
        export_pptx_report(
            pptx_path, u["batch_name"], u.get("pilot_name"), datetime.now(), upload_id,
            u.get("duration_s"), u.get("distance_m"), u.get("drone_sn"), u.get("drone_code"),
            u.get("files_total"), flight_days_count, flight_start, flight_end,
            estate_names, compact_map_path, cluster_rows,
        )
    except Exception as e:
        print(f"[DRONE PPTX RENDER ERROR] {e}")
        return None

    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("UPDATE drone_report_uploads SET result_pptx_path = %s WHERE id = %s", (pptx_path, upload_id))
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[DRONE PPTX SAVE ERROR] {e}")
    return pptx_path


@app.route(DASHBOARD_PREFIX + "/drone/<int:upload_id>/download/<kind>")
@admin_required
def dashboard_drone_download(upload_id, kind):
    f = _get_drone_upload(upload_id)
    if not f:
        return "Data tidak ditemukan", 404
    peer_url = _drone_peer_redirect(f)
    if peer_url:
        return redirect(peer_url)
    if f["worker_host"] and f["worker_host"] != THIS_HOSTNAME:
        return f"Batch ini diproses di server '{f['worker_host']}', buka Log Drone dari server itu.", 409

    if f["status"] == "done" and kind == "png" and not (f.get("result_map_path") and os.path.exists(f["result_map_path"])):
        new_path = _build_drone_map(upload_id)
        if new_path:
            f["result_map_path"] = new_path
    elif f["status"] == "done" and kind == "pptx" and not (f.get("result_pptx_path") and os.path.exists(f["result_pptx_path"])):
        new_path = _build_drone_pptx(upload_id)
        if new_path:
            f["result_pptx_path"] = new_path

    path_map = {
        "gpx": ("result_gpx_path", "application/gpx+xml"),
        "xlsx": ("result_xlsx_path", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        "png": ("result_map_path", "image/png"),
        "pptx": ("result_pptx_path", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        "shp": ("result_shp_path", "application/zip"),
    }
    if kind not in path_map:
        return "Jenis file tidak dikenal", 400
    field, mimetype = path_map[kind]
    path = f.get(field)
    if not path or not os.path.exists(path):
        return "File belum tersedia", 404
    download_name = os.path.basename(path)
    data = open(path, "rb").read()
    resp = Response(data, mimetype=mimetype)
    resp.headers["Content-Disposition"] = f'attachment; filename="{download_name}"'
    return resp


@app.route(DASHBOARD_PREFIX + "/drone/log")
@admin_required
def dashboard_drone_log():
    log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "drone_log_worker.log")
    if not os.path.exists(log_file):
        return Response("Log file belum ada — worker belum pernah jalan.", mimetype="text/plain")
    try:
        n_lines = request.args.get("lines", 300, type=int)
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        content = "".join(lines[-n_lines:])
        return Response(content or "(log kosong)", mimetype="text/plain")
    except Exception as e:
        return Response(f"Gagal membaca log: {e}", mimetype="text/plain"), 500


# ── Auto-start Batch Worker (background thread, satu proses dengan dashboard) ──
def _start_batch_worker_thread():
    try:
        import batch_worker
        t = threading.Thread(target=batch_worker.run_worker_loop, daemon=True, name="BatchWorker")
        t.start()
        print("[OK] Batch worker thread aktif (Deteksi TM berjalan otomatis di background)")
    except Exception as e:
        print(f"[WARN] Batch worker TIDAK aktif: {e}")
        print("[WARN] Pastikan api_server.py dijalankan via QGIS Python (python-qgis-ltr.bat)")
        print("[WARN] agar GDAL/torch/sahi tersedia untuk proses batch deteksi TM")


def _start_custom_batch_worker_thread():
    try:
        import custom_batch_worker
        t = threading.Thread(target=custom_batch_worker.run_custom_worker_loop, daemon=True, name="CustomBatchWorker")
        t.start()
        print("[OK] Custom batch worker thread aktif (Deteksi Custom berjalan otomatis di background)")
    except Exception as e:
        print(f"[WARN] Custom batch worker TIDAK aktif: {e}")
        print("[WARN] Pastikan api_server.py dijalankan via QGIS Python (python-qgis-ltr.bat)")
        print("[WARN] agar GDAL/torch/sahi tersedia untuk proses deteksi custom")


def _start_raw_photo_worker_thread():
    try:
        import raw_photo_worker
        t = threading.Thread(target=raw_photo_worker.run_raw_photo_worker_loop, daemon=True, name="RawPhotoWorker")
        t.start()
        print("[OK] Raw photo worker thread aktif (Deteksi RAW Foto berjalan otomatis di background)")
    except Exception as e:
        print(f"[WARN] Raw photo worker TIDAK aktif: {e}")
        print("[WARN] Pastikan api_server.py dijalankan via QGIS Python (python-qgis-ltr.bat)")
        print("[WARN] agar GDAL/torch/sahi tersedia untuk proses deteksi RAW foto")


def _start_drone_log_worker_thread():
    try:
        from Processing.log_drone import drone_log_worker
        t = threading.Thread(target=drone_log_worker.run_drone_log_worker_loop, daemon=True, name="DroneLogWorker")
        t.start()
        print("[OK] Drone log worker thread aktif (Log Drone berjalan otomatis di background)")
    except Exception as e:
        print(f"[WARN] Drone log worker TIDAK aktif: {e}")
        print("[WARN] Pastikan playwright & 'python -m playwright install chromium' sudah dijalankan")


_start_batch_worker_thread()
_start_custom_batch_worker_thread()
_start_raw_photo_worker_thread()
_start_drone_log_worker_thread()


# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    PORT = int(os.environ.get("AGRIPALM_PORT", "8000"))
    print("=" * 60)
    print("  Agripalm Vision API Server + Batch Worker")
    print(f"  Host: 127.0.0.1:{PORT} (lokal saja, akses lewat Nginx reverse proxy)")
    print(f"  Dashboard: http://agripalm.bumitama.local{DASHBOARD_PREFIX}")
    print(f"  DB:   {DB_HOST}/{DB_NAME}")
    print(f"  LDAP: {LDAP_HOST}:{LDAP_PORT}")
    print("=" * 60)
    app.run(host="127.0.0.1", port=PORT, debug=False)
