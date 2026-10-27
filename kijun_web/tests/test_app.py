"""施設基準検索サイトの動作確認（build_db.pyで作ったDBが必要）。

    python kijun_web/build_db.py
    python -m pytest kijun_web/tests -q
"""
import re
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from kijun_web import app as appmod

if not appmod.DB_PATH.exists():
    pytest.skip("kijun.sqlite がありません（先に build_db.py を実行）", allow_module_level=True)

client = TestClient(appmod.app)
KIJUN = "地域包括ケア病棟入院料１及び地域包括ケア入院医療管理料１"


def _first_facility_id(name_like: str) -> str:
    import sqlite3
    con = sqlite3.connect(appmod.DB_PATH)
    return con.execute("SELECT id FROM facilities WHERE name LIKE ? LIMIT 1", (f"%{name_like}%",)).fetchone()[0]


@pytest.mark.parametrize("path", [
    "/", "/kijun", "/about", "/pref/" + quote("高知県"),
    "/kijun/" + quote(KIJUN), "/kijun/" + quote(KIJUN) + "/" + quote("高知県"),
    "/search?k=" + quote("地域包括ケア"), "/search?q=" + quote("近森"),
])
def test_pages_ok(path):
    r = client.get(path)
    assert r.status_code == 200
    assert "出典：各地方厚生局" in r.text  # PDL1.0の出典表示が全ページに出ていること


def test_not_found():
    assert client.get("/kijun/" + quote("存在しない施設基準")).status_code == 404
    assert client.get("/facility/000000000").status_code == 404
    assert client.get("/pref/" + quote("存在しない県")).status_code == 404


def test_facility_page_and_csv():
    fid = _first_facility_id("近森病院")
    r = client.get(f"/facility/{fid}")
    assert r.status_code == 200 and "近森病院" in r.text
    c = client.get(f"/facility/{fid}.csv")
    assert c.status_code == 200
    lines = c.content.decode("utf-8-sig").splitlines()
    assert lines[0].startswith("都道府県,医療機関番号,医療機関名称")
    assert "出典" in lines[-1]


def test_exact_vs_partial_kijun():
    # 部分一致：地域包括ケア系の5種類のどれか
    partial = client.get("/search?k=" + quote("地域包括ケア") + "&pref=39").text
    exact = client.get("/search?k=" + quote(KIJUN) + "&pref=39").text
    n = lambda html: int(re.search(r'<div class="count">([\d,]+) 件', html)[1].replace(",", ""))
    assert n(exact) <= n(partial)
    assert "いずれかを届け出ている" in partial and "いずれかを届け出ている" not in exact


def test_search_csv_with_kijun_has_filing_columns():
    c = client.get("/search.csv?kx=" + quote(KIJUN) + "&pref=39")
    header = c.content.decode("utf-8-sig").splitlines()[0]
    assert "受理届出名称" in header and "算定開始年月日" in header


def test_sitemaps_within_limit():
    idx = client.get("/sitemap.xml").text
    parts = re.findall(r"/sitemap-([\w-]+)\.xml", idx)
    assert "kijun" in parts and "facilities-0" in parts
    for p in parts:
        body = client.get(f"/sitemap-{p}.xml").text
        assert body.count("<url>") <= 50_000  # Googleのsitemap 1ファイルあたりの上限


def test_robots_blocks_search_and_csv():
    t = client.get("/robots.txt").text
    assert "Disallow: /search" in t and "Sitemap:" in t
