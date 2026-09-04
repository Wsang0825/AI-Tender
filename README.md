# 区域新能源招投标自动搜索系统

这是一个个人本地系统。用户直接在 Codex 中提出搜索要求，Codex 调用本地 Python 工具执行一次真实公开来源搜索；程序负责抓取、规则解析、Evidence、Snapshot、去重和状态计算，不调用任何 AI API，也不运行后台定时扫描。

## 最简单的使用方式

在 Codex 中直接说：

> 搜内蒙古最近 30 天光伏储能项目，只看现在还能参与的。

Codex 应调用：

```powershell
cd D:\AI-Tender\app
D:\AI-Tender.venv\Scripts\python.exe -m tender_ai codex-search --region 内蒙古自治区 --days 30 --industry solar --industry storage --only-open
```

通常不需要用户手工记命令。给 Codex 的完整工作流见 [D:\AI-Tender\CODEX_SEARCH_GUIDE.md](D:\AI-Tender\CODEX_SEARCH_GUIDE.md)。

当前系统状态入口：[D:\AI-Tender\CURRENT_STATUS.md](D:\AI-Tender\CURRENT_STATUS.md)。

## CLI

```powershell
# 结构化按需搜索
python -m tender_ai codex-search --region 新疆维吾尔自治区 --city 哈密市 --days 30 --equipment 光伏支架 --deep --only-open

# 快捷中文搜索
python -m tender_ai search "甘肃最近 7 天 EPC 项目，未知的也给我"

# 只看计划，不访问网站、不写正式搜索结果
python -m tender_ai codex-search --region 云南省 --days 30 --industry solar --dry-run

# 读取一次搜索结果
python -m tender_ai inspect --project PROJECT_ID
python -m tender_ai review --session SESSION_ID
python -m tender_ai verify --project PROJECT_ID
python -m tender_ai recalc

# Excel 默认只导出 OPEN；加参数包含 UNKNOWN
python -m tender_ai export --session SESSION_ID
python -m tender_ai export --session SESSION_ID --include-unknown

# 历史条件、模板和扩展搜索
python -m tender_ai sessions
python -m tender_ai templates
python -m tender_ai expand-search --session SESSION_ID --deep

# 本地数据浏览器，不会自动搜索
python -m tender_ai web --host 127.0.0.1 --port 8765
```

也可以双击 `D:\AI-Tender\start_web.bat` 启动 Web 数据浏览器。

## Web 数据浏览器

地址：`http://127.0.0.1:8765`

Web 是数据浏览器和配置控制台，不是聊天机器人，包含：

- 总览、OPEN、UNKNOWN、今天新增、今天变化、延期重开、关注、忽略
- 搜索历史、Session 结果、Source Plan、Excel 导出
- 项目详情、公告、Evidence、Timeline、Snapshot、PDF/文档/附件路径
- Codex Review 待处理队列、Field Conflict、状态原因
- Search Profile、地区、行业关键词、来源和 Search Provider 设置
- Manual Override、取消人工修正、Favorite / Ignore
- Source Health、Discovery 新来源和系统诊断

修改设置后下一次显式搜索生效。没有 scheduler 开关，系统固定为按需执行。

## 数据和输出路径

- 应用：`D:\AI-Tender\app`
- SQLite：`D:\AI-Tender\data\tender.db`
- Snapshot：`D:\AI-Tender\data\snapshots`
- 文档解析结果：`D:\AI-Tender\data\documents`
- 附件：`D:\AI-Tender\downloads`
- Search Session：`D:\AI-Tender\output\sessions\<session_id>`
- 浏览器 Profile：`D:\AI-Tender\data\browser_profiles\<source_id>`
- 全国地区目录：`config\region_catalog.yaml`
- Search Profile：`config\search_profiles.yaml`
- 行业关键词组：`config\industry_profiles.yaml`
- Generic Adapter：`config\source_adapters\`
- 当前状态与验证报告：`D:\AI-Tender\CURRENT_STATUS.md`、`D:\AI-Tender\ARCHITECTURE_REPORT.md`

来源注册表还登记法定平台、地方官方来源家族和能源央企/电建电网采购平台，包括军队采购网、央采网、中直机关采购网、政采云、税务采购网、各级公共资源与政府采购站点、交通/水利招投标平台、国企阳光采购平台、发电集团采购平台、国网 ECP、南网、中石油、中石化和中海油等。全国海量省市区县子站点通过总站目录或 Discovery 发现，`registry_only` / `CATALOG` 只表示已登记，不表示已经逐站接入。

深度搜索或显式 `--wechat` 会使用 `weixin_public_index` 查询公开索引中的微信公众号文章。任何登录、验证码、人机检测、JavaScript 验证或 HTTP 412 验证会立即通过 CLI stderr 提醒，并在结果文件标记人工动作和未覆盖来源。

二手网站和公众号只作为线索：系统会用项目名称、项目/招标编号、招标人和代理机构执行有限官方追源；有官方命中时结果优先使用官方链接，未命中时保留二手出处并标注“官方公告未找到/待核验”。

每次搜索输出 `summary.md`、`results.json`、`open_projects.json`、`unknown_projects.json`、`codex_review.md`、`codex_review.json`、`errors.json` 和 `sources.json`。

每次搜索同时输出统一可读报告 `output\sessions\<session_id>\search_report.md`。对外回答搜索结果时，必须把官方来源、二手来源、来源等级、截止时间、状态、未覆盖来源和核验限制整理进这份文档，并向用户提供文档路径；JSON文件保留给Codex和程序继续处理。

重复执行相同搜索时，系统会自动隐藏上次已经报告且没有内容、状态、公告或来源变化的项目，只输出新增、更新、延期重开和新发现来源；被隐藏项目仍保留在数据库和历史 Session 中，并在结果文件记录忽略数量。来源遇到登录、验证码或 JavaScript 验证（包括带验证页特征的 HTTP 412）时，会标记 `NEEDS_ATTENTION` 和 `manual_action_required`，明确提示人工处理，不伪装成无结果。

## 架构边界

- Codex：自然语言理解、复杂公告阅读、疑难字段判断、结果总结、按需核验编排。
- Python：公开来源搜索、HTTP/API/HTML 抓取、Snapshot、确定性规则抽取、PDF/DOCX/XLSX 解析、Evidence、状态机、跨来源去重、SQLite、CLI。
- Python 当前不调用 OpenAI、Anthropic、Gemini 或任何其他模型 API，不需要 API Key。
- `OPEN`、`UNKNOWN`、`CLOSED` 由 Python Status Engine 计算；Codex 只能用带真实原文 Evidence 的 `set-field` 写回事实字段。
- 规则无法可靠确认时保留 NULL/UNKNOWN，并生成 Codex Review，不猜值。
- SQLite 使用 WAL、busy timeout、foreign keys 和 FTS5；FTS5 不可用时自动回退 LIKE。
- 旧 LLM 接口仅为兼容预留，禁用且不在主执行路径；`llm_extracted` 不代表当前调用过模型。

## 扩展方式

- 换地区：修改 Search Profile 或在 CLI 传 `--region`，不改爬虫代码。
- 换行业：新增 Industry Profile / Keyword Group。
- 增加普通网站：新增 Generic Adapter YAML。
- 增加特殊网站：新增 Custom Adapter。
- 更换搜索服务：启用合法 Search Provider fallback。
- 网站改版：使用已保存 Snapshot、`replay` 和 Source Contract Test 离线排查。
- 状态规则变化：修改规则版本后执行 `python -m tender_ai recalc`。

## 验证

```powershell
cd D:\AI-Tender\app
python -m alembic upgrade head
python -m pytest -q
python -m tender_ai doctor
```

不要删除 `D:\AI-Tender\data\tender.db`，不要把 `.env`、Cookie、浏览器 Profile、缓存、日志、附件或数据库备份提交到 Git。
