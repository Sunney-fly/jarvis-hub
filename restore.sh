#!/usr/bin/env bash
# 恢复数据库备份
# 用法：bash scripts/restore.sh backups/jarvis-20250601-030000.sql.gz
set -euo pipefail
cd "$(dirname "$0")/.."

FILE="${1:?用法: scripts/restore.sh backups/jarvis-xxx.sql.gz}"
if [ ! -f "$FILE" ]; then
  echo "文件不存在: $FILE"
  exit 1
fi

echo "⚠️  将覆盖当前数据库内容，10 秒内 Ctrl+C 可取消"
sleep 10

gunzip -c "$FILE" | docker compose exec -T postgres psql -U "${POSTGRES_USER:-jarvis}" -d "${POSTGRES_DB:-jarvis}"
echo "✅ 数据库已恢复（日历/WebUI 数据卷恢复见 docs/OPERATIONS.md）"
