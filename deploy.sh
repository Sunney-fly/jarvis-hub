#!/usr/bin/env bash
# 一键部署（在 VPS 上运行）：生成密钥 → 生成日历账号 → 构建并启动全部服务
set -euo pipefail
cd "$(dirname "$0")/.."

bash scripts/seed-env.sh

# 生成 Radicale（日历）用户，密码与 .env 中的 RADICALE_PASSWORD 一致
RADICALE_PW=$(grep '^RADICALE_PASSWORD=' .env | cut -d= -f2-)
if [ -n "$RADICALE_PW" ]; then
  if command -v htpasswd >/dev/null 2>&1; then
    htpasswd -nbB jarvis "$RADICALE_PW" > tools/radicale/users
    echo "✅ 日历账号已生成（用户名 jarvis）"
  else
    echo "⚠️  未安装 htpasswd（apt install apache2-utils），日历登录会失败"
    echo "   请手动执行：htpasswd -nbB jarvis '$RADICALE_PW' > tools/radicale/users"
  fi
fi

docker compose up -d --build
echo ""
echo "✅ 服务已启动，查看状态：docker compose ps"
echo "   查看日志：docker compose logs -f core"
