# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## アプリ名候補

現在のリポジトリ名 `byosho-tool` は仮称。病床機能報告・DPC・届出情報・人口データなど複数の公的医療データを統合する方向性を踏まえたネーミング検討中。

方針：**親しみやすさ重視**、一般市民にも病院経営者にも届く言葉感

| 候補 | 読み | コンセプト |
|---|---|---|
| まちみる | まちみる | 「地域を見る」を一語に凝縮。ひらがなで親しみやすい |
| まちてらす | まちてらす | まち＋テラス（照らす・開放的な場所）。地域医療を照らし出すイメージ |
| まちのカルテ | まちのカルテ | 地域の医療診断書。カルテはデータが増えるほど充実するという文脈も自然 |
| ほすぴな | ほすぴな | Hospi＋な。柔らかい語感で覚えやすい |
| いりょうのわ | いりょうのわ | 医療の輪。つながり・広がりのイメージ。ロゴに◯が使いやすい |

---

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
`main` ブランチは削除済み。`master` が唯一の本番ブランチ。

変更をコミットしたら必ず `master` と作業ブランチの両方にプッシュする：

```bash
git push origin HEAD:master && git push origin HEAD:<作業ブランチ名>
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
  ├── sample_data.py     ← デモ用サンプルデータ生成
  ├── build_dpc.py       ← DPC調査データの取り込み（dpc_*.parquet を生成）
  └── build_shisetsu_kijun.py ← 施設基準届出Excelの取り込み（下記セクション参照）

shisetsu_search.py        ← 施設基準届出「検索専用」の独立アプリ（本体と別デプロイ可能）
parse_chubu_shisetsu_pdf.py ← 施設基準届出「届出受理医療機関名簿」PDF形式の専用パーサー
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

## 施設基準届出データ（診療報酬）

各地方厚生局が公表する「施設基準届出受理状況」を全国分収集し、`shisetsu_kijun_cache.parquet` として保持している。病院詳細ページの「施設基準届出」セクションと、条件検索の「施設基準届出」タブ、および独立アプリ `shisetsu_search.py` で使用する。

### データの二重構造（重要）

地方厚生局が公表するファイルには**2つの全く異なる系統**がある。どちらも「届出受理医療機関一覧表／名簿」という似た名前で紛らわしいので注意。

| 系統 | 内容 | 対応スクリプト |
|---|---|---|
| **全件スナップショット**（Excel/PDF） | ある時点の届出状況を病院・届出単位で全件記載 | `build_shisetsu_kijun.py`（Excel） / `parse_chubu_shisetsu_pdf.py`（PDF） |
| **新規・変更届出のみ**（PDF diff） | 直近数週間の新規／変更分のみを記載する差分レポート | `parse_shinki_pdf.py`（scratchpad、本採用せず） |

**必ず「全件スナップショット」形式のファイルを使うこと。** 差分レポートは一部の医療機関しか載っておらず、全体を代替できない。

### Excelの内部構造（`build_shisetsu_kijun.py`）

全件スナップショットのExcelは1つの届出が**複数行**に展開される構造になっている：

```
行1: 医療機関番号・医療機関名称・受理届出名称・受理記号・受理番号・算定開始年月日（本体行、備考列は空）
行2以降: 同じ届出について「備考（見出し）」「備考（データ）」列に
         病棟種別・病床区分・病棟数・病床数・区分 等の内訳が1行ずつ入る
```

`_normalize_label()` で見出しの表記ゆれ（全角スペース・「届出に係る」プレフィックス等）を吸収し、既知ラベル（病棟種別／病床区分／病棟数／病床数／区分）は専用列に、それ以外は `内訳その他` にまとめる。

**重要：地方厚生局によって「備考」欄を埋めているかどうかが全く違う。** 同じ24列Excelフォーマットでも、実際にデータが入っているかは厚生局の運用次第。実測した状況（2026年時点）：

| 厚生局 | 区分（備考詳細）の有無 |
|---|---|
| 東北・九州（+沖縄）・東海北陸 | ✅ あり |
| 関東信越・近畿・北海道・中国四国 | ❌ 備考が常に空 |

四国4県（香川・徳島・愛媛・高知）は「コード内容別医療機関一覧表」という**別物のファイル**（開設者・診療科・病床数等の医療機関名簿で、届出情報自体が無い）が配布されており、このパイプラインでは使えない。**2026年7月に再度取得を試みたが、同じ「コード内容別医療機関一覧表」形式しか無く依然入手不能。** `shisetsu_kijun_cache.parquet` 内の四国4県データ（34,383行）は、正しい形式のファイルが手に入る前の古いスナップショット（2026-05時点、`受理番号`/`区分`/`病床数`が全て空）をそのまま引き継いでいるだけなので、新規に更新する際は誤って削除しないよう注意（`shisetsu_raw/` の該当ファイルが `build_shisetsu_kijun.py` でスキップされても、既存の四国データは別途保持してconcatし直す）。

### PDFパーサー（`parse_chubu_shisetsu_pdf.py`）— 2026年7月以降は基本不要

東海北陸厚生局（富山・石川・岐阜・愛知・静岡・三重）と茨城・兵庫は、当初PDF（「届出受理医療機関名簿」）でしか届出情報が手に入らなかったため専用パーサーを組んだが、**2026年7月に該当8都道府県すべてで正規のExcel（24列フォーマット）が入手できることが確認された**。以後の更新はExcelを`build_shisetsu_kijun.py`（`--dir shisetsu_raw/`）に通すだけで済み、このPDFパーサーは基本的に不要。ただし今後別の厚生局がPDFしか公表しないケースに備えて残してある。以下は当時の教訓（ただし **受理届出名称（正式名称）が無く、受理記号（略号）と算定期間のみ**が記載されるため、`build_kigou_to_meisho()` で既存の `shisetsu_kijun_cache.parquet` から「受理記号→受理届出名称」の逆引き辞書を作り、正式名称を復元していた）。

PDF解析で得た教訓（同種のPDFを今後解析する際に必ず踏まえること）：

1. **1医療機関のレコードが複数ページにまたがる**（大病院は届出数が多く数ページ分続く）。ページ単位で処理すると境界を誤検出するため、`page.extract_words()` の `doctop`（文書全体でのY座標。`top` はページ内相対値なので使えない）で全ページを結合してから処理する。
2. **医療機関番号アンカーの検出**は「項番＋医療機関番号がスペース無しで連結される」ため（例："13901,1802,6" = 項番139 + 医療機関番号01,1802,6）、行頭マッチ（`^`）ではなく末尾からの `search()` で検出する。区切り文字も厚生局によって `-` `,` `・` とバラバラ。
3. **各ページ冒頭のヘッダー行**（タイトル・列見出し・「電話番号（FAX番号）」等）を除外しないと、複数ページにまたがるレコードの途中にヘッダーテキストが混入する（`top < 96` を除外）。
4. **性能**: 医療機関数×総単語数の総当たりスキャンは病院数の多い県（愛知3,594件等）でO(n²)的に遅くなる。`doctop` でソート済みリストに対し `bisect` で区間を取得すること。
5. 「（受理記号）第N号」の確定行に、直前の病床数等の数値が結合されるケースがある（例："378（医療ＤＸ）第494号"）。正規表現は `^` アンカーを付けず `search()` で検出する。

### 区分（急性期一般入院料等）の全国カバレッジ問題と対処

施設基準届出データだけでは、**一般病棟・療養病棟・障害者施設等の区分（段階）が全国的には揃わない**（上表の通り関東信越・近畿・北海道・中国四国は空）。

**解決策**: 病床機能報告データ（`ward_cache.parquet` の `入院基本料` 列）は、病床を持つ医療機関が毎年義務的に報告する全国統一フォーマットであり、急性期一般入院料１〜７・地域一般入院料１〜３・療養病棟入院料１・２・障害者施設等入院基本料の区分を**全国100%**保持している。そのため `app.py` では、施設基準届出側の区分が空の場合に `ward_df` から自動補完している（病院詳細ページ・条件検索の両方）。

ただし **精神病棟・有床診療所・結核病棟は病床機能報告の対象外制度**のため、この3種は地方厚生局データ（東北・九州・東海北陸のみ）に頼るしかない。

### 施設基準届出データを追加・更新する際の注意

- Excel/PDFは容量が大きいため、チャット添付ではなく `shisetsu_raw/` フォルダに置いて `git push` する運用にしている（ユーザー側でWindowsのgitブランチが `main` 等になっている場合、`git push origin HEAD:claude/work-status-check-8PEo3` の形で明示的にpushする必要がある）
- 都道府県ごとにマージする際は、**列を追加する新データフレームの列に合わせて既存データフレーム側を絞り込むと、既存データにあった列（`医療機関名_正規化` 等）が消える事故が起きる**（実際に発生し本番でKeyErrorになった）。必ず「両者の列集合の和集合」で揃えてからconcatする。
- 条件検索の区分フィルターUIは、親の届出名称チェックボックスの選択と離れた場所に置くと「気づかれない」（実際に指摘された）。`st.container(border=True)` で視覚的に区別し、選択した届出名称の**直下**に表示すること。
- `shisetsu_raw/` を丸ごと入れ替えて全件再構築する場合は `python build_shisetsu_kijun.py --dir shisetsu_raw/` を使うと楽（フォルダ内の全`.xlsx`を自動処理し、24列フォーマットに合わないファイル—四国4県の「コード内容別医療機関一覧表」等—は自動でスキップされ、エラーにはならない）。ただしこの`--dir`実行は`shisetsu_kijun_cache.parquet`を**丸ごと上書き**するため、実行前に必ずバックアップを取り、処理後は「スキップ（データなし）」と表示された都道府県がないか・行数が不自然に減っていないかを旧データと比較してから本番に反映すること。
- `shisetsu_raw/届出受理医療機関名簿の受理番号欄における略称一覧.pdf`（厚労省公表の略称→正式名称対応表、2026年7月にユーザーが追加）は、全都道府県がExcel化されたことで通常の処理では不要になったが、`受理記号`列の意味を調べたい時や将来また略称のみのソースが出てきた時のための参考資料としてリポジトリに保持している。

## 未実装機能

- **移動時間フィルター**: 住所入力 → OSRM Table API → N分以内の病院を絞り込み（SPEC.md 参照）
- **人口10万人あたりの指標**: 地域ごとの医師数・病床数を人口10万人あたりで表示。人口データは総務省「住民基本台帳に基づく人口」または国勢調査を使用すること（独自推計禁止）
- **施設基準届出の全国区分カバレッジ**: 四国4県は届出データ自体が未収集（別形式のファイルが必要）。関東信越・近畿等の精神病棟・有床診療所・結核病棟の区分は病床機能報告でカバーできないため、対応する詳細版ファイルが厚生局から公表されていないか要確認

## 開発上の教訓・注意点（トラブルシューティング用）

- **`*`（全角＊含む）のマスク値**: 病床機能報告・様式2（手術）・DPCデータで年間10件以下の値は `*` として報告される。`pd.to_numeric(errors="coerce").fillna(0)` で単純変換すると `*` が `0` に化けて「実績なし」と誤表示される。`-1` をセンチネル値として保持し、表示時のみ `"*"` に変換すること（`data_processor.py` の手術データ、`build_dpc.py` のDPCデータで対応済み）。集計（`.sum()`）する際も `-1` 同士を単純加算すると意味不明な負の値になるため、専用の集計関数（正の値のみ合計、無ければ`-1`を維持）が必要。
- **Streamlitで `st.markdown()` 内の `<script>` は実行されない**（innerHTML経由のため）。カスタムJSを動かす場合は `st.components.v1.html()` を使い、親ページのDOMを触るなら `window.parent.document` 経由でアクセスする（ブラウザ自動翻訳の抑止用notranslateタグ設定で対応）。
- **ブラウザの自動翻訳が日本語ページを誤って再翻訳する**ことがある（「地域包括ケア」→「地域含むケア」等）。`<meta name="google" content="notranslate">` と `<html lang="ja">` を設定して防止する。
- **ユーザーのWindows環境でのgit push失敗**: ローカルブランチ名が作業指定ブランチ名と一致していないと `git push origin <branch>` が `src refspec ... does not match any` で失敗する。`git push origin HEAD:<branch>` を使うか、先に `git pull origin <branch>` でマージしてから push する。マージコミット時にVimが開いたら `Esc` → `:wq` で保存終了。
- **Streamlit Community Cloudのメモリ制限**（約1GB）: 大きいparquetを `@st.cache_data` 無しで読み込むとOOMでアプリが落ちる。新しいキャッシュ読み込み関数を追加する際は必ず `@st.cache_data(show_spinner=False)` を付け、可能なら `drop_duplicates()` で不要な重複行を削減する。
