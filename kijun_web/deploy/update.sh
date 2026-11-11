#!/bin/bash
# kijun-update.timer から毎日呼ばれる。GitHub のブランチに新しいコミットがあれば取り込み、
# 依存パッケージとDBを作り直してサイトを再起動する。ドメインがこのサーバーを向いたらHTTPSの証明書も取る。
set -euo pipefail
# git reset でこのファイル自身が書き換わっても途中から読み違えないよう、全体を関数にしてから実行する
main() {
APP=/opt/kijun
REPO_DIR=$APP/byosho-tool
BRANCH=$(cat /etc/kijun-web.branch)
DOMAIN=medilenz.jp

cd "$REPO_DIR"
sudo -u kijun git fetch --depth 1 origin "$BRANCH"
if [ "$(sudo -u kijun git rev-parse HEAD)" != "$(sudo -u kijun git rev-parse FETCH_HEAD)" ]; then
  echo "更新あり: $(sudo -u kijun git rev-parse --short FETCH_HEAD)"
  sudo -u kijun git reset --hard FETCH_HEAD
  sudo -u kijun "$APP/venv/bin/pip" install -q -r kijun_web/requirements.txt
  # DBは一時ファイルに作ってから置き換えるので、作り直し中もサイトは古いDBで動き続ける
  sudo -u kijun "$APP/venv/bin/python" kijun_web/build_db.py
  systemctl restart kijun-web
else
  echo "更新なし"
fi

# HTTPS: ドメインがこのサーバーのIPを向いていて、まだ証明書が無ければ取る（更新は certbot.timer が行う）
MYIP=$(curl -s -4 https://api.ipify.org || true)
if [ ! -d "/etc/letsencrypt/live/$DOMAIN" ] && [ -n "$MYIP" ] && [ "$(dig +short A "$DOMAIN" | tail -1)" = "$MYIP" ]; then
  WWW=""
  [ "$(dig +short A "www.$DOMAIN" | tail -1)" = "$MYIP" ] && WWW="-d www.$DOMAIN"
  certbot --nginx --non-interactive --agree-tos --register-unsafely-without-email --redirect -d "$DOMAIN" $WWW
fi
}
main "$@"
exit
