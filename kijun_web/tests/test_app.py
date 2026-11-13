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
    "/", "/kijun", "/about", "/guide", "/operator", "/contact", "/privacy", "/pref/" + quote("高知県"),
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
    msg = "のいずれかを届け出ている医療機関を表示"   # 部分一致の時だけ出る案内（フォームの「いずれか」の選択肢と区別する）
    assert msg in partial and msg not in exact


def test_search_csv_with_kijun_has_filing_columns():
    c = client.get("/search.csv?kx=" + quote(KIJUN) + "&pref=39")
    header = c.content.decode("utf-8-sig").splitlines()[0]
    assert "受理届出名称" in header and "算定開始年月日" in header


def test_kubun_ref_fills_only_blank_kubun():
    import sqlite3
    con = sqlite3.connect(appmod.DB_PATH)
    # 参考値は公式の区分が空の届出にだけ入る
    assert con.execute("SELECT COUNT(*) FROM filings WHERE kubun != '' AND kubun_ref != ''").fetchone()[0] == 0
    fid = con.execute(
        """SELECT x.facility_id FROM filings x JOIN facilities f ON f.id = x.facility_id
           WHERE f.pref_code = '01' AND x.kubun_ref != '' LIMIT 1"""
    ).fetchone()[0]
    r = client.get(f"/facility/{fid}")
    assert "参考" in r.text and "病床機能報告" in r.text
    header = client.get(f"/facility/{fid}.csv").content.decode("utf-8-sig").splitlines()[0]
    assert header.endswith("区分（参考：病床機能報告）")


def _item_id(symbol: str) -> int:
    import sqlite3
    con = sqlite3.connect(appmod.DB_PATH)
    return con.execute("SELECT id FROM items WHERE symbol = ? ORDER BY n_facilities DESC", (symbol,)).fetchone()[0]


def test_item_search_and_or():
    a, b = _item_id("一般入院"), _item_id("地包ケア１")
    n = lambda url: int(re.search(r'<div class="count">([\d,]+) 件', client.get(url).text)[1].replace(",", ""))
    both = n(f"/search?pref=39&item={a}&item={b}")
    either = n(f"/search?pref=39&item={a}&item={b}&mode=or")
    only_a = n(f"/search?pref=39&item={a}")
    assert 0 < both <= only_a <= either


def test_areas_json_and_area_filter():
    areas = client.get("/areas/39.json").json()
    names = [a["name"] for a in areas]
    assert "中央" in names and all(a["cities"] for a in areas)
    kochi = next(a for a in areas if a["name"] == "中央")
    r = client.get("/search?pref=39&area=" + quote("中央"))
    total = int(re.search(r'<div class="count">([\d,]+) 件', r.text)[1].replace(",", ""))
    assert total == kochi["n"]


def test_search_csv_item_columns():
    a = _item_id("一般入院")
    lines = client.get(f"/search.csv?pref=39&item={a}").content.decode("utf-8-sig").splitlines()
    assert lines[0].endswith("一般入院（一般病棟入院基本料）") and "二次医療圏" in lines[0]
    assert lines[1].endswith(",○")


def test_items_follow_official_order():
    import sqlite3
    con = sqlite3.connect(appmod.DB_PATH)
    first = [r[0] for r in con.execute("SELECT symbol FROM items ORDER BY id LIMIT 3")]
    assert first == ["情報通信", "機能強化", "外来感染"]   # 略称一覧（基本診療料）の先頭と同じ並び


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


def _kubun_id(kijun: str, name: str) -> int:
    import sqlite3
    con = sqlite3.connect(appmod.DB_PATH)
    return con.execute(
        "SELECT b.id FROM kubuns b JOIN kijun k ON k.id = b.kijun_id WHERE k.name = ? AND b.name = ?", (kijun, name)
    ).fetchone()[0]


def _count(html: str) -> int:
    return int(re.search(r'class="count">\s*([\d,]+) 件', html).group(1).replace(",", ""))


def test_kubun_reference_values_use_official_wording():
    """病床機能報告の参考値（療養病棟入院料１、障害者施設等10対１入院基本料）が、
    厚生局の区分欄と同じ書き方（入院料１、１０対１入院基本料）の項目にまとまっている。"""
    import sqlite3
    con = sqlite3.connect(appmod.DB_PATH)
    names = {r[0] for r in con.execute("SELECT name FROM kubuns")}
    assert "入院料１" in names and "１０対１入院基本料" in names
    assert not any(n.startswith(("療養病棟", "障害者施設等")) or "10" in n for n in names)
    # 区分は入院基本料の届出だけ
    assert all(k.endswith("入院基本料") for (k,) in con.execute(
        "SELECT DISTINCT k.name FROM kubuns b JOIN kijun k ON k.id = b.kijun_id"))


def test_kubun_search_official_and_reference():
    kb = _kubun_id("一般病棟入院基本料", "急性期一般入院料１")
    # 高知県（四国厚生局：区分を公表）→ 公式の○
    html = client.get("/search", params={"pref": "39", "kb": kb}).text
    assert _count(html) > 0 and '<span class="ok" title' in html and "急性期一般入院料１" in html
    # 北海道（区分を公表していない）→ 参考値の○と注意書き
    # （関東信越・近畿は2026年11月から届出項目別の名簿で公式の区分が入るようになった）
    html = client.get("/search", params={"pref": "01", "kb": kb}).text
    assert _count(html) > 0 and 'class="ok ref"' in html and "区分について" in html


def test_kubun_free_text_falls_back_to_kubun():
    """施設基準の名称に無い語（急性期一般入院料）は区分で探す（以前は0件だった）。"""
    html = client.get("/search", params={"pref": "39", "k": "急性期一般入院料"}).text
    assert _count(html) > 0 and "入院基本料の<strong>区分</strong>です" in html


def test_kubun_and_item_and_or():
    kb = _kubun_id("一般病棟入院基本料", "急性期一般入院料１")
    import sqlite3
    item = sqlite3.connect(appmod.DB_PATH).execute(
        "SELECT i.id FROM items i JOIN kijun k ON k.id = i.kijun_id WHERE k.name = ? ORDER BY i.n_facilities DESC",
        (KIJUN,)).fetchone()[0]
    base = {"kb": kb, "item": item}
    n_and = _count(client.get("/search", params=base).text)
    n_or = _count(client.get("/search", params={**base, "mode": "or"}).text)
    n_kb = _count(client.get("/search", params={"kb": kb}).text)
    assert 0 < n_and <= n_kb <= n_or


def test_search_csv_kubun_columns():
    kb = _kubun_id("一般病棟入院基本料", "急性期一般入院料１")
    text = client.get("/search.csv", params={"pref": "01", "kb": kb}).text
    header = text.splitlines()[0]
    assert "区分：急性期一般入院料１（一般病棟入院基本料）" in header
    assert "○（参考" in text and "区分の列：" in text


def test_kanto_kinki_kubun_is_official():
    """関東信越・近畿は届出項目別の名簿で公式の区分が入り、参考値はほぼ使われない。"""
    import sqlite3
    con = sqlite3.connect(appmod.DB_PATH)
    for pref in ("08", "13", "27"):   # 茨城・東京・大阪
        total, official = con.execute(
            """SELECT COUNT(*), SUM(x.kubun != '') FROM filings x JOIN kijun k ON k.id = x.kijun_id
               JOIN facilities f ON f.id = x.facility_id
               WHERE f.pref_code = ? AND k.name IN ('一般病棟入院基本料', '療養病棟入院基本料')""", (pref,)
        ).fetchone()
        assert official / total > 0.9, pref


def test_privacy_and_ads_txt():
    assert client.get("/privacy").status_code == 200
    assert "Google AdSense" in client.get("/privacy").text
    assert "/privacy" in client.get("/").text            # フッターからリンク
    if not appmod.ADSENSE_CLIENT:
        assert client.get("/ads.txt").status_code == 404
        assert "adsbygoogle" not in client.get("/").text  # IDが無ければ広告タグは出さない


def test_ad_slot_renders_when_configured():
    g = appmod.templates.env.globals
    old = (g["adsense_client"], g["adsense_slot"])
    g["adsense_client"], g["adsense_slot"] = "ca-pub-0000000000000000", "1234567890"
    try:
        html = client.get("/kijun/" + quote(KIJUN)).text
        assert 'class="ad-slot"' in html and "ca-pub-0000000000000000" in html and "google-adsense-account" in html
    finally:
        g["adsense_client"], g["adsense_slot"] = old


def _get(path: str) -> str:
    return client.get(path).text




def test_multiple_prefs():
    """都道府県は複数選べる（高知＋徳島 = それぞれの合計）。2つ以上なら二次医療圏は無視する。"""
    kochi = _count(_get("/search?pref=39"))
    toku = _count(_get("/search?pref=36"))
    both = _count(_get("/search?pref=39&pref=36"))
    assert both == kochi + toku
    assert _count(_get("/search?pref=39&pref=36&area=" + quote("中央"))) == both


def test_top_condition_table_prefilled():
    """トップは条件表。検索結果の「条件を変更する」から戻ると、選んだ条件が入っている。"""
    r = client.get("/")
    assert 'name="pref" value="39"' in r.text and 'name="item"' in r.text and 'name="kb"' in r.text
    r = client.get("/?pref=39&ftype=" + quote("病院") + "&item=1")
    assert re.search(r'name="pref" value="39" checked', r.text)
    assert re.search(r'name="item" value="1" checked', r.text)
    s = client.get("/search?pref=39&item=1")
    assert 'href="/?pref=39&amp;item=1"' in s.text


def test_adsense_review_pages_linked():
    """AdSenseの審査で見られる運営者情報・お問い合わせ・プライバシーポリシーが全ページのフッターから辿れる。"""
    html = client.get("/kijun").text
    for path in ["/operator", "/contact", "/privacy", "/about", "/guide"]:
        assert f'href="{path}"' in html
    assert "MedilenZ運営事務局" in client.get("/operator").text
    sm = client.get("/sitemap-static.xml").text
    assert "/operator" in sm and "/contact" in sm and "/guide" in sm


def test_analytics_tag_only_on_production_domain():
    """GA4のタグはページにあるが、medilenz.jp で開いた時だけ読み込む（手元・IP直打ちでは送らない）。"""
    html = client.get("/").text
    assert "G-8Y6SDBSCMQ" in html and "medilenz" in html and "csv_download" in html
    assert "Google アナリティクス" in client.get("/privacy").text


def test_kubun_sorted_by_nursing_standard():
    """入院基本料の区分は、看護の基準が高い順（７対１→１０対１、急性期一般入院料１→６、特別入院基本料は最後）。"""
    k = appmod.kubun_sort_key
    ippan = ["特別入院基本料", "地域一般入院料１", "急性期一般入院料６", "急性期一般入院料１", "急性期病院Ａ一般入院料"]
    assert sorted(ippan, key=lambda n: k("一般病棟入院基本料", n)) == [
        "急性期病院Ａ一般入院料", "急性期一般入院料１", "急性期一般入院料６", "地域一般入院料１", "特別入院基本料"]
    ratio = ["１５対１入院基本料", "７対１入院基本料", "１０対１入院基本料"]
    assert sorted(ratio, key=lambda n: k("障害者施設等入院基本料", n)) == ["７対１入院基本料", "１０対１入院基本料", "１５対１入院基本料"]


def test_contact_form_linked():
    html = client.get("/contact").text
    assert "https://forms.gle/3ZzbjR1PvCZbqFZi6" in html and "準備中" not in html
