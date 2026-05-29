"""
病床機能報告サンプルデータ生成モジュール
実データと同じ列構成でランダムなデモデータを生成する
"""
import pandas as pd
import numpy as np

REGIONS = {
    "東京都": ["区中央部", "区南部", "区西南部", "区西部", "区西北部", "区東北部", "区東部", "南多摩", "西多摩", "島しょ"],
    "大阪府": ["大阪市", "豊能", "三島", "北河内", "中河内", "南河内", "堺市", "泉州"],
    "愛知県": ["名古屋", "海部", "尾張中部", "尾張東部", "尾張西部", "尾張北部", "知多半島", "西三河北部", "西三河南部東", "西三河南部西", "東三河北部", "東三河南部"],
    "神奈川県": ["横浜市", "川崎市", "相模原", "横須賀・三浦", "県央", "湘南東部", "湘南西部", "県西"],
    "埼玉県": ["さいたま", "南部", "南西部", "東部", "西部", "北部", "利根"],
    "千葉県": ["千葉", "東葛南部", "東葛北部", "印旛", "香取海匝", "山武長生夷隅", "安房", "君津", "市原"],
}

HOSPITAL_TYPES = ["国立", "都道府県立", "市町村立", "日赤", "済生会", "厚生連", "社会保険", "民間"]

PREFIXES = ["中央", "総合", "済生会", "赤十字", "厚生", "協立", "共済", "記念", "市立", "県立"]
SUFFIXES = ["病院", "医療センター", "総合病院", "記念病院"]


def _hospital_name(rng, pref, i):
    prefix = rng.choice(PREFIXES)
    suffix = rng.choice(SUFFIXES)
    return f"{prefix}{pref[:2]}{suffix}{'ABCDE'[i % 5] if i >= 20 else ''}"


def generate_sample_data(years=None, seed=42):
    """複数年の病床機能報告サンプルデータを生成して返す"""
    if years is None:
        years = [2020, 2021, 2022, 2023]

    rng = np.random.default_rng(seed)
    rows = []

    for pref, regions in REGIONS.items():
        for region in regions:
            n_hospitals = rng.integers(8, 25)
            # 病院ごとに固定特性を生成
            hospital_names = [_hospital_name(rng, pref, i) for i in range(n_hospitals)]
            base_scales = rng.integers(50, 600, size=n_hospitals)  # 総病床規模
            profiles = np.array([
                rng.dirichlet(rng.uniform(0.5, 3, size=4))
                for _ in range(n_hospitals)
            ])

            for year in years:
                for i in range(n_hospitals):
                    scale = base_scales[i] + rng.integers(-20, 20)
                    profile = profiles[i]
                    noise = rng.uniform(0.95, 1.05, size=4)

                    kyoka = (scale * profile * noise).astype(int)
                    # 最低1床以上
                    kyoka = np.maximum(kyoka, [0, 0, 0, 0])
                    # 稼働率は種別によって異なる傾向
                    occ_means = [0.85, 0.80, 0.88, 0.92]
                    kado = np.array([
                        int(kyoka[k] * rng.uniform(occ_means[k] - 0.1, occ_means[k] + 0.05))
                        for k in range(4)
                    ])
                    kado = np.minimum(kado, kyoka)

                    total_kyoka = kyoka.sum()
                    total_kado = kado.sum()

                    # 在棟患者延べ数（稼働率の正確な計算: 在棟延べ数/365/許可病床数）
                    # 種別ごとの平均稼働率目安: 高度急性期80%, 急性期75%, 回復期83%, 慢性期87%
                    occ_means_z = [0.80, 0.75, 0.83, 0.87]
                    zaitou = np.array([
                        int(kyoka[k] * rng.uniform(
                            max(0.0, occ_means_z[k] - 0.08),
                            occ_means_z[k] + 0.06
                        ) * 365)
                        for k in range(4)
                    ])
                    total_zaitou = int(zaitou.sum())

                    med_per_100 = rng.uniform(5, 25)
                    ns_per_100 = rng.uniform(30, 80)
                    doctors = max(1, int(total_kyoka * med_per_100 / 100))
                    nurses = max(1, int(total_kyoka * ns_per_100 / 100))

                    emergency = rng.integers(0, 3000) if total_kyoka > 100 else 0
                    surgery = rng.integers(0, 5000) if total_kyoka > 80 else 0

                    # 医療設備（規模に応じたサンプル台数）
                    # CT内訳（マルチスライス列数別）
                    ct_n  = int(rng.integers(1, 4)) if total_kyoka > 100 else int(rng.integers(0, 2))
                    if ct_n > 0:
                        ct_64    = int(rng.integers(0, ct_n + 1))
                        ct_16_64 = int(rng.integers(0, max(1, ct_n - ct_64 + 1)))
                        ct_16    = max(0, ct_n - ct_64 - ct_16_64)
                    else:
                        ct_64 = ct_16_64 = ct_16 = 0
                    ct_other = 0

                    # MRI内訳（テスラ別）
                    mri_n = int(rng.integers(0, 3)) if total_kyoka > 150 else int(rng.integers(0, 2))
                    if mri_n > 0:
                        mri_3t    = int(rng.integers(0, mri_n + 1))
                        mri_15_3t = int(rng.integers(0, max(1, mri_n - mri_3t + 1)))
                        mri_15    = max(0, mri_n - mri_3t - mri_15_3t)
                    else:
                        mri_3t = mri_15_3t = mri_15 = 0

                    pet_n    = int(rng.integers(0, 2)) if total_kyoka > 300 else 0
                    petct_n  = int(rng.integers(0, 2)) if total_kyoka > 200 else 0
                    petmri_n = 1 if total_kyoka > 400 and rng.random() > 0.8 else 0
                    rt_n     = int(rng.integers(0, 2)) if total_kyoka > 200 else 0
                    robot_n  = 1 if total_kyoka > 250 and rng.random() > 0.7 else 0

                    rows.append({
                        "報告年度": year,
                        "都道府県名": pref,
                        "二次医療圏名": region,
                        "医療機関コード": f"{pref[:2]}{region[:2]}{i:04d}",
                        "医療機関名": hospital_names[i],
                        "開設者区分名": rng.choice(HOSPITAL_TYPES),
                        "高度急性期_許可病床数": int(kyoka[0]),
                        "高度急性期_稼働病床数": int(kado[0]),
                        "高度急性期_在棟延べ数": int(zaitou[0]),
                        "急性期_許可病床数": int(kyoka[1]),
                        "急性期_稼働病床数": int(kado[1]),
                        "急性期_在棟延べ数": int(zaitou[1]),
                        "回復期_許可病床数": int(kyoka[2]),
                        "回復期_稼働病床数": int(kado[2]),
                        "回復期_在棟延べ数": int(zaitou[2]),
                        "慢性期_許可病床数": int(kyoka[3]),
                        "慢性期_稼働病床数": int(kado[3]),
                        "慢性期_在棟延べ数": int(zaitou[3]),
                        "合計_許可病床数": int(total_kyoka),
                        "合計_稼働病床数": int(total_kado),
                        "合計_在棟延べ数": total_zaitou,
                        "常勤医師数": doctors,
                        "常勤看護師数": nurses,
                        "救急搬送件数": int(emergency),
                        "手術件数": int(surgery),
                        "CT台数": ct_n,
                        "CT_64列以上": ct_64,
                        "CT_16〜64列": ct_16_64,
                        "CT_16列未満": ct_16,
                        "CT_その他": ct_other,
                        "MRI台数": mri_n,
                        "MRI_3T以上": mri_3t,
                        "MRI_1.5〜3T": mri_15_3t,
                        "MRI_1.5T未満": mri_15,
                        "PET台数": pet_n,
                        "PETCT台数": petct_n,
                        "PETMRI台数": petmri_n,
                        "内視鏡手術支援機器台数": robot_n,
                        "IMRT台数": rt_n,
                    })

    return pd.DataFrame(rows)


BED_TYPES = ["高度急性期", "急性期", "回復期", "慢性期"]
BED_COLORS = {
    "高度急性期": "#e74c3c",
    "急性期": "#e67e22",
    "回復期": "#2ecc71",
    "慢性期": "#3498db",
}
