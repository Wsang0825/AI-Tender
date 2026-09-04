# Contributing to AI-Tender

感谢参与 AI-Tender。贡献可以是来源适配器、解析回归测试、配置、文档、测试或缺陷修复。请先阅读 [SECURITY.md](SECURITY.md)，不要在 Issue 或 Pull Request 中公开凭据、Cookie、浏览器 Profile、个人数据库或下载附件。

## Development setup

项目要求 Python `>=3.12,<3.13`，推荐从源码 checkout 并使用 editable install：

```bash
git clone https://github.com/Wsang0825/AI-Tender.git
cd AI-Tender
python3.12 -m venv .venv
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

需要额外的浏览器适配器依赖时，再按实际来源需求安装可选 extra；不要为了通过测试安装未使用的服务或凭据。核心 Python 路径不需要 OpenAI、Anthropic 或 Gemini API key。

建议为本地数据库显式指定路径，并将运行数据留在被 `.gitignore` 排除的目录：

```bash
python -m tender_ai init-db --database runtime/tender.db
```

当前代码的 Snapshot、下载和输出目录还会按 checkout 的父目录推导，部署时请确保该位置可写，不要把这些运行产物加入提交。

## Database migrations

新数据库可以使用 `python -m tender_ai init-db --database runtime/tender.db` 初始化。已有数据库的 schema 变化使用 Alembic，并通过 `TENDER_DATABASE_URL` 指向目标库：

```bash
export TENDER_DATABASE_URL="sqlite:///$(pwd)/runtime/tender.db"
alembic upgrade head
```

提交 migration 前应在独立临时数据库上验证升级，不要删除真实数据库来规避迁移错误，也不要修改已经发布的 migration 文件；新增 schema 变化应新增版本文件并配套测试。

## Running tests

```bash
python -m pytest -q
python -m tender_ai --help
python -m tender_ai doctor --database runtime/tender.db
```

测试应尽量使用合成数据、临时 SQLite 和离线 HTTP 响应。不要让单元测试依赖登录、验证码、个人 Cookie 或不可控的实时网页。

基础 CI 使用：

```bash
python -m pytest -q --ignore=tests/test_recall_benchmark.py
```

`tests/test_recall_benchmark.py` 当前依赖作者本地保存的历史真实报告，干净 checkout 中没有该报告；不要通过提交真实报告或删除测试来掩盖这一限制。

## Project structure

```text
src/tender_ai/
  cli/                  Typer CLI 命令
  crawlers/             公开来源抓取编排和 HTTP 客户端
  discovery/            Discovery Provider、候选发现和线索追源
  documents/            附件下载和文档解析
  extractors/           确定性字段抽取和 Evidence 生成
  matching/             身份规范化和跨来源去重
  snapshots/            原始快照留存
  sources/              Source contract、注册表和适配器
  status/               状态规则和时间元数据
  storage/              SQLAlchemy 模型、SQLite 和 repository
  web/                  本地数据浏览器
tests/                  离线单元、集成边界和回归测试
config/                 来源、行业、地区、Provider 和搜索配置
migrations/             Alembic schema migrations
docs/                   公开架构和 Codex 工作流文档
examples/               合成演示数据和说明
```

## Pull requests

- 一个 Pull Request 尽量只解决一个问题。
- 新功能、行为修复和配置行为变化应有测试或明确说明为何无法测试。
- Parser / Adapter 修改尽可能增加离线 regression fixture 或最小合成响应。
- 行为改变需要同步更新 README 或 `docs/`。
- 不要提交数据库、SQLite WAL/SHM 文件、Cookie、浏览器 Profile、下载附件、日志、缓存、`.env` 或真实凭据。
- 不要 force-push、重写公共历史或把无关重构混入功能 PR。
- PR 描述应说明测试命令、外部网络依赖和已知未覆盖范围。

## Adding a source adapter

来源配置位于 `config/sources.yaml`。每个来源使用唯一 `source_id`，并通过 `adapter`、`adapter_level`、`crawl_enabled` 等字段声明实际接入状态。`registry_only` 或 `crawl_enabled: false` 只表示登记，不表示已经成功访问。

### Generic HTML source

对普通列表页，优先使用声明式 Generic Adapter：

1. 在 `config/source_adapters/` 新增 YAML 配置；
2. 配置 `list_url`、可选 `search_url`、列表项和标题/链接选择器、分页、详情正文和附件选择器；
3. 在 `config/sources.yaml` 将来源的 `adapter` 设为 `generic:<配置文件名>`，或使用 `adapter_level: GENERIC_HTML` 和 `adapter_config`；
4. 确认 `browser_required: false`，不要用 Generic HTTP Adapter 处理必须执行 JavaScript 或需要登录的来源；
5. 添加离线 HTML 响应测试，覆盖列表、详情、附件和日期解析；
6. 运行 `sources --json`、针对来源的 dry-run 和测试，确认未把登记状态写成在线成功。

Generic Adapter 的实际字段模型见 `src/tender_ai/sources/generic.py`，构造映射见 `src/tender_ai/sources/adapters.py`，不要在文档中发明其他接口。

### Custom adapter

站点有稳定 JSON/API 或特殊 HTML 行为时，才在 `src/tender_ai/sources/adapters.py` 中添加或修改自定义适配器，并在 `build_adapter()` 的映射中注册。此类代码变更必须配套离线响应和失败路径测试，记录访问限制、HTTP 状态、编码和附件行为。需要浏览器或人工验证的来源必须显式报告，不得绕过访问控制。

## Evidence and extraction rules

- 不允许为了提高字段填充率而猜值。
- 无法可靠提取时保持 `UNKNOWN` / `NULL`，并保留 Review 所需上下文。
- 尽量保存原始来源 URL、Snapshot、文档路径、原文片段和解析版本。
- `OPEN`、`UNKNOWN`、`CLOSED` 由 Status Engine 根据事实和 Evidence 计算。
- Codex 或其他 LLM 输出不能直接绕过 Evidence 修改状态。
- `set-field` 只用于根据真实原文完成 Review 后写回事实字段；写回后应运行 `recalc`。
- 二手线索和公众号内容必须标记来源等级，并按项目名称、编号、招标人、代理机构追查官方公告。
- 登录、验证码、人机验证、HTTP 412/403/429 和超时必须保留阻断状态和未覆盖范围。

## Code of conduct

目前仓库没有单独的行为准则文件。请保持技术讨论基于可复现的事实、测试输出和来源证据；安全问题不要公开披露细节，按 [SECURITY.md](SECURITY.md) 处理。
