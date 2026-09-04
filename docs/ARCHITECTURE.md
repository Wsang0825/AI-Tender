# AI-Tender Architecture

本文档描述当前仓库中已经实现的结构和边界。它不把登记的来源、预留的 Provider 或未来设想描述成已经接入的能力。

## System boundary

AI-Tender 是一个从源码 checkout 运行的本地 Python 应用：

- `config/` 提供来源、地区、行业、搜索 Profile、Provider 和概念配置；
- `src/tender_ai/` 提供抓取、Discovery、解析、Evidence、状态、存储、CLI 和 Web 实现；
- SQLite 保存结构化数据和运行状态；
- 大正文、Snapshot、解析文本和附件保存在文件系统，不塞进 SQLite；
- Codex 是可选的上层编排与推理协作者，Python 主路径不调用模型 API。

当前应用是按需运行模式，没有后台 scheduler。用户必须显式启动 CLI 搜索、验证、抽取或 Web 表单操作。

## End-to-end data flow

```text
SearchRequest
    │
    ├── Search Profile / Industry / Region / Concept configuration
    │
    ▼
Source plan
    │
    ├── CrawlRunner       已配置的公开来源
    └── DiscoveryRunner   公开搜索 Provider 和候选发现
    │
    ▼
Candidate pool
    │
    ├── Candidate identity / enrichment / official trace
    ├── HTTP response and attachment download
    └── Snapshot / DocumentParse
    │
    ▼
Deterministic extraction
    │
    ├── normalized project fields
    ├── Evidence records
    ├── Timeline events
    └── Field conflicts / Codex Review
    │
    ▼
Identity resolution and deduplication
    │
    ▼
Status Engine
    │
    ├── OPEN
    ├── UNKNOWN
    └── CLOSED
    │
    ▼
Search Session outputs / Web browser / Excel export
```

## Source discovery and adapters

### Source registry

`config/sources.yaml` 是来源注册表。每个 `SourceDefinition` 记录 `source_id`、名称、类别、地区、访问方式、适配器、状态和是否允许在线抓取。

`registry_only`、目录型来源或 `crawl_enabled: false` 只说明来源已经登记或被发现，不说明本版本已经逐站接入或成功访问。运行结果必须区分：

- 已访问并成功返回；
- 访问失败或超时；
- 登录、验证码或人机验证阻断；
- 已登记但没有在线 Adapter；
- HTTP 200 但疑似异常零结果。

### Adapter selection

`SourceRegistry` 根据配置构造适配器，`build_adapter()` 负责将 adapter 名称映射到实现：

- 固定 JSON/API 或特殊 HTML 来源使用自定义类；
- 普通列表页可使用 `config/source_adapters/` 下的 Generic HTML YAML；
- 必须执行 JavaScript、登录或浏览器人工验证的来源不能伪装成普通 HTTP Adapter；
- 遇到访问控制时，系统记录人工动作要求，不绕过限制。

所有适配器都遵循 `SourceAdapter` 的最小接口：搜索、列表、详情、附件、规范化和健康检查。适配器不应该把不确定的网页文本直接提升为确定业务事实。

## Candidate Recall and enrichment

候选发现层与结构化业务层分离：

- `Candidate` 保存发现阶段的 URL、标题、摘要、候选分类、来源、完整性和下一步动作；
- `Project` / `Announcement` 保存已经结构化的业务记录；
- `CandidateSource`、Enrichment Query/Result、Candidate Fact 和 Source Pivot 保留二手线索及官方追源过程；
- 低置信、二手、EPC、历史项目、结构相关和暂时未核实的候选不能因为不是 `OPEN` 就从候选池删除。

交付层可以使用 Delta Result 隐藏同一搜索范围内已经报告且没有变化的项目，但候选池和数据库仍保留这些记录。

## Snapshot and document parsing

来源响应、附件和本地解析结果之间通过 URL、内容哈希、Snapshot、DocumentParse 和路径建立关联。解析器输出质量、版本和错误信息，供后续抽取和 Replay 使用。

当前接入的解析路径包含 HTML、PDF、DOCX 和 XLSX。PDF/DOCX/XLSX 的实际可处理程度取决于依赖安装、文件结构、编码、大小和来源内容。解析失败应进入错误或 Review，而不是用标题或摘要猜测缺失字段。

## Evidence and structured fields

Evidence 是字段可追溯性的核心。一个事实字段应尽量关联：

- 原始来源 URL；
- Snapshot 或 DocumentParse；
- 原文片段或文档定位；
- 抽取器类型和版本；
- 捕获时间和置信信息。

确定性抽取器先处理日期、编号、招标人、项目名称等可规则化内容。多个来源提供冲突值时，冲突会被保存并进入 Review。缺少足够原文时保持 `NULL` / `UNKNOWN`。

## Identity resolution and deduplication

项目身份解析优先使用项目编号、招标编号、完整项目名称、招标人和代理机构。规范化只用于比较和检索，不应把相似标题自动当成同一项目。

确定性编号或有证据支持的合并可以持久化；`PROBABLE_MATCH` 等不确定匹配需要 Review。原始项目名、规范化名称、来源链接和关联关系应保留，方便追溯。

## Status Engine

Status Engine 根据事实字段、时间元数据和 Evidence 计算：

- `OPEN`：证据支持项目仍可能参与；
- `CLOSED`：截止或关闭事实足够明确；
- `UNKNOWN`：当前证据不足、冲突未解或来源状态无法确认。

`OPEN` / `CLOSED` 不是 LLM 的自由文本结论。Codex 只能通过真实原文 Evidence 完成事实字段 Review，之后由 Python 重新计算状态。`recalc` 不访问网站，只按当前规则重算已有项目。

延期、变更、澄清和重开通过新的 Evidence、Timeline 和状态历史保存，不能删除旧事实。

## Review queue and verification

Review 用来承接：

- 缺失或弱来源字段；
- 来源间冲突；
- 低质量文档；
- 项目身份不明确；
- 二手线索的官方追源；
- 需要登录或浏览器人工验证的来源。

`verify` 对 UNKNOWN、弱来源和冲突项目执行精确查询；`review` 生成离线 Review 文件；`set-field` 只能把带真实证据的事实写回，不能直接写状态。

## Persistence and runtime files

SQLite 使用 WAL、busy timeout、foreign keys、FTS5（不可用时回退 LIKE）和跨进程写入队列。多个本地搜索任务共享一个数据库时，后启动任务会等待锁释放，不应手动删除 `.lock` 文件或终止已有任务。

结构化数据在 SQLite 中，运行输出和大文件在文件系统中。当前实现的默认运行根目录为 checkout 的上一级，数据库、Snapshot、文档、缓存、附件、输出和浏览器 Profile 均由该位置推导；可以用 `--database` 或 `TENDER_DATABASE_URL` / `TENDER_DB_PATH` 指定数据库，但其他运行目录的统一配置仍是已知限制。

## Web browser and export

`python -m tender_ai web` 启动本地 FastAPI/Uvicorn 数据浏览器。Web 负责查看、配置、Review 和人工写回，不会因为 `create_app()` 自动发起搜索。

Excel 导出由 `export --session SESSION_ID` 生成，默认只导出 `OPEN`，使用 `--include-unknown` 可以加入 `UNKNOWN`。

## Codex boundary

Codex 可以：

- 解析自然语言条件并生成 `SearchRequest`；
- 读取 Session 报告、Snapshot、解析文档和 Review；
- 分析疑难公告和字段冲突；
- 指引人工完成合法的登录或验证码操作；
- 在提供原文 Evidence 后调用 `set-field` 写回事实；
- 运行 `recalc` 并总结结果。

Codex 不可以：

- 凭空填充事实字段；
- 直接写入或覆盖 `OPEN` / `CLOSED`；
- 把搜索摘要当作官方公告；
- 把二手链接冒充官方来源；
- 把未访问、失败或未配置来源写成成功覆盖；
- 绕过验证码、登录、人机验证或其他访问控制。

## Known boundaries

- 来源注册表的规模不等于在线接入规模；以每个来源的运行状态和 Coverage Manifest 为准。
- 实时搜索受目标网站变更、网络、限流、验证码、登录和第三方 Provider 稳定性影响。
- 项目默认 runtime path 仍以 checkout 父目录为中心，尚未提供统一的运行目录配置；部署时应使用可写目录并隔离运行数据。
- 当前核心 Python 路径不依赖模型 API；Codex 集成发生在外部编排层。
