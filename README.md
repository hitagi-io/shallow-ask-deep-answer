# 浅问深答

面向内部资料的 RAG 问答系统，使用 FastAPI、Vue 3 和 DeepAgents。PostgreSQL 保存账户、会话与索引清单，Milvus 保存文档向量。

- **双模式问答**：知识库模式检索内部资料并校验检索报告与引用文件；通用模式使用模型知识，可通过 Tavily 联网搜索。
- **流式会话**：保存历史、停止生成、断线重连，支持多个会话同时生成。
- **知识库管理**：管理员可上传、覆盖、删除 TXT、PDF、DOCX 文件，构建索引并检查、修复异常。

两种问答模式都会按问题选择回答长度与结构，优先给出结论或可直接使用的内容，并延续用户在当前会话中的语言、格式与详略要求。知识库回答保留制度条件、例外与文件归属；通用问答的联网结果保留每条来源对应的搜索片段，以便引用和说明信息缺口。

## 快速开始

以下命令使用 Windows PowerShell，默认在项目根目录执行。

### 1. 准备环境

准备 Python 3.12、Node.js 22.12+（含 npm）和 PostgreSQL。使用仓库内的 Milvus 部署配置还需 Docker（Linux 容器）与 Docker Compose。

启动 PostgreSQL 后创建数据库（已存在时跳过）：

```powershell
psql -U postgres -c "CREATE DATABASE shallow_ask_deep_answer;"
```

使用 [Compose 配置](./deploy/milvus/compose.yaml) 启动本地 Milvus 及其依赖：

```powershell
docker compose -f deploy/milvus/compose.yaml up -d --wait
```

Milvus 地址为 `http://localhost:19530`，健康检查地址为 `http://localhost:9092/healthz`。该配置用于本地开发，数据保存在 Docker 命名卷中；已有 Milvus 服务可跳过此步并修改 `config/vector_db.yaml` 中的连接地址。

后端首次启动会初始化数据库表与检查点结构，数据库账户需有建表和修改表权限。

### 2. 安装依赖与配置

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

编辑 `.env`：

| 参数 | 用途 |
| --- | --- |
| `PG_CONNECTION_STRING` | PostgreSQL 连接地址，数据库名与上一步一致 |
| `JWT_SECRET` | 必填，登录令牌签名密钥 |
| `OPENAI_API_KEY` | 聊天模型密钥，当前使用 DashScope 兼容接口 |
| `DASHSCOPE_API_KEY` | DashScope 文本嵌入密钥 |
| `TAVILY_API_KEY` | 可选，仅联网搜索需要 |

同一个具有相应权限的 DashScope 密钥可填入两项模型密钥。生成 `JWT_SECRET`：

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

启动前核对 `config/` 中的模型、接口与 Milvus 连接。`.env` 已被 Git 忽略，真实凭证只保存在本地。

### 3. 启动服务

启动后端：

```powershell
python api_server.py
```

在另一个终端，从项目根目录安装前端依赖并启动：

```powershell
npm --prefix frontend-chat ci
npm --prefix frontend-chat run dev
```

打开 [http://localhost:5173](http://localhost:5173)。后端默认监听 `0.0.0.0:8000`；修改后端端口时，同步调整 [Vite 代理](./frontend-chat/vite.config.ts)。

检查存储服务是否就绪：

```powershell
Invoke-RestMethod http://localhost:8000/api/health/ready
```

该接口仅检查 PostgreSQL 与 Milvus，不检查模型服务；问答异常时还需查看模型配置、密钥及后端日志。

### 4. 注册与使用

在页面注册并登录：用户名 2～50 字符；密码 10～128 字符，包含英文字母、数字和特殊符号。新账户默认为普通用户；连接 `shallow_ask_deep_answer` 数据库，为已有用户授予管理员权限：

```sql
UPDATE users SET role = 'admin' WHERE username = 'your-username';
```

权限默认缓存 30 秒，等待后刷新页面。管理员上传文档会自动尝试入库，确认入库成功后即可进行知识库问答。TXT 使用 UTF-8 编码；扫描版 PDF 需先转换为可提取文本的文件。

## 配置

| 文件或目录 | 内容 |
| --- | --- |
| [.env.example](./.env.example) | 环境变量模板：连接、密钥、端口、并发、限流和超时 |
| [config/agent.yaml](./config/agent.yaml) | 聊天模型、接口地址、请求超时与重试 |
| [config/vector_db.yaml](./config/vector_db.yaml) | Milvus、嵌入模型、文档切块、检索与健康检查 |
| [prompts/](./prompts/) | 知识库、检索、通用问答和会话压缩提示词 |
| [deploy/milvus/compose.yaml](./deploy/milvus/compose.yaml) | 本地 Milvus、etcd 与 MinIO 部署 |

修改配置或提示词后重启后端。需要查看接口文档时，在 `.env` 设置 `ENABLE_API_DOCS=true`，重启后访问 [http://localhost:8000/docs](http://localhost:8000/docs)。

## 使用与维护

- **会话**：首次发送后固定问答模式；切换模式会打开或复用空白会话。刷新、切换会话或短暂断网不会取消生成，点击“停止生成”才会取消；服务进程重启后不会自动续跑。生成中的会话不能再次发送或删除。
- **文档**：源文件保存在 `knowledge/`（被 Git 忽略），修复索引依赖这些文件。同内容文件共用索引；手动放入目录的新文件需点击“构建”。
- **索引**：启动后自动检查，此后默认每 6 小时检查并尝试修复；也可在管理页面查看健康报告并修复。检查会扫描源文件及向量文本，耗时随资料量增加。
- **索引变更**：修改分块参数、嵌入模型或相关依赖后需重建；修改解析逻辑或模型服务行为时，还需递增 `config/vector_db.yaml` 的 `index_version`。重启后点击“构建”，按需重建异常或过期内容。默认集合为 `knowledge_chunks_v1`，旧集合不自动迁移。
- **部署**：生产环境构建并托管 `frontend-chat/dist/`，将 `/api` 转发到后端，关闭 SSE 响应缓冲。并发、限流和超时参数见 `.env.example`。

## 开发检查

在项目根目录执行：

```powershell
python -m compileall -q api_server.py auth.py chat_agent.py rag utils
git diff --check
npm --prefix frontend-chat run build
```

前端构建包含 TypeScript 类型检查。业务改动需联调登录、上传、双模式问答、停止生成和索引修复；提示词改动还需核对工具及协议契约，检查引用、无结果和检索失败场景。

## 许可证

[MIT](./LICENSE)
