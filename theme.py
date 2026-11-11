"""MedilenZ の色（DESIGN.md「2. 色」「グラフの色」）を Python から使うための定数。

HTML/CSS では CSS 変数（var(--brand) など）を使い、Plotly・folium・iframe の中など
CSS 変数が届かない所だけ、ここの値を使う。16進数を直書きしないこと。
"""

# ── トークン（kijun_web/static/style.css の :root と同じ値）──
BRAND = "#12886D"
BRAND_DARK = "#0B5E4B"
BRAND_SOFT = "#EAF4F0"
BRAND_LINE = "#BFDFD4"
INK = "#26251F"
INK_MUTED = "#6E6A5E"
INK_FAINT = "#9C9890"
LINE = "#E8E4DB"
BG = "#FAF8F4"
CARD = "#FFFFFF"
OK = "#1A7F4B"
WARN = "#A8630A"
DANGER = "#B3362B"

# ── 病床機能4区分（順序のある区分。濃いほど急性度が高い）──
BED_COLORS = {
    "高度急性期": "#0B4F40",
    "急性期": "#12886D",
    "回復期": "#3FA587",
    "慢性期": "#79C2AA",
}

# 規模など「大きさ」を段階で示すとき（濃いほど大きい）。病床機能と同じ緑の濃淡。
SEQ4 = ["#0B4F40", "#12886D", "#3FA587", "#79C2AA"]

# ── 複数の系列を見分けるときの色（この順で使う。入れ替えない・8色まで）──
CATEGORICAL = [
    "#2a78d6",  # 青
    "#eb6834",  # 橙
    "#1baf7a",  # 水色
    "#eda100",  # 黄
    "#e87ba4",  # 桃
    "#008300",  # 緑
    "#6250d6",  # 紫
    "#e34948",  # 赤
]

# 強調しない系列・その他（グラフで「選択中の病院」以外を表す時など）
MUTED_SERIES = "#CFC9BD"
