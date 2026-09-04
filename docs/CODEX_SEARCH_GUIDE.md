# Codex Search Guide

本文档面向使用 Codex 编排 AI-Tender 的维护者和开发者。它以当前 CLI 和代码行为为准，不引入不存在的命令。

## 1. Natural language to CLI

用户可以用自然语言描述地区、时间、行业、设备、项目类型、来源和状态目标。Codex 应将其转换为 `search` 或 `codex-search` 参数。

常见映射：

| 用户条件 | CLI 参数 |
|---|---|
| 地区 | `--region`、`--city`、`--county` |
| 最近 N 天 | `--days N` |
| 起止日期 | `--date-from`、`--date-to` |
| 行业 | 重复使用 `--industry` |
| 项目类型 | `--project-type` |
| 设备 | `--equipment` |
| 包含关键词 | `--keyword` |
| 排除关键词 | `--exclude-keyword` |
| 来源等级/类别 | `--source-level`、`--source-category` |
| 只看可参与 | `--only-open` |
| 同时保留 UNKNOWN | `--include-unknown` |
| 发现未知来源和候选 | `--discovery` |
| 公开公众号索引线索 | `--wechat` |
| 深度搜索组合 | `--deep` |
| 搜索模式 | `--search-mode exact\|broad\|opportunity` |
| 结果模式 | `--result-mode full\|delta` |
| 数据库 | `--database PATH_OR_URL` |
| 只看计划 | `--dry-run` |

最小示例：

```bash
python -m tender_ai codex-search \
  --region "内蒙古自治区" \
  --days 30 \
  --industry solar \
  --industry storage \
  --only-open \
  --database runtime/tender.db
```

`codex-search` 会在 JSON 输出中附带 `NEXT_ACTIONS_FOR_CODEX`，其中包含应读取的报告、人工动作和 Review 后续步骤。

## 2. Codex is optional

Python 程序本身可以独立运行 CLI、数据库、解析、状态计算、Web 和导出。项目当前不调用 OpenAI、Anthropic、Gemini 或其他模型 API，因此不要要求用户设置不存在的模型 API key。

Codex 主要用于：

1. 理解自然语言搜索意图；
2. 选择结构化 CLI 参数；
3. 阅读复杂原文和附件；
4. 处理歧义、冲突和官方追源；
5. 组织结果和下一步动作。

## 3. Read/write/network boundaries

| 命令 | 网络 | 数据库/文件写入 | 用途 |
|---|---:|---:|---|
| `--help` | 否 | 否 | 查看命令和参数 |
| `doctor` | 否 | 可能初始化/更新数据库 | 环境和运行状态检查 |
| `init-db` | 否 | 是 | 创建表并同步来源 |
| `recalc` | 否 | 是 | 按当前规则重算状态 |
| `sources --json` | 否 | 初始化数据库 | 查看来源注册表和健康状态 |
| `sessions` / `history` | 否 | 初始化数据库 | 查看搜索历史 |
| `inspect` | 否 | 初始化数据库 | 查看项目、Evidence 和 Timeline |
| `review` | 否 | 写 Review 文件 | 重新生成一个 Session 的 Review |
| `export` | 否 | 写 XLSX | 导出搜索结果 |
| `crawl` | 是，除非 `--dry-run` | 真实运行时是 | 抓取已核验来源 |
| `discovery` | 是，除非 `--dry-run` | 真实运行时是 | 发现未知 URL 和候选 |
| `search` / `codex-search` | 是，除非 `--dry-run` | 真实运行时是 | 完整按需搜索流程 |
| `extract` | 读本地文件 | 真实运行时是 | 解析已保存公告和附件 |
| `verify` | 是，除非 `--dry-run` | 真实运行时是 | 精确核验 UNKNOWN/冲突项目 |
| `replay` | 否 | 真实运行时是 | 从 Snapshot 离线重跑 |
| `set-field` | 否 | 是 | 带真实 Evidence 写回事实字段 |
| `resolve-review` | 否 | 是 | 更新 Review 队列状态 |
| `template-save` / `template-toggle` | 否 | 是 | 保存或修改搜索模板 |
| `template-run` | 是，除非 `--dry-run` | 真实运行时是 | 执行已保存模板 |

即使一个命令标记为“初始化数据库”，也不应把它当成完全只读操作。使用者应为每次开发、测试或演示指定独立数据库。

## 4. Dry-run first

对于可能产生大量网络请求的搜索，先生成计划：

```bash
python -m tender_ai codex-search \
  --region "新疆维吾尔自治区" \
  --city "哈密市" \
  --days 30 \
  --equipment "光伏支架" \
  --deep \
  --database runtime/tender.db \
  --dry-run
```

dry-run 用于检查来源计划、查询预算和参数解析，不应被描述成真实搜索结果。确认范围后删除 `--dry-run` 再执行。

## 5. After a search

完成搜索后按以下顺序处理：

1. 读取命令 JSON 输出中的 `session_id` 和 `NEXT_ACTIONS_FOR_CODEX`；
2. 读取该 Session 的 `search_report.md`、`summary.md`、`results.json`、`candidate_pool.json`、`layers.json`、`sources.json` 和 `errors.json`；
3. 先处理 `manual_action_sources`：记录来源、动作类型、HTTP 状态、打开地址和覆盖限制；
4. 优先检查 `OPEN`、临近截止日期项目和官方来源；
5. 对 `UNKNOWN`、Field Conflict、二手线索和低置信候选读取 Snapshot、Parsed Document 和 Review；
6. 有明确项目编号、项目名称、招标人或代理机构时，再做官方追源；
7. 把项目级 EPC、可研/测绘、历史安装、箱变或设备钢平台等价值线索单独报告，不能混入直接组件支架采购或 `OPEN` 清单；
8. 只有在事实和原文确认后，才考虑 `set-field`；
9. 写回事实后运行 `recalc`，让 Status Engine 重新计算；
10. 最终报告必须包含官方来源、二手来源、来源等级、截止时间、状态、未覆盖来源和核验限制。

## 6. UNKNOWN and status

`UNKNOWN` 表示当前证据不足或存在未解决冲突，不等于“没有项目”，也不等于“已经关闭”。

不要：

- 用公告标题猜截止时间、招标人、预算或项目编号；
- 用搜索摘要替代公告原文；
- 把 HTTP 200 的异常空响应当成确定零结果；
- 把 `registry_only`、`ADAPTER_NOT_CONFIGURED` 或人工阻断来源当成成功覆盖；
- 因为用户说“只看 OPEN”而从候选池删除 UNKNOWN。

`--only-open` 是交付筛选选项；Candidate Pool、Coverage Manifest 和错误信息仍应保留。

## 7. Verification and Review

查看待处理 Review：

```bash
python -m tender_ai review \
  --session SESSION_ID \
  --database runtime/tender.db
```

对候选项目执行精确核验：

```bash
python -m tender_ai verify \
  --project PROJECT_ID \
  --database runtime/tender.db
```

先用 dry-run 查看核验范围：

```bash
python -m tender_ai verify \
  --max-tasks 12 \
  --database runtime/tender.db \
  --dry-run
```

## 8. Evidence write-back

`set-field` 只能写事实字段，不能直接写 `status`、`status_reason`、`lifecycle_state` 或其他状态结果。调用前必须具备：

- 目标 `project_id` 或公告 ID；
- 真实来源 URL；
- 原文证据片段；
- 必要时的 Snapshot、Document ID、页码或 Review ID。

示例中的 URL 和 ID 是占位符，必须替换为真实值：

```bash
python -m tender_ai set-field \
  --project PROJECT_ID \
  --field bid_deadline \
  --value "2026-09-30 17:00" \
  --evidence-text "投标截止时间：2026年9月30日17:00" \
  --source-url "https://example.invalid/replace-with-official-notice" \
  --precision DATETIME \
  --database runtime/tender.db

python -m tender_ai recalc --database runtime/tender.db
```

没有真实原文时，不应调用 `set-field`。不要为了让表格更完整而写入推断值。

## 9. Manual verification and blocked sources

遇到登录、验证码、人机检测、JavaScript challenge 或 HTTP 412/403/429：

1. 保留来源 URL、状态码、错误文本和来源 ID；
2. 立即告诉用户需要人工操作以及未覆盖范围；
3. 不绕过验证、不复用他人 Cookie、不伪装为成功访问；
4. 用户完成合法验证并明确回复后，只定向重试受阻来源；
5. 最终报告保留阻断记录。

公众号和二手平台只能作为候选线索。官方追源未命中时，明确写“二手线索，官方公告未找到/待核验”。

## 10. Result modes

- `full` / `FULL_RESULT`：交付当前条件范围内的完整候选池视图；
- `delta` / `DELTA_RESULT`：可以隐藏此前已报告且没有变化的项目，但需要保留 `suppression_reasons` 和抑制数量；
- 结果模式只影响交付层，不应删除数据库记录、Candidate Pool、Evidence 或历史 Session。

## 11. Database concurrency

SQLite 使用本地写入队列。并发搜索时：

- 后启动任务会等待已有写入任务；
- 出现等待提示时不要删除 `.lock` 文件；
- 等待超时应报告当前任务、数据库路径和未覆盖范围；
- 开发和测试最好为每个任务指定独立数据库。
