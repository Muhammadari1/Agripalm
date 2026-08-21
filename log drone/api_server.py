"""
Report Log Drone — Standalone
===============================
Paket berdiri sendiri berisi HANYA fitur "Report Log Drone" yang di-copy dari
dashboard Agripalm Vision utama (menu Drone > Report Log Drone). Semua route,
tabel DB, worker, dan template di file/folder ini adalah SALINAN LANGSUNG dari
kode aslinya -- tidak ada logika fitur yang diubah.

Yang DIBANGUN ULANG (bukan salinan) supaya paket ini bisa jalan sendiri tanpa
seluruh dashboard utama:
    - Sistem login (di dashboard asli tersebar di banyak menu/tabel; di sini
      disederhanakan jadi hanya yang dibutuhkan: tabel `dashboard_admins` +
      tabel `users` eksternal untuk password, sama seperti aslinya).
    - Sidebar (base.html) dipangkas jadi cuma 1 menu (tidak ada Overview/Users/
      Licenses/dst yang memang tidak ikut dibawa).
    - batch_worker.py di sini adalah versi RINGKAS (lihat docstring di file
      itu) -- cuma berisi get_db/WORKER_HOSTNAME/ARESTA_PATH yang dibutuhkan
      drone_report_worker.py & drone_map.py, TANPA mesin Deteksi TM yang
      butuh instalasi Agripalm Vision penuh (torch/sahi/model .pt).

Jalankan lewat QGIS Python (butuh GDAL untuk geopandas):
    "C:\\Program Files\\QGIS 3.40.6\\bin\\python-qgis-ltr.bat" api_server.py
atau pakai JALANKAN_SERVER.bat yang sudah disiapkan.
"""

import os
import threading
import zipfile
from datetime import datetime
from functools import wraps
from werkzeug.utils import secure_filename

from flask import Flask, request, jsonify, render_template, redirect, url_for, Response, session
import psycopg2

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "ReportLogDrone-Standalone-Secret-2026")

# ── Config ────────────────────────────────────────────────────────────────────
DASHBOARD_PREFIX = os.environ.get("AGRIPALM_DASHBOARD_PREFIX", "/dashboard")
DASHBOARD_SEED_ADMIN = os.environ.get("AGRIPALM_SEED_ADMIN", "admin")

DB_HOST = os.environ.get("AGRIPALM_DB_HOST", "localhost")
DB_USER = os.environ.get("AGRIPALM_DB_USER", "postgres")
DB_PASS = os.environ.get("AGRIPALM_DB_PASS", "")
DB_NAME = os.environ.get("AGRIPALM_DB_NAME", "gpnserver")

# Bypass login, HANYA untuk akses loopback (127.0.0.1) di laptop lokal -- pola
# sama seperti dashboard utama. Default OFF, di-set "1" oleh JALANKAN_SERVER.bat
# supaya paket ini langsung bisa dipakai di laptop baru tanpa perlu setup tabel
# `users`/LDAP dulu. Matikan (hapus env var ini) kalau mau login sungguhan.
LOCAL_NO_LOGIN = os.environ.get("AGRIPALM_LOCAL_NO_LOGIN", "0") == "1"

try:
    from ldap3 import Server, Connection, ALL
    LDAP_AVAILABLE = True
except ImportError:
    LDAP_AVAILABLE = False

LDAP_HOST = os.environ.get("AGRIPALM_LDAP_HOST", "")
LDAP_PORT = int(os.environ.get("AGRIPALM_LDAP_PORT", "389"))

# Folder penyimpanan upload & hasil (GPX/Excel/SHP/PNG/PPT) -- identik dengan
# punya dashboard asli (drone_report_data/), independen sepenuhnya.
DRONE_REPORT_ROOT = os.environ.get("AGRIPALM_DRONE_REPORT_ROOT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "drone_report_data"))
DRONE_REPORT_INCOMING = os.path.join(DRONE_REPORT_ROOT, "incoming")
os.makedirs(DRONE_REPORT_INCOMING, exist_ok=True)
ALLOWED_DRONE_EXTENSIONS = {".txt"}

# 2 server berbagi 1 DB tapi tidak berbagi disk -- lihat _drone_report_peer_redirect().
# Kosongkan kalau cuma 1 server (paling umum untuk paket standalone ini).
import socket
THIS_HOSTNAME = socket.gethostname()
AGRIPALM_PEER_URL = os.environ.get("AGRIPALM_PEER_URL", "").rstrip("/")


# ── Database Helper ───────────────────────────────────────────────────────────
def get_db():
    try:
        return psycopg2.connect(host=DB_HOST, user=DB_USER, password=DB_PASS, database=DB_NAME)
    except Exception as e:
        print(f"[DB ERROR] {e}")
        return None


# ── Login & admin (disederhanakan dari dashboard asli -- 1 role saja, tidak ada
# pembagian menu/estate karena paket ini cuma punya 1 menu) ────────────────────
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
        cur.execute("SELECT COUNT(*) FROM dashboard_admins")
        if cur.fetchone()[0] == 0:
            cur.execute(
                "INSERT INTO dashboard_admins (username) VALUES (%s) ON CONFLICT DO NOTHING",
                (DASHBOARD_SEED_ADMIN,))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"[ADMIN TABLE] {e}")


def _is_admin(username):
    conn = get_db()
    if not conn:
        return username == DASHBOARD_SEED_ADMIN
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM dashboard_admins WHERE username = %s", (username,))
        row = cur.fetchone()
        cur.close(); conn.close()
        return row is not None
    except Exception:
        return username == DASHBOARD_SEED_ADMIN


def _try_ldap_login(username, password):
    if not LDAP_AVAILABLE or not LDAP_HOST:
        return False
    try:
        server = Server(LDAP_HOST, port=LDAP_PORT, get_info=ALL)
        ldap_conn = Connection(server, user=username, password=password, auto_bind=False)
        if ldap_conn.bind():
            ldap_conn.unbind()
            return True
    except Exception as e:
        print(f"[LDAP] {e}")
    return False


def _try_db_login(username, password):
    """Cek ke tabel `users` (username, password) -- tabel ini TIDAK dibuat oleh
    paket ini (sama seperti dashboard asli), harus sudah ada di database yang
    dipakai. Kalau tidak ada, gunakan AGRIPALM_LOCAL_NO_LOGIN=1 saja."""
    conn = get_db()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("SELECT password FROM users WHERE username = %s", (username,))
        row = cur.fetchone()
        cur.close(); conn.close()
        return row is not None and row[0] == password
    except Exception:
        return False


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_user"):
            return redirect(url_for("dashboard_login"))
        return f(*args, **kwargs)
    return decorated


def _is_loopback_request():
    return request.remote_addr in ("127.0.0.1", "::1")


@app.before_request
def _dashboard_access_gate():
    path = request.path
    if not path.startswith(DASHBOARD_PREFIX):
        return

    if LOCAL_NO_LOGIN and _is_loopback_request() and not session.get("admin_user"):
        session["admin_user"] = DASHBOARD_SEED_ADMIN

    if path == DASHBOARD_PREFIX + "/login" and session.get("admin_user"):
        return redirect(url_for("dashboard_drone_report"))
    if path in (DASHBOARD_PREFIX + "/login", DASHBOARD_PREFIX + "/logout"):
        return
    # Tidak ada gate menu/estate lagi di sini -- paket ini cuma 1 menu, jadi
    # login = akses penuh (beda dari dashboard asli yang punya role admin/user).


@app.route(DASHBOARD_PREFIX + "/login", methods=["GET", "POST"])
def dashboard_login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not _is_admin(username):
            error = "Username tidak terdaftar."
        elif not password:
            error = "Password wajib diisi."
        elif _try_ldap_login(username, password) or _try_db_login(username, password):
            session["admin_user"] = username
            return redirect(url_for("dashboard_drone_report"))
        else:
            error = "Login gagal. Password tidak cocok."
    return render_template("login.html", error=error)


@app.route(DASHBOARD_PREFIX + "/logout")
def dashboard_logout():
    session.pop("admin_user", None)
    return redirect(url_for("dashboard_login"))


@app.route(DASHBOARD_PREFIX)
@admin_required
def dashboard_root():
    return redirect(url_for("dashboard_drone_report"))


# ══════════════════════════════════════════════════════════════════════════════
# BAGIAN DI BAWAH INI ADALAH SALINAN LANGSUNG dari api_server.py dashboard utama
# (menu Drone > Report Log Drone) -- tidak ada logika yang diubah.
# ══════════════════════════════════════════════════════════════════════════════

def _ensure_drone_report_tables():
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
            "ALTER TABLE drone_report_uploads ADD COLUMN IF NOT EXISTS lokasi_kerja TEXT",
            "ALTER TABLE drone_report_uploads ADD COLUMN IF NOT EXISTS bulan VARCHAR(7)",
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
        print(f"[DRONE REPORT TABLES] {e}")


def _drone_report_upload_rows():
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
            print(f"[DRONE REPORT LIST ERROR] {e}")
    return rows


@app.route(DASHBOARD_PREFIX + "/drone-report")
@admin_required
def dashboard_drone_report():
    from Processing.log_drone.drone_usage_history import bulan_options
    return render_template(
        "drone_report_log.html", active="drone_report",
        uploads=_drone_report_upload_rows(), bulan_options=bulan_options(),
        current_bulan=datetime.now().strftime("%Y-%m"),
    )


@app.route(DASHBOARD_PREFIX + "/drone-report/rekap")
@admin_required
def dashboard_drone_report_rekap():
    from Processing.log_drone.drone_usage_history import get_recap_data, bulan_label
    bulan_list, entities = get_recap_data()
    return render_template(
        "drone_report_rekap.html", active="drone_report",
        bulan_list=bulan_list, bulan_label=bulan_label, entities=entities,
    )


@app.route(DASHBOARD_PREFIX + "/drone-report/upload", methods=["POST"])
@admin_required
def dashboard_drone_report_upload():
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
        lokasi_kerja = request.form.get("lokasi_kerja", "").strip() or None
        bulan = request.form.get("bulan", "").strip() or None
        cur.execute(
            "INSERT INTO drone_report_uploads (created_by, batch_name, files_total, pilot_name, drone_code, lokasi_kerja, bulan) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (session.get("admin_user"), batch_name, len(files), pilot_name, drone_code, lokasi_kerja, bulan),
        )
        upload_id = cur.fetchone()[0]

        ts_ms = int(datetime.now().timestamp() * 1000)
        for i, f in enumerate(files):
            filename = secure_filename(f.filename)
            save_path = os.path.join(DRONE_REPORT_INCOMING, f"{ts_ms}_{i}_{filename}")
            f.save(save_path)
            cur.execute(
                "INSERT INTO drone_report_files (upload_id, original_filename, input_path, status) "
                "VALUES (%s, %s, %s, 'pending')",
                (upload_id, f.filename, save_path),
            )
        conn.commit(); cur.close(); conn.close()
        return jsonify({"success": True, "upload_id": upload_id, "files": len(files)})
    except Exception as e:
        print(f"[DRONE REPORT UPLOAD ERROR] {e}")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route(DASHBOARD_PREFIX + "/drone-report/status")
@admin_required
def dashboard_drone_report_status():
    uploads = []
    for row in _drone_report_upload_rows():
        uploads.append({
            "id": row[0], "batch_name": row[1], "status": row[2], "error_message": row[3],
            "drone_sn": row[4], "duration_s": row[5], "distance_m": row[6],
            "created_at": row[7].isoformat() if row[7] else None,
            "worker_host": row[8], "files_total": row[9],
        })
    return jsonify({"uploads": uploads})


@app.route(DASHBOARD_PREFIX + "/drone-report/delete/<int:upload_id>", methods=["POST"])
@admin_required
def dashboard_drone_report_delete(upload_id):
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
            print(f"[DRONE REPORT DELETE ERROR] {e}")
    return redirect(url_for("dashboard_drone_report"))


def _get_drone_report_upload(upload_id):
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
        print(f"[DRONE REPORT GET ERROR] {e}")
        return None
    return {
        "id": row[0], "batch_name": row[1], "status": row[2], "error_message": row[3],
        "drone_sn": row[4], "duration_s": row[5], "distance_m": row[6],
        "result_gpx_path": row[7], "result_xlsx_path": row[8], "result_map_path": row[9],
        "finished_at": row[10], "worker_host": row[11], "files_total": row[12],
        "result_pptx_path": row[13], "created_by": row[14], "result_shp_path": row[15],
        "pilot_name": row[16], "drone_code": row[17], "filenames": filenames,
    }


def _drone_report_peer_redirect(u):
    worker_host = (u or {}).get("worker_host")
    if not worker_host or worker_host == THIS_HOSTNAME:
        return None
    if not AGRIPALM_PEER_URL:
        return None
    return AGRIPALM_PEER_URL + request.full_path


@app.route(DASHBOARD_PREFIX + "/drone-report/<int:upload_id>")
@admin_required
def dashboard_drone_report_detail(upload_id):
    u = _get_drone_report_upload(upload_id)
    if not u:
        return redirect(url_for("dashboard_drone_report"))
    peer_url = _drone_report_peer_redirect(u)
    if peer_url:
        return redirect(peer_url)
    if u["worker_host"] and u["worker_host"] != THIS_HOSTNAME:
        return render_template(
            "drone_report_log_detail.html", active="drone_report", file=u,
            wrong_server_host=u["worker_host"],
        )
    return render_template("drone_report_log_detail.html", active="drone_report", file=u)


@app.route(DASHBOARD_PREFIX + "/drone-report/<int:upload_id>/geojson")
@admin_required
def dashboard_drone_report_geojson(upload_id):
    u = _get_drone_report_upload(upload_id)
    if not u:
        return jsonify({"error": "Data belum tersedia"}), 404
    peer_url = _drone_report_peer_redirect(u)
    if peer_url:
        return redirect(peer_url)
    if u["worker_host"] and u["worker_host"] != THIS_HOSTNAME:
        return jsonify({"error": f"Batch ini diproses di server '{u['worker_host']}', buka Report Log Drone dari server itu."}), 409
    if u["status"] != "done":
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
        print(f"[DRONE REPORT GEOJSON ERROR] {e}")
        return jsonify({"error": str(e)}), 500


def _load_drone_report_rows(upload_id):
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
        print(f"[DRONE REPORT LOAD ROWS ERROR] {e}")
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


def _build_drone_report_map(upload_id):
    all_rows, file_metas = _load_drone_report_rows(upload_id)
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
            print(f"[DRONE REPORT MAP BUILD ERROR] {e}")

    if not out_dir:
        return None

    from Processing.log_drone.drone_map import compute_region_groups, render_flight_map_png_multi

    zip_path = os.path.join(out_dir, f"batch_{upload_id}_peta_per_region.zip")
    region_groups = compute_region_groups(all_rows, file_metas, work_dir=out_dir)
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for region_idx, (region, (region_rows, region_metas)) in enumerate(sorted(region_groups.items())):
                if not region_rows:
                    continue
                png_path = os.path.join(out_dir, f"batch_{upload_id}_region_{region_idx + 1}_peta.png")
                render_flight_map_png_multi(
                    region_rows, png_path, work_dir=out_dir, file_metas=region_metas,
                    uploader_name=created_by, region_name=region,
                )
                archive.write(png_path, arcname=os.path.basename(png_path))
    except Exception as e:
        print(f"[DRONE REPORT MAP RENDER ERROR] {e}")
        return None

    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("UPDATE drone_report_uploads SET result_map_path = %s WHERE id = %s", (zip_path, upload_id))
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[DRONE REPORT MAP SAVE ERROR] {e}")
    return zip_path


def _build_drone_report_pptx(upload_id):
    u = _get_drone_report_upload(upload_id)
    if not u:
        return None
    map_path = u.get("result_map_path")
    if not map_path or not os.path.exists(map_path):
        map_path = _build_drone_report_map(upload_id)
    if not map_path:
        return None

    all_rows, file_metas = _load_drone_report_rows(upload_id)

    from Processing.log_drone.drone_export import export_pptx_report
    from Processing.log_drone.drone_map import (
        compute_cluster_recaps, compute_region_groups, render_flight_map_png_compact,
        compute_estate_names, compute_flight_period, compute_flight_days_count,
    )

    out_dir = os.path.dirname(map_path)

    pptx_path = os.path.join(out_dir, f"batch_{upload_id}_report_per_region.pptx")
    try:
        from pptx import Presentation
        prs = Presentation()
        prs.slide_width = 12192000
        prs.slide_height = 6858000
        region_groups = compute_region_groups(all_rows, file_metas, work_dir=out_dir)
        for region, (region_rows, region_metas) in sorted(region_groups.items()):
            compact_map_path = os.path.join(
                out_dir, f"batch_{upload_id}_region_{secure_filename(region) or 'tanpa_region'}_compact.png"
            )
            try:
                render_flight_map_png_compact(
                    region_rows, compact_map_path, work_dir=out_dir, file_metas=region_metas,
                )
            except Exception as e:
                print(f"[DRONE REPORT PPTX MAP ERROR] region={region}: {e}")
                compact_map_path = None

            region_duration = sum((meta.get("durationSecs") or 0) for meta, _ in region_metas.values())
            region_distance = sum((meta.get("totalDistance") or 0) for meta, _ in region_metas.values())
            region_sns = [meta["droneSerial"] for meta, _ in region_metas.values() if meta.get("droneSerial")]
            cluster_rows = compute_cluster_recaps(region_rows, region_metas, u.get("pilot_name"))
            estate_names = compute_estate_names(region_rows, work_dir=out_dir)
            flight_start, flight_end = compute_flight_period(region_metas)
            flight_days_count = compute_flight_days_count(region_metas)
            export_pptx_report(
                pptx_path, u["batch_name"], u.get("pilot_name"), datetime.now(), upload_id,
                region_duration, region_distance, ", ".join(dict.fromkeys(region_sns)), u.get("drone_code"),
                len(region_metas), flight_days_count, flight_start, flight_end,
                estate_names, compact_map_path, cluster_rows, prs=prs, region_name=region,
            )
        prs.save(pptx_path)
    except Exception as e:
        print(f"[DRONE REPORT PPTX RENDER ERROR] {e}")
        return None

    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("UPDATE drone_report_uploads SET result_pptx_path = %s WHERE id = %s", (pptx_path, upload_id))
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"[DRONE REPORT PPTX SAVE ERROR] {e}")
    return pptx_path


@app.route(DASHBOARD_PREFIX + "/drone-report/<int:upload_id>/download/<kind>")
@admin_required
def dashboard_drone_report_download(upload_id, kind):
    u = _get_drone_report_upload(upload_id)
    if not u:
        return "Data tidak ditemukan", 404
    peer_url = _drone_report_peer_redirect(u)
    if peer_url:
        return redirect(peer_url)
    if u["worker_host"] and u["worker_host"] != THIS_HOSTNAME:
        return f"Batch ini diproses di server '{u['worker_host']}', buka Report Log Drone dari server itu.", 409

    if u["status"] == "done" and kind == "png" and not (
        u.get("result_map_path") and u["result_map_path"].endswith("_per_region.zip")
        and os.path.exists(u["result_map_path"])
    ):
        new_path = _build_drone_report_map(upload_id)
        if new_path:
            u["result_map_path"] = new_path
    elif u["status"] == "done" and kind == "pptx" and not (
        u.get("result_pptx_path") and "_report_per_region.pptx" in os.path.basename(u["result_pptx_path"])
        and os.path.exists(u["result_pptx_path"])
    ):
        new_path = _build_drone_report_pptx(upload_id)
        if new_path:
            u["result_pptx_path"] = new_path

    path_map = {
        "gpx": ("result_gpx_path", "application/gpx+xml"),
        "xlsx": ("result_xlsx_path", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        "png": ("result_map_path", "application/zip"),
        "pptx": ("result_pptx_path", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        "shp": ("result_shp_path", "application/zip"),
    }
    if kind not in path_map:
        return "Jenis file tidak dikenal", 400
    field, mimetype = path_map[kind]
    path = u.get(field)
    if not path or not os.path.exists(path):
        return "File belum tersedia", 404
    download_name = os.path.basename(path)
    data = open(path, "rb").read()
    resp = Response(data, mimetype=mimetype)
    resp.headers["Content-Disposition"] = f'attachment; filename="{download_name}"'
    return resp


@app.route(DASHBOARD_PREFIX + "/drone-report/log")
@admin_required
def dashboard_drone_report_log():
    log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "drone_report_worker.log")
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


# ── Auto-start Report Log Drone Worker (background thread, satu proses dengan
# dashboard, sama seperti aslinya) ─────────────────────────────────────────────
def _start_drone_report_worker_thread():
    try:
        from Processing.log_drone import drone_report_worker
        t = threading.Thread(target=drone_report_worker.run_drone_report_worker_loop, daemon=True, name="DroneReportWorker")
        t.start()
        print("[OK] Drone report worker thread aktif (berjalan otomatis di background)")
    except Exception as e:
        print(f"[WARN] Drone report worker TIDAK aktif: {e}")
        print("[WARN] Pastikan playwright & 'python -m playwright install chromium' sudah dijalankan")


_ensure_admin_table()
_ensure_drone_report_tables()

from Processing.log_drone.drone_usage_history import init_db as _init_drone_history_db, seed_if_empty as _seed_drone_history
_init_drone_history_db()
_seed_drone_history()

_start_drone_report_worker_thread()


# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    PORT = int(os.environ.get("AGRIPALM_PORT", "8001"))
    print("=" * 60)
    print("  Report Log Drone — Standalone")
    print(f"  http://127.0.0.1:{PORT}{DASHBOARD_PREFIX}")
    print(f"  DB: {DB_HOST}/{DB_NAME}")
    print("=" * 60)
    app.run(host="0.0.0.0", port=PORT, debug=False)
