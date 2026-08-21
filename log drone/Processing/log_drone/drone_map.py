"""
Render peta statis (PNG) jalur terbang drone di atas basemap blok Aresta, dan sediakan
GeoJSON gabungan (blok Aresta + jalur) untuk peta interaktif Leaflet di halaman detail.

Logika basemap/label/reprojeksi diadaptasi dari project standalone DroneLogExtractor
(basemap.py + flight_log_batch.py, sudah diuji), disesuaikan di sini supaya baca layer
Aresta lewat ARESTA_PATH milik dashboard (batch_worker.py) -- bukan file lokal tetap --
dan reuse perbaikan geometri (_repair_aresta_geometries) yang sudah ada, bukan ditulis
ulang.
"""
import os
import re
from datetime import datetime, timedelta

import geopandas as gpd
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from pyproj import Transformer
from shapely.geometry import box

from batch_worker import ARESTA_PATH, _repair_aresta_geometries

SRC_CRS = "EPSG:4326"
DEFAULT_DST_CRS = "EPSG:32749"  # WGS84 / UTM zone 49S (sesuai Aresta)

_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\arial.ttf",
]


def _load_font(size):
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def reproject_track(rows, dst_crs=DEFAULT_DST_CRS):
    transformer = Transformer.from_crs(SRC_CRS, dst_crs, always_xy=True)
    out = []
    for r in rows:
        if r.get("time_s") is None:
            continue
        x, y = transformer.transform(r["lon_deg"], r["lat_deg"])
        out.append({**r, "x": x, "y": y})
    out.sort(key=lambda r: r["time_s"])
    return out


def _compute_extent(track, padding_pct=25.0, resolution=(1600, 1200), min_span_m=50.0):
    xs = [p["x"] for p in track]
    ys = [p["y"] for p in track]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)

    span_x = max(xmax - xmin, min_span_m)
    span_y = max(ymax - ymin, min_span_m)
    pad = 1.0 + padding_pct / 100.0
    span_x *= pad
    span_y *= pad

    target_ratio = resolution[0] / resolution[1]
    cur_ratio = span_x / span_y
    if cur_ratio < target_ratio:
        span_x = span_y * target_ratio
    else:
        span_y = span_x / target_ratio

    cx, cy = (xmin + xmax) / 2.0, (ymin + ymax) / 2.0
    return (cx - span_x / 2, cy - span_y / 2, cx + span_x / 2, cy + span_y / 2)


def _load_aresta_gdf(extent=None, work_dir=None):
    """Baca layer Aresta (dengan perbaikan geometri, reuse batch_worker), opsional
    dibatasi bbox extent (UTM meter) untuk performa. Return GeoDataFrame kosong kalau
    Aresta tidak ada/gagal dibaca (peta tetap dibuat, cuma tanpa blok)."""
    if not ARESTA_PATH or not os.path.isfile(ARESTA_PATH):
        return gpd.GeoDataFrame()
    try:
        path = _repair_aresta_geometries(ARESTA_PATH, work_dir or os.path.dirname(ARESTA_PATH))
        if extent is not None:
            xmin, ymin, xmax, ymax = extent
            return gpd.read_file(path, bbox=(xmin, ymin, xmax, ymax))
        return gpd.read_file(path)
    except Exception:
        return gpd.GeoDataFrame()


def _render_basemap_image(gdf, extent, resolution=(1600, 1200), dpi=100):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    xmin, ymin, xmax, ymax = extent
    w_px, h_px = resolution
    fig = plt.figure(figsize=(w_px / dpi, h_px / dpi), dpi=dpi)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(xmin, xmax)
    ax.set_ylim(ymin, ymax)
    ax.set_aspect("equal")
    ax.axis("off")

    if gdf is not None and len(gdf) > 0:
        gdf.plot(ax=ax, facecolor="#dfead3", edgecolor="#6f8f57", linewidth=0.6)
    else:
        ax.set_facecolor("#e8e8e8")

    fig.canvas.draw()
    buf = np.asarray(fig.canvas.buffer_rgba())
    real_h, real_w = buf.shape[0], buf.shape[1]
    img = Image.fromarray(buf, "RGBA").convert("RGB")
    plt.close(fig)

    def world_to_pixel(x, y):
        px = (x - xmin) / (xmax - xmin) * real_w
        py = (ymax - y) / (ymax - ymin) * real_h
        return px, py

    return img, world_to_pixel, (real_w, real_h)


def render_flight_map_png(rows, out_path, padding_pct=25.0, resolution=(1600, 1200), work_dir=None):
    """Render 1 PNG: basemap Aresta (kalau ada) + jalur penuh + marker Takeoff (hijau)
    / Landing (merah)."""
    track = reproject_track(rows)
    if len(track) < 2:
        raise ValueError("Tidak cukup titik koordinat (dengan waktu) untuk membuat peta.")

    extent = _compute_extent(track, padding_pct=padding_pct, resolution=resolution)
    gdf = _load_aresta_gdf(extent=extent, work_dir=work_dir)
    if gdf is not None and len(gdf) > 0 and gdf.crs and gdf.crs.to_string() != DEFAULT_DST_CRS:
        try:
            gdf = gdf.to_crs(DEFAULT_DST_CRS)
        except Exception:
            pass

    base_img, world_to_pixel, real_res = _render_basemap_image(gdf, extent, resolution=resolution)
    base_rgba = base_img.convert("RGBA")

    trail_px = [world_to_pixel(p["x"], p["y"]) for p in track]
    draw = ImageDraw.Draw(base_rgba)
    draw.line(trail_px, fill=(255, 140, 0), width=4, joint="curve")

    r = max(6, int(real_res[0] * 0.008))
    font = _load_font(max(14, int(real_res[0] * 0.02)))

    sx, sy = trail_px[0]
    draw.ellipse([sx - r, sy - r, sx + r, sy + r], fill=(22, 163, 74), outline=(255, 255, 255), width=2)
    draw.text((sx + r + 4, sy - r), "Takeoff", font=font, fill=(22, 163, 74),
               stroke_width=2, stroke_fill=(255, 255, 255))

    ex, ey = trail_px[-1]
    draw.ellipse([ex - r, ey - r, ex + r, ey + r], fill=(220, 38, 38), outline=(255, 255, 255), width=2)
    draw.text((ex + r + 4, ey - r), "Landing", font=font, fill=(220, 38, 38),
               stroke_width=2, stroke_fill=(255, 255, 255))

    base_rgba.convert("RGB").save(out_path)


def build_track_geojson(rows):
    """FeatureCollection sederhana (1 LineString jalur + 2 Point start/end), EPSG:4326,
    untuk digabung dengan geojson blok Aresta di endpoint Flask."""
    coords = [[r["lon_deg"], r["lat_deg"]] for r in rows]
    first, last = rows[0], rows[-1]
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"kind": "track"},
                "geometry": {"type": "LineString", "coordinates": coords},
            },
            {
                "type": "Feature",
                "properties": {"kind": "takeoff"},
                "geometry": {"type": "Point", "coordinates": [first["lon_deg"], first["lat_deg"]]},
            },
            {
                "type": "Feature",
                "properties": {"kind": "landing"},
                "geometry": {"type": "Point", "coordinates": [last["lon_deg"], last["lat_deg"]]},
            },
        ],
    }


def build_aresta_geojson(rows, work_dir=None):
    """Blok Aresta yang overlap dengan bbox jalur terbang (EPSG:4326) sebagai dict
    GeoJSON siap di-jsonify."""
    track = reproject_track(rows)
    if len(track) < 2:
        return {"type": "FeatureCollection", "features": []}
    extent = _compute_extent(track, padding_pct=25.0)
    gdf = _load_aresta_gdf(extent=extent, work_dir=work_dir)
    if gdf is None or len(gdf) == 0:
        return {"type": "FeatureCollection", "features": []}
    if gdf.crs and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)
    return gdf.__geo_interface__


# ---------------- Multi-track (Report Log Drone: gabungan banyak file jadi 1 peta) ----

def reproject_track_multi(rows, dst_crs=DEFAULT_DST_CRS):
    """Sama seperti reproject_track, tapi rows berisi banyak flight log sekaligus
    (tiap row ditandai key "track_idx" = file asal). Reprojeksi & urutkan PER TRACK
    (bukan digabung lalu diurutkan global oleh time_s) -- karena tiap flight log
    punya time_s yang mulai dari 0 lagi, kalau disatukan lalu diurut global titiknya
    akan saling interleave/meloncat antar file. Return list of point-list, 1 per
    file, urut sesuai track_idx."""
    transformer = Transformer.from_crs(SRC_CRS, dst_crs, always_xy=True)
    tracks = {}
    for r in rows:
        if r.get("time_s") is None:
            continue
        x, y = transformer.transform(r["lon_deg"], r["lat_deg"])
        tracks.setdefault(r["track_idx"], []).append({**r, "x": x, "y": y})
    ordered = []
    for idx in sorted(tracks.keys()):
        pts = sorted(tracks[idx], key=lambda p: p["time_s"])
        if pts:
            ordered.append(pts)
    return ordered


def _find_aresta_column(gdf, aliases):
    """Find an Aresta attribute despite casing or underscore differences."""
    normalized = {
        str(column).strip().lower().replace("_", "").replace(" ", ""): column
        for column in gdf.columns
    }
    for alias in aliases:
        column = normalized.get(alias.lower().replace("_", "").replace(" ", ""))
        if column:
            return column
    return None


def _map_label_candidates(gdf, extent=None):
    """Return label candidates for Estate and block attributes using points inside polygons.

    Estate is rendered once per estate, while each block gets its own candidate.
    ``representative_point`` is preferred over a centroid because it stays inside
    concave or multi-part polygons and is more useful for a label.
    """
    if gdf is None or len(gdf) == 0:
        return []

    clip_box = box(*extent) if extent is not None else None
    candidates = []
    estate_column = _find_aresta_column(gdf, ("Estate", "ESTATE", "Nama_Estate"))
    # Kode_Blok/Blok_SAP berisi nama blok murni (mis. Q45a), sedangkan
    # Blok_ID biasanya menggabungkan kode Estate di depannya (mis. BBNEQ45a).
    block_column = _find_aresta_column(
        gdf, ("Kode_Blok", "Blok_SAP", "NO_BLOK", "NOBLOK", "Blok", "Blok_ID", "BLOCK_ID", "BLOCK")
    )
    if estate_column:
        for name, group in gdf.groupby(estate_column):
            name = str(name).strip()
            if not name:
                continue
            geometries = [g for g in group.geometry if g is not None and not g.is_empty]
            if clip_box is not None:
                geometries = [g.intersection(clip_box) for g in geometries]
                geometries = [g for g in geometries if not g.is_empty]
            if not geometries:
                continue
            cleaned_geometries = []
            for geometry in geometries:
                try:
                    cleaned_geometries.append(geometry if geometry.is_valid else geometry.buffer(0))
                except Exception:
                    continue
            if not cleaned_geometries:
                continue
            try:
                geometry = gpd.GeoSeries(cleaned_geometries, crs=gdf.crs).union_all()
            except Exception:
                try:
                    geometry = gpd.GeoSeries(cleaned_geometries, crs=gdf.crs).unary_union
                except Exception:
                    geometry = max(cleaned_geometries, key=lambda value: value.area)
            point = geometry.representative_point()
            candidates.append({
                "kind": "estate", "text": name, "x": point.x, "y": point.y,
                "geometry": geometry,
            })

    if block_column:
        for _, row in gdf.iterrows():
            name = str(row.get(block_column) or "").strip()
            geometry = row.geometry
            if not name or geometry is None or geometry.is_empty:
                continue
            try:
                if clip_box is not None:
                    geometry = geometry.intersection(clip_box)
                if geometry.is_empty:
                    continue
                if not geometry.is_valid:
                    geometry = geometry.buffer(0)
                if geometry.is_empty:
                    continue
                point = geometry.representative_point()
            except Exception:
                continue
            candidates.append({
                "kind": "block", "text": name, "x": point.x, "y": point.y,
                "geometry": geometry,
            })

    return candidates


def _draw_map_labels(draw, gdf, world_to_pixel, real_res, extent=None):
    """Draw non-overlapping Estate/Blok labels in pixel space.

    Estate labels are placed first, with several nearby fallback positions. A
    label without a collision-free position is omitted so a zoomed-out map stays
    readable instead of rendering a pile of overlapping text.
    """
    candidates = _map_label_candidates(gdf, extent=extent)
    if not candidates:
        return

    map_width, map_height = real_res
    occupied = []
    scale_ratio = None
    if extent is not None:
        xmin, _, xmax, _ = extent
        meters_per_px = (xmax - xmin) / map_width
        scale_ratio = _nice_scale_ratio(meters_per_px * 100 / 0.0254)

    # Estate tetap diprioritaskan sebagai konteks area. Untuk blok, dahulukan
    # polygon yang paling besar di layar agar pada skala 1:5.000 atau lebih
    # detail, label blok yang benar-benar terlihat tidak kalah oleh polygon kecil.
    candidates.sort(key=lambda item: (
        item["kind"] != "estate",
        -(abs(world_to_pixel(item["geometry"].bounds[2], item["geometry"].bounds[1])[0]
             - world_to_pixel(item["geometry"].bounds[0], item["geometry"].bounds[3])[0])
          * abs(world_to_pixel(item["geometry"].bounds[2], item["geometry"].bounds[1])[1]
                - world_to_pixel(item["geometry"].bounds[0], item["geometry"].bounds[3])[1])),
        item["text"],
    ))
    for item in candidates:
        if item["kind"] == "block" and scale_ratio is not None and scale_ratio > 30000:
            continue
        geometry = item["geometry"]
        min_x, min_y, max_x, max_y = geometry.bounds
        left_px, top_px = world_to_pixel(min_x, max_y)
        right_px, bottom_px = world_to_pixel(max_x, min_y)
        polygon_span = min(abs(right_px - left_px), abs(bottom_px - top_px))
        if item["kind"] == "estate":
            font_size = max(16, min(42, int(polygon_span * 0.14)))
        else:
            # Block labels scale with the visible block. The minimum is higher
            # for detailed maps so labels remain visible at 1:5.000 and below,
            # while the cap keeps them smaller than Estate labels.
            block_factor = 0.08 if scale_ratio is not None and scale_ratio > 10000 else 0.14
            block_max = 18 if scale_ratio is not None and scale_ratio > 10000 else 26
            font_size = max(10, min(block_max, int(polygon_span * block_factor)))
        font = _load_font(font_size)
        text = item["text"]
        px, py = world_to_pixel(item["x"], item["y"])
        bbox = draw.textbbox((0, 0), text, font=font, stroke_width=2)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        if text_width > map_width * 0.45:
            smaller_size = max(10, int(font.size * (map_width * 0.45 / text_width)))
            font = _load_font(smaller_size)
            bbox = draw.textbbox((0, 0), text, font=font, stroke_width=2)
            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]

        gap = max(4, int(map_width * 0.004))
        offsets = [
            (0, 0),
            (0, -(text_height + gap)),
            (0, text_height + gap),
            (text_width / 2 + gap, 0),
            (-(text_width / 2 + gap), 0),
            (text_width / 2 + gap, -(text_height + gap)),
            (-(text_width / 2 + gap), -(text_height + gap)),
            (text_width / 2 + gap, text_height + gap),
            (-(text_width / 2 + gap), text_height + gap),
        ]
        for dx, dy in offsets:
            left = px + dx - text_width / 2 - gap
            top = py + dy - text_height / 2 - gap
            right = left + text_width + 2 * gap
            bottom = top + text_height + 2 * gap
            if left < 0 or top < 0 or right > map_width or bottom > map_height:
                continue
            if any(left < r[2] and right > r[0] and top < r[3] and bottom > r[1] for r in occupied):
                continue

            draw.text(
                (px + dx - text_width / 2, py + dy - text_height / 2),
                text,
                font=font,
                fill=(20, 20, 20),
                stroke_width=2,
                stroke_fill=(255, 255, 255),
            )
            occupied.append((left, top, right, bottom))
            break


def _nice_scale_length_m(target_m):
    """Bulatkan target_m ke angka 'bagus' (1-2-5 x 10^n) terdekat ke bawah, dipakai
    supaya skala bar tidak menampilkan angka aneh seperti '187 m'."""
    steps = [1, 2, 5]
    magnitude = 1
    best = 1
    while True:
        for s in steps:
            val = s * magnitude
            if val <= target_m:
                best = val
            else:
                return best
        magnitude *= 10


_INDONESIAN_MONTHS = [
    "Januari", "Februari", "Maret", "April", "Mei", "Juni",
    "Juli", "Agustus", "September", "Oktober", "November", "Desember",
]


def _indonesian_month_year_label(dates):
    """Judul "Bulan Tahun" dari kumpulan tanggal flight (file_metas startTime) --
    1 bulan -> "Juni 2027", beberapa bulan tahun sama -> "Juni & Juli 2027" /
    "Juni, Juli & Agustus 2027", beda tahun -> tahun ditulis per bulan. Nama bulan
    Indonesia di-hardcode (bukan strftime("%B"), tidak reliable lintas-locale
    Windows)."""
    if not dates:
        return ""
    pairs = sorted({(d.year, d.month) for d in dates})
    years = {y for y, _ in pairs}
    if len(years) == 1:
        names = [_INDONESIAN_MONTHS[m - 1] for _, m in pairs]
        year = pairs[0][0]
        if len(names) == 1:
            return f"{names[0]} {year}"
        if len(names) == 2:
            return f"{names[0]} & {names[1]} {year}"
        return f"{', '.join(names[:-1])} & {names[-1]} {year}"
    labels = [f"{_INDONESIAN_MONTHS[m - 1]} {y}" for y, m in pairs]
    if len(labels) == 2:
        return " & ".join(labels)
    return f"{', '.join(labels[:-1])} & {labels[-1]}"


def _nice_scale_ratio(target_ratio):
    """Sama pola dengan _nice_scale_length_m, tapi untuk SKALA ANGKA (rasio cetak
    1:X) -- dibulatkan ke angka kartografi standar 1-2-5 x 10^n terdekat
    (1.000/2.000/5.000/10.000/25.000/50.000 dst)."""
    return max(2000, _nice_scale_length_m(target_ratio))


def _fit_font_to_width(draw, text, max_width, base_size, min_size):
    """Cari ukuran font terbesar (turun dari base_size) yang muat di max_width --
    supaya judul bisa dibuat besar tapi tetap otomatis mengecil kalau teksnya
    (mis. "Desember 2026 & Januari 2027") kepanjangan untuk kotak sidebar."""
    size = base_size
    while size > min_size:
        font = _load_font(size)
        bbox = draw.textbbox((0, 0), text, font=font)
        if bbox[2] - bbox[0] <= max_width:
            return font
        size -= 2
    return _load_font(min_size)


def _draw_title_box(draw, box, line1, line2, canvas_h, line3=None):
    x0, y0, x1, y1 = box
    cx = (x0 + x1) / 2
    max_w = (x1 - x0) * 0.9

    f1 = _fit_font_to_width(draw, line1, max_w, max(30, int(canvas_h * 0.052)), 18)
    if line3:
        f2 = _fit_font_to_width(draw, line2 or "", max_w, max(20, int(canvas_h * 0.030)), 13)
        f3 = _fit_font_to_width(draw, line3, max_w, max(18, int(canvas_h * 0.028)), 12)
        draw.text((cx, y0 + (y1 - y0) * 0.28), line1, font=f1, fill=(20, 20, 20), anchor="mm")
        draw.text((cx, y0 + (y1 - y0) * 0.55), line2, font=f2, fill=(20, 20, 20), anchor="mm")
        draw.text((cx, y0 + (y1 - y0) * 0.78), line3, font=f3, fill=(20, 20, 20), anchor="mm")
    else:
        draw.text((cx, y0 + (y1 - y0) * 0.36), line1, font=f1, fill=(20, 20, 20), anchor="mm")
        if line2:
            f2 = _fit_font_to_width(draw, line2, max_w, max(20, int(canvas_h * 0.034)), 13)
            draw.text((cx, y0 + (y1 - y0) * 0.72), line2, font=f2, fill=(20, 20, 20), anchor="mm")


def _draw_north_arrow(draw, box, canvas_h):
    x0, y0, x1, y1 = box
    cx = (x0 + x1) / 2
    bh = y1 - y0
    label_font = _load_font(max(13, int(canvas_h * 0.018)))
    n_font = _load_font(max(18, int(canvas_h * 0.026)))

    draw.text((cx, y0 + bh * 0.14), "Orientation", font=label_font, fill=(20, 20, 20), anchor="mm")
    draw.text((cx, y0 + bh * 0.32), "N", font=n_font, fill=(20, 20, 20), anchor="mm")

    apex_y = y0 + bh * 0.46
    tail_y = y0 + bh * 0.86
    shaft_w = max(3, int(canvas_h * 0.004))
    head_w = bh * 0.11
    draw.polygon(
        [(cx, apex_y), (cx - head_w, apex_y + head_w * 1.4), (cx + head_w, apex_y + head_w * 1.4)],
        fill=(20, 20, 20),
    )
    draw.line([(cx, apex_y + head_w * 1.2), (cx, tail_y)], fill=(20, 20, 20), width=shaft_w)


def _draw_scale_box(draw, box, extent, canvas_h, map_real_res, dpi=100):
    """Cuma skala ANGKA (rasio 1:X) -- tanpa skala garis/grafik, sesuai instruksi
    user -- teksnya dibuat besar & rata tengah (horizontal & vertikal) di kotak."""
    x0, y0, x1, y1 = box
    cx = (x0 + x1) / 2
    bh = y1 - y0
    title_font = _load_font(max(15, int(canvas_h * 0.02)))
    value_font = _load_font(max(22, int(canvas_h * 0.034)))

    xmin, _, xmax, _ = extent
    meters_per_px = (xmax - xmin) / map_real_res[0]
    ratio_str = f"1 : {_nice_scale_ratio(meters_per_px * dpi / 0.0254):,}".replace(",", ".")

    draw.text((cx, y0 + bh * 0.35), "Skala Angka", font=title_font, fill=(30, 30, 30), anchor="mm")
    draw.text((cx, y0 + bh * 0.65), ratio_str, font=value_font, fill=(30, 30, 30), anchor="mm")


def _draw_legend_box(draw, box, canvas_h):
    """Isi legenda menyesuaikan simbol yang BENAR dipakai di peta Report Log Drone:
    jalur terbang, 1 titik per area (label tanggal), blok Aresta."""
    x0, y0, x1, y1 = box
    pad = int(canvas_h * 0.014)
    title_font = _load_font(max(15, int(canvas_h * 0.02)))
    item_font = _load_font(max(13, int(canvas_h * 0.017)))

    y = y0 + pad
    draw.text((x0 + pad, y), "Legenda", font=title_font, fill=(20, 20, 20))
    y += title_font.size + int(pad * 1.2)

    row_h = item_font.size + int(pad * 0.9)
    sw = int(canvas_h * 0.022)

    items = [
        ("line", (255, 140, 0), "Jalur Terbang"),
        ("dot", (255, 140, 0), "Titik Penerbangan"),
        ("box", (223, 234, 211), "Blok (Aresta)"),
    ]
    for kind, color, label in items:
        cy = y + row_h / 2
        if kind == "line":
            draw.line([(x0 + pad, cy), (x0 + pad + sw, cy)], fill=color, width=4)
        elif kind == "dot":
            r = sw / 2.2
            draw.ellipse([x0 + pad + sw / 2 - r, cy - r, x0 + pad + sw / 2 + r, cy + r],
                         fill=color, outline=(255, 255, 255), width=1)
        else:
            draw.rectangle([x0 + pad, cy - sw / 2, x0 + pad + sw, cy + sw / 2],
                            fill=color, outline=(111, 143, 87), width=1)
        draw.text((x0 + pad + sw + pad * 0.6, cy), label, font=item_font, fill=(30, 30, 30), anchor="lm")
        y += row_h


def _cluster_tracks_by_proximity(tracks, proximity_m=300.0):
    """Kelompokkan track (1 track = 1 file asal) yang lokasinya berdekatan jadi 1
    cluster -- dipakai untuk kotak keterangan gabungan per area (beda dari
    pengelompokan per Estate). Union-find sederhana, transitif: kalau A dekat B dan
    B dekat C, ketiganya jadi 1 cluster meski A jauh dari C. Jarak dihitung dari
    centroid tiap track (meter, CRS UTM). Return list of list-of-index (index ke
    `tracks`)."""
    n = len(tracks)
    centroids = []
    for t in tracks:
        xs = [p["x"] for p in t]
        ys = [p["y"] for p in t]
        centroids.append((sum(xs) / len(xs), sum(ys) / len(ys)))

    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(n):
        for j in range(i + 1, n):
            dx = centroids[i][0] - centroids[j][0]
            dy = centroids[i][1] - centroids[j][1]
            if (dx * dx + dy * dy) ** 0.5 <= proximity_m:
                union(i, j)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def _parse_iso(raw):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


_FILENAME_DATETIME_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[_ ]\[?(\d{2})-(\d{2})-(\d{2})\]?")
_FILENAME_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _extract_flight_datetime(filename, fallback_iso=None):
    """Waktu terbang drone SEBENARNYA -- diambil dari NAMA FILE .txt (konvensi DJI
    "FlightRecord_YYYY-MM-DD_[HH-MM-SS].txt", ditulis app DJI saat rekam, akurat).
    TIDAK pakai flight_meta["startTime"] dari JSON hasil opendronelog.com --
    lapangan itu ternyata mencerminkan waktu file diproses/diupload ke situs itu,
    BUKAN waktu terbang asli (file .txt DJI sendiri berformat biner, tidak bisa
    dibaca tanggalnya langsung tanpa decoder resmi DJI, makanya pakai nama file).
    fallback_iso dipakai kalau nama file tidak cocok pola DJI (mis. Litchi/CSV)."""
    if filename:
        m = _FILENAME_DATETIME_RE.search(filename)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                                 int(m.group(4)), int(m.group(5)), int(m.group(6)))
            except ValueError:
                pass
        m2 = _FILENAME_DATE_RE.search(filename)
        if m2:
            try:
                return datetime(int(m2.group(1)), int(m2.group(2)), int(m2.group(3)))
            except ValueError:
                pass
    return _parse_iso(fallback_iso)


def _format_date_ddmmyy(dt):
    """Format datetime jadi "DD-MM-YY", dipakai sebagai label titik penerbangan
    per area di peta. Return "" kalau None."""
    return dt.strftime("%d-%m-%y") if dt else ""


def _cluster_date_range(cluster, tracks, file_metas):
    """(earliest, latest) tanggal penerbangan di antara semua track dalam 1
    cluster (berdasar _extract_flight_datetime, bukan flight_meta["startTime"]
    yang tidak akurat)."""
    earliest = latest = None
    for i in cluster:
        track_idx = tracks[i][0]["track_idx"]
        meta, filename = file_metas.get(track_idx) or ({}, None)
        dt = _extract_flight_datetime(filename, meta.get("startTime"))
        if dt is not None:
            if earliest is None or dt < earliest:
                earliest = dt
            if latest is None or dt > latest:
                latest = dt
    return earliest, latest


def _format_date_period(earliest, latest):
    """"DD-MM-YY" kalau cuma 1 tanggal, atau "DD-MM-YY s.d DD-MM-YY" kalau
    cluster-nya diterbangi di rentang beberapa tanggal berbeda."""
    if earliest is None:
        return "-"
    e = _format_date_ddmmyy(earliest)
    if latest is None or latest.date() == earliest.date():
        return e
    return f"{e} s.d {_format_date_ddmmyy(latest)}"


def _cluster_label_positions(tracks, file_metas):
    """1 label per cluster track yang berdekatan (bukan per track) -- isinya
    tanggal penerbangan paling awal di cluster itu (format DD-MM-YY), diposisikan
    di titik yang paling dekat ke centroid cluster. Label yang sama persis dipakai
    di peta (render_flight_map_png_multi) DAN sebagai kolom "Titik" di tabel rekap
    Report/PPT (compute_cluster_recaps) -- supaya keduanya selalu sinkron."""
    if not file_metas:
        return []
    clusters = _cluster_tracks_by_proximity(tracks)
    out = []
    for cluster in clusters:
        cluster_pts = [p for i in cluster for p in tracks[i]]
        cx = sum(p["x"] for p in cluster_pts) / len(cluster_pts)
        cy = sum(p["y"] for p in cluster_pts) / len(cluster_pts)
        nearest = min(cluster_pts, key=lambda p: (p["x"] - cx) ** 2 + (p["y"] - cy) ** 2)
        earliest, _ = _cluster_date_range(cluster, tracks, file_metas)
        label = _format_date_ddmmyy(earliest) or "-"
        out.append((label, nearest["x"], nearest["y"]))
    return out


def compute_cluster_recaps(rows, file_metas, uploader_name):
    """Hitung rekap per cluster track yang berdekatan (Titik/Nama user/Periode
    waktu/Durasi/Jarak TOTAL dari semua track dalam cluster itu) untuk tabel rekap
    di Report (PPT). "Titik" = label yang sama persis dengan yang digambar di peta
    untuk cluster itu (lihat _cluster_label_positions) -- supaya baris tabel bisa
    dicocokkan langsung ke titik yang mana di peta. "Periode waktu" = rentang
    tanggal terbang di area itu (bisa beda dari "Titik" kalau area itu diterbangi
    di beberapa tanggal berbeda -- "Titik" cuma tanggal paling awal)."""
    if not file_metas:
        return []
    tracks = [t for t in reproject_track_multi(rows) if len(t) >= 2]
    if not tracks:
        return []
    clusters = _cluster_tracks_by_proximity(tracks)

    recaps = []
    for cluster in clusters:
        total_duration = 0.0
        total_distance = 0.0
        have_duration = False
        have_distance = False
        for i in cluster:
            track_idx = tracks[i][0]["track_idx"]
            meta, _ = file_metas.get(track_idx) or ({}, None)
            dur = meta.get("durationSecs")
            dist = meta.get("totalDistance")
            if dur is not None:
                total_duration += dur
                have_duration = True
            if dist is not None:
                total_distance += dist
                have_distance = True

        earliest, latest = _cluster_date_range(cluster, tracks, file_metas)
        recaps.append({
            "titik": _format_date_ddmmyy(earliest) or "-",
            "periode": _format_date_period(earliest, latest),
            "uploader_name": uploader_name,
            "duration_s": total_duration if have_duration else None,
            "distance_m": total_distance if have_distance else None,
        })
    return recaps


def compute_estate_names(rows, work_dir=None):
    """Nama Estate unik yang tercakup di area penerbangan batch ini (kolom "Estate"
    di Aresta.shp) -- dipakai buat "Lokasi/Area Kerja" di Report (PPT)."""
    tracks = [t for t in reproject_track_multi(rows) if len(t) >= 2]
    if not tracks:
        return []
    all_pts = [p for t in tracks for p in t]
    extent = _compute_extent(all_pts, padding_pct=25.0)
    gdf = _load_aresta_gdf(extent=extent, work_dir=work_dir)
    if gdf is None or len(gdf) == 0 or "Estate" not in gdf.columns:
        return []
    return sorted({str(v).strip() for v in gdf["Estate"].dropna().unique() if str(v).strip()})


def compute_region_groups(rows, file_metas, work_dir=None):
    """Group complete flight tracks by the Aresta ``Region`` polygon.

    A track is assigned using its UTM centroid, with the nearest Region polygon
    as fallback when the centroid falls on a boundary or outside the layer.
    The returned rows and metadata retain their original ``track_idx`` values so
    dates, maps, and report tables remain consistent.
    """
    tracks = [t for t in reproject_track_multi(rows) if t]
    if not tracks:
        return {}
    all_pts = [p for t in tracks for p in t]
    extent = _compute_extent(all_pts, padding_pct=25.0)
    gdf = _load_aresta_gdf(extent=extent, work_dir=work_dir)
    if gdf is None or len(gdf) == 0:
        return {"Tanpa Region": (rows, file_metas)}
    region_column = _find_aresta_column(gdf, ("Region", "Region_New", "Wilayah"))
    if not region_column:
        return {"Tanpa Region": (rows, file_metas)}

    from shapely.geometry import Point

    groups = {}
    for track in tracks:
        track_idx = track[0]["track_idx"]
        cx = sum(p["x"] for p in track) / len(track)
        cy = sum(p["y"] for p in track) / len(track)
        point = Point(cx, cy)
        matches = gdf[gdf.geometry.contains(point)]
        if len(matches) == 0:
            distances = gdf.geometry.distance(point)
            matches = gdf.loc[[distances.idxmin()]] if len(distances) else gdf.iloc[0:0]
        region = str(matches.iloc[0].get(region_column) or "Tanpa Region").strip() or "Tanpa Region"
        group = groups.setdefault(region, {"track_idxs": set()})
        group["track_idxs"].add(track_idx)

    result = {}
    for region, group in groups.items():
        track_idxs = group["track_idxs"]
        result[region] = (
            [row for row in rows if row.get("track_idx") in track_idxs],
            {idx: meta for idx, meta in file_metas.items() if idx in track_idxs},
        )
    return result


def compute_flight_blocks(rows, file_metas, work_dir=None):
    """Untuk TIAP penerbangan (1 track = 1 file, BUKAN digabung per cluster seperti
    compute_estate_names), cari 1 blok Aresta yang paling mewakili lokasi
    penerbangan itu -- dipakai buat kolom ESTATE/BLOK di "Excel Monitoring"
    (export_excel_monitoring). Caranya: hitung centroid titik-titik track itu (UTM),
    cek blok mana yang MENGANDUNG titik itu (gdf.contains); kalau kebetulan
    centroid-nya jatuh di luar semua poligon (mis. track pendek/di pinggir blok),
    fallback ke blok TERDEKAT. Return {track_idx: {"estate": str|None,
    "blok": str|None}}."""
    tracks = [t for t in reproject_track_multi(rows) if len(t) >= 1]
    if not tracks:
        return {}
    all_pts = [p for t in tracks for p in t]
    extent = _compute_extent(all_pts, padding_pct=25.0) if len(all_pts) >= 2 else None
    gdf = _load_aresta_gdf(extent=extent, work_dir=work_dir)
    if gdf is None or len(gdf) == 0:
        return {}

    from shapely.geometry import Point
    region_column = _find_aresta_column(gdf, ("Region", "Region_New", "Wilayah"))
    estate_column = _find_aresta_column(gdf, ("Estate", "Nama_Estate"))
    block_column = _find_aresta_column(
        gdf, ("Kode_Blok", "Blok_SAP", "NO_BLOK", "NOBLOK", "Blok", "Blok_ID", "BLOCK_ID", "BLOCK")
    )

    result = {}
    for track in tracks:
        track_idx = track[0]["track_idx"]
        cx = sum(p["x"] for p in track) / len(track)
        cy = sum(p["y"] for p in track) / len(track)
        pt = Point(cx, cy)
        try:
            match = gdf[gdf.contains(pt)]
            if len(match) == 0:
                match = gdf.iloc[[gdf.distance(pt).idxmin()]]
        except Exception:
            match = gdf.iloc[0:0]

        if len(match) > 0:
            row = match.iloc[0]
            region = str(row.get(region_column) or "").strip() if region_column else None
            estate = str(row.get(estate_column) or "").strip() if estate_column else None
            blok = str(row.get(block_column) or "").strip() if block_column else None
            region = region or None
            estate = estate or None
            blok = blok or None
            result[track_idx] = {"region": region, "estate": estate, "blok": blok}
        else:
            result[track_idx] = {"region": None, "estate": None, "blok": None}
    return result


def compute_flight_period(file_metas):
    """(earliest, latest) datetime penerbangan SELURUH batch (bukan per cluster
    seperti _cluster_date_range) -- dipakai buat "Waktu Mulai - Selesai" di
    Report (PPT)."""
    if not file_metas:
        return None, None
    earliest = latest = None
    for meta, filename in file_metas.values():
        dt = _extract_flight_datetime(filename, meta.get("startTime"))
        if dt is None:
            continue
        dur = meta.get("durationSecs") or 0
        end_dt = dt + timedelta(seconds=dur)
        if earliest is None or dt < earliest:
            earliest = dt
        if latest is None or end_dt > latest:
            latest = end_dt
    return earliest, latest


def compute_flight_days_count(file_metas):
    """Jumlah HARI UNIK drone terbang di batch ini (bukan jumlah file/sesi) --
    dipakai buat kartu "Total Penerbangan" di Report (PPT). Misal terbang
    tanggal 14, 15, 17, 21 (dari beberapa file) -> hasilnya 4."""
    if not file_metas:
        return 0
    days = set()
    for meta, filename in file_metas.values():
        dt = _extract_flight_datetime(filename, meta.get("startTime"))
        if dt is not None:
            days.add(dt.date())
    return len(days)


def _render_map_base(rows, padding_pct, resolution, work_dir, file_metas):
    """Bagian peta yang SAMA dipakai baik oleh peta kop-lengkap
    (render_flight_map_png_multi, tombol "Peta (PNG)") maupun peta ringkas
    (render_flight_map_png_compact, khusus Report/PPT): basemap Aresta + jalur per
    file + marker Takeoff/Landing + 1 label tanggal per cluster + label nama
    Estate. Return (base_rgba, world_to_pixel, real_res, extent, gdf) supaya
    pemanggil bisa lanjut compose elemen tambahan (sidebar kop-peta ATAU legenda
    ringkas)."""
    tracks = [t for t in reproject_track_multi(rows) if len(t) >= 2]
    if not tracks:
        raise ValueError("Tidak cukup titik koordinat (dengan waktu) untuk membuat peta.")

    all_pts = [p for t in tracks for p in t]
    extent = _compute_extent(all_pts, padding_pct=padding_pct, resolution=resolution)
    gdf = _load_aresta_gdf(extent=extent, work_dir=work_dir)
    if gdf is not None and len(gdf) > 0 and gdf.crs and gdf.crs.to_string() != DEFAULT_DST_CRS:
        try:
            gdf = gdf.to_crs(DEFAULT_DST_CRS)
        except Exception:
            pass

    base_img, world_to_pixel, real_res = _render_basemap_image(gdf, extent, resolution=resolution)
    base_rgba = base_img.convert("RGBA")
    draw = ImageDraw.Draw(base_rgba)

    r = max(6, int(real_res[0] * 0.008))
    font = _load_font(max(14, int(real_res[0] * 0.02)))

    for track in tracks:
        trail_px = [world_to_pixel(p["x"], p["y"]) for p in track]
        draw.line(trail_px, fill=(255, 140, 0), width=4, joint="curve")

    # 1 titik (oranye) + 1 label per cluster track yang berdekatan (bukan per
    # track) -- labelnya tanggal penerbangan PALING AWAL di cluster itu, supaya
    # area yang banyak track-nya saling berdekatan tidak numpuk banyak
    # titik/label tumpang tindih. Label yang sama persis juga dipakai sebagai
    # kolom "Titik" di tabel rekap Report (PPT) -- lihat compute_cluster_recaps().
    for label, wx, wy in _cluster_label_positions(tracks, file_metas):
        px, py = world_to_pixel(wx, wy)
        draw.ellipse([px - r, py - r, px + r, py + r], fill=(255, 140, 0), outline=(255, 255, 255), width=2)
        draw.text((px + r + 4, py - r), label, font=font, fill=(20, 20, 20),
                   stroke_width=2, stroke_fill=(255, 255, 255))

    _draw_map_labels(draw, gdf, world_to_pixel, real_res, extent=extent)

    return base_rgba, world_to_pixel, real_res, extent, gdf


def _draw_compact_legend(draw, extent, real_res):
    """Legenda RINGKAS -- 1 kotak kecil semi-transparan pojok kiri-bawah gambar,
    menyatukan Jalur Terbang/Titik Penerbangan + 1 baris "Skala 1:X | N (arah utara)"
    jadi satu blok kompak. Dipakai KHUSUS oleh render_flight_map_png_compact()
    (panel peta di Report/PPT) -- beda dari _draw_legend_box (sidebar kop-peta
    penuh yang dipakai tombol "Peta (PNG)")."""
    pad = int(real_res[0] * 0.018)
    box_w = int(real_res[0] * 0.30)
    box_h = int(real_res[1] * 0.30)
    x0, y0 = pad, real_res[1] - pad - box_h
    x1, y1 = x0 + box_w, y0 + box_h

    draw.rectangle([x0, y0, x1, y1], fill=(255, 255, 255, 230), outline=(120, 120, 120), width=1)

    font = _load_font(max(11, int(real_res[0] * 0.015)))
    sw = int(real_res[0] * 0.022)
    items = [
        ("line", (255, 140, 0), "Jalur Terbang"),
        ("dot", (255, 140, 0), "Titik Penerbangan"),
    ]
    row_h = font.size + int(pad * 0.5)
    y = y0 + pad * 0.7
    for kind, color, label in items:
        cy = y + row_h / 2
        if kind == "line":
            draw.line([(x0 + pad, cy), (x0 + pad + sw, cy)], fill=color, width=4)
        else:
            rr = sw / 2.2
            draw.ellipse([x0 + pad + sw / 2 - rr, cy - rr, x0 + pad + sw / 2 + rr, cy + rr],
                         fill=color, outline=(255, 255, 255), width=1)
        draw.text((x0 + pad + sw + pad * 0.5, cy), label, font=font, fill=(30, 30, 30), anchor="lm")
        y += row_h

    xmin, _, xmax, _ = extent
    meters_per_px = (xmax - xmin) / real_res[0]
    ratio = _nice_scale_ratio(meters_per_px * 100 / 0.0254)
    scale_line = f"Skala 1:{ratio:,}".replace(",", ".") + "  |  N ↑"
    draw.text((x0 + pad, y + pad * 0.4), scale_line, font=font, fill=(30, 30, 30))


def render_flight_map_png_compact(rows, out_path, resolution=(1200, 630), work_dir=None, file_metas=None):
    """Peta RINGKAS khusus untuk ditempel di slide Report (PPT) -- konten sama
    dengan render_flight_map_png_multi (basemap+jalur+label tanggal+label Estate),
    TAPI tanpa sidebar kop-peta besar (judul "Flight Log" sudah ada di slide PPT
    sendiri, jadi kalau dobel jadi aneh) -- legendanya digabung jadi 1 kotak kecil
    menyatu di pojok kiri-bawah gambar (lihat _draw_compact_legend)."""
    base_rgba, _, real_res, extent, _ = _render_map_base(rows, 25.0, resolution, work_dir, file_metas)
    draw = ImageDraw.Draw(base_rgba)
    _draw_compact_legend(draw, extent, real_res)
    base_rgba.convert("RGB").save(out_path)


def render_flight_map_png_multi(rows, out_path, padding_pct=25.0, resolution=(1600, 1200), work_dir=None,
                                 file_metas=None, uploader_name=None, region_name=None):
    """Render kop peta gaya print-layout: panel peta (basemap Aresta + jalur per
    file + label Estate) di kiri, sidebar di kanan berisi kotak Judul ("Flight Log"
    + bulan-tahun, dari file_metas), Orientasi (panah N), Skala (angka + grafik),
    dan Legenda (jalur/Titik Penerbangan/blok Aresta). 1 titik oranye + label
    TANGGAL (format DD-MM-YY) per area/cluster track yang berdekatan (lihat
    _cluster_label_positions) -- bukan per takeoff/landing tiap track. Kotak
    keterangan per area (nama user/periode/durasi/jarak) ada di tabel rekap
    Report (PPT), lihat compute_cluster_recaps() -- bukan di peta ini."""
    base_rgba, world_to_pixel, real_res, extent, gdf = _render_map_base(
        rows, padding_pct, resolution, work_dir, file_metas)

    flight_dates = []
    if file_metas:
        for meta, filename in file_metas.values():
            dt = _extract_flight_datetime(filename, meta.get("startTime"))
            if dt:
                flight_dates.append(dt)
    month_year_label = _indonesian_month_year_label(flight_dates)

    map_w, map_h = real_res
    sidebar_w = int(map_w * 0.32)
    canvas_w, canvas_h = map_w + sidebar_w, map_h

    canvas = Image.new("RGB", (canvas_w, canvas_h), (255, 255, 255))
    canvas.paste(base_rgba.convert("RGB"), (0, 0))
    cdraw = ImageDraw.Draw(canvas)

    om = int(canvas_h * 0.012)
    gap = int(canvas_h * 0.01)
    sidebar_left = map_w + om
    sidebar_right = canvas_w - om
    content_top = om
    content_bottom = canvas_h - om
    content_h = content_bottom - content_top

    title_h = int(content_h * 0.19)
    orient_h = int(content_h * 0.15)
    scale_h = int(content_h * 0.13)

    y = content_top
    title_box = (sidebar_left, y, sidebar_right, y + title_h)
    y += title_h + gap
    orient_box = (sidebar_left, y, sidebar_right, y + orient_h)
    y += orient_h + gap
    scale_box = (sidebar_left, y, sidebar_right, y + scale_h)
    y += scale_h + gap
    legend_box = (sidebar_left, y, sidebar_right, content_bottom)

    _draw_title_box(cdraw, title_box, "Flight Log", region_name or "", canvas_h, month_year_label)
    _draw_north_arrow(cdraw, orient_box, canvas_h)
    _draw_scale_box(cdraw, scale_box, extent, canvas_h, real_res)
    _draw_legend_box(cdraw, legend_box, canvas_h)

    for b in (title_box, orient_box, scale_box, legend_box):
        cdraw.rectangle(b, outline=(0, 0, 0), width=2)
    cdraw.line([(map_w, 0), (map_w, canvas_h)], fill=(0, 0, 0), width=3)
    cdraw.rectangle([1, 1, canvas_w - 2, canvas_h - 2], outline=(0, 0, 0), width=3)

    canvas.save(out_path)


def build_track_geojson_multi(rows):
    """Sama seperti build_track_geojson, tapi rows dari banyak file (key "track_idx"
    per row) -- hasilkan N LineString + N pasang Point takeoff/landing (1 set per
    file), bukan cuma 1+1+1. Frontend Leaflet (drone_report_log_detail.html) tidak
    perlu tahu ini multi-file -- L.geoJSON() sudah generik render per-feature."""
    groups = {}
    order = []
    for r in rows:
        idx = r["track_idx"]
        if idx not in groups:
            groups[idx] = []
            order.append(idx)
        groups[idx].append(r)

    features = []
    for idx in order:
        pts = groups[idx]
        source = pts[0].get("source_file")
        coords = [[p["lon_deg"], p["lat_deg"]] for p in pts]
        first, last = pts[0], pts[-1]
        features.append({
            "type": "Feature",
            "properties": {"kind": "track", "track_idx": idx, "source_file": source},
            "geometry": {"type": "LineString", "coordinates": coords},
        })
        features.append({
            "type": "Feature",
            "properties": {"kind": "takeoff", "track_idx": idx, "source_file": source},
            "geometry": {"type": "Point", "coordinates": [first["lon_deg"], first["lat_deg"]]},
        })
        features.append({
            "type": "Feature",
            "properties": {"kind": "landing", "track_idx": idx, "source_file": source},
            "geometry": {"type": "Point", "coordinates": [last["lon_deg"], last["lat_deg"]]},
        })
    return {"type": "FeatureCollection", "features": features}


def build_aresta_geojson_multi(rows, work_dir=None):
    """Sama seperti build_aresta_geojson, tapi extent dihitung dari gabungan semua
    track (rows dari banyak file)."""
    all_pts = [p for t in reproject_track_multi(rows) for p in t]
    if len(all_pts) < 2:
        return {"type": "FeatureCollection", "features": []}
    extent = _compute_extent(all_pts, padding_pct=25.0)
    gdf = _load_aresta_gdf(extent=extent, work_dir=work_dir)
    if gdf is None or len(gdf) == 0:
        return {"type": "FeatureCollection", "features": []}
    if gdf.crs and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)
    return gdf.__geo_interface__
