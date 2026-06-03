# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## アプリ起動

```bash
streamlit run app.py
```

DuckDB を新規構築する場合：
```bash
python build_db.py              # 2021〜2023年 全年度
python build_db.py --years 2023 # 特定年度のみ
python build_master.py          # 医療情報ネット座標データ取り込み（初回のみ）
```

## Git・デプロイ

**Streamlit Cloud（`byosho-tool-testver.streamlit.app`）は `master` ブランチを参照している。**  
`git push` は必ず master と main の両方に行う：

```bash
git push origin HEAD:master && git push origin HEAD:main
```

## データ管理

- **ローカル**: `data/byosho.duckdb`（`.gitignore` 対象）
- **Streamlit Cloud**: `data_cache.parquet` / `ward_cache.parquet` / `surgery_cache.parquet`（リポジトリに含める）
- DuckDB を更新した場合は parquet も再生成してコミットする：
  ```python
  import duckdb, pandas as pd
  con = duckdb.connect('data/byosho.duckdb')
  con.execute('SELECT * FROM hospitals').fetchdf().to_parquet('data_cache.parquet', index=False)
  con.execute('SELECT * FROM wards').fetchdf().to_parquet('ward_cache.parquet', index=False)
  con.execute('SELECT * FROM surgery').fetchdf().to_parquet('surgery_cache.parquet', index=False)
  con.close()
  ```
- MHLW サーバー（`www.mhlw.go.jp`）は本番サーバーから 403 になる。ダウンロードはユーザーのブラウザから行う。

## アーキテクチャ

```
app.py          ← Streamlit UI（全タブ・サイドバー）
  ├── data_processor.py  ← データ読み込み・集計ロジック
  ├── charts.py          ← Plotly グラフ定義
  ├── geocoder.py        ← 座標取得（locations → geocache → Nominatim の優先順）
  └── sample_data.py     ← デモ用サンプルデータ生成
```

起動時のデータ読み込み順：
1. `data/byosho.duckdb` が存在すれば DuckDB から読む（`st.cache_data`）
2. なければ `data_cache.parquet` 等から読む（Streamlit Cloud 用）
3. どちらもなければ「データが未準備です」→ サンプル生成 or Excel アップロード

## data_processor.py 主要関数

| 関数 | 説明 |
|---|---|
| `load_mhlw_byosho_extended(bytes, year)` | 様式1 Excel → (hospitals_df, wards_df) |
| `load_multiple_mhlw_extended(list, year)` | 複数地域ファイルを結合（byosho は7〜8地域ファイル） |
| `load_mhlw_yoshiki2(bytes, year)` | 様式2 Excel → 手術集計 df |
| `load_mhlw_shisetsu(bytes)` → `merge_shisetsu()` | 設備票を hospitals に結合 |
| `add_derived_columns(df)` | 稼働率・医師/看護師 per100床・CT台数合計などを付加 |
| `load_hospitals_from_db / load_wards_from_db / load_surgery_from_db` | DuckDB → DataFrame |

## 様式2（手術データ）の注意点

- **2021年は7地域ファイル**（000953885〜000953892.xlsx、891 は欠番）。2022/2023年は全国1ファイル。
- 2021年の Excel は5段組ヘッダー（rows 0〜4）。`_detect_yoshiki2_is_multilevel()` で自動判定。
- 2021年の「二次医療圏名」列は `構想区域名` という列名で入っている場合がある（`load_mhlw_yoshiki2` 内で吸収済み）。

## DB スキーマ（概要）

- `hospitals`: 病院単位・年度別（20,830行 / 2021〜2023）
- `wards`: 病棟単位（81,687行）
- `surgery`: 手術実績、病院単位・年度別（12,320行 / 2021: 5,419・2022: 3,407・2023: 3,494）
- `geocache`: Nominatim 取得済み座標キャッシュ
- `locations`: 厚労省公式座標（`build_master.py` 実行後）

## charts.py 主要関数

`ranking_table_fig(df, highlight, rank_col, show_cols, col_labels)` — ランキングタブで使用。`rank_col` で並び替え基準（許可病床数・稼働率・医師数など7種）を切り替える。

## app.py 構造（行番号目安）

| 範囲 | 内容 |
|---|---|
| 1〜370 | import・定数・キャッシュ関数 |
| 371〜465 | DuckDB / parquet からのデータ読み込み・セッションステート初期化 |
| 466〜700 | サイドバー（データ未準備時のアップロード UI・病院選択アコーディオン） |
| 703〜780 | 管理者セクション（キャッシュクリア・様式2アップロード・ダウンロード） |
| 781〜 | メインエリア（tab1〜tab7） |

## セッションステート 主要キー

| キー | 内容 |
|---|---|
| `df` | 病院データ DataFrame（全年度） |
| `ward_df` | 病棟データ DataFrame |
| `surgery_df` | 手術データ DataFrame |
| `_datasrc` | `"db"` / `"parquet"` / `"sample"` / `"none"` |
| `_sb_open` | サイドバーで開いているアコーディオンセクション（`"①"` 等） |
| `_view_mode` | `"detail"`（病院詳細）/ `"search"`（条件検索） |
| `_yoshiki2_parquet` | 管理者パネルで様式2インポート後の parquet バイト列（ダウンロード用） |

## 手術データの更新手順

手術データ（様式2）を更新する場合（年度追加・データ差し替え等）：

1. アプリの管理者パネルで様式2ファイルをアップロード → 「手術データを取り込む」
2. 成功後に表示されるダウンロードボタンで `surgery_cache.parquet` を取得
3. Claude Code セッションに貼り付け → 以下を実行してコミット：
  ```bash
  cp <ダウンロードした parquet> surgery_cache.parquet
  git add surgery_cache.parquet
  git commit -m "手術データを更新"
  git push origin HEAD:master && git push origin HEAD:main
  ```

**2021年は7地域ファイル**（000953885〜000953892.xlsx、891は欠番）を全て選択してアップロードすること。

## 未実装機能（SPEC.md 参照）

- 移動時間フィルター（住所入力 → OSRM Table API → N分以内の病院を絞り込み）
