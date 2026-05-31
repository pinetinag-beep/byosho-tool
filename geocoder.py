"""
病院名+都道府県名 → 緯度経度変換

優先順位:
  1. locations テーブル（厚労省 医療情報ネット 公式座標）
  2. geocache テーブル（Nominatim でジオコーディングした結果）
"""
import time
import duckdb


def _ensure_tables(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS geocache (
            hospital_name VARCHAR,
            pref          VARCHAR,
            lat           DOUBLE,
            lon           DOUBLE,
            found         BOOLEAN
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS locations (
            施設名        VARCHAR,
            医療機関コード VARCHAR,
            lat           DOUBLE,
            lon           DOUBLE,
            都道府県名    VARCHAR,
            住所          VARCHAR,
            data_source   VARCHAR,
            data_date     VARCHAR
        )
    """)


def _has_locations(con, pref: str) -> bool:
    """locations テーブルに該当都道府県のデータが存在するか。"""
    try:
        count = con.execute(
            "SELECT COUNT(*) FROM locations WHERE 都道府県名 = ?", [pref]
        ).fetchone()[0]
        return count > 0
    except Exception:
        return False


def load_cached_coords(db_path: str, pref: str) -> dict:
    """
    pref 内の座標を {hospital_name: (lat, lon)} で返す。
    locations テーブル（公式）→ geocache（Nominatim）の順で優先。
    """
    con = duckdb.connect(db_path)
    _ensure_tables(con)

    result: dict[str, tuple] = {}

    # 1. Nominatim キャッシュ
    rows = con.execute(
        "SELECT hospital_name, lat, lon FROM geocache WHERE pref = ? AND found = TRUE",
        [pref],
    ).fetchall()
    for r in rows:
        result[r[0]] = (r[1], r[2])

    # 2. 公式座標で上書き（より信頼度が高い）
    try:
        official = con.execute(
            "SELECT 施設名, lat, lon FROM locations WHERE 都道府県名 = ?",
            [pref],
        ).fetchall()
        for r in official:
            if r[0] and r[1] is not None and r[2] is not None:
                result[r[0]] = (r[1], r[2])
    except Exception:
        pass

    con.close()
    return result


def count_uncached(db_path: str, names: list[str], pref: str) -> int:
    """
    座標がまだない病院の数を返す。
    locations テーブルにあればカウント対象外。
    """
    if not names:
        return 0
    con = duckdb.connect(db_path)
    _ensure_tables(con)

    covered: set[str] = set()

    # geocache にあるもの
    for r in con.execute(
        "SELECT hospital_name FROM geocache WHERE pref = ?", [pref]
    ).fetchall():
        covered.add(r[0])

    # locations にあるもの
    try:
        for r in con.execute(
            "SELECT 施設名 FROM locations WHERE 都道府県名 = ?", [pref]
        ).fetchall():
            if r[0]:
                covered.add(r[0])
    except Exception:
        pass

    con.close()
    return sum(1 for n in names if n not in covered)


def has_official_locations(db_path: str) -> bool:
    """locations テーブルにデータが入っているか。"""
    try:
        con = duckdb.connect(db_path)
        _ensure_tables(con)
        count = con.execute("SELECT COUNT(*) FROM locations").fetchone()[0]
        con.close()
        return count > 0
    except Exception:
        return False


def geocode_batch(df, db_path: str, progress_cb=None):
    """
    df（医療機関名・都道府県名 列を含む）をジオコーディングし、
    lat / lon 列を付けて返す。locations/geocache 済みはスキップ。
    """
    from geopy.geocoders import Nominatim

    con = duckdb.connect(db_path)
    _ensure_tables(con)

    geolocator = Nominatim(user_agent="byosho-tool-geocoder/1.0")

    pairs = df[["医療機関名", "都道府県名"]].drop_duplicates().values.tolist()
    total = len(pairs)
    results: dict[tuple, tuple] = {}

    for i, (name, pref) in enumerate(pairs):
        # locations テーブル（公式）を先に確認
        try:
            official = con.execute(
                "SELECT lat, lon FROM locations WHERE 施設名 = ? AND 都道府県名 = ? LIMIT 1",
                [name, pref],
            ).fetchone()
            if official and official[0] is not None:
                results[(name, pref)] = (official[0], official[1])
                if progress_cb:
                    progress_cb(i + 1, total)
                continue
        except Exception:
            pass

        # geocache を確認
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
