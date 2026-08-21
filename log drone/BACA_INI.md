# Report Log Drone — Standalone

Paket ini isinya HANYA fitur "Report Log Drone" (upload flight log DJI .txt →
GPX/Excel/SHP/Peta PNG/Report PPT), disalin dari dashboard Agripalm Vision
utama supaya bisa dipakai sendiri di laptop lain tanpa membawa seluruh
dashboard (Deteksi TM, Custom, TBM, dst).

## Yang dibawa apa adanya (salinan langsung, logic tidak diubah)
- `Processing/log_drone/drone_report_worker.py`, `drone_export.py`, `drone_map.py`, `opendronelog_client.py`
- `templates/drone_report_log.html`, `templates/drone_report_log_detail.html`
- Semua route, skema tabel DB (`drone_report_uploads`, `drone_report_files`), dan logic worker

## Yang ditulis ulang (perlu, supaya bisa jalan sendiri)
- `api_server.py` — Flask app baru, ringkas (login + 1 menu), bukan copy dari dashboard utama yang isinya banyak menu lain
- `batch_worker.py` — versi RINGKAS, cuma berisi koneksi DB + lokasi Aresta.shp (lihat docstring di file itu). Versi asli butuh instalasi penuh "Agripalm Vision" (torch/sahi/model TM) yang di luar cakupan fitur ini
- `templates/base.html` — sidebar dipangkas jadi cuma 1 menu

## Cara menjalankan
1. Pastikan **QGIS** terinstall di laptop tujuan (dipakai untuk GDAL/geopandas, sama seperti dashboard utama).
2. Install dependency Python lewat Python bawaan QGIS:
   ```
   "C:\Program Files\QGIS <versi>\bin\python-qgis-ltr.bat" -m pip install -r requirements.txt
   "C:\Program Files\QGIS <versi>\bin\python-qgis-ltr.bat" -m playwright install chromium
   ```
3. Edit `JALANKAN_SERVER.bat`, isi bagian database (`AGRIPALM_DB_HOST/USER/PASS/NAME`) — lihat pilihan di bawah.
4. Copy `Aresta.shp` (+file pendukungnya .shx/.dbf/.prj) ke laptop itu, sesuaikan `AGRIPALM_ARESTA_PATH` di bat file. Kalau tidak ada, fitur tetap jalan, cuma kolom Estate/Blok akan kosong.
5. Jalankan `JALANKAN_SERVER.bat`, buka `http://127.0.0.1:8001/dashboard/drone-report`.

Login otomatis ter-bypass untuk akses dari laptop itu sendiri (`AGRIPALM_LOCAL_NO_LOGIN=1`, sudah diset default di bat file) — tidak perlu setup user/password dulu.

## PENTING — soal database, WAJIB dibaca sebelum dikirim ke orang lain

`AGRIPALM_DB_PASS` di `JALANKAN_SERVER.bat` sengaja saya KOSONGKAN (beda dari
dashboard utama yang credential-nya ter-hardcode ke database produksi). Ada 2 pilihan:

- **Database sendiri (disarankan kalau dikirim ke luar tim/laptop tidak terpercaya)**:
  install Postgres di laptop itu, database kosong baru — tabel akan otomatis
  dibuat sendiri saat pertama kali dijalankan. Data & upload di laptop itu
  TIDAK akan tercampur dengan dashboard utama.
- **Pakai database yang SAMA dengan dashboard utama** (kalau laptop itu di
  jaringan yang sama/boleh akses DB produksi): isi `AGRIPALM_DB_HOST/USER/PASS/NAME`
  persis sama dengan yang dipakai `JALANKAN_SERVER.bat` di dashboard utama.
  Konsekuensinya: upload dari laptop ini akan MUNCUL juga di menu Report Log
  Drone dashboard utama (dan sebaliknya), karena datanya benar-benar satu tabel
  yang sama — bukan salinan terpisah.

Jangan copy-paste `AGRIPALM_DB_PASS` dashboard utama ke sini kalau laptop/orang
penerimanya tidak seharusnya punya akses ke database produksi.

## Batasan yang perlu diketahui
- Kolom Estate/Blok di Excel Monitoring & label peta butuh file `Aresta.shp` — tanpa itu, tetap jalan tapi kolom itu kosong.
- Upload tetap diproses lewat situs pihak ketiga `app.opendronelog.com` (butuh koneksi internet, sama seperti dashboard utama) via Playwright.
- Kalau laptop ini nantinya mau digabung lagi jadi 2-server-1-database dengan dashboard utama (peer-redirect), isi `AGRIPALM_PEER_URL` — tapi untuk pemakaian standalone biasa, biarkan kosong.
