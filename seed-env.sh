#!/usr/bin/env bash
# 生成 .env 并填入随机密钥（在 VPS 上运行）
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  cp env.example .env
fi

gen() { openssl rand -hex 24; }

sed -i "s|^POSTGRES_PASSWORD=$|POSTGRES_PASSWORD=$(gen)|" .env
sed -i "s|^AUTH_TOKEN=$|AUTH_TOKEN=$(gen)|" .env
sed -i "s|^WEBUI_SECRET_KEY=$|WEBUI_SECRET_KEY=$(gen)|" .env
sed -i "s|^RADICALE_PASSWORD=$|RADICALE_PASSWORD=$(gen)|" .env

echo "✅ 已生成 .env（随机密钥已填好）"
echo "下一步：编辑 .env，填入以下 API Key（至少填一个模型）："
echo "  - DEEPSEEK_API_KEY（必填，platform.deepseek.com）"
echo "  - QWEN_API_KEY / GLM_API_KEY（备用，可选）"
echo "  - SILICONFLOW_API_KEY（记忆 + 语音转写，强烈建议）"
echo "  - WXPUSHER_APP_TOKEN / WXPUSHER_UIDS（微信推送）"
echo "  - QWEATHER_API_KEY（天气，可选）"