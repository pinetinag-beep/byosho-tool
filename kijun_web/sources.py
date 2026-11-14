"""出典の書き方（公共データ利用規約 第1.0版＝PDL1.0）。

地方厚生局の利用規約（例：関東信越厚生局「利用規約・リンク・著作権等」）の記載例どおり、
出典には「ページの名前」（厚生局名）（ページのURL）（利用した日）を書き、加工した旨を添える:
  「○○県内の保険医療機関・保険薬局の指定一覧」（関東信越厚生局）（URL）を加工して作成
利用した日は、fetch_kouseikyoku.py --apply が県ごとに shisetsu_raw/fetched.json に書く
（名簿を取った日）。
"""
from __future__ import annotations

BASE = "https://kouseikyoku.mhlw.go.jp"

# (厚生局名, 名簿のページの名前, ページのパス, 管轄の都道府県)
BUREAUS = [
    ("北海道厚生局", "施設基準等の届出事項（届出受理医療機関名簿）",
     "/hokkaido/gyomu/gyomu/hoken_kikan/todokede_juri_ichiran.html", ["北海道"]),
    ("東北厚生局", "施設基準の届出等受理医療機関名簿",
     "/tohoku/gyomu/gyomu/hoken_kikan/documents/201805koushin.html",
     ["青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県"]),
    ("関東信越厚生局", "保険医療機関・保険薬局の施設基準の届出受理状況及び保険外併用療養費医療機関一覧",
     "/kantoshinetsu/chousa/kijyun.html",
     ["茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県", "新潟県", "山梨県", "長野県"]),
    ("東海北陸厚生局", "施設基準の届出受理状況（全体）", "/tokaihokuriku/newpage_00349.html",
     ["富山県", "石川県", "岐阜県", "静岡県", "愛知県", "三重県"]),
    ("近畿厚生局", "保険医療機関・保険薬局の施設基準の届出受理状況及び保険外併用療養費の報告状況",
     "/kinki/gyomu/gyomu/hoken_kikan/shitei_jokyo_00004.html",
     ["福井県", "滋賀県", "京都府", "大阪府", "兵庫県", "奈良県", "和歌山県"]),
    ("中国四国厚生局", "保険医療機関等・指定訪問看護事業所の届出受理状況等について",
     "/chugokushikoku/chousaka/shisetsukijunjuri.html", ["鳥取県", "島根県", "岡山県", "広島県", "山口県"]),
    ("四国厚生支局", "保険医療機関・保険薬局の管内指定状況等について",
     "/shikoku/gyomu/gyomu/hoken_kikan/shitei/index.html", ["徳島県", "香川県", "愛媛県", "高知県"]),
    ("九州厚生局", "届出受理医療機関名簿", "/kyushu/gyomu/gyomu/hoken_kikan/index_00007.html",
     ["福岡県", "佐賀県", "長崎県", "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県"]),
]

# 都道府県名 → (厚生局名, ページの名前, URL)
BUREAU_OF_PREF = {p: (b, title, BASE + path) for b, title, path, prefs in BUREAUS for p in prefs}

# 厚生労働省のデータ（病院ページの病床機能報告・DPC・外来機能報告）
MHLW_URLS = {
    "病床機能報告": "https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/0000055891.html",
    "DPC": "https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/0000049343.html",
    "外来機能報告": "https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/open_data_00021.html",
}
