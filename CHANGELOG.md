# Changelog

本文件只记录当前仓库 Git history 和代码中可以核实的变化，不代表 GitHub Release 已经发布。

## [0.2.0] - 2026-09-04

- 增加 Candidate Recall、Candidate Enrichment、身份解析和价值线索分层。
- 扩展搜索结果交付模型，保留完整候选池并区分 Full Result、Delta Result 和 A-J 展示层。
- 增加来源覆盖清单、来源健康、人工验证提醒、质量指标和 Recall 回归基准接口。
- 完善公开来源适配器、公告抽取、文档解析、Evidence、Snapshot、Timeline 和 Review 工作流。
- 增加 SQLite 写入队列、离线 Replay、状态重算、模板、Excel 导出和本地 Web 数据浏览器能力。
- 增加对应的单元、架构韧性、来源适配、数据库锁、交付控制和价值线索测试。

## Earlier history

- `6679bf0`：完成 Codex-driven 按需搜索、Review、核验、导出和本地 Web 工作流。
- `12e8cf3`：加入 Evidence-driven extraction、文档解析和 Codex Review。
- `e5b985c`：补充真实来源抓取和 Discovery engine。
- `1ed9997`：将来源、地区、行业、Provider 和 Adapter 配置化并增强韧性。
- `29365d7`：建立招投标核心模型、SQLite 存储和 Status Engine。
- `58f22db`：初始化项目结构、配置和测试环境。
