# Interface V2

[English](README.md)

Interface V2 是用于研究人们对 AI 系统信任的实验平台，包含 FastAPI 后端、SQLite
持久化、React 被试端与管理端、受控的 AI 对话流程、语音识别、数据导出工具和部署模板。

## 仓库范围

本仓库仅包含应用源代码、测试、依赖锁文件、安全的配置示例和通用运维文档。仓库不会包含被试记录、音频、导出文件、日志、密钥、本地开发环境、前端构建产物或内部设计与规划材料。

## 环境要求

- Python 3.11 或更高版本
- Node.js 20.19 或更高版本，或 Node.js 22.12 或更高版本
- 与 Node.js 匹配的 npm
- 推荐使用 `uv` 管理 Python 依赖

## 本地启动

在仓库根目录安装依赖：

```bash
uv sync --extra dev
cd frontend && npm ci
```

没有 `uv` 时可使用：

```bash
python3 -m pip install -e ".[dev]"
```

需要测试模型服务、ASR 或管理员功能时，请由 `deployment/interface-v2.env.example`
复制出受保护的环境文件并填入值；填好的环境文件不能提交到 Git。后端至少需要一个非空的 `APP_SECRET_KEY`。

启动后端：

```bash
APP_SECRET_KEY=local-development-only \
uv run python -m uvicorn backend.app.main:app --reload --port 8000 --no-proxy-headers
```

在另一个终端启动前端：

```bash
cd frontend && npm run dev
```

也可以使用一键开发脚本：

```bash
scripts/run_dev.sh
```

要以接近生产的方式检查静态资源服务，先构建前端，再启动后端：

```bash
cd frontend && npm run build
cd ..
APP_SECRET_KEY=local-development-only \
uv run python -m uvicorn backend.app.main:app --port 8000 --no-proxy-headers
```

`frontend/dist/` 是生成目录，已被 Git 忽略。

## 配置与安全

`deployment/interface-v2.env.example` 列出了由运维人员管理的变量。请复制为受保护的环境文件，再按目标环境填写；不得提交 API 密钥、管理员凭证、会话密钥或已填好的环境文件。

准备启用正式被试访问前，请确认：

- 修改 `APP_SECRET_KEY` 会使现有会话失效。
- `ADMIN_PASSWORD_HASH` 应使用 Argon2id 哈希。
- `APP_BASE_URL` 应为公开 HTTPS 地址；除 `localhost` 外，浏览器麦克风功能需要 HTTPS。
- `DATA_DIR` 和 `DATABASE_URL` 指向仅允许服务账号写入的目录。
- `TENCENT_ASR_ENDPOINT` 应与所用腾讯云 ASR 凭证所在的服务区域一致。

以下命令通过交互输入生成 Argon2id 哈希，避免密码进入 shell 历史：

```bash
uv run python -c 'from getpass import getpass; from argon2 import PasswordHasher; print(PasswordHasher().hash(getpass("Admin password: ")))'
```

## 验证

```bash
uv run python -m pytest backend/tests/test_health.py backend/tests/test_static_serving.py -v
uv run python -m py_compile backend/app/main.py backend/app/settings.py
cd frontend && npm run typecheck
cd frontend && npm run build
```

完整后端测试：

```bash
uv run python -m pytest backend/tests -v
```

## 文档

- [运维说明](docs/OPERATIONS.md)：安装、验证、安全部署、备份和恢复。
- [数据结构说明](docs/DATA_STRUCTURE.md)：主要实验记录、导出边界和数据处理要求。

## 数据处理

`data/` 下的运行时内容可能包含被试身份信息、实验回答、录音、转写和运维日志，因此默认被 Git 忽略；仓库只保留空目录标记 `.gitkeep`。发布改动前，请检查 `git status`，确认生成文件仍被忽略，并审查暂存内容中没有凭证或被试材料。
