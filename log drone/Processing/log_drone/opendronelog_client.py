"""
Otomasi browser (Playwright) ke app.opendronelog.com untuk memproses 1 file flight
log DJI (.txt). Dipakai lewat browser asli (bukan HTTP client custom) karena server
dashboard ini jalan di jaringan kantor (Fortinet SSL-inspection) -- Chromium (via
Playwright) otomatis percaya sertifikat Fortinet lewat Windows certificate store,
beda dari client HTTP/binary custom (terbukti gagal TLS handshake terhadap sertifikat
itu saat dicoba langsung, lihat catatan implementasi).

Alur per file (selector dikonfirmasi lewat eksplorasi manual, screenshot terlampir di
catatan implementasi -- kalau situs opendronelog mengubah UI-nya, di sinilah yang perlu
disesuaikan):
  1. Buka https://app.opendronelog.com/
  2. Upload file .txt lewat <input type="file">
  3. Tunggu tombol "Export" muncul (tanda parsing selesai)
  4. Klik "Export" -> klik "Export All Data (JSON)", tangkap file download
  5. Return path JSON yang didownload (berisi flight/telemetry/track lengkap)

Timeout keras per file supaya 1 file bermasalah tidak macetkan worker selamanya
(pola sama seperti GDAL_SUBPROCESS_TIMEOUT_SECONDS di batch_worker.py).
"""
import os

# PENTING: service dashboard ini jalan lewat nssm sebagai akun "Local System", yang
# profile foldernya beda dari akun yang dipakai buat "playwright install chromium"
# (mis. admin.infra) -- coba di-atasi lewat env var PLAYWRIGHT_BROWSERS_PATH di
# install_service.bat/nssm AppEnvironmentExtra, tapi TERBUKTI tidak selalu sampai ke
# proses service (lihat catatan implementasi -- error "Executable doesn't exist" di
# path systemprofile tetap muncul walau env var sudah di-set di nssm). Supaya tidak
# bergantung ke nssm sama sekali, folder browser di-set LANGSUNG di sini, di kode
# Python, SEBELUM playwright diimport -- satu-satunya sumber kebenaran, tidak peduli
# service jalan sebagai akun apa. Foldernya tetap sama: <root dashboard>/ms-playwright.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_DASHBOARD_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.join(_DASHBOARD_ROOT, "ms-playwright"))

from playwright.sync_api import sync_playwright  # noqa: E402
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError  # noqa: E402

OPENDRONELOG_URL = "https://app.opendronelog.com/"
NAV_TIMEOUT_MS = 30000
PROCESS_TIMEOUT_MS = int(os.environ.get("AGRIPALM_DRONE_PROCESS_TIMEOUT_MS", "120000"))
DOWNLOAD_TIMEOUT_MS = 20000


class DroneLogProcessError(Exception):
    pass


def process_flight_log(txt_path, out_json_path):
    """Upload txt_path ke app.opendronelog.com, download 'Export All Data (JSON)',
    simpan ke out_json_path. Raise DroneLogProcessError kalau gagal di tahap manapun
    (pesan error dibuat sejelas mungkin supaya gampang didiagnosa dari tabel hasil)."""
    if not os.path.isfile(txt_path):
        raise DroneLogProcessError(f"File tidak ditemukan: {txt_path}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(accept_downloads=True)

            try:
                page.goto(OPENDRONELOG_URL, timeout=NAV_TIMEOUT_MS, wait_until="load")
            except PlaywrightTimeoutError:
                raise DroneLogProcessError(
                    "Timeout membuka app.opendronelog.com (cek koneksi internet server)."
                )

            file_input = page.locator('input[type="file"]')
            if file_input.count() == 0:
                raise DroneLogProcessError(
                    "Elemen upload file tidak ditemukan di halaman (UI opendronelog mungkin berubah)."
                )
            file_input.first.set_input_files(txt_path)

            try:
                page.wait_for_selector('button:has-text("Export")', timeout=PROCESS_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                error_text = _extract_visible_error(page)
                if error_text:
                    raise DroneLogProcessError(f"opendronelog gagal memproses file: {error_text}")
                raise DroneLogProcessError(
                    "Timeout menunggu hasil parsing (file besar / situs lambat / format tidak dikenali)."
                )

            page.wait_for_timeout(300)
            page.get_by_role("button", name="Export", exact=False).click()
            page.wait_for_timeout(200)

            try:
                with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
                    page.get_by_text("Export All Data (JSON)").click()
                download = dl_info.value
            except PlaywrightTimeoutError:
                raise DroneLogProcessError("Timeout menunggu download JSON dari opendronelog.")

            download.save_as(out_json_path)
            return out_json_path
        finally:
            browser.close()


def _extract_visible_error(page):
    """Best-effort: tangkap pesan error yang mungkin ditampilkan situs (elemen umum
    role=alert). Return None kalau tidak ketemu -- bukan error fatal, cuma detail
    tambahan untuk pesan error yang lebih jelas."""
    try:
        alert = page.locator('[role="alert"]')
        if alert.count() > 0:
            return alert.first.inner_text(timeout=1000).strip()
    except Exception:
        pass
    return None
