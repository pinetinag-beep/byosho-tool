#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
byosho DuckDB build script
==========================
Usage:
    python build_db.py                   # all years (2021-2023)
    python build_db.py --years 2023      # latest year only
    python build_db.py --years 2022 2023 # specific years
"""
import sys
import argparse
import datetime
import urllib.request
from pathlib import Path

# force UTF-8 output on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ── URL map ───────────────────────────────────────────────────────────────────
BASE = "https://www.mhlw.go.jp/content/10800000/"

YEAR_URLS = {
    2023: {  # Reiwa 5
        "byosho": [
            ("Hokkaido-Tohoku",   f"{BASE}001299892.xlsx"),
            ("Kanto-A",           f"{BASE}001299893.xlsx"),
            ("Kanto-B",           f"{BASE}001299894.xlsx"),
            ("Chubu",             f"{BASE}001299895.xlsx"),
            ("Kinki",             f"{BASE}001299901.xlsx"),
            ("Chugoku-Shikoku",   f"{BASE}001299914.xlsx"),
            ("Kyushu-Okinawa",    f"{BASE}001299921.xlsx"),
        ],
        "shisetsu": f"{BASE}001299890.xlsx",
        "yoshiki2": f"{BASE}001299957.xlsx",
    },
    2022: {  # Reiwa 4
        "byosho": [
            ("Hokkaido-Tohoku",   f"{BASE}001151960.xlsx"),
            ("Kanto-A",           f"{BASE}001151962.xlsx"),
            ("Kanto-B",           f"{BASE}001151965.xlsx"),
            ("Chubu",             f"{BASE}001151966.xlsx"),
            ("Kinki",             f"{BASE}001151968.xlsx"),
            ("Chugoku",           f"{BASE}001151969.xlsx"),
            ("Shikoku",           f"{BASE}001151970.xlsx"),
            ("Kyushu-Okinawa",    f"{BASE}001151971.xlsx"),
        ],
        "shisetsu": f"{BASE}001151957.xlsx",
        "yoshiki2": f"{BASE}001151972.xlsx",
    },
    2021: {  # Reiwa 3
        "byosho": [
            ("Hokkaido-Tohoku",   f"{BASE}000953876.xlsx"),
            ("Kanto-A",           f"{BASE}000953878.xlsx"),
            ("Kanto-B",           f"{BASE}000953879.xlsx"),
            ("Chubu",             f"{BASE}000953880.xlsx"),
            ("Kinki",             f"{BASE}000953881.xlsx"),
            ("Chugoku-Shikoku",   f"{BASE}000953882.xlsx"),
            ("Kyushu-Okinawa",    f"{BASE}000953883.xlsx"),
        ],
        "shisetsu": f"{BASE}000953853.xlsx",
        "yoshiki2": f"{BASE}000953855.xlsx",
    },
}

DEFAULT_YEARS = [2021, 2022, 2023]
DB_PATH = Path(__file__).parent / "data" / "byosho.duckdb"


# ── download helper ──────────────────────────────────────────────────────────

def _download(url: str, label: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        print(f"    OK  {label}  ({len(data)//1024:,} KB)")
        return data
    except Exception as e:
        print(f"    NG  {label}: {e}")
        raise


# ── process one year ─────────────────────────────────────────────────────────

def _build_year(year: int, urls: dict):
    import pandas as pd
    from data_processor import (
        load_multiple_mhlw_extended,
        load_mhlw_shisetsu, merge_shisetsu,
        load_mhlw_yoshiki2,
    )

    print(f"  [byosho]")
    files_bytes = []
    for label, url in urls["byosho"]:
        data = _download(url, label)
        files_bytes.append((label, data))

    hosp_df, ward_df = load_multiple_mhlw_extended(files_bytes, year=year)
    print(f"    -> hospitals:{len(hosp_df):,}  wards:{len(ward_df):,}")

    if "shisetsu" in urls:
        print(f"  [shisetsu]")
        try:
            shi = _download(urls["shisetsu"], "shisetsu")
            shisetsu_df = load_mhlw_shisetsu(shi)
            hosp_df = merge_shisetsu(hosp_df, shisetsu_df)
            print(f"    -> merged OK")
        except Exception as e:
            print(f"    -> shisetsu skip: {e}")

    surg_df = None
    if "yoshiki2" in urls:
        print(f"  [yoshiki2 (surgery)]")
        try:
            y2 = _download(urls["yoshiki2"], "yoshiki2")
            surg_df = load_mhlw_yoshiki2(y2, year=year)
            print(f"    -> surgery:{len(surg_df):,}")
        except Exception as e:
            print(f"    -> yoshiki2 skip: {e}")

    return hosp_df, ward_df, surg_df


# ── write DuckDB ─────────────────────────────────────────────────────────────

def _write_db(db_path: Path, hosp_df, ward_df, surg_df, years: list):
    import duckdb
    import pandas as pd

    db_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"\n  Writing DuckDB: {db_path}")

    con = duckdb.connect(str(db_path))
    for tbl in ["hospitals", "wards", "surgery", "meta"]:
        con.execute(f"DROP TABLE IF EXISTS {tbl}")

    con.register("_hosp", hosp_df)
    con.execute("CREATE TABLE hospitals AS SELECT * FROM _hosp")

    con.register("_ward", ward_df)
    con.execute("CREATE TABLE wards AS SELECT * FROM _ward")

    if surg_df is not None and not surg_df.empty:
        con.register("_surg", surg_df)
        con.execute("CREATE TABLE surgery AS SELECT * FROM _surg")
    else:
        con.execute("CREATE TABLE surgery (dummy VARCHAR)")

    con.execute("""
        CREATE TABLE meta (
            updated_at   VARCHAR,
            years        VARCHAR,
            hospital_cnt INTEGER,
            ward_cnt     INTEGER
        )
    """)
    con.execute("INSERT INTO meta VALUES (?, ?, ?, ?)", [
        datetime.datetime.now().strftime("%Y/%m/%d %H:%M"),
        ", ".join(str(y) for y in sorted(years)),
        len(hosp_df),
        len(ward_df),
    ])
    con.close()
    print(f"  Done: hospitals={len(hosp_df):,}  wards={len(ward_df):,}")


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", nargs="+", type=int, default=DEFAULT_YEARS)
    parser.add_argument("--db", default=str(DB_PATH))
    args = parser.parse_args()

    years = sorted(args.years)
    db_path = Path(args.db)

    print("=" * 60)
    print("byosho DuckDB build")
    print(f"years : {years}")
    print(f"output: {db_path}")
    print("=" * 60)

    import pandas as pd
    all_hosp, all_ward, all_surg = [], [], []

    for year in years:
        if year not in YEAR_URLS:
            print(f"\nWARN: {year} not in YEAR_URLS (skipped)")
            continue
        print(f"\n--- {year} ---")
        try:
            h, w, s = _build_year(year, YEAR_URLS[year])
            all_hosp.append(h)
            all_ward.append(w)
            if s is not None:
                all_surg.append(s)
        except Exception as e:
            print(f"  ERROR year={year}: {e}")
            print("  Skipping this year and continuing...")

    if not all_hosp:
        print("ERROR: no data collected")
        sys.exit(1)

    # 重複列名を除去してから concat（年度差異による列名不整合を防ぐ）
    def _dedup(df):
        return df.loc[:, ~df.columns.duplicated()]

    hosp_df = pd.concat([_dedup(d) for d in all_hosp], ignore_index=True)
    ward_df = pd.concat([_dedup(d) for d in all_ward], ignore_index=True)
    surg_df = pd.concat([_dedup(d) for d in all_surg], ignore_index=True) if all_surg else None

    _write_db(db_path, hosp_df, ward_df, surg_df, years)

    print("\n" + "=" * 60)
    print("  BUILD COMPLETE!")
    print("  Run app.py to start the dashboard.")
    print("=" * 60)


if __name__ == "__main__":
    main()
