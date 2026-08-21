"""
batch_worker.py (versi ringkas untuk paket standalone "Report Log Drone")
==========================================================================
File asli `batch_worker.py` di dashboard Agripalm Vision berisi seluruh mesin
Deteksi TM (splitter, inferensi model .pt lewat torch/sahi, rekap kesehatan) --
ribuan baris, dan meng-import modul proprietary dari folder instalasi
"C:\\Program Files\\Agripalm Vision\\geoprocessing\\..." yang TIDAK ikut
dibawa ke paket ini (di luar cakupan fitur "Report Log Drone").

drone_report_worker.py dan drone_map.py (di Processing/log_drone/) cuma
butuh 4 hal dari batch_worker.py: koneksi database (get_db), nama host worker
(WORKER_HOSTNAME), lokasi file Aresta.shp (ARESTA_PATH), dan fungsi perbaikan
geometri Aresta yang tidak valid (_repair_aresta_geometries). File ini HANYA
berisi 4 hal itu -- supaya paket standalone ini tidak butuh instalasi
Agripalm Vision penuh (torch/sahi/model .pt) untuk sekadar menjalankan fitur
Report Log Drone.
"""
import os
import socket
import logging
from logging.handlers import RotatingFileHandler

import psycopg2
import geopandas as gpd

# ── Config ────────────────────────────────────────────────────────────────────
WORKER_HOSTNAME = socket.gethostname()

DB_HOST = os.environ.get("AGRIPALM_DB_HOST", "localhost")
DB_USER = os.environ.get("AGRIPALM_DB_USER", "postgres")
DB_PASS = os.environ.get("AGRIPALM_DB_PASS", "")
DB_NAME = os.environ.get("AGRIPALM_DB_NAME", "gpnserver")

# Lokasi Aresta.shp -- dipakai drone_map.py untuk spatial join Estate/Blok
# (label peta, kolom ESTATE/BLOK di Excel Monitoring). WAJIB diisi lewat env
# var AGRIPALM_ARESTA_PATH kalau lokasinya beda dari default di bawah.
ARESTA_PATH = os.environ.get("AGRIPALM_ARESTA_PATH", r"C:\Program Files\Agripalm Vision\geoprocessing\nutripalm\Sgpar\Aresta.shp")

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "batch_worker.log")

logger = logging.getLogger("batch_worker")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    _file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    _file_handler.setFormatter(_formatter)
    logger.addHandler(_file_handler)
    _console_handler = logging.StreamHandler()
    _console_handler.setFormatter(_formatter)
    logger.addHandler(_console_handler)


def get_db():
    try:
        return psycopg2.connect(host=DB_HOST, user=DB_USER, password=DB_PASS, database=DB_NAME)
    except Exception as e:
        logger.error(f"[DB ERROR] {e}")
        return None


def _repair_aresta_geometries(aresta_path, work_dir):
    """Sama persis dengan versi di batch_worker.py asli: baca Aresta.shp, kalau ada
    geometri tidak valid (self-intersection dsb), perbaiki pakai buffer(0) dan
    simpan salinan perbaikan -- dipakai drone_map.py sebelum spatial join supaya
    titik yang sebenarnya di dalam blok tidak diam-diam gagal match / ke-skip."""
    if not aresta_path or not os.path.isfile(aresta_path):
        return aresta_path
    try:
        gdf = gpd.read_file(aresta_path)
        invalid_mask = ~gdf.geometry.is_valid
        n_invalid = int(invalid_mask.sum())
        if n_invalid == 0:
            return aresta_path

        gdf.loc[invalid_mask, "geometry"] = gdf.loc[invalid_mask, "geometry"].buffer(0)
        still_invalid = int((~gdf.geometry.is_valid).sum())
        logger.warning(f"[ARESTA REPAIR] {n_invalid} blok Aresta geometrinya tidak valid — "
                       f"{n_invalid - still_invalid} berhasil diperbaiki otomatis"
                       + (f", {still_invalid} masih gagal" if still_invalid else ""))

        repaired_path = os.path.join(work_dir, "_aresta_repaired.shp")
        gdf.to_file(repaired_path)
        return repaired_path
    except Exception as e:
        logger.warning(f"[ARESTA REPAIR] Gagal cek/perbaiki Aresta, pakai file asli: {e}")
        return aresta_path
