"""文档解析质量门：普通/扫描 PDF 优先 PyMuPDF4LLM，质量不足时再交给后续 MinerU。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DocumentQuality:
    score: float
    text: str
    parser: str
    needs_ocr: bool = False
    needs_mineru: bool = False


def document_quality_score(text: str, *, expected_terms: tuple[str, ...] = ()) -> float:
    value = text or ""
    if not value:
        return 0.0
    printable = sum(1 for char in value if char.isprintable() and char not in "\ufffd")
    base = min(1.0, printable / max(200, len(value)))
    if expected_terms:
        base *= min(1.0, sum(term in value for term in expected_terms) / min(3, len(expected_terms)))
    return round(max(0.0, min(1.0, base)), 4)


def extract_pdf(path: str | Path, *, expected_terms: tuple[str, ...] = ()) -> DocumentQuality:
    target = Path(path)
    try:
        import pymupdf4llm

        text = pymupdf4llm.to_markdown(str(target))
        parser = "pymupdf4llm"
    except Exception:
        text = ""
        parser = "pymupdf4llm_unavailable"
    score = document_quality_score(text, expected_terms=expected_terms)
    return DocumentQuality(score, text, parser, needs_ocr=score < 0.35, needs_mineru=score < 0.15)


__all__ = ["DocumentQuality", "document_quality_score", "extract_pdf"]
