"""PDF 解析质量门。

普通 PDF 先走 PyMuPDF4LLM；文本质量不足时尝试其 OCR/hybrid 参数，仍不足时
把 ``needs_mineru`` 交给上层。MinerU 是可选重依赖，当前环境未安装时不会阻塞
规则抽取。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DocumentQuality:
    score: float  # 0-100
    text: str
    parser: str
    needs_ocr: bool = False
    needs_mineru: bool = False
    page_count: int = 0
    pages: tuple[str, ...] = ()
    used_ocr: bool = False
    error: str | None = None


def document_quality_score(text: str, *, expected_terms: tuple[str, ...] = ()) -> float:
    value = text or ""
    if not value:
        return 0.0
    printable = sum(1 for char in value if char.isprintable() and char not in "\ufffd")
    printable_ratio = printable / max(1, len(value))
    non_space = sum(1 for char in value if not char.isspace())
    # 短标题不能被误认为高质量正文，长正文也不因少量控制字符被判为失败。
    length_score = min(1.0, non_space / 400)
    base = printable_ratio * (0.25 + 0.75 * length_score)
    if expected_terms:
        found = sum(term in value for term in expected_terms)
        base *= min(1.0, found / min(3, len(expected_terms)))
    return round(max(0.0, min(100.0, base * 100)), 2)


def _page_texts(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, list):
        return (str(value or ""),)
    pages: list[str] = []
    for item in value:
        if isinstance(item, dict):
            page = item.get("text") or item.get("markdown") or item.get("content") or ""
        else:
            page = str(item or "")
        pages.append(str(page))
    return tuple(pages) or ("",)


def _run_pymupdf4llm(path: Path, *, use_ocr: bool = False) -> tuple[str, tuple[str, ...]]:
    import pymupdf4llm

    kwargs: dict[str, Any] = {"page_chunks": True, "show_progress": False, "force_text": True}
    if use_ocr:
        # 0.3.x 对 hybrid OCR 参数采用 **kwargs；不支持时在调用方重试标准路径。
        kwargs["use_ocr"] = True
    raw = pymupdf4llm.to_markdown(str(path), **kwargs)
    pages = _page_texts(raw)
    return "\n\n".join(page for page in pages if page), pages


def extract_pdf(path: str | Path, *, expected_terms: tuple[str, ...] = ()) -> DocumentQuality:
    target = Path(path)
    errors: list[str] = []
    try:
        text, pages = _run_pymupdf4llm(target)
        parser = "pymupdf4llm"
        used_ocr = False
    except Exception as exc:
        text = ""
        pages = ()
        parser = "pymupdf4llm_unavailable"
        used_ocr = False
        errors.append(f"standard:{exc}")
    score = document_quality_score(text, expected_terms=expected_terms)
    if score < 35:
        try:
            try:
                ocr_text, ocr_pages = _run_pymupdf4llm(target, use_ocr=True)
            except TypeError:
                ocr_text, ocr_pages = _run_pymupdf4llm(target, use_ocr=False)
            ocr_score = document_quality_score(ocr_text, expected_terms=expected_terms)
            if ocr_score > score:
                text, pages, score, parser = ocr_text, ocr_pages, ocr_score, "pymupdf4llm.hybrid_ocr"
                used_ocr = True
        except Exception as exc:
            errors.append(f"hybrid_ocr:{exc}")
    return DocumentQuality(
        score=score,
        text=text,
        parser=parser,
        needs_ocr=score < 35,
        needs_mineru=score < 15,
        page_count=len(pages),
        pages=pages,
        used_ocr=used_ocr,
        error="; ".join(errors)[:2000] or None,
    )


__all__ = ["DocumentQuality", "document_quality_score", "extract_pdf"]
