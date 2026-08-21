@echo off
title Report Log Drone - Standalone
cd /d "%~dp0"

echo ============================================================
echo   Report Log Drone - Standalone
echo ============================================================
echo.

REM Cari QGIS Python secara otomatis (dibutuhkan untuk GDAL/geopandas)
set "QGIS_PY="
for /D %%D in ("C:\Program Files\QGIS*") do (
    if exist "%%D\bin\python-qgis-ltr.bat" set "QGIS_PY=%%D\bin\python-qgis-ltr.bat"
)
if not defined QGIS_PY (
    for /D %%D in ("C:\Program Files\QGIS*") do (
        if exist "%%D\bin\python-qgis.bat" set "QGIS_PY=%%D\bin\python-qgis.bat"
    )
)
if not defined QGIS_PY (
    echo [ERROR] QGIS Python tidak ditemukan di C:\Program Files\QGIS*
    echo [ERROR] Install QGIS versi berapapun dulu ^(dipakai hanya untuk GDAL/geopandas^).
    pause
    exit /b 1
)

echo [INFO] QGIS Python: %QGIS_PY%
echo.

REM -- Wajib diisi sesuai database yang mau dipakai --------------------------
REM Kalau punya database Postgres sendiri, isi di sini. Kalau mau memakai
REM database yang SAMA dengan dashboard Agripalm Vision utama, isi persis
REM sama seperti punya server itu (tanya admin dashboard utama untuk nilainya).
set AGRIPALM_DB_HOST=localhost
set AGRIPALM_DB_USER=postgres
set AGRIPALM_DB_PASS=AgripalmLocal2026
set AGRIPALM_DB_NAME=gpnserver

REM Lokasi file Aresta.shp (dipakai untuk isi kolom Estate/Blok otomatis).
REM Kosongkan/abaikan kalau tidak punya file ini -- Estate/Blok akan kosong,
REM fitur lain tetap jalan normal.
set AGRIPALM_ARESTA_PATH=C:\Program Files\Agripalm Vision\geoprocessing\nutripalm\Sgpar\Aresta.shp

REM Bypass login untuk akses dari laptop ini sendiri (127.0.0.1) -- supaya
REM langsung bisa dipakai tanpa perlu setup tabel `users`/LDAP dulu. Hapus
REM baris ini (atau set ke 0) kalau mau login sungguhan diaktifkan.
set AGRIPALM_LOCAL_NO_LOGIN=1

set AGRIPALM_PORT=8001

echo [INFO] Menjalankan server di http://127.0.0.1:%AGRIPALM_PORT%/dashboard/drone-report
echo [INFO] JANGAN TUTUP jendela ini selama dipakai.
echo.

"%QGIS_PY%" api_server.py

echo.
echo [INFO] Server berhenti. Tekan tombol apa saja untuk menutup.
pause >nul
