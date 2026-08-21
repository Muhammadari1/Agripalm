"""
Report Log Drone Worker — proses flight log DJI (.txt) lewat app.opendronelog.com
=============================================================================
Duplikat independen dari drone_log_worker.py, TAPI beda pola kerja: kalau 1 upload
berisi banyak file .txt, semuanya diproses sebagai SATU rekap gabungan (1 GPX/Excel
gabungan, durasi & jarak dijumlah) -- bukan hasil terpisah per file seperti Log Drone
yang asli. Tiap file tetap harus diupload & diparsing satu-satu ke app.opendronelog.com
(keterbatasan situs pihak ketiga, tidak bisa digabung di sana), tapi unit KLAIM &
SELESAI-nya sekarang di level upload (drone_report_uploads), bukan di level file
(drone_report_files -- tabel itu tetap dipakai untuk tracking status parsing tiap
file, hasilnya nanti dijumlah/digabung).

Peta (PNG) dan Report (PPT) SENGAJA TIDAK dibuat di sini -- worker cuma bikin GPX +
Excel supaya batch cepat selesai. Peta/PPT baru dibangun LAZY (saat pertama kali
di-download user) oleh _build_drone_report_map()/_build_drone_report_pptx() di
api_server.py, memakai JSON hasil parsing tiap file yang sudah disimpan
(result_json_path) -- tidak perlu upload ulang ke opendronelog.com.

Bisa dijalankan standalone:
    "C:\\Program Files\\QGIS 3.40.6\\bin\\python-qgis-ltr.bat" Processing\\log_drone\\drone_report_worker.py
"""
import os
import sys
import time
import logging
import traceback
from datetime import datetime
from logging.handlers import RotatingFileHandler

# Modul ini dipakai dua cara: diimport sebagai Processing.log_drone.drone_report_worker
# (oleh api_server.py) ATAU dijalankan langsung sebagai skrip standalone. Supaya import
# bare (from opendronelog_client import ...) jalan di dua-duanya, tambahkan folder ini
# sendiri DAN root dashboard (utk import batch_worker) ke sys.path secara eksplisit.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_DASHBOARD_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
for _p in (_THIS_DIR, _DASHBOARD_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from batch_worker import get_db, WORKER_HOSTNAME  # noqa: E402
from opendronelog_client import process_flight_log, DroneLogProcessError  # noqa: E402
from drone_export import (  # noqa: E402
    load_rows, export_gpx_multi, export_excel_monitoring, export_shp_multi, DroneExportParseError,
)
from drone_map import compute_flight_blocks, compute_flight_days_count  # noqa: E402
from drone_usage_history import record_usage  # noqa: E402

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "drone_report_worker.log")

logger = logging.getLogger("drone_report_worker")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    _file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    _file_handler.setFormatter(_formatter)
    logger.addHandler(_file_handler)
    _console_handler = logging.StreamHandler(sys.stdout)
    _console_handler.setFormatter(_formatter)
    logger.addHandler(_console_handler)

POLL_INTERVAL_SECONDS = 5

DRONE_REPORT_ROOT = os.environ.get(
    "AGRIPALM_DRONE_REPORT_ROOT",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "drone_report_data"),
)
RESULT_DIR = os.path.join(DRONE_REPORT_ROOT, "results")
os.makedirs(RESULT_DIR, exist_ok=True)


def update_upload(upload_id, **fields):
    if not fields:
        return
    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        set_clause = ", ".join(f"{k} = %s" for k in fields)
        values = list(fields.values()) + [upload_id]
        cur.execute(f"UPDATE drone_report_uploads SET {set_clause} WHERE id = %s", values)
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        logger.error(f"[UPDATE UPLOAD ERROR] {e}")


def update_file(file_id, **fields):
    if not fields:
        return
    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        set_clause = ", ".join(f"{k} = %s" for k in fields)
        values = list(fields.values()) + [file_id]
        cur.execute(f"UPDATE drone_report_files SET {set_clause} WHERE id = %s", values)
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        logger.error(f"[UPDATE FILE ERROR] {e}")


def _bump_upload_counters(upload_id, done_delta=0, failed_delta=0):
    if not upload_id:
        return
    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE drone_report_uploads SET files_done = files_done + %s, "
            "files_failed = files_failed + %s WHERE id = %s",
            (done_delta, failed_delta, upload_id),
        )
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        logger.error(f"[UPLOAD COUNTER ERROR] {e}")


def get_next_pending_upload():
    conn = get_db()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE drone_report_uploads SET status = 'processing' "
            "WHERE id = ("
            "  SELECT id FROM drone_report_uploads WHERE status = 'pending' "
            "  ORDER BY id ASC LIMIT 1 FOR UPDATE SKIP LOCKED"
            ") "
            "RETURNING id, batch_name, created_by, pilot_name, drone_code, lokasi_kerja, bulan"
        )
        row = cur.fetchone()
        conn.commit()
        cur.close(); conn.close()
        if row:
            return {"id": row[0], "batch_name": row[1], "created_by": row[2],
                    "pilot_name": row[3], "drone_code": row[4],
                    "lokasi_kerja": row[5], "bulan": row[6]}
    except Exception as e:
        logger.error(f"[GET UPLOAD ERROR] {e}")
    return None


def get_orphaned_processing_uploads():
    """Sama seperti drone_log_worker/raw_photo_worker: hanya reclaim batch milik
    worker_host sendiri, supaya tidak "mencuri" batch yang masih aktif diproses
    server lain."""
    conn = get_db()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, batch_name, created_by, pilot_name, drone_code, lokasi_kerja, bulan FROM drone_report_uploads "
            "WHERE status = 'processing' AND worker_host = %s",
            (WORKER_HOSTNAME,),
        )
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{"id": r[0], "batch_name": r[1], "created_by": r[2],
                  "pilot_name": r[3], "drone_code": r[4],
                  "lokasi_kerja": r[5], "bulan": r[6]} for r in rows]
    except Exception as e:
        logger.error(f"[ORPHAN CHECK ERROR] {e}")
        return []


def get_upload_files(upload_id):
    conn = get_db()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, original_filename, input_path FROM drone_report_files "
            "WHERE upload_id = %s ORDER BY id ASC",
            (upload_id,),
        )
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{"id": r[0], "original_filename": r[1], "input_path": r[2]} for r in rows]
    except Exception as e:
        logger.error(f"[GET UPLOAD FILES ERROR] {e}")
        return []


def process_drone_report_upload(upload_job):
    upload_id = upload_job["id"]
    batch_name = upload_job["batch_name"] or f"batch_{upload_id}"
    logger.info(f"[UPLOAD {upload_id}] Mulai — {batch_name} worker={WORKER_HOSTNAME}")
    update_upload(upload_id, status="processing", started_at=datetime.now(), worker_host=WORKER_HOSTNAME)

    files = get_upload_files(upload_id)
    out_dir = os.path.join(RESULT_DIR, str(upload_id))
    os.makedirs(out_dir, exist_ok=True)

    all_rows = []
    file_metas = {}
    total_duration = 0.0
    total_distance = 0.0
    have_duration = False
    have_distance = False
    drone_sns = []
    failed_names = []

    # Tiap file tetap diproses SATU-SATU ke opendronelog.com (situsnya tidak bisa
    # terima banyak file sekaligus) -- yang beda dari Log Drone: hasilnya dikumpulkan
    # dulu di all_rows/file_metas, digabung jadi 1 rekap SETELAH loop ini selesai.
    for track_idx, job in enumerate(files):
        file_id = job["id"]
        filename = job["original_filename"]
        logger.info(f"[UPLOAD {upload_id}] [FILE {file_id}] Mulai — {filename}")
        update_file(file_id, status="processing", started_at=datetime.now())
        base = os.path.splitext(filename)[0]
        try:
            if not os.path.isfile(job["input_path"]):
                raise DroneLogProcessError(f"File tidak ditemukan: {job['input_path']}")

            json_path = os.path.join(out_dir, f"{track_idx}_{base}_raw.json")
            logger.info(f"[UPLOAD {upload_id}] [FILE {file_id}] Upload & proses lewat app.opendronelog.com...")
            process_flight_log(job["input_path"], json_path)

            rows, flight_meta = load_rows(json_path)
            logger.info(f"[UPLOAD {upload_id}] [FILE {file_id}] {len(rows)} titik koordinat berhasil diparsing.")

            duration_s = flight_meta.get("durationSecs")
            distance_m = flight_meta.get("totalDistance")
            sn = flight_meta.get("droneSerial")

            update_file(
                file_id, status="done", finished_at=datetime.now(),
                result_json_path=json_path, drone_sn=sn,
                duration_s=duration_s, distance_m=distance_m,
            )
            _bump_upload_counters(upload_id, done_delta=1)

            for r in rows:
                all_rows.append({**r, "track_idx": track_idx, "source_file": filename})
            file_metas[track_idx] = (flight_meta, filename)
            if duration_s is not None:
                total_duration += duration_s
                have_duration = True
            if distance_m is not None:
                total_distance += distance_m
                have_distance = True
            if sn:
                drone_sns.append(sn)

        except (DroneLogProcessError, DroneExportParseError) as e:
            logger.error(f"[UPLOAD {upload_id}] [FILE {file_id}] GAGAL: {e}")
            update_file(file_id, status="failed", finished_at=datetime.now(), error_message=str(e))
            _bump_upload_counters(upload_id, failed_delta=1)
            failed_names.append(filename)
        except Exception as e:
            error_detail = f"{e}\n{traceback.format_exc()}"
            logger.error(f"[UPLOAD {upload_id}] [FILE {file_id}] GAGAL (tak terduga): {error_detail}")
            update_file(file_id, status="failed", finished_at=datetime.now(),
                         error_message=f"Error tak terduga: {e}")
            _bump_upload_counters(upload_id, failed_delta=1)
            failed_names.append(filename)

    if not all_rows:
        logger.error(f"[UPLOAD {upload_id}] GAGAL — semua file gagal diproses.")
        update_upload(
            upload_id, status="failed", finished_at=datetime.now(),
            error_message="Semua file gagal diproses: " + ", ".join(failed_names),
        )
        return

    fields = {
        "status": "done",
        "finished_at": datetime.now(),
        "duration_s": total_duration if have_duration else None,
        "distance_m": total_distance if have_distance else None,
        "drone_sn": ", ".join(dict.fromkeys(drone_sns)) if drone_sns else None,
    }

    notes = []
    if failed_names:
        notes.append(f"{len(failed_names)} file gagal diproses: {', '.join(failed_names)}")

    try:
        gpx_path = os.path.join(out_dir, f"batch_{upload_id}.gpx")
        export_gpx_multi(all_rows, file_metas, gpx_path)
        fields["result_gpx_path"] = gpx_path

        xlsx_path = os.path.join(out_dir, f"batch_{upload_id}_telemetry.xlsx")
        block_info = compute_flight_blocks(all_rows, file_metas, work_dir=out_dir)
        export_excel_monitoring(
            file_metas, upload_job.get("pilot_name"), upload_job.get("drone_code"),
            fields.get("drone_sn"), block_info, xlsx_path,
        )
        fields["result_xlsx_path"] = xlsx_path
    except Exception as e:
        logger.error(f"[UPLOAD {upload_id}] GAGAL membuat GPX/Excel gabungan: {e}")
        notes.append(f"Gagal membuat GPX/Excel: {e}")

    # SHP dicoba TERPISAH -- backend-nya beda (Fiona/GDAL lewat geopandas), kalau
    # gagal jangan sampai bikin GPX/Excel yang sudah berhasil ikut hilang.
    try:
        shp_path = os.path.join(out_dir, f"batch_{upload_id}_shp.zip")
        export_shp_multi(all_rows, file_metas, shp_path)
        fields["result_shp_path"] = shp_path
    except Exception as e:
        logger.error(f"[UPLOAD {upload_id}] GAGAL membuat SHP: {e}")
        notes.append(f"Gagal membuat SHP: {e}")

    # Peta (PNG) & Report (PPT) SENGAJA tidak dibuat di sini -- baru dibangun lazy
    # saat user pertama kali klik download Peta (PNG) / Report (lihat
    # _build_drone_report_map()/_build_drone_report_pptx() di api_server.py). Upload
    # & proses cuma menghasilkan GPX + Excel supaya batch cepat selesai; JSON hasil
    # parsing tiap file (result_json_path, sudah disimpan di atas) cukup buat
    # membangun peta/PPT belakangan tanpa perlu upload ulang ke opendronelog.com.

    if notes:
        fields["error_message"] = "; ".join(notes)

    update_upload(upload_id, **fields)

    if upload_job.get("bulan"):
        try:
            record_usage(
                nama=upload_job.get("pilot_name"),
                lokasi_kerja=upload_job.get("lokasi_kerja"),
                kode_drone=upload_job.get("drone_code"),
                bulan=upload_job.get("bulan"),
                hari=compute_flight_days_count(file_metas),
                flight=len(files) - len(failed_names),
                durasi_menit=(total_duration / 60.0) if have_duration else 0,
                upload_id=upload_id,
            )
        except Exception as e:
            logger.error(f"[UPLOAD {upload_id}] Gagal simpan riwayat pemakaian: {e}")

    logger.info(f"[UPLOAD {upload_id}] SELESAI.")


def run_drone_report_worker_loop():
    logger.info("=" * 60)
    logger.info("Agripalm Report Log Drone Worker — proses via app.opendronelog.com")
    logger.info(f"Result dir : {RESULT_DIR}")
    logger.info(f"Log file   : {LOG_FILE}")
    logger.info("=" * 60)

    orphaned = get_orphaned_processing_uploads()
    if orphaned:
        logger.info(f"[RESUME] Ditemukan {len(orphaned)} batch yang terputus, mengulang...")
        for job in orphaned:
            process_drone_report_upload(job)

    while True:
        try:
            job = get_next_pending_upload()
            if job:
                process_drone_report_upload(job)
            else:
                time.sleep(POLL_INTERVAL_SECONDS)
        except Exception as e:
            logger.error(f"[LOOP ERROR] {e}\n{traceback.format_exc()}")
            time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_drone_report_worker_loop()
