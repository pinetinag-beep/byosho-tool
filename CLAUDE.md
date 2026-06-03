# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## プロジェクトの目的

医療に関するデータをビジュアライズ・比較し、**医療提供体制の可視化**を実現する。

- 一般の人が地域の医療の実情を直感的に理解できるようにする
- 過剰医療提供・過疎医療提供などの問題を広く知らしめる

対象データは厚生労働省「病床機能報告」を中心に、今後他の医療データとの連携も視野に入れる。  
ユーザーは医療の専門家だけでなく、一般市民も想定する。

## ビジネス方針

**将来的な有料化を前提に開発する。**

優先ターゲット：
- **病院経営者** — 自院の競合比較・地域内ポジション把握
- **医療系コンサルタント** — 複数地域の一括分析・レポート出力
- 次点: 自治体・行政（地域医療構想策定）、医療機器/製薬メーカー営業（設備・手術件数調査）

有料化に向けた主要課題：
- 認証・ユーザー管理機能の実装
- レポート出力（PDF/Excel）
- Streamlit Community Cloudから本番インフラへの移行
- 厚労省データの商用利用可否の確認
- 特定商取引法表示・プライバシーポリシーの整備

**開発上の制約**  
率・指標・スコアは必ず公的機関（厚労省・総務省等）の定義に基づいて実装すること。  
独自解釈による指標の作成・表示は禁止。定義の出典を必ず把握した上で実装する。

---

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

## ユーザーへの作業依頼ルール

ユーザーのWindows環境での作業が必要な場合は、必ず以下の形式でステップ・バイ・ステップで指示する。まとめて1行で言わない。

**ユーザー環境の注意点：**
- Pythonの実行は `py` コマンドを使う（`python` はMicrosoft Store stubで動かない）
- 作業フォルダ: `C:\Users\inter\OneDrive\Desktop\byosho_tool`

**指示の例（住所データを追加する場合）：**

1. コマンドプロンプトを開く
2. 以下を実行してフォルダに移動する：
   ```
   cd C:\Users\inter\OneDrive\Desktop\byosho_tool
   ```
3. 以下を実行して住所データを取り込む：
   ```
   py build_master.py
   ```
4. 完了したら、以下を実行してparquetをエクスポートする：
   ```
   py -c "import duckdb, pandas as pd; con=duckdb.connect('data/byosho.duckdb'); con.execute('SELECT * FROM hospitals').fetchdf().to_parquet('data_cache_new.parquet', index=False); con.close(); print('done')"
   ```
5. `data_cache_new.parquet` をこのチャットに添付する
6. Claudeがコミット・プッシュする

---

## データ更新の全般的な流れ

DuckDBを更新してStreamlit Cloudに反映する一般手順：

**ステップ1（ユーザー：Windowsで実行）**
1. コマンドプロンプトを開く
2. `cd C:\Users\inter\OneDrive\Desktop\byosho_tool` で移動
3. 必要なスクリプトを `py <スクリプト名>` で実行

**ステップ2（ユーザー：parquetをエクスポート）**
4. 以下を実行して parquet を生成：
   ```
   py -c "import duckdb, pandas as pd; con=duckdb.connect('data/byosho.duckdb'); con.execute('SELECT * FROM hospitals').fetchdf().to_parquet('data_cache.parquet', index=False); con.execute('SELECT * FROM wards').fetchdf().to_parquet('ward_cache.parquet', index=False); con.execute('SELECT * FROM surgery').fetchdf().to_parquet('surgery_cache.parquet', index=False); con.close(); print('done')"
   ```
5. 生成された parquet ファイルをこのチャットに添付する

**ステップ3（Claude：コミット・プッシュ）**
6. Claude がリポジトリに配置してコミット・プッシュする

---

## 手術データの更新手順

手術データ（様式2）を更新する場合：

1. アプリの管理者パネルで様式2ファイルをアップロード → 「手術データを取り込む」
2. 成功後に表示されるダウンロードボタンで `surgery_cache.parquet` を取得
3. このチャットに `surgery_cache.parquet` を添付する
4. Claude がリポジトリにコミットして Streamlit Cloud に反映する

**2021年は7地域ファイル**（000953885〜000953892.xlsx、891は欠番）を全て選択してアップロードすること。

## 未実装機能

- **移動時間フィルター**: 住所入力 → OSRM Table API → N分以内の病院を絞り込み（SPEC.md 参照）
- **人口10万人あたりの指標**: 地域ごとの医師数・病床数を人口10万人あたりで表示。人口データは総務省「住民基本台帳に基づく人口」または国勢調査を使用すること（独自推計禁止）
- **DPCデータ取り込み**: 厚労省公表の DPC 導入の影響評価に係る調査データの取り込み・可視化
