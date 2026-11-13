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
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
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
# 広告（Google AdSense）。審査に通ってから環境変数で入れる。空なら広告のタグは一切出さない
ADSENSE_CLIENT = os.environ.get("ADSENSE_CLIENT", "").strip()   # 例: ca-pub-1234567890123456
ADSENSE_SLOT = os.environ.get("ADSENSE_SLOT", "").strip()       # 手動で置く広告ユニットのID（無ければ自動広告のみ）
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "").strip()     # プライバシーポリシーの問い合わせ先
# アクセス解析（Google アナリティクス 4）。Streamlit版と同じプロパティ（medilenz.jp のウェブストリーム）。
# 本番のドメインで開いた時だけ送る（IPアドレス直打ち・手元での確認は数えない）
GA_MEASUREMENT_ID = os.environ.get("GA_MEASUREMENT_ID", "G-8Y6SDBSCMQ").strip()
OPERATOR_NAME = "MedilenZ運営事務局"                              # 運営者情報に載せる名前（ユーザー判断、2026年11月）
# お問い合わせはGoogleフォームで受ける（メールアドレスをページに出さないため）。URLが決まったらここに入れる
CONTACT_FORM_URL = os.environ.get("CONTACT_FORM_URL", "").strip()
AD_PREVIEW = os.environ.get("AD_PREVIEW", "") == "1"             # 見た目の確認用: 広告枠の位置にダミーの枠を出す

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
# トップと検索結果は届出項目のチェックボックス（約1,300個）を含み350KB程度になるので圧縮して返す
app.add_middleware(GZipMiddleware, minimum_size=1000)
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
# CSSを変えた時にブラウザが古いCSSを使い続けないよう、URLに更新時刻を付ける
templates.env.globals["css_version"] = int((HERE / "static" / "style.css").stat().st_mtime)
templates.env.globals.update(
    kijun_url=kijun_url, facility_url=facility_url, streamlit_url=streamlit_url, streamlit_home=STREAMLIT_URL,
    site_url=SITE_URL, kouseikyoku_url=KOUSEIKYOKU_URL, pdl_url=PDL_URL,
    mhlw_terms_url=MHLW_TERMS_URL, ftypes=FTYPES, kubun_ref_note=kubun_ref_note,
    adsense_client=ADSENSE_CLIENT, adsense_slot=ADSENSE_SLOT, contact_email=CONTACT_EMAIL, operator_name=OPERATOR_NAME, ga_id=GA_MEASUREMENT_ID,
    contact_form_url=CONTACT_FORM_URL, ad_preview=AD_PREVIEW,
)


def render(request: Request, template: str, status_code: int = 200, **ctx) -> HTMLResponse:
    with db() as con:
        ctx.setdefault("meta", _meta(con))
    return templates.TemplateResponse(request, template, ctx, status_code=status_code)


# ── 検索 ────────────────────────────────────────────────────

@dataclass
class Cond:
    """検索条件。フォームの複数選択（二次医療圏・市区町村・施設の種類・届出項目）は同じ名前の
    パラメータを繰り返す（?area=中央&area=安芸）。"""
    pref: str = ""             # 都道府県が1つだけの時のコード（二次医療圏・市区町村はこの時だけ使える）
    prefs: list[str] = field(default_factory=list)   # 選んだ都道府県（複数可）
    areas: list[str] = field(default_factory=list)
    cities: list[str] = field(default_factory=list)
    ftypes: list[str] = field(default_factory=list)
    q: str = ""
    items: list[int] = field(default_factory=list)
    kubuns: list[int] = field(default_factory=list)   # 入院基本料の区分（急性期一般入院料１など、kubuns.id）
    mode: str = "and"          # 届出項目・区分を「すべて」届け出ている(and) / 「いずれか」(or)
    k: str = ""                # 施設基準名称の一部（施設基準ページの「条件を足して検索」など、従来のリンク用）
    kx: str = ""               # 施設基準名称の完全一致（施設基準ページのCSVリンク用）

    def __post_init__(self):
        if self.pref and not self.prefs:
            self.prefs = [self.pref]
        self.prefs = list(dict.fromkeys(p for p in self.prefs if p))
        self.pref = self.prefs[0] if len(self.prefs) == 1 else ""
        if not self.pref:
            self.areas, self.cities = [], []

    @classmethod
    def from_request(cls, request: Request) -> "Cond":
        qp = request.query_params
        items = [int(v) for v in qp.getlist("item") if v.isdigit()]
        kubuns = [int(v) for v in qp.getlist("kb") if v.isdigit()]
        return cls(
            prefs=[v.strip() for v in qp.getlist("pref") if v.strip()],
            areas=[v for v in qp.getlist("area") if v],
            cities=[v for v in qp.getlist("city") if v],
            ftypes=[v for v in qp.getlist("ftype") if v in FTYPES],
            q=qp.get("q", "").strip(),
            items=list(dict.fromkeys(items)),
            kubuns=list(dict.fromkeys(kubuns)),
            mode="or" if qp.get("mode") == "or" else "and",
            k=qp.get("k", "").strip(),
            kx=qp.get("kx", "").strip(),
        )

    def query(self, **extra) -> str:
        pairs = [("pref", p) for p in self.prefs] + [("area", a) for a in self.areas] + [("city", c) for c in self.cities]
        pairs += [("ftype", t) for t in self.ftypes] + [("q", self.q)] + [("item", i) for i in self.items]
        pairs += [("kb", i) for i in self.kubuns]
        if self.n_selected > 1 and self.mode == "or":
            pairs.append(("mode", "or"))
        pairs += [("k", self.k), ("kx", self.kx)] + list(extra.items())
        return urlencode([(k, v) for k, v in pairs if v not in ("", None)])

    @property
    def n_selected(self) -> int:
        return len(self.items) + len(self.kubuns)

    @property
    def selected_keys(self) -> list[str]:
        """検索フォームのチップ（届出項目は i+id、区分は b+id）。"""
        return [f"i{i}" for i in self.items] + [f"b{i}" for i in self.kubuns]

    @property
    def empty(self) -> bool:
        return not (self.prefs or self.ftypes or self.q or self.items or self.kubuns or self.k or self.kx)


def _search_sql(con, c: Cond) -> tuple[str, list, str, list]:
    """(施設のWHERE句, パラメータ, 該当届出のWHERE句, パラメータ) を返す。

    q  … 医療機関名または住所の一部
    k  … 施設基準名称。既存の名称と完全に一致すれば（候補リストから選んだ場合）その1つだけ、
          そうでなければ名称の一部として、一致する施設基準のどれかを届け出ている施設を探す
    kx … 施設基準名称の完全一致（施設基準ページからのCSVリンク用）
    items … 届出項目（届出名称×受理記号）
    kubuns … 入院基本料の区分（公式の区分欄、無い地域は病床機能報告からの参考値）
             届出項目・区分は合わせて、mode が and ならすべて、or ならいずれかに当てはまる施設
    """
    k, kx = c.k, c.kx
    if k and not kx and con.execute("SELECT 1 FROM kijun WHERE name = ?", (k,)).fetchone():
        kx = k
    where, params = [], []
    if c.prefs:
        where.append(f"f.pref_code IN ({','.join('?' * len(c.prefs))})")
        params += c.prefs
    if c.pref:
        if c.areas:
            where.append(f"f.area IN ({','.join('?' * len(c.areas))})")
            params += c.areas
        if c.cities:
            where.append(f"f.city IN ({','.join('?' * len(c.cities))})")
            params += c.cities
    if c.ftypes:
        where.append(f"f.ftype IN ({','.join('?' * len(c.ftypes))})")
        params += c.ftypes
    if c.q:
        key = f"%{search_key(c.q)}%"
        where.append("(f.name_search LIKE ? OR f.address_search LIKE ?)")
        params += [key, key]
    sel, sel_params = [], []
    if c.items:
        marks = ",".join("?" * len(c.items))
        if c.mode == "or" or len(c.items) == 1:
            sel.append(f"f.id IN (SELECT facility_id FROM filings WHERE item_id IN ({marks}))")
            sel_params += c.items
        else:
            sel.append(f"""f.id IN (SELECT facility_id FROM filings WHERE item_id IN ({marks})
                                    GROUP BY facility_id HAVING COUNT(DISTINCT item_id) = ?)""")
            sel_params += c.items + [len(c.items)]
    if c.kubuns:
        if c.mode == "or":
            sel.append(f"f.id IN (SELECT facility_id FROM fac_kubun WHERE kubun_id IN ({','.join('?' * len(c.kubuns))}))")
            sel_params += c.kubuns
        else:
            for kb in c.kubuns:
                sel.append("f.id IN (SELECT facility_id FROM fac_kubun WHERE kubun_id = ?)")
                sel_params.append(kb)
    if sel:
        where.append("(" + (" OR " if c.mode == "or" else " AND ").join(sel) + ")")
        params += sel_params
    kwhere, kparams = "", []
    if kx:
        kwhere, kparams = "x.kijun_id IN (SELECT id FROM kijun WHERE name = ?)", [kx]
    elif k:
        kwhere, kparams = "x.kijun_id IN (SELECT id FROM kijun WHERE name_search LIKE ?)", [f"%{search_key(k)}%"]
        if not con.execute("SELECT 1 FROM kijun WHERE name_search LIKE ?", kparams).fetchone():
            # 施設基準の名称に無い語でも、区分（急性期一般入院料１など）に当てはまればそちらで探す
            kb_ids = [r[0] for r in con.execute("SELECT id FROM kubuns WHERE search LIKE ?", kparams)]
            if kb_ids:
                where.append(f"f.id IN (SELECT facility_id FROM fac_kubun WHERE kubun_id IN ({','.join('?' * len(kb_ids))}))")
                params += kb_ids
                return " AND ".join(where), params, "", []
    if kwhere:
        # 「施設ごとに届出を確認する」(EXISTS)形だと、「加算」のように多くの施設基準に
        # 当てはまる語で全国9万施設を1件ずつ調べて1.6秒かかった。先に施設基準→施設の
        # 索引(ix_fil_kijun)で該当施設を集めてから絞る形にする。
        where.append(f"f.id IN (SELECT x.facility_id FROM filings x WHERE {kwhere})")
        params += kparams
    return (" AND ".join(where) or "1"), params, kwhere, kparams


def _form_ctx(con) -> dict:
    """検索フォームに要るもの。届出項目（約1,300件）はページにJSONで埋め込み、入力候補と
    「一覧から選ぶ」をブラウザ側で組み立てる（gzipで20KB程度）。"""
    items = con.execute(
        """SELECT i.id, i.symbol, i.section, kj.name AS kijun, i.n_facilities
           FROM items i JOIN kijun kj ON kj.id = i.kijun_id ORDER BY i.id"""
    ).fetchall()
    item_data = [
        {"id": r["id"], "s": r["symbol"] or "", "n": r["kijun"], "c": r["n_facilities"], "g": r["section"],
         "k": search_key(f'{r["symbol"]} {r["kijun"]}')}
        for r in items
    ]
    sections = list(dict.fromkeys(r["section"] for r in items))
    for i in item_data:
        i["id"] = f"i{i['id']}"
    # 入院基本料の区分も候補に出す（「一覧から選ぶ」には出さない。g が空）
    item_data += [
        {"id": f"b{r['id']}", "s": "区分", "n": r["name"], "p": r["kijun"], "c": r["n_facilities"], "g": "",
         "k": r["search"]}
        for r in _kubun_rows(con)
    ]
    return {"prefs": _prefs(con), "item_data": item_data, "item_sections": sections}


def _kubun_rows(con, ids: list[int] | None = None) -> list[sqlite3.Row]:
    sql = """SELECT b.id, b.name, b.search, b.n_facilities, b.n_ref, kj.name AS kijun
             FROM kubuns b JOIN kijun kj ON kj.id = b.kijun_id"""
    if ids is None:
        return con.execute(sql + " ORDER BY b.id").fetchall()
    if not ids:
        return []
    rows = con.execute(sql + f" WHERE b.id IN ({','.join('?' * len(ids))})", ids).fetchall()
    order = {i: n for n, i in enumerate(ids)}
    return sorted(rows, key=lambda r: order[r["id"]])


def _kubun_hits(con, kb_ids: list[int], where: str, params: list,
                fac_ids: list[str] | None = None) -> dict[str, dict[int, sqlite3.Row]]:
    """施設ごとに、選んだ区分のうち当てはまるもの（ref=1 なら病床機能報告からの参考値）。"""
    if not kb_ids:
        return {}
    sql = f"SELECT facility_id, kubun_id, ref, start_date FROM fac_kubun WHERE kubun_id IN ({','.join('?' * len(kb_ids))})"
    args: list = list(kb_ids)
    if fac_ids is not None:
        sql += f" AND facility_id IN ({','.join('?' * len(fac_ids))})"
        args += fac_ids
    else:
        sql += f" AND facility_id IN (SELECT f.id FROM facilities f WHERE {where})"
        args += params
    hits: dict[str, dict[int, sqlite3.Row]] = {}
    for r in con.execute(sql, args):
        hits.setdefault(r["facility_id"], {})[r["kubun_id"]] = r
    return hits


# トップの「こんな探し方ができます」。周辺ビジネス（製薬・医療機器の営業、人材紹介、コンサル）がよく
# 調べそうな組み合わせを、実際の検索URLと件数つきで出す。施設基準名は部分一致（k）、区分は名前で引く。
EXAMPLES = [
    {"label": "地域包括ケア病棟のある病院", "pref": "東京都", "k": "地域包括ケア病棟入院料", "ftype": "病院"},
    {"label": "回復期リハビリテーション病棟のある病院", "pref": "大阪府", "k": "回復期リハビリテーション病棟入院料", "ftype": "病院"},
    {"label": "急性期一般入院料１の病院", "pref": "", "kubun": "急性期一般入院料１"},
    {"label": "在宅療養支援診療所", "pref": "福岡県", "k": "在宅療養支援診療所"},
    {"label": "外来腫瘍化学療法診療料を届け出ている医療機関", "pref": "愛知県", "k": "外来腫瘍化学療法診療料"},
    {"label": "緩和ケア病棟のある病院", "pref": "", "k": "緩和ケア病棟入院料"},
]
_examples_cache: dict[str, list[dict]] = {}


def _examples(con) -> list[dict]:
    built = _meta(con).get("built_at", "")
    if built in _examples_cache:
        return _examples_cache[built]
    prefs = {r["name"]: r["code"] for r in _prefs(con)}
    out = []
    for ex in EXAMPLES:
        c = Cond(pref=prefs.get(ex["pref"], ""), k=ex.get("k", ""), ftypes=[ex["ftype"]] if ex.get("ftype") else [])
        if ex.get("kubun"):
            r = con.execute("SELECT id FROM kubuns WHERE name = ?", (ex["kubun"],)).fetchone()
            if not r:
                continue
            c.kubuns = [r["id"]]
        where, params, _, _ = _search_sql(con, c)
        n = con.execute(f"SELECT COUNT(*) FROM facilities f WHERE {where}", params).fetchone()[0]
        if n:
            out.append({"label": ex["label"], "pref": ex["pref"] or "全国", "n": n, "url": "/search?" + c.query()})
    _examples_cache.clear()
    _examples_cache[built] = out
    return out


REGIONS = [("北海道・東北", 1, 7), ("関東", 8, 14), ("中部", 15, 23), ("近畿", 24, 30),
           ("中国", 31, 35), ("四国", 36, 39), ("九州・沖縄", 40, 47)]


def _cond_table_ctx(con) -> dict:
    """トップの条件表（都道府県・届出項目をチェックボックスで並べる）に要るもの。サーバー側で
    全部描くので、JavaScript が無くても選んで検索できる。"""
    prefs = _prefs(con)
    regions = [(name, [p for p in prefs if lo <= int(p["code"]) <= hi]) for name, lo, hi in REGIONS]
    rows = con.execute(
        """SELECT i.id, i.symbol, i.section, kj.name AS kijun, i.n_facilities
           FROM items i JOIN kijun kj ON kj.id = i.kijun_id ORDER BY i.id"""
    ).fetchall()
    sections: dict[str, list] = {}
    for r in rows:
        sections.setdefault(r["section"], []).append(r)
    kubuns = [r for r in _kubun_rows(con) if r["n_facilities"]]
    return {"regions": regions, "sections": list(sections.items()), "kubun_rows": kubuns, "ftypes": FTYPES}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    cond = Cond.from_request(request)   # 検索結果の「条件を変更する」から来た時は、選んだ条件を入れた状態で開く
    with db() as con:
        ctx = _cond_table_ctx(con)
        examples = _examples(con)
        type_counts = {r["ftype"]: r["n"] for r in con.execute("SELECT ftype, COUNT(*) AS n FROM facilities GROUP BY ftype")}
    return render(request, "index.html", cond=cond, examples=examples, type_counts=type_counts,
                  sel_items=set(cond.items), sel_kubuns=set(cond.kubuns), **ctx)


@app.get("/facilities/suggest.json")
def facility_suggest(q: str = "", pref: str = ""):
    """トップの「医療機関を調べる」の入力候補（名前の一部、病院・有床診療所を先に）。"""
    key = search_key(q)
    if len(key) < 2:
        return JSONResponse([])
    sql = """SELECT id, name, pref_name, city, ftype, n_kijun FROM facilities WHERE name_search LIKE ?"""
    args: list = [f"%{key}%"]
    if pref:
        sql += " AND pref_code = ?"
        args.append(pref)
    sql += """ ORDER BY CASE WHEN name_search LIKE ? THEN 0 ELSE 1 END,
                        CASE ftype WHEN '病院' THEN 0 WHEN '有床診療所' THEN 1 ELSE 2 END, n_kijun DESC LIMIT 12"""
    args.append(f"{key}%")
    with db() as con:
        rows = con.execute(sql, args).fetchall()
    return JSONResponse([{"url": facility_url(r["id"]), "name": r["name"], "where": f'{r["pref_name"]} {r["city"] or ""}'.strip(),
                          "ftype": r["ftype"], "n": r["n_kijun"]} for r in rows])


@app.get("/areas/{pref}.json")
def areas_json(pref: str):
    """検索フォームで都道府県を選んだ時に出す二次医療圏と市区町村。"""
    with db() as con:
        rows = con.execute(
            "SELECT area, city, n FROM areas WHERE pref_code = ? ORDER BY area_order, area, city_order",
            (pref,),
        ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        a = out.setdefault(r["area"], {"name": r["area"], "n": 0, "cities": []})
        a["n"] += r["n"]
        a["cities"].append({"name": r["city"], "n": r["n"]})
    return JSONResponse(list(out.values()), headers={"Cache-Control": "public, max-age=3600"})


def _selected_items(con, ids: list[int]) -> list[sqlite3.Row]:
    if not ids:
        return []
    rows = con.execute(
        f"""SELECT i.id, i.symbol, i.section, kj.name AS kijun FROM items i JOIN kijun kj ON kj.id = i.kijun_id
            WHERE i.id IN ({','.join('?' * len(ids))})""",
        ids,
    ).fetchall()
    order = {i: n for n, i in enumerate(ids)}
    return sorted(rows, key=lambda r: order[r["id"]])


def _item_hits(con, item_ids: list[int], where: str, params: list, fac_ids: list[str] | None = None) -> dict[str, set]:
    """施設ごとに、選んだ届出項目のうち届け出ているもの。"""
    if not item_ids:
        return {}
    sql = f"SELECT facility_id, item_id FROM filings WHERE item_id IN ({','.join('?' * len(item_ids))})"
    args: list = list(item_ids)
    if fac_ids is not None:
        sql += f" AND facility_id IN ({','.join('?' * len(fac_ids))})"
        args += fac_ids
    else:
        sql += f" AND facility_id IN (SELECT f.id FROM facilities f WHERE {where})"
        args += params
    hits: dict[str, set] = {}
    for fid, iid in con.execute(sql, args):
        hits.setdefault(fid, set()).add(iid)
    return hits


def _sel_columns(con, c: Cond, where: str, params: list, fac_ids: list[str] | None = None):
    """選んだ届出項目・区分を結果の列にする。戻り値は (列の一覧, 施設ごとの {列key: 印})。
    印は "ok"（届け出ている／公式の区分）、"ref"（病床機能報告からの参考値）、
    "ref_changed"・"ref_likely_changed"（参考値だが、その後に届出が出し直されている）。"""
    cols = [{"key": f"i{r['id']}", "head": r["symbol"], "title": r["kijun"], "label": f"{r['symbol']}（{r['kijun']}）",
             "kubun": False} for r in _selected_items(con, c.items)]
    kbs = _kubun_rows(con, c.kubuns)
    cols += [{"key": f"b{r['id']}", "head": r["name"], "title": f"{r['kijun']}の区分",
              "label": f"区分：{r['name']}（{r['kijun']}）", "kubun": True} for r in kbs]
    marks: dict[str, dict[str, str]] = {}
    if fac_ids == []:
        return cols, marks
    for fid, got in _item_hits(con, c.items, where, params, fac_ids).items():
        marks.setdefault(fid, {}).update({f"i{i}": "ok" for i in got})
    ref_date = _meta(con).get("kubun_ref_date")
    kb_of = {r["id"]: r for r in kbs}
    for fid, got in _kubun_hits(con, c.kubuns, where, params, fac_ids).items():
        m = marks.setdefault(fid, {})
        for kid, h in got.items():
            if not h["ref"]:
                m[f"b{kid}"] = "ok"
            else:
                note = kubun_ref_note(kb_of[kid]["kijun"] + kb_of[kid]["name"], h["start_date"], ref_date)
                m[f"b{kid}"] = f"ref_{note}" if note else "ref"
    return cols, marks


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, page: int = Query(1, ge=1)):
    c = Cond.from_request(request)
    if c.kx and not c.k:
        c.k, c.kx = c.kx, ""
    with db() as con:
        where, params, kwhere, kparams = _search_sql(con, c)
        total = con.execute(f"SELECT COUNT(*) FROM facilities f WHERE {where}", params).fetchone()[0]
        pages = max(1, math.ceil(total / PAGE_SIZE))
        page = min(page, pages)
        rows = con.execute(
            f"""SELECT f.* FROM facilities f WHERE {where}
                ORDER BY f.pref_code, CASE f.ftype WHEN '病院' THEN 0 WHEN '有床診療所' THEN 1 ELSE 2 END, f.name
                LIMIT ? OFFSET ?""",
            params + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
        ).fetchall()
        ids = [r["id"] for r in rows]
        sel_cols, marks = _sel_columns(con, c, where, params, ids)
        hits: dict[str, list[str]] = {}
        if kwhere and ids:
            for h in con.execute(
                f"""SELECT DISTINCT x.facility_id, kj.name FROM filings x JOIN kijun kj ON kj.id = x.kijun_id
                    WHERE x.facility_id IN ({','.join('?' * len(ids))}) AND {kwhere}
                    ORDER BY kj.n_facilities DESC""",
                ids + kparams,
            ):
                hits.setdefault(h["facility_id"], []).append(h["name"])
        matched_kijun = []
        exact_k = bool(c.k) and con.execute("SELECT 1 FROM kijun WHERE name = ?", (c.k,)).fetchone() is not None
        matched_kubun = []
        if c.k and not c.kx and not exact_k:
            matched_kijun = con.execute(
                "SELECT name, n_facilities FROM kijun WHERE name_search LIKE ? ORDER BY n_facilities DESC LIMIT 30",
                [f"%{search_key(c.k)}%"],
            ).fetchall()
            if not matched_kijun:
                matched_kubun = con.execute(
                    """SELECT b.id, b.name, kj.name AS kijun, b.n_facilities FROM kubuns b JOIN kijun kj ON kj.id = b.kijun_id
                       WHERE b.search LIKE ? ORDER BY b.n_facilities DESC""",
                    [f"%{search_key(c.k)}%"],
                ).fetchall()
        ctx = {"prefs": _prefs(con)}
    pref_name = "・".join(p["name"] for p in ctx["prefs"] if p["code"] in c.prefs)
    return render(
        request, "search.html",
        rows=rows, hits=hits, sel_cols=sel_cols, marks=marks, total=total, page=page, pages=pages,
        pref_name=pref_name, cond=c, matched_kijun=matched_kijun, matched_kubun=matched_kubun,
        has_kubun=any(col["kubun"] for col in sel_cols) or bool(matched_kubun),
        csv_url="/search.csv?" + c.query(),
        page_url=lambda p: "/search?" + c.query(page=p),
        narrow_url=lambda name: "/search?" + Cond(prefs=c.prefs, areas=c.areas, cities=c.cities, ftypes=c.ftypes,
                                                  q=c.q, k=name).query(),
        kubun_url=lambda kid: "/search?" + Cond(prefs=c.prefs, areas=c.areas, cities=c.cities, ftypes=c.ftypes,
                                                q=c.q, kubuns=[kid]).query(),
        **ctx,
    )


FACILITY_COLS = ["都道府県", "医療機関番号", "医療機関名称", "施設種別", "住所", "市区町村", "二次医療圏"]
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
        if any(h.startswith("区分：") for h in header):
            w.writerow(["区分の列：○は厚生局が公表している区分。○（参考）は区分が公表されていない届出（北海道など）"
                        "について、厚生労働省「病床機能報告」（7月1日時点の病棟の入院基本料）を加工して補ったもの。"
                        "区分が分からない届出（北海道の精神病棟・結核病棟・有床診療所など）は結果に含まれません"])
        w.writerow(["出典：各地方厚生局「施設基準の届出受理状況」を加工して作成（MedilenZ）"])
        yield buf.getvalue()

    return StreamingResponse(
        gen(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@app.get("/search.csv")
def search_csv(request: Request):
    """検索結果のCSV。
    - 届出項目を選んだ場合: 医療機関を1行ずつ、選んだ項目ごとに届け出ていれば「○」の列を付ける
    - 施設基準名称（k/kx）で探した場合: 該当する届出を1行ずつ（区分・受理番号なども出す）
    - それ以外: 医療機関を1行ずつ（全届出100万行を一度に出さないため）"""
    c = Cond.from_request(request)
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    where, params, kwhere, kparams = _search_sql(con, c)
    fac_sql = f"""SELECT f.id, f.pref_name, f.code, f.name, f.ftype, f.address, f.city, f.area, f.n_kijun
                  FROM facilities f WHERE {where} ORDER BY f.pref_code, f.name"""
    if c.items or c.kubuns:
        cols, marks = _sel_columns(con, c, where, params)
        header = FACILITY_COLS + [col["label"] for col in cols]
        text = {"ok": "○", "ref": "○（参考）", "ref_changed": "○（参考・出し直しあり）",
                "ref_likely_changed": "○（参考・改定で変更の可能性大）"}
        rows = ([*r][1:8] + [text.get(marks.get(r["id"], {}).get(col["key"], ""), "") for col in cols]
                for r in con.execute(fac_sql, params))
        name = "届出項目" + "・".join(col["head"] for col in cols[:3]) + ("ほか" if len(cols) > 3 else "")
    elif kwhere:
        header = FACILITY_COLS + FILING_COLS
        rows = con.execute(
            f"""SELECT f.pref_name, f.code, f.name, f.ftype, f.address, f.city, f.area,
                       kj.name, x.symbol, x.juri_no, COALESCE(NULLIF(x.start_date, ''), x.start_raw),
                       x.byoto, x.byosho_kubun, x.n_byoto, x.n_byosho, x.kubun, x.other, x.kubun_ref
                FROM facilities f JOIN filings x ON x.facility_id = f.id JOIN kijun kj ON kj.id = x.kijun_id
                WHERE {where} AND {kwhere}
                ORDER BY f.pref_code, f.name, kj.name""",
            params + kparams,
        )
        name = c.kx or c.k
    else:
        header = FACILITY_COLS + ["届出している施設基準の数"]
        rows = ([*r][1:] for r in con.execute(fac_sql, params))
        name = c.q or "医療機関一覧"
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
        """SELECT f.pref_name, f.code, f.name, f.ftype, f.address, f.city, f.area,
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
        # 厚生局の名簿と同じ順（基本診療料→特掲診療料…、各区分の中は略称一覧の順）に並べる
        filings = con.execute(
            """SELECT kj.name AS kijun, COALESCE(i.section, 'その他の届出') AS section, x.*
               FROM filings x JOIN kijun kj ON kj.id = x.kijun_id LEFT JOIN items i ON i.id = x.item_id
               WHERE x.facility_id = ? ORDER BY COALESCE(i.id, 1e9), x.start_date""",
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


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return render(request, "privacy.html")


@app.get("/operator", response_class=HTMLResponse)
def operator(request: Request):
    return render(request, "operator.html")


@app.get("/contact", response_class=HTMLResponse)
def contact(request: Request):
    return render(request, "contact.html")


@app.get("/guide", response_class=HTMLResponse)
def guide(request: Request):
    with db() as con:
        examples = _examples(con)
    return render(request, "guide.html", examples=examples)


@app.get("/ads.txt", response_class=PlainTextResponse)
def ads_txt():
    """Google AdSense の販売者確認用。ADSENSE_CLIENT（ca-pub-…）が無ければ置かない。"""
    if not ADSENSE_CLIENT:
        raise HTTPException(404)
    pub = ADSENSE_CLIENT.removeprefix("ca-")
    return f"google.com, {pub}, DIRECT, f08c47fec0942fa0\n"


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
            paths = ["/", "/guide", "/kijun", "/about", "/operator", "/contact", "/privacy"] + [f"/pref/{quote(p['name'])}" for p in _prefs(con)]
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
