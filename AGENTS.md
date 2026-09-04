# AI-Tender 项目操作说明

当前状态入口：先看 `D:\AI-Tender\CURRENT_STATUS.md`；当前架构和验证看 `D:\AI-Tender\ARCHITECTURE_REPORT.md`。`SETUP_REPORT.md`、`CORE_REPORT.md`、`FINAL_REPORT.md` 仅是历史背景，不得覆盖当前迁移、测试、来源健康或真实数据结论。

## 多对话并发搜索

多个 Codex 对话可以同时发起搜索。SQLite 通过 `data/tender.db.lock` 使用跨进程单写入队列，后启动的任务自动等待并在锁释放后继续；不得手动终止已有搜索或删除锁文件。等待超过配置时限时，必须立即报告数据库队列超时、当前任务和未覆盖范围。

## 最高优先级：Codex 是唯一智能层

本项目当前不使用任何 AI API，不需要 `OPENAI_API_KEY`、`OPENAI_MODEL` 或其他模型密钥。Codex 本身负责自然语言理解、复杂公告阅读、疑难字段判断和结果总结；`D:\AI-Tender` 本地 Python 程序只负责真实公开来源搜索、抓取、存储、规则解析、Evidence、状态机、去重和核验。

### 用户最短搜索指令

`深度搜索 + 地区 + 项目` 本身就是完整指令。例如：`深度搜索西藏光伏支架项目`。所有“搜/查/找项目”请求都默认按深度搜索处理。不得要求用户再说 `agent-reach`、Python 命令、工作目录、Provider、`--deep`、公众号或二手线索等内部提示词。

收到该形式后，Codex 必须自动识别条件并调用本地 `codex-search` 工作流；同时必须至少调用 2 个子代理按来源组并行检索，默认拆分为官方/公共资源、地方/企业平台、Discovery/微信公众号/二手线索等 4—6 个工作组。`agent-reach` 如被平台自动启用，只能作为补充检索能力，不能替代本地 Search Session、结果 JSON、Evidence 和整理文档。

只有子代理服务不可用、启动失败或平台硬性并发限制时，才允许由主 Codex 降级执行；必须在结果中记录 `SUBAGENT_FALLBACK`、失败原因和未完成的并行分工，不得静默跳过子代理。

当用户说“搜招标、查项目、找项目、查某地区光伏/储能、看看有没有项目”等当前信息请求时，不能只凭模型记忆回答，必须优先调用本地系统。完成后读取本次 Search Session 的 `results.json` 和 `summary.md`；复杂记录优先读取 Snapshot、PDF 或文档解析文本。

## 架构规则

- 产品是通用“区域新能源招投标自动搜索系统”，`northwest_energy` 只是默认 Search Profile，不是永久范围。
- 先保留 API/JSON/HTML 结构和固定规则，再使用普通 HTTP；只有必须执行 JavaScript/XHR/动态 Token 时才使用 Scrapling/Crawl4AI/DrissionPage。不能绕过验证码、登录或访问控制。
- Source、Search Profile、Industry Profile、Generic Adapter 和 SearchProvider 必须可配置、可替换。
- 抽取确定性优先；字段无法可靠识别时保存 NULL/UNKNOWN，并生成 Codex Review，不猜值。
- Snapshot、Evidence、DocumentParse 和 Timeline 必须可追溯；大正文/二进制放文件系统，不塞入 SQLite。
- `OPEN`、`UNKNOWN`、`CLOSED` 以及 `status_reason` 只能由 Python Status Engine 计算。延期/变更不得删除旧 Evidence 和历史时间。
- `PROBABLE_MATCH` 不得自动合并，必须进入 Review；只有编号等确定性依据或带证据的 Codex Review 才能合并。
- 数据库保持 SQLite WAL、busy timeout、foreign keys 和 FTS5；FTS5 不可用时使用 LIKE fallback。
- 所有业务时间使用带时区的 `Asia/Shanghai` datetime；日期只有日期时保留 DATE_ONLY/INFERRED 元数据，不伪造具体时刻。
- `.env`、Cookie、浏览器 Profile、缓存、日志、大附件、数据库备份不得提交 Git。

## 持久化边界

每次搜索必须生成 `search_report.md`、`summary.md`、`results.json` 和相应 Review/来源文件；未访问、失败、转载和未核实内容必须明确标注。`FULL_RESULT` 必须返回当前条件范围内完整 Candidate Pool，不得因历史 Session 或项目未变化而隐藏；只有 `DELTA_RESULT` 才能抑制已交付且未变化的项目，并输出 `suppressed_unchanged_count` 与逐项目 `suppression_reasons`。

登录、验证码、人机检测、JavaScript 挑战、HTTP 412 或登录过期必须立即上报，并记录 `manual_action_required=true`、动作类型、HTTP 状态和覆盖限制；不得绕过访问控制。

人工验证必须形成可执行的用户动作：反馈来源、HTTP 状态、打开地址、默认使用 Microsoft Edge、独立浏览器 Profile 和操作步骤，要求用户亲自完成登录/验证码/人机检测；在用户回复“已完成人工验证”前暂停受阻来源，确认后只重试对应 `source_id` 并继续搜索，不能重复扫描其他来源，也不能把受阻来源写成成功。

Discovery 发现千里马、招标网、北极星、普通转载或微信公众号线索时，必须提取项目名称、编号、招标人/代理机构并执行有限的官方追源查询。最终结果优先使用官方/法定公告；未找到官方公告的线索只能标记为“二手线索/官方未找到或待核验”，不得只把二手 URL 当作官方结果。

Discovery 或外部补充检索发现以下类型时，必须单独上报为“有价值但非直接组件支架采购线索”，不能因不满足严格招标关键词而丢弃，也不能混入直接支架采购或 OPEN 清单：项目级 EPC/工程总承包（支架可能嵌入设备材料范围）、可研/测绘/勘测/规划等前期项目、已竣工/验收/投产且公开提到支架安装的历史项目、箱变/变压器/设备钢结构平台等相近结构件采购。每条保留项目名、阶段、地区、原始标题、来源等级、来源 URL、原文摘要、价值原因、范围限制和后续追踪查询；明确写出“当前证据不能证明是直接组件支架采购”。

## 子代理状态与官方追源硬规则

- `wait_agent`/`wait_threads` 的 `timed_out` 只代表本次等待调用超时，不代表子代理超时、失败或停止；`status` 为空不得推断失败；`previous_status=running` 表示子代理仍在运行。
- 只有 `completed`、`errored`、`interrupted` 或 `shutdown` 才能作为最终状态。等待调用超时不得触发 interrupt、关闭子代理或 `SUBAGENT_FALLBACK`；应继续等待或发送不打断任务的收尾消息。首次等待至少 60 秒，后续原则上每隔至少 60 秒检查一次。
- 最终汇总前必须逐一核对每个子代理的真实状态。只有最终结果才能纳入汇总；仍在运行或尚未返回最终状态的，必须明确标注“仍在运行/尚未返回最终结果”，不得写成“超时、失败或无结果”。
- 第三方网站、行业媒体、微信公众号和二手平台只提供线索。必须按项目全名、招标/项目编号、招标人、代理机构和延期/变更/澄清继续检索官方公告；找到后以官方来源为主，找不到则明确写“二手线索，官方公告未找到/待核验”。禁止用摘要猜测脱敏字段或只反馈二手链接。

不要重新初始化项目、删除真实数据或绕过 Alembic。来源清单、来源等级、CLI 参数、Review、Verification、Evidence 写回、文档格式和详细验证规则只在处理搜索任务时读取 `D:\AI-Tender\CODEX_SEARCH_GUIDE.md`，避免每个新对话重复加载。代码改动后按需运行 pytest 和 `python -m tender_ai doctor`。

## 全国召回与第三方补全架构

本项目面向全国任意地区、行业、设备和工程机会。陕西及西北只属于配置中的回归/默认 Profile，不得在搜索、身份解析或来源规划代码中写地区特例。

- Search 的第一阶段是 Candidate Recall：先按 `SearchRequest.search_mode`（`exact`、`broad`、`opportunity`）和 `config/concepts/` 的 Query Matrix 扩展并保存候选；UNKNOWN、SECONDARY_ONLY、EPC、结构相关、日期缺失、官方未找到、访问阻断和预算耗尽都不能删除 Candidate。
- `Candidate` 是发现层，`Project/Announcement` 是已结构化的业务层。`relevance`、`verification_status`、`tender_status`、`enrichment_state`、`blocker`、`next_action` 分开保存，不能用 OPEN/CLOSED 过滤候选池。
- 第三方或公众号结果必须保留 `CandidateSource`、`CandidateEnrichmentQuery`、`CandidateEnrichmentResult`、`CandidateFact` 和 `SourcePivot`。先解析身份，再按项目全名、项目编号、招标编号、招标人、代理机构、延期/变更/澄清递归追源；有官方结果时以官方 Evidence 为准，找不到时只能标记二手线索待核验。
- 身份不明确时不得生成“项目”之类无意义查询。`IdentityResolution` 应优先使用编号和完整项目名，去除价格/容量等媒体标题前缀；`AMBIGUOUS` 进入 Review 或等待更多事实。
- Coverage、Enrichment 和 Verification 使用独立预算。每个 Search Session 必须生成 Coverage Manifest，明确 attempted、successful、failed、blocked、ADAPTER_NOT_CONFIGURED、SUSPECT_ZERO_RESULTS、skipped、query counts 和 candidate yield。
- `FULL_RESULT` 返回本条件范围内完整 Candidate Pool，`DELTA_RESULT` 只交付 NEW/UPDATED/REOPENED、新来源和状态变化；抑制只影响交付，不影响数据库和 `candidate_pool.json`。
- 结果必须用 A-J 层展示：官方可参与、官方待补、EPC/嵌入、结构相关、历史结果、二手待追源、低置信、已关闭、排除、未覆盖/阻断。高价值 Candidate 不得只因不是 OPEN 而消失。
- `search_report.md` 是用户可读交付文档，`results.json`、`candidate_pool.json`、`layers.json` 是 Codex/程序接口。复杂项目优先读取本地 Snapshot、附件和 `data/documents`，确认原文后才能用 `set-field` 写回 Evidence。

## 真实限制的表达

当前系统不能声称“全网已覆盖”：`registry_only`、`CATALOG_ONLY`、`ADAPTER_NOT_CONFIGURED`、超时、HTTP 412/419/403/429、验证码、登录和人工验证都必须逐来源报告。HTTP 200 且异常为零结果时，要结合历史基线标记 `SUSPECT_ZERO_RESULTS`。数据库、Snapshot、附件和搜索历史只能通过新增迁移和幂等更新扩展，不得重建或清空。
