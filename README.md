# AI-Tender

西北五省新能源招投标自动搜索系统的个人本地项目。

当前阶段只完成开发环境、浏览器、依赖、参考仓库和可运行性验证，不实现招投标业务逻辑。

## 目录

- `src/tender_ai/`：主程序包骨架，按后续采集、发现、文档、证据和存储职责拆分
- `config/`：配置文件目录
- `tests/`：项目测试目录

统一工作目录为 `D:\AI-Tender`，虚拟环境为 `D:\AI-Tender.venv`。加载当前 PowerShell 环境：

```powershell
. D:\AI-Tender\env.ps1
```

环境安装、实际工具测试和参考仓库清单见 [SETUP_REPORT.md](../SETUP_REPORT.md)。
## 第二阶段核心框架

当前版本只提供核心领域模型和本地基础设施，不执行大规模网站抓取：

```powershell
python -m tender_ai doctor
python -m tender_ai init-db
python -m tender_ai recalc
python -m tender_ai sources
```

数据库默认位于 `D:\AI-Tender\data\tender.db`。来源注册表在 `config/sources.yaml`，所有重要字段应通过 `evidence` 表保留原始证据。
