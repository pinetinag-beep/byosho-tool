"""薬局を探す（/yakkyoku/…）。各地方厚生局「届出受理医療機関名簿（薬局）」を加工したもの。

データは build_db._build_yakkyoku() が作る y_* のテーブル。医科のページ（app.py）とは別のテーブル・URLにしている
（医療機関番号の体系が医科と別で、届出の種類も別のため）。
"""
from __future__ import annotations

import math
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from .yakkyoku_kijun import DESC, GROUPS, OTHER, group_of, order_key

router = APIRouter()
PAGE_SIZE = 50


def y_url(fid: str) -> str:
    return f"/yakkyoku/{fid}"


def y_pref_url(pref_name: str) -> str:
    return f"/yakkyoku/pref/{quote(pref_name, safe='')}"


def y_city_url(pref_name: str, city: str) -> str:
    return f"/yakkyoku/city/{quote(pref_name, safe='')}/{quote(city, safe='')}"


def y_kijun_url(name: str) -> str:
    return f"/yakkyoku/kijun/{quote(name, safe='')}"


def _a():
    from . import app as A   # app.py がこのモジュールを読み込むので、使う時に読む
    return A


def _kijun_groups(con) -> list[dict]:
    """届出をまとまりごとに（フォームとトップの一覧用）。"""
    rows = con.execute("SELECT id, name, symbol, n_facilities FROM y_kijun").fetchall()
    out = {k: {"key": k, "label": lab, "items": []} for k, lab, _ in GROUPS}
    out[OTHER[0]] = {"key": OTHER[0], "label": OTHER[1], "items": []}
    for r in sorted(rows, key=lambda r: order_key(r["name"])):
        out[group_of(r["name"])]["items"].append({**dict(r), "desc": DESC.get(r["name"], "")})
    return [g for g in out.values() if g["items"]]


def _y_prefs(con):
    return con.execute("SELECT * FROM y_prefs ORDER BY code").fetchall()


class YCond:
    def __init__(self, request: Request):
        qp = request.query_params
        self.pref = qp.get("pref", "").strip()
        self.city = qp.get("city", "").strip() if self.pref else ""
        self.q = qp.get("q", "").strip()
        self.items = list(dict.fromkeys(int(x) for v in qp.getlist("item") for x in v.split(".") if x.isdigit()))
        self.mode = "and" if qp.get("mode") == "and" else "or"

    def where(self, search_key) -> tuple[str, list]:
        w, p = [], []
        if self.pref:
            w.append("f.pref_code = ?")
            p.append(self.pref)
        if self.city:
            w.append("f.city = ?")
            p.append(self.city)
        if self.q:
            k = f"%{search_key(self.q)}%"
            w.append("(f.name_search LIKE ? OR f.address_search LIKE ?)")
            p += [k, k]
        if self.items:
            q = ",".join("?" * len(self.items))
            if self.mode == "and" and len(self.items) > 1:
                w.append(f"""f.id IN (SELECT facility_id FROM y_filings WHERE kijun_id IN ({q})
                             GROUP BY facility_id HAVING COUNT(DISTINCT kijun_id) = {len(self.items)})""")
            else:
                w.append(f"f.id IN (SELECT facility_id FROM y_filings WHERE kijun_id IN ({q}))")
            p += self.items
        return (" WHERE " + " AND ".join(w)) if w else "", p

    def query(self, **extra) -> str:
        from urllib.parse import urlencode
        pairs = [("pref", self.pref), ("city", self.city), ("q", self.q)] + [("item", i) for i in self.items]
        if len(self.items) > 1 and self.mode == "and":
            pairs.append(("mode", "and"))
        pairs += list(extra.items())
        return urlencode([(k, v) for k, v in pairs if v not in ("", None)])


def _hits(con, fids: list[str], items: list[int]) -> dict[str, list[str]]:
    if not fids or not items:
        return {}
    out: dict[str, list[str]] = {}
    for fid, name in con.execute(
            f"""SELECT fl.facility_id, k.name FROM y_filings fl JOIN y_kijun k ON k.id = fl.kijun_id
                WHERE fl.facility_id IN ({','.join('?' * len(fids))}) AND fl.kijun_id IN ({','.join('?' * len(items))})""",
            fids + items):
        out.setdefault(fid, []).append(name)
    return out


@router.get("/yakkyoku", response_class=HTMLResponse)
def y_index(request: Request):
    A = _a()
    c = YCond(request)   # 検索結果の「条件を変更する」から戻った時は条件を入れた状態で出す
    with A.db() as con:
        groups = _kijun_groups(con)
        prefs = _y_prefs(con)
        n = con.execute("SELECT COUNT(*) FROM y_fac").fetchone()[0]
        as_of = con.execute("SELECT MIN(as_of_label), MAX(as_of_label) FROM y_prefs").fetchone()
    return A.render(request, "y_index.html", groups=groups, prefs=prefs, n_total=n, c=c, as_of=as_of)


@router.get("/yakkyoku/search", response_class=HTMLResponse)
def y_search(request: Request, page: int = Query(1, ge=1)):
    A = _a()
    c = YCond(request)
    with A.db() as con:
        where, params = c.where(A.search_key)
        total = con.execute(f"SELECT COUNT(*) FROM y_fac f{where}", params).fetchone()[0]
        pages = max(1, math.ceil(total / PAGE_SIZE))
        page = min(page, pages)
        rows = con.execute(
            f"""SELECT f.* FROM y_fac f{where} ORDER BY f.pref_code, f.city, f.name LIMIT ? OFFSET ?""",
            params + [PAGE_SIZE, (page - 1) * PAGE_SIZE]).fetchall()
        hits = _hits(con, [r["id"] for r in rows], c.items)
        sel = [dict(r) for r in con.execute(
            f"SELECT id, name, symbol FROM y_kijun WHERE id IN ({','.join('?' * len(c.items))})", c.items)] if c.items else []
        pref = con.execute("SELECT * FROM y_prefs WHERE code = ?", (c.pref,)).fetchone() if c.pref else None
        groups = _kijun_groups(con)
        prefs = _y_prefs(con)
        cities = con.execute(
            "SELECT city, SUM(n) AS n FROM y_areas WHERE pref_code = ? GROUP BY city ORDER BY MIN(city_order)",
            (c.pref,)).fetchall() if c.pref else []
    return A.render(request, "y_search.html", c=c, rows=rows, hits=hits, sel=sel, total=total, page=page,
                    pages=pages, pref=pref, groups=groups, prefs=prefs, cities=cities,
                    page_url=lambda p: "/yakkyoku/search?" + c.query(page=p if p > 1 else ""),
                    csv_url="/yakkyoku/search.csv?" + c.query())


@router.get("/yakkyoku/search.csv")
def y_search_csv(request: Request):
    """薬局の検索結果のCSV。薬局を1行ずつ、選んだ届出ごとに届け出ていれば「○」の列を付ける。"""
    A = _a()
    c = YCond(request)
    con = A.sqlite3.connect(f"file:{A.DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = A.sqlite3.Row
    where, params = c.where(A.search_key)
    sel = con.execute(f"SELECT id, name FROM y_kijun WHERE id IN ({','.join('?' * len(c.items))})",
                      c.items).fetchall() if c.items else []
    rows = con.execute(f"SELECT f.* FROM y_fac f{where} ORDER BY f.pref_code, f.city, f.name", params).fetchall()
    have: dict[str, set[int]] = {}
    if sel:
        ids = [s["id"] for s in sel]
        for fid, kid in con.execute(f"SELECT facility_id, kijun_id FROM y_filings WHERE kijun_id IN ({','.join('?' * len(ids))})", ids):
            have.setdefault(fid, set()).add(kid)
    con.close()
    header = ["都道府県", "市区町村", "二次医療圏", "薬局名", "所在地", "医療機関コード（10桁）",
              "調剤基本料", "地域支援・医薬品供給対応体制加算", "届出の数"] + [f"届出：{s['name']}" for s in sel]

    def gen():
        for r in rows:
            yield [r["pref_name"], r["city"] or "", r["area"] or "", r["name"], r["address"] or "", r["id"],
                   r["kihon"] or "", r["chiiki"] or "", r["n_kijun"]] + \
                  ["○" if s["id"] in have.get(r["id"], ()) else "" for s in sel]
    return A._csv_response(header, gen(), "薬局の届出.csv")


@router.get("/yakkyoku/kijun/{name}", response_class=HTMLResponse)
def y_kijun(request: Request, name: str):
    A = _a()
    with A.db() as con:
        kj = con.execute("SELECT * FROM y_kijun WHERE name = ?", (name,)).fetchone()
        if not kj:
            raise HTTPException(404)
        by_pref = con.execute(
            """SELECT p.code, p.name, p.n_facilities AS n_all, COALESCE(kp.n, 0) AS n FROM y_prefs p
               LEFT JOIN y_kijun_pref kp ON kp.pref_code = p.code AND kp.kijun_id = ? ORDER BY p.code""",
            (kj["id"],)).fetchall()
        n_total = con.execute("SELECT COUNT(*) FROM y_fac").fetchone()[0]
        groups = _kijun_groups(con)
    return A.render(request, "y_kijun.html", kj=kj, desc=DESC.get(kj["name"], ""), by_pref=by_pref,
                    n_total=n_total, groups=groups)


@router.get("/yakkyoku/pref/{pref_name}", response_class=HTMLResponse)
def y_pref(request: Request, pref_name: str):
    A = _a()
    with A.db() as con:
        pref = con.execute("SELECT * FROM y_prefs WHERE name = ?", (pref_name,)).fetchone()
        if not pref:
            raise HTTPException(404)
        areas: dict[str, dict] = {}
        for r in con.execute("""SELECT area, city, SUM(n) AS n FROM y_areas WHERE pref_code = ?
                                GROUP BY area, city ORDER BY MIN(area_order), MIN(city_order)""", (pref["code"],)):
            areas.setdefault(r["area"], {"area": r["area"], "cities": []})["cities"].append(dict(r))
        kijun = con.execute(
            """SELECT k.id, k.name, kp.n FROM y_kijun_pref kp JOIN y_kijun k ON k.id = kp.kijun_id
               WHERE kp.pref_code = ?""", (pref["code"],)).fetchall()
        kihon = con.execute("SELECT kihon, COUNT(*) AS n FROM y_fac WHERE pref_code = ? GROUP BY kihon",
                            (pref["code"],)).fetchall()
    kijun = sorted(kijun, key=lambda r: order_key(r["name"]))
    return A.render(request, "y_pref.html", pref=pref, areas=list(areas.values()), kijun=kijun,
                    kihon={r["kihon"]: r["n"] for r in kihon})


@router.get("/yakkyoku/city/{pref_name}/{city}", response_class=HTMLResponse)
def y_city(request: Request, pref_name: str, city: str):
    A = _a()
    with A.db() as con:
        pref = con.execute("SELECT * FROM y_prefs WHERE name = ?", (pref_name,)).fetchone()
        if not pref:
            raise HTTPException(404)
        rows = con.execute("SELECT * FROM y_fac WHERE pref_code = ? AND city = ? ORDER BY name",
                           (pref["code"], city)).fetchall()
        if not rows:
            raise HTTPException(404)
        n_hosp = con.execute("SELECT COUNT(*) FROM facilities WHERE pref_code = ? AND city = ?",
                             (pref["code"], city)).fetchone()[0]
        others = con.execute(
            """SELECT city, SUM(n) AS n FROM y_areas WHERE pref_code = ? AND area = ? AND city <> ?
               GROUP BY city ORDER BY MIN(city_order)""", (pref["code"], rows[0]["area"], city)).fetchall()
    return A.render(request, "y_city.html", pref=pref, city=city, rows=rows, n_med=n_hosp, others=others,
                    area=rows[0]["area"])


@router.get("/yakkyoku/{fid}", response_class=HTMLResponse)
def y_facility(request: Request, fid: str):
    A = _a()
    if not (len(fid) == 10 and fid.isdigit()):
        raise HTTPException(404)
    with A.db() as con:
        f = con.execute("SELECT * FROM y_fac WHERE id = ?", (fid,)).fetchone()
        if not f:
            raise HTTPException(404)
        pref = con.execute("SELECT * FROM y_prefs WHERE code = ?", (f["pref_code"],)).fetchone()
        filings = con.execute(
            """SELECT k.name, fl.symbol, fl.juri_no, fl.start_date FROM y_filings fl JOIN y_kijun k ON k.id = fl.kijun_id
               WHERE fl.facility_id = ?""", (fid,)).fetchall()
        near = con.execute(
            "SELECT id, name, kihon, n_kijun FROM y_fac WHERE pref_code = ? AND city = ? AND id <> ? ORDER BY name LIMIT 20",
            (f["pref_code"], f["city"], fid)).fetchall() if f["city"] else []
        n_near = con.execute("SELECT COUNT(*) FROM y_fac WHERE pref_code = ? AND city = ?",
                             (f["pref_code"], f["city"])).fetchone()[0] if f["city"] else 0
        hosp = con.execute(
            """SELECT f.id, f.name, h.beds FROM facilities f LEFT JOIN hstats h ON h.facility_id = f.id
               WHERE f.pref_code = ? AND f.city = ? AND f.ftype = '病院'
               ORDER BY COALESCE(h.beds, 0) DESC, f.name LIMIT 10""",
            (f["pref_code"], f["city"])).fetchall() if f["city"] else []
    filings = sorted(filings, key=lambda r: order_key(r["name"]))
    groups = []
    for key, label, _names in GROUPS + [(OTHER[0], OTHER[1], [])]:
        g = [dict(r, desc=DESC.get(r["name"], "")) for r in filings if group_of(r["name"]) == key]
        if g:
            groups.append({"label": label, "rows": g})
    return A.render(request, "y_facility.html", f=f, pref=pref, groups=groups, n_filings=len(filings),
                    near=near, n_near=n_near, hosp=hosp)
