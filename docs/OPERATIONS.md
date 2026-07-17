# 运维说明

本文档说明如何在任意受控环境中运行、验证、部署和维护 Interface V2。所有命令均从仓库根目录执行；路径以变量表示，不依赖某台开发机或某个特定服务器。

## 1. 运行形态

开发阶段可分别运行 Vite 前端和 FastAPI 后端。生产环境应先构建 React 前端，再由 FastAPI 同时提供 API、管理入口和静态资源：

```text
浏览器 -> HTTPS 反向代理 -> FastAPI -> SQLite 数据目录
```

正式被试使用语音功能时，公开访问必须使用 HTTPS。请将反向代理的可信代理设置、证书续期和网络访问控制纳入所在组织的运维流程。

## 2. 安装与本地验证

```bash
uv sync --extra dev
cd frontend && npm ci
cd ..
uv run python -m pytest backend/tests/test_health.py backend/tests/test_static_serving.py -v
uv run python -m py_compile backend/app/main.py backend/app/settings.py
cd frontend && npm run typecheck && npm run build
```

完整的后端验证命令为：

```bash
uv run python -m pytest backend/tests -v
```

执行 `npm run build` 会生成 `frontend/dist/`。该目录由 FastAPI 在生产模式下提供，但不应被提交到 Git。

## 3. 环境变量

以 `deployment/interface-v2.env.example` 为模板创建只允许服务账号读取的环境文件。填写后的文件不得提交。至少应配置以下项目：

| 配置 | 作用 |
| --- | --- |
| `APP_BASE_URL` | 对外 HTTPS 地址 |
| `APP_SECRET_KEY` | 会话签名密钥；变更后现有会话失效 |
| `DATA_DIR`、`DATABASE_URL` | SQLite 数据和运行文件位置 |
| `ADMIN_USER`、`ADMIN_PASSWORD_HASH` | 管理员认证；密码哈希使用 Argon2id |
| 模型服务变量 | 业务对话和评估所需的服务地址、密钥和模型标识 |
| 腾讯 ASR 变量 | 腾讯云访问凭证及其对应服务区域的端点 |

建议将应用程序目录、状态目录、配置目录和备份目录分别设为：

```text
APP_DIR      只读的应用代码与虚拟环境
STATE_DIR    服务账号独占的数据库、音频、导出和日志
CONFIG_DIR   受保护的环境变量文件
BACKUP_DIR   受保护的备份文件
```

服务账号应能写入 `STATE_DIR` 和 `BACKUP_DIR`，但不应能修改 `APP_DIR` 或配置模板。数据库、音频、导出和日志不能放入工作副本，也不能交由 Git 同步。

## 4. 生产部署

1. 在受控的 `APP_DIR` 中获取经过审查的提交。
2. 使用锁文件建立生产 Python 环境，并执行 `npm ci && npm run build`。
3. 创建受保护的 `STATE_DIR`、`CONFIG_DIR` 和 `BACKUP_DIR`；将模板复制为环境文件并填写密钥。
4. 复制 `deployment/interface-v2.service`，再将其中的工作目录、环境文件、可写目录和 Python 可执行文件改为当前主机的实际目录。
5. 由服务管理器加载并启动服务，确认服务只监听本地回环地址。
6. 由 HTTPS 反向代理将公开请求转发给该本地端口；不要直接公开应用端口。
7. 通过浏览器和受控凭证验证被试端、管理端、ASR、模型服务和导出流程后，再决定是否开放招募。

示例服务文件默认采用严格的 systemd 隔离选项。修改主机路径时，`WorkingDirectory`、`EnvironmentFile`、`ExecStart` 和 `ReadWritePaths` 必须保持一致。

## 5. 上线后检查

在反向代理和服务均启动后，确认：

```bash
curl -i https://<public-host>/api/health
curl -i https://<public-host>/admin
```

健康检查应成功返回，管理端应显示登录页面。随后使用非真实被试数据验证：登录、前测、实验流程、录音授权、ASR 转写、管理端导出与恢复。不要在日志、命令历史或截图中记录密码、会话 Cookie、API 密钥或被试信息。

## 6. 备份与恢复

使用应用提供的脚本生成和恢复备份：

```bash
uv run python scripts/backup_data.py --help
uv run python scripts/restore_backup.py --help
```

备份应写入 `BACKUP_DIR`，并按照组织的数据保留政策进行加密、访问控制和异地保存。恢复前先停止写入应用，保留当前数据副本，并在隔离环境验证恢复结果。恢复完成后，再运行健康检查和导出检查。

## 7. 发布前清单

- [ ] 后端测试、Python 编译、前端类型检查和构建均通过。
- [ ] `git status` 中没有运行数据库、WAL/SHM 文件、音频、导出、日志、环境文件或构建产物。
- [ ] 已审查暂存变更，不含密钥、密码哈希、被试身份信息或真实实验数据。
- [ ] HTTPS、管理端认证、存储权限、备份和恢复流程已在目标环境验证。
- [ ] 已取得研究与数据治理流程要求的上线授权。
