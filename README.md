# 浅问深答

基于 FastAPI、Vue 3 和 DeepAgents 的多用户私有知识库问答系统，使用 PostgreSQL 保存业务数据、Milvus 存储文档向量。

## 功能

- **双模式问答**：知识库检索与通用问答，通用模式支持 Tavily 联网搜索。
- **文档入库**：支持 TXT、Markdown、PDF、DOCX、XLSX、PPTX 和图片，扫描文档支持本地 OCR。
- **检索与引用**：向量与 BM25 混合检索、rerank 精排，点击知识库来源可查看提取文本、上下文及高亮证据。
- **私有工作空间**：项目、资料和会话按账号隔离，支持流式回答、历史记录、停止生成和索引维护。

## 快速开始

准备 Python 3.13、[Node.js 22.12+](https://vite.dev/guide/#scaffolding-your-first-vite-project)、PostgreSQL 和 Milvus。以下命令使用 Windows PowerShell，在项目根目录执行。

### 1. 准备存储与依赖

启动 PostgreSQL 后创建数据库；本地 Milvus 使用 Docker Compose 启动（需 Linux 容器）：

```powershell
psql -U postgres -c "CREATE DATABASE shallow_ask_deep_answer;"
docker compose -f deploy/milvus/compose.yaml up -d --wait
```

已有数据库或 Milvus 服务可跳过对应命令；使用已有 Milvus 时，在 `config/vector_db.yaml` 设置 `milvus_uri`。首次部署使用空数据库，后端自动建表，数据库账号需有建表权限；不提供旧版业务数据迁移。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -s -m pip install -r requirements.txt
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

### 2. 配置环境变量

编辑 `.env`，完整参数见 [.env.example](./.env.example)：

| 变量 | 用途 |
| --- | --- |
| `PG_CONNECTION_STRING` | PostgreSQL 连接地址 |
| `JWT_SECRET` | 登录令牌签名密钥，必填随机值 |
| `OPENAI_API_KEY` | 聊天模型密钥，默认使用 DashScope 兼容接口 |
| `DASHSCOPE_API_KEY` | DashScope 嵌入与精排密钥 |
| `TAVILY_API_KEY` | 可选，联网搜索密钥 |

同一个具有对应模型权限的 DashScope 密钥可填入两项模型密钥。生成 `JWT_SECRET`：

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

### 3. 启动服务

启动后端：

```powershell
python -s api_server.py
```

另开终端，在项目根目录启动前端：

```powershell
npm --prefix frontend-chat ci
npm --prefix frontend-chat run dev
```

访问 [http://localhost:5173](http://localhost:5173)。后端默认端口为 `8000`；修改时同步调整 [Vite 代理](./frontend-chat/vite.config.ts)。

## 使用

1. 注册并登录，在「知识库」中创建项目、上传文档，等待文件显示「已入库」。
2. 在对话输入区启用「知识库」，选择项目后提问；关闭该按钮则使用通用问答。
3. 点击知识库回答末尾的来源查看提取文本；文档未入库或索引异常时，可在知识库页面展开“索引维护”，点击“修复与补建”统一处理未入库文件、异常索引和残留分块，也可单独点击“检查索引”查看状态。

上传或入库期间，该项目暂不能发送新问题。单文件默认上限 50 MB；TXT、Markdown 使用 UTF-8，Office 文件需为未加密的新格式。OCR 可能存在识别误差，关键内容请核对原文。

上传进度在同一条目中显示，绿色条随实际工作完成向右增长，完成后隐藏。百分比按传输、检查、解析、分块、向量化、写入、校验和收尾八个阶段等权计算，阶段内使用已处理数量，不代表剩余耗时。等待外部服务时进度保持不变。更新此功能后需重启后端，启动时会为上传任务补充进度字段；更新前已经运行的任务没有详细阶段记录。

“最近上传”显示当前项目最近 7 条上传任务，并显示全部尚未完成的任务，按上传时间由新到旧排列。成功上传的文件随后被删除时，历史记录显示“文件已删除”。

“项目资料”默认按最近一次上传时间由新到旧排列。状态选择框右侧的图标可切换上传时间正序／倒序、文件名升序／降序、未入库优先或已入库优先；同一入库状态内按上传时间由新到旧排列。按上传时间排序时，没有上传记录的资料排在最后，并按文件名排序。

每页显示 10 条，用左右箭头翻页，或输入页码后按回车跳转，翻页会替换当前列表。搜索、状态筛选和排序均作用于全部资料，改变条件后回到第 1 页；刷新列表保留当前页，末页资料删空后返回上一页。项目总数和问答可用状态不受当前页影响。“索引维护”位于“项目资料”上方。

刷新浏览器时会恢复知识库页面及当前项目；项目已删除时返回项目选择页。

## 配置与维护

| 位置 | 内容 |
| --- | --- |
| [.env.example](./.env.example) | 连接、密钥、端口、并发与超时 |
| [config/agent.yaml](./config/agent.yaml) | 聊天模型、接口、搜索与会话参数 |
| [config/vector_db.yaml](./config/vector_db.yaml) | Milvus、嵌入模型、文档解析与切块 |
| [config/retrieval.yaml](./config/retrieval.yaml) | 混合检索与精排 |
| [prompts/](./prompts/) | 问答、检索与会话压缩提示词 |

- 修改后端配置或提示词后重启后端；修改嵌入模型、切块或解析参数后需重建索引，修改解析代码还需递增 `config/vector_db.yaml` 的 `index_version`。前端环境变量需写入 `frontend-chat/.env.local` 或构建环境，修改后重启或重新构建前端。
- 每个 API worker 的向量请求共享 `embedding_concurrency` 个名额（默认 16），其中 `embedding_query_reserved_slots` 个名额（默认 4）预留给提问向量化；所有项目的入库合计最多占 12 个，提问可使用全部 16 个。参数位于 `config/vector_db.yaml`，只影响向量请求，修改后重启后端即可，无需重建索引。
- 源文件默认位于仓库同级的 `<仓库目录名>-data/<账号 ID>/<项目 UUID>/`，可通过 `KNOWLEDGE_STORAGE_ROOT` 指定源码目录之外的绝对路径作为存储根目录。备份需包含源文件、PostgreSQL 和 Milvus 数据。
- [就绪检查](http://localhost:8000/api/health/ready)仅检测 PostgreSQL 和 Milvus，不验证模型服务。设置 `ENABLE_API_DOCS=true` 并重启后，可访问 [API 文档](http://localhost:8000/docs)。
- 前端发布：运行 `npm --prefix frontend-chat run build`，托管 `frontend-chat/dist/`，将同域 `/api` 代理到后端，并关闭 SSE 响应缓冲。

## 开发检查

前端的公共字号、控件尺寸、圆角和间距在 `frontend-chat/src/style.css` 中维护；页面标题共用 `PageHeader`，按钮、表单、菜单和弹窗操作区复用公共样式。页面内只保留布局及专属状态的样式，调整时同时检查桌面和窄屏效果。

```powershell
python -s -m pip check
python -m compileall -q api_server.py auth.py accounts.py chat_agent.py rag utils
npm --prefix frontend-chat run build
```

后端入口为 `api_server.py`，问答逻辑位于 `chat_agent.py` 与 `rag/`；辅助模块位于 `utils/`，前端位于 `frontend-chat/src/`。

## 许可证

[MIT](./LICENSE)
