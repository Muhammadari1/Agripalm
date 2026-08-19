import sqlite3
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = PROJECT_ROOT / "db" / "kesehatan_tanaman.sqlite"

STATUS_ORDER = ["well canopy", "normal canopy", "abnormal canopy"]

STATUS_COLORS = {
    "well canopy": "#2e7d32",
    "normal canopy": "#f9a825",
    "abnormal canopy": "#e53935",
}

STATUS_LABELS = {
    "well canopy": "Well",
    "normal canopy": "Normal",
    "abnormal canopy": "Abnormal",
}

# Semakin tinggi batas ini, semakin banyak titik yang muncul di peta.
# Nilai 4000 terlalu kecil untuk dataset GeoJSON yang mencakup banyak blok/pohon.
MAX_MAP_POINTS = 200_000


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def parse_bloks_param(bloks: str | None) -> list | None:
    if not bloks:
        return None
    return [b for b in bloks.split(",") if b]


@lru_cache(maxsize=1)
def get_blok_list() -> tuple:
    with get_connection() as conn:
        rows = conn.execute("SELECT id_blok FROM blok_master ORDER BY id_blok").fetchall()
    return tuple(r["id_blok"] for r in rows)


@lru_cache(maxsize=1)
def get_area_hierarchy() -> tuple:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT id_blok, nama_blok, region, estate, divisi
               FROM blok_master ORDER BY region, estate, id_blok"""
        ).fetchall()
    return tuple(dict(r) for r in rows)


def _distinct_periods(sumber_data: str) -> list:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT periode_label, MIN(survey_date) AS d
               FROM canopy_survey WHERE sumber_data = ?
               GROUP BY periode_label ORDER BY d""",
            (sumber_data,),
        ).fetchall()
    return [r["periode_label"] for r in rows]


@lru_cache(maxsize=1)
def get_geo_periods() -> tuple:
    return tuple(_distinct_periods("drone_ai_detection"))


@lru_cache(maxsize=1)
def get_excel_periods() -> tuple:
    return tuple(_distinct_periods("excel_manual_agregat"))


def get_canopy(periode: str, bloks: list | None = None) -> pd.DataFrame:
    query = """
        SELECT id_pohon, id_blok, lon, lat, diameter_m AS "DIAMETER", status AS "CANOPY", kelas AS "KELAS"
        FROM canopy_survey
        WHERE sumber_data = 'drone_ai_detection' AND periode_label = ? AND lon IS NOT NULL
    """
    params = [periode]
    if bloks:
        query += f" AND id_blok IN ({','.join('?' * len(bloks))})"
        params.extend(bloks)
    with get_connection() as conn:
        return pd.read_sql_query(query, conn, params=params)


def get_growth(bloks: list | None = None) -> pd.DataFrame:
    query = """
        SELECT periode_label AS periode, survey_date, AVG(diameter_m) AS avg_diameter
        FROM canopy_survey
        WHERE diameter_m IS NOT NULL
    """
    params = []
    if bloks:
        query += f" AND id_blok IN ({','.join('?' * len(bloks))})"
        params.extend(bloks)
    query += " GROUP BY periode_label ORDER BY survey_date"
    with get_connection() as conn:
        return pd.read_sql_query(query, conn, params=params)


def get_trend(bloks: list | None = None) -> pd.DataFrame:
    query = """
        SELECT periode_label AS periode, survey_date, status, AVG(diameter_m) AS avg_diameter
        FROM canopy_survey
        WHERE diameter_m IS NOT NULL AND status IS NOT NULL
    """
    params = []
    if bloks:
        query += f" AND id_blok IN ({','.join('?' * len(bloks))})"
        params.extend(bloks)
    query += " GROUP BY periode_label, status ORDER BY survey_date"
    with get_connection() as conn:
        df = pd.read_sql_query(query, conn, params=params)
    df["status"] = pd.Categorical(df["status"], categories=STATUS_ORDER, ordered=True)
    return df.sort_values(["survey_date", "status"])


def get_recap(bloks: list | None = None) -> pd.DataFrame:
    query = """
        SELECT periode_label AS periode, survey_date, status, COUNT(*) AS jumlah
        FROM canopy_survey
        WHERE status IS NOT NULL
    """
    params = []
    if bloks:
        query += f" AND id_blok IN ({','.join('?' * len(bloks))})"
        params.extend(bloks)
    query += " GROUP BY periode_label, status ORDER BY survey_date"
    with get_connection() as conn:
        df = pd.read_sql_query(query, conn, params=params)
    df["persen"] = df.groupby("periode")["jumlah"].transform(lambda s: s / s.sum() * 100)
    df["status"] = pd.Categorical(df["status"], categories=STATUS_ORDER, ordered=True)
    return df.sort_values(["survey_date", "status"])


def get_area(periode: str, bloks: list | None = None) -> pd.DataFrame:
    query = """
        SELECT status, COUNT(*) AS jumlah, SUM(3.141592653589793 * radius_m * radius_m) AS area_m2
        FROM canopy_survey
        WHERE sumber_data = 'drone_ai_detection' AND periode_label = ? AND radius_m IS NOT NULL
    """
    params = [periode]
    if bloks:
        query += f" AND id_blok IN ({','.join('?' * len(bloks))})"
        params.extend(bloks)
    query += " GROUP BY status"
    with get_connection() as conn:
        df = pd.read_sql_query(query, conn, params=params)
    total = df["jumlah"].sum()
    df["luas_ha"] = df["area_m2"] / 10_000
    df["persen"] = df["jumlah"] / total * 100 if total else 0.0
    df["status"] = pd.Categorical(df["status"], categories=STATUS_ORDER, ordered=True)
    return df.sort_values("status")


def get_openarea(bloks: list | None = None) -> pd.DataFrame:
    query = "SELECT id_blok, survey_date, periode_label, luas_ha FROM open_area_survey WHERE 1=1"
    params = []
    if bloks:
        query += f" AND id_blok IN ({','.join('?' * len(bloks))})"
        params.extend(bloks)
    with get_connection() as conn:
        return pd.read_sql_query(query, conn, params=params)


def get_photos(periode: str | None = None, bloks: list | None = None) -> pd.DataFrame:
    query = """SELECT id_photo, id_blok, survey_date, periode_label, file_path, thumbnail_path, lon, lat
               FROM drone_photos WHERE 1=1"""
    params = []
    if periode:
        query += " AND periode_label = ?"
        params.append(periode)
    if bloks:
        query += f" AND id_blok IN ({','.join('?' * len(bloks))})"
        params.extend(bloks)
    query += " ORDER BY survey_date"
    with get_connection() as conn:
        return pd.read_sql_query(query, conn, params=params)
