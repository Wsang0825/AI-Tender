"""真实语料上的 Candidate Recall 回归基准。

这个模块只做测量，不把缺失的外部案例伪造成 Project，也不把搜索摘要
当成官方事实。基准可以同时读取当前 SQLite 中已经持久化的项目和已有的
真实检索报告/快照文字；每个案例都会记录实际命中位置和本地语料可用性。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml
from sqlalchemy import select

from tender_ai.config_loader import APP_ROOT
from tender_ai.storage.database import create_engine_for, initialize_database, session_scope
from tender_ai.storage.models import Project


DEFAULT_BENCHMARK_PATH = APP_ROOT / "config" / "recall_benchmarks" / "known_cases.yaml"
DEFAULT_REPORT_PATH = APP_ROOT.parent / "RECALL_REGRESSION_REPORT.md"


def _normalise(value: Any) -> str:
    return "".join(str(value or "").casefold().split())


@dataclass(frozen=True)
class RecallCase:
    case_id: str
    region: str
    title_terms: tuple[str, ...]
    expected_class: str
    availability: str
    source_artifact: str | None = None
    project_id: str | None = None
    source_url: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class RecallBenchmark:
    benchmark_id: str
    window_start: str
    window_end: str
    intent: str
    cases: tuple[RecallCase, ...]
    source_artifacts: tuple[str, ...] = ()


def load_recall_benchmark(path: Path | None = None) -> RecallBenchmark:
    target = path or DEFAULT_BENCHMARK_PATH
    payload = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, Mapping):
        raise ValueError(f"Recall benchmark 根节点必须是对象: {target}")
    raw_cases = payload.get("cases") or []
    if not isinstance(raw_cases, list):
        raise ValueError("Recall benchmark 的 cases 必须是列表")
    cases: list[RecallCase] = []
    for raw in raw_cases:
        if not isinstance(raw, Mapping):
            raise ValueError(f"Recall benchmark 案例必须是对象: {raw!r}")
        terms = raw.get("title_terms") or []
        if isinstance(terms, str):
            terms = [terms]
        cases.append(
            RecallCase(
                case_id=str(raw["case_id"]),
                region=str(raw.get("region") or ""),
                title_terms=tuple(str(item) for item in terms if str(item).strip()),
                expected_class=str(raw.get("expected_class") or "UNCLASSIFIED"),
                availability=str(raw.get("availability") or "UNKNOWN"),
                source_artifact=str(raw["source_artifact"]) if raw.get("source_artifact") else None,
                project_id=str(raw["project_id"]) if raw.get("project_id") else None,
                source_url=str(raw["source_url"]) if raw.get("source_url") else None,
                notes=str(raw["notes"]) if raw.get("notes") else None,
            )
        )
    artifacts = payload.get("source_artifacts") or []
    if isinstance(artifacts, str):
        artifacts = [artifacts]
    return RecallBenchmark(
        benchmark_id=str(payload.get("benchmark_id") or target.stem),
        window_start=str(payload.get("window_start") or ""),
        window_end=str(payload.get("window_end") or ""),
        intent=str(payload.get("intent") or ""),
        cases=tuple(cases),
        source_artifacts=tuple(str(item) for item in artifacts if str(item).strip()),
    )


def _project_row(project: Project) -> dict[str, Any]:
    return {
        "project_id": project.project_id,
        "title": project.project_name,
        "region": " / ".join(item for item in (project.province, project.city, project.county) if item),
        "source_url": project.source_url,
    }


def _case_matches(case: RecallCase, row: Mapping[str, Any]) -> bool:
    if case.project_id and str(row.get("project_id") or "") == case.project_id:
        return True
    if case.source_url and str(row.get("source_url") or "") == case.source_url:
        return True
    haystack = _normalise(" ".join(str(row.get(name) or "") for name in ("title", "region", "source_url", "text")))
    return bool(case.title_terms) and all(_normalise(term) in haystack for term in case.title_terms)


def _artifact_path(value: str, *, root: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def _load_artifacts(benchmark: RecallBenchmark, *, root: Path, artifact_texts: Mapping[str, str] | None = None) -> dict[str, str]:
    loaded = dict(artifact_texts or {})
    names = set(benchmark.source_artifacts)
    names.update(case.source_artifact for case in benchmark.cases if case.source_artifact)
    for name in names:
        if not name or name in loaded:
            continue
        path = _artifact_path(name, root=root)
        if path.exists() and path.is_file():
            try:
                loaded[name] = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                loaded[name] = ""
        else:
            loaded[name] = ""
    return loaded


def run_recall_benchmark(
    *,
    benchmark_path: Path | None = None,
    database: str | Path | None = None,
    project_rows: Iterable[Mapping[str, Any]] | None = None,
    artifact_texts: Mapping[str, str] | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """在真实 SQLite/报告语料上运行 Recall 测量。

    ``project_rows`` 和 ``artifact_texts`` 只用于离线测试；未传入时读取
    当前数据库和基准中声明的本地文件。``UNAVAILABLE_IN_LOCAL_CORPUS``
    案例不会被计为 Recall 通过，也不会被计为失败的搜索结果。
    """

    benchmark = load_recall_benchmark(benchmark_path)
    corpus_root = root or APP_ROOT.parent
    if project_rows is None:
        engine = initialize_database(create_engine_for(database))
        with session_scope(engine) as session:
            rows = [_project_row(project) for project in session.scalars(select(Project)).all()]
    else:
        rows = [dict(row) for row in project_rows]
    artifacts = _load_artifacts(benchmark, root=corpus_root, artifact_texts=artifact_texts)
    results: list[dict[str, Any]] = []
    matched_count = 0
    observable_count = 0
    for case in benchmark.cases:
        if not case.availability.upper().startswith("UNAVAILABLE"):
            observable_count += 1
        hit = next((row for row in rows if _case_matches(case, row)), None)
        matched_by = "database" if hit is not None else None
        if hit is None and case.source_artifact:
            text = artifacts.get(case.source_artifact, "")
            if case.title_terms and all(_normalise(term) in _normalise(text) for term in case.title_terms):
                hit = {"artifact": case.source_artifact}
                matched_by = "artifact"
        if hit is not None:
            matched_count += 1
        if hit is not None:
            result_status = "MATCHED"
        elif case.availability.upper().startswith("UNAVAILABLE"):
            result_status = "UNAVAILABLE_IN_LOCAL_CORPUS"
        else:
            result_status = "NOT_RECALLED"
        results.append({
            "case_id": case.case_id,
            "region": case.region,
            "title_terms": list(case.title_terms),
            "expected_class": case.expected_class,
            "availability": case.availability,
            "status": result_status,
            "matched_by": matched_by,
            "matched_project_id": hit.get("project_id") if isinstance(hit, Mapping) else None,
            "source_artifact": case.source_artifact,
            "source_url": case.source_url,
            "notes": case.notes,
        })
    region_summary: dict[str, dict[str, int]] = {}
    for result in results:
        bucket = region_summary.setdefault(result["region"], {"total": 0, "matched": 0, "observable": 0, "unavailable": 0})
        bucket["total"] += 1
        bucket["matched"] += int(result["status"] == "MATCHED")
        bucket["observable"] += int(not str(result["availability"]).upper().startswith("UNAVAILABLE"))
        bucket["unavailable"] += int(result["status"] == "UNAVAILABLE_IN_LOCAL_CORPUS")
    observable_matched = sum(
        1 for result in results
        if result["status"] == "MATCHED" and not str(result["availability"]).upper().startswith("UNAVAILABLE")
    )
    return {
        "benchmark_id": benchmark.benchmark_id,
        "window_start": benchmark.window_start,
        "window_end": benchmark.window_end,
        "intent": benchmark.intent,
        "total_cases": len(results),
        "matched_cases": matched_count,
        "recall": f"{matched_count}/{len(results)}" if results else "0/0",
        "observable_cases": observable_count,
        "observable_matched_cases": observable_matched,
        "observable_recall": round(observable_matched / observable_count, 4) if observable_count else "N/A",
        "unavailable_cases": sum(1 for result in results if result["status"] == "UNAVAILABLE_IN_LOCAL_CORPUS"),
        "not_recalled_cases": sum(1 for result in results if result["status"] == "NOT_RECALLED"),
        "region_summary": region_summary,
        "cases": results,
        "source_artifacts": list(benchmark.source_artifacts),
        "corpus": {
            "project_count": len(rows),
            "artifact_count": len([value for value in artifacts.values() if value]),
            "artifact_paths": [name for name, value in artifacts.items() if value],
        },
    }


def render_recall_report(payload: Mapping[str, Any]) -> str:
    lines = [
        f"# Candidate Recall Regression - {payload.get('benchmark_id')}",
        "",
        f"窗口：{payload.get('window_start')} 至 {payload.get('window_end')}",
        f"意图：{payload.get('intent')}",
        f"Recall：{payload.get('recall')}（不可评估案例不计为通过；详见逐条状态）",
        f"本地语料可评估 Recall：{payload.get('observable_matched_cases')}/{payload.get('observable_cases')} = {payload.get('observable_recall')}",
        f"本地语料未提供案例：{payload.get('unavailable_cases')}",
        f"可评估但未召回：{payload.get('not_recalled_cases')}",
        "",
        "## 分地区统计",
        "",
    ]
    for region, values in (payload.get("region_summary") or {}).items():
        lines.append(f"- {region}：{values['matched']}/{values['total']}；可评估 {values['observable']}；本地不可评估 {values['unavailable']}")
    lines.extend(["", "## 逐条结果", ""])
    for case in payload.get("cases") or []:
        lines.extend([
            f"### {case['case_id']}",
            f"- 地区：{case['region']}",
            f"- 关键词：{' / '.join(case['title_terms'])}",
            f"- 预期分类：{case['expected_class']}",
            f"- 可用性：{case['availability']}",
            f"- 结果：{case['status']}；命中方式：{case.get('matched_by') or '无'}",
            f"- 证据文件：{case.get('source_artifact') or '数据库/无'}",
            f"- 来源 URL：{case.get('source_url') or '未声明'}",
            f"- 说明：{case.get('notes') or ''}",
            "",
        ])
    lines.extend([
        "## 解释",
        "",
        "本报告只测量当前本地数据库和已保存真实报告/快照能否召回案例。标记为 UNAVAILABLE_IN_LOCAL_CORPUS 的案例没有被伪造为通过或失败；它们需要补充真实 Snapshot/Gold 证据后再纳入可评估 Recall。",
    ])
    return "\n".join(lines) + "\n"


def write_recall_report(payload: Mapping[str, Any], path: Path | None = None) -> Path:
    target = path or DEFAULT_REPORT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_recall_report(payload), encoding="utf-8")
    return target


def payload_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


__all__ = [
    "DEFAULT_BENCHMARK_PATH", "DEFAULT_REPORT_PATH", "RecallBenchmark", "RecallCase",
    "load_recall_benchmark", "payload_json", "render_recall_report", "run_recall_benchmark",
    "write_recall_report",
]
