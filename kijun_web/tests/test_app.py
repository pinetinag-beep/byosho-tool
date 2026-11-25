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
    msg = "のいずれかを届け出ている医療機関です"   # 部分一致の時だけ出る案内（フォームの「いずれか」の選択肢と区別する）
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
           WHERE x.kubun_ref != '' LIMIT 1"""
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
    # 北海道も2026年11月から届出項目別の名簿で公式の区分が入る
    html = client.get("/search", params={"pref": "01", "kb": kb}).text
    assert _count(html) > 0 and '<span class="ok" title' in html
    # 区分が公表されていない届出（参考値）→ 参考の○と注意書き
    import sqlite3
    con = sqlite3.connect(appmod.DB_PATH)
    row = con.execute(
        """SELECT f.pref_code, k.name, x.kubun_ref FROM filings x JOIN facilities f ON f.id = x.facility_id
           JOIN kijun k ON k.id = x.kijun_id WHERE x.kubun_ref != '' LIMIT 1""").fetchone()
    html = client.get("/search", params={"pref": row[0], "kb": _kubun_id(row[1], row[2])}).text
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


def test_facility_page_shows_byosho():
    """病床機能報告と突合できた病院のページに、病床・人員・設備と年度ごとの推移、出典が出る。"""
    import sqlite3
    con = sqlite3.connect(appmod.DB_PATH)
    fid = con.execute("SELECT id FROM facilities WHERE byosho_code IS NOT NULL AND name LIKE '%近森病院%'").fetchone()[0]
    html = client.get(f"/facility/{fid}").text
    assert "病床・人員・設備（病床機能報告）" in html and "年度ごとの推移" in html
    assert "厚生労働省「病床機能報告」" in html and "streamlit.app" not in html
    assert "ﾊｲｹｱ" not in html


def test_facility_page_shows_dpc():
    with appmod.db() as con:
        fid = con.execute(
            "SELECT f.id FROM facilities f JOIN dpc_hosp d ON d.code = f.byosho_code LIMIT 1").fetchone()[0]
    html = _get(f"/facility/{fid}")
    assert "入院患者の診療実績（DPC）" in html and "退院患者調査" in html and "主要診断群" in html


def test_facility_page_shows_gairai_for_clinic():
    with appmod.db() as con:
        fid = con.execute(
            """SELECT f.id FROM facilities f JOIN gairai g ON g.facility_id = f.id
               WHERE f.ftype = '有床診療所' AND g.n_first > 0 LIMIT 1""").fetchone()[0]
    html = _get(f"/facility/{fid}")
    assert "外来の状況（外来機能報告）" in html and "紹介率" in html


def test_sources_follow_bureau_citation_example():
    # 厚生局の利用規約の記載例：「ページ名」（厚生局名）（URL）（利用した日）を加工して作成
    html = _get(f"/pref/{quote('高知県')}")
    assert "（四国厚生支局）" in html and "kouseikyoku.mhlw.go.jp/shikoku/" in html and "に利用）を加工して作成" in html
    about = _get("/about")
    assert 'id="sources"' in about and about.count("kouseikyoku.mhlw.go.jp/") >= 8 and "www.mhlw.go.jp" in about


def test_hospital_conditions_filter_and_show_values():
    # 病床・設備・実績（病床機能報告・DPC・外来機能報告）で絞り込み、使った値を結果とCSVに出す
    html = _get("/search?pref=39&bmin=200&eq=robot&amb=1000")
    assert "許可病床数 200床以上" in html and "手術支援ロボット（ダビンチ等）あり" in html and "高知赤十字病院" in html
    assert "病床機能報告" in html
    csv_text = client.get("/search.csv?pref=39&bmin=200&eq=robot&amb=1000").text
    assert "許可病床数（床）" in csv_text and "救急車の受入件数（件）" in csv_text
    # トップに条件を入れた状態で戻れる
    top = _get("/?pref=39&bmin=200&eq=robot&amb=1000")
    assert 'name="bmin" min="0" step="1" inputmode="numeric" value="200"' in top and 'value="robot" checked' in top


def test_analytics_page_groups_and_events():
    # 経路データ探索で medilenz.jp での動きが分かるよう、ページの種類とサイト独自のイベントを送る
    assert 'content_group: "\\u30c8\\u30c3\\u30d7"' in _get("/")
    assert 'gtag("event", "search"' in _get("/search?pref=39")
    assert 'gtag("event", "view_facility"' in _get("/facility/390110406")
    assert 'data-ga="suggest"' in _get("/")


def test_facility_seo_title_jsonld_and_nearby():
    import json
    import re
    import sqlite3
    con = sqlite3.connect(appmod.DB_PATH)
    # 病床機能報告とつながる病院：タイトルに病床数、構造化データは Hospital、近くの医療機関へのリンク
    fid = con.execute(
        """SELECT f.id FROM facilities f JOIN hstats h ON h.facility_id = f.id
           WHERE f.ftype = '病院' AND h.beds > 0 AND f.area NOT LIKE '（%' LIMIT 1""").fetchone()[0]
    html = client.get(f"/facility/{fid}").text
    assert re.search(r"<title>[^<]*の病床数・施設基準", html)
    ld = json.loads(re.search(r'application/ld\+json">(.*?)</script>', html, re.S).group(1))
    org, crumbs = ld["@graph"]
    assert org["@type"] == "Hospital" and org["address"]["addressRegion"]
    assert [x["position"] for x in crumbs["itemListElement"]] == [1, 2, 3, 4]   # トップ・県・市区町村・医療機関
    assert "近くの医療機関" in html and html.count('href="/facility/') > 1
    assert f'href="/facility/{fid}"' not in html.split("近くの医療機関")[1]   # 自分自身は出さない
    # 病床機能報告とも医療情報ネットとも結びつかない診療所：従来のタイトル、MedicalClinic
    cid = con.execute("""SELECT id FROM facilities WHERE ftype = '無床診療所'
                         AND id NOT IN (SELECT facility_id FROM navi WHERE sched != '') LIMIT 1""").fetchone()[0]
    html = client.get(f"/facility/{cid}").text
    assert "の施設基準届出一覧｜MedilenZ</title>" in html and '"MedicalClinic"' in html


def test_city_and_area_pages():
    import sqlite3
    from urllib.parse import quote
    con = sqlite3.connect(appmod.DB_PATH)
    pref, city, area = con.execute(
        """SELECT p.name, f.city, f.area FROM facilities f JOIN prefs p ON p.code = f.pref_code
           WHERE f.ftype = '病院' AND f.area NOT LIKE '（%' LIMIT 1""").fetchone()
    r = client.get(f"/city/{quote(pref)}/{quote(city)}")
    assert r.status_code == 200 and f"<title>{pref}{city}の病院・診療所一覧" in r.text
    assert f'href="/area/{quote(pref)}/{quote(area)}"' in r.text   # パンくずから二次医療圏へ
    r = client.get(f"/area/{quote(pref)}/{quote(area)}")
    assert r.status_code == 200 and f"{area}医療圏（{pref}）の病院" in r.text
    assert f'href="/city/{quote(pref)}/{quote(city)}"' in r.text
    assert client.get(f"/city/{quote(pref)}/{quote('存在しない市')}").status_code == 404
    assert client.get(f"/area/{quote(pref)}/{quote('（二次医療圏不明）')}").status_code == 404
    # 都道府県ページから二次医療圏・市区町村へ、サイトマップにも載る
    assert f'href="/area/{quote(pref)}/{quote(area)}"' in client.get(f"/pref/{quote(pref)}").text
    assert f"/city/{quote(pref)}/{quote(city)}<" in client.get("/sitemap-static.xml").text


def test_kijun_page_hospitals_and_related():
    import sqlite3
    con = sqlite3.connect(appmod.DB_PATH)
    name = con.execute(
        """SELECT k.name FROM kijun k WHERE EXISTS (SELECT 1 FROM kijun_rel r WHERE r.kijun_id = k.id)
           AND EXISTS (SELECT 1 FROM filings x JOIN facilities f ON f.id = x.facility_id
                       WHERE x.kijun_id = k.id AND f.ftype = '病院')
           ORDER BY k.n_facilities DESC LIMIT 1""").fetchone()[0]
    html = client.get(appmod.kijun_url(name)).text
    assert "届け出ている主な病院（許可病床数の多い順）" in html and 'href="/facility/' in html
    assert "一緒に届け出られていることが多い施設基準" in html
    # 関連の施設基準は自分自身を含まず、上位12件まで
    assert con.execute("SELECT COUNT(*) FROM kijun_rel WHERE kijun_id = rel_id").fetchone()[0] == 0
    assert con.execute("SELECT MAX(c) FROM (SELECT COUNT(*) c FROM kijun_rel GROUP BY kijun_id)").fetchone()[0] <= 12


def test_facility_summary_sentence():
    import re
    import sqlite3
    con = sqlite3.connect(appmod.DB_PATH)
    fid = con.execute("SELECT facility_id FROM hstats WHERE beds > 0 AND dpc_total > 0 LIMIT 1").fetchone()[0]
    m = re.search(r'<p class="summary">(.*?)</p>', client.get(f"/facility/{fid}").text, re.S)
    assert m and "許可病床数は" in m.group(1) and "種類の施設基準を届け出ています" in m.group(1) and "退院患者数は年間" in m.group(1)


def test_specialty_page_and_grouped_items():
    """診療科から探す：説明つきの届出を選んで検索。item=1.2.3 のように「.」でまとめた値も1つずつ送った時と同じ検索になる。"""
    r = client.get("/specialty")
    assert r.status_code == 200
    assert "循環器内科" in r.text and "どんなものか" in r.text and "MedilenZが作ったもの" in r.text
    vals = re.findall(r'name="item" value="([\d.]+)"', r.text)
    assert len(vals) > 250
    # 区分をまとめた行（特定集中治療室管理料１〜３など）は、区分ごとのチェックボックスも持つ
    assert 'class="sp-grp"' in r.text and 'class="sp-subs"' in r.text
    ids = [vals[0].split(".")[0], vals[1].split(".")[0]]
    grouped = ".".join(ids)
    # まとめた値と、1つずつ送った値は同じ検索になる
    a = client.get(f"/search?item={grouped}&mode=or&ftype=病院")
    b = client.get("/search?" + "&".join(f"item={i}" for i in ids) + "&mode=or&ftype=病院")
    assert a.status_code == b.status_code == 200
    count = lambda html: re.search(r"([\d,]+)\s*件（", html).group(1)
    assert count(a.text) == count(b.text)
    assert "/specialty<" in client.get("/sitemap-static.xml").text
    assert 'href="/specialty"' in client.get("/").text


def test_yakkyoku_pages():
    """薬局を探す：トップ・検索・CSV・薬局・都道府県・市区町村・届出のページ。"""
    import csv as _csv
    import io
    import sqlite3
    con = sqlite3.connect(appmod.DB_PATH)
    con.row_factory = sqlite3.Row
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name = 'y_fac'").fetchone():
        pytest.skip("薬局のデータがありません（yakkyoku_cache.parquet）")
    assert con.execute("SELECT COUNT(DISTINCT pref_code) FROM y_fac").fetchone()[0] == 47
    f = con.execute("SELECT * FROM y_fac WHERE kihon IS NOT NULL AND chiiki IS NOT NULL AND city IS NOT NULL LIMIT 1").fetchone()
    assert len(f["id"]) == 10 and f["id"][2] == "4"       # 都道府県2桁＋薬局「4」＋医療機関番号7桁
    k1 = con.execute("SELECT id, name FROM y_kijun WHERE name = '調剤基本料１'").fetchone()
    k2 = con.execute("SELECT id, name FROM y_kijun WHERE name = '無菌製剤処理加算'").fetchone()

    r = client.get("/yakkyoku")
    assert r.status_code == 200 and "調剤基本料（薬局の基本の区分）" in r.text and "どんなものか" in r.text
    r = client.get(f"/yakkyoku/{f['id']}")
    assert r.status_code == 200 and f["name"] in r.text and '"@type": "Pharmacy"' in r.text and f["kihon"] in r.text
    assert client.get("/yakkyoku/0000000000").status_code == 404
    assert client.get("/yakkyoku/abc").status_code == 404
    assert client.get(f"/yakkyoku/pref/{quote(f['pref_name'])}").status_code == 200
    assert client.get(f"/yakkyoku/city/{quote(f['pref_name'])}/{quote(f['city'])}").status_code == 200
    r = client.get(f"/yakkyoku/kijun/{quote(k2['name'])}")
    assert r.status_code == 200 and "無菌" in r.text

    # いずれか／すべて：すべて（初期値）の方が少ない。CSVは選んだ届出ごとに○の列
    n = lambda html: int(re.search(r'class="count">([\d,]+) 件', html).group(1).replace(",", ""))
    any_ = n(client.get(f"/yakkyoku/search?pref=13&item={k1['id']}&item={k2['id']}&mode=or").text)
    all_ = n(client.get(f"/yakkyoku/search?pref=13&item={k1['id']}&item={k2['id']}").text)
    # 届け出ていない（nx）と列だけ（sx）
    only1 = n(client.get(f"/yakkyoku/search?pref=13&item={k1['id']}").text)
    assert n(client.get(f"/yakkyoku/search?pref=13&item={k1['id']}&nx={k2['id']}").text) == only1 - all_
    r = client.get(f"/yakkyoku/search?pref=13&item={k1['id']}&sx={k2['id']}")
    assert n(r.text) == only1 and 'name="sx" value="%d"' % k2["id"] in r.text
    assert client.get("/shika/items/suggest.json", params={"q": "在宅"}).json()
    db_all = con.execute("""SELECT COUNT(*) FROM (SELECT f.id FROM y_fac f JOIN y_filings fl ON fl.facility_id = f.id
                            WHERE f.pref_code = '13' AND fl.kijun_id IN (?, ?) GROUP BY f.id HAVING COUNT(DISTINCT fl.kijun_id) = 2)""",
                         (k1["id"], k2["id"])).fetchone()[0]
    assert 0 < all_ == db_all < any_
    r = client.get(f"/yakkyoku/search.csv?pref=39&item={k2['id']}")
    rows = list(_csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert rows[0][-1] == f"届出：{k2['name']}" and rows[1][0] == "高知県" and rows[1][-1] == "○"
    # サイトマップとヘッダーのメニュー
    assert "/sitemap-yakkyoku-0.xml" in client.get("/sitemap.xml").text
    assert f"/yakkyoku/{f['id']}<" in client.get("/sitemap-yakkyoku-0.xml").text
    assert 'href="/yakkyoku"' in client.get("/").text


def test_shika_pages():
    """歯科診療所を探す：薬局と同じ仕組み（yakkyoku.Kind）で /shika/… のページを作る。"""
    import csv as _csv
    import io
    import sqlite3
    con = sqlite3.connect(appmod.DB_PATH)
    con.row_factory = sqlite3.Row
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name = 'd_fac'").fetchone():
        pytest.skip("歯科のデータがありません（shika_cache.parquet）")
    assert con.execute("SELECT COUNT(DISTINCT pref_code) FROM d_fac").fetchone()[0] == 47
    f = con.execute("SELECT * FROM d_fac WHERE kihon IS NOT NULL AND city IS NOT NULL LIMIT 1").fetchone()
    assert len(f["id"]) == 10 and f["id"][2] == "3"       # 都道府県2桁＋歯科「3」＋医療機関番号7桁
    k = con.execute("SELECT id, name FROM d_kijun WHERE name = '在宅療養支援歯科診療所１'").fetchone()

    r = client.get("/shika")
    assert r.status_code == 200 and "歯科診療所を探す" in r.text and "在宅（訪問歯科診療）" in r.text
    r = client.get(f"/shika/{f['id']}")
    assert r.status_code == 200 and f["name"] in r.text and '"@type": "Dentist"' in r.text and "口腔管理体制強化加算" in r.text
    assert client.get(f"/shika/pref/{quote(f['pref_name'])}").status_code == 200
    r = client.get(f"/shika/city/{quote(f['pref_name'])}/{quote(f['city'])}")
    assert r.status_code == 200 and "の薬局（" in r.text      # 同じ市区町村の薬局へのリンク
    assert client.get(f"/shika/kijun/{quote(k['name'])}").status_code == 200
    r = client.get(f"/shika/search.csv?pref=39&item={k['id']}")
    rows = list(_csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert rows[0][3] == "歯科診療所名" and rows[0][-1] == f"届出：{k['name']}" and rows[1][-1] == "○"
    # 薬局のページが歯科の設定に引きずられていない（同じ関数・テンプレートを使うので）
    assert '"@type": "Pharmacy"' in client.get(f"/yakkyoku/{con.execute('SELECT id FROM y_fac LIMIT 1').fetchone()[0]}").text
    assert "/sitemap-shika-0.xml" in client.get("/sitemap.xml").text
    assert 'href="/shika"' in client.get("/").text


def test_changes_pages():
    """新規届出・取り下げ（/changes）：名簿の比較の一覧・絞り込み・CSV、医療機関・施設基準ページの「動き」。"""
    import csv as _csv
    import io
    import sqlite3
    con = sqlite3.connect(appmod.DB_PATH)
    con.row_factory = sqlite3.Row
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name = 'kdiff'").fetchone() or \
            not con.execute("SELECT COUNT(*) FROM kdiff").fetchone()[0]:
        pytest.skip("名簿の比較のデータがありません（kijun_diff.parquet）")
    month = con.execute("SELECT MAX(month) FROM kdiff").fetchone()[0]
    n_new = con.execute("SELECT COUNT(*) FROM kdiff WHERE month = ? AND kind = '新規'", (month,)).fetchone()[0]
    n = lambda html: int(re.search(r'class="count">([\d,]+) 件', html).group(1).replace(",", ""))
    r = client.get("/changes")
    assert r.status_code == 200 and "noindex" not in r.text
    assert n(client.get(f"/changes?month={month}&kind=新規").text) == n_new
    # 県だけ選ぶと、その県の名簿が最後に更新された月になる
    p = con.execute("SELECT pref_code, MAX(month) AS m FROM kdiff GROUP BY pref_code ORDER BY m LIMIT 1").fetchone()
    r = client.get(f"/changes?pref={p['pref_code']}")
    assert n(r.text) == con.execute("SELECT COUNT(*) FROM kdiff WHERE pref_code = ? AND month = ?", (p["pref_code"], p["m"])).fetchone()[0]
    # 区分の変更は前の届出名と組になっている
    chg = con.execute("SELECT * FROM kdiff WHERE kind = '変更' LIMIT 1").fetchone()
    assert chg["prev_name"] and chg["prev_name"] != chg["kijun_name"]
    rows = list(_csv.reader(io.StringIO(client.get(f"/changes.csv?month={month}&kind=変更").content.decode("utf-8-sig"))))
    assert rows[0][7] == "動き" and rows[1][7] == "変更" and rows[1][9]
    # 医療機関ページ・施設基準ページ
    fid = con.execute("SELECT k.facility_id FROM kdiff k JOIN facilities f ON f.id = k.facility_id LIMIT 1").fetchone()[0]
    assert "届出の動き" in client.get(f"/facility/{fid}").text
    assert 'href="/changes"' in client.get("/").text


def test_changes_shika_yakkyoku():
    """歯科・薬局の新規届出・取り下げ（/shika/changes・/yakkyoku/changes）：件数・CSV・施設ページの「動き」・/shika/{id}と衝突しない。"""
    import sqlite3
    con = sqlite3.connect(appmod.DB_PATH)
    con.row_factory = sqlite3.Row
    for base, t, fac in (("/shika", "d_kdiff", "d_fac"), ("/yakkyoku", "y_kdiff", "y_fac")):
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (t,)).fetchone() or \
                not con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]:
            continue
        month = con.execute(f"SELECT MAX(month) FROM {t}").fetchone()[0]
        n_all = con.execute(f"SELECT COUNT(*) FROM {t} WHERE month = ?", (month,)).fetchone()[0]
        r = client.get(f"{base}/changes")
        assert r.status_code == 200 and 'class="cur" aria-current="page"' in r.text
        assert int(re.search(r'class="count">([\d,]+) 件', r.text).group(1).replace(",", "")) == n_all
        csv_text = client.get(f"{base}/changes.csv").content.decode("utf-8-sig")
        import csv as _csv
        import io
        assert sum(1 for r in _csv.reader(io.StringIO(csv_text)) if len(r) > 5) == n_all + 1   # 末尾の出典の行は除く
        fid = con.execute(f"SELECT k.facility_id FROM {t} k JOIN {fac} f ON f.id = k.facility_id LIMIT 1").fetchone()
        if fid:
            assert len(fid[0]) == 10 and "届出の動き" in client.get(f"{base}/{fid[0]}").text


def test_yk_index_merged_rows():
    """歯科・薬局のトップ：略称（受理記号）のチェックボックスで選び、「説明つき」の表は区分違いを1行にまとめて連動させる。"""
    for base in ("/shika", "/yakkyoku"):
        r = client.get(base)
        assert r.status_code == 200
        ids = re.findall(r'name="item" value="(\d+)" data-label=', r.text)
        assert ids and len(ids) == len(set(ids))   # 検索に送るチェックボックスは届出1つにつき1つ
        assert 'id="yk-filter"' in r.text and 'data-view="d"' in r.text
        for grp in re.findall(r'class="sp-grp" data-ids="([\d.]+)"', r.text):   # まとめた行の中身は略称の一覧にある
            assert set(grp.split(".")) <= set(ids)
        assert client.get(f"{base}/search?item={ids[0]}").status_code == 200


def test_facility_pages_show_navi_hours():
    """医療情報ネットとつながった医科・歯科・薬局のページに、診療科・診療時間・休診日と出典が出る。"""
    with appmod.db() as con:
        clinic = con.execute("""SELECT f.id FROM facilities f JOIN navi n ON n.facility_id = f.id
                                WHERE f.ftype = '無床診療所' AND n.sched != '' AND n.closed != '' LIMIT 1""").fetchone()[0]
        dent = con.execute("SELECT f.id FROM d_fac f JOIN navi n ON n.facility_id = f.id WHERE n.sched != '' LIMIT 1").fetchone()[0]
        pharm = con.execute("SELECT f.id FROM y_fac f JOIN navi n ON n.facility_id = f.id WHERE n.sched != '' LIMIT 1").fetchone()[0]
    html = _get(f"/facility/{clinic}")
    assert 'id="hours"' in html and "休診日" in html and 'class="hours"' in html
    assert "医療情報ネットのオープンデータ" in html and "の診療時間・診療科・施設基準" in html
    assert "http://https" not in html
    # 診療時間と施設基準はタブで分ける（パネルは両方HTMLに入っている）
    assert 'data-tab="hours"' in html and 'data-panel="hours"' in html and 'data-panel="kijun"' in html
    assert 'id="hours"' in _get(f"/shika/{dent}")
    p = _get(f"/yakkyoku/{pharm}")
    assert "営業時間" in p and ">営業<" in p


def test_homepage_link_under_name():
    """ホームページ（医療情報ネット）は医療機関名のすぐ下に出す。"""
    with appmod.db() as con:
        fid = con.execute("SELECT f.id FROM facilities f JOIN navi n ON n.facility_id = f.id WHERE n.url != '' LIMIT 1").fetchone()[0]
    html = _get(f"/facility/{fid}")
    assert html.index('class="hp-link"') < html.index('class="summary"')


def test_facility_pdf_with_qr():
    """医科・歯科・薬局のページをPDFで出せる（ページへのリンクボタンがあり、robots.txtで巡回させない）。"""
    with appmod.db() as con:
        fid = con.execute("SELECT id FROM facilities WHERE byosho_code IS NOT NULL LIMIT 1").fetchone()[0]
        dent = con.execute("SELECT id FROM d_fac LIMIT 1").fetchone()[0]
        pharm = con.execute("SELECT id FROM y_fac LIMIT 1").fetchone()[0]
    for path in (f"/facility/{fid}.pdf", f"/shika/{dent}.pdf", f"/yakkyoku/{pharm}.pdf"):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["content-type"] == "application/pdf", path
        assert r.content.startswith(b"%PDF") and b"/FontFile2" in r.content   # 日本語フォントを埋め込んでいる
    assert f'href="/facility/{fid}.pdf"' in client.get(f"/facility/{fid}").text
    assert client.get("/facility/000000000.pdf").status_code == 404
    assert "Disallow: /*.pdf" in client.get("/robots.txt").text


def test_favicon_and_site_name():
    """ファビコン（直下とstatic）と、トップのサイト名の構造化データ。"""
    assert client.get("/favicon.ico").headers["content-type"] == "image/x-icon"
    assert client.get("/apple-touch-icon.png").status_code == 200
    assert client.get("/static/favicon.svg").status_code == 200
    assert client.get("/site.webmanifest").json()["name"] == "MedilenZ"
    top = client.get("/").text
    assert 'rel="icon" href="/favicon.ico"' in top and '"@type": "WebSite"' in top and '"name": "MedilenZ"' in top
    assert '"@type": "WebSite"' not in client.get("/about").text   # サイト名の構造化データはトップだけ



def test_search_add_columns():
    """検索結果に「列を追加する」で病床数・稼働率・医師数などを足せ、CSVにも同じ列が入る。"""
    q = [("pref", "39"), ("ftype", "病院"), ("add", "beds"), ("add", "occ"), ("add", "doc")]
    r = client.get("/search", params=q)
    assert r.status_code == 200
    assert "列を追加する" in r.text and "病床稼働率" in r.text and "常勤医師数" in r.text
    assert 'name="add" value="occ" checked' in r.text
    t = client.get("/search.csv", params=q).text
    head = t.lstrip("﻿").splitlines()[0]
    assert head.endswith("許可病床数（床）,病床稼働率（%）,常勤医師数（人）")
    assert "在棟延べ数÷（許可病床数×365）" in t
    # 施設基準名で探した「届出1行ずつ」のCSVにも足せる
    t = client.get("/search.csv", params=[("pref", "39"), ("kx", "急性期看護補助体制加算"), ("add", "beds")]).text
    assert t.lstrip("﻿").splitlines()[0].endswith("許可病床数（床）")


def test_search_exclude_items():
    """「これがあって、あれがない」：item で届け出ている、nx で届け出ていない項目を指定できる。"""
    import sqlite3
    con = sqlite3.connect(str(appmod.DB_PATH))
    a, b = [r[0] for r in con.execute("SELECT id FROM items ORDER BY n_facilities DESC LIMIT 2")]
    base = [("pref", "13"), ("ftype", "病院")]

    def n(q):
        return int(re.search(r"([\d,]+) 件", client.get("/search", params=q).text).group(1).replace(",", ""))

    has_a, has_ab = n(base + [("item", a)]), n(base + [("item", a), ("item", b)])
    assert n(base + [("item", a), ("nx", b)]) == has_a - has_ab
    assert n(base) - n(base + [("nx", a)]) == has_a
    r = client.get("/search", params=base + [("item", a), ("nx", b)])
    assert "届け出ていない" in r.text
    assert "どれも届け出ていない" in client.get("/search.csv", params=base + [("nx", b)]).text
    assert f'name="nx" value="{b}" checked' in client.get("/", params=base + [("nx", b)]).text


def test_search_show_only_columns():
    """「列を追加する」の届出の列（sx）：絞り込まず、届け出ている施設に○。古い nxmode=show のURLも同じ意味で読む。"""
    import sqlite3
    con = sqlite3.connect(str(appmod.DB_PATH))
    a, b = [r[0] for r in con.execute("SELECT id FROM items ORDER BY n_facilities DESC LIMIT 2")]
    base = [("pref", "13"), ("ftype", "病院"), ("item", a)]

    def n(q):
        return int(re.search(r"([\d,]+) 件", client.get("/search", params=q).text).group(1).replace(",", ""))

    assert n(base + [("sx", b)]) == n(base) == n(base + [("nx", b), ("nxmode", "show")])
    t = client.get("/search.csv", params=base + [("sx", b)]).text.lstrip("\ufeff").splitlines()
    assert any(line.endswith(",○,○") for line in t) and any(line.endswith(",○,") for line in t)
    assert "列だけ表示した届出" in "\n".join(t)
    r = client.get("/search", params=base + [("sx", b)])
    assert 'name="sx" value="%d"' % b in r.text
    j = client.get("/items/suggest.json", params={"q": "地域包括"}).json()
    assert j and all(x["key"][0] in "ib" for x in j)


def test_gone_and_old_urls():
    """消したページ（/vision）は転送、名簿から消えた医療機関は410で事情を出す、ほかの404はGAに記録。"""
    import sqlite3
    r = client.get("/vision/高知県/中央", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"].startswith("/area/")
    con = sqlite3.connect(str(appmod.DB_PATH))
    row = con.execute("""SELECT facility_id FROM kdiff WHERE kind = '取り下げ'
                         AND facility_id NOT IN (SELECT id FROM facilities) LIMIT 1""").fetchone()
    if row:
        r = client.get(f"/facility/{row[0]}")
        assert r.status_code == 410 and "名簿から削除されました" in r.text
    r = client.get("/no-such-page")
    assert r.status_code == 404 and "page_not_found" in r.text


def test_grouped_items_any_within_group():
    """診療科から探すのまとめた行（item=12.13.14）は、行の中はいずれか、行どうしはすべて。"""
    import sqlite3
    con = sqlite3.connect(str(appmod.DB_PATH))
    ids = [r[0] for r in con.execute(
        """SELECT i.id FROM items i JOIN kijun kj ON kj.id = i.kijun_id
           WHERE kj.name LIKE '脳血管疾患等リハビリテーション料%' ORDER BY i.id""")]
    assert len(ids) >= 2
    g = ".".join(map(str, ids))

    def n(q):
        return int(re.search(r"([\d,]+) 件", client.get("/search", params=q).text).group(1).replace(",", ""))

    base = [("pref", "13"), ("ftype", "病院"), ("mode", "and")]
    assert n(base + [("item", g)]) == n(base + [("item", g), ("mode", "or")]) > 0
    assert n(base + [("item", str(i)) for i in ids]) < n(base + [("item", g)])
    assert f"item={g.replace('.', '.')}" in client.get("/search", params=base + [("item", g)]).text.replace("%2E", ".")


def test_ga_events_have_no_raw_template_code():
    """GAのイベントのJSに、Jinjaの式が{{ }}の外でそのまま出ていない（歯科・薬局の検索で一度あった）。"""
    for u in ["/shika/search?pref=39", "/yakkyoku/search?pref=13", "/search?pref=39"]:
        t = client.get(u).text
        assert "K.word ~" not in t and "content_group: K." not in t
