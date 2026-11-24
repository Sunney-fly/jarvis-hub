#!/usr/bin/env bash
# 每日备份：Postgres 全量 + 日历/WebUI 数据卷，保留最近 10 天
# 在 VPS 上配置 cron：0 3 * * * cd /root/jarvis-hub && bash scripts/backup.sh >> /var/log/jarvis-backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."

STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p backups

docker compose exec -T postgres pg_dump -U "${POSTGRES_USER:-jarvis}" "${POSTGRES_DB:-jarvis}" \
  | gzip > "backups/jarvis-$STAMP.sql.gz"

docker run --rm -v jarvis_radicale_data:/data -v "$PWD/backups:/backup" alpine \
  tar czf "/backup/radicale-$STAMP.tgz" -C /data .

docker run --rm -v jarvis_webui_data:/data -v "$PWD/backups:/backup" alpine \
  tar czf "/backup/webui-$STAMP.tgz" -C /data .

# 清理：每种保留最近 10 份
for kind in jarvis radicale webui; do
  ls -1t "backups/$kind"-*.sql.gz "backups/$kind"-*.tgz 2>/dev/null | awk 'NR>10 {print}' | xargs -r rm -f || true
done

echo "[$(date '+%F %T')] 备份完成：backups/jarvis-$STAMP.sql.gz"
