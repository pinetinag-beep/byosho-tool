# 本番VPS移行手順（ConoHa VPS）

2026年8月、Streamlit Community Cloudから本番用のVPSへ移行する方針が決まった際の手順書。
背景・比較検討の経緯は `CLAUDE.md` の「インフラ・技術スタック検討」セクション参照。

**方針**: アプリのアーキテクチャ（Streamlit）は変更せず、ホスティング先だけをVPSに移す。
認証・マルチテナント等の有料化対応は今回のスコープ外（別途検討）。

**進め方**: 各ステップはユーザー側で実行し、詰まった箇所や出力結果をClaudeに共有しながら
進める（このリポジトリの他の作業依頼と同じ進め方）。コマンドはUbuntu想定。

---

## ステップ0: 事前準備

- ConoHa VPSのアカウント作成・支払い方法の登録（ConoHa側の画面で実施）
- ドメインを使うかどうか決める
  - 無い場合：VPSのグローバルIPアドレスに直接アクセスする形になる（`http://<IP>:8501` 等）。
    HTTPS化は後回しでもアプリ自体は動く。
  - 使う場合：お名前.com等でドメインを取得し、AレコードをVPSのIPに向ける
    （このステップだけ済ませておけば、後のnginx設定でスムーズ）。

---

## ステップ1: サーバー作成

1. ConoHaのコントロールパネルにログイン
2. 「サーバー追加」
3. **プラン**: メモリ2GB（CLAUDE.md記載の現状メモリ使用量見立て——施設基準届出約62MB＋DPC約197MB＋座標約74MB等——を踏まえた最低ライン。窮屈さを感じたら後から4GBにプラン変更可能）
4. **イメージタイプ**: Ubuntu 24.04（最新LTS）
5. **rootパスワード**: 強力なものを設定（1Passwordなどに保存）
6. **SSH Key**: 可能なら公開鍵認証を設定（パスワード認証よりセキュア。ConoHaのSSH Key機能で作成・登録）
7. ネームタグ（例: `byosho-tool-prod`）を入力して作成
8. 作成完了後、割り当てられた**グローバルIPアドレス**をメモ

---

## ステップ2: SSH接続確認

ローカルのターミナル（Windowsなら PowerShell や WSL、Macなら標準ターミナル）から：

```bash
ssh root@<グローバルIPアドレス>
```

初回は「fingerprintを確認しますか」と聞かれるので `yes`。ログインできればOK。

---

## ステップ3: 基本セットアップ（セキュリティ）

**rootで直接運用し続けるのはリスクが高いので、まず一般ユーザーを作る。**

```bash
adduser deploy
usermod -aG sudo deploy
```

SSH Keyを使っている場合は、そのキーを新ユーザーにもコピー：

```bash
rsync --archive --chown=deploy:deploy ~/.ssh /home/deploy
```

ファイアウォール（SSH・HTTP・HTTPSのみ許可）：

```bash
ufw allow OpenSSH
ufw allow 80
ufw allow 443
ufw enable
```

以降は `ssh deploy@<IP>` で作業する（root直接ログインは徐々に無効化していく）。

---

## ステップ4: Python環境の構築

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv git nginx
```

Pythonのバージョン確認（3.11以上を推奨。requirements.txtの依存パッケージが対応しているか要確認）：

```bash
python3 --version
```

---

## ステップ5: リポジトリの取得

```bash
cd ~
git clone https://github.com/pinetinag-beep/byosho-tool.git
cd byosho-tool
```

**プライベートリポジトリの場合**、パスワード認証は使えないため、GitHubのPersonal Access
Token（PAT）を作るか、SSH鍵をこのサーバーにも登録してGitHub側に公開鍵を追加する必要がある。
`git clone git@github.com:pinetinag-beep/byosho-tool.git`（SSH経由）が扱いやすい。

`data_cache.parquet` / `ward_cache.parquet` / `surgery_cache.parquet` 等は
リポジトリに含まれているので、clone するだけでデータも一緒に来る（`CLAUDE.md`の
「データ管理」セクション参照）。

---

## ステップ6: 依存パッケージのインストール

仮想環境を作ってから入れる（システムのPythonを汚さないため）：

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

---

## ステップ7: 動作確認（フォアグラウンドで一度起動）

```bash
streamlit run app.py --server.port 8501 --server.address 0.0.0.0
```

ローカルPCのブラウザから `http://<グローバルIPアドレス>:8501` にアクセスして起動確認
（ステップ3のufwで一時的に `ufw allow 8501` しておくと確認しやすい。確認できたら
`ufw delete allow 8501` で閉じる——最終的には80/443経由でのみアクセスさせる）。
`Ctrl+C` で停止。

---

## ステップ8: systemdサービス化（永続稼働）

フォアグラウンド起動だとSSHを切断すると止まってしまうので、常駐サービスとして登録する。

```bash
sudo nano /etc/systemd/system/byosho-tool.service
```

内容：

```ini
[Unit]
Description=byosho-tool Streamlit app
After=network.target

[Service]
Type=simple
User=deploy
WorkingDirectory=/home/deploy/byosho-tool
ExecStart=/home/deploy/byosho-tool/venv/bin/streamlit run app.py --server.port 8501 --server.address 127.0.0.1 --server.headless true
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

（`--server.address 127.0.0.1` にして外部から直接8501ポートを叩けないようにし、
nginxだけを外部公開する構成にする。）

```bash
sudo systemctl daemon-reload
sudo systemctl enable byosho-tool
sudo systemctl start byosho-tool
sudo systemctl status byosho-tool
```

`active (running)` になっていればOK。ログ確認は `journalctl -u byosho-tool -f`。

---

## ステップ9: nginxリバースプロキシ + SSL

```bash
sudo nano /etc/nginx/sites-available/byosho-tool
```

内容（ドメインがある場合は `server_name` にそのドメインを、無い場合はIPアドレスを指定）：

```nginx
server {
    listen 80;
    server_name <ドメイン名 または グローバルIP>;

    location / {
        proxy_pass http://127.0.0.1:8501;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 86400;
    }
}
```

（`Upgrade`/`Connection` ヘッダーはStreamlitのWebSocket通信に必須。忘れると
画面が固まって動かなくなる。）

```bash
sudo ln -s /etc/nginx/sites-available/byosho-tool /etc/nginx/sites-enabled/
sudo nginx -t   # 設定ファイルの文法チェック
sudo systemctl reload nginx
```

ここで `http://<ドメイン or IP>` にアクセスしてアプリが表示されればOK。

**ドメインがある場合はSSL化**（Let's Encrypt、無料）：

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d <ドメイン名>
```

対話式で進み、自動でnginx設定にSSL証明書を組み込んでくれる。証明書は自動更新設定も
一緒に入る（`certbot renew --dry-run` で更新テスト可能）。

---

## ステップ10: 主要画面の動作確認

`CLAUDE.md`にある通り、「pushの前に必ずローカルでStreamlitを起動して主要画面の
動作を再確認する」のと同じ考え方で、本番URLで以下を一通りブラウザ確認する：

- ホーム画面（3カード表示）
- 病院名で探す → 病院詳細ページ（各タブ）
- 距離・所要時間で探す（病床機能報告／DPC両方）
- 条件で病院を検索（病床機能報告／DPC両方）
- 管理者パネル（キャッシュクリア等が動くか）

---

## 今後の更新フロー（データ更新・コード変更の反映）

```bash
cd ~/byosho-tool
git pull
source venv/bin/activate
pip install -r requirements.txt   # 依存が変わっていれば
sudo systemctl restart byosho-tool
```

**MHLWオープンデータの403問題は変わらない**——本番サーバーから `www.mhlw.go.jp` へ
直接アクセスすると403になるため、生データのダウンロードは引き続きユーザーのブラウザ
経由で行い、`git push` でこのサーバーに反映する運用を継続する（`CLAUDE.md`参照）。

---

## 未検討・今後の課題

- **メモリ監視**: 2GBプランで実際にどれだけ余裕があるか、本番トラフィックで様子を見る
  （`free -h` や `htop` で確認。窮屈ならConoHaのプラン変更で4GBに増設可能）
- **バックアップ**: VPSのスナップショット機能を定期的に使うか、`data/`配下の重要ファイルを
  外部（S3等）にも退避するか検討
- **監視・アラート**: サービスが落ちた時に気づける仕組み（UptimeRobot等の外形監視が手軽）
- **Streamlit Cloud（現行）との並行運用期間**: 切り替え時にどちらを正としてDNSを向けるか、
  切り替えのタイミングと切り戻し手順
