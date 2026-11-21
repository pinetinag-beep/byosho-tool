"""毎月の新規届出・取り下げ・区分の変更（/changes）。

データは build_kijun_diff.py が作る kijun_diff.parquet（前回と今回の名簿の比較）を、build_db._build_kdiff() が kdiff に入れたもの。
都道府県ごとに名簿の時点が変わった県だけを比べているので、月によって対象の県が違う（月は「今回の名簿の時点」）。
"""
from __future__ import annotations

import math
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

router = APIRouter()
PAGE_SIZE = 100
KINDS = ("新規", "変更", "取り下げ")


def _a():
    from . import app as A   # app.py がこのモジュールを読み込むので、使う時に読む
    return A


def month_label(m: str) -> str:
    try:
        return f"{int(m[:4])}年{int(m[5:7])}月"
    except (ValueError, TypeError):
        return m or ""


class DCond:
    def __init__(self, request: Request, months: list[str], pref_months: dict | None = None):
        qp = request.query_params
        m = qp.get("month", "")
        self.pref = qp.get("pref", "").strip()
        if m not in months and self.pref and pref_months and self.pref in pref_months:
            m = pref_months[self.pref]   # 県だけ選んだ時は、その県の名簿が最後に更新された月
        self.month = m if m in months else (months[0] if months else "")
        self.kind = qp.get("kind", "") if qp.get("kind", "") in KINDS else ""
        self.q = qp.get("q", "").strip()
        self.kijun = qp.get("kijun", "").strip()   # 届出名の完全一致（施設基準ページからのリンク用）

    def where(self, search_key) -> tuple[str, list]:
        w, p = ["k.month = ?"], [self.month]
        if self.pref:
            w.append("k.pref_code = ?")
            p.append(self.pref)
        if self.kind:
            w.append("k.kind = ?")
            p.append(self.kind)
        if self.kijun:
            w.append("(k.kijun_name = ? OR k.prev_name = ?)")
            p += [self.kijun, self.kijun]
        if self.q:
            w.append("(k.kijun_name LIKE ? OR k.prev_name LIKE ? OR k.fac_name LIKE ?)")
            p += [f"%{self.q}%"] * 3
        return " WHERE " + " AND ".join(w), p

    def query(self, **extra) -> str:
        pairs = [("month", self.month), ("pref", self.pref), ("kind", self.kind), ("kijun", self.kijun), ("q", self.q)]
        pairs += list(extra.items())
        return urlencode([(k, v) for k, v in pairs if v not in ("", None)])

    @property
    def filtered(self) -> bool:
        return bool(self.kind or self.q or self.kijun)


def _pref_months(con) -> dict:
    return dict(con.execute("SELECT pref_code, MAX(month) FROM kdiff GROUP BY pref_code").fetchall())


def _months(con) -> list[str]:
    try:
        return [r[0] for r in con.execute("SELECT DISTINCT month FROM kdiff ORDER BY month DESC")]
    except Exception:  # noqa: BLE001  （古いDB）
        return []


_ROW_SQL = """SELECT k.*, f.id AS live_id, f.city FROM kdiff k LEFT JOIN facilities f ON f.id = k.facility_id"""


@router.get("/changes", response_class=HTMLResponse)
def changes(request: Request, page: int = Query(1, ge=1)):
    A = _a()
    with A.db() as con:
        months = _months(con)
        c = DCond(request, months, _pref_months(con) if months else None)
        if not months:
            return A.render(request, "changes.html", c=c, months=[], month_label=month_label, empty=True)
        where, params = c.where(A.search_key)
        total = con.execute(f"SELECT COUNT(*) FROM kdiff k{where}", params).fetchone()[0]
        pages = max(1, math.ceil(total / PAGE_SIZE))
        page = min(page, pages)
        rows = con.execute(f"{_ROW_SQL}{where} ORDER BY k.pref_code, f.city, k.fac_name, k.kind LIMIT ? OFFSET ?",
                           params + [PAGE_SIZE, (page - 1) * PAGE_SIZE]).fetchall()
        # 月全体（県で絞った時はその県）の集計
        base_w, base_p = "WHERE month = ?" + (" AND pref_code = ?" if c.pref else ""), [c.month] + ([c.pref] if c.pref else [])
        counts = dict(con.execute(f"SELECT kind, COUNT(*) FROM kdiff {base_w} GROUP BY kind", base_p).fetchall())
        fac_events = dict(con.execute(
            f"SELECT fac_event, COUNT(DISTINCT facility_id) FROM kdiff {base_w} AND fac_event <> '' GROUP BY fac_event",
            base_p).fetchall())
        by_kijun = con.execute(f"""
            SELECT kijun_name AS name, kijun_id,
                   SUM(kind = '新規') AS n_new, SUM(kind = '変更') AS n_chg, SUM(kind = '取り下げ') AS n_drop
            FROM kdiff {base_w} GROUP BY kijun_name ORDER BY COUNT(*) DESC LIMIT 40""", base_p).fetchall()
        by_pref = con.execute("""
            SELECT pref_code, pref_name, prev_month, SUM(kind = '新規') AS n_new, SUM(kind = '変更') AS n_chg,
                   SUM(kind = '取り下げ') AS n_drop
            FROM kdiff WHERE month = ? GROUP BY pref_code ORDER BY pref_code""", (c.month,)).fetchall()
        prefs = A._prefs(con)
    pref = next((p for p in by_pref if p["pref_code"] == c.pref), None)
    return A.render(
        request, "changes.html", c=c, months=months, month_label=month_label, rows=rows, total=total, page=page,
        pages=pages, counts=counts, fac_events=fac_events, by_kijun=by_kijun, by_pref=by_pref, prefs=prefs, pref=pref,
        page_url=lambda p: "/changes?" + c.query(page=p if p > 1 else ""), csv_url="/changes.csv?" + c.query())


@router.get("/changes.csv")
def changes_csv(request: Request):
    A = _a()
    con = A.sqlite3.connect(f"file:{A.DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = A.sqlite3.Row
    months = _months(con)
    c = DCond(request, months, _pref_months(con) if months else None)
    where, params = c.where(A.search_key)
    rows = con.execute(f"{_ROW_SQL}{where} ORDER BY k.pref_code, f.city, k.fac_name, k.kind", params).fetchall()
    con.close()
    header = ["名簿の時点（前回→今回）", "都道府県", "市区町村", "医療機関名", "所在地", "医療機関コード（10桁）", "施設の種類",
              "動き", "届出", "前の届出（区分の変更）", "受理記号", "受理番号", "算定開始年月日", "医療機関の動き"]

    def gen():
        for r in rows:
            yield [f"{month_label(r['prev_month'])}→{month_label(r['month'])}", r["pref_name"], r["city"] or "",
                   r["fac_name"], r["address"], r["pref_code"] + "1" + r["facility_id"][2:], r["ftype"], r["kind"],
                   r["kijun_name"], r["prev_name"], r["symbol"], r["juri_no"], r["start_date"] or "", r["fac_event"]]
    return A._csv_response(header, gen(), f"施設基準の新規届出・取り下げ_{c.month}.csv")
