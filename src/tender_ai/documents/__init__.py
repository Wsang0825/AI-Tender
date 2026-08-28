"""PDF、网页与 Office 文档解析入口。"""
from tender_ai.documents.download import DOWNLOAD_ROOT, DownloadedAttachment, download_attachment, safe_filename
from tender_ai.documents.parser import DocumentPage, ParsedDocument, parse_docx, parse_html, parse_json_text, parse_mineru, parse_path, parse_pdf_document, parse_xlsx

__all__ = [
    "DOWNLOAD_ROOT", "DownloadedAttachment", "DocumentPage", "ParsedDocument", "download_attachment", "parse_docx",
    "parse_html", "parse_json_text", "parse_mineru", "parse_path", "parse_pdf_document", "parse_xlsx", "safe_filename",
]
