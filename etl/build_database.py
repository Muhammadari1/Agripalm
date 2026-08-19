"""
ETL: konversi GeoJSON (deteksi drone per periode) + Excel (pengukuran manual/agregat)
ke database SQLite multitemporal (skema di db/schema.sql).

Masalah inti yang diselesaikan di sini: setiap file GeoJSON adalah hasil deteksi AI yang
BERDIRI SENDIRI per periode -- tidak ada id_pohon bawaan, dan urutan baris di file berbeda-beda
tiap periode. Supaya time-slider & grafik pertumbuhan per pohon bisa jalan, id_pohon harus
konsisten lintas periode. Pendekatan yang dipakai: spatial nearest-neighbor matching
(pohon kelapa sawit tidak berpindah tempat, jadi posisi jadi kunci pencocokan).

Jalankan untuk 1 blok (uji coba):
    python etl/build_database.py --blok GMKET23a --reset

Jalankan untuk semua blok:
    python etl/build_database.py --all --reset --quiet
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "Data"
DB_DIR = PROJECT_ROOT / "db"
SCHEMA_PATH = DB_DIR / "schema.sql"
DEFAULT_DB_PATH = DB_DIR / "kesehatan_tanaman.sqlite"
EXCEL_PATH = DATA_DIR / "Database Kanopi GMKE T23.xlsx"

GEO_PERIODE_ORDER = ["2024 R1", "2024 R2", "2025 R1", "2025 R2"]
EXCEL_PERIODE_ORDER = [
    "Februari 2023", "Agustus 2023", "Januari 2024", "Juli 2024",
    "Oktober 2024", "Mei 2025", "Oktober 2025", "Mei 2026",
]
DEFAULT_THRESHOLD_M = 5.0  # dikalibrasi empiris lewat scratchpad/tune_threshold.py, lihat README

INDO_MONTHS = {
    "Januari": 1, "Februari": 2, "Maret": 3, "April": 4, "Mei": 5, "Juni": 6,
    "Juli": 7, "Agustus": 8, "September": 9, "Oktober": 10, "November": 11, "Desember": 12,
}


# ---------------------------------------------------------------------------
# Helpers tanggal
# ---------------------------------------------------------------------------
def excel_periode_to_date(label: str) -> str:
    month_name, year = label.split()
    return f"{int(year):04d}-{INDO_MONTHS[month_name]:02d}-01"


def geo_periode_to_date(label: str) -> str:
    # Tanggal akuisisi pasti tidak ada di metadata sumber - ini tanggal NOMINAL
    # hanya untuk pengurutan kronologis (R1 -> awal tahun, R2 -> pertengahan tahun).
    year, round_ = label.split()
    return f"{int(year):04d}-{'01' if round_ == 'R1' else '07'}-01"


# ---------------------------------------------------------------------------
# Load sumber data (dibaca sekali dari disk, dipartisi per blok di memori -
# jauh lebih cepat daripada baca ulang file GeoJSON 17-21MB tiap blok)
# ---------------------------------------------------------------------------
def load_all_geojson() -> dict:
    """Return {periode_label: GeoDataFrame semua blok}."""
    files = sorted(DATA_DIR.glob("GMKET23 20* R*.geojson"))
    by_periode = {}
    for f in files:
        year_round = f.stem.replace("GMKET23 ", "")
        gdf = gpd.read_file(f)
        gdf = gdf.dropna(subset=["Blok_ID"])
        by_periode[year_round] = gdf
    return by_periode


def load_all_excel() -> pd.DataFrame:
    return pd.read_excel(EXCEL_PATH, sheet_name="Database")


def subset_by_blok(all_geo_by_periode: dict, id_blok: str) -> dict:
    out = {}
    for periode, gdf in all_geo_by_periode.items():
        subset = gdf[gdf["Blok_ID"] == id_blok]
        if not subset.empty:
            out[periode] = subset.reset_index(drop=True)
    return out


def list_all_bloks(all_geo_by_periode: dict) -> list:
    bloks = set()
    for gdf in all_geo_by_periode.values():
        bloks.update(gdf["Blok_ID"].dropna().unique().tolist())
    return sorted(bloks)


# ---------------------------------------------------------------------------
# Spatial ID matching
# ---------------------------------------------------------------------------
def estimate_typical_spacing_m(gdf: gpd.GeoDataFrame) -> float:
    """Median jarak ke tetangga terdekat dalam 1 periode -> proxy jarak tanam antar pohon."""
    xy = np.column_stack([gdf.geometry.x.values, gdf.geometry.y.values])
    tree = cKDTree(xy)
    dist, _ = tree.query(xy, k=2)  # k=1 adalah diri sendiri (jarak 0)
    return float(np.median(dist[:, 1]))


def estimate_safety_margin_m(gdf: gpd.GeoDataFrame) -> float:
    """Persentil-1 jarak ke tetangga terdekat dalam 1 periode -> proxy pasangan pohon TERDEKAT
    di seluruh blok. Threshold matching di atas angka ini berisiko 'salah lompat' ke pohon
    tetangga yang sebenarnya berbeda, bukan pohon yang sama di periode berikutnya."""
    xy = np.column_stack([gdf.geometry.x.values, gdf.geometry.y.values])
    tree = cKDTree(xy)
    dist, _ = tree.query(xy, k=2)
    return float(np.percentile(dist[:, 1], 1))


def match_trees_across_periods(id_blok: str, geo_by_periode: dict, threshold_m: float):
    """
    Greedy one-to-one nearest-neighbor matching, periode demi periode secara kronologis.
    Posisi acuan tiap id_pohon di-update ke posisi TERBARU tiap kali match (tracking),
    supaya toleran terhadap sedikit pergeseran titik pusat deteksi antar survey.
    """
    periods = [p for p in GEO_PERIODE_ORDER if p in geo_by_periode]

    active_xy: dict[str, tuple] = {}       # id_pohon -> (x, y) posisi UTM terakhir diketahui
    trees_master_rows = []
    canopy_rows = []
    qa = {p: {"total": 0, "matched": 0, "baru": 0, "hilang": 0, "distances": []} for p in periods}
    seq = 1

    for i, periode in enumerate(periods):
        gdf = geo_by_periode[periode]
        gdf_wgs84 = gdf.to_crs(epsg=4326)
        xs, ys = gdf.geometry.x.values, gdf.geometry.y.values
        lons, lats = gdf_wgs84.geometry.x.values, gdf_wgs84.geometry.y.values
        n = len(gdf)
        qa[periode]["total"] = n

        matched_id = [None] * n
        matched_dist = [None] * n

        if active_xy:
            tree_ids = list(active_xy.keys())
            tree_xy = np.array([active_xy[t] for t in tree_ids])
            kd = cKDTree(tree_xy)
            d, idx = kd.query(np.column_stack([xs, ys]), k=1)

            order = np.argsort(d)
            used_trees = set()
            for j in order:
                if d[j] > threshold_m:
                    break  # sisanya pasti lebih jauh lagi (sudah urut)
                t_id = tree_ids[idx[j]]
                if t_id in used_trees:
                    continue
                matched_id[j] = t_id
                matched_dist[j] = float(d[j])
                used_trees.add(t_id)

        seen_this_period = set()
        for j in range(n):
            row = gdf.iloc[j]
            if matched_id[j] is None:
                new_id = f"{id_blok}-{seq:06d}"
                seq += 1
                trees_master_rows.append({
                    "id_pohon": new_id,
                    "id_blok": id_blok,
                    "lon_awal": float(lons[j]),
                    "lat_awal": float(lats[j]),
                    "tahun_tanam": _safe_int(row.get("Tahun_Tana")),
                    "first_seen_periode": periode,
                })
                matched_id[j] = new_id
                qa[periode]["baru"] += 1
            else:
                qa[periode]["matched"] += 1
                qa[periode]["distances"].append(matched_dist[j])

            active_xy[matched_id[j]] = (xs[j], ys[j])
            seen_this_period.add(matched_id[j])

            canopy_rows.append({
                "id_pohon": matched_id[j],
                "id_blok": id_blok,
                "survey_date": geo_periode_to_date(periode),
                "periode_label": periode,
                "lon": float(lons[j]),
                "lat": float(lats[j]),
                "radius_m": _safe_float(row.get("RADIUS")),
                "diameter_m": _safe_float(row.get("DIAMETER")),
                "status": row.get("CANOPY"),
                "kelas": row.get("KELAS"),
                "confidence": _safe_float(row.get("CONFIDENCE")),
                "sumber_data": "drone_ai_detection",
                "match_distance_m": matched_dist[j],
            })

        qa[periode]["hilang"] = len(active_xy) - len(seen_this_period)

    return trees_master_rows, canopy_rows, qa


def _safe_float(v):
    try:
        return None if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)
    except (TypeError, ValueError):
        return None


def _safe_int(v):
    try:
        return None if v is None or (isinstance(v, float) and np.isnan(v)) else int(float(v))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# blok_master
# ---------------------------------------------------------------------------
def build_blok_master_row(id_blok: str, geo_by_periode: dict) -> dict:
    all_points = pd.concat([g for g in geo_by_periode.values()], ignore_index=True)
    largest_periode_gdf = max(geo_by_periode.values(), key=len)

    hull_area_m2 = largest_periode_gdf.geometry.union_all().convex_hull.area
    luas_ha = round(hull_area_m2 / 10_000, 2)

    tahun_tanam = _mode_or_none(all_points.get("Tahun_Tana"))
    populasi = _mode_or_none(all_points.get("Populasi"))
    sph = _mode_or_none(all_points.get("SPH"))
    region = _mode_or_none(all_points.get("Region"))
    estate = _mode_or_none(all_points.get("Estate"))
    divisi = _mode_or_none(all_points.get("Divisi"))

    return {
        "id_blok": id_blok,
        "nama_blok": id_blok,
        "region": region,
        "estate": estate,
        "divisi": None if divisi is None else str(divisi),
        "boundary_geojson": None,
        "luas_ha": luas_ha,
        "luas_metode": "estimasi_convex_hull",
        "tahun_tanam": _safe_int(tahun_tanam),
        "populasi": _safe_int(populasi),
        "sph": _safe_float(sph),
    }


def _mode_or_none(series):
    if series is None:
        return None
    s = series.dropna()
    if s.empty:
        return None
    return s.mode().iloc[0]


# ---------------------------------------------------------------------------
# Excel (tanpa geometri) -> canopy_survey
# ---------------------------------------------------------------------------
def build_excel_canopy_rows(id_blok: str, excel_df: pd.DataFrame) -> list:
    rows = []
    for periode in EXCEL_PERIODE_ORDER:
        subset = excel_df[excel_df["Periode"] == periode]
        for _, row in subset.iterrows():
            rows.append({
                "id_pohon": None,
                "id_blok": id_blok,
                "survey_date": excel_periode_to_date(periode),
                "periode_label": periode,
                "lon": None,
                "lat": None,
                "radius_m": None,
                "diameter_m": _safe_float(row["DIAMETER"]),
                "status": row["CANOPY"],
                "kelas": None,
                "confidence": None,
                "sumber_data": "excel_manual_agregat",
                "match_distance_m": None,
            })
    return rows


# ---------------------------------------------------------------------------
# Database write
# ---------------------------------------------------------------------------
def init_db(db_path: Path, reset: bool):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if reset and db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def write_blok_master(conn, row: dict):
    conn.execute(
        """INSERT OR REPLACE INTO blok_master
           (id_blok, nama_blok, region, estate, divisi, boundary_geojson, luas_ha, luas_metode,
            tahun_tanam, populasi, sph)
           VALUES (:id_blok, :nama_blok, :region, :estate, :divisi, :boundary_geojson, :luas_ha,
                   :luas_metode, :tahun_tanam, :populasi, :sph)""",
        row,
    )


def write_trees_master(conn, rows: list):
    conn.executemany(
        """INSERT OR REPLACE INTO trees_master
           (id_pohon, id_blok, lon_awal, lat_awal, tahun_tanam, first_seen_periode)
           VALUES (:id_pohon, :id_blok, :lon_awal, :lat_awal, :tahun_tanam, :first_seen_periode)""",
        rows,
    )


def write_canopy_survey(conn, rows: list):
    conn.executemany(
        """INSERT INTO canopy_survey
           (id_pohon, id_blok, survey_date, periode_label, lon, lat, radius_m, diameter_m,
            status, kelas, confidence, sumber_data, match_distance_m)
           VALUES (:id_pohon, :id_blok, :survey_date, :periode_label, :lon, :lat, :radius_m,
                   :diameter_m, :status, :kelas, :confidence, :sumber_data, :match_distance_m)""",
        rows,
    )


# ---------------------------------------------------------------------------
# QA report
# ---------------------------------------------------------------------------
def print_qa_report(id_blok: str, threshold_m: float, spacing_m: float, safety_margin_m: float, qa: dict):
    print(f"\n=== QA Matching id_pohon - blok {id_blok} ===")
    print(f"Median jarak antar pohon terdekat (periode pertama) : {spacing_m:.2f} m")
    print(f"Jarak pasangan pohon TERDEKAT di seluruh blok (P1)   : {safety_margin_m:.2f} m")
    print(f"Threshold matching yang dipakai                      : {threshold_m:.2f} m")
    if threshold_m > safety_margin_m:
        print(
            f"  PERINGATAN: threshold ({threshold_m:.2f}m) lebih besar dari jarak pohon "
            f"terdekat di blok ini ({safety_margin_m:.2f}m). Ada risiko kecil pohon "
            f"'lompat' ke ID tetangga yang salah di area yang rapat. Cek match_distance_m "
            f"yang tinggi di canopy_survey untuk spot-check manual."
        )
    print()

    header = f"{'Periode':<10}{'Total':>8}{'Matched':>10}{'Baru':>8}{'Hilang':>8}{'Match%':>9}{'Jarak median (m)':>20}"
    print(header)
    print("-" * len(header))
    for periode, s in qa.items():
        pct = (s["matched"] / s["total"] * 100) if s["total"] else 0.0
        med_dist = np.median(s["distances"]) if s["distances"] else float("nan")
        print(
            f"{periode:<10}{s['total']:>8}{s['matched']:>10}{s['baru']:>8}{s['hilang']:>8}"
            f"{pct:>8.1f}%{med_dist:>20.3f}"
        )
    print()


def process_blok(conn, id_blok: str, geo_by_periode: dict, excel_df_blok: pd.DataFrame, threshold_override):
    first_periode_gdf = geo_by_periode[[p for p in GEO_PERIODE_ORDER if p in geo_by_periode][0]]
    spacing_m = estimate_typical_spacing_m(first_periode_gdf)
    safety_margin_m = estimate_safety_margin_m(first_periode_gdf)
    # Default threshold dikalibrasi empiris (bukan spacing/2 - itu terlalu ketat untuk periode
    # belakangan karena drift titik pusat deteksi AI membesar seiring waktu, lihat catatan di README).
    threshold_m = threshold_override if threshold_override is not None else DEFAULT_THRESHOLD_M

    trees_rows, canopy_geo_rows, qa = match_trees_across_periods(id_blok, geo_by_periode, threshold_m)
    canopy_excel_rows = build_excel_canopy_rows(id_blok, excel_df_blok)
    blok_row = build_blok_master_row(id_blok, geo_by_periode)

    write_blok_master(conn, blok_row)
    write_trees_master(conn, trees_rows)
    write_canopy_survey(conn, canopy_geo_rows)
    write_canopy_survey(conn, canopy_excel_rows)

    return {
        "blok_row": blok_row,
        "n_trees": len(trees_rows),
        "n_canopy_drone": len(canopy_geo_rows),
        "n_canopy_excel": len(canopy_excel_rows),
        "threshold_m": threshold_m,
        "spacing_m": spacing_m,
        "safety_margin_m": safety_margin_m,
        "qa": qa,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="ETL data kanopi -> SQLite multitemporal")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--blok", help="proses 1 id_blok saja, mis. GMKET23a")
    group.add_argument("--all", action="store_true", help="proses semua blok yang ada di GeoJSON")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--threshold-m", type=float, default=None, help="override threshold matching (meter)")
    parser.add_argument("--reset", action="store_true", help="hapus & buat ulang database dari nol")
    parser.add_argument("--quiet", action="store_true", help="ringkas: sembunyikan detail QA per blok")
    args = parser.parse_args()

    db_path = Path(args.db_path)

    print("Loading semua file GeoJSON ...")
    all_geo = load_all_geojson()
    for p, g in all_geo.items():
        print(f"  {p}: {len(g)} titik total")

    print("Loading Excel ...")
    all_excel = load_all_excel()

    if args.all:
        target_bloks = list_all_bloks(all_geo)
    else:
        target_bloks = [args.blok]
    print(f"\nBlok yang diproses ({len(target_bloks)}): {', '.join(target_bloks)}")

    print(f"\nMenulis ke database: {db_path}")
    conn = init_db(db_path, reset=args.reset)

    summaries = []
    with conn:
        for id_blok in target_bloks:
            geo_by_periode = subset_by_blok(all_geo, id_blok)
            if not geo_by_periode:
                print(f"  [{id_blok}] dilewati - tidak ada data GeoJSON untuk blok ini")
                continue
            excel_df_blok = all_excel[all_excel["Blok_ID"] == id_blok].reset_index(drop=True)
            summary = process_blok(conn, id_blok, geo_by_periode, excel_df_blok, args.threshold_m)
            summaries.append((id_blok, summary))
            print(
                f"  [{id_blok}] trees_master={summary['n_trees']} "
                f"canopy_survey={summary['n_canopy_drone'] + summary['n_canopy_excel']} "
                f"(drone={summary['n_canopy_drone']}, excel={summary['n_canopy_excel']}) "
                f"luas~{summary['blok_row']['luas_ha']}Ha"
            )
    conn.close()

    total_trees = sum(s["n_trees"] for _, s in summaries)
    total_canopy = sum(s["n_canopy_drone"] + s["n_canopy_excel"] for _, s in summaries)
    print(f"\n=== Selesai: {len(summaries)} blok, {total_trees} pohon unik, {total_canopy} baris canopy_survey ===")

    if not args.quiet:
        for id_blok, summary in summaries:
            print_qa_report(id_blok, summary["threshold_m"], summary["spacing_m"], summary["safety_margin_m"], summary["qa"])


if __name__ == "__main__":
    main()
