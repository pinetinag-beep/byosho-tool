"""施設基準届出の検索サイト（MedilenZ）。

ページをサーバー側で組み立てて返す（Googleが中身を読めるように。Streamlit版は
画面がJavaScriptで描かれるため検索エンジンにほぼ載らなかった）。データは
build_db.py が作る読み取り専用のSQLite。

起動（開発）:  uvicorn kijun_web.app:app --reload   （リポジトリ直下で）
"""
from __future__ import annotations

import csv
import io
import math
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .build_db import OUT as DEFAULT_DB, search_key

HERE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("KIJUN_DB", DEFAULT_DB))
SITE_URL = os.environ.get("SITE_URL", "https://medilenz.jp").rstrip("/")
STREAMLIT_URL = os.environ.get("STREAMLIT_URL", "https://byosho-tool-testver.streamlit.app").rstrip("/")
PAGE_SIZE = 50
SITEMAP_CHUNK = 40_000
FTYPES = ["病院", "有床診療所", "無床診療所"]
KOUSEIKYOKU_URL = "https://kouseikyoku.mhlw.go.jp/"
PDL_URL = "https://www.digital.go.jp/resources/open_data/public_data_license_v1.0"
MHLW_TERMS_URL = "https://www.mhlw.go.jp/chosakuken/index.html"

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


@contextmanager
def db():
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    try:
        yield con
    finally:
        con.close()


def _meta(con) -> dict:
    return {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM meta")}


def _prefs(con) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM prefs ORDER BY code").fetchall()


def _pref_by_name(con, name: str):
    return con.execute("SELECT * FROM prefs WHERE name = ?", (name,)).fetchone()


def _url(path: str, **params) -> str:
    q = {k: v for k, v in params.items() if v not in (None, "", [])}
    return path + ("?" + urlencode(q, doseq=True) if q else "")


def kijun_url(name: str, pref_name: str | None = None) -> str:
    url = "/kijun/" + quote(name, safe="")
    return url + "/" + quote(pref_name, safe="") if pref_name else url


def facility_url(fid: str) -> str:
    return f"/facility/{fid}"


def streamlit_url(byosho_name: str | None) -> str | None:
    return f"{STREAMLIT_URL}/?hospital={quote(byosho_name)}" if byosho_name else None


def jdate(iso: str | None) -> str:
    """「2026-06-01」→「2026年6月1日」"""
    try:
        y, m, d = (iso or "").split("-")
        return f"{int(y)}年{int(m)}月{int(d)}日"
    except ValueError:
        return ""


# 病床機能報告の区分（参考値）のうち、令和8年度改定で区分の体系が変わったもの。
KUBUN_REF_REVISED = ("急性期一般入院料１", "特定機能病院")


def kubun_ref_note(ref: str | None, start_date: str | None, ref_date: str | None) -> str:
    """区分の参考値にどの程度の注意書きを付けるか（"" / "changed" / "likely_changed"）。

    区分を公表している厚生局の届出（2026年10月時点）と照合した結果:
    算定開始日が病床機能報告の時点（7月1日）以前の届出は、参考値が99%一致した（出し直されていない）。
    それより後に出し直された届出は76%、うち急性期一般入院料１・特定機能病院は8%しか一致しなかった
    （令和8年度改定で急性期病院Ａ/Ｂ一般入院料・特定機能病院Ａ等に移ったため）。
    """
    if not ref or not start_date or not ref_date or start_date <= ref_date:
        return ""
    return "likely_changed" if any(k in ref for k in KUBUN_REF_REVISED) else "changed"


templates.env.filters["jdate"] = jdate
templates.env.globals.update(
    kijun_url=kijun_url, facility_url=facility_url, streamlit_url=streamlit_url, streamlit_home=STREAMLIT_URL,
    site_url=SITE_URL, kouseikyoku_url=KOUSEIKYOKU_URL, pdl_url=PDL_URL,
    mhlw_terms_url=MHLW_TERMS_URL, ftypes=FTYPES, kubun_ref_note=kubun_ref_note,
)


def render(request: Request, template: str, status_code: int = 200, **ctx) -> HTMLResponse:
    with db() as con:
        ctx.setdefault("meta", _meta(con))
    return templates.TemplateResponse(request, template, ctx, status_code=status_code)


# ── 検索 ────────────────────────────────────────────────────

def _search_sql(con, pref: str, q: str, k: str, kx: str, ftype: str) -> tuple[str, list, str, list]:
    """(施設のWHERE句, パラメータ, 該当届出のWHERE句, パラメータ) を返す。

    q  … 医療機関名または住所の一部
    k  … 施設基準名称。既存の名称と完全に一致すれば（候補リストから選んだ場合）その1つだけ、
          そうでなければ名称の一部として、一致する施設基準のどれかを届け出ている施設を探す
    kx … 施設基準名称の完全一致（施設基準ページからのCSVリンク用）
    """
    if k and not kx and con.execute("SELECT 1 FROM kijun WHERE name = ?", (k,)).fetchone():
        kx = k
    where, params = [], []
    if pref:
        where.append("f.pref_code = ?")
        params.append(pref)
    if ftype in FTYPES:
        where.append("f.ftype = ?")
        params.append(ftype)
    if q:
        key = f"%{search_key(q)}%"
        where.append("(f.name_search LIKE ? OR f.address_search LIKE ?)")
        params += [key, key]
    kwhere, kparams = "", []
    if kx:
        kwhere, kparams = "x.kijun_id IN (SELECT id FROM kijun WHERE name = ?)", [kx]
    elif k:
        kwhere, kparams = "x.kijun_id IN (SELECT id FROM kijun WHERE name_search LIKE ?)", [f"%{search_key(k)}%"]
    if kwhere:
        # 「施設ごとに届出を確認する」(EXISTS)形だと、「加算」のように多くの施設基準に
        # 当てはまる語で全国9万施設を1件ずつ調べて1.6秒かかった。先に施設基準→施設の
        # 索引(ix_fil_kijun)で該当施設を集めてから絞る形にする。
        where.append(f"f.id IN (SELECT x.facility_id FROM filings x WHERE {kwhere})")
        params += kparams
    return (" AND ".join(where) or "1"), params, kwhere, kparams


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with db() as con:
        popular = con.execute("SELECT name, n_facilities FROM kijun ORDER BY n_facilities DESC LIMIT 24").fetchall()
        prefs = _prefs(con)
        kijun_names = [r["name"] for r in con.execute("SELECT name FROM kijun ORDER BY n_facilities DESC")]
    return render(request, "index.html", popular=popular, prefs=prefs, kijun_names=kijun_names)


@app.get("/search", response_class=HTMLResponse)
def search(
    request: Request,
    pref: str = "", q: str = "", k: str = "", kx: str = "", ftype: str = "",
    page: int = Query(1, ge=1),
):
    q, k, kx = q.strip(), k.strip(), kx.strip()
    if kx and not k:
        k, kx = kx, ""
    with db() as con:
        where, params, kwhere, kparams = _search_sql(con, pref, q, k, kx, ftype)
        total = con.execute(f"SELECT COUNT(*) FROM facilities f WHERE {where}", params).fetchone()[0]
        pages = max(1, math.ceil(total / PAGE_SIZE))
        page = min(page, pages)
        rows = con.execute(
            f"""SELECT f.* FROM facilities f WHERE {where}
                ORDER BY f.pref_code, CASE f.ftype WHEN '病院' THEN 0 WHEN '有床診療所' THEN 1 ELSE 2 END, f.name
                LIMIT ? OFFSET ?""",
            params + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
        ).fetchall()
        hits: dict[str, list[str]] = {}
        if kwhere and rows:
            ids = [r["id"] for r in rows]
            for h in con.execute(
                f"""SELECT DISTINCT x.facility_id, kj.name FROM filings x JOIN kijun kj ON kj.id = x.kijun_id
                    WHERE x.facility_id IN ({','.join('?' * len(ids))}) AND {kwhere}
                    ORDER BY kj.n_facilities DESC""",
                ids + kparams,
            ):
                hits.setdefault(h["facility_id"], []).append(h["name"])
        matched_kijun = []
        exact_k = bool(k) and con.execute("SELECT 1 FROM kijun WHERE name = ?", (k,)).fetchone() is not None
        if k and not kx and not exact_k:
            matched_kijun = con.execute(
                "SELECT name, n_facilities FROM kijun WHERE name_search LIKE ? ORDER BY n_facilities DESC LIMIT 30",
                [f"%{search_key(k)}%"],
            ).fetchall()
        prefs = _prefs(con)
        kijun_names = [r["name"] for r in con.execute("SELECT name FROM kijun ORDER BY n_facilities DESC")]
    pref_name = next((p["name"] for p in prefs if p["code"] == pref), "")
    params_dict = dict(pref=pref, q=q, k=k, kx=kx, ftype=ftype)
    return render(
        request, "search.html",
        rows=rows, hits=hits, total=total, page=page, pages=pages, prefs=prefs, pref_name=pref_name,
        params=params_dict, matched_kijun=matched_kijun, kijun_names=kijun_names,
        csv_url=_url("/search.csv", **params_dict),
        page_url=lambda p: _url("/search", **params_dict, page=p),
        narrow_url=lambda name: _url("/search", k=name, pref=pref, q=q, ftype=ftype),
    )


FACILITY_COLS = ["都道府県", "医療機関番号", "医療機関名称", "施設種別", "住所"]
FILING_COLS = ["受理届出名称", "受理記号", "受理番号", "算定開始年月日", "病棟種別", "病床区分",
               "病棟数", "病床数", "区分", "内訳その他", "区分（参考：病床機能報告）"]


def _csv_response(header: list[str], rows, filename: str) -> StreamingResponse:
    def gen():
        buf = io.StringIO()
        w = csv.writer(buf)
        buf.write("﻿")  # Excelで文字化けしないようBOM付きUTF-8
        w.writerow(header)
        for r in rows:
            w.writerow(r)
            if buf.tell() > 64_000:
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()
        w.writerow([])
        if "区分（参考：病床機能報告）" in header:
            w.writerow(["区分（参考：病床機能報告）：厚生局が区分を公表していない届出について、"
                        "厚生労働省「病床機能報告」（7月1日時点の病棟の入院基本料）を加工して補ったもの。"
                        "届出の時点とは異なり、診療報酬改定等で変わっている場合があります"])
        w.writerow(["出典：各地方厚生局「施設基準の届出受理状況」を加工して作成（MedilenZ）"])
        yield buf.getvalue()

    return StreamingResponse(
        gen(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@app.get("/search.csv")
def search_csv(pref: str = "", q: str = "", k: str = "", kx: str = "", ftype: str = ""):
    """検索結果のCSV。施設基準を指定した場合は該当する届出を1行ずつ、
    指定しない場合は医療機関を1行ずつ出す（全届出96万行を一度に出さないため）。"""
    q, k, kx = q.strip(), k.strip(), kx.strip()
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    where, params, kwhere, kparams = _search_sql(con, pref, q, k, kx, ftype)
    if kwhere:
        header = FACILITY_COLS + FILING_COLS
        sql = f"""SELECT f.pref_name, f.code, f.name, f.ftype, f.address,
                         kj.name, x.symbol, x.juri_no, COALESCE(NULLIF(x.start_date, ''), x.start_raw),
                         x.byoto, x.byosho_kubun, x.n_byoto, x.n_byosho, x.kubun, x.other, x.kubun_ref
                  FROM facilities f JOIN filings x ON x.facility_id = f.id JOIN kijun kj ON kj.id = x.kijun_id
                  WHERE {where} AND {kwhere}
                  ORDER BY f.pref_code, f.name, kj.name"""
        rows = con.execute(sql, params + kparams)
        name = kx or k
    else:
        header = FACILITY_COLS + ["届出している施設基準の数"]
        rows = con.execute(
            f"""SELECT f.pref_name, f.code, f.name, f.ftype, f.address, f.n_kijun
                FROM facilities f WHERE {where} ORDER BY f.pref_code, f.name""",
            params,
        )
        name = q or "医療機関一覧"
    return _csv_response(header, rows, f"施設基準届出_{name}.csv")


# ── 医療機関ページ ───────────────────────────────────────────

# 「/facility/{fid}」より先に定義すること（後にすると「○○.csv」がfidとして通常ページ側に吸い込まれる）
@app.get("/facility/{fid}.csv")
def facility_csv(fid: str):
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    f = con.execute("SELECT name FROM facilities WHERE id = ?", (fid,)).fetchone()
    if not f:
        raise HTTPException(404)
    rows = con.execute(
        """SELECT f.pref_name, f.code, f.name, f.ftype, f.address,
                  kj.name, x.symbol, x.juri_no, COALESCE(NULLIF(x.start_date, ''), x.start_raw),
                  x.byoto, x.byosho_kubun, x.n_byoto, x.n_byosho, x.kubun, x.other, x.kubun_ref
           FROM filings x JOIN facilities f ON f.id = x.facility_id JOIN kijun kj ON kj.id = x.kijun_id
           WHERE x.facility_id = ? ORDER BY kj.name""",
        (fid,),
    )
    return _csv_response(FACILITY_COLS + FILING_COLS, rows, f"施設基準届出_{f[0]}.csv")


@app.get("/facility/{fid}", response_class=HTMLResponse)
def facility(request: Request, fid: str):
    with db() as con:
        f = con.execute("SELECT * FROM facilities WHERE id = ?", (fid,)).fetchone()
        if not f:
            raise HTTPException(404)
        filings = con.execute(
            """SELECT kj.name AS kijun, x.* FROM filings x JOIN kijun kj ON kj.id = x.kijun_id
               WHERE x.facility_id = ? ORDER BY kj.name, x.start_date""",
            (fid,),
        ).fetchall()
        pref = con.execute("SELECT * FROM prefs WHERE code = ?", (f["pref_code"],)).fetchone()
    return render(request, "facility.html", f=f, filings=filings, pref=pref)


# ── 施設基準ページ ───────────────────────────────────────────

@app.get("/kijun", response_class=HTMLResponse)
def kijun_index(request: Request):
    with db() as con:
        rows = con.execute("SELECT name, symbol, n_facilities FROM kijun ORDER BY n_facilities DESC, name").fetchall()
    return render(request, "kijun_index.html", rows=rows)


@app.get("/kijun/{name}", response_class=HTMLResponse)
def kijun_detail(request: Request, name: str):
    with db() as con:
        kj = con.execute("SELECT * FROM kijun WHERE name = ?", (name,)).fetchone()
        if not kj:
            raise HTTPException(404)
        by_pref = con.execute(
            """SELECT p.code, p.name, COALESCE(kp.n, 0) AS n, p.n_facilities FROM prefs p
               LEFT JOIN kijun_pref kp ON kp.pref_code = p.code AND kp.kijun_id = ?
               ORDER BY p.code""",
            (kj["id"],),
        ).fetchall()
        by_type = con.execute(
            """SELECT f.ftype, COUNT(DISTINCT f.id) AS n FROM filings x JOIN facilities f ON f.id = x.facility_id
               WHERE x.kijun_id = ? GROUP BY f.ftype""",
            (kj["id"],),
        ).fetchall()
        kubun = con.execute(
            """SELECT x.kubun, COUNT(DISTINCT x.facility_id) AS n FROM filings x
               WHERE x.kijun_id = ? AND x.kubun != '' GROUP BY x.kubun ORDER BY n DESC LIMIT 20""",
            (kj["id"],),
        ).fetchall()
    type_counts = {r["ftype"]: r["n"] for r in by_type}
    return render(request, "kijun.html", kj=kj, by_pref=by_pref, type_counts=type_counts, kubun=kubun,
                  csv_url=_url("/search.csv", kx=name))


@app.get("/kijun/{name}/{pref_name}", response_class=HTMLResponse)
def kijun_pref(request: Request, name: str, pref_name: str, page: int = Query(1, ge=1)):
    with db() as con:
        kj = con.execute("SELECT * FROM kijun WHERE name = ?", (name,)).fetchone()
        pref = _pref_by_name(con, pref_name)
        if not kj or not pref:
            raise HTTPException(404)
        total = con.execute(
            """SELECT COUNT(DISTINCT x.facility_id) FROM filings x JOIN facilities f ON f.id = x.facility_id
               WHERE x.kijun_id = ? AND f.pref_code = ?""",
            (kj["id"], pref["code"]),
        ).fetchone()[0]
        size = 100
        pages = max(1, math.ceil(total / size))
        page = min(page, pages)
        rows = con.execute(
            """SELECT f.id, f.name, f.ftype, f.address,
                      MIN(NULLIF(x.start_date, '')) AS start_date,
                      GROUP_CONCAT(DISTINCT NULLIF(x.kubun, '')) AS kubun,
                      GROUP_CONCAT(DISTINCT NULLIF(x.kubun_ref, '')) AS kubun_ref
               FROM filings x JOIN facilities f ON f.id = x.facility_id
               WHERE x.kijun_id = ? AND f.pref_code = ?
               GROUP BY f.id
               ORDER BY CASE f.ftype WHEN '病院' THEN 0 WHEN '有床診療所' THEN 1 ELSE 2 END, f.name
               LIMIT ? OFFSET ?""",
            (kj["id"], pref["code"], size, (page - 1) * size),
        ).fetchall()
    return render(
        request, "kijun_pref.html", kj=kj, pref=pref, rows=rows, total=total, page=page, pages=pages,
        csv_url=_url("/search.csv", kx=name, pref=pref["code"]),
        page_url=lambda p: kijun_url(name, pref_name) + (f"?page={p}" if p > 1 else ""),
    )


# ── 都道府県・その他 ─────────────────────────────────────────

@app.get("/pref/{pref_name}", response_class=HTMLResponse)
def pref_page(request: Request, pref_name: str):
    with db() as con:
        pref = _pref_by_name(con, pref_name)
        if not pref:
            raise HTTPException(404)
        types = con.execute(
            "SELECT ftype, COUNT(*) AS n FROM facilities WHERE pref_code = ? GROUP BY ftype", (pref["code"],)
        ).fetchall()
        top = con.execute(
            """SELECT kj.name, kp.n FROM kijun_pref kp JOIN kijun kj ON kj.id = kp.kijun_id
               WHERE kp.pref_code = ? ORDER BY kp.n DESC LIMIT 60""",
            (pref["code"],),
        ).fetchall()
        hospitals = con.execute(
            "SELECT id, name, n_kijun FROM facilities WHERE pref_code = ? AND ftype = '病院' ORDER BY name",
            (pref["code"],),
        ).fetchall()
    return render(request, "pref.html", pref=pref, types={r["ftype"]: r["n"] for r in types}, top=top,
                  hospitals=hospitals)


@app.get("/about", response_class=HTMLResponse)
def about(request: Request):
    with db() as con:
        prefs = _prefs(con)
    return render(request, "about.html", prefs=prefs)


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return (
        "User-agent: *\n"
        "Disallow: /search\n"
        "Disallow: /*.csv\n"
        f"Sitemap: {SITE_URL}/sitemap.xml\n"
    )


def _xml(body: str) -> Response:
    return Response('<?xml version="1.0" encoding="UTF-8"?>\n' + body, media_type="application/xml")


def _loc(path: str) -> str:
    return f"<url><loc>{SITE_URL}{path}</loc></url>"


@app.get("/sitemap.xml")
def sitemap_index():
    with db() as con:
        n_fac = con.execute("SELECT COUNT(*) FROM facilities").fetchone()[0]
    names = ["static", "kijun"] + [f"facilities-{i}" for i in range(math.ceil(n_fac / SITEMAP_CHUNK))]
    items = "".join(f"<sitemap><loc>{SITE_URL}/sitemap-{n}.xml</loc></sitemap>" for n in names)
    return _xml(f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</sitemapindex>')


@app.get("/sitemap-{part}.xml")
def sitemap_part(part: str):
    with db() as con:
        if part == "static":
            paths = ["/", "/kijun", "/about"] + [f"/pref/{quote(p['name'])}" for p in _prefs(con)]
        elif part == "kijun":
            paths = [kijun_url(r["name"]) for r in con.execute("SELECT name FROM kijun")]
            paths += [
                kijun_url(r["kname"], r["pname"])
                for r in con.execute(
                    """SELECT kj.name AS kname, p.name AS pname FROM kijun_pref kp
                       JOIN kijun kj ON kj.id = kp.kijun_id JOIN prefs p ON p.code = kp.pref_code"""
                )
            ]
        elif part.startswith("facilities-") and part[11:].isdigit():
            i = int(part[11:])
            paths = [
                facility_url(r["id"])
                for r in con.execute("SELECT id FROM facilities ORDER BY id LIMIT ? OFFSET ?",
                                     (SITEMAP_CHUNK, i * SITEMAP_CHUNK))
            ]
            if not paths:
                raise HTTPException(404)
        else:
            raise HTTPException(404)
    body = "".join(_loc(p) for p in paths)
    return _xml(f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>')


@app.exception_handler(404)
def not_found(request: Request, exc):
    return render(request, "404.html", status_code=404)
