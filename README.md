# jarvis-hub

**一个人的 AI 中枢：模型主备自动切换、工具自装、跨设备长期记忆。**

A self-hosted personal AI hub: primary/backup model failover, self-installing tools, and
cross-device memory. Runs on one small VPS with Docker Compose. 6k lines of Python, 69 tests.

---

## 它不是什么

不是又一个「套个界面接 ChatGPT」的项目。它要解决的是几个真实存在、但套壳项目都不碰的问题：

| 问题 | 套壳项目的做法 | 这里的做法 |
|---|---|---|
| 主模型挂了怎么办 | 报错 | 按 `PRIMARY_MODEL` → `BACKUP_MODELS` 顺序自动切换（连接失败 / 超时 / 5xx 触发；**超时默认 120 秒，挂起会等满才切**，不是秒切） |
| 需要一个新工具 | 等开发者加 | 三层机制：写 YAML / 接 MCP / 让它自己写代码装上 |
| 昨天聊过的事 | 忘了 | Postgres + pgvector 长期记忆，跨设备同一份 |
| 只有主动问才有用 | 是 | 定时早间简报 + 事件提醒，主动推送 |
| 不知道花了多少钱 | 不知道 | 每次调用记账，月度预算超支告警 |

## 架构

```
VPS (4C8G, Docker Compose)
├── core        FastAPI 中枢：Agent 循环 / 调度 / 记忆 / 记账   ← 本仓库主体
├── postgres    Postgres 16 + pgvector（会话、记忆、任务、账本）
├── webui       Open WebUI（现成前端，手机 PWA 入口）
├── searxng     搜索聚合
├── radicale    CalDAV 日历
├── browser     Playwright 渲染服务
├── uptime-kuma 监控告警
└── caddy       （可选）Tailscale HTTPS，解锁手机麦克风
```

`core` 内部的模块边界：

```
core/src/core/
├── main.py       HTTP 路由、鉴权、SSE（最大的单文件，~1100 行）
├── agent/        Agent 循环（规划 → 执行 → 检查）与提示词
├── models/       provider 主备路由 + 复杂度分类器（便宜档 / 强档）
├── tools/        registry + builtin/ + dynamic/（三层自装机制）
├── memory/       事实抽取与检索（pgvector）
├── knowledge/    文档入库与检索
├── scheduler/    定时任务
├── push/         微信推送
└── voice/        STT / TTS
```

## 三层「自己装工具」

这是整个项目里最不常见的一块，见 [docs/TOOLS.md](docs/TOOLS.md)。

| 层 | 方式 | 装一个工具的工作量 | 护栏 |
|---|---|---|---|
| 1 声明式 | 写一个 YAML | 2 分钟 | 低风险，只调 HTTP / 命令 |
| 2 MCP | 加一行 `mcp.json` 配置 | 1 分钟 | 复用现成生态 |
| 3 自我写码 | 跟它说一句话 | 0（它自己写 + 测 + 装） | import 白名单 + 冒烟测试 + 第二个模型审阅 + 默认确认模式。**这不是沙箱**：拦得住明显危险调用，拦不住逻辑层面的越权 |

所有动态工具热生效，不用重启。第 3 层默认 `TOOL_INSTALL_MODE=confirm`——装之前必须你点头。

## 快速开始

前提：一台 Ubuntu 22.04+ 的 VPS（4C8G 起）、Docker、至少一个模型 API Key。

```bash
git clone https://github.com/Sunney-fly/jarvis-hub.git
cd jarvis-hub

# 1. 准备配置：模板里没有真实密钥，只有占位符
cp env.example .env
# 2. 填至少一个模型 key（DEEPSEEK_API_KEY / QWEN_API_KEY / GLM_API_KEY 任选其一）
$EDITOR .env
# 3. 自动补随机密码（数据库、日历、鉴权 token）
bash scripts/seed-env.sh
# 4. 起来
docker compose up -d
# 5. 看日志确认 core 就绪
docker compose logs -f core
```

完整部署（Tailscale 私网、日历、微信推送、语音）见 [docs/DEPLOY.md](docs/DEPLOY.md)。

## 本地验证（不需要 VPS）

核心逻辑可以离线跑，测试自带假的模型路由，不消耗额度：

```bash
cd core
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q
# 69 passed
```

## 文档

| 文件 | 内容 |
|---|---|
| [docs/DEPLOY.md](docs/DEPLOY.md) | 从裸 VPS 到可用，逐步骤 |
| [docs/TOOLS.md](docs/TOOLS.md) | 三层工具机制与 YAML 写法 |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | 日常运维、备份、恢复、排障 |
| [docs/COST.md](docs/COST.md) | 成本模型与预算控制 |
| [docs/ANDROID.md](docs/ANDROID.md) | 手机端接入（PWA + 语音 + 日历订阅） |
| [docs/APP-UPDATE.md](docs/APP-UPDATE.md) | 端侧 App 的更新分发链路 |

## 边界与已知限制

- **需要真实 API Key**，没有「零配置试用」模式。这是有意的：它的价值在编排真实模型，mock 起来没意义。
- **单用户设计**。没有多租户、没有权限体系，`AUTH_TOKEN` 一把钥匙。
- **中文优先**。提示词、分类器的关键词表都是按中文调过的。
- **部署有门槛**。4C8G、Docker、国内网络环境需要配镜像加速；这些在 DEPLOY.md 里都写了。
- 记忆检索用的是**向量相似度 + 阈值**，不是图结构；跨文档的多跳推理会弱。

## License

MIT
