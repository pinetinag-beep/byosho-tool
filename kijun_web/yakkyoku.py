"""薬局を探す（/yakkyoku/…）・歯科診療所を探す（/shika/…）。各地方厚生局「届出受理医療機関名簿（薬局）（歯科）」を加工したもの。

データは build_db._build_yakkyoku() が作る y_* と、_build_shika() が作る d_* のテーブル。医科のページ（app.py）とは
別のテーブル・URLにしている（医療機関番号の体系が医科と別で、届出の種類も別のため）。
薬局と歯科はページの形が同じなので、違うところ（テーブル・URL・呼び名・一覧に出す列・届出の説明）を Kind にまとめ、
同じ関数・テンプレート（y_*.html）で両方のページを作る。テンプレートでは K が Kind。
"""
from __future__ import annotations

import math
import re
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from dataclasses import dataclass
from types import ModuleType

from . import shika_kijun, yakkyoku_kijun

router = APIRouter()
PAGE_SIZE = 50


@dataclass(frozen=True)
class Kind:
    x: str            # テーブル名の頭（y_fac など）
    base: str         # URL
    word: str         # 施設の呼び名（「薬局」「歯科診療所」）
    roster: str       # 名簿の種類（出典に書く）
    col1: str         # 一覧に出す1つ目の届出の見出し（_fac.kihon）
    col2: str         # 2つ目（_fac.chiiki）
    col2_strip: str   # 2つ目の届出名から、一覧で省く頭の部分（省いて何も残らなければ「あり」と出す）
    mod: ModuleType   # 届出のまとまりと説明
    schema: str       # 構造化データの種類
    ga_event: str
    topics: str       # タイトル・説明文に入れる主な届出
    placeholder: str
    pref_facts: tuple  # 都道府県のページで件数を出す届出（1つ目の列の値）
    col1_strip: str = ""   # 1つ目の届出名から、一覧で省く部分

    def v1(self, v) -> str:   # 一覧に出す値
        return (v.replace(self.col1_strip, "") or "あり") if v else "—"

    def v2(self, v) -> str:
        return (v.replace(self.col2_strip, "") or "あり") if v else "—"

    def url(self, fid: str) -> str:
        return f"{self.base}/{fid}"

    def pref_url(self, pref_name: str) -> str:
        return f"{self.base}/pref/{quote(pref_name, safe='')}"

    def city_url(self, pref_name: str, city: str) -> str:
        return f"{self.base}/city/{quote(pref_name, safe='')}/{quote(city, safe='')}"

    def kijun_url(self, name: str) -> str:
        return f"{self.base}/kijun/{quote(name, safe='')}"

    def t(self, name: str) -> str:   # テーブル名
        return f"{self.x}_{name}"


YAKKYOKU = Kind(
    x="y", base="/yakkyoku", word="薬局", roster="薬局",
    col1="調剤基本料", col2="地域支援・医薬品供給対応体制加算", col2_strip="地域支援・医薬品供給対応体制",
    mod=yakkyoku_kijun, schema="Pharmacy", ga_event="view_pharmacy",
    topics="調剤基本料・地域支援・在宅・無菌製剤処理など", placeholder="例：さくら薬局、横浜市港北区",
    pref_facts=("調剤基本料１", "調剤基本料２", "調剤基本料３イ", "調剤基本料３ロ", "調剤基本料３ハ", "特別調剤基本料Ａ"))
SHIKA = Kind(
    x="d", base="/shika", word="歯科診療所", roster="歯科",
    col1="口腔管理体制強化加算", col2="歯科外来診療医療安全対策加算", col2_strip="歯科外来診療医療安全対策",
    mod=shika_kijun, schema="Dentist", ga_event="view_dentist",
    topics="口腔管理体制強化加算・在宅療養支援歯科診療所・歯科外来診療の安全対策・CAD/CAMなど",
    placeholder="例：さくら歯科、横浜市港北区", pref_facts=(),
    col1_strip="小児口腔機能管理料の注５に規定する口腔管理体制強化加算")
KINDS = (YAKKYOKU, SHIKA)


# 医科のページから薬局へのリンクに使う
y_url, y_pref_url, y_city_url, y_kijun_url = YAKKYOKU.url, YAKKYOKU.pref_url, YAKKYOKU.city_url, YAKKYOKU.kijun_url


def _a():
    from . import app as A   # app.py がこのモジュールを読み込むので、使う時に読む
    return A


OTHER_MIN = 30   # トップの一覧の「その他」には、これ以上の施設が届け出ているものだけを出す


def _kijun_groups(K: Kind, con, all_other: bool = True) -> list[dict]:
    """届出をまとまりごとに（フォームとトップの一覧用）。
    歯科の名簿には病院の歯科が届け出ている医科の届出（入院基本料など）がまばらに載っていて数百種類になるので、
    トップの一覧（all_other=False）の「その他」は届け出ている施設が多いものだけにする。"""
    m = K.mod
    rows = con.execute(f"SELECT id, name, symbol, n_facilities FROM {K.t('kijun')}").fetchall()
    out = {k: {"key": k, "label": lab, "items": []} for k, lab, _ in m.GROUPS}
    out[m.OTHER[0]] = {"key": m.OTHER[0], "label": m.OTHER[1], "items": []}
    for r in sorted(rows, key=lambda r: m.order_key(r["name"])):
        out[m.group_of(r["name"])]["items"].append({**dict(r), "desc": m.DESC.get(r["name"], "")})
    if not all_other:
        o = out[m.OTHER[0]]
        o["n_hidden"] = sum(1 for k in o["items"] if k["n_facilities"] < OTHER_MIN)
        o["items"] = [k for k in o["items"] if k["n_facilities"] >= OTHER_MIN]
    return [g for g in out.values() if g["items"]]


_FAM = re.compile(r"[０-９0-9]+[イロハ]?(?=$|の注|（)")
_ZEN = str.maketrans("０１２３４５６７８９イロハ", "0123456789abc")   # 「３イ・３ロ・３ハ」はいろは順


def _nat(s: str) -> tuple:
    """「２」より「１０」が後になるように（区分の並び）。"""
    return tuple((0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.findall(r"\d+|\D+", s.translate(_ZEN)))


def _common_prefix(names: list[str]) -> str:
    p = names[0]
    for n in names[1:]:
        while not n.startswith(p):
            p = p[:-1]
    return p


def _merge_rows(K: Kind, con, items: list[dict]) -> list[dict]:
    """区分の数字だけが違う届出（歯科外来診療感染対策加算１〜４、調剤基本料１・２・３イ…など）を1行にまとめる。
    1行のチェックで中の区分をすべて、区分ごとのチェックで1つずつ選べる（「診療科から探す」と同じ形）。"""
    fams: dict[str, list[dict]] = {}
    for k in items:
        fams.setdefault(_FAM.sub("", k["name"]), []).append(k)
    rows = []
    for key, ks in fams.items():
        if len(ks) == 1:
            k = ks[0]
            rows.append({**k, "subs": [], "ids": [k["id"]]})
            continue
        names = [k["name"] for k in ks]
        pre = _common_prefix(names)
        suf = _common_prefix([n[len(pre):][::-1] for n in names])[::-1]
        ks = sorted(ks, key=lambda k: _nat(k["name"][len(pre):len(k["name"]) - len(suf)]))
        ids = [k["id"] for k in ks]
        n = con.execute(f"SELECT COUNT(DISTINCT facility_id) FROM {K.t('filings')} WHERE kijun_id IN ({','.join('?' * len(ids))})",
                        ids).fetchone()[0]
        main = max(ks, key=lambda k: k["n_facilities"])
        rows.append({"name": (pre + suf).rstrip("・（"), "id": ids[0], "ids": ids, "n_facilities": n,
                     "desc": main["desc"] or next((k["desc"] for k in ks if k["desc"]), ""),
                     "subs": [{**k, "label": k["name"][len(pre):len(k["name"]) - len(suf)] or "通常"} for k in ks]})
    return rows


def _prefs(K: Kind, con):
    return con.execute(f"SELECT * FROM {K.t('prefs')} ORDER BY code").fetchall()


class YCond:
    def __init__(self, request: Request, K: Kind = YAKKYOKU):
        qp = request.query_params
        self.K = K
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
            fl = self.K.t("filings")
            if self.mode == "and" and len(self.items) > 1:
                w.append(f"""f.id IN (SELECT facility_id FROM {fl} WHERE kijun_id IN ({q})
                             GROUP BY facility_id HAVING COUNT(DISTINCT kijun_id) = {len(self.items)})""")
            else:
                w.append(f"f.id IN (SELECT facility_id FROM {fl} WHERE kijun_id IN ({q}))")
            p += self.items
        return (" WHERE " + " AND ".join(w)) if w else "", p

    def query(self, **extra) -> str:
        from urllib.parse import urlencode
        pairs = [("pref", self.pref), ("city", self.city), ("q", self.q)] + [("item", i) for i in self.items]
        if len(self.items) > 1 and self.mode == "and":
            pairs.append(("mode", "and"))
        pairs += list(extra.items())
        return urlencode([(k, v) for k, v in pairs if v not in ("", None)])


def _hits(K: Kind, con, fids: list[str], items: list[int]) -> dict[str, list[str]]:
    if not fids or not items:
        return {}
    out: dict[str, list[str]] = {}
    for fid, name in con.execute(
            f"""SELECT fl.facility_id, k.name FROM {K.t('filings')} fl JOIN {K.t('kijun')} k ON k.id = fl.kijun_id
                WHERE fl.facility_id IN ({','.join('?' * len(fids))}) AND fl.kijun_id IN ({','.join('?' * len(items))})""",
            fids + items):
        out.setdefault(fid, []).append(name)
    return out


def k_index(K: Kind, request: Request):
    A = _a()
    c = YCond(request, K)   # 検索結果の「条件を変更する」から戻った時は条件を入れた状態で出す
    with A.db() as con:
        groups = _kijun_groups(K, con, all_other=False)
        for g in groups:
            g["rows"] = _merge_rows(K, con, g["items"])
        prefs = _prefs(K, con)
        n = con.execute(f"SELECT COUNT(*) FROM {K.t('fac')}").fetchone()[0]
        as_of = con.execute(f"SELECT MIN(as_of_label), MAX(as_of_label) FROM {K.t('prefs')}").fetchone()
        kd_top = _kd_top(con, K)
    return A.render(request, "y_index.html", K=K, groups=groups, prefs=prefs, n_total=n, c=c, as_of=as_of, kd_top=kd_top)


def _has_kd(con, K: Kind) -> bool:
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (K.t("kdiff"),)).fetchone())


def _kd_top(con, K: Kind):
    """最新の月の新規届出・取り下げの件数（トップの案内用）。"""
    if not _has_kd(con, K):
        return None
    t = K.t("kdiff")
    return con.execute(f"""SELECT month, SUM(kind = '新規') AS n_new, SUM(kind = '取り下げ') AS n_drop FROM {t}
                           WHERE month = (SELECT MAX(month) FROM {t})""").fetchone()


def _count(con, K: Kind) -> int:
    try:
        return con.execute(f"SELECT COUNT(*) FROM {K.t('fac')}").fetchone()[0]
    except Exception:  # noqa: BLE001  （テーブルがまだ無い古いDB）
        return 0


def k_search(K: Kind, request: Request, page: int):
    A = _a()
    c = YCond(request, K)
    with A.db() as con:
        where, params = c.where(A.search_key)
        total = con.execute(f"SELECT COUNT(*) FROM {K.t('fac')} f{where}", params).fetchone()[0]
        pages = max(1, math.ceil(total / PAGE_SIZE))
        page = min(page, pages)
        rows = con.execute(
            f"""SELECT f.* FROM {K.t('fac')} f{where} ORDER BY f.pref_code, f.city, f.name LIMIT ? OFFSET ?""",
            params + [PAGE_SIZE, (page - 1) * PAGE_SIZE]).fetchall()
        hits = _hits(K, con, [r["id"] for r in rows], c.items)
        sel = [dict(r) for r in con.execute(
            f"SELECT id, name, symbol FROM {K.t('kijun')} WHERE id IN ({','.join('?' * len(c.items))})", c.items)] if c.items else []
        pref = con.execute(f"SELECT * FROM {K.t('prefs')} WHERE code = ?", (c.pref,)).fetchone() if c.pref else None
        cities = con.execute(
            f"SELECT city, SUM(n) AS n FROM {K.t('areas')} WHERE pref_code = ? GROUP BY city ORDER BY MIN(city_order)",
            (c.pref,)).fetchall() if c.pref else []
    return A.render(request, "y_search.html", K=K, c=c, rows=rows, hits=hits, sel=sel, total=total, page=page,
                    pages=pages, pref=pref, cities=cities,
                    page_url=lambda p: f"{K.base}/search?" + c.query(page=p if p > 1 else ""),
                    csv_url=f"{K.base}/search.csv?" + c.query())


def k_search_csv(K: Kind, request: Request):
    """検索結果のCSV。施設を1行ずつ、選んだ届出ごとに届け出ていれば「○」の列を付ける。"""
    A = _a()
    c = YCond(request, K)
    con = A.sqlite3.connect(f"file:{A.DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = A.sqlite3.Row
    where, params = c.where(A.search_key)
    sel = con.execute(f"SELECT id, name FROM {K.t('kijun')} WHERE id IN ({','.join('?' * len(c.items))})",
                      c.items).fetchall() if c.items else []
    rows = con.execute(f"SELECT f.* FROM {K.t('fac')} f{where} ORDER BY f.pref_code, f.city, f.name", params).fetchall()
    have: dict[str, set[int]] = {}
    if sel:
        ids = [s["id"] for s in sel]
        for fid, kid in con.execute(
                f"SELECT facility_id, kijun_id FROM {K.t('filings')} WHERE kijun_id IN ({','.join('?' * len(ids))})", ids):
            have.setdefault(fid, set()).add(kid)
    con.close()
    header = ["都道府県", "市区町村", "二次医療圏", f"{K.word}名", "所在地", "医療機関コード（10桁）",
              K.col1, K.col2, "届出の数"] + [f"届出：{s['name']}" for s in sel]

    def gen():
        for r in rows:
            yield [r["pref_name"], r["city"] or "", r["area"] or "", r["name"], r["address"] or "", r["id"],
                   r["kihon"] or "", r["chiiki"] or "", r["n_kijun"]] + \
                  ["○" if s["id"] in have.get(r["id"], ()) else "" for s in sel]
    return A._csv_response(header, gen(), f"{K.word}の届出.csv")


def k_kijun(K: Kind, request: Request, name: str):
    A = _a()
    with A.db() as con:
        kj = con.execute(f"SELECT * FROM {K.t('kijun')} WHERE name = ?", (name,)).fetchone()
        if not kj:
            raise HTTPException(404)
        by_pref = con.execute(
            f"""SELECT p.code, p.name, p.n_facilities AS n_all, COALESCE(kp.n, 0) AS n FROM {K.t('prefs')} p
               LEFT JOIN {K.t('kijun_pref')} kp ON kp.pref_code = p.code AND kp.kijun_id = ? ORDER BY p.code""",
            (kj["id"],)).fetchall()
        n_total = con.execute(f"SELECT COUNT(*) FROM {K.t('fac')}").fetchone()[0]
        groups = _kijun_groups(K, con, all_other=False)
        kd = None
        if _has_kd(con, K):
            t = K.t("kdiff")
            kd = con.execute(f"""SELECT month, SUM(kind = '新規') AS n_new, SUM(kind = '変更') AS n_chg,
                                    SUM(kind = '取り下げ') AS n_drop FROM {t}
                                 WHERE (kijun_name = ? OR prev_name = ?) AND month = (SELECT MAX(month) FROM {t})""",
                             (kj["name"], kj["name"])).fetchone()
            kd = kd if kd and kd["month"] else None
    return A.render(request, "y_kijun.html", K=K, kj=kj, desc=K.mod.DESC.get(kj["name"], ""), by_pref=by_pref,
                    n_total=n_total, groups=groups, kd=kd)


def k_pref(K: Kind, request: Request, pref_name: str):
    A = _a()
    with A.db() as con:
        pref = con.execute(f"SELECT * FROM {K.t('prefs')} WHERE name = ?", (pref_name,)).fetchone()
        if not pref:
            raise HTTPException(404)
        areas: dict[str, dict] = {}
        for r in con.execute(f"""SELECT area, city, SUM(n) AS n FROM {K.t('areas')} WHERE pref_code = ?
                                GROUP BY area, city ORDER BY MIN(area_order), MIN(city_order)""", (pref["code"],)):
            areas.setdefault(r["area"], {"area": r["area"], "cities": []})["cities"].append(dict(r))
        kijun = con.execute(
            f"""SELECT k.id, k.name, kp.n FROM {K.t('kijun_pref')} kp JOIN {K.t('kijun')} k ON k.id = kp.kijun_id
               WHERE kp.pref_code = ?""", (pref["code"],)).fetchall()
        kihon = con.execute(f"SELECT kihon, COUNT(*) AS n FROM {K.t('fac')} WHERE pref_code = ? GROUP BY kihon",
                            (pref["code"],)).fetchall()
    kijun = sorted(kijun, key=lambda r: K.mod.order_key(r["name"]))
    return A.render(request, "y_pref.html", K=K, pref=pref, areas=list(areas.values()), kijun=kijun,
                    kihon={r["kihon"]: r["n"] for r in kihon})


def k_city(K: Kind, request: Request, pref_name: str, city: str):
    A = _a()
    with A.db() as con:
        pref = con.execute(f"SELECT * FROM {K.t('prefs')} WHERE name = ?", (pref_name,)).fetchone()
        if not pref:
            raise HTTPException(404)
        rows = con.execute(f"SELECT * FROM {K.t('fac')} WHERE pref_code = ? AND city = ? ORDER BY name",
                           (pref["code"], city)).fetchall()
        if not rows:
            raise HTTPException(404)
        n_hosp = con.execute("SELECT COUNT(*) FROM facilities WHERE pref_code = ? AND city = ?",
                             (pref["code"], city)).fetchone()[0]
        others = con.execute(
            f"""SELECT city, SUM(n) AS n FROM {K.t('areas')} WHERE pref_code = ? AND area = ? AND city <> ?
               GROUP BY city ORDER BY MIN(city_order)""", (pref["code"], rows[0]["area"], city)).fetchall()
        # 同じ市区町村の、もう一方（薬局なら歯科、歯科なら薬局）の数
        sib = [(k, con.execute(f"SELECT COUNT(*) FROM {k.t('fac')} WHERE pref_code = ? AND city = ?",
                               (pref["code"], city)).fetchone()[0]) for k in KINDS if k is not K and _count(con, k)]
    return A.render(request, "y_city.html", K=K, pref=pref, city=city, rows=rows, n_med=n_hosp, others=others,
                    area=rows[0]["area"], sib=[(k, n) for k, n in sib if n])


def k_facility(K: Kind, request: Request, fid: str):
    A = _a()
    if not (len(fid) == 10 and fid.isdigit()):
        raise HTTPException(404)
    m = K.mod
    with A.db() as con:
        f = con.execute(f"SELECT * FROM {K.t('fac')} WHERE id = ?", (fid,)).fetchone()
        if not f:
            raise HTTPException(404)
        pref = con.execute(f"SELECT * FROM {K.t('prefs')} WHERE code = ?", (f["pref_code"],)).fetchone()
        filings = con.execute(
            f"""SELECT k.name, fl.symbol, fl.juri_no, fl.start_date FROM {K.t('filings')} fl
               JOIN {K.t('kijun')} k ON k.id = fl.kijun_id WHERE fl.facility_id = ?""", (fid,)).fetchall()
        near = con.execute(
            f"SELECT id, name, kihon, n_kijun FROM {K.t('fac')} WHERE pref_code = ? AND city = ? AND id <> ? ORDER BY name LIMIT 20",
            (f["pref_code"], f["city"], fid)).fetchall() if f["city"] else []
        n_near = con.execute(f"SELECT COUNT(*) FROM {K.t('fac')} WHERE pref_code = ? AND city = ?",
                             (f["pref_code"], f["city"])).fetchone()[0] if f["city"] else 0
        hosp = con.execute(
            """SELECT f.id, f.name, h.beds FROM facilities f LEFT JOIN hstats h ON h.facility_id = f.id
               WHERE f.pref_code = ? AND f.city = ? AND f.ftype = '病院'
               ORDER BY COALESCE(h.beds, 0) DESC, f.name LIMIT 10""",
            (f["pref_code"], f["city"])).fetchall() if f["city"] else []
        kd = con.execute(f"SELECT * FROM {K.t('kdiff')} WHERE facility_id = ? ORDER BY month DESC, kind, kijun_name",
                         (fid,)).fetchall() if _has_kd(con, K) else []
    filings = sorted(filings, key=lambda r: m.order_key(r["name"]))
    groups = []
    for key, label, _names in m.GROUPS + [(m.OTHER[0], m.OTHER[1], [])]:
        g = [dict(r, desc=m.DESC.get(r["name"], "")) for r in filings if m.group_of(r["name"]) == key]
        if g:
            groups.append({"label": label, "rows": g})
    return A.render(request, "y_facility.html", K=K, f=f, pref=pref, groups=groups, n_filings=len(filings),
                    near=near, n_near=n_near, hosp=hosp, kd=kd)


def _register(K: Kind) -> None:
    b = K.base

    def index(request: Request):
        return k_index(K, request)

    def search(request: Request, page: int = Query(1, ge=1)):
        return k_search(K, request, page)

    def search_csv(request: Request):
        return k_search_csv(K, request)

    def kijun(request: Request, name: str):
        return k_kijun(K, request, name)

    def pref(request: Request, pref_name: str):
        return k_pref(K, request, pref_name)

    def city(request: Request, pref_name: str, city: str):
        return k_city(K, request, pref_name, city)

    def facility(request: Request, fid: str):
        return k_facility(K, request, fid)

    html = {"methods": ["GET"], "response_class": HTMLResponse}
    router.add_api_route(b, index, **html)
    router.add_api_route(f"{b}/search", search, **html)
    router.add_api_route(f"{b}/search.csv", search_csv, methods=["GET"])
    router.add_api_route(f"{b}/kijun/{{name}}", kijun, **html)
    router.add_api_route(f"{b}/pref/{{pref_name}}", pref, **html)
    router.add_api_route(f"{b}/city/{{pref_name}}/{{city}}", city, **html)
    router.add_api_route(f"{b}/{{fid}}", facility, **html)


for _K in KINDS:
    _register(_K)
