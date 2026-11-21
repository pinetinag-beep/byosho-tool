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
import re
import sqlite3
import unicodedata
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
from .sources import BASE as KOUSEIKYOKU_BASE, BUREAUS, MHLW_URLS
from .specialties import build as build_specialties
from . import yakkyoku as ymod

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
# AdSenseのサイト運営者ID（公開される値。2026年11月、ユーザーのAdSenseアカウントのもの）
# サーバーの /etc/kijun-web.env には初期設定で「ADSENSE_CLIENT=」（空）の行があるので、空なら既定値を使う
ADSENSE_CLIENT = os.environ.get("ADSENSE_CLIENT", "").strip() or "ca-pub-9626016324824409"
ADSENSE_SLOT = os.environ.get("ADSENSE_SLOT", "").strip()       # 手動で置く広告ユニットのID（無ければ自動広告のみ）
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "").strip()     # プライバシーポリシーの問い合わせ先
# アクセス解析（Google アナリティクス 4）。Streamlit版と同じプロパティ（medilenz.jp のウェブストリーム）。
# 本番のドメインで開いた時だけ送る（IPアドレス直打ち・手元での確認は数えない）
GA_MEASUREMENT_ID = os.environ.get("GA_MEASUREMENT_ID", "G-8Y6SDBSCMQ").strip()
OPERATOR_NAME = "MedilenZ運営事務局"                              # 運営者情報に載せる名前（ユーザー判断、2026年11月）
# お問い合わせはGoogleフォームで受ける（メールアドレスをページに出さないため）。URLが決まったらここに入れる
CONTACT_FORM_URL = os.environ.get("CONTACT_FORM_URL", "https://forms.gle/3ZzbjR1PvCZbqFZi6").strip()
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


def city_url(pref_name: str, city: str) -> str:
    """市区町村の医療機関一覧（/city/京都府/舞鶴市）。政令市は区まで（facilities.city と同じ）。"""
    return f"/city/{quote(pref_name, safe='')}/{quote(city, safe='')}"


def area_url(pref_name: str, area: str) -> str:
    """二次医療圏の病院一覧（/area/京都府/中丹）。"""
    return f"/area/{quote(pref_name, safe='')}/{quote(area, safe='')}"


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
templates.env.globals.update(y_url=ymod.y_url, y_pref_url=ymod.y_pref_url, y_city_url=ymod.y_city_url,
                             y_kijun_url=ymod.y_kijun_url)
app.include_router(ymod.router)   # 薬局を探す（/yakkyoku/…）
templates.env.globals["css_version"] = int((HERE / "static" / "style.css").stat().st_mtime)
templates.env.globals.update(
    kijun_url=kijun_url, facility_url=facility_url, city_url=city_url, area_url=area_url, streamlit_url=streamlit_url, streamlit_home=STREAMLIT_URL,
    site_url=SITE_URL, kouseikyoku_url=KOUSEIKYOKU_URL, pdl_url=PDL_URL,
    mhlw_terms_url=MHLW_TERMS_URL, ftypes=FTYPES, kubun_ref_note=kubun_ref_note,
    adsense_client=ADSENSE_CLIENT, adsense_slot=ADSENSE_SLOT, contact_email=CONTACT_EMAIL, operator_name=OPERATOR_NAME, ga_id=GA_MEASUREMENT_ID,
    contact_form_url=CONTACT_FORM_URL, ad_preview=AD_PREVIEW, mhlw_urls=MHLW_URLS,
)


# Googleアナリティクスのコンテンツグループ（ページの種類）。経路データ探索で「トップ→検索結果→医療機関」のように見るため
GA_PAGE = {
    "index.html": "トップ", "search.html": "検索結果", "facility.html": "医療機関",
    "kijun_index.html": "施設基準の一覧", "kijun.html": "施設基準", "kijun_pref.html": "施設基準×都道府県",
    "pref.html": "都道府県", "guide.html": "使い方", "about.html": "データについて",
    "city.html": "市区町村", "area.html": "二次医療圏", "specialty.html": "診療科から探す",
    "y_index.html": "薬局トップ", "y_search.html": "薬局の検索結果", "y_facility.html": "薬局",
    "y_kijun.html": "薬局の施設基準", "y_pref.html": "薬局×都道府県", "y_city.html": "薬局×市区町村",
    # 歯科（/shika）も同じ y_*.html を使い、render() で「薬局」を「歯科」に置き換える
}


def render(request: Request, template: str, status_code: int = 200, **ctx) -> HTMLResponse:
    with db() as con:
        ctx.setdefault("meta", _meta(con))
    ga = GA_PAGE.get(template, "その他")
    if template.startswith("y_") and ctx.get("K") is not None and ctx["K"].x != "y":   # 歯科も同じテンプレート
        ga = ga.replace("薬局", "歯科")
    ctx.setdefault("ga_page", ga)
    return templates.TemplateResponse(request, template, ctx, status_code=status_code)


# ── 検索 ────────────────────────────────────────────────────

# トップの条件表「病床・設備・実績（病院）」。hstats の列と画面の表示名
H_FUNCS = [("hk", "b_hk", "高度急性期"), ("ak", "b_ak", "急性期"), ("kf", "b_kf", "回復期"), ("mk", "b_mk", "慢性期")]
H_EQUIP = [("ct", "ct", "CT"), ("ct64", "ct64", "CT（64列以上）"), ("mri", "mri", "MRI"), ("mri3t", "mri3t", "MRI（3T以上）"),
           ("pet", "pet", "PET（PET-CT・PET-MRIを含む）"), ("angio", "angio", "血管連続撮影装置"),
           ("robot", "robot", "手術支援ロボット（ダビンチ等）"), ("imrt", "imrt", "IMRT（強度変調放射線治療）")]
H_NUMS = [  # (パラメータ, hstats の列, 表示名, 単位)
    ("amb", "amb", "救急車の受入件数", "件"), ("dpc", "dpc_total", "DPC退院患者数", "件"),
    ("dsurg", "dpc_surg", "DPC手術ありの件数", "件"), ("ref", "referral", "紹介率", "%"),
]


def _pos_num(v: str | None) -> float | None:
    try:
        x = float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return x if x >= 0 else None


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
    # 病床・設備・実績（病院。病床機能報告・DPC・外来機能報告の最新年度の値で絞り込む）
    bmin: float | None = None  # 許可病床数の下限
    bmax: float | None = None  # 許可病床数の上限
    funcs: list[str] = field(default_factory=list)   # 持っている病床機能（H_FUNCS のキー。いずれも持つ）
    eqs: list[str] = field(default_factory=list)     # 持っている設備（H_EQUIP のキー。いずれも持つ）
    nums: dict = field(default_factory=dict)         # 実績の下限（H_NUMS のキー → 値）

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
        # item=12.13.14 のように「.」でまとめた値も受け付ける（「診療科から探す」の1行＝複数の届出項目）
        items = [int(x) for v in qp.getlist("item") for x in v.split(".") if x.isdigit()]
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
            bmin=_pos_num(qp.get("bmin")),
            bmax=_pos_num(qp.get("bmax")),
            funcs=[v for v in dict.fromkeys(qp.getlist("fn")) if v in {k for k, _, _ in H_FUNCS}],
            eqs=[v for v in dict.fromkeys(qp.getlist("eq")) if v in {k for k, _, _ in H_EQUIP}],
            nums={k: n for k, _, _, _ in H_NUMS if (n := _pos_num(qp.get(k))) is not None},
        )

    def query(self, **extra) -> str:
        pairs = [("pref", p) for p in self.prefs] + [("area", a) for a in self.areas] + [("city", c) for c in self.cities]
        pairs += [("ftype", t) for t in self.ftypes] + [("q", self.q)] + [("item", i) for i in self.items]
        pairs += [("kb", i) for i in self.kubuns]
        if self.n_selected > 1 and self.mode == "or":
            pairs.append(("mode", "or"))
        pairs += [("k", self.k), ("kx", self.kx)]
        pairs += [("bmin", _fmt_num(self.bmin)), ("bmax", _fmt_num(self.bmax))]
        pairs += [("fn", f) for f in self.funcs] + [("eq", e) for e in self.eqs]
        pairs += [(k, _fmt_num(v)) for k, v in self.nums.items()] + list(extra.items())
        return urlencode([(k, v) for k, v in pairs if v not in ("", None)])

    @property
    def n_selected(self) -> int:
        return len(self.items) + len(self.kubuns)

    @property
    def selected_keys(self) -> list[str]:
        """検索フォームのチップ（届出項目は i+id、区分は b+id）。"""
        return [f"i{i}" for i in self.items] + [f"b{i}" for i in self.kubuns]

    @property
    def has_hosp(self) -> bool:
        """病床・設備・実績の条件があるか。"""
        return bool(self.bmin is not None or self.bmax is not None or self.funcs or self.eqs or self.nums)

    @property
    def hosp_labels(self) -> list[str]:
        """検索結果の「条件」欄に出す文言。"""
        out = []
        if self.bmin is not None or self.bmax is not None:
            lo = f"{_fmt_num(self.bmin)}床以上" if self.bmin is not None else ""
            hi = f"{_fmt_num(self.bmax)}床以下" if self.bmax is not None else ""
            out.append("許可病床数 " + "・".join(x for x in (lo, hi) if x))
        names = dict((k, n) for k, _, n in H_FUNCS)
        out += [f"{names[f]}の病床あり" for f in self.funcs]
        names = dict((k, n) for k, _, n in H_EQUIP)
        out += [f"{names[e]}あり" for e in self.eqs]
        for k, _, n, unit in H_NUMS:
            if k in self.nums:
                out.append(f"{n} {_fmt_num(self.nums[k])}{unit}以上")
        return out

    @property
    def empty(self) -> bool:
        return not (self.prefs or self.ftypes or self.q or self.items or self.kubuns or self.k or self.kx
                    or self.has_hosp)


def _fmt_num(v: float | None) -> str:
    if v is None:
        return ""
    return str(int(v)) if float(v).is_integer() else str(v)


def _hosp_sql(c: Cond) -> tuple[str, list]:
    """病床・設備・実績の条件（すべて満たす）。hstats に無い医療機関（病床機能報告に無い診療所など）は外れる。"""
    w, p = [], []
    if c.bmin is not None:
        w.append("beds >= ?"); p.append(c.bmin)
    if c.bmax is not None:
        w.append("beds <= ?"); p.append(c.bmax)
    cols = dict((k, col) for k, col, _ in H_FUNCS)
    w += [f"{cols[f]} > 0" for f in c.funcs]
    cols = dict((k, col) for k, col, _ in H_EQUIP)
    w += [f"{cols[e]} > 0" for e in c.eqs]
    cols = dict((k, col) for k, col, _, _ in H_NUMS)
    for k, v in c.nums.items():
        w.append(f"{cols[k]} >= ?"); p.append(v)
    return " AND ".join(w), p


def _hosp_cols(c: Cond) -> list[dict]:
    """検索結果・CSVに足す列（条件に使った値。CLAUDE.md「検索条件に使ったデータは結果とCSVに出す」）。"""
    if not c.has_hosp:
        return []
    cols = [{"col": "beds", "label": "許可病床数", "unit": "床"}]
    cols += [{"col": col, "label": f"{n}の病床数", "unit": "床"} for k, col, n in H_FUNCS if k in c.funcs]
    cols += [{"col": col, "label": n, "unit": "台"} for k, col, n in H_EQUIP if k in c.eqs]
    cols += [{"col": col, "label": n, "unit": unit} for k, col, n, unit in H_NUMS if k in c.nums]
    return cols


def _hosp_values(con, ids: list[str] | None, where: str = "", params: list | None = None) -> dict[str, sqlite3.Row]:
    if ids is not None:
        if not ids:
            return {}
        rows = con.execute(f"SELECT * FROM hstats WHERE facility_id IN ({','.join('?' * len(ids))})", ids)
    else:
        rows = con.execute(f"SELECT h.* FROM hstats h JOIN facilities f ON f.id = h.facility_id WHERE {where}", params or [])
    return {r["facility_id"]: r for r in rows}


def _hosp_fmt(v, unit: str) -> str:
    if v is None:
        return ""
    if unit == "%":
        return f"{v:.1f}"
    return f"{int(v):,}" if float(v).is_integer() else str(v)


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
    if c.has_hosp:
        hw, hp = _hosp_sql(c)
        where.append(f"f.id IN (SELECT facility_id FROM hstats WHERE {hw})")
        params += hp
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


# 入院基本料の区分の並び順：入院基本料の種類ごとにまとめ、その中は看護配置の基準が高い（手厚い）順。
KUBUN_GROUP_ORDER = ["一般病棟入院基本料", "特定機能病院入院基本料", "専門病院入院基本料", "療養病棟入院基本料",
                     "障害者施設等入院基本料", "精神病棟入院基本料", "結核病棟入院基本料",
                     "有床診療所入院基本料", "有床診療所療養病床入院基本料"]
_SPECIAL_KUBUN = ("特別入院基本料", "超過減算", "経過措置", "注11")


def kubun_sort_key(kijun: str, name: str) -> tuple:
    """区分を看護の基準が高い順に並べるキー。
    - 「N対1」はNが小さいほど上（７対１ → １０対１ → １３対１ …）
    - 一般病棟：急性期病院Ａ → 急性期病院Ｂ → 急性期一般入院料１〜６ → 地域一般入院料１〜３
    - 同じ配置なら 急性期病院Ａ → Ｂ → Ｃ → 記号なし（特定機能病院・精神病棟）
    - 番号付き（入院料１・入院基本料１など）は番号が小さいほど上
    - 特別入院基本料・夜勤時間特別入院基本料・超過減算・経過措置などの例外は最後"""
    n = unicodedata.normalize("NFKC", name)
    special = 1 if any(w in n for w in _SPECIAL_KUBUN) else 0
    m = re.search(r"(\d+)対1", n)
    ratio = int(m.group(1)) if m else 99
    letter = next((i for i, c in enumerate("ABC") if re.search(rf"(病院|特定機能病院){c}", n)), 3)
    tier = 50
    if (m2 := re.search(r"急性期一般入院料(\d+)", n)):
        tier = 10 + int(m2.group(1))
    elif (m2 := re.search(r"地域一般入院料(\d+)", n)):
        tier = 20 + int(m2.group(1))
    elif n.startswith("急性期病院"):
        tier = 0
    elif "割" in n:
        tier = 60
    num_m = re.search(r"入院(基本)?料(\d+)$", n)
    num = int(num_m.group(2)) if num_m else 40
    # 療養病棟は令和6年改定後の「入院料１・２」が今の名前。旧名の「入院基本料１・２」は後ろに
    old = 1 if kijun == "療養病棟入院基本料" and "入院基本料" in n else 0
    return (special, ratio, tier, letter, old, num, n)


def _kubun_groups(con) -> list[tuple[str, list]]:
    rows = [r for r in _kubun_rows(con) if r["n_facilities"]]
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["kijun"], []).append(r)
    order = {k: i for i, k in enumerate(KUBUN_GROUP_ORDER)}
    return [(k, sorted(v, key=lambda r: kubun_sort_key(k, r["name"])))
            for k, v in sorted(groups.items(), key=lambda kv: order.get(kv[0], 99))]


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
    return {"regions": regions, "sections": list(sections.items()), "kubun_groups": _kubun_groups(con), "ftypes": FTYPES}


def _hosp_counts(con) -> dict[str, int]:
    """条件表の病床機能・設備のチェックボックスに添える件数（その病床・設備がある病院の数）。"""
    try:
        cols = [col for _, col, _ in H_FUNCS] + [col for _, col, _ in H_EQUIP]
        r = con.execute("SELECT " + ", ".join(f"SUM({c} > 0) AS {c}" for c in cols) + ", COUNT(*) AS n FROM hstats").fetchone()
    except sqlite3.OperationalError:
        return {}
    return {k: r[k] or 0 for k in r.keys()}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    cond = Cond.from_request(request)   # 検索結果の「条件を変更する」から来た時は、選んだ条件を入れた状態で開く
    with db() as con:
        ctx = _cond_table_ctx(con)
        examples = _examples(con)
        type_counts = {r["ftype"]: r["n"] for r in con.execute("SELECT ftype, COUNT(*) AS n FROM facilities GROUP BY ftype")}
        hosp_counts = _hosp_counts(con)
        n_other = {k.x: ymod._count(con, k) for k in ymod.KINDS}   # 探す対象のタブに出す薬局・歯科の件数
    return render(request, "index.html", cond=cond, n_other=n_other, examples=examples, type_counts=type_counts, hosp_counts=hosp_counts,
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
        hosp_cols, hosp_vals = _hosp_cols(c), _hosp_values(con, ids) if c.has_hosp else {}
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
        rows=rows, hits=hits, sel_cols=sel_cols, marks=marks, total=total,
        hosp_cols=hosp_cols, hosp_vals=hosp_vals, hosp_fmt=_hosp_fmt, page=page, pages=pages,
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


def _csv_response(header: list[str], rows, filename: str, hosp_years: dict | None = None) -> StreamingResponse:
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
            w.writerow(["区分の列：○は厚生局が公表している区分。○（参考）は区分が公表されていない一部の届出"
                        "について、厚生労働省「病床機能報告」（7月1日時点の病棟の入院基本料）を加工して補ったもの。"
                        "区分が分からない届出は結果に含まれません"])
        if hosp_years:
            by, dy, gy = (hosp_years.get(f"hstats_{k}_year", "") for k in ("byosho", "dpc", "gairai"))
            w.writerow([f"病床・設備・救急車の受入件数：厚生労働省「病床機能報告」（{reiwa(int(by)) if by else ''}）、"
                        f"DPC：厚生労働省「{dpc_source(int(dy)) if dy else 'DPC退院患者調査'}」、"
                        f"紹介率：厚生労働省「外来機能報告」（{reiwa(int(gy)) if gy else ''}）を加工して作成。"
                        "病床機能報告に載っていない医療機関（多くの診療所）は、これらの条件では結果に含まれません"])
        w.writerow(["出典：各地方厚生局「施設基準の届出受理状況」を加工して作成（MedilenZ）。"
                    f"各厚生局のページのURLと利用した日は {SITE_URL}/about#sources"])
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
    if c.has_hosp:
        # 病床・設備・実績の条件に使った値を列に足す（医療機関ごとの行の時）
        hcols = _hosp_cols(c)
        hv = _hosp_values(con, None, where, params)
        ids_sql = f"SELECT f.id FROM facilities f WHERE {where} ORDER BY f.pref_code, f.name"
        if c.items or c.kubuns or not kwhere:
            ids = [r[0] for r in con.execute(ids_sql, params)]
            header = header + [f"{h['label']}（{h['unit']}）" for h in hcols]
            rows = ([*r] + [_csv_num(hv.get(fid), h["col"]) for h in hcols] for fid, r in zip(ids, rows))
    return _csv_response(header, rows, f"施設基準届出_{name}.csv", hosp_years=_hosp_years(con) if c.has_hosp else None)


def _csv_num(r, col):
    if r is None or r[col] is None:
        return ""
    v = r[col]
    return round(v, 1) if col == "referral" else int(v)


def _hosp_years(con) -> dict[str, str]:
    m = {r[0]: r[1] for r in con.execute("SELECT key, value FROM meta WHERE key LIKE 'hstats_%'")}
    return {k: v for k, v in m.items()}


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
        byosho = _byosho_ctx(con, f["byosho_code"])
        dpc = _dpc_ctx(con, f["byosho_code"])
        gairai = _gairai_ctx(con, fid)
        near = _nearby(con, f)
    return render(request, "facility.html", f=f, filings=filings, pref=pref, byosho=byosho, dpc=dpc, gairai=gairai,
                  near=near)


def _nearby(con, f) -> dict:
    """医療機関ページの下に出す「同じ市区町村の医療機関」「同じ二次医療圏の病院」。
    見る人が近くの医療機関へ進めるようにし、検索エンジンが13万ページをたどれるようにする（内部リンク）。
    病院を先に、その中は許可病床数（病床機能報告）の多い順。"""
    cols = """f.id, f.name, f.ftype, f.n_kijun, h.beds FROM facilities f
              LEFT JOIN hstats h ON h.facility_id = f.id"""
    order = "ORDER BY (f.ftype = '病院') DESC, COALESCE(h.beds, 0) DESC, f.n_kijun DESC, f.id"
    city, area = f["city"], f["area"]
    out = {"city": city, "area": area if area and not area.startswith("（") else None,
           "city_rows": [], "city_n": 0, "area_rows": [], "area_n": 0}
    if city:
        args = (f["pref_code"], city, f["id"])
        out["city_rows"] = con.execute(
            f"SELECT {cols} WHERE f.pref_code = ? AND f.city = ? AND f.id <> ? {order} LIMIT 20", args).fetchall()
        out["city_n"] = con.execute(
            "SELECT COUNT(*) FROM facilities WHERE pref_code = ? AND city = ? AND id <> ?", args).fetchone()[0]
    if out["area"]:
        args = (f["pref_code"], area, city or "", f["id"])
        where = "WHERE f.pref_code = ? AND f.area = ? AND f.city <> ? AND f.id <> ? AND f.ftype = '病院'"
        out["area_rows"] = con.execute(f"SELECT {cols} {where} {order} LIMIT 15", args).fetchall()
        out["area_n"] = con.execute(
            "SELECT COUNT(*) FROM facilities f " + where, args).fetchone()[0]
    return out


# ── 病床機能報告（厚生労働省）。病院ページに出す ─────────────────────
BED_FUNCS = [("高度急性期", "#0B4F40"), ("急性期", "#12886D"), ("回復期", "#3FA587"), ("慢性期", "#79C2AA")]
EQUIPMENT = [  # (列, 表示名)。台数が1以上のものだけ出す
    ("CT台数", "CT"), ("MRI台数", "MRI"), ("血管連続撮影装置台数", "血管連続撮影装置"), ("SPECT台数", "SPECT"),
    ("PET台数", "PET"), ("PETCT台数", "PET-CT"), ("PETMRI台数", "PET-MRI"), ("マンモグラフィ台数", "マンモグラフィ"),
    ("IMRT台数", "IMRT（強度変調放射線治療）"), ("ガンマナイフ台数", "ガンマナイフ"), ("サイバーナイフ台数", "サイバーナイフ"),
    ("内視鏡手術支援機器台数", "内視鏡手術支援機器（ダビンチ等）"),
]
EQUIP_DETAIL = {"CT台数": [("CT_64列以上", "64列以上"), ("CT_16〜64列", "16〜64列"), ("CT_16列未満", "16列未満")],
                "MRI台数": [("MRI_3T以上", "3T以上"), ("MRI_1.5〜3T", "1.5〜3T"), ("MRI_1.5T未満", "1.5T未満")]}


def reiwa(year: int) -> str:
    return f"令和{year - 2018}年度"


def _num(v):
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


def _occupancy(r) -> float | None:
    """病床稼働率（%）＝在棟延べ数 ÷（許可病床数 × 365）× 100。病床機能報告の定義どおりで、
    在棟延べ数は報告年度の前年度1年間の実績、許可病床数は7月1日時点（期間が違うことを注記する）。
    100%を超える値も補正しない（Streamlit版と同じ方針）。"""
    beds, days = _num(r["合計_許可病床数"]), _num(r["合計_在棟延べ数"])
    if not beds or not days:
        return None
    return round(days / (beds * 365) * 100, 1)


def _byosho_ctx(con, code: str | None) -> dict | None:
    if not code:
        return None
    code = str(code).zfill(10)
    rows = con.execute("SELECT * FROM byosho_hosp WHERE code = ? ORDER BY year", (code,)).fetchall()
    if not rows:
        return None
    r = rows[-1]
    total = _num(r["合計_許可病床数"]) or 0
    funcs = [{"name": n, "color": c, "beds": _num(r[f"{n}_許可病床数"]) or 0} for n, c in BED_FUNCS]
    for x in funcs:
        x["pct"] = round(x["beds"] / total * 100, 1) if total else 0
    equipment = []
    for col, label in EQUIPMENT:
        n = _num(r[col])
        if n:
            detail = [f"{lab} {_num(r[c])}台" for c, lab in EQUIP_DETAIL.get(col, []) if _num(r[c])]
            equipment.append({"label": label, "n": n, "detail": "・".join(detail)})
    wards_raw = con.execute(
        """SELECT 機能区分 AS func, 入院基本料 AS fee, 許可病床数 AS beds FROM byosho_ward
           WHERE code = ? AND year = ? ORDER BY
             CASE 機能区分 WHEN '高度急性期' THEN 0 WHEN '急性期' THEN 1 WHEN '回復期' THEN 2 WHEN '慢性期' THEN 3 ELSE 4 END,
             許可病床数 DESC""",
        (code, r["year"]),
    ).fetchall()
    # 病床機能報告は病院の自己申告で、半角カナ（ﾊｲｹｱﾕﾆｯﾄ）などが混じるので表示用にそろえる
    wards = [{"func": w["func"], "fee": unicodedata.normalize("NFKC", w["fee"]) if w["fee"] else w["fee"], "beds": w["beds"]}
             for w in wards_raw]
    trend = [{
        "year": x["year"], "label": reiwa(x["year"]),
        "beds": [_num(x[f"{n}_許可病床数"]) or 0 for n, _ in BED_FUNCS], "total": _num(x["合計_許可病床数"]) or 0,
        "occ": _occupancy(x), "doctors": _num(x["常勤医師数"]), "nurses": _num(x["常勤看護師数"]),
        "ambulance": _num(x["救急搬送件数"]),
    } for x in rows]
    return {
        "year": r["year"], "label": reiwa(r["year"]), "name": r["name"], "total": total, "funcs": funcs,
        "occ": _occupancy(r), "doctors": _num(r["常勤医師数"]), "nurses": _num(r["常勤看護師数"]),
        "ambulance": _num(r["救急搬送件数"]), "equipment": equipment, "wards": wards, "trend": trend,
        "years": f"{reiwa(rows[0]['year'])}〜{reiwa(r['year'])}" if len(rows) > 1 else reiwa(r["year"]),
        "staff": [(lab, _num(r[c])) for c, lab in [("常勤理学療法士数", "理学療法士"), ("常勤作業療法士数", "作業療法士"),
                                                 ("常勤言語聴覚士数", "言語聴覚士"), ("常勤薬剤師数", "薬剤師")] if _num(r[c])],
    }


# ── 外来機能報告（厚生労働省）。病院・診療所のページに出す ─────────────
def _gairai_ctx(con, facility_id: str) -> dict | None:
    try:
        rows = con.execute("SELECT * FROM gairai WHERE facility_id = ? ORDER BY year", (facility_id,)).fetchall()
    except sqlite3.OperationalError:  # 外来機能報告を入れていない古いDB
        return None
    if not rows:
        return None

    def rec(x):
        # 初診患者数が0の年は報告されていないもの（令和4年度は全件0）として、紹介率などを出さない
        has = bool(_num(x["n_first"]))
        return {
            "year": x["year"], "label": reiwa(x["year"]),
            "n_first": _num(x["n_first"]) if has else None, "n_referred": _num(x["n_referred"]) if has else None,
            "n_back": _num(x["n_back"]) if has else None,
            "referral_rate": x["referral_rate"] if has else None, "back_rate": x["back_rate"] if has else None,
            "focus_first": x["focus_first"], "focus_re": x["focus_re"],
        }
    recs = [rec(x) for x in rows]
    shown = [r for r in recs if r["n_first"] is not None or r["focus_first"] is not None]
    if not shown:
        return None
    last = shown[-1]
    focus_ok = (last["focus_first"] is not None and last["focus_re"] is not None
                and last["focus_first"] >= 40 and last["focus_re"] >= 25)
    return {
        **last, "owner": rows[-1]["owner"], "kind": rows[-1]["kind"], "trend": shown, "focus_ok": focus_ok,
        "years": f"{shown[0]['label']}〜{last['label']}" if len(shown) > 1 else last["label"],
    }


# ── DPC（厚生労働省のDPC退院患者調査）。病院ページに出す ───────────────
MDC_LABELS = {  # 主要診断群（MDC）の名称。厚生労働省の定義どおり
    "MDC01": "神経系疾患", "MDC02": "眼科系疾患", "MDC03": "耳鼻咽喉科系疾患", "MDC04": "呼吸器系疾患",
    "MDC05": "循環器系疾患", "MDC06": "消化器系疾患、肝臓・胆道・膵臓疾患", "MDC07": "筋骨格系疾患",
    "MDC08": "皮膚・皮下組織の疾患", "MDC09": "乳房の疾患", "MDC10": "内分泌・栄養・代謝に関する疾患",
    "MDC11": "腎・尿路系疾患及び男性生殖器系疾患", "MDC12": "女性生殖器系疾患及び産褥期疾患・異常妊娠分娩",
    "MDC13": "血液・造血器・免疫臓器の疾患", "MDC14": "新生児疾患、先天性奇形", "MDC15": "小児疾患",
    "MDC16": "外傷・熱傷・中毒", "MDC17": "精神疾患", "MDC18": "その他",
}
DPC_ITEMS = [("件数_手術有", "手術あり"), ("件数_全身麻酔", "全身麻酔"), ("件数_化学療法有", "化学療法あり"),
             ("件数_放射線療法有", "放射線療法あり"), ("件数_救急車搬送有", "救急車による搬送")]


def dpc_source(year: int) -> str:
    """調査名は令和6年度から「DPCの評価・検証等に係る調査」に変わった（公表資料の名称どおりに書く）。"""
    name = "DPCの評価・検証等に係る調査" if year >= 2024 else "DPC導入の影響評価に係る調査"
    return f"{reiwa(year)}{name}「退院患者調査」"


templates.env.globals.update(reiwa=reiwa, dpc_source=dpc_source, fmt_num=_fmt_num, h_funcs=H_FUNCS, h_equip=H_EQUIP, h_nums=H_NUMS)


def _dpc_kind(t: str | None) -> str:
    t = t or ""
    if "DPC参加" in t:
        return "DPC対象病院"
    if "準備" in t:
        return "DPC準備病院"
    if "出来高" in t:
        return "出来高算定病院（DPCのデータ提出のみ）"
    return t


def _dpc_ctx(con, code: str | None) -> dict | None:
    if not code:
        return None
    code = str(code).zfill(10)
    try:
        rows = con.execute("SELECT * FROM dpc_hosp WHERE code = ? ORDER BY year", (code,)).fetchall()
    except sqlite3.OperationalError:  # DPCを入れていない古いDB
        return None
    rows = [r for r in rows if _num(r["件数_総数"])]
    if not rows:
        return None
    r = rows[-1]
    total = _num(r["件数_総数"])
    items = []
    for col, label in DPC_ITEMS:
        n = _num(r[col])
        items.append({"label": label, "n": n, "pct": round(n / total * 100, 1) if n is not None and total else None})
    mdc = []
    for m in con.execute("SELECT mdc, n_surg, n_nosurg FROM dpc_mdc WHERE code = ? AND year = ?", (code, r["year"])):
        s, ns = _num(m["n_surg"]), _num(m["n_nosurg"])
        known = (s or 0) + (ns or 0)
        mdc.append({"code": m["mdc"], "label": MDC_LABELS.get(m["mdc"], m["mdc"]), "surg": s, "nosurg": ns,
                    "known": known, "partial": s is None or ns is None})
    mdc.sort(key=lambda x: -x["known"])
    top = max((x["known"] for x in mdc), default=0)
    for x in mdc:
        x["w"] = round(x["known"] / top * 100, 1) if top else 0
        x["pct"] = round(x["known"] / total * 100, 1) if total else None
    trend = [{"label": reiwa(x["year"]), "total": _num(x["件数_総数"]),
              **{col: _num(x[col]) for col, _ in DPC_ITEMS}} for x in rows]
    return {
        "year": r["year"], "label": reiwa(r["year"]), "name": r["dpc_name"], "kind": _dpc_kind(r["dpc_type"]),
        "beds": _num(r["dpc_beds"]), "total": total, "procs": items, "mdc": mdc, "trend": trend,
        "sources": "、".join(dpc_source(x["year"]) for x in rows),
    }


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
        # 届け出ている病院（許可病床数の多い順）。医療機関ページへのリンクを兼ねる
        hosp = con.execute(
            """SELECT f.id, f.name, f.city, f.n_kijun, p.name AS pref_name, h.beds, h.dpc_total
               FROM facilities f JOIN prefs p ON p.code = f.pref_code
               LEFT JOIN hstats h ON h.facility_id = f.id
               WHERE f.ftype = '病院' AND f.id IN (SELECT facility_id FROM filings WHERE kijun_id = ?)
               ORDER BY COALESCE(h.beds, 0) DESC, f.n_kijun DESC LIMIT 30""",
            (kj["id"],)).fetchall()
        rel = con.execute(
            """SELECT k.name, r.n, k.n_facilities FROM kijun_rel r JOIN kijun k ON k.id = r.rel_id
               WHERE r.kijun_id = ? ORDER BY r.rk""", (kj["id"],)).fetchall()
    type_counts = {r["ftype"]: r["n"] for r in by_type}
    return render(request, "kijun.html", kj=kj, by_pref=by_pref, type_counts=type_counts, kubun=kubun,
                  hosp=hosp, rel=rel, csv_url=_url("/search.csv", kx=name))



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
        areas = _pref_areas(con, pref["code"])
    return render(request, "pref.html", pref=pref, types={r["ftype"]: r["n"] for r in types}, top=top,
                  hospitals=hospitals, areas=areas)


# ── 市区町村・二次医療圏の一覧（「〇〇市 病院」「〇〇医療圏 病院」の検索に答えるページ）──
_HOSP_COLS = """f.id, f.name, f.ftype, f.city, f.area, f.n_kijun,
                h.beds, h.b_hk, h.b_ak, h.b_kf, h.b_mk, h.dpc_total
                FROM facilities f LEFT JOIN hstats h ON h.facility_id = f.id"""


def _valid_area(area: str | None) -> bool:
    return bool(area) and not area.startswith("（")


@app.get("/city/{pref_name}/{city}", response_class=HTMLResponse)
def city_page(request: Request, pref_name: str, city: str):
    with db() as con:
        pref = _pref_by_name(con, pref_name)
        if not pref:
            raise HTTPException(404)
        rows = con.execute(
            f"""SELECT {_HOSP_COLS} WHERE f.pref_code = ? AND f.city = ?
                ORDER BY (f.ftype = '病院') DESC, COALESCE(h.beds, 0) DESC, f.n_kijun DESC, f.name""",
            (pref["code"], city)).fetchall()
        if not rows:
            raise HTTPException(404)
        areas = sorted({r["area"] for r in rows if _valid_area(r["area"])})
        # 同じ二次医療圏のほかの市区町村（一覧へのリンク）
        others = con.execute(
            """SELECT city, SUM(n) AS n FROM areas WHERE pref_code = ? AND area IN ({}) AND city <> ?
               GROUP BY city ORDER BY MIN(city_order)""".format(",".join("?" * len(areas))),
            (pref["code"], *areas, city)).fetchall() if areas else []
        # 同じ市区町村の歯科診療所・薬局の数（それぞれのページへのリンク）
        sib = []
        for K in ymod.KINDS:
            if ymod._count(con, K):
                n = con.execute(f"SELECT COUNT(*) FROM {K.t('fac')} WHERE pref_code = ? AND city = ?",
                                (pref["code"], city)).fetchone()[0]
                if n:
                    sib.append((K, n))
    hosp = [r for r in rows if r["ftype"] == "病院"]
    clinics = [r for r in rows if r["ftype"] != "病院"]
    return render(request, "city.html", pref=pref, city=city, hosp=hosp, clinics=clinics, areas=areas, others=others,
                  sib=sib)


@app.get("/area/{pref_name}/{area}", response_class=HTMLResponse)
def area_page(request: Request, pref_name: str, area: str):
    with db() as con:
        pref = _pref_by_name(con, pref_name)
        if not pref or not _valid_area(area):
            raise HTTPException(404)
        cities = con.execute(
            """SELECT f.city, COUNT(*) AS n, SUM(f.ftype = '病院') AS n_hosp FROM facilities f
               JOIN (SELECT city, MIN(city_order) AS o FROM areas WHERE pref_code = ? AND area = ? GROUP BY city) a
                 ON a.city = f.city
               WHERE f.pref_code = ? AND f.area = ? GROUP BY f.city ORDER BY MIN(a.o)""",
            (pref["code"], area, pref["code"], area)).fetchall()
        if not cities:
            raise HTTPException(404)
        hosp = con.execute(
            f"""SELECT {_HOSP_COLS} WHERE f.pref_code = ? AND f.area = ? AND f.ftype = '病院'
                ORDER BY COALESCE(h.beds, 0) DESC, f.n_kijun DESC, f.name""",
            (pref["code"], area)).fetchall()
        n_all = sum(c["n"] for c in cities)
    return render(request, "area.html", pref=pref, area=area, cities=cities, hosp=hosp, n_all=n_all)


def _pref_areas(con, pref_code: str) -> list[dict]:
    """都道府県ページの「二次医療圏・市区町村から探す」。二次医療圏ごとに市区町村と医療機関数。"""
    out: dict[str, dict] = {}
    for r in con.execute(
        """SELECT area, city, SUM(n) AS n FROM areas WHERE pref_code = ?
           GROUP BY area, city ORDER BY MIN(area_order), MIN(city_order)""", (pref_code,)):
        if not _valid_area(r["area"]):
            continue
        out.setdefault(r["area"], {"area": r["area"], "cities": []})["cities"].append({"city": r["city"], "n": r["n"]})
    return list(out.values())


_SPEC_CACHE: dict = {}


def _specialties(con) -> list[dict]:
    """診療科ごとの届出（specialties.py）。DBはビルドのたびに置き換わりサイトも再起動するので、プロセス内で1回だけ作る。"""
    key = DB_PATH.stat().st_mtime if DB_PATH.exists() else 0
    if _SPEC_CACHE.get("key") != key:
        _SPEC_CACHE.update(key=key, data=build_specialties(con))
    return _SPEC_CACHE["data"]


@app.on_event("startup")
def _warm_specialties():
    """「診療科から探す」の表（作るのに数秒かかる）を、起動直後に裏で作っておく。"""
    import threading

    def run():
        try:
            with db() as con:
                _specialties(con)
        except Exception:
            pass
    if DB_PATH.exists():
        threading.Thread(target=run, daemon=True).start()


@app.get("/specialty", response_class=HTMLResponse)
def specialty(request: Request):
    """診療科から探す：診療科ごとの届出を説明つきで並べ、チェックした届出を「いずれか」で検索する。"""
    with db() as con:
        specs = _specialties(con)
        prefs = _prefs(con)
    return render(request, "specialty.html", specs=specs, prefs=prefs)


@app.get("/about", response_class=HTMLResponse)
def about(request: Request):
    with db() as con:
        prefs = _prefs(con)
    # 出典の一覧：厚生局ごとに、名簿のページ（URL）・管轄の都道府県・利用した日（名簿を取った日）
    used = {}
    for p in prefs:
        if p["bureau"] and p["used_on_label"]:
            used.setdefault(p["bureau"], set()).add((p["used_on"], p["used_on_label"]))
    bureaus = [{"name": b, "page": title, "url": KOUSEIKYOKU_BASE + path, "prefs": "・".join(ps),
                "used_on": "、".join(lab for _, lab in sorted(used.get(b, [])))}
               for b, title, path, ps in BUREAUS]
    return render(request, "about.html", prefs=prefs, bureaus=bureaus)


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
        n_other = {K.base[1:]: ymod._count(con, K) for K in ymod.KINDS}
    names = ["static", "kijun"] + [f"facilities-{i}" for i in range(math.ceil(n_fac / SITEMAP_CHUNK))]
    for key, n in n_other.items():   # 薬局（yakkyoku）・歯科（shika）
        if n:
            names += [key] + [f"{key}-{i}" for i in range(math.ceil(n / SITEMAP_CHUNK))]
    items = "".join(f"<sitemap><loc>{SITE_URL}/sitemap-{n}.xml</loc></sitemap>" for n in names)
    return _xml(f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</sitemapindex>')


def _other_kind(part: str):
    """サイトマップの名前（yakkyoku・yakkyoku-0・shika・shika-0 …）から薬局・歯科の Kind を返す。"""
    for K in ymod.KINDS:
        key = K.base[1:]
        if part == key or (part.startswith(key + "-") and part[len(key) + 1:].isdigit()):
            return K
    return None


@app.get("/sitemap-{part}.xml")
def sitemap_part(part: str):
    with db() as con:
        if part == "static":
            paths = ["/", "/guide", "/kijun", "/specialty", "/about", "/operator", "/contact", "/privacy"] + [f"/pref/{quote(p['name'])}" for p in _prefs(con)]
            for r in con.execute(
                """SELECT DISTINCT p.name AS pref, f.area, f.city FROM facilities f
                   JOIN prefs p ON p.code = f.pref_code ORDER BY f.pref_code, f.area, f.city"""):
                if r["city"]:
                    paths.append(city_url(r["pref"], r["city"]))
            for r in con.execute(
                """SELECT DISTINCT p.name AS pref, f.area FROM facilities f
                   JOIN prefs p ON p.code = f.pref_code ORDER BY f.pref_code, f.area"""):
                if _valid_area(r["area"]):
                    paths.append(area_url(r["pref"], r["area"]))
        elif part == "kijun":
            paths = [kijun_url(r["name"]) for r in con.execute("SELECT name FROM kijun")]
            paths += [
                kijun_url(r["kname"], r["pname"])
                for r in con.execute(
                    """SELECT kj.name AS kname, p.name AS pname FROM kijun_pref kp
                       JOIN kijun kj ON kj.id = kp.kijun_id JOIN prefs p ON p.code = kp.pref_code"""
                )
            ]
        elif (K := _other_kind(part)) is not None:
            if part == K.base[1:]:
                paths = [K.base]
                paths += [K.pref_url(r["name"]) for r in con.execute(f"SELECT name FROM {K.t('prefs')} ORDER BY code")]
                paths += [K.city_url(r["pref"], r["city"]) for r in con.execute(
                    f"""SELECT DISTINCT p.name AS pref, f.city FROM {K.t('fac')} f JOIN {K.t('prefs')} p ON p.code = f.pref_code
                       WHERE f.city IS NOT NULL ORDER BY f.pref_code, f.city""")]
                paths += [K.kijun_url(r["name"]) for r in con.execute(f"SELECT name FROM {K.t('kijun')}")]
            else:
                i = int(part.rsplit("-", 1)[1])
                paths = [K.url(r["id"]) for r in con.execute(
                    f"SELECT id FROM {K.t('fac')} ORDER BY id LIMIT ? OFFSET ?", (SITEMAP_CHUNK, i * SITEMAP_CHUNK))]
                if not paths:
                    raise HTTPException(404)
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
