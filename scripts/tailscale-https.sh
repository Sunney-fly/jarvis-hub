#!/usr/bin/env bash
# 启用 Tailscale HTTPS：手机浏览器只有 https 才能用麦克风（语音输入）
# 用法：bash scripts/tailscale-https.sh <主机名>.<tailnet名>.ts.net
# 例：  bash scripts/tailscale-https.sh jarvis-vps.xxx.ts.net
set -euo pipefail
cd "$(dirname "$0")/.."

FQDN="${1:?用法: scripts/tailscale-https.sh <主机名>.<tailnet名>.ts.net}"

mkdir -p /etc/tailscale-certs
tailscale cert --cert-file /etc/tailscale-certs/fullchain.pem --key-file /etc/tailscale-certs/privkey.pem "$FQDN"

# 写入 .env 供 Caddy 使用
if grep -q '^CADDY_HOST=' .env; then
  sed -i "s|^CADDY_HOST=.*|CADDY_HOST=$FQDN|" .env
else
  echo "CADDY_HOST=$FQDN" >> .env
fi

docker compose --profile https up -d caddy

echo "✅ HTTPS 已启用：https://$FQDN （手机浏览器打开此地址，麦克风/语音可用）"
echo ""
echo "证书 90 天过期，添加自动续期 cron（每月 1 日）："
echo "crontab -e 添加："
echo "0 4 1 * * tailscale cert --cert-file /etc/tailscale-certs/fullchain.pem --key-file /etc/tailscale-certs/privkey.pem $FQDN && cd /root/jarvis-hub && docker compose --profile https restart caddy"
