#!/bin/bash
# ConoHa の管理画面でサーバーを作るときに「スタートアップスクリプト」に貼る2行。
# 本体（cloud-init.sh）を GitHub から取って実行する。
curl -fsSL https://raw.githubusercontent.com/pinetinag-beep/byosho-tool/claude/clever-brown-95s15n/kijun_web/deploy/cloud-init.sh -o /root/kijun-cloud-init.sh
bash /root/kijun-cloud-init.sh
