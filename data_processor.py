"""
病床機能報告データの読み込み・正規化・集計処理
"""
import io
import pandas as pd
import numpy as np

BED_TYPES = ["高度急性期", "急性期", "回復期", "慢性期"]
BED_COLORS = {
    "高度急性期": "#e74c3c",
    "急性期":     "#e67e22",
    "回復期":     "#2ecc71",
    "慢性期":     "#3498db",
}

# 都道府県コード → 都道府県名
PREF_CODE_MAP = {
    "01": "北海道", "02": "青森県", "03": "岩手県", "04": "宮城県", "05": "秋田県",
    "06": "山形県", "07": "福島県", "08": "茨城県", "09": "栃木県", "10": "群馬県",
    "11": "埼玉県", "12": "千葉県", "13": "東京都", "14": "神奈川県", "15": "新潟県",
    "16": "富山県", "17": "石川県", "18": "福井県", "19": "山梨県", "20": "長野県",
    "21": "岐阜県", "22": "静岡県", "23": "愛知県", "24": "三重県", "25": "滋賀県",
    "26": "京都府", "27": "大阪府", "28": "兵庫県", "29": "奈良県", "30": "和歌山県",
    "31": "鳥取県", "32": "島根県", "33": "岡山県", "34": "広島県", "35": "山口県",
    "36": "徳島県", "37": "香川県", "38": "愛媛県", "39": "高知県", "40": "福岡県",
    "41": "佐賀県", "42": "長崎県", "43": "熊本県", "44": "大分県", "45": "宮崎県",
    "46": "鹿児島県", "47": "沖縄県",
}

REQUIRED_COLS = [
    "報告年度", "都道府県名", "二次医療圏名", "医療機関名",
    "高度急性期_許可病床数", "急性期_許可病床数", "回復期_許可病床数", "慢性期_許可病床数",
    "合計_許可病床数", "合計_稼働病床数",
]

# 様式1・2の機能区分列名（年度で変わる可能性があるためキーワード検索）
_FUNC_COL_KEYWORD = "7月1日時点の機能"
_FUNC_COL_NEXT_KEYWORD = "7月1日の病床機能の予定"


def _find_col(cols, keyword):
    """列名リストからキーワードを含む最初の列名を返す"""
    for c in cols:
        if keyword in str(c):
            return c
    return None


# ── MHLW公式データ専用ローダー ────────────────────────────────


def load_mhlw_byosho(file_bytes: bytes, year: int = 2024) -> pd.DataFrame:
    """
    厚労省 病床機能報告 様式1・2病棟票Excelを読み込んで
    病院単位・病床種別に集計した標準形DataFrameを返す。
    ヘッダーは5行構造（row4が実質ヘッダー、row5は必須/任意区分）。
    稼働率用に在棟患者延べ数も集計する。
    """
    df = pd.read_excel(io.BytesIO(file_bytes), header=4, skiprows=[5])
    df.columns = [str(c).strip() for c in df.columns]

    # 機能区分列を特定
    func_col = _find_col(df.columns, _FUNC_COL_KEYWORD)
    if func_col is None:
        raise ValueError("機能区分列が見つかりません。様式1・2病棟票ファイルか確認してください。")

    # 医療機関コード列（列名が改行や注釈付きなので部分一致）
    code_col = _find_col(df.columns, "医療機関コード")
    # 在棟患者延べ数列（稼働率の正確な計算に使用）
    zaitou_col_name = _find_col(df.columns, "在棟患者延べ数")

    # 病床数列
    ippan_kyoka = "一般病床_許可病床"
    ryoyo_kyoka = "療養病床_許可病床"
    ippan_max   = "一般病床_最大使用病床数"
    ryoyo_max   = "療養病床_最大使用病床数"

    # 必要列だけ抽出して新しいDataFrameを作る（断片化警告を避けるため）
    pref_code_col = "都道府県コード"

    # 二次医療圏列：「名」付きを優先、なければ広く検索（コード列より名前列を先に取る）
    iryo_col = _find_col(df.columns, "二次医療圏名") or _find_col(df.columns, "二次医療圏名称") or _find_col(df.columns, "二次医療圏")

    keep_cols = [func_col]
    if code_col:
        keep_cols = [code_col] + keep_cols
    for c in ["医療機関名", pref_code_col]:
        if c in df.columns:
            keep_cols.append(c)
    if iryo_col:
        keep_cols.append(iryo_col)
    for c in [ippan_kyoka, ryoyo_kyoka, ippan_max, ryoyo_max]:
        if c in df.columns:
            keep_cols.append(c)
    if zaitou_col_name and zaitou_col_name not in keep_cols:
        keep_cols.append(zaitou_col_name)

    df = df[keep_cols].copy()

    for col in [ippan_kyoka, ryoyo_kyoka, ippan_max, ryoyo_max]:
        if col not in df.columns:
            df[col] = 0
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    df["_許可病床計"] = df[ippan_kyoka] + df[ryoyo_kyoka]
    df["_最大使用計"] = df[ippan_max]   + df[ryoyo_max]

    # 在棟患者延べ数（病棟単位 → 後で病院・機能区分単位に集計）
    if zaitou_col_name and zaitou_col_name in df.columns:
        df["_在棟延べ数"] = pd.to_numeric(df[zaitou_col_name], errors="coerce").fillna(0)
    else:
        df["_在棟延べ数"] = 0

    # 都道府県名変換
    if pref_code_col in df.columns:
        df["都道府県名"] = df[pref_code_col].astype(str).str.zfill(2).map(PREF_CODE_MAP)
    elif "都道府県名" not in df.columns:
        df["都道府県名"] = "不明"

    # 二次医療圏列名を「二次医療圏名」に統一（年度差異を吸収）
    if iryo_col and iryo_col != "二次医療圏名":
        df = df.rename(columns={iryo_col: "二次医療圏名"})
    if "二次医療圏名" not in df.columns:
        df["二次医療圏名"] = "不明"

    # 有効な機能区分のみ抽出（休棟中等を除外）
    valid_funcs = set(BED_TYPES)
    df = df[df[func_col].isin(valid_funcs)].copy()

    # 病院単位に集計するためのキー列
    group_keys = ["医療機関名", "都道府県名", "二次医療圏名"]
    if code_col:
        group_keys = [code_col] + group_keys

    # 機能区分 × 病院 でピボット集計
    agg_kyoka  = df.groupby(group_keys + [func_col])["_許可病床計"].sum().unstack(fill_value=0)
    agg_kado   = df.groupby(group_keys + [func_col])["_最大使用計"].sum().unstack(fill_value=0)
    agg_zaitou = df.groupby(group_keys + [func_col])["_在棟延べ数"].sum().unstack(fill_value=0)

    result = pd.DataFrame(index=agg_kyoka.index)

    for t in BED_TYPES:
        result[f"{t}_許可病床数"] = agg_kyoka.get(t, 0)
        result[f"{t}_稼働病床数"] = agg_kado.get(t, 0)
        result[f"{t}_在棟延べ数"] = agg_zaitou.get(t, 0)

    result = result.reset_index()

    # コード列名の正規化（int64 で読まれる場合も str に統一）
    if code_col and code_col in result.columns:
        result = result.rename(columns={code_col: "医療機関コード"})
    if "医療機関コード" in result.columns:
        result["医療機関コード"] = result["医療機関コード"].astype(str).str.strip()

    result["合計_許可病床数"] = sum(result.get(f"{t}_許可病床数", 0) for t in BED_TYPES)
    result["合計_稼働病床数"] = sum(result.get(f"{t}_稼働病床数", 0) for t in BED_TYPES)
    result["合計_在棟延べ数"] = sum(result.get(f"{t}_在棟延べ数", 0) for t in BED_TYPES)
    result["報告年度"] = year

    return result[result["合計_許可病床数"] > 0].copy()


def load_mhlw_shisetsu(file_bytes: bytes) -> pd.DataFrame:
    """
    厚労省 病床機能報告 施設票Excelを読み込んで
    スタッフ数・医療設備・救急搬送件数のDataFrameを返す。
    様式1と医療機関コードでマージして使う。
    """
    df = pd.read_excel(io.BytesIO(file_bytes), header=4, skiprows=[5])
    df.columns = [str(c).strip() for c in df.columns]

    code_col = _find_col(df.columns, "医療機関コード")

    keep = {}
    if code_col:
        keep["医療機関コード"] = df[code_col].astype(str).str.strip()

    # ── スタッフ数 ──
    staff_kw = [
        ("施設全体_医師_常勤",   "常勤医師数"),
        ("常勤_医師",            "常勤医師数"),
        ("常勤医師",             "常勤医師数"),
        ("施設全体_看護師_常勤", "常勤看護師数"),
        ("常勤_看護師",          "常勤看護師数"),
        ("常勤看護師",           "常勤看護師数"),
        ("救急車の受入件数",     "救急搬送件数"),
        ("救急搬送",             "救急搬送件数"),
    ]
    for kw, dst in staff_kw:
        if dst not in keep:
            col = _find_col(df.columns, kw)
            if col:
                keep[dst] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)

    # ── 医療設備（実際の列名に基づくキーワード検索）──
    equip_kw = [
        # ── CT内訳（マルチスライス列数別）──
        # 注: 実列名 "CT_マルチスライス_64列以上" など
        ("64列以上",                "CT_64列以上"),
        ("16列以上64列未満",        "CT_16〜64列"),
        ("マルチスライス_16列未満", "CT_16列未満"),
        ("CT_その他",               "CT_その他"),
        # ── MRI内訳（テスラ別）──
        # 注: 3T以上はT半角、1.5TはＴ全角（U+FF34）
        ("MRI_3T",                  "MRI_3T以上"),
        ("1.5Ｔ以上",           "MRI_1.5〜3T"),   # Ｔ (U+FF34 全角)
        ("1.5Ｔ未満",           "MRI_1.5T未満"),  # Ｔ (U+FF34 全角)
        # ── PET系 ──
        ("PETCT",                   "PETCT台数"),
        ("PETMRI",                  "PETMRI台数"),
        ("PET",                     "PET台数"),       # PETCT/MRI の後に検索
        # ── 放射線治療系 ──
        ("強度変調放射線治療器",     "IMRT台数"),
        ("ガンマナイフ",             "ガンマナイフ台数"),
        ("サイバーナイフ",           "サイバーナイフ台数"),
        # ── その他 ──
        ("内視鏡手術用支援機器",     "内視鏡手術支援機器台数"),
        ("血管連続撮影",             "血管連続撮影装置台数"),
        ("SPECT",                   "SPECT台数"),
        ("マンモグラフィ",           "マンモグラフィ台数"),
    ]
    for kw, dst in equip_kw:
        col = _find_col(df.columns, kw)
        if col:
            keep[dst] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)

    # CT台数 = 内訳合計（列が存在する場合）
    ct_sub = ["CT_64列以上", "CT_16〜64列", "CT_16列未満", "CT_その他"]
    ct_available = [c for c in ct_sub if c in keep]
    if ct_available:
        keep["CT台数"] = sum(keep[c] for c in ct_available)

    # MRI台数 = 内訳合計（列が存在する場合）
    mri_sub = ["MRI_3T以上", "MRI_1.5〜3T", "MRI_1.5T未満"]
    mri_available = [c for c in mri_sub if c in keep]
    if mri_available:
        keep["MRI台数"] = sum(keep[c] for c in mri_available)

    return pd.DataFrame(keep)


def merge_shisetsu(hospital_df: pd.DataFrame, shisetsu_df: pd.DataFrame) -> pd.DataFrame:
    """
    施設票DataFrameを病院単位DataFrameに左結合する（医療機関コードで結合）。
    スタッフ数・医療設備列が hospital_df に追加される。
    型の不一致（int64 vs str）を防ぐため、結合前に両側を str に統一する。
    """
    if shisetsu_df is None or shisetsu_df.empty:
        return hospital_df
    if "医療機関コード" not in hospital_df.columns or "医療機関コード" not in shisetsu_df.columns:
        return hospital_df

    hosp = hospital_df.copy()
    sshi = shisetsu_df.drop_duplicates(subset="医療機関コード").copy()

    # 両側を str に統一してからマージ
    hosp["医療機関コード"] = hosp["医療機関コード"].astype(str).str.strip()
    sshi["医療機関コード"] = sshi["医療機関コード"].astype(str).str.strip()

    merged = hosp.merge(sshi, on="医療機関コード", how="left", suffixes=("", "_s"))
    # サフィックス付き重複列を削除
    merged = merged.drop(columns=[c for c in merged.columns if c.endswith("_s")])
    return merged


def load_mhlw_yoshiki2(file_bytes: bytes, year: int = 2024) -> pd.DataFrame:
    """
    厚労省 病床機能報告 様式2病棟票（年間合計）Excelを読み込んで
    病院単位の手術件数DataFrameを返す。
    * は非公表（少数例マスク）→ 0 として扱う。
    """
    df = pd.read_excel(io.BytesIO(file_bytes), header=4, skiprows=[5])
    df.columns = [str(c).strip() for c in df.columns]

    code_col = _find_col(df.columns, "医療機関コード")
    pref_code_col = "都道府県コード"

    # ── 手術関連列のマッピング（術式別） ──
    surgery_cols = {
        "手術総数":                 "手術総数",
        "全身麻酔の手術総数":       "全身麻酔手術数",
        "人工心肺を用いた手術":     "人工心肺手術数",
        "胸腔鏡下手術":             "胸腔鏡下手術数",
        "腹腔鏡下手術":             "腹腔鏡下手術数",
        "内視鏡手術用支援機器手術": "ロボット支援手術数",
        "悪性腫瘍手術":             "悪性腫瘍手術数",
        "脳血管内手術":             "脳血管内手術数",
    }

    # ── 臓器別手術列のマッピング ──
    # 「臓器別の状況_XXX」= 全数、「臓器別の状況_XXX.1」= 全身麻酔の手術
    ORGAN_KWTABLE = [
        ("臓器別の状況_皮膚",   "皮膚・皮下組織"),
        ("臓器別の状況_筋骨格", "筋骨格系・四肢・体幹"),
        ("臓器別の状況_神経系", "神経系・頭蓋"),
        ("臓器別の状況_眼",     "眼"),
        ("臓器別の状況_耳鼻",   "耳鼻咽喉"),
        ("臓器別の状況_顔面",   "顔面・口腔・頸部"),
        ("臓器別の状況_胸部",   "胸部"),
        ("臓器別の状況_心",     "心・脈管"),
        ("臓器別の状況_腹部",   "腹部"),
        ("臓器別の状況_尿路",   "尿路系・副腎"),
        ("臓器別の状況_性器",   "性器"),
        ("臓器別の状況_歯科",   "歯科"),
    ]

    # 二次医療圏列：「名」付きを優先（コード列より名前列を先に取る）
    iryo_col_y2 = (_find_col(df.columns, "二次医療圏名")
                   or _find_col(df.columns, "二次医療圏名称")
                   or _find_col(df.columns, "二次医療圏"))

    # ── 必要列を抽出 ──
    keep_cols = []
    if code_col:
        keep_cols.append(code_col)
    for c in ["医療機関名", pref_code_col]:
        if c in df.columns:
            keep_cols.append(c)
    if iryo_col_y2:
        keep_cols.append(iryo_col_y2)

    # 術式別（キーワード一致）
    src_to_dst = {}
    for src, dst in surgery_cols.items():
        col = _find_col(df.columns, src)
        if col:
            keep_cols.append(col)
            src_to_dst[col] = dst

    # 臓器別（全数 = .1なし、全身麻酔 = .1あり）
    organ_src_to_dst = {}
    for kw, label in ORGAN_KWTABLE:
        col_t = next((c for c in df.columns if kw in c and not c.endswith(".1")), None)
        if col_t and col_t not in keep_cols:
            keep_cols.append(col_t)
            organ_src_to_dst[col_t] = f"手術_{label}"
        col_g = next((c for c in df.columns if kw in c and c.endswith(".1")), None)
        if col_g and col_g not in keep_cols:
            keep_cols.append(col_g)
            organ_src_to_dst[col_g] = f"全麻_{label}"

    df = df[keep_cols].copy()

    # * → NaN → 0 変換
    for col in list(src_to_dst.keys()) + list(organ_src_to_dst.keys()):
        df[col] = df[col].replace("*", np.nan)
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    # 都道府県名変換
    if pref_code_col in df.columns:
        df["都道府県名"] = df[pref_code_col].astype(str).str.zfill(2).map(PREF_CODE_MAP)

    # 二次医療圏列名を統一
    if iryo_col_y2 and iryo_col_y2 != "二次医療圏名":
        df = df.rename(columns={iryo_col_y2: "二次医療圏名"})
    if "二次医療圏名" not in df.columns:
        df["二次医療圏名"] = "不明"

    # 病院単位に集計（病棟単位を合算）
    group_keys = ["医療機関名", "都道府県名", "二次医療圏名"]
    if code_col:
        group_keys = [code_col] + group_keys

    all_val_cols = list(src_to_dst.keys()) + list(organ_src_to_dst.keys())
    agg = df.groupby(group_keys, as_index=False)[all_val_cols].sum()
    agg = agg.rename(columns={**src_to_dst, **organ_src_to_dst})
    if code_col and code_col in agg.columns:
        agg = agg.rename(columns={code_col: "医療機関コード"})
    agg["報告年度"] = year

    return agg[agg["手術総数"] > 0].copy()


def detect_mhlw_format(df_raw: pd.DataFrame) -> bool:
    """先頭数行を見てMHLW公式フォーマットか判定する"""
    cols = [str(c) for c in df_raw.columns]
    return any("7月1日時点の機能" in c or "一般病床_許可病床" in c for c in cols)


# ── 汎用ローダー ──────────────────────────────────────────────


def load_data(uploaded_file, year: int = 2024) -> pd.DataFrame:
    """
    アップロードされたファイルを読み込んでDataFrameを返す。
    MHLW公式様式1・2を自動判定し、必要に応じて集計変換する。
    """
    name = uploaded_file.name.lower()
    raw_bytes = uploaded_file.read()

    if name.endswith(".csv"):
        for enc in ("utf-8-sig", "shift-jis", "cp932"):
            try:
                df = pd.read_csv(io.BytesIO(raw_bytes), encoding=enc)
                break
            except UnicodeDecodeError:
                pass
        return normalize(df)

    elif name.endswith((".xlsx", ".xls")):
        # MHLW様式1・2かどうかを先頭5行で判定
        probe = pd.read_excel(io.BytesIO(raw_bytes), header=4, skiprows=[5], nrows=3)
        probe.columns = [str(c).strip() for c in probe.columns]

        if detect_mhlw_format(probe):
            return load_mhlw_byosho(raw_bytes, year=year)
        else:
            df = pd.read_excel(io.BytesIO(raw_bytes))
            return normalize(df)
    else:
        raise ValueError("CSV または Excel ファイルをアップロードしてください")


def load_mhlw_byosho_extended(file_bytes: bytes, year: int = 2024) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    厚労省 病床機能報告 様式1・2病棟票Excelを読み込んで
    (hospital_df, ward_df) のタプルを返す。
    hospital_df は load_mhlw_byosho と同等の病院単位集計。
    ward_df は病棟単位の詳細DataFrame。
    """
    df = pd.read_excel(io.BytesIO(file_bytes), header=4, skiprows=[5])
    df.columns = [str(c).strip() for c in df.columns]

    # 機能区分列を特定
    func_col = _find_col(df.columns, _FUNC_COL_KEYWORD)
    if func_col is None:
        raise ValueError("機能区分列が見つかりません。様式1・2病棟票ファイルか確認してください。")

    code_col = _find_col(df.columns, "医療機関コード")

    # ── 病棟単位 DataFrame の構築 ──
    # 必要な列をキーワード検索で特定する補助関数（位置インデックス番号ベース）
    cols_list = list(df.columns)

    def _col_by_keyword(keyword):
        """列名にキーワードを含む最初の列名を返す（_find_col の別名）"""
        return _find_col(cols_list, keyword)

    # 列の特定（仕様書に記載の列番号を参考にキーワード検索）
    nyuuin_kihonryo_col   = _col_by_keyword("算定する入院基本料・特定入院料") or (cols_list[26] if len(cols_list) > 26 else None)
    todoke_byosho_col     = None  # 届出病床数は入院基本料列の次の列
    # 届出病床数: 同一キーワードのうち数値列を探す
    for c in cols_list:
        if "届出病床数" in c:
            todoke_byosho_col = c
            break

    # 入院基本料列と届出病床数列（連続する場合は位置で取得）
    if nyuuin_kihonryo_col and todoke_byosho_col is None:
        idx = cols_list.index(nyuuin_kihonryo_col)
        if idx + 1 < len(cols_list):
            todoke_byosho_col = cols_list[idx + 1]

    # 各種患者数・退棟先列
    shinki_col   = _col_by_keyword("新規入棟患者数")
    kyukyu_col   = _col_by_keyword("うち、予定外の救急医療入院の患者")
    taitou_col   = _col_by_keyword("退棟患者数")
    zaitou_ward_col = _col_by_keyword("在棟患者延べ数")
    katei_col    = _col_by_keyword("うち家庭へ退院")
    tain_col     = _col_by_keyword("うち、他の病院、診療所へ転院")
    roken_col    = _col_by_keyword("うち、介護老人保健施設に入所")
    tokuyo_col   = _col_by_keyword("うち、介護老人福祉施設に入所")
    kaigo_iryoin_col = _col_by_keyword("うち、介護医療院に入所")
    yurou_col    = _col_by_keyword("うち、社会福祉施設・有料老人ホーム等に入所")
    shimon_col   = _col_by_keyword("うち、終了（死亡退院等）")

    pref_code_col = "都道府県コード"

    ippan_kyoka = "一般病床_許可病床"
    ryoyo_kyoka = "療養病床_許可病床"
    ippan_max   = "一般病床_最大使用病床数"
    ryoyo_max   = "療養病床_最大使用病床数"

    # 都道府県名変換
    if pref_code_col in df.columns:
        df["都道府県名"] = df[pref_code_col].astype(str).str.zfill(2).map(PREF_CODE_MAP)
    elif "都道府県名" not in df.columns:
        df["都道府県名"] = "不明"

    # 二次医療圏列名を「二次医療圏名」に統一（年度差異を吸収、名前列を優先）
    iryo_col_ext = (_find_col(list(df.columns), "二次医療圏名")
                    or _find_col(list(df.columns), "二次医療圏名称")
                    or _find_col(list(df.columns), "二次医療圏"))
    if iryo_col_ext and iryo_col_ext != "二次医療圏名":
        df = df.rename(columns={iryo_col_ext: "二次医療圏名"})
    if "二次医療圏名" not in df.columns:
        df["二次医療圏名"] = "不明"

    for col in [ippan_kyoka, ryoyo_kyoka, ippan_max, ryoyo_max]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        else:
            df[col] = 0

    df["_許可病床計"] = df[ippan_kyoka] + df[ryoyo_kyoka]
    df["_最大使用計"] = df[ippan_max]   + df[ryoyo_max]

    # 有効な機能区分のみ
    valid_funcs = set(BED_TYPES)
    df_valid = df[df[func_col].isin(valid_funcs)].copy()

    # ── ward_df の構築 ──
    ward_base_cols = []
    if code_col:
        ward_base_cols.append(code_col)
    for c in ["医療機関名", "都道府県名", "二次医療圏名"]:
        if c in df_valid.columns:
            ward_base_cols.append(c)

    ward_df = df_valid[ward_base_cols].copy()
    ward_df = ward_df.rename(columns={code_col: "医療機関コード"} if code_col else {})

    ward_df["機能区分"] = df_valid[func_col].values

    # 入院基本料
    if nyuuin_kihonryo_col:
        ward_df["入院基本料"] = df_valid[nyuuin_kihonryo_col].values
    else:
        ward_df["入院基本料"] = np.nan

    # 届出病床数
    if todoke_byosho_col:
        ward_df["届出病床数"] = pd.to_numeric(df_valid[todoke_byosho_col], errors="coerce").fillna(0).values
    else:
        ward_df["届出病床数"] = 0

    # 許可病床数（一般+療養）
    ward_df["許可病床数"] = df_valid["_許可病床計"].values
    # 最大使用病床数
    ward_df["最大使用病床数"] = df_valid["_最大使用計"].values

    def _safe_num(col_name):
        if col_name and col_name in df_valid.columns:
            return pd.to_numeric(df_valid[col_name], errors="coerce").fillna(0).values
        return np.zeros(len(df_valid))

    ward_df["新規入棟患者数"]   = _safe_num(shinki_col)
    ward_df["救急入院患者数"]   = _safe_num(kyukyu_col)
    ward_df["退棟患者数"]       = _safe_num(taitou_col)
    ward_df["在棟延べ数"]       = _safe_num(zaitou_ward_col)  # 稼働率計算用
    ward_df["家庭退院数"]       = _safe_num(katei_col)
    ward_df["他院転院数"]       = _safe_num(tain_col)
    ward_df["施設入所数"] = (
        _safe_num(roken_col) + _safe_num(tokuyo_col) +
        _safe_num(kaigo_iryoin_col) + _safe_num(yurou_col)
    )
    ward_df["死亡退院数"]       = _safe_num(shimon_col)
    ward_df["報告年度"]         = year

    ward_df = ward_df.reset_index(drop=True)

    # ── hospital_df（既存関数と同等） ──
    hospital_df = load_mhlw_byosho(file_bytes, year=year)

    return hospital_df, ward_df


def load_multiple_mhlw_extended(files_bytes: list[tuple[str, bytes]], year: int = 2024) -> tuple[pd.DataFrame, pd.DataFrame]:
    """複数の様式1・2ファイル（地域別）を結合して (hospital_df, ward_df) を返す"""
    hosp_dfs = []
    ward_dfs = []
    for fname, fbytes in files_bytes:
        try:
            h_df, w_df = load_mhlw_byosho_extended(fbytes, year=year)
            hosp_dfs.append(h_df)
            ward_dfs.append(w_df)
        except Exception as e:
            print(f"スキップ ({fname}): {e}")
    if not hosp_dfs:
        raise ValueError("有効なファイルが1件もありませんでした")
    hospital_df = pd.concat(hosp_dfs, ignore_index=True)
    ward_df = pd.concat(ward_dfs, ignore_index=True)
    return hospital_df, ward_df


def load_multiple_mhlw(files_bytes: list[tuple[str, bytes]], year: int = 2024) -> pd.DataFrame:
    """複数の様式1・2ファイル（地域別）を結合して返す"""
    dfs = []
    for fname, fbytes in files_bytes:
        try:
            dfs.append(load_mhlw_byosho(fbytes, year=year))
        except Exception as e:
            print(f"スキップ ({fname}): {e}")
    if not dfs:
        raise ValueError("有効なファイルが1件もありませんでした")
    return pd.concat(dfs, ignore_index=True)


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """列名の揺れを吸収して標準形に変換する（独自CSV用）"""
    df = df.copy()
    df.columns = df.columns.str.strip()

    if "合計_許可病床数" not in df.columns:
        df["合計_許可病床数"] = sum(
            df.get(f"{t}_許可病床数", pd.Series(0, index=df.index)) for t in BED_TYPES
        )
    if "合計_稼働病床数" not in df.columns:
        df["合計_稼働病床数"] = sum(
            df.get(f"{t}_稼働病床数", pd.Series(0, index=df.index)) for t in BED_TYPES
        )

    num_cols = [c for c in df.columns if any(k in c for k in ["病床数", "件数", "医師数", "看護師数"])]
    for c in num_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)

    if "報告年度" in df.columns:
        df["報告年度"] = pd.to_numeric(df["報告年度"], errors="coerce").astype("Int64")

    return df


# ── 集計ヘルパー ──────────────────────────────────────────────


def occupancy_rate(df: pd.DataFrame, bed_type: str = "合計") -> pd.Series:
    """
    病床稼働率を計算する。
    ・在棟患者延べ数がある場合: 在棟延べ数 / 365 / 許可病床数  ← 正しい計算式
    ・ない場合（サンプルデータ等）: 稼働病床数 / 許可病床数（後方互換）
    """
    kyoka_col  = f"{bed_type}_許可病床数"
    zaitou_col = f"{bed_type}_在棟延べ数"
    kado_col   = f"{bed_type}_稼働病床数"

    kyoka = pd.to_numeric(df[kyoka_col], errors="coerce").replace(0, np.nan)

    if zaitou_col in df.columns:
        zaitou = pd.to_numeric(df[zaitou_col], errors="coerce").fillna(0)
        # 在棟延べ数が全て0ならフォールバック（データ欠損の可能性）
        if zaitou.sum() > 0:
            return (zaitou / 365 / kyoka).fillna(0)

    if kado_col in df.columns:
        kado = pd.to_numeric(df[kado_col], errors="coerce").fillna(0)
        return (kado / kyoka).fillna(0).clip(0, 1)

    return pd.Series(0.0, index=df.index)


def add_derived_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["合計稼働率"] = occupancy_rate(df)
    for t in BED_TYPES:
        if f"{t}_許可病床数" in df.columns and f"{t}_稼働病床数" in df.columns:
            df[f"{t}_稼働率"] = occupancy_rate(df, t)
    if "常勤医師数" in df.columns and "合計_許可病床数" in df.columns:
        df["医師数_per100床"] = (
            pd.to_numeric(df["常勤医師数"], errors="coerce") /
            pd.to_numeric(df["合計_許可病床数"], errors="coerce").replace(0, np.nan) * 100
        ).round(1)
    if "常勤看護師数" in df.columns and "合計_許可病床数" in df.columns:
        df["看護師数_per100床"] = (
            pd.to_numeric(df["常勤看護師数"], errors="coerce") /
            pd.to_numeric(df["合計_許可病床数"], errors="coerce").replace(0, np.nan) * 100
        ).round(1)
    return df


def region_share(df: pd.DataFrame, year: int, pref: str, region: str) -> pd.DataFrame:
    sub = df[
        (df["報告年度"] == year) &
        (df["都道府県名"] == pref) &
        (df["二次医療圏名"] == region)
    ].copy()
    sub = add_derived_columns(sub)
    total = pd.to_numeric(sub["合計_許可病床数"], errors="coerce").sum()
    sub["地域シェア(%)"] = (
        pd.to_numeric(sub["合計_許可病床数"], errors="coerce") / total * 100
    ).round(1)
    sub["地域内順位"] = (
        pd.to_numeric(sub["合計_許可病床数"], errors="coerce")
        .rank(ascending=False, method="min").astype(int)
    )
    return sub.sort_values("地域内順位")


def hospital_trend(df: pd.DataFrame, hospital_code: str) -> pd.DataFrame:
    sub = df[df["医療機関コード"].astype(str) == str(hospital_code)].copy()
    sub = add_derived_columns(sub)
    return sub.sort_values("報告年度")


def bed_composition(row: pd.Series) -> pd.Series:
    vals = {t: float(row.get(f"{t}_許可病床数", 0)) for t in BED_TYPES}
    total = sum(vals.values()) or 1
    return pd.Series({t: round(v / total * 100, 1) for t, v in vals.items()})


# ── DuckDB サポート ──────────────────────────────────────────────


def _get_duckdb():
    """duckdb モジュールを返す（未インストールなら ImportError）"""
    try:
        import duckdb
        return duckdb
    except ImportError:
        raise ImportError(
            "DuckDB が必要です。インストールしてください: pip install duckdb"
        )


def load_hospitals_from_db(db_path: str) -> pd.DataFrame:
    """DuckDB から病院レベルデータを読み込む"""
    duckdb = _get_duckdb()
    con = duckdb.connect(db_path, read_only=True)
    try:
        df = con.execute("SELECT * FROM hospitals").df()
    finally:
        con.close()
    return df


def load_wards_from_db(db_path: str) -> pd.DataFrame:
    """DuckDB から病棟レベルデータを読み込む"""
    duckdb = _get_duckdb()
    con = duckdb.connect(db_path, read_only=True)
    try:
        # wards テーブルが存在しない場合は空を返す
        tables = con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        ).fetchdf()["table_name"].tolist()
        if "wards" not in tables:
            return pd.DataFrame()
        df = con.execute("SELECT * FROM wards").df()
    finally:
        con.close()
    return df


def load_surgery_from_db(db_path: str) -> pd.DataFrame:
    """DuckDB から手術データを読み込む"""
    duckdb = _get_duckdb()
    con = duckdb.connect(db_path, read_only=True)
    try:
        tables = con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        ).fetchdf()["table_name"].tolist()
        if "surgery" not in tables:
            return pd.DataFrame()
        df = con.execute("SELECT * FROM surgery").df()
        # dummy列だけのテーブル（手術データなし）の場合は空を返す
        if list(df.columns) == ["dummy"]:
            return pd.DataFrame()
    finally:
        con.close()
    return df


def get_db_meta(db_path: str) -> dict:
    """DuckDB のメタ情報（更新日・年度・件数）を返す"""
    duckdb = _get_duckdb()
    con = duckdb.connect(db_path, read_only=True)
    try:
        tables = con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        ).fetchdf()["table_name"].tolist()
        if "meta" not in tables:
            return {"updated_at": "不明", "years": "不明", "hospital_cnt": 0, "ward_cnt": 0}
        row = con.execute("SELECT * FROM meta LIMIT 1").fetchdf().iloc[0]
        return {
            "updated_at":   str(row["updated_at"]),
            "years":        str(row["years"]),
            "hospital_cnt": int(row["hospital_cnt"]),
            "ward_cnt":     int(row["ward_cnt"]),
        }
    except Exception:
        return {"updated_at": "不明", "years": "不明", "hospital_cnt": 0, "ward_cnt": 0}
    finally:
        con.close()
