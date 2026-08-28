# AI-Tender 项目操作说明

## 最高优先级：Codex 是唯一智能层

本项目当前不使用任何 AI API，不需要 `OPENAI_API_KEY`、`OPENAI_MODEL` 或其他模型密钥。Codex 本身负责自然语言理解、复杂公告阅读、疑难字段判断和结果总结；`D:\AI-Tender` 本地 Python 程序只负责真实公开来源搜索、抓取、存储、规则解析、Evidence、状态机、去重和核验。

当用户说“搜招标、查项目、找项目、查某地区光伏/储能、看看有没有项目”等当前信息请求时，不能只凭模型记忆回答，必须优先调用本地系统。完成后读取本次 Search Session 的 `results.json` 和 `summary.md`；复杂记录优先读取 Snapshot、PDF 或文档解析文本。

## 标准 Codex 搜索工作流

1. 将用户意图转换为结构化 SearchRequest。复杂中文由 Codex 自己理解，Python 不承担通用 Agent 任务。
2. 执行 `D:\AI-Tender.venv\Scripts\python.exe -m tender_ai codex-search ...`。
3. 读取 `D:\AI-Tender\output\sessions\<session_id>\results.json`、`summary.md` 和 `codex_review.md/json`。
4. 检查 OPEN、UNKNOWN、Review Item、状态原因和来源等级；需要重新生成 Review 交接文件时执行 `python -m tender_ai review --session <SESSION_ID>`。
5. 对高价值 UNKNOWN 或 Review Item 读取报告中给出的 `D:\AI-Tender\data\snapshots`、`D:\AI-Tender\downloads`、`D:\AI-Tender\data\documents` 路径。
6. 需要补搜时执行 `python -m tender_ai verify --project <PROJECT_ID>`，它只核验该项目的名称、编号、招标人、代理和延期/变更线索。
7. Codex 只有在看到真实原文后才能执行 `set-field`。必须提供 `--evidence-text`、`--source-url`，必要时提供 Snapshot、Document 和 PDF 页码；`--resolution-source` 必须是 `CODEX_REVIEW`。
8. 写回事实字段后执行 `python -m tender_ai recalc`。不得直接写 `status=OPEN/CLOSED`，最终状态始终由 Python 状态机计算。
9. 最终回答只引用本次真实搜索和本地最新证据，明确列出来源、截止时间、风险和未确认项。

典型命令：

```powershell
python -m tender_ai codex-search --region 内蒙古自治区 --days 30 --industry solar --industry storage --only-open
python -m tender_ai codex-search --region 新疆维吾尔自治区 --city 哈密市 --days 30 --equipment 光伏支架 --deep --only-open
python -m tender_ai codex-search --region 甘肃省 --days 7 --project-type EPC --include-unknown
```

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

## 项目路径

- 应用：`D:\AI-Tender\app`
- 数据库：`D:\AI-Tender\data\tender.db`
- Snapshot：`D:\AI-Tender\data\snapshots`
- 文档解析文本：`D:\AI-Tender\data\documents`
- 附件：`D:\AI-Tender\downloads`
- Search 输出：`D:\AI-Tender\output\sessions`
- Python 环境：`D:\AI-Tender.venv`
- Codex 指南：`D:\AI-Tender\CODEX_SEARCH_GUIDE.md`

## 维护与验证

不要重新初始化项目、删除真实数据或重做已通过的前三阶段。数据库结构变更使用 Alembic Migration。改动后运行：

```powershell
python -m alembic upgrade head
pytest
python -m tender_ai doctor
```
