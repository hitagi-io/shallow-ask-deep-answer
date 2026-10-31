# 浅问深答

面向内部资料的检索增强生成（RAG）问答系统，提供知识库问答、通用问答、会话管理和知识库维护能力。

## 目录

- [功能概览](#功能概览)
- [技术栈](#技术栈)
- [项目结构](#项目结构)
- [快速开始](#快速开始)
- [知识库一致性](#知识库一致性)
- [许可证](#许可证)

## 功能概览

- **知识库问答（默认）**：事实性回答必须基于本轮检索到的内部资料。系统会校验检索报告和来源引用；没有有效证据、引用了本轮结果之外的文件，或检索服务异常时，会返回对应的固定提示，不会用模型自身知识补答。
- **通用问答**：使用模型自身知识回答，并在需要时通过 Tavily 搜索公开信息。搜索无结果和搜索服务故障会分别处理。
- **可控检索**：检索子代理会拆解问题、检索资料、评估相关性，并在必要时改写查询重试；单轮最多调用检索工具 3 次。
- **流式对话**：`POST /api/chat/stream` 通过 SSE 返回结果。通用模式实时输出；知识库模式在拿到检索证据后增量输出，并在结束前完成来源校验（校验不通过会整段替换为兜底文案）。服务端只在正常结束时发送 `done`，客户端断开、取消或超时会以 `cancelled` / 错误状态结束，并释放上游生成器与并发槽位；生成有端到端截止时间，单次模型请求也有超时与重试上限。
- **会话持久化**：用户消息、助手消息和生成任务全部由服务端在 PostgreSQL 中记录（含请求幂等键、消息 ID 和 completed/failed/cancelled 状态），同一会话同时只允许一个生成任务。服务端按固定间隔更新生成任务的心跳时间戳；进程异常退出后，超时未更新的运行中任务会由后续请求标记为失败并补写中断提示。LangGraph 检查点按 `thread_id` 恢复上下文；消息达到 30 条或 20,000 tokens 时自动摘要，并保留最近 10 条。
- **账户与限流**：使用 Argon2id 存储密码、JWT 完成鉴权，并区分普通用户和管理员；注册、登录、对话接口均有限流（限流判定与计数在数据库咨询锁内原子完成），对话还通过数据库租约限制单用户并发数，多 worker 部署下同样生效。
- **知识库管理**：支持 `.txt`、`.pdf`、`.docx` 文件的上传、删除、索引补建和健康检查。入库失败会清理相关向量与记录，并最多重试 3 次。
- **一致性修复**：按文件内容 MD5 去重，后台检查 `knowledge/`、`md5.txt` 和 Milvus 的状态，并自动修复残留向量、孤立记录和缺失向量。

## 技术栈

- **Agent 与模型**：deepagents、LangChain、LangGraph；主 Agent 和检索子代理默认使用 `deepseek-v4.1-flash-1017`，通过 DashScope 的 OpenAI 兼容接口调用。
- **检索与解析**：Milvus、`langchain-milvus`、`pymilvus`、DashScope 文本嵌入；使用 `pypdf` 和 `docx2txt` 解析文档，Tavily 提供联网搜索。
- **服务与存储**：FastAPI、Uvicorn、PostgreSQL。PostgreSQL 同时保存业务数据和 LangGraph 检查点，并提供跨进程咨询锁。
- **前端**：Vue 3、Vite、TypeScript、marked、DOMPurify。

## 项目结构

```text
DeepAgent-RAG/
├── api_server.py       # FastAPI 入口：对话、账户和知识库接口
├── main_deep_agent.py  # 主 Agent、双模式和工具配置
├── auth.py             # 账户、JWT、限流和会话数据访问
├── rag/                # 检索子代理、向量入库和索引健康检查
├── utils/              # 配置、提示词、文件、锁和回答校验工具
├── config/             # Agent 与向量库配置
├── prompts_data/       # 各模式的提示词模板
├── frontend-chat/      # Vue 前端
├── knowledge/          # 知识库源文件，运行时创建并被 Git 忽略
├── md5.txt             # 内容 MD5 入库记录，运行时生成并被 Git 忽略
├── requirements.txt    # Python 依赖
├── LICENSE             # MIT 许可证
└── .env                # 本地环境变量，不要提交
```

## 快速开始

建议使用 Python 3.12+、Node.js 20+，并准备 PostgreSQL 和 Docker。以下命令以 Windows PowerShell 为例。

### 1. 准备 PostgreSQL 和 Milvus

创建 PostgreSQL 数据库：

```powershell
psql -U postgres -c "CREATE DATABASE deepagent_rag;"
```

在用于保存 Milvus 配置和数据的目录中执行：

```powershell
cd D:\milvus
Invoke-WebRequest -Uri https://github.com/milvus-io/milvus/releases/download/v2.4.23/milvus-standalone-docker-compose.yml -OutFile docker-compose.yml
docker compose up -d
```

Milvus 默认监听 `http://localhost:19530`。可用以下命令检查服务：

```powershell
Invoke-WebRequest http://localhost:9091/healthz
```

停止 Milvus：

```powershell
docker compose down
```

### 2. 安装依赖并配置环境变量

在项目根目录创建虚拟环境并安装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

创建项目根目录下的 `.env`：

```ini
DASHSCOPE_API_KEY=your-dashscope-key
OPENAI_API_KEY=your-dashscope-key
TAVILY_API_KEY=your-tavily-key
PG_CONNECTION_STRING=postgresql://postgres:password@localhost:5432/deepagent_rag
JWT_SECRET=your-random-secret
JWT_EXPIRE_HOURS=72
```

`TAVILY_API_KEY` 只在通用问答需要联网搜索时使用。`JWT_SECRET` 必须配置，否则服务启动时会拒绝运行。可用以下命令生成：

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

模型参数位于 `config/agent.yaml`，向量库和索引参数位于 `config/vector_db.yaml`。

### 3. 启动后端和前端

在项目根目录启动后端：

```powershell
python api_server.py
```

默认监听 `0.0.0.0:8000`，启动时会自动初始化数据库。健康检查接口为 `GET /api/health`。

在另一个终端启动前端：

```powershell
cd frontend-chat
npm ci
npm run dev
```

打开 <http://localhost:5173>。Vite 会将 `/api` 请求代理到 `http://localhost:8000`。

### 4. 注册并使用

用户名长度为 2–50 个字符；密码至少 10 个字符，并且必须包含数字、英文字母和特殊符号。注册用户默认为普通用户。授予管理员权限时，在 PostgreSQL 中执行：

```sql
UPDATE users SET role = 'admin' WHERE username = 'your-username';
```

管理员可以在前端的知识库管理入口上传文档、查看索引状态并补建向量。对话页默认使用知识库问答；切换到通用问答后，模型可以使用自身知识，并在配置 `TAVILY_API_KEY` 时进行联网搜索。

## 知识库一致性

知识库由三部分组成：

- `knowledge/`：源文件；
- `md5.txt`：每行一个已入库内容的 MD5；
- Milvus：文档切块后的向量，`source` 元数据保存文件绝对路径。

### 内容去重

内容相同的文件只入库一次，并共享同一组向量。文档列表按 MD5 记录显示入库状态；健康检查还会确认磁盘文件和向量是否仍然存在。

### 入库与删除

单文件入库按“切分 → 写入向量 → 写入 MD5 → 校验”的顺序执行。校验要求向量条数等于切块数且 MD5 记录存在；如果 Milvus 的数据暂时不可见，系统会先 `flush` 后复验，仍失败则清理并重建，最多重试 3 次。

删除操作会同步清理源文件、MD5 记录和向量。若删除的是承载向量的文件，但目录中仍有相同内容的副本，系统会使用副本重新建立向量。

上传同名文件按覆盖处理：新版本先完成入库并通过校验，之后才清理旧版本向量；新版本入库失败时源文件回滚为旧版本，旧索引继续可用，不会出现内容已替换但检索不到的空窗。若旧内容在目录中还有同内容副本，原先挂在被覆盖文件上的向量会改挂到副本上，避免副本进入“记录在、向量无”的状态。

### 定时检查与自愈

后台按 `health_check_interval_hours` 检查一致性，实际间隔不低于 10 分钟。新上传但尚未构建索引的文件不视为异常。

| 问题 | 类型 | 修复动作 |
| --- | --- | --- |
| 向量和文件存在，但 MD5 记录缺失 | `missing_md5_record` | 根据 Milvus 和磁盘文件重建 `md5.txt` |
| 文件已删除，但向量或记录残留 | `ghost_vectors` / `orphan_md5` | 清理残留向量和孤立记录 |
| 文件和记录存在，但向量缺失 | `missing_vectors` | 选择一个同内容文件重新入库 |

修复顺序为“清理残留向量 → 重建 MD5 记录 → 重建缺失向量”。重索引失败时会保留待重试状态。多 worker 或多实例部署时，索引写操作和定时自愈分别通过 PostgreSQL 咨询锁协调。

## 许可证

本项目采用 [MIT 许可证](./LICENSE)。
