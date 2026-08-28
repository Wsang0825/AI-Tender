# AI-Tender

区域新能源招投标自动搜索系统的个人本地项目。系统范围由 Search Profile 配置，默认 Profile 为 `northwest_energy`；更换地区、行业或来源不需要修改爬虫代码。

## 目录

- `src/tender_ai/`：采集、Discovery、解析、证据、快照、状态和存储模块
- `config/search_profiles.yaml`：搜索范围、来源、预算和时间范围
- `config/industry_profiles.yaml`：行业关键词组、同义词和排除词
- `config/region_catalog.yaml`：独立行政区划目录
- `config/source_adapters/`：声明式 Generic HTML Adapter 配置
- `tests/`：离线单元测试与架构契约测试

## 常用命令

```powershell
python -m tender_ai doctor
python -m tender_ai init-db
python -m tender_ai crawl
python -m tender_ai crawl --profile northwest_energy --dry-run
python -m tender_ai discovery
python -m tender_ai discovery --dry-run
python -m tender_ai replay --source SOURCE_ID
python -m tender_ai recalc
python -m tender_ai sources
```

数据库默认位于 `D:\AI-Tender\data\tender.db`，附件位于 `D:\AI-Tender\downloads`，候选公告快照位于 `D:\AI-Tender\data\snapshots`。

状态对用户显示为 `OPEN`、`UNKNOWN`、`CLOSED`，数据库另存 `status_reason`、时间精度和来源证据。FTS5 可用时支持全文搜索，不可用时自动回退到 `LIKE`。
