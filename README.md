# 浅问深答

面向内部资料的 RAG 问答系统，支持知识库问答、通用问答、流式对话和文档管理。

- **知识库问答**：检索内部资料，校验回答依据与引用来源。
- **通用问答**：使用模型知识回答，可通过 Tavily 搜索公开信息。
- **会话管理**：保存聊天历史，支持停止生成和切换问答模式。
- **知识库管理**：管理员可上传、覆盖、删除 TXT、PDF、DOCX 文件，检查并修复索引。

后端使用 FastAPI，前端使用 Vue 3；PostgreSQL 保存账户、会话和索引清单，Milvus 保存文档向量。

## 快速开始

以下命令使用 Windows PowerShell，默认在项目根目录执行。

### 1. 准备环境

需要 Python 3.12、Node.js 与 npm，以及已启动的 PostgreSQL 和 Milvus。Python 依赖及项目环境版本见 [requirements.txt](./requirements.txt)，前端依赖见 [package.json](./frontend-chat/package.json)。数据库与向量库需自行部署。

创建数据库（已存在时跳过）：

```powershell
psql -U postgres -c "CREATE DATABASE deepagent_rag;"
```

应用启动时会初始化业务表、`index_manifests` 索引清单和检查点结构，数据库账户需具备相应的建表与修改表权限。

### 2. 安装依赖与配置

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

编辑 `.env`，填写以下参数：

| 参数 | 用途 |
| --- | --- |
| `PG_CONNECTION_STRING` | PostgreSQL 连接地址 |
| `JWT_SECRET` | 登录令牌签名密钥，未设置时服务拒绝启动 |
| `OPENAI_API_KEY` | 聊天模型密钥；当前使用 DashScope 兼容接口 |
| `DASHSCOPE_API_KEY` | 文本嵌入服务密钥 |
| `TAVILY_API_KEY` | 联网搜索密钥，仅使用搜索时需要 |

可用以下命令生成 `JWT_SECRET`，将结果填入 `.env`：

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

启动前核对 `config/` 中的模型名称、接口地址和 Milvus 连接。`.env` 已被 Git 忽略，真实凭证只保存在本地配置中。

### 3. 启动服务

启动后端：

```powershell
python api_server.py
```

在另一个终端，从项目根目录启动前端：

```powershell
cd frontend-chat
npm ci
npm run dev
```

打开 [http://localhost:5173](http://localhost:5173)。后端默认监听 `0.0.0.0:8000`，前端开发服务器通过 `/api` 转发请求；修改后端端口时需同步调整 [vite.config.ts](./frontend-chat/vite.config.ts)。

检查 PostgreSQL 与 Milvus 是否就绪：

```powershell
Invoke-RestMethod http://localhost:8000/api/health/ready
```

该探针不检查模型服务；就绪后仍无法问答时，检查模型配置、凭证和后端日志。

### 4. 注册与使用

在页面注册并登录。密码至少 10 个字符，包含英文字母、数字和特殊符号。新账户默认为普通用户；管理知识库需在数据库中授予管理员权限：

```sql
UPDATE users SET role = 'admin' WHERE username = 'your-username';
```

权限默认缓存 30 秒，修改后等待缓存过期并重新登录。管理员上传文档后会自动尝试入库，确认成功即可进行知识库问答。

## 配置

| 文件或目录 | 内容 |
| --- | --- |
| [.env.example](./.env.example) | 环境变量模板：连接、密钥、端口、并发、限流和超时 |
| [config/agent.yaml](./config/agent.yaml) | 聊天模型、接口地址、请求超时与重试 |
| [config/vector_db.yaml](./config/vector_db.yaml) | Milvus、嵌入模型、文档切块、检索与健康检查 |
| [prompts/](./prompts/) | 知识库、检索、通用问答和会话压缩提示词 |

修改配置或提示词后重启后端。需要查看接口文档时，在 `.env` 设置 `ENABLE_API_DOCS=true`，重启后访问 [http://localhost:8000/docs](http://localhost:8000/docs)。

### 提示词维护与评估

四份提示词分别负责知识库任务委派、检索总结、通用问答和会话压缩。采用 Anthropic 的[提示工程最佳实践](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices)与[上下文工程建议](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)：明确成功标准，分隔指令与资料，给出代表性示例，按需检索并及时停止。当前配置使用 DeepSeek，以上采用通用方法，效果需在实际模型上评估。

修改时保持提示词、工具描述和 `utils/retrieval_protocol.py` / `utils/answer_guard.py` 的契约一致：`${...}` 由代码常量替换，会话摘要的 `{messages}` 由中间件填入。报告的 `sources` 是召回文件集合，引用还需结合 `answer` 和 `cited_sources` 判断。提示词中的最多 3 次查询是模型行为约束，不是运行时代码强制上限。

用固定问题、相同资料和模型配置比较修改前后的表现，至少覆盖以下场景；记录结论是否忠实、引用是否对应、工具次数与耗时，重复运行以观察波动。模板渲染与协议检查通过不代表模型回答质量已提升。

| 场景 | 验收标准 |
| --- | --- |
| 知识库目标不明或纯翻译请求 | 只调用澄清工具，本轮不检索、不输出事实答案 |
| 明确的单一知识库问题 | 只委派必要任务，结论保留原文条件，引用实际支持的文件 |
| 多对象比较且部分检索失败 | 收齐报告后回答有据部分，明确失败对象，不补造比较结论 |
| 任务报告缺失或不可解析 | 使用依据校验失败文案，不能被其他成功报告掩盖 |
| 文档冲突、片段不相关或夹带指令 | 保留冲突和缺口；资料中的指令不改变任务或工具状态 |
| 通用翻译、实时查询、用户禁止联网 | 分别直接交付、按需搜索、遵从不联网并说明实时信息缺口 |
| 搜索仅有摘要或搜索失败 | 不声称阅读全文、不虚构网页归属和链接，不用记忆补实时值 |
| 长会话含目标更正、未执行计划和旧结论 | 摘要保留最新目标、执行状态及来源属性，旧结论不充当新证据 |

## 使用与维护

- 每个会话在首次发送后固定为知识库或通用模式，使用 `user_id:conv_id` 保存模型上下文。空白会话可直接切换模式；已有消息时，切换模式会打开或复用唯一的空白会话。选择历史会话会恢复其固定模式。
- 回答由后台任务生成，默认每 0.5 秒保存进度。刷新、切换会话或短暂断网后会重新订阅已有回答；点击“停止生成”才会发送取消请求。生成中仍可新建、切换、重命名会话，其他会话可以独立提问；同一会话生成期间不能重复发送或删除。
- 默认不限制每个用户并行生成的会话数（`MAX_CONCURRENT_CHAT_PER_USER=0`）；设为正数可恢复上限。请求频率限制和生成超时仍生效。
- 服务进程重启不会自动重跑模型；已保存内容会保留，失联任务在心跳超时后读取时标记为中断。后端启动时会自动补充生成阶段字段，更新前后端后需重启服务。
- 上传文件会自动入库；手动放入知识库目录的文件需点击“构建”。构建会补建未入库、分块损坏或版本过期的内容，不清空整个向量库。
- 文件显示已入库但检索异常时，先检查索引健康报告，再执行修复。同内容文件按内容去重。
- 默认源文件目录为 `knowledge/`（被 Git 忽略）。PostgreSQL 的 `index_manifests` 按 Milvus 地址、集合和内容 SHA-256 保存来源、索引版本、写入代次、预期分块数、按序分块哈希及构建状态；不提供旧格式迁移。
- 当前默认集合为 `knowledge_chunks_v1`，使用显式主键和动态分块元数据。更新后重启后端并点击“构建”写入该集合；不沿用旧 `milvus` 集合的结构或记录。
- 健康检查重新读取文件字节，并逐块核对 Milvus 的文本哈希、序号、来源、版本和代次，可发现部分缺块、等量重复、内容陈旧及中断写入。文件列表使用相同校验标准。检查需要扫描源文件和分块文本，耗时随资料量增加。
- 索引版本自动包含分块参数、嵌入模型及解析依赖版本；修改解析规则或服务端模型行为时递增 `config/vector_db.yaml` 的 `index_version`。修复从源文件重建异常内容，不以残留块数补造清单；失败保留清单以供重试。同内容副本共用索引，删除来源文件时会转移到存活副本。
- 生产环境需单独托管 `frontend-chat/dist/`，将 `/api` 转发到后端，并关闭 SSE 响应缓冲。

## 开发检查

在项目根目录执行：

```powershell
python -m compileall -q api_server.py auth.py chat_agent.py rag utils
git diff --check
npm --prefix frontend-chat run build
```

前端构建包含 TypeScript 类型检查。涉及业务行为的改动，还需联调登录、上传、双模式问答、停止生成和索引修复。

## 许可证

[MIT](./LICENSE)
