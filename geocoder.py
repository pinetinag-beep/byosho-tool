"""
病院名+都道府県名 → 緯度経度変換（Nominatim）＋ DuckDB キャッシュ
"""
import time
import duckdb


def _ensure_geocache(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS geocache (
            hospital_name VARCHAR,
            pref          VARCHAR,
            lat           DOUBLE,
            lon           DOUBLE,
            found         BOOLEAN
        )
    """)


def load_cached_coords(db_path: str, pref: str) -> dict:
    """pref 内のキャッシュ済み座標を {hospital_name: (lat, lon)} で返す。"""
    con = duckdb.connect(db_path)
    _ensure_geocache(con)
    rows = con.execute(
        "SELECT hospital_name, lat, lon FROM geocache WHERE pref = ? AND found = TRUE",
        [pref],
    ).fetchall()
    con.close()
    return {r[0]: (r[1], r[2]) for r in rows}


def count_uncached(db_path: str, names: list[str], pref: str) -> int:
    """キャッシュされていない病院名の数を返す。"""
    if not names:
        return 0
    con = duckdb.connect(db_path)
    _ensure_geocache(con)
    cached = {
        r[0]
        for r in con.execute(
            "SELECT hospital_name FROM geocache WHERE pref = ?", [pref]
        ).fetchall()
    }
    con.close()
    return sum(1 for n in names if n not in cached)


def geocode_batch(df, db_path: str, progress_cb=None):
    """
    df（医療機関名・都道府県名 列を含む）をジオコーディングし、
    lat / lon 列を付けて返す。キャッシュ済みの病院はスキップ。
    """
    from geopy.geocoders import Nominatim
    import pandas as pd

    con = duckdb.connect(db_path)
    _ensure_geocache(con)

    geolocator = Nominatim(user_agent="byosho-tool-geocoder/1.0")

    pairs = df[["医療機関名", "都道府県名"]].drop_duplicates().values.tolist()
    total = len(pairs)
    results: dict[tuple, tuple] = {}

    for i, (name, pref) in enumerate(pairs):
        cached = con.execute(
            "SELECT lat, lon, found FROM geocache WHERE hospital_name = ? AND pref = ?",
            [name, pref],
        ).fetchone()

        if cached is not None:
            results[(name, pref)] = (cached[0], cached[1]) if cached[2] else (None, None)
        else:
            query = f"{pref} {name}"
            try:
                loc = geolocator.geocode(query, timeout=10)
                if loc:
                    lat, lon, found = loc.latitude, loc.longitude, True
                else:
                    lat, lon, found = None, None, False
            except Exception:
                lat, lon, found = None, None, False

            existing = con.execute(
                "SELECT 1 FROM geocache WHERE hospital_name = ? AND pref = ?",
                [name, pref],
            ).fetchone()
            if existing is None:
                con.execute(
                    "INSERT INTO geocache VALUES (?, ?, ?, ?, ?)",
                    [name, pref, lat, lon, found],
                )

            results[(name, pref)] = (lat, lon) if found else (None, None)
            time.sleep(1.1)

        if progress_cb:
            progress_cb(i + 1, total)

    con.close()

    df = df.copy()
    df["lat"] = df.apply(
        lambda r: results.get((r["医療機関名"], r["都道府県名"]), (None, None))[0], axis=1
    )
    df["lon"] = df.apply(
        lambda r: results.get((r["医療機関名"], r["都道府県名"]), (None, None))[1], axis=1
    )
    return df
