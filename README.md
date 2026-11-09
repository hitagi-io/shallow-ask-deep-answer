# 浅问深答

多用户私有知识库问答系统，使用 FastAPI、Vue 3 和 DeepAgents。每个账号可按项目组织文档并围绕资料提问，也可使用通用模式问答。PostgreSQL 保存账号、项目、会话与索引清单，Milvus 按账号和项目保存独立的文档向量集合。

- **双模式问答**：知识库模式检索内部资料并校验检索报告与引用文件；通用模式使用模型知识，可通过 Tavily 联网搜索。
- **流式会话**：保存历史、停止生成、断线重连，支持多个会话同时生成。
- **私有项目管理**：所有用户均可创建、重命名、删除自己的项目，上传、覆盖、删除 TXT、PDF、DOCX 文件，并检查、修复索引；资料、索引与对话按账号隔离。
- **混合检索与精排**：向量召回和 BM25 关键词召回各取 20 个片段，按分块去重并进行 RRF 融合，再用 rerank 返回最相关的 5 个片段。

知识库模式围绕所选项目的资料回答，并校验检索报告与引用来源；每个引用文件单独一行，文件名中的标点保持原样。通用模式适合一般问答、写作和公开信息查询。联网搜索结果保留各来源对应的片段，供回答引用。

## 快速开始

以下命令使用 Windows PowerShell，默认在项目根目录执行。

### 1. 准备环境

准备以下环境：

| 依赖 | 说明 |
| --- | --- |
| Python | 使用 3.13；当前依赖清单注明基于 Python 3.13.18 环境核对 |
| Node.js 与 npm | 使用 Node.js 22.12+；前端依赖版本由 `package-lock.json` 锁定 |
| PostgreSQL | 需提前创建数据库；服务账号需有建表、修改表权限 |
| Milvus | 可使用已有服务，或用仓库内的 Docker Compose 配置启动 |
| Docker 与 Docker Compose | 仅本地部署 Milvus 时需要；Windows 下使用 Linux 容器 |

启动 PostgreSQL 后创建数据库（已存在时跳过）：

```powershell
psql -U postgres -c "CREATE DATABASE shallow_ask_deep_answer;"
```

使用 [Compose 配置](./deploy/milvus/compose.yaml) 启动本地 Milvus 及其依赖：

```powershell
docker compose -f deploy/milvus/compose.yaml up -d --wait
```

Milvus 地址为 `http://localhost:19530`，健康检查地址为 `http://localhost:9092/healthz`。该配置用于本地开发，数据保存在 Docker 命名卷中；已有 Milvus 服务可跳过此步并修改 `config/vector_db.yaml` 中的连接地址。

后端启动时初始化业务表、索引清单与文件入库记录表，以及模型会话检查点结构，项目向量集合按需创建。首次部署请使用空数据库；旧版共享知识库与角色数据没有自动迁移流程。

### 2. 安装依赖与配置

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -s -m pip install -r requirements.txt
python -s -m pip check
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

编辑 `.env`：

| 参数 | 用途 |
| --- | --- |
| `PG_CONNECTION_STRING` | PostgreSQL 连接地址，数据库名与上一步一致 |
| `JWT_SECRET` | 必填，登录令牌签名密钥 |
| `OPENAI_API_KEY` | 聊天模型密钥，当前使用 DashScope 兼容接口 |
| `DASHSCOPE_API_KEY` | DashScope 原生文本嵌入与 rerank 精排密钥 |
| `TAVILY_API_KEY` | 可选，仅联网搜索需要 |

同一个具有相应权限的 DashScope 密钥可填入两项模型密钥。生成 `JWT_SECRET`：

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

启动前核对 `config/agent.yaml` 中的聊天模型和接口地址、`config/vector_db.yaml` 中的嵌入模型和 Milvus 连接，以及 `config/retrieval.yaml` 中的精排模型，确保所填密钥有对应模型的调用权限。`.env` 已被 Git 忽略，真实凭证只保存在本地。

### 3. 启动服务

启动后端：

```powershell
python -s api_server.py
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

两项存储探测通过时返回 HTTP 200 和 `status: ready`；失败或超时返回 HTTP 503。该接口不检查聊天模型、嵌入模型或 Tavily，问答与入库能力仍需通过实际操作确认。

### 4. 注册与使用

在页面注册并登录：用户名 2～50 字符；密码 10～128 字符，包含英文字母、数字和特殊符号。所有账号具有相同能力，不设置管理员角色。

1. 从首页侧边栏进入「知识库」，创建项目并上传文档。上传后自动尝试入库，确认文件显示「已入库」。
2. 返回对话页，在输入框下方点亮「知识库」按钮，选择项目后发送问题。新对话默认未选择项目。
3. 取消选中「知识库」按钮即使用通用模式，此时项目选择禁用。
4. 首页侧边栏底部的齿轮菜单提供「编辑信息」和「关于」。编辑信息可修改用户名（同时也是登录名）和密码；关于页面介绍两种模式、文档入库、对话操作和引用查看。

单文件上传上限默认为 50 MB，可通过 `MAX_UPLOAD_MB` 调整。TXT 需使用 UTF-8 编码；扫描版 PDF 需先进行 OCR，转换为可提取文本的文件。

## 配置

| 文件或目录 | 内容 |
| --- | --- |
| [.env.example](./.env.example) | 环境变量模板：连接、密钥、端口、并发、限流和超时 |
| [config/agent.yaml](./config/agent.yaml) | 聊天模型、接口地址、请求超时与重试、检索上限、搜索裁剪与会话压缩参数 |
| [config/vector_db.yaml](./config/vector_db.yaml) | Milvus、嵌入模型及请求限制、文档切块、BM25 分词与健康检查 |
| [config/retrieval.yaml](./config/retrieval.yaml) | 每路候选数、RRF 融合、rerank 模型与最终返回片段数 |
| [prompts/](./prompts/) | 知识库、检索、通用问答和会话压缩提示词 |
| [deploy/milvus/compose.yaml](./deploy/milvus/compose.yaml) | 本地 Milvus、etcd 与 MinIO 部署 |

修改配置或提示词后重启后端。需要查看接口文档时，在 `.env` 设置 `ENABLE_API_DOCS=true`，重启后访问 [http://localhost:8000/docs](http://localhost:8000/docs)。

知识库统一使用混合检索与精排，主要参数如下：

| 参数 | 默认值 | 中文说明 |
| --- | --- | --- |
| `top_k` | `5` | 精排后交给检索子代理的最大片段数 |
| `candidate_k` | `20` | 向量和 BM25 各自的候选数，须不小于 `top_k` |
| `rrf_k` | `60` | 倒数排名融合的平滑常数 |
| `rerank.model` | `qwen3.7-text-rerank` | 对融合后的完整候选并集进行相关性精排 |
| `embedding_model` | `qwen3.7-text-embedding-flash` | 文档和查询使用的稠密嵌入模型 |

同一分块在两路召回中只进入精排一次；最多 40 个候选全部送入精排，不先截取 Top5。一个文件的不同分块保留各自身份，引用继续使用原文件名。BM25 采用 Milvus 内置稀疏索引和 Jieba 分词，与稠密向量共用分块主键，上传、覆盖、删除沿用项目索引事务。

嵌入与精排使用原生 DashScope HTTP 接口：连接超时均为 5 秒，单次读写超时分别为 20 秒和 30 秒，最多尝试 3 次（含首次请求）。仅网络故障、超时、远端协议错误及 408/429/部分 5xx 响应会重试；鉴权、参数、本地协议和响应格式错误直接报错。嵌入按每批 20 个片段独立重试；Milvus SDK 负责请求级重试，入库失败会清理本次写入，不再重复执行整个入库事务。精排失败会走现有服务故障协议，停止该次知识库查询。

`config/agent.yaml` 中的主要检索与会话参数如下，表中为仓库当前配置值：

| 参数 | 配置值 | 用途 |
| --- | --- | --- |
| `kb_subagent_max_retrieval_calls` | `3` | 每个知识库子任务的检索次数上限，空结果和失败也计入 |
| `search_result_limit` | `5` | Tavily 请求和返回结果列表共用的数量上限；本项目要求为 1～20 |
| `search_content_limit` | `3000` | 每条搜索片段及综合摘要的最大字符数，超出时截断并标记 |
| `summary_trigger_messages` | `30` | 达到此消息数量时触发会话压缩 |
| `summary_trigger_tokens` | `20000` | 达到此估算 token 数时触发会话压缩，与消息数条件为“或”关系 |
| `summary_keep_messages` | `10` | 压缩时保留的近期消息数量；中间件可能为保留完整工具调用对而调整边界 |

以上六项参数均须为正整数，`search_result_limit` 不得超过 20，`summary_keep_messages` 须小于 `summary_trigger_messages`；不接受布尔值、字符串或小数。缺失或无效配置会在启动时直接报错。搜索工具描述与实际结果裁剪共用这些配置。

`llm_request_timeout_seconds` 与 `health_check_interval_hours` 须为有限正数，`llm_max_retries` 须为非负整数，`health_auto_repair_max_rounds` 须为正整数；这些 YAML 配置缺失或非法时直接报错。内部模型消息沿用 LangChain 的文本接口，不把非文本块强转为回答；检索与搜索响应异常不会被伪装成正常的空结果。

提示词位于 [prompts/](./prompts/)，模板变量由 [utils/prompt_loader.py](./utils/prompt_loader.py) 注入。修改时保留模板占位符与检索协议约定；会话压缩提示词中的 `{messages}` 由中间件在运行时填充。

## 日常使用

- **会话**：首次发送后固定问答模式与项目；切换模式或项目会打开或复用空白会话。刷新、切换会话或短暂断网不会取消生成，点击“停止生成”才会取消；服务进程重启后不会自动续跑。连接持续失败最多尝试 5 次后提示刷新重试，响应格式或页面处理错误会直接报告。生成中的会话不能再次发送或删除。
- **文档**：同名文件可覆盖上传，同内容文件仅在同一项目内共用索引。文件列表读取磁盘目录和已记录的入库状态，不查询 Milvus 或扫描文档正文；「已入库」表示入库记录有效，完整性另看检查报告。没有记录的旧文件显示「待确认」，定时维护或「补建索引」会补齐记录。文件未入库时可点击「补建索引」；在「索引维护」中使用「检查索引」查看健康报告，使用「修复索引」处理异常。
- **项目**：删除项目会清理项目文档、向量集合、索引清单、相关对话与模型上下文。项目内有回答正在生成时不能删除。

### 账号管理

从侧边栏底部齿轮菜单进入「编辑信息」：

- **修改用户名**：用户名同时用于登录，修改后使用新用户名登录，项目和对话保留。
- **修改密码**：需验证当前密码。修改后所有旧登录凭证失效，当前页面及同浏览器其他标签页退出；其他设备在下一次请求、页面重新获得焦点或可见页面的定期复核（每 15 秒）时返回登录页。已建立的回答流也会复核凭证版本并断开失效订阅。
- **注销账号**：依次阅读须知、验证当前密码、输入「注销 用户名」并最终确认。验证凭证仅在 5 分钟内有效，不能用于普通登录。最终确认会删除账号、项目源文件、向量与索引清单、对话及模型上下文，无法撤销。有生成中的回答或正在启动的对话请求时拒绝注销；外部存储清理失败时保留账号供重试，但部分数据可能已经删除。

## 部署与维护

### 数据存储与版本更新

源文件默认保存在仓库同级的 `<仓库目录名>-data/<账号 ID>/<项目 UUID>/`，仓库目录名为 `shallow-ask-deep-answer` 时即为 `shallow-ask-deep-answer-data/`。可用 `KNOWLEDGE_STORAGE_ROOT` 指定其他绝对路径，但必须位于源码目录之外。索引修复依赖源文件，备份时需同时考虑源文件、PostgreSQL 数据和 Milvus 数据。

启动时会为现有 `users` 表补充 `auth_version` 列，用于跨 worker 校验凭证版本，并创建 `indexed_source_files` 保存已知文件摘要、大小和修改时间；这项更新不代表支持旧版共享知识库的数据迁移。更新代码、配置或提示词后需重启后端。

修改分块参数、嵌入模型或相关解析依赖后需重建索引；修改解析逻辑或模型服务行为时，还需递增 `config/vector_db.yaml` 的 `index_version`。重启后点击「补建索引」，按需重建异常或过期内容。集合名由 `collection_prefix`、账号 ID 与项目 UUID 生成，每个项目独立存储。

本版本集合包含 `vector` 和 `sparse` 两个字段及 BM25 函数，集合结构与旧纯向量索引不同。部署到已有纯向量数据的环境前须迁移或重新创建集合；单纯补建分块无法改变旧集合结构。BM25 分词配置也属于集合结构，修改后须重新创建集合。索引版本指纹包含嵌入接口、双向量结构、BM25 分词参数和解析依赖版本；查询的候选数、精排参数，以及 `httpx`、`langchain-milvus` 客户端版本变化不触发重新 embedding。指纹保留当前锁定依赖的兼容种子值，本次调整不会使按现有依赖生成的索引失效。

### 健康检查与超时

后端启动后逐项目检查索引，此后默认每 6 小时检查并尝试修复，单轮最多修复 3 次；检查间隔和修复次数在 `config/vector_db.yaml` 中配置。检查会扫描源文件及向量文本，耗时随资料量增加，不同项目使用独立的索引操作锁。

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `READINESS_TIMEOUT_SECONDS` | `3` 秒 | 就绪探针总时限，包含进程启动及 PostgreSQL、Milvus 并行探测 |
| `HEALTH_REPORT_TTL_SECONDS` | `120` 秒 | 项目健康报告缓存有效期；过期后普通读取返回 `null`，不会自动重扫 |
| `HEALTH_CHECK_TIMEOUT_SECONDS` | `60` 秒 | 只读检查总时限，包含排队、进程启动、扫描和结果传输 |
| `HEALTH_CHECK_MAX_CONCURRENT` | `4` | 每个 API worker 的只读检查进程并发上限 |
| `VITE_KNOWLEDGE_REQUEST_TIMEOUT_MS` | `65000` 毫秒 | 前端文件列表和健康报告请求超时 |

就绪探针结果缓存 1 秒。页面加载、刷新和自动轮询只读取文件入库记录及已有健康报告，不启动完整扫描；没有缓存报告时返回 `null`，页面显示「待检查」，过期报告保留检查时间供参考。「检查索引」通过 `GET /api/projects/{project_id}/index/health?refresh=1` 执行完整扫描，同一 API worker 内、同一项目的并发检查共享任务。检查超时返回 HTTP 503，超时或全部等待客户端断开时结束并回收检查进程。上述只读检查限制不适用于手动补建、修复或定时自动修复。

前端超时应略大于后端只读检查时限，需写入 `frontend-chat/.env.local` 或前端构建环境，根目录 `.env` 对此参数不生效。修改后重新启动或构建前端。自动轮询仅重试报告读取，不反复触发深度检查；切换项目、隐藏知识库页面或退出时取消旧请求。

显式配置不会被静默替换：前端请求超时须为 1～2147483647 的整数毫秒；后端就绪与只读检查超时须为不小于 0.5 秒的有限数值，并发上限须为正整数。生成保存间隔须为不小于 0.1 秒的有限数值，失联判定时间须不小于保存间隔的 3 倍。`LOG_LEVEL` 支持 `DEBUG`、`INFO`、`WARNING`、`ERROR`、`CRITICAL`。

### 前端发布

```powershell
npm --prefix frontend-chat ci
npm --prefix frontend-chat run build
```

托管构建产物 `frontend-chat/dist/`，将同域 `/api` 请求转发到后端，并关闭 SSE 响应缓冲。Vite 的开发代理仅在开发服务器中生效。后端监听地址、worker 数、并发、限流和超时参数见 [.env.example](./.env.example)。

### 常见问题

| 现象 | 排查方向 |
| --- | --- |
| 后端无法启动 | 检查 PostgreSQL 是否运行、数据库是否创建、账号是否有建表和修改表权限，以及 `JWT_SECRET` 是否已填写 |
| `/api/health/ready` 返回 503 | 查看响应中的 `checks`，检查存储连接；若进程启动或依赖加载较慢，可适当增大 `READINESS_TIMEOUT_SECONDS` |
| 就绪检查通过，但问答或入库失败 | 检查模型名称、接口地址、密钥权限及后端日志；就绪检查不验证外部模型服务 |
| 文档显示「待入库」或索引异常 | 确认文件可提取文本及嵌入服务可用，尝试「补建索引」；在「索引维护」中检查和修复 |
| 文件列表或健康报告超时 | 检查资料量和存储响应，按需调整前后端检查时限及扫描并发 |
| 发布后 API 请求失败或回答无法逐步显示 | 检查同域 `/api` 反向代理、SSE 响应缓冲及代理超时配置 |

## 开发检查

在项目根目录执行：

```powershell
python -m compileall -q api_server.py auth.py accounts.py chat_agent.py rag utils
python -s -m unittest discover -s tests -v
git diff --check
npm --prefix frontend-chat run build
```

Python 编译检查仅验证语法，前端构建包含 TypeScript 类型检查。业务改动还需联调注册登录、改密与注销、项目管理、跨账号访问隔离、上传、双模式问答、停止生成和索引修复；提示词改动需核对模板变量、工具及协议契约，检查引用、无结果和检索失败场景。

离线回归通过模拟外部服务检查错误传递、重试范围、消息与检索协议，不连接真实数据库或模型。错误处理遵循明确边界：外部输入、权限、来源及索引完整性仍须校验；内部必需字段直接按契约读取；异常须向调用方或日志暴露，不能用空结果、默认成功或无依据的兼容分支掩盖。临时故障恢复、取消清理和上传回滚保留各自明确的触发条件。

这一约定参考了 [OpenAI Codex Prompting Guide 的错误处理建议](https://developers.openai.com/cookbook/examples/gpt-5/codex_prompting_guide)，以及[社区对 Codex 过度防御与复杂化的直接讨论](https://www.reddit.com/r/codex/comments/1qa3s7y/any_advice_for_codex_52_thinking_medium_to_calm/)。社区反馈用于识别排查线索，具体修复以本项目调用链与回归结果为依据。

## 许可证

[MIT](./LICENSE)
