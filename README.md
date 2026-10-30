# 浅问深答

面向内部资料的检索增强生成（RAG）问答系统。

## 目录

- [核心特性](#核心特性)
- [技术栈](#技术栈)
- [项目结构](#项目结构)
- [快速开始](#快速开始)
- [知识库一致性](#知识库一致性)
- [许可证](#许可证)

## 核心特性

- **双模式问答**
  - **知识库模式（默认）**：寒暄、翻译、改写等不依赖外部事实的任务直接回答，不进行检索；其余涉及事实的问题，先结合对话历史补全指代和省略信息，改写为不依赖上下文、可独立检索的完整问题（原问题已完整时保持原样），再交由检索子代理处理。事实性内容只能来自检索结果，并在回答末尾附参考文件；检索无结果或服务故障时返回对应的固定提示，不会使用模型自身知识作答。
  - **通用模式**：使用模型自身知识回答，必要时使用 Tavily 进行联网搜索，并区分“搜索故障”与“搜索无结果”。
- **可控检索与降级**：检索子代理按“拆解 → 检索 → 相关性评估 → 改写重试”执行，提示词要求单次回答最多调用检索工具 3 次。“未找到资料”与“检索服务故障”由服务端分别接管为固定文案；主 Agent 每次调用模型时还会动态注入当天日期，用于理解相对时间。
- **流式对话与主动停止**：`POST /api/chat/stream` 通过 SSE 增量返回内容。点击“停止”后，服务端关闭上游生成器并释放并发槽位，已生成的内容保留在当前会话中。
- **会话持久化与压缩**：LangGraph 检查点和业务消息均存入 PostgreSQL，会话按 `thread_id` 恢复。达到 30 条消息或 20,000 tokens 时自动摘要压缩，保留最近 10 条消息。
- **账户与限流**：使用 Argon2id 密码哈希和 JWT 鉴权（默认有效期 72 小时），区分管理员与普通用户；登录、注册和对话接口均有限流，对话另有单用户并发上限。
- **知识库管理**：支持 `.txt`、`.pdf`、`.docx` 的上传、删除和索引补建。上传后自动切分、入库并校验，失败时清理该文件的向量与记录后重试，最多尝试 3 次。
- **内容去重与自动修复**：按文件内容 MD5 去重，后台按 `health_check_interval_hours` 指定的间隔检查并修复源文件、入库记录和 Milvus 向量之间的不一致（最小间隔 10 分钟）；索引操作和定时任务通过 PostgreSQL 咨询锁保证多进程安全。

## 技术栈

- **Agent 与模型**：deepagents + LangChain + LangGraph；主 Agent 与检索子代理默认使用 `deepseek-v4.1-flash-1017`，通过 DashScope 的 OpenAI 兼容接口调用。
- **检索与解析**：Milvus（langchain-milvus / pymilvus）+ DashScope 文本嵌入（默认 `qwen3.7-text-embedding-flash`）；pypdf、docx2txt 解析文档，Tavily 提供联网搜索。
- **服务与存储**：FastAPI + Uvicorn；PostgreSQL 保存业务数据和 LangGraph 检查点，并提供咨询锁。
- **前端**：Vue 3 + Vite + TypeScript；marked 渲染 Markdown，DOMPurify 过滤 XSS。

## 项目结构

```text
DeepAgent-RAG/
├── README.md           # 项目说明与使用指南
├── api_server.py       # FastAPI 入口：对话、账户与知识库接口
├── main_deep_agent.py  # 主 Agent 构建与双模式切换
├── auth.py             # 账户认证、JWT、限流与会话存储
├── rag/                # 检索子代理、向量入库与索引自愈
├── utils/              # 配置与提示词加载、文件工具、PG 咨询锁、降级处理
├── config/             # Agent 与向量库配置
├── prompts_data/       # 主 Agent、通用模式与检索子代理提示词
├── frontend-chat/      # Vue 3 + Vite + TypeScript 前端
├── requirements.txt    # Python 依赖清单
├── knowledge/          # 知识库源文件（运行时创建，已忽略）
├── md5.txt             # 内容 MD5 入库记录（运行时生成，已忽略）
├── LICENSE             # MIT 许可证
├── .gitignore          # Git 忽略规则
└── .env                # 环境变量（需自行创建，勿提交）
```

## 快速开始

建议使用 Python 3.12+、Node.js 20+，并提前安装 PostgreSQL 和 Docker。以下命令以 Windows PowerShell 为例。

### 1. 准备 PostgreSQL 与 Milvus

安装 PostgreSQL，将其 `bin` 目录加入系统 `Path`，然后创建数据库：

```powershell
psql -U postgres -c "CREATE DATABASE deepagent_rag;"   # 按提示输入 postgres 密码
psql -U postgres -l                                    # 确认数据库已创建
```

在用于存放 Milvus 配置和数据的目录（例如 `D:\milvus`）执行：

```powershell
cd D:\milvus
Invoke-WebRequest -Uri https://github.com/milvus-io/milvus/releases/download/v2.4.23/milvus-standalone-docker-compose.yml -OutFile docker-compose.yml
docker compose up -d
```

数据保存在该目录的 `volumes/` 中，停止服务使用 `docker compose down`。Milvus 默认地址为 `http://localhost:19530`；执行 `Invoke-WebRequest http://localhost:9091/healthz`，返回 `OK` 即表示服务正常。

### 2. 安装依赖并配置环境变量

在项目根目录创建虚拟环境并安装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1     # 若禁止运行脚本，改用 .\.venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

`requirements.txt` 已按 Python 3.12 环境固定主要依赖版本。

在项目根目录创建 `.env`：

```ini
DASHSCOPE_API_KEY=your-dashscope-key   # 文本嵌入使用
OPENAI_API_KEY=your-dashscope-key      # OpenAI 兼容模型接口使用；填同一个 Key
TAVILY_API_KEY=your-tavily-key         # 可选；不配置时联网搜索不可用
PG_CONNECTION_STRING=postgresql://postgres:password@localhost:5432/deepagent_rag
JWT_SECRET=your-random-secret          # 必填，缺失时拒绝启动
JWT_EXPIRE_HOURS=72
```

使用以下命令生成 `JWT_SECRET`：

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

模型与向量库参数分别位于 `config/agent.yaml` 和 `config/vector_db.yaml`。

### 3. 启动服务

后端和前端分别在独立终端中运行。

**后端（项目根目录）**：

```powershell
python api_server.py
```

默认监听 `0.0.0.0:8000`，启动时自动建表，健康检查接口为 `GET /api/health`。

**前端（`frontend-chat` 目录）**：

```powershell
npm ci
npm run dev
```

打开 `http://localhost:5173`，Vite 会将 `/api` 代理到 `http://localhost:8000`。

### 4. 注册、授权与使用

用户名须为 2–50 个字符；密码至少 10 个字符，且包含数字、英文字母和特殊符号。注册用户默认为普通用户，如需授予管理员权限，连接 PostgreSQL 执行以下语句，然后重新登录：

```sql
UPDATE users SET role = 'admin' WHERE username = 'your-username';
```

管理员可通过侧栏“知识库”或左下角设置菜单进入“管理知识库”上传文档。若文件上传成功但入库失败，请在文档列表中查看状态，并使用“补建向量库”重新入库。

对话页默认选中“知识库问答”，依据内部资料回答。输入框底部可切换为“通用问答”，使用模型知识并按需联网搜索；联网搜索需配置 `TAVILY_API_KEY`。窄屏下可通过左上角菜单打开侧栏。

## 知识库一致性

知识库由 `knowledge/` 中的源文件、`md5.txt` 中的入库记录（每行一个内容 MD5）和 Milvus 向量组成，向量的 `source` 元数据保存文件绝对路径。

**内容级去重**

相同内容的文件只入库一次并共享一组向量，`source` 指向首个入库文件。文档列表根据 MD5 记录显示“已入库”，健康检查则进一步核对该内容是否仍有文件和向量支撑。

**入库与删除**

入库依次经过“切分 → 写入向量 → 写入记录 → 立即校验”，要求该 `source` 的向量条数等于切块数且记录存在。若新数据因 Milvus 的 Bounded 一致性暂时不可见，系统会先执行 `flush` 再复验；仍不通过则清理并重建，最多尝试 3 次。`md5.txt` 的删除和整体重建通过临时文件原子替换，新增记录则直接追加。

删除时先移除磁盘文件，再清理记录和向量；向量删除失败时会尝试恢复记录。若被删除的文件持有向量，但仍有同内容副本，系统会立即使用副本重新入库。残留状态由后续健康检查修复。

**定时检查与自愈**

后台按 `health_check_interval_hours` 指定的间隔（最小 10 分钟）检查以下问题；新上传但从未构建的文件不属于异常：

| 现象 | 问题类型 | 修复动作 |
| --- | --- | --- |
| 向量和文件存在，但入库记录缺失 | `missing_md5_record` | 以 Milvus 与磁盘文件为准重建 `md5.txt` |
| 文件已删除，但向量或记录残留 | `ghost_vectors` / `orphan_md5` | 清理残留向量并剔除孤立记录 |
| 文件和记录存在，但向量缺失 | `missing_vectors` | 选择该内容的一个代表文件重新入库 |

修复按“清理残留向量 → 重建入库记录 → 重建缺失向量”的顺序执行。重索引失败时会补回记录，供下一轮检查重试。多 worker 或多实例下，索引写操作由 PG 咨询锁互斥，定时任务通过会话级咨询锁选出唯一执行者；管理页中的知识库操作也会互斥并自动同步状态。

## 许可证

本项目采用 [MIT 许可证](./LICENSE)。
