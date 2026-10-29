# 施設基準届出 検索サイト（kijun_web）

全国の病院・診療所の施設基準届出を、無料で検索・CSVダウンロードできるWebサイト。
medilenz.jp のトップとして公開する想定（2026年10月〜開発）。既存のStreamlit版（病床機能報告・DPCの分析）は
そのまま残し、病院ページから「病床機能報告・DPCで詳しく見る」でリンクする。

## なぜStreamlitとは別に作ったか

- Streamlitの画面はJavaScriptで描かれるため、Googleが中身を読めず検索結果にほぼ出ない。
  このサイトはページをサーバー側で組み立てて返すので、「○○病院 施設基準」「○○県 ○○加算 届出」のような
  検索から人が来られる（医療機関ページ9万件・施設基準×都道府県ページ約3.6万件をsitemapで案内）。
- 競合の施設基準検索サービスはCSVが有料・伏せ字付きのものが多い。ここでは**CSVを無料・伏せ字なし**で出す。

## 構成

| ファイル | 役割 |
|---|---|
| `build_db.py` | リポジトリ直下の `shisetsu_kijun_cache.parquet` → `data/kijun.sqlite`（約150MB、git管理外） |
| `app.py` | FastAPI。ページはJinja2テンプレート（`templates/`）でサーバー側生成 |
| `static/style.css` | Streamlit版と同じブランドトークン（緑 #12886D、暖色グレー） |
| `tests/test_app.py` | 主要ページ・CSV・sitemapの動作確認 |

URL:
- `/` トップ（検索フォーム・よく調べられる施設基準・都道府県）
- `/search?k=施設基準&q=医療機関名or住所&pref=都道府県コード&ftype=種類` 検索結果（noindex）、`/search.csv` そのCSV
  - `k` が施設基準の正式名称と完全一致すればその1つだけ、そうでなければ名称の一部としてまとめて探す
- `/facility/{都道府県コード2桁+医療機関番号7桁}` 医療機関ページ、`.csv` でCSV
- `/kijun` 施設基準の一覧、`/kijun/{名称}` 都道府県別の届出数、`/kijun/{名称}/{都道府県名}` 医療機関一覧
- `/pref/{都道府県名}`、`/about`（出典・PDL1.0・データ時点）、`/sitemap.xml`、`/robots.txt`

## 検索フォーム（caremap.jp を参考に作り直し、2026年10月）

- 表形式（左に見出し・右に選択肢）で、都道府県 → 二次医療圏 → 市区町村（二次医療圏を1つ選ぶと出る）、
  施設の種類、医療機関名・住所、届出項目を指定する。条件は `?pref=39&area=中央&city=高知市&ftype=病院&item=33&item=170&mode=or`
  のように同じ名前を繰り返す（`app.py` の `Cond`）。結果は選んだ届出項目ごとに「○」の列、CSVも同じ形
- **届出項目** = 届出名称×受理記号（`items` テーブル）。同じ名称でも記号が違うもの（外医ＤＸ２／入医ＤＸ２＝外来／入院）は別項目。
  並び順と区分（基本診療料・特掲診療料・入院時食事療養・その他の届出・先進医療）は厚労省「届出受理医療機関名簿の
  受理番号欄における略称一覧」に合わせる。`make_abbr_order.py`（開発環境のみ・pdfplumber）が `shisetsu_raw/` の
  最新の略称一覧PDFから `abbr_order.json` を作り、`build_db.py` はそれを読むだけ。改定で略称一覧が新しくなったら
  PDFを `shisetsu_raw/` に置いて `make_abbr_order.py` を実行し直す。一覧に無い記号（廃止された届出等）は、
  名簿の届出の並び（略称一覧の順と99%一致）から位置を推定する
- **市区町村・二次医療圏**: 外来機能報告（`gairai_form1_annual.parquet`）の市区町村名称→二次医療圏名の対応に、
  住所の先頭を照合して決める（98.8%）。外来機能報告に無い町村は住所から市区町村名を取り出し、同じ郡の町村が
  すべて同じ二次医療圏ならそれを使う。決まらない432施設（0.5%）は「（二次医療圏不明）」

## 区分の参考値（病床機能報告で補う）

北海道・関東信越・近畿の厚生局は、入院基本料の区分（急性期一般入院料１など）を公表していない。
`build_db.py` は、病床機能報告と突合できた病院について、`ward_cache.parquet`（最新年度・7月1日時点）の
病棟ごとの入院基本料から区分を補い、`filings.kubun_ref` に入れる（公式の `kubun` が空の届出だけ）。
画面では「参考」タグと出典・時点を付け、公式の区分とは見分けがつくようにしている。CSVも別の列。

- 対象は一般病棟・療養病棟・障害者施設等・特定機能病院・専門病院の入院基本料（2026年10月で3,327件）。
  精神病棟・結核病棟・有床診療所は病床機能報告から補えないので空欄のまま
- 区分を公表している厚生局の届出と照合した精度（2026年10月）: 算定開始日が病床機能報告の時点以前の
  届出は99%一致、その後に出し直された届出は76%、うち急性期一般入院料１・特定機能病院は8%
  （令和8年度改定で急性期病院Ａ/Ｂ等に移ったため）。この結果から、出し直された届出にだけ注意書きを付ける
  （`app.py` の `kubun_ref_note()`）

## 開発時の起動（リポジトリ直下で）

```bash
pip install -r kijun_web/requirements.txt
python kijun_web/build_db.py          # 20秒ほど。ピークメモリ約600MB
uvicorn kijun_web.app:app --reload    # http://localhost:8000
python -m pytest kijun_web/tests -q
```

環境変数: `SITE_URL`（canonical・sitemapのURL、既定 `https://medilenz.jp`）、
`STREAMLIT_URL`（分析版へのリンク先）、`KIJUN_DB`（DBの場所）。

## 毎月の更新

1. ユーザー: 各地方厚生局の最新の「届出受理医療機関名簿」（全件Excel）を `shisetsu_raw/` に置いてpush
   - ファイル名はダウンロードしたままでよい。同じ県のファイルが複数あれば、`build_shisetsu_kijun.py` が
     ファイル内の「令和X年Y月Z日 現在」が新しい方だけを使う（古いファイルは後で消せばよい）
2. Claude: `build_shisetsu_kijun.py --dir shisetsu_raw/` で parquet を作り直し、行数が不自然に減っていないか
   旧データと比べてからコミット（CLAUDE.md「施設基準届出データを追加・更新する際の注意」参照）
3. サーバー: `git pull` → `python kijun_web/build_db.py` → `sudo systemctl restart kijun-web`
   - DBは一時ファイルに書いてから置き換えるので、作り直し中もサイトは古いDBで動き続ける
   - 直前のDBは `data/kijun.prev.sqlite` に残る（「今月の新規届出・取り下げ」機能の比較元にする予定）

## 本番環境（予定）

- VPS メモリ2GB推奨（アプリ自体は軽いが、`build_db.py` の実行時に約600MBを使う。1GBならスワップを足す）
- uvicorn（systemd常駐、127.0.0.1:8000）＋ nginx ＋ Let's Encrypt。手順は既存の `DEPLOY.md`（ConoHa VPS時代）とほぼ同じ
- ドメイン medilenz.jp（お名前.comで管理）のAレコードを新サーバーへ向ける
