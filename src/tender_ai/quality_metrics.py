"""Quality metrics that distinguish recall from successful execution."""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable


def calculate_quality_metrics(
    *,
    candidates: Iterable[Any],
    sources: Iterable[dict[str, Any]] = (),
    enrichment_queries: int = 0,
    official_conversions: int = 0,
    secondary_usable_conversions: int | None = None,
) -> dict[str, Any]:
    rows = list(candidates)
    source_rows = list(sources)
    total = len(rows)

    def read(row: Any, name: str, default: Any = None) -> Any:
        return row.get(name, default) if isinstance(row, dict) else getattr(row, name, default)

    official = sum(1 for row in rows if str(read(row, "verification_status", "")).upper() in {"OFFICIAL_VERIFIED", "OFFICIAL_PARTIAL", "MULTI_SOURCE_CONFIRMED"} or read(row, "official_found", False))
    secondary_rows = [row for row in rows if str(read(row, "candidate_class", "")).upper() == "SECONDARY_LEAD" or str(read(row, "verification_status", "")).upper() == "SECONDARY_ONLY"]
    multi_source = sum(1 for row in rows if str(read(row, "verification_status", "")).upper() == "MULTI_SOURCE_CONFIRMED" or int(read(row, "source_count", 0) or 0) >= 2)
    usable_states = {"USABLE", "COMPLETE"}
    complete = [float(read(row, "completeness_score", None) or read(row, "rank_score", 0) or 0) for row in rows]
    states = Counter(str(read(row, "enrichment_state", "")) for row in rows)
    status_counts = Counter(str(read(row, "tender_status", None) or read(row, "status", "UNKNOWN")) for row in rows)
    source_total = len(source_rows)
    attempted = sum(1 for row in source_rows if row.get("selected") or row.get("attempted") or row.get("status") in {"ACTIVE", "READY", "COMPLETED"})
    successful = sum(1 for row in source_rows if row.get("status") in {"ACTIVE", "READY", "COMPLETED"} and not row.get("error"))
    critical_fields = ("qualification_deadline", "registration_deadline", "document_deadline", "bid_deadline", "open_time")
    critical_covered = sum(
        1 for row in rows
        if read(row, "critical_evidence_fields", None)
        or (read(row, "evidence_ids", None) and any(read(row, field, None) for field in critical_fields))
    )
    secondary_official = sum(
        1 for row in secondary_rows
        if str(read(row, "verification_status", "")).upper() in {"OFFICIAL_VERIFIED", "OFFICIAL_PARTIAL", "MULTI_SOURCE_CONFIRMED"}
        or read(row, "official_found", False)
    )
    secondary_usable = secondary_usable_conversions if secondary_usable_conversions is not None else sum(
        1 for row in secondary_rows if str(read(row, "enrichment_state", "")).upper() in usable_states
    )
    secondary_rate = round(secondary_official / len(secondary_rows), 4) if secondary_rows else "N/A"
    secondary_usable_rate = round(secondary_usable / len(secondary_rows), 4) if secondary_rows else "N/A"
    return {
        "candidate_recall": total,
        "secondary_candidates": len(secondary_rows),
        "official_verified_count": official,
        "multi_source_confirmed_count": multi_source,
        "usable_count": sum(states[state] for state in usable_states),
        "partial_count": states["PARTIAL"],
        "blocked_count": states["BLOCKED"],
        "exhausted_count": states["EXHAUSTED"],
        "candidate_precision": round(sum(1 for row in rows if str(read(row, "relevance_class", "")) not in {"IRRELEVANT", ""}) / total, 4) if total else 0.0,
        "official_verification_rate": round(official / total, 4) if total else 0.0,
        "enrichment_success_rate": round(sum(1 for state in ("USABLE", "COMPLETE") for _ in range(states[state])) / total, 4) if total else 0.0,
        "completeness_score_distribution": {
            "average": round(sum(complete) / total, 2) if total else 0.0,
            "complete_ge_85": sum(value >= 85 for value in complete),
            "usable_60_84": sum(60 <= value < 85 for value in complete),
            "partial_30_59": sum(30 <= value < 60 for value in complete),
            "lead_lt_30": sum(value < 30 for value in complete),
        },
        "critical_fact_coverage": round(critical_covered / total, 4) if total else 0.0,
        "critical_evidence_coverage": round(critical_covered / total, 4) if total else 0.0,
        "source_coverage": {"target": source_total, "attempted": attempted, "successful": successful, "rate": round(successful / source_total, 4) if source_total else 0.0},
        "dedup_precision_recall": "requires labeled benchmark corpus",
        "geo_accuracy": "requires labeled benchmark corpus",
        "identity_resolution_accuracy": "requires labeled benchmark corpus",
        "secondary_to_official_conversion_rate": secondary_rate,
        "secondary_to_usable_rate": secondary_usable_rate,
        "secondary_official_verified_count": secondary_official,
        "secondary_usable_count": secondary_usable,
        "status_counts": dict(status_counts),
        "enrichment_queries": enrichment_queries,
        "candidate_state_counts": dict(states),
    }


__all__ = ["calculate_quality_metrics"]
