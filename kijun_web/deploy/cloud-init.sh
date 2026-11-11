#!/bin/bash
# 施設基準届出 検索サイト（kijun_web）の本番サーバーを、新しく作ったUbuntuサーバーの初回起動時に組み立てる。
# ConoHa のサーバー作成時に user_data として渡す（Claude の環境からはSSHで入れないため、サーバー自身に
# GitHub からコードを取らせて組み立てる）。何度実行しても同じ状態になるように書いてある。
# 経過は /var/log/kijun-setup.log、終わると /var/log/kijun-setup.done ができる。
set -euxo pipefail
exec > >(tee -a /var/log/kijun-setup.log) 2>&1

REPO=https://github.com/pinetinag-beep/byosho-tool.git
BRANCH=claude/clever-brown-95s15n       # 公開に使うブランチ（/etc/kijun-web.branch で変えられる）
APP=/opt/kijun
export DEBIAN_FRONTEND=noninteractive

timedatectl set-timezone Asia/Tokyo || true

# データベースの作り直しで一時的に約600MB使うので、メモリ2GBでも念のためスワップを足す
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q /swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

apt-get update
apt-get install -y git python3-venv python3-pip nginx certbot python3-certbot-nginx dnsutils ufw

ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable

id kijun >/dev/null 2>&1 || useradd --system --create-home --home-dir "$APP" --shell /usr/sbin/nologin kijun
[ -f /etc/kijun-web.branch ] || echo "$BRANCH" > /etc/kijun-web.branch
BRANCH=$(cat /etc/kijun-web.branch)
if [ ! -d "$APP/byosho-tool/.git" ]; then
  sudo -u kijun git clone --depth 1 --branch "$BRANCH" "$REPO" "$APP/byosho-tool"
fi
[ -d "$APP/venv" ] || sudo -u kijun python3 -m venv "$APP/venv"
sudo -u kijun "$APP/venv/bin/pip" install -q --upgrade pip
sudo -u kijun "$APP/venv/bin/pip" install -q -r "$APP/byosho-tool/kijun_web/requirements.txt"
sudo -u kijun bash -c "cd $APP/byosho-tool && $APP/venv/bin/python kijun_web/build_db.py"

# 設定（広告のIDなどは公開後にここへ書いて `systemctl restart kijun-web`）
if [ ! -f /etc/kijun-web.env ]; then
  cat > /etc/kijun-web.env <<'ENV'
SITE_URL=https://medilenz.jp
ADSENSE_CLIENT=
ADSENSE_SLOT=
CONTACT_EMAIL=
ENV
fi

cat > /etc/systemd/system/kijun-web.service <<UNIT
[Unit]
Description=MedilenZ 施設基準届出 検索サイト
After=network.target

[Service]
User=kijun
WorkingDirectory=$APP/byosho-tool
EnvironmentFile=/etc/kijun-web.env
ExecStart=$APP/venv/bin/uvicorn kijun_web.app:app --host 127.0.0.1 --port 8000 --workers 2 --proxy-headers
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

# 1時間ごとに、GitHub に新しいデータ・コードがあれば取り込んでDBを作り直す（無ければ確認だけで終わる。
# HTTPSの証明書の取得もここで試す）
cat > /etc/systemd/system/kijun-update.service <<UNIT
[Unit]
Description=MedilenZ 施設基準検索サイトの更新
[Service]
Type=oneshot
ExecStart=/bin/bash $APP/byosho-tool/kijun_web/deploy/update.sh
UNIT
cat > /etc/systemd/system/kijun-update.timer <<'UNIT'
[Unit]
Description=MedilenZ 施設基準検索サイトの定期更新
[Timer]
OnCalendar=hourly
OnBootSec=5min
Persistent=true
[Install]
WantedBy=timers.target
UNIT

cat > /etc/nginx/sites-available/kijun-web <<NGINX
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name medilenz.jp www.medilenz.jp _;
    client_max_body_size 1m;

    location /static/ {
        alias $APP/byosho-tool/kijun_web/static/;
        expires 7d;
    }
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 120s;
    }
}
NGINX
ln -sf /etc/nginx/sites-available/kijun-web /etc/nginx/sites-enabled/kijun-web
rm -f /etc/nginx/sites-enabled/default
chmod o+x "$APP"   # nginx が static を読めるように
nginx -t

systemctl daemon-reload
systemctl enable --now kijun-web
systemctl enable --now kijun-update.timer
systemctl reload nginx

date > /var/log/kijun-setup.done
