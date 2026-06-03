# 病床機能報告 分析ツール — 仕様定義書

最終更新: 2026-05-31

---

## 1. プロジェクト概要

厚生労働省が毎年公表する「病床機能報告」データをもとに、病院ごとの病床構成・稼働率・地域比較・経年トレンドを可視化する Streamlit 製 Web アプリ。  
対応データ: 2021〜2023年度（DuckDB に格納済み）

**デプロイ先**: Streamlit Community Cloud（`estver.streamlit.app`）  
**リポジトリ**: `pinetinag-beep/byosho-tool`  
**本番ブランチ**: `master`（`main` は削除済み）

---

## 2. ファイル構成

```
byosho-tool/
├── app.py              # Streamlit メインアプリ
├── charts.py           # Plotly グラフ定義
├── data_processor.py   # データ加工・集計ロジック
├── sample_data.py      # デモ用サンプルデータ生成
├── geocoder.py         # 病院緯度経度の取得・キャッシュ管理
├── build_db.py         # 病床機能報告データ → DuckDB 構築スクリプト
├── build_master.py     # 医療情報ネット座標データ → DuckDB 格納スクリプト
├── requirements.txt    # Python 依存ライブラリ
├── .streamlit/
│   └── config.toml     # Streamlit サーバー設定・テーマ
├── .devcontainer/
│   └── devcontainer.json
└── data/
    └── byosho.duckdb   # メインデータベース（ローカル）
```

---

## 3. 技術スタック

| 用途 | ライブラリ・サービス |
|---|---|
| Web UI | Streamlit >= 1.35 |
| グラフ | Plotly >= 5.20 |
| DB | DuckDB >= 0.10 |
| 地図 | Folium 0.20 + streamlit-folium 0.22 |
| ジオコーディング | Geopy 2.4（Nominatim / OpenStreetMap） |
| 車ルーティング（予定） | OSRM 公開サーバー（無料・APIキー不要） |
| データ読み込み | Pandas, PyArrow, openpyxl |

---

## 4. データベース設計（byosho.duckdb）

### 4-1. hospitals（病床機能報告 病院単位）

| 列名 | 型 | 説明 |
|---|---|---|
| 医療機関コード | VARCHAR | 主キー相当（10桁） |
| 医療機関名 | VARCHAR | |
| 都道府県名 | VARCHAR | |
| 二次医療圏名 | VARCHAR | |
| 高度急性期_許可病床数 | BIGINT | |
| 高度急性期_稼働病床数 | BIGINT | |
| 高度急性期_在棟延べ数 | BIGINT | |
| 急性期_許可病床数〜在棟延べ数 | BIGINT | （同上パターン） |
| 回復期_許可病床数〜在棟延べ数 | BIGINT | |
| 慢性期_許可病床数〜在棟延べ数 | BIGINT | |
| 合計_許可病床数〜在棟延べ数 | BIGINT | |
| 報告年度 | BIGINT | |
| 常勤医師数 | BIGINT | |
| 常勤看護師数 | BIGINT | |
| 救急搬送件数 | BIGINT | |
| CT_64列以上〜その他 | BIGINT | CT 台数内訳 |
| MRI_3T以上〜1.5T未満 | BIGINT | MRI 台数内訳 |
| PETCT台数、PETMRI台数、PET台数 | BIGINT | |
| IMRT台数、ガンマナイフ台数、サイバーナイフ台数 | BIGINT | |
| 内視鏡手術支援機器台数 | BIGINT | ロボット手術 |
| 血管連続撮影装置台数、SPECT台数 | BIGINT | |
| CT台数、MRI台数 | BIGINT | 合計台数 |
| マンモグラフィ台数 | DOUBLE | |

**行数**: 20,830 行（2021〜2023年度合計）

### 4-2. wards（病棟単位データ）

| 列名 | 型 |
|---|---|
| 医療機関コード、医療機関名 | BIGINT / VARCHAR |
| 都道府県名、二次医療圏名 | VARCHAR |
| 機能区分 | VARCHAR |
| 入院基本料 | VARCHAR |
| 届出病床数、許可病床数、最大使用病床数 | BIGINT |
| 新規入棟患者数、救急入院患者数 | BIGINT / DOUBLE |
| 退棟患者数、在棟延べ数 | BIGINT |
| 家庭退院数、他院転院数、施設入所数、死亡退院数 | BIGINT / DOUBLE |
| 報告年度 | BIGINT |

**行数**: 81,687 行

### 4-3. surgery（手術実績）

主な列: 医療機関コード、手術総数、全身麻酔手術数、腹腔鏡下・胸腔鏡下・ロボット支援・悪性腫瘍・脳血管内・人工心肺手術数、臓器別手術数（12部位 × 全数／全身麻酔）、報告年度

**行数**: 7,495 行

### 4-4. meta（DBメタ情報）

`updated_at`, `years`, `hospital_cnt`, `ward_cnt`

### 4-5. geocache（Nominatim ジオコーディングキャッシュ）

`hospital_name`, `pref`, `lat`, `lon`, `found`  
→ `geocoder.py` が自動生成・追記。地図タブの「ジオコーディング実行」で蓄積。

### 4-6. locations（公式座標データ）★ build_master.py 実行後に生成

`施設名`, `医療機関コード`, `lat`, `lon`, `都道府県名`, `住所`, `data_source`, `data_date`  
→ 厚労省 医療情報ネット オープンデータ（e-gov）から取得。geocache より優先参照。

---

## 5. 画面設計

### 5-1. サイドバー

- DB ステータス表示（年度・病院数）
- データ未準備時: サンプルデータ生成 / Excel アップロード
- **病院を選択**: 年度 → 都道府県 → 二次医療圏 → 医療機関名
- **病院検索**（キーワード・設備・手術条件で絞り込み）← 旧タブ7から移動済み
- 管理者セクション（キャッシュクリア、build_db.py 実行案内）

### 5-2. タブ構成（全7タブ）

| # | タブ名 | 内容 |
|---|---|---|
| 1 | 📊 病院概要 | 病床種別ドーナツ・稼働率ゲージ・設備モダリティ |
| 2 | 🗺️ 地図 | 都道府県／二次医療圏の病院をマップ表示（folium） |
| 3 | 🏆 地域比較 | 二次医療圏内の病床数比較・シェア・散布図 |
| 4 | 📋 ランキング | 地域内病院ランキング・稼働率順位 |
| 5 | 📈 経年トレンド | 病床数・稼働率・スタッフ数の年次推移 |
| 6 | 👨‍⚕️ スタッフ分析 | 医師・看護師数の地域比較（100床あたり） |
| 7 | 📋 詳細分析 | 病棟別入院基本料・入退院経路・手術実績 |

> **モバイル対応**: 🗺️ 地図を2番目に配置（iOS で横スクロール不要）

---

## 6. 地図機能（geocoder.py）

### 座標取得の優先順位

```
1. locations テーブル（厚労省公式・高精度）
       ↓ なければ
2. geocache テーブル（Nominatim で取得済みのキャッシュ）
       ↓ なければ
3. Nominatim API でリアルタイム取得（1件/秒 レート制限）→ geocache に保存
```

### 主要関数

| 関数 | 役割 |
|---|---|
| `load_cached_coords(db_path, pref)` | 都道府県の座標を一括取得（上記優先順位） |
| `count_uncached(db_path, names, pref)` | 未取得病院数を返す |
| `has_official_locations(db_path)` | locations テーブルにデータがあるか |
| `geocode_batch(df, db_path, progress_cb)` | 未取得病院を Nominatim でジオコーディング |

### 地図表示仕様

- マーカー色: 🔴500床以上 / 🟠300〜499床 / 🟢100〜299床 / 🔵100床未満
- マーカーサイズ: 許可病床数に比例（5〜22px）
- 選択中の病院: 赤枠でハイライト
- ポップアップ: 病院名・二次医療圏・許可病床数・稼働率

---

## 7. 公式座標データ取り込み（build_master.py）

**データソース**: 厚労省 医療情報ネット オープンデータ（e-Gov データポータル）  
**形式**: ZIP → UTF-8-BOM CSV  
**主要列**: 施設名、医療機関コード、緯度、経度、都道府県名、住所

```bash
# ローカルで実行（初回のみ）
python build_master.py

# ローカルファイルを指定する場合
python build_master.py --file e-gov20250601.zip
```

処理後、`locations` テーブルに格納。以降は地図タブのジオコーディング待ち時間がゼロになる。

---

## 8. 今後の実装予定

### 所要時間フィルター（設計済み・未実装）

病院検索サイドバーに追加する絞り込み条件。

**UI**
```
📍 出発地
  住所・ランドマーク入力: [___________]
⏱️ 移動手段: ○ 車  ○ 公共交通（近似）
   上限:     [30] 分以内
```

**処理フロー**
1. 住所 → Nominatim でジオコーディング
2. `locations` テーブルから病院座標を一括取得
3. 直線距離で粗くフィルタ（API 呼び出し削減）
4. **車**: OSRM Table API（無料・APIキー不要）で一括所要時間取得
5. **公共交通**: 直線距離 ÷ 平均速度 25km/h で近似（免責表示あり）
6. 他の検索条件と AND で結果を絞り込み

**前提条件**: `locations` テーブルに公式座標データが格納されていること（`build_master.py` 実行済み）

---

## 9. ブランチ・デプロイ管理

| ブランチ | 状態 | 説明 |
|---|---|---|
| `master` | **本番** | Streamlit Cloud がここを参照。唯一の永続ブランチ |
| `claude/*` | 作業用 | Claude Code セッションが自動生成。完了後は `master` にマージ |

**運用ルール**: 開発は Claude Code の作業ブランチで行い、動作確認後に `master` へマージ。

---

## 10. ローカル開発セットアップ

```bash
git clone https://github.com/pinetinag-beep/byosho-tool.git
cd byosho-tool
pip install -r requirements.txt

# データ構築（初回）
python build_db.py          # 病床機能報告データの取り込み
python build_master.py      # 医療情報ネット座標データの取り込み

# アプリ起動
streamlit run app.py
```
