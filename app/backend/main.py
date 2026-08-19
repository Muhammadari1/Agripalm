from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import data as D

app = FastAPI(title="Dashboard Kesehatan Tanaman")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
MEDIA_DIR = Path(__file__).resolve().parent / "media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)


@app.get("/api/meta")
def get_meta():
    return {
        "bloks": list(D.get_blok_list()),
        "geo_periods": list(D.get_geo_periods()),
        "excel_periods": list(D.get_excel_periods()),
        "status_order": D.STATUS_ORDER,
        "status_colors": D.STATUS_COLORS,
        "status_labels": D.STATUS_LABELS,
    }


@app.get("/api/areas")
def get_areas():
    """Hierarki Region -> Estate -> Blok, dipakai untuk filter area global di frontend."""
    return list(D.get_area_hierarchy())


@app.get("/api/canopy")
def get_canopy(
    periode: str = Query(..., description="Periode survey drone, mis. '2024 R1'"),
    blok: str | None = Query(None, description="1 id_blok (alias tunggal utk 'bloks')"),
    bloks: str | None = Query(None, description="Comma-separated id_blok, kosong = semua"),
):
    bloks_list = D.parse_bloks_param(bloks) or (D.parse_bloks_param(blok))
    df = D.get_canopy(periode, bloks_list)

    total = len(df)
    sampled = False
    if total > D.MAX_MAP_POINTS:
        df = df.sample(D.MAX_MAP_POINTS, random_state=42)
        sampled = True

    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [row.lon, row.lat]},
            "properties": {
                "id_pohon": row.id_pohon,
                "Blok_ID": row.id_blok,
                "CANOPY": row.CANOPY,
                "DIAMETER": round(float(row.DIAMETER), 2) if pd_notna(row.DIAMETER) else None,
                "KELAS": row.KELAS,
            },
        }
        for row in df.itertuples()
    ]

    return JSONResponse(
        {
            "type": "FeatureCollection",
            "features": features,
            "meta": {"total": total, "returned": len(features), "sampled": sampled},
        }
    )


def pd_notna(v):
    try:
        return v == v  # NaN != NaN
    except Exception:
        return v is not None


@app.get("/api/growth")
def get_growth(bloks: str | None = Query(None)):
    df = D.get_growth(D.parse_bloks_param(bloks))
    return [
        {"periode": r.periode, "avg_diameter": round(float(r.avg_diameter), 3)}
        for r in df.itertuples()
    ]


@app.get("/api/growth-stats")
def get_growth_stats(blok: str = Query(..., description="id_blok")):
    """Fase 2: statistik pertumbuhan untuk 1 blok - dipakai panel analitik per-blok."""
    bloks_list = [blok]
    growth = D.get_growth(bloks_list)
    trend = D.get_trend(bloks_list)
    recap = D.get_recap(bloks_list)
    return {
        "blok": blok,
        "growth": [
            {"periode": r.periode, "avg_diameter": round(float(r.avg_diameter), 3)} for r in growth.itertuples()
        ],
        "trend_by_status": [
            {"periode": r.periode, "status": r.status, "avg_diameter": round(float(r.avg_diameter), 3)}
            for r in trend.itertuples()
        ],
        "recap": [
            {"periode": r.periode, "status": r.status, "jumlah": int(r.jumlah), "persen": round(float(r.persen), 2)}
            for r in recap.itertuples()
        ],
    }


@app.get("/api/trend")
def get_trend(bloks: str | None = Query(None)):
    df = D.get_trend(D.parse_bloks_param(bloks))
    return [
        {"periode": r.periode, "status": r.status, "avg_diameter": round(float(r.avg_diameter), 3)}
        for r in df.itertuples()
    ]


@app.get("/api/recap")
def get_recap(bloks: str | None = Query(None)):
    df = D.get_recap(D.parse_bloks_param(bloks))
    return [
        {"periode": r.periode, "status": r.status, "jumlah": int(r.jumlah), "persen": round(float(r.persen), 2)}
        for r in df.itertuples()
    ]


@app.get("/api/area")
def get_area(
    periode: str = Query(..., description="Periode survey drone, mis. '2024 R1'"),
    bloks: str | None = Query(None),
):
    df = D.get_area(periode, D.parse_bloks_param(bloks))
    return [
        {
            "status": r.status,
            "jumlah": int(r.jumlah),
            "luas_ha": round(float(r.luas_ha), 3),
            "persen": round(float(r.persen), 2),
        }
        for r in df.itertuples()
    ]


@app.get("/api/openarea")
def get_openarea(bloks: str | None = Query(None)):
    """Fase 2 (scaffold): belum ada sumber data open-area, tabel masih kosong.
    Endpoint sudah aktif supaya frontend/ETL bisa langsung dipakai begitu data tersedia."""
    df = D.get_openarea(D.parse_bloks_param(bloks))
    return [
        {
            "id_blok": r.id_blok,
            "survey_date": r.survey_date,
            "periode_label": r.periode_label,
            "luas_ha": r.luas_ha,
        }
        for r in df.itertuples()
    ]


@app.get("/api/photos")
def get_photos(periode: str | None = Query(None), bloks: str | None = Query(None)):
    df = D.get_photos(periode, D.parse_bloks_param(bloks))
    return [
        {
            "id_photo": int(r.id_photo),
            "id_blok": r.id_blok,
            "periode_label": r.periode_label,
            "file_path": r.file_path,
            "thumbnail_path": r.thumbnail_path,
            "lon": r.lon,
            "lat": r.lat,
        }
        for r in df.itertuples()
    ]


app.mount("/media", StaticFiles(directory=str(MEDIA_DIR)), name="media")
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
