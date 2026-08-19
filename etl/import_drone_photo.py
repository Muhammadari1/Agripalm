"""
Import 1 foto drone (JPG/TIF) ke tabel drone_photos.

Membuat versi web-friendly (thumbnail + full resized) di app/backend/media/photos/,
lalu insert 1 baris ke drone_photos. Posisi (lon/lat) default dihitung dari centroid
titik canopy_survey blok tsb pada periode yang sama - dipakai karena file foto sumber
seringkali TIDAK punya georeferensi yang valid/cocok (mis. hasil sample/placeholder),
jadi jangan asal percaya tag GeoTIFF tanpa dicek dulu. Override manual dengan --lon/--lat
kalau memang tahu lokasi persisnya.

Contoh:
    python etl/import_drone_photo.py --image "Data/GMKET23 2024 R1.JPG" ^
        --blok GMKET23a --periode "2024 R1"
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from PIL import Image

Image.MAX_IMAGE_PIXELS = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "db" / "kesehatan_tanaman.sqlite"
MEDIA_DIR = PROJECT_ROOT / "app" / "backend" / "media" / "photos"

FULL_MAX_DIM = 1600
THUMB_MAX_DIM = 360

INDO_MONTHS = {
    "Januari": 1, "Februari": 2, "Maret": 3, "April": 4, "Mei": 5, "Juni": 6,
    "Juli": 7, "Agustus": 8, "September": 9, "Oktober": 10, "November": 11, "Desember": 12,
}


def resize_max(img: Image.Image, max_dim: int) -> Image.Image:
    w, h = img.size
    scale = min(1.0, max_dim / max(w, h))
    if scale >= 1.0:
        return img.copy()
    return img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)


def periode_to_date(label: str) -> str:
    parts = label.split()
    if len(parts) == 2 and parts[0] in INDO_MONTHS:
        return f"{int(parts[1]):04d}-{INDO_MONTHS[parts[0]]:02d}-01"
    if len(parts) == 2 and parts[1] in ("R1", "R2"):
        return f"{int(parts[0]):04d}-{'01' if parts[1] == 'R1' else '07'}-01"
    raise ValueError(f"Tidak bisa parse periode: {label}")


def get_blok_centroid(conn, id_blok: str, periode: str):
    row = conn.execute(
        """SELECT AVG(lon), AVG(lat), COUNT(*) FROM canopy_survey
           WHERE id_blok = ? AND periode_label = ? AND lon IS NOT NULL""",
        (id_blok, periode),
    ).fetchone()
    if row is None or row[2] == 0:
        return None, None
    return row[0], row[1]


def main():
    parser = argparse.ArgumentParser(description="Import foto drone ke drone_photos")
    parser.add_argument("--image", required=True, help="path file JPG/TIF sumber")
    parser.add_argument("--blok", required=True, help="id_blok, mis. GMKET23a")
    parser.add_argument("--periode", required=True, help="periode_label, mis. '2024 R1'")
    parser.add_argument("--lon", type=float, default=None, help="override lon (default: centroid blok)")
    parser.add_argument("--lat", type=float, default=None, help="override lat (default: centroid blok)")
    parser.add_argument("--db-path", default=str(DB_PATH))
    args = parser.parse_args()

    src = Path(args.image)
    if not src.exists():
        raise SystemExit(f"File tidak ditemukan: {src}")

    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    slug = f"{args.blok}_{args.periode.replace(' ', '')}"
    full_path = MEDIA_DIR / f"{slug}_full.jpg"
    thumb_path = MEDIA_DIR / f"{slug}_thumb.jpg"

    print(f"Loading {src} ...")
    im = Image.open(src).convert("RGB")
    print(f"  ukuran asli: {im.size}")

    resize_max(im, FULL_MAX_DIM).save(full_path, "JPEG", quality=85, optimize=True)
    resize_max(im, THUMB_MAX_DIM).save(thumb_path, "JPEG", quality=80, optimize=True)
    print(f"  full  -> {full_path.name} ({full_path.stat().st_size // 1024} KB)")
    print(f"  thumb -> {thumb_path.name} ({thumb_path.stat().st_size // 1024} KB)")

    conn = sqlite3.connect(args.db_path)
    lon, lat = args.lon, args.lat
    if lon is None or lat is None:
        c_lon, c_lat = get_blok_centroid(conn, args.blok, args.periode)
        lon = lon if lon is not None else c_lon
        lat = lat if lat is not None else c_lat
        if lon is None:
            print("  PERINGATAN: tidak ada data canopy_survey untuk blok/periode ini, "
                  "lon/lat disimpan NULL. Berikan --lon/--lat manual kalau perlu tampil di peta.")

    survey_date = periode_to_date(args.periode)
    with conn:
        conn.execute(
            """INSERT INTO drone_photos (id_blok, survey_date, periode_label, file_path, thumbnail_path, lon, lat)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                args.blok, survey_date, args.periode,
                f"/media/photos/{full_path.name}", f"/media/photos/{thumb_path.name}",
                lon, lat,
            ),
        )
    conn.close()
    print(f"\nTersimpan: drone_photos untuk {args.blok} / {args.periode} (lon={lon}, lat={lat})")


if __name__ == "__main__":
    main()
