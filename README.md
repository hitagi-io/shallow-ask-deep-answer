# 浅问深答

基于 FastAPI、Vue 3 和 DeepAgents 的多用户私有知识库问答系统，使用 PostgreSQL 保存业务数据、Milvus 存储文档向量。

## 功能

- **双模式问答**：知识库检索与通用问答，通用模式支持 Tavily 联网搜索。
- **文档入库**：Any2Markdown 统一转换 TXT、Markdown、PDF、DOCX、XLSX、PPTX 和图片。扫描页与 Office 内嵌图片使用本地 Xiaomi-OCR-0 识别，提取结果保留页码、幻灯片对象、工作表及单元格位置。
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

已有数据库或 Milvus 服务可跳过对应命令；使用已有 Milvus 时，在 `config/vector_db.yaml` 设置 `milvus_uri`。首次部署使用空数据库，后端自动建表；已有当前项目结构会执行增量迁移，数据库账号需有建表与修改表结构的权限。

文档解析使用独立的 Linux 容器，需要 Docker 能访问 NVIDIA GPU。首次启动会构建 Any2Markdown，下载 vLLM 镜像和小米模型权重，并为解码准备 CUDA Graph。权重及 CUDA、Triton 编译缓存保存在 Docker 命名卷中。

```powershell
docker compose -f deploy/documents/compose.yaml up -d --build --wait
```

模型开始监听后，在 PowerShell 运行 `Invoke-RestMethod http://127.0.0.1:8088/models/default/status` 确认模型可用。Any2Markdown 在本机 8088 端口为应用提供转换服务。服务连接设置见 [.env.example](./.env.example)，版本及部署配置见 [文档解析 Compose](./deploy/documents/compose.yaml)。更换 GPU 或主机时见[在其他机器上部署 OCR](#在其他机器上部署-ocr)。

小米模型以 BF16 运行。生成表格时，vLLM 重放单请求解码的 CUDA Graph，并从当前请求中已出现的文本提出最多 3 个后续 token，由同一个模型一次验证。重复出现的表格标记也能走这条路径。初始化进度可用 `docker compose -f deploy/documents/compose.yaml logs -f xiaomi-ocr` 查看。

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

## 在其他机器上部署 OCR

**后端和解析服务同机，且 Docker 能访问兼容的 NVIDIA GPU 时，先沿用默认配置。** 在新机项目根目录执行上面的 Compose 启动命令并检查模型状态。新机首次启动会下载权重、准备本机 GPU 的编译缓存和 CUDA Graph。默认配置已在 RTX 5070 Laptop（8 GB）上验证，针对逐张识别表格图片的场景做了加速。

| 新机情况 | 操作 |
| --- | --- |
| NVIDIA GPU，按单请求解析 | 沿用 `--max-num-seqs 1`、`--gpu-memory-utilization 0.78`、解码 CUDA Graph 和最多 3 个候选 token 的 GPU n-gram 配置。换显卡后的速度以新机实测为准。 |
| 模型启动时报显存不足 | 用 `docker compose -f deploy/documents/compose.yaml logs xiaomi-ocr` 查看出错阶段。若日志明确提示 KV 缓存容纳不了 8192 个 token，可调低 Compose 中的 `--max-model-len` 并重新启动。图像输入和表格输出共用此长度，调整后用最长的扫描表核对末行。若加载权重时已耗尽显存，先释放其他进程占用的显存或换用显存更大的 GPU。 |
| 新机只有 CPU、AMD 或 Apple GPU | 当前镜像和加速参数面向 NVIDIA CUDA。可把后端和解析服务一起部署到兼容的 NVIDIA 主机，在新机通过前端访问。要在新机本地推理，需要另行适配推理后端。 |
| 后端和解析服务分属两台主机 | Compose 将 8088 绑定到回环地址，应用也会校验 `ANY2MARKDOWN_URL` 是否指向本机。跨主机直连需要同时适配服务监听和应用地址校验。 |

## 使用

1. 注册并登录，在「知识库」中创建项目、上传文档，等待文件显示「已入库」。
2. 在对话输入区启用「知识库」，选择项目后提问；关闭该按钮则使用通用问答。
3. 点击知识库回答末尾的来源查看提取文本；文档未入库或索引异常时，可在知识库页面展开“索引维护”，点击“修复与补建”统一处理未入库文件、异常索引和残留分块，也可单独点击“检查索引”查看状态。

上传或入库期间，该项目暂不能发送新问题。上传默认每个文件限 50 MiB，每次请求限 32 个文件、共 101 MiB（含表单开销）。网页会自动逐个发送文件。TXT、Markdown 使用 UTF-8，Office 文件需为未加密的新格式。OCR 可能存在识别误差，关键内容请核对原文。

PDF 按页选择文字提取或 OCR，带文字层的页面还会检查内嵌图片。Office 文档按原生对象补充转换结果中的嵌图、公式和定位信息。表格按行保存，保留表头上下文、列号及空值。每条原文引用都带有入库时的解析快照标识。点击引用时，系统从当前项目的 PostgreSQL 快照中读取对应文字和上下文，并核对原文件摘要。

XLSX 在独立子进程中校验、转换和恢复单元格位置。默认每本限 25 万个单元格、400 万个提取字符和 180 秒，每个 API 工作进程同时处理一本。1 GiB 内存上限适用于该子进程，Any2Markdown 容器和模型服务独立使用内存与显存。公式缓存、重复的首行上下文和图片 OCR 的文字均计入输出限额。超限时可拆分工作簿，或在 `config/vector_db.yaml` 中调整限额。

上传进度在同一条目中显示，绿色条随实际工作完成向右增长，完成后隐藏。百分比按传输、检查、解析、分块、向量化、写入、校验和收尾八个阶段等权计算，阶段内使用已处理数量，不代表剩余耗时。等待外部服务时进度保持不变。更新此功能后需重启后端，启动时会为上传任务补充进度字段；更新前已经运行的任务没有详细阶段记录。

“最近上传”显示当前项目最近 7 条上传任务，并显示全部尚未完成或仍需恢复的任务，按上传时间由新到旧排列。成功上传的文件随后被删除时，历史记录显示“文件已删除”。

“项目资料”默认按最近一次上传时间由新到旧排列。状态选择框右侧的图标可切换上传时间正序／倒序、文件名升序／降序、未入库优先或已入库优先；同一入库状态内按上传时间由新到旧排列。按上传时间排序时，没有上传记录的资料排在最后，并按文件名排序。

每页显示 10 条，用左右箭头翻页，或输入页码后按回车跳转，翻页会替换当前列表。搜索、状态筛选和排序均作用于全部资料，改变条件后回到第 1 页；刷新列表保留当前页，末页资料删空后返回上一页。项目总数和问答可用状态不受当前页影响。“索引维护”位于“项目资料”上方。

刷新浏览器时会恢复知识库页面及当前项目；项目已删除时返回项目选择页。

如果项目资料目录不可读，或一次扫描中所有已入库来源及其同内容副本都消失，手动和定时修复会保留索引与原文快照。恢复正确的资料目录后可再次检查。需要清空项目时，可通过文件或项目删除接口明确提交删除。

删除项目会先将历史对话设为只读，确认注销会先停用账号。数据库保存清理进度，服务重启后会自动继续。删除接口返回 HTTP 200 表示清理已完成，HTTP 202 表示请求已持久化并等待后台清理。单个文件在清理期间从资料列表隐藏，待删除记录清除后可重新上传同名文件。

索引按账号和项目登记 Milvus 位置。升级时会从旧索引的来源路径核对归属，并清除已硬删除项目遗留的 PostgreSQL 快照。如果升级前更换过 Milvus 服务或集合前缀，清理日志提示无法核实历史位置时，可在 `config/vector_db.yaml` 的 `legacy_milvus_locations` 中填写当时的 `milvus_uri` 和 `collection_prefix`，重启后自动重试。服务会核对旧命名空间哈希，再清理对应集合。

回答下方的“执行过程”单独显示总耗时，按阶段分组展示模型、检索、校验和保存步骤。步骤以层级编号表示所属阶段，同级按开始时间排列；并行时直接标明对应的步骤编号。点击阶段可展开或收起子步骤，整个弹窗只保留一层滚动。阶段耗时已包含子步骤，不应重复相加；记录缺失时不推测总耗时。可复制问题编号排查故障。侧边栏底部的账号设置菜单中，“操作记录”位于“编辑信息”上方，点击后在独立页面查看本人登录、账号与项目变更、文档操作、生成和索引恢复的结果。

## 执行记录与失败恢复

- 回答结果区分“已回答、等待澄清、无足够资料、回答未通过校验、依赖服务故障”。后两种明确标记为失败；断流、超时或进程失联保留已生成内容和错误原因，不再把固定故障提示当作成功回答。
- 每次生成关联 `generation_id → attempt_id → trace_id`。Trace 记录关键步骤、模型与 token 用量（供应方提供时）、配置与提示词摘要、证据版本和引用编号，不保存问题、答案、检索正文或凭证。`GET /api/generations/{generation_id}/trace` 仅允许读取本人的任务。
- 审计记录采用追加写入；数据库内的关键成功事件与业务共用事务，文件等跨存储操作先记录意图，上传另有持久化阶段记录。`GET /api/audit-events` 按本人账号分页读取。Trace 保留 14 天，审计保留 90 天并分批清理；注销账号会删除 Trace，审计仅保留编号、操作与结果等元数据直至到期。Trace 写入使用有界队列，丢弃或存储故障会明确告警；关键审计写入失败会阻止尚未执行的操作。
- 相同 `request_id` 只允许重放相同会话、项目、模式、问题及重试来源；内容不一致返回 409。SSE 重连继续订阅已有任务，每次刷新用一条查询核对凭证、读取进度并检查心跳，发现失联才执行恢复。每 15 秒也会扫描失联任务。“重新提问”创建新任务并通过 `retry_of` 关联原尝试。每次尝试从已发布的会话检查点开始，在内存中运行；服务端按 `attempt_id` 核验写入资格，在同一事务中发布检查点、回答和终态。取消或失联的旧线程无法覆盖重试结果。知识库在项目读锁内取得候选文本，释放读锁后执行精排。
- 覆盖上传在移动文件前保存新旧哈希、备份位置和目标索引代次。工作进程退出后，接管者先核对发布清单：确认未发布才回滚原文件并清理本次向量；确认已发布则保留新文件并继续清理。发布回执丢失时重新查询数据库，无法确认或完整性校验失败时保留恢复材料并报告失败，阻止该项目继续问答。可在“修复与补建”重新核对；不能自动核实的损坏仍需检查存储。首次上传未发布时保留源文件供补建。
- 启动时在现有数据库初始化锁内执行版本化增量迁移，保留历史消息，旧记录的结果与 Trace 保持空值。升级前运行的上传没有恢复日志，无法自动推断备份关系。此次不从 LangGraph checkpoint 自动续跑失败任务；人工重试会重新执行问题。

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
- 上传代理应流式转发请求体，并与后端的 `MAX_UPLOAD_REQUEST_MB` 使用相同的总大小上限。Nginx 默认配置下，可在上传接口的 `location` 中设置 `client_max_body_size 101m;`、`proxy_request_buffering off;` 和 `proxy_http_version 1.1;`，以便后端在接收文件前完成鉴权。

## 会话与账号配额

每次登录都会获得独立的会话编号。退出登录时，服务端撤销当前会话，撤销记录保留至令牌到期。修改登录名需要验证当前密码，并沿用当前会话编号和到期时间。登录和注册入口先按来源和 worker 总量限流，跨 worker 的数据库配额继续生效，重复限流按操作汇总审计。

改名、改密、注销验证和最终注销共用 60 秒的滚动窗口。每个登录会话最多放行 5 次，每个账号合计最多 10 次。会话超限的请求不会继续扣减账号额度。各 worker 在查询数据库前先按已签名的会话标识限流，并通过 `ACCOUNT_CHANGE_WORKER_RATE_LIMIT` 限制每个接口每分钟放行的总量（默认 120 次）。超限事件按操作汇总写入审计。改密成功后，所有已登录的会话均需重新登录。

每个账号默认最多同时打开 4 条聊天订阅。知识库可存储 1 GiB 源文件，每日可接收 250 MiB 上传，同时最多有 8 个正在接收、排队或执行的上传任务。服务器在读取请求正文前预留账号的字节数和任务名额，断线或接收超时会释放预留额度。已有源文件、暂存文件和覆盖备份都占用存储空间，删除资料会释放空间。

上传、补建和修复共享每日 1000 万个分块字符的向量化额度，复用已完成的索引时免计。每日用量从升级后开始累计，按 UTC 零点重置。以上默认值可在 `.env.example` 中查看。回答中的站外图片以链接显示，点击后由浏览器打开。

## 开发检查

前端的公共字号、控件尺寸、圆角和间距在 `frontend-chat/src/style.css` 中维护；页面标题共用 `PageHeader`，按钮、表单、菜单和弹窗操作区复用公共样式。页面内只保留布局及专属状态的样式，调整时同时检查桌面和窄屏效果。

```powershell
python -s -m pip check
python -m compileall -q api_server.py auth.py accounts.py chat_agent.py rag utils
npm --prefix frontend-chat run build
```

后端入口为 `api_server.py`，问答逻辑位于 `chat_agent.py` 与 `rag/`；辅助模块位于 `utils/`，前端位于 `frontend-chat/src/`。

## 许可证

本项目自有代码采用 [MIT 许可证](./LICENSE)。

文档解析依赖以下第三方组件。Docker Compose 在部署时构建 Any2Markdown、拉取 vLLM 镜像并下载模型权重，具体版本见[文档解析部署配置](./deploy/documents/compose.yaml)。

| 组件 | 在本项目中的用途 | 仓库内的许可证 | 上游来源 |
| --- | --- | --- | --- |
| Any2Markdown | 通过本机 HTTP 服务将文档转换为 Markdown | [AGPL-3.0 全文](./licenses/AGPL-3.0.txt) | [该版本的 LICENSE](https://github.com/SeerRay-Lab/Any2Markdown/blob/54bf7f0a27d87ef43b712b948c97ec23bfa81c59/LICENSE) |
| Xiaomi-OCR-0 模型权重 | 识别扫描页和内嵌图片 | [Apache-2.0 全文](./licenses/Apache-2.0.txt) · [模型许可声明摘录](./licenses/Xiaomi-OCR-0.md) | [该版本的模型卡](https://huggingface.co/xiaomi-research/Xiaomi-OCR-0/blob/e4d1c4a6804bd9ef342b93d705a73af003e2ef4e/README.md) |
| vLLM | 在本地 GPU 上运行 Xiaomi-OCR-0 | [Apache-2.0 全文](./licenses/Apache-2.0.txt) | [v0.33.0 的 LICENSE](https://github.com/vllm-project/vllm/blob/v0.33.0/LICENSE) |
