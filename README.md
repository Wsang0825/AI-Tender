# AI-Tender

Evidence-driven tender intelligence for renewable-energy opportunities.

[![CI](https://github.com/Wsang0825/AI-Tender/actions/workflows/ci.yml/badge.svg)](https://github.com/Wsang0825/AI-Tender/actions/workflows/ci.yml) [![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

AI-Tender 是一个本地优先、证据驱动、可审计的新能源招投标搜索与研判系统。它把公开来源中的公告、附件和项目线索整理成可追溯的项目记录，覆盖来源发现、抓取、文档解析、证据留存、跨来源归并、状态计算、人工复核和结果导出。

项目当前版本为 `0.2.0`。这是一个需要在本地部署的开源项目，不是托管式 SaaS，也不声称已经覆盖全部互联网来源或适用于所有生产环境。

## What is AI-Tender / 项目简介

新能源招投标信息分散在公共资源交易平台、政府采购网站、能源央国企采购平台、地方站点和公开行业线索中。AI-Tender 将这些来源接入统一的数据流，并保存每一步的来源和处理结果，使使用者可以回答的不只是“搜到了什么”，还包括：

- 这条信息来自哪里，是否有原文或附件证据；
- 多个页面是否指向同一个项目；
- 项目当前是 `OPEN`、`UNKNOWN` 还是 `CLOSED`，判断依据是什么；
- 哪些字段仍然需要人工或 Codex 复核；
- 哪些来源没有访问成功，搜索覆盖边界在哪里。

## Why AI-Tender / 为什么需要它

普通关键词搜索通常只返回链接，无法稳定保存快照、处理附件、归并重复公告或解释状态变化。直接让 LLM 填满字段则会把“无法确认”伪装成确定事实。

AI-Tender 将可重复的工作交给 Python，将需要理解和判断的工作显式交给 Codex 或人工：

- 规则和数据状态可重放、可测试；
- 关键字段尽量绑定原始 Evidence；
- 无法可靠确认的字段保留 `NULL` / `UNKNOWN`；
- 延期、变更、重开和来源阻断不会被静默丢弃；
- 搜索结果、候选池、Review 和来源覆盖情况可以继续被程序处理。

## Key Features / 核心能力

- **公开来源搜索**：支持已配置的公共资源、政府采购、能源电力和企业采购来源，并保留来源等级和健康状态。
- **Candidate Recall**：先保存合理候选，再区分相关性、核验状态、招标状态、阻断原因和下一步动作。
- **Evidence-first**：保存来源 URL、Snapshot、解析文档、原文片段、字段值和时间线，便于审计和回放。
- **确定性状态引擎**：由 Python 计算 `OPEN`、`UNKNOWN`、`CLOSED`；Codex 不能直接把一个项目改成确定状态。
- **身份解析与去重**：基于项目名称、项目编号、招标人、代理机构等事实进行跨来源归并；不确定匹配进入 Review。
- **官方追源**：公众号和二手平台只作为线索，系统会尝试根据项目名称、编号、招标人和代理机构追查官方公告。
- **文档处理**：支持 HTML、PDF、DOCX、XLSX 等已接入解析路径，并记录解析质量和失败原因。
- **结果交付**：每次搜索生成 Markdown、JSON 和可选 Excel 结果，同时提供本地 Web 数据浏览器。
- **本地可控**：核心数据使用 SQLite；Python 主路径不调用 OpenAI、Anthropic、Gemini 或其他模型 API，也不运行后台定时扫描。

## Architecture / 架构

```text
Search request
      │
      ▼
Source plan ──► Crawl / Discovery ──► Candidate pool
                                      │
                                      ▼
                         Snapshot / Document parsing
                                      │
                                      ▼
                         Deterministic extraction
                                      │
                                      ▼
                           Evidence / Timeline
                                      │
                                      ▼
                       Identity resolution / Deduplication
                                      │
                                      ▼
                             Status Engine / Review
                                      │
                                      ▼
                         Report / JSON / Excel / Web UI
```

### Python 的职责

Python 负责：

- crawling、Discovery 和来源健康记录；
- Snapshot、附件下载和解析产物留存；
- HTML、PDF、DOCX、XLSX 等文档解析；
- 确定性字段抽取、Evidence 和 Timeline；
- Candidate Recall、身份解析和跨来源去重；
- 状态引擎、SQLite 持久化、Review 文件和导出；
- 本地 Web 数据浏览器。

### Codex 的职责

Codex 是可选的上层协作智能层，负责：

- 将自然语言需求映射为搜索参数；
- 编排一次或多次 CLI 搜索和核验；
- 阅读疑难公告、附件和 Snapshot；
- 对歧义字段进行 Review；
- 在有真实 Evidence 的前提下提出事实修正；
- 汇总结果、覆盖范围、阻断来源和下一步动作；
- 协助维护者理解代码和贡献变更。

Codex 不应该凭空写入事实，也不能绕过 Evidence 直接修改 `OPEN` / `CLOSED`。无法可靠确认的字段必须保持 `NULL` / `UNKNOWN`。

更细的边界和数据流见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

## Quick Start / 快速开始

### Requirements

- Python `>=3.12,<3.13`；
- Git；
- 真实搜索需要可访问目标公开来源的网络环境；
- 需要从仓库 checkout 运行，因为配置文件位于仓库的 `config/` 目录中。

项目元数据目前只明确 Python 3.12。不同操作系统的浏览器、文件锁和来源访问行为可能不同，不要把未验证的平台当作已支持平台。

### Install

```bash
git clone https://github.com/Wsang0825/AI-Tender.git
cd AI-Tender

python3.12 -m venv .venv
# macOS / Linux
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -e "[test]"
```

Windows PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[test]"
```

如果 PowerShell 禁止执行激活脚本，可以不激活虚拟环境，直接使用 `.venv/Scripts/python.exe` 运行下面的命令。

### Initialize and verify

建议显式指定一个本地运行数据库。`runtime/` 已加入 `.gitignore`，不会进入 Git：

```bash
python -m tender_ai init-db --database runtime/tender.db
python -m tender_ai doctor --database runtime/tender.db
python -m tender_ai --help
```

`init-db` 会创建 SQLite 表并同步来源注册表。`doctor` 会检查配置、数据库、解析器、Search Provider 和最近运行状态；它可能初始化或更新本地数据库，不是纯只读命令。

对已有数据库执行 schema migration 前请先备份本地数据库，并通过 `TENDER_DATABASE_URL` 指定目标。新部署使用 `init-db` 即可；迁移既有库时使用 Alembic：

```bash
export TENDER_DATABASE_URL="sqlite:///$(pwd)/runtime/tender.db"
alembic upgrade head
```

PowerShell：

```powershell
$databasePath = (Join-Path (Get-Location) "runtime\tender.db").Replace("\", "/")
$env:TENDER_DATABASE_URL = "sqlite:///$databasePath"
alembic upgrade head
```

不要删除或覆盖已有数据库来“解决”迁移问题。

### Run the first search

先运行 dry-run 查看计划，不访问来源网站：

```bash
python -m tender_ai codex-search \
  --region "内蒙古自治区" \
  --days 30 \
  --industry solar \
  --industry storage \
  --only-open \
  --database runtime/tender.db \
  --dry-run
```

确认计划后，执行真实搜索：

```bash
python -m tender_ai codex-search \
  --region "内蒙古自治区" \
  --days 30 \
  --industry solar \
  --industry storage \
  --only-open \
  --database runtime/tender.db
```

也可以使用中文快捷查询：

```bash
python -m tender_ai search "甘肃最近 7 天 EPC 项目，未知的也给我" --database runtime/tender.db
```

真实搜索会访问配置中的公开来源，并在运行目录生成 Search Session、`search_report.md`、`summary.md`、`results.json`、`candidate_pool.json`、`layers.json`、Review、来源和错误信息。命令输出中的 `session_id` 可用于后续查看和导出。

### Browse and export results

查看搜索历史：

```bash
python -m tender_ai sessions --database runtime/tender.db
```

查看单个项目：

```bash
python -m tender_ai inspect --project PROJECT_ID --database runtime/tender.db
```

导出 Excel：

```bash
python -m tender_ai export \
  --session SESSION_ID \
  --database runtime/tender.db \
  --output runtime/result.xlsx
```

启动本地 Web 数据浏览器：

```bash
python -m tender_ai web --database runtime/tender.db
```

然后打开 <http://127.0.0.1:8765>。Web 默认只监听本机；当前实现没有认证层，不要直接暴露到公网。

### Runtime paths and configuration

数据库参数优先级为：CLI `--database`，其次是 `TENDER_DATABASE_URL`，最后是 `TENDER_DB_PATH`。例如：

```bash
export TENDER_DB_PATH="$PWD/runtime/tender.db"
python -m tender_ai doctor
```

PowerShell：

```powershell
$env:TENDER_DB_PATH = (Join-Path (Get-Location) "runtime\tender.db")
python -m tender_ai doctor
```

`.env.example` 只列出代码实际读取的环境变量；项目当前不会自动加载 `.env`，请在 shell 中导出变量，或直接使用 CLI 参数。`SEARXNG_URL` 只有在 `config/search_providers.yaml` 启用 `searxng` 后才有意义。

当前实现的默认运行目录由 checkout 目录的上一级推导，包含数据库、缓存、Snapshot、文档、下载附件、搜索输出和浏览器 Profile。为了避免不同安装位置的权限问题，部署时建议显式使用 `--database`，并确保 checkout 的父目录可写；这些运行产物不应提交到 Git。这是当前代码的已知限制，部署时应将运行目录视为本地状态而不是仓库内容。

## Codex Workflow / 与 Codex 协作

在 Codex 中可以直接提出：

> 搜内蒙古最近 30 天光伏储能项目，只看现在还能参与的。

上层工作流可以将其映射为：

```bash
python -m tender_ai codex-search \
  --region "内蒙古自治区" \
  --days 30 \
  --industry solar \
  --industry storage \
  --only-open \
  --database runtime/tender.db
```

随后 Codex 应优先读取本次 Session 的 `search_report.md`、`summary.md`、`results.json` 和 `codex_review.md`，检查来源覆盖和人工处理提醒；遇到 `UNKNOWN` 或字段冲突时阅读对应 Snapshot、解析文档和 Evidence，不能用猜测补齐字段。

Codex 不是 Python 主路径的强制依赖。没有 Codex 时，使用者仍可直接运行 CLI、查看数据库和导出结果；Codex 主要提供自然语言编排、复杂文档理解和 Review 协作。

详细的命令边界、dry-run、核验和 Evidence 写回流程见 [`docs/CODEX_SEARCH_GUIDE.md`](docs/CODEX_SEARCH_GUIDE.md)。

## Evidence-first Design / 证据优先

```text
Source
  → Snapshot
  → Parsed Document
  → Evidence
  → Structured Fields
  → Status Engine
  → Review
  → Report
```

`UNKNOWN` 不是系统错误，而是“当前证据不足以确认”的明确状态。来源被验证码、登录、HTTP 412/403/429、超时或适配器缺失阻断时，结果应保留阻断原因和未覆盖范围，不能伪装成零结果或成功访问。

公众号和二手招标平台只提供线索。找到官方公告时以官方来源为准；没有找到时应标记为“二手线索，官方公告未找到/待核验”，而不是只输出二手链接。

## Testing / 验证

本地完整测试命令：

```bash
python -m pytest -q
```

CLI smoke checks：

```bash
python -m tender_ai --help
python -m tender_ai doctor --database runtime/tender.db
```

`tests/test_recall_benchmark.py` 当前依赖作者本地保存的一份历史真实报告，干净 checkout 不具备该文件；基础 CI 因此明确排除该测试，而不是伪造报告或删除测试。详见 [`.github/workflows/ci.yml`](.github/workflows/ci.yml)。

## Project Structure / 项目结构

```text
src/tender_ai/       Python package、CLI、Web、抓取、解析、Evidence 和状态逻辑
tests/               单元测试、回归测试和离线架构测试
config/              来源、行业、地区、Provider 和 Search Profile 配置
migrations/          Alembic schema migrations
docs/                面向使用者和贡献者的公开文档
examples/            不含真实来源内容的合成示例
```

## Documentation / 文档

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)：数据流、模块边界和 Evidence-first 架构。
- [`docs/CODEX_SEARCH_GUIDE.md`](docs/CODEX_SEARCH_GUIDE.md)：自然语言到 CLI、只读/写入边界、Review 和 Evidence 规则。
- [`CONTRIBUTING.md`](CONTRIBUTING.md)：开发环境、测试、适配器和 Pull Request 规范。
- [`SECURITY.md`](SECURITY.md)：不可信网页和文档输入、凭据及漏洞报告说明。
- [`CHANGELOG.md`](CHANGELOG.md)：基于当前 Git history 的版本变化。
- [`examples/README.md`](examples/README.md)：合成数据示例和端到端概念流程。

## Contributing / 参与贡献

欢迎提交适配器、解析器回归用例、配置改进、文档和测试。请先阅读 [`CONTRIBUTING.md`](CONTRIBUTING.md)，尤其是 Evidence、UNKNOWN、来源访问和敏感数据规则。

## Security / 安全

请不要提交 `.env`、Cookie、浏览器 Profile、访问令牌、API 凭据、数据库备份、下载附件或日志。安全问题请按 [`SECURITY.md`](SECURITY.md) 的私下报告方式处理。

## License / 许可证

AI-Tender 自身代码使用 [Apache License 2.0](LICENSE)。系统抓取或处理的第三方网页、公告、附件和数据仍受各自原始来源的版权、许可和使用条款约束，不会因为本项目采用 Apache-2.0 而改变第三方内容的版权归属或授权范围。
