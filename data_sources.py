"""公表データの出典表示（公共データ利用規約 第1.0版＝PDL1.0 への準拠）。

厚生労働省（地方厚生局を含む）のWebサイトのコンテンツは、権利表記が無い限り
PDL1.0 の下で利用できる（https://www.mhlw.go.jp/chosakuken/index.html）。
PDL1.0 が利用者に求めることは主に次の3つ:

1. 出典を記載する（例:「出典：「○○動向調査」（厚生労働省）（当該ページのURL）」）
2. 編集・加工した場合は、出典とは別に加工した旨を記載する
   （例:「「○○」（厚生労働省）（URL）を加工して作成」）
3. 加工した情報を、あたかも国（府省等）が作成したかのような態様で公表・利用しない

このアプリの画面・PDF・CSVはいずれも公表データを集計・突合・指標計算した
「加工物」なので、各表示箇所の出典には「を加工して作成」を付け、一覧（LP・
アプリのフッター）では出典URLと、国が作成したものではない旨を明記する。

**新しいデータ種別を取り込んだら、SOURCES に1行追加し、データ期間も更新すること**
（CLAUDE.md「データ取込み状況」の表と同じタイミングで）。
"""

PDL_URL = "https://www.digital.go.jp/resources/open_data/public_data_license_v1.0"
MHLW_TERMS_URL = "https://www.mhlw.go.jp/chosakuken/index.html"

SOURCES = [
    {
        "title": "病床機能報告",
        "publisher": "厚生労働省",
        "url": "https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/0000055891.html",
        "period": "令和4〜7年度報告（様式2の手術データを含む）",
    },
    {
        "title": "DPC導入の影響評価に係る調査（令和6年度以降は「DPCの評価・検証等に係る調査」）「退院患者調査」",
        "publisher": "厚生労働省",
        "url": "https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/0000049343.html",
        "period": "令和4〜6年度",
    },
    {
        "title": "外来機能報告",
        "publisher": "厚生労働省",
        "url": "https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/open_data_00021.html",
        "period": "令和4〜7年度報告",
    },
    {
        "title": "施設基準の届出受理状況（届出受理医療機関名簿）",
        "publisher": "各地方厚生局",
        "url": "https://kouseikyoku.mhlw.go.jp/",
        "period": "2026年7月時点（新規・変更／失効の届出は2026年7月12日公表分まで反映）",
    },
    {
        "title": "医療情報ネットのオープンデータ（医療機関の所在地・座標）",
        "publisher": "厚生労働省",
        "url": "https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/kenkou_iryou/iryou/newpage_43373.html",
        "period": "2025年6月1日時点",
    },
]

NOT_GOVT_NOTICE = (
    "本サービスで表示・出力する集計結果・指標・グラフ・ランキング等は、上記の公表データを"
    "MedilenZが編集・加工して作成したものであり、厚生労働省・地方厚生局が作成したもの"
    "ではありません。また、本サービスは国の機関の承認・推奨を受けたものではありません。"
)

FOOTER_CREDIT = (
    "出典：厚生労働省「病床機能報告」「DPC導入の影響評価に係る調査」「外来機能報告」、"
    "各地方厚生局「施設基準の届出受理状況」等を加工して作成"
)


def credit(title: str, publisher: str = "厚生労働省", detail: str = "") -> str:
    """個別の表・グラフに添える出典（加工した旨を含む）。

    例: credit("令和7年度病床機能報告") → 「令和7年度病床機能報告」（厚生労働省）を加工して作成
    """
    text = f"「{title}」（{publisher}）を加工して作成"
    if detail:
        text += f"（{detail}）"
    return text


def attribution_markdown() -> str:
    """LP・アプリのフッターに置く「データの出典と利用条件」の本文（Markdown）。"""
    lines = [
        "本サービスは、以下の公表データを編集・加工して作成しています。",
        "",
    ]
    for s in SOURCES:
        lines.append(
            f"- 出典：「{s['title']}」（{s['publisher']}）（[{s['url']}]({s['url']})）"
            f"　… 利用データ：{s['period']}"
        )
    lines += [
        "",
        "これらのデータは「[公共データ利用規約（第1.0版）]"
        f"({PDL_URL})」（PDL1.0）"
        f"（[厚生労働省の利用規約]({MHLW_TERMS_URL})）に基づいて利用しています。",
        "",
        NOT_GOVT_NOTICE,
        "",
        "本サービスからダウンロードしたデータ（CSV・PDF等）を第三者に提供・公表する場合は、"
        "上記の出典と、MedilenZが加工したものである旨を記載してください。",
    ]
    return "\n".join(lines)
