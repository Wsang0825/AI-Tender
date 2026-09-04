# Examples

本目录只包含合成演示，不包含真实招标公告、第三方网页正文、下载附件、Cookie 或个人数据库。

## Synthetic evidence-first flow

```text
Input
  region=Demo Province
  industry=solar
  days=30
      │
      ▼
Discovery candidate
  title=示例光伏项目采购公告
  source=https://example.invalid/demo-notice
      │
      ▼
Snapshot
  content_hash=synthetic-demo-hash
      │
      ▼
Parsed document
  parser=synthetic fixture
      │
      ▼
Evidence
  field=bid_deadline
  source_text=投标截止时间：2026年9月30日17:00
      │
      ▼
Structured project
  project_code=DEMO-2026-001
      │
      ▼
Status Engine
  status=UNKNOWN
  reason=合成示例不代表可参与结论
      │
      ▼
Report / Review
```

这里故意把状态设为 `UNKNOWN`：有一个示例字段证据，不代表整份公告足以判断项目是否仍可参与。真实应用必须使用可追溯的原始来源，并由 Status Engine 根据完整事实计算状态。

## Running a real demo locally

```bash
python -m tender_ai init-db --database runtime/demo.db
python -m tender_ai codex-search \
  --region "内蒙古自治区" \
  --days 7 \
  --industry solar \
  --database runtime/demo.db \
  --dry-run
```

上面的 dry-run 只展示计划，不访问网站，也不代表已经找到真实项目。要执行真实搜索，去掉 `--dry-run`，并阅读输出中的来源覆盖和阻断信息。

## Why there is no copied webpage fixture

第三方公告和附件可能受版权、使用条款或访问限制约束。仓库不复制大段真实网页内容来制造“漂亮结果”；贡献者应优先使用最小合成数据，或确认拥有公开和再分发所需的权利后再添加精简 fixture。
