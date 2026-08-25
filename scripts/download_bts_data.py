"""
00_download_bts_data.py
-----------------------
Downloads BTS Airline On-Time Performance pre-zipped CSV files for a given
year range and extracts them into data/raw/bts_ontime/.

Usage:
    python scripts/data_prep/00_download_bts_data.py

Requirements:
    pip install requests tqdm

What it downloads:
    - All 12 months of the selected year from the BTS PREZIP server
    - Full dataset (all carriers, all airports) — filtering to AS/SEA
      happens in the next script (01_parse_bts.py)

Each zip is ~15-25 MB; unzipped CSV is ~150-250 MB per month.
Full year ≈ 2-3 GB unzipped. Keep raw zips for re-processing.
"""

import os
import sys
import zipfile
import requests
from pathlib import Path

try:
    from tqdm import tqdm
    USE_TQDM = True
except ImportError:
    USE_TQDM = False
    print("Tip: install tqdm for progress bars  →  pip install tqdm")

# ── Configuration ──────────────────────────────────────────────────────────────

YEAR = 2023           # Change to download a different year
MONTHS = range(1, 13) # 1–12 for full year; use range(1,4) for just Q1, etc.

# Path relative to this script's location (works from any cwd)
SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent.parent
OUTPUT_DIR = PROJECT_ROOT / "data" / "raw" / "bts_ontime"

BASE_URL = (
    "https://transtats.bts.gov/PREZIP/"
    "On_Time_Reporting_Carrier_On_Time_Performance_1987_present_{year}_{month}.zip"
)

# ── Helpers ────────────────────────────────────────────────────────────────────

def download_file(url: str, dest: Path) -> bool:
    """Download url to dest, showing progress. Returns True on success."""
    try:
        resp = requests.get(url, stream=True, timeout=120)
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))

        if USE_TQDM:
            bar = tqdm(total=total, unit="B", unit_scale=True,
                       desc=dest.name, leave=False)

        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
                if USE_TQDM:
                    bar.update(len(chunk))

        if USE_TQDM:
            bar.close()
        return True

    except requests.HTTPError as e:
        print(f"  ✗ HTTP error: {e}")
        return False
    except requests.ConnectionError:
        print("  ✗ Connection error — check your internet connection.")
        return False


def extract_zip(zip_path: Path, extract_to: Path) -> None:
    """Extract a zip file and remove the archive afterward."""
    with zipfile.ZipFile(zip_path, "r") as z:
        names = z.namelist()
        print(f"  → Extracting {len(names)} file(s)...")
        z.extractall(extract_to)
    zip_path.unlink()  # remove zip after extracting


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\nBTS On-Time Performance Download")
    print(f"Year: {YEAR}  |  Months: {list(MONTHS)}")
    print(f"Output: {OUTPUT_DIR}\n")

    success, failed = [], []

    for month in MONTHS:
        url = BASE_URL.format(year=YEAR, month=month)
        zip_name = f"bts_ontime_{YEAR}_{month:02d}.zip"
        zip_path = OUTPUT_DIR / zip_name

        # Check if already extracted
        csv_pattern = list(OUTPUT_DIR.glob(f"*{YEAR}_{month}*.csv"))
        if csv_pattern:
            print(f"  [{month:02d}/{YEAR}] Already extracted — skipping.")
            success.append(month)
            continue

        print(f"  [{month:02d}/{YEAR}] Downloading...")
        ok = download_file(url, zip_path)

        if ok and zip_path.exists():
            extract_zip(zip_path, OUTPUT_DIR)
            success.append(month)
            print(f"  [{month:02d}/{YEAR}] ✓ Done")
        else:
            failed.append(month)

    print(f"\n{'─'*50}")
    print(f"Downloaded & extracted: {len(success)}/{len(list(MONTHS))} months")
    if failed:
        print(f"Failed months: {failed}")
        sys.exit(1)
    else:
        print("All files ready. Next step: run 01_parse_bts.py")


if __name__ == "__main__":
    main()
