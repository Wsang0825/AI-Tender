"""已经核验过的公开招投标来源适配器。

固定来源优先使用公开 HTTP 页面或页面实际调用的 JSON 接口。适配器不绕过验证码、
登录和访问控制；遇到这类限制时把来源标记为需要人工关注，并保留失败证据。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from html import unescape
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from tender_ai.cache import DiskCache
from tender_ai.config_loader import APP_ROOT, RegionRegistry
from tender_ai.crawlers.http import HttpClient, HttpFetchError, HttpResponse
from tender_ai.extractors.tender import ExtractionResult, normalize_detail
from tender_ai.sources.base import AdapterHealth, SourceAdapter
from tender_ai.sources.contracts import AttachmentLink, DetailPayload, RawListingItem
from tender_ai.sources.registry import SourceDefinition
from tender_ai.status.time import now_shanghai, parse_datetime


_DATE_RE = re.compile(r"20\d{2}\s*[年./-]\s*\d{1,2}\s*[月./-]\s*\d{1,2}")
_FILE_RE = re.compile(r"\.(?:pdf|docx?|xlsx?|xls|zip|rar|7z)(?:$|[?#])", re.I)
_URL_RE = re.compile(r"https?://[^'\"\s)]+|/[^'\"\s)]+")


def _clean(value: Any) -> str:
    text = unescape(str(value or ""))
    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "lxml").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip(" \t\r\n:：;；,，")


def _absolute(base: str, value: str | None) -> str:
    if not value:
        return ""
    value = unescape(str(value)).strip()
    return urljoin(base, value)


def _parse_date(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    try:
        return parse_datetime(str(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _date_from_text(value: str) -> datetime | None:
    match = _DATE_RE.search(value or "")
    return _parse_date(match.group(0) if match else None)


def _title_from_soup(soup: BeautifulSoup, fallback: str = "") -> str:
    for selector in ("h1", ".article-title", ".detail-title", ".title", "title"):
        node = soup.select_one(selector)
        title = _clean(node.get_text(" ", strip=True)) if node else ""
        if title and title not in {"首页", "详情", "公告"}:
            return title
    return _clean(fallback)


def _visible_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return _clean(soup.get_text(" ", strip=True))


def _metadata_from_soup(soup: BeautifulSoup) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for row in soup.select("tr"):
        cells = [_clean(cell.get_text(" ", strip=True)) for cell in row.select("th,td")]
        if len(cells) < 2:
            continue
        for index in range(0, len(cells) - 1, 2):
            key, value = cells[index], cells[index + 1]
            if key and value and len(key) <= 80:
                metadata.setdefault(key, value)
                normalized_key = {
                    "项目名称": "projectname",
                    "采购项目名称": "projectname",
                    "项目编号": "projectnum",
                    "采购项目编号": "projectnum",
                    "招标编号": "tendercode",
                    "采购人": "purchaser",
                    "采购单位": "purchaser",
                    "招标人": "tenderer",
                    "招标单位": "tenderer",
                    "招标代理机构": "agency",
                    "代理机构": "agency",
                    "发布时间": "published_at",
                    "公告日期": "published_at",
                    "预算金额": "budget",
                    "采购预算": "budget",
                }.get(key)
                if normalized_key:
                    metadata.setdefault(normalized_key, value)
    return metadata


def _extract_attachments(html: str, base_url: str) -> list[AttachmentLink]:
    soup = BeautifulSoup(html or "", "lxml")
    found: dict[str, AttachmentLink] = {}
    for anchor in soup.select("a[href], a[onclick]"):
        raw = anchor.get("href") or ""
        onclick = anchor.get("onclick") or ""
        candidates = [raw]
        if onclick:
            candidates.extend(_URL_RE.findall(onclick))
        for candidate in candidates:
            candidate = candidate.strip().strip("'\"")
            if not candidate or candidate.lower().startswith(("javascript:", "#", "mailto:")):
                continue
            absolute = _absolute(base_url, candidate)
            label = _clean(anchor.get_text(" ", strip=True))
            parsed = urlparse(absolute)
            path_and_query = f"{parsed.path}?{parsed.query}".lower()
            special_download = any(token in path_and_query for token in ("/attach/", "downloadztbattach", "/download/", "attachment"))
            if not _FILE_RE.search(absolute) and not special_download:
                continue
            name = unquote(Path(parsed.path).name) or label or None
            found[absolute] = AttachmentLink(url=absolute, file_name=name)
    return list(found.values())


def _matches(item: RawListingItem, query: str) -> bool:
    terms = [term for term in re.split(r"\s+", _clean(query)) if term]
    if not terms:
        return True
    haystack = " ".join([item.title, str(item.metadata.get("content", ""))]).casefold()
    return all(term.casefold() in haystack for term in terms)


def _json_records(payload: Any) -> list[dict[str, Any]]:
    """兼容各省搜索接口外层 result/data/records 的实际返回形态。"""

    if isinstance(payload, str):
        try:
            return _json_records(json.loads(payload))
        except json.JSONDecodeError:
            return []
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("records", "data", "items", "rows", "list", "result", "content"):
        value = payload.get(key)
        if isinstance(value, list):
            records = [dict(item) for item in value if isinstance(item, dict)]
            if records:
                return records
        if isinstance(value, dict):
            records = _json_records(value)
            if records:
                return records
        if isinstance(value, str):
            records = _json_records(value)
            if records:
                return records
    return []


def _row_item(row: dict[str, Any], base_url: str, *, default_date_keys: tuple[str, ...] = ()) -> RawListingItem | None:
    title = _clean(row.get("title") or row.get("noticeTitle") or row.get("name") or row.get("projectname"))
    raw_url = row.get("linkurl") or row.get("url") or row.get("detailUrl") or row.get("href")
    url = _absolute(base_url, str(raw_url) if raw_url else None)
    if not title or not url:
        return None
    date_keys = default_date_keys or ("webdate", "infodate", "showdate", "publishdate", "published_at", "noticeTime")
    published = next((_parse_date(row.get(key)) for key in date_keys if row.get(key)), None)
    metadata = dict(row)
    metadata["published_at"] = published
    return RawListingItem(title=title, url=url, published_at=published, metadata=metadata)


class HttpSourceAdapter(SourceAdapter):
    """固定来源 HTTP 适配器基类。"""

    def __init__(self, definition: SourceDefinition):
        self.definition = definition
        self.source_id = definition.source_id
        self.cache = DiskCache(APP_ROOT.parent / "data" / "cache")
        self.http = HttpClient(cache=self.cache)
        self.regions = RegionRegistry.from_file()

    def close(self) -> None:
        self.http.close()
        self.cache.close()

    def _get(self, url: str, *, namespace: str | None = None, expire: float = 180.0, headers: dict[str, str] | None = None) -> HttpResponse:
        return self.http.get(url, headers=headers, cache_namespace=namespace or f"source:{self.source_id}:html", cache_expire=expire)

    def _detail_from_response(self, response: HttpResponse, detail_url: str) -> DetailPayload:
        soup = BeautifulSoup(response.text, "lxml")
        title = _title_from_soup(soup, detail_url)
        return DetailPayload(
            title=title,
            url=detail_url,
            html=response.text,
            text=_visible_text(response.text),
            metadata=_metadata_from_soup(soup),
            attachments=_extract_attachments(response.text, detail_url),
        )

    def fetch_detail(self, detail_url: str, **kwargs: Any) -> DetailPayload | None:
        expected_host = urlparse(self.definition.base_url).netloc.lower()
        actual_host = urlparse(detail_url).netloc.lower()
        if expected_host and actual_host and actual_host != expected_host:
            return None
        response = self._get(detail_url, namespace=f"source:{self.source_id}:detail", expire=900.0)
        return self._detail_from_response(response, detail_url)

    def fetch_attachments(self, detail: DetailPayload, **kwargs: Any) -> list[AttachmentLink]:
        return list(detail.attachments) if isinstance(detail, DetailPayload) else []

    def normalize(self, payload: Any, **kwargs: Any) -> TenderRecord | None:  # type: ignore[name-defined]
        if not isinstance(payload, DetailPayload):
            return None
        return normalize_detail(payload, self.definition, self.regions).record

    def normalize_with_evidence(self, payload: DetailPayload) -> ExtractionResult:
        return normalize_detail(payload, self.definition, self.regions)

    def health_check(self) -> AdapterHealth:
        if not self.definition.crawl_enabled:
            return AdapterHealth(self.source_id, "DISABLED", "配置未启用抓取")
        return AdapterHealth(self.source_id, "ACTIVE", "已配置公开 HTTP/API 适配器")


class CCGPAdapter(HttpSourceAdapter):
    paths = ("/cggg/dfgg/gkzb/", "/cggg/dfgg/jzxcs/")

    def _page_url(self, path: str, page: int) -> str:
        base = _absolute(self.definition.base_url, path)
        if page == 0:
            return base
        return urljoin(base, f"index_{page}.htm")

    def fetch_list(self, listing_url: str, **kwargs: Any) -> list[RawListingItem]:
        response = self._get(listing_url, namespace=f"source:{self.source_id}:list", expire=300.0)
        soup = BeautifulSoup(response.text, "lxml")
        items: list[RawListingItem] = []
        for anchor in soup.select("a[href]"):
            title = _clean(anchor.get_text(" ", strip=True))
            url = _absolute(listing_url, anchor.get("href"))
            if not title or "/cggg/" not in url or not url.lower().endswith((".htm", ".html")):
                continue
            parent = _clean(anchor.parent.get_text(" ", strip=True) if anchor.parent else "")
            item = RawListingItem(title=title, url=url, published_at=_date_from_text(parent), metadata={"content": parent})
            if item.url not in {existing.url for existing in items}:
                items.append(item)
        return items

    def search(self, query: str, **kwargs: Any) -> list[RawListingItem]:
        max_pages = int(kwargs.get("max_pages", self.definition.max_pages))
        results: list[RawListingItem] = []
        seen: set[str] = set()
        for path in self.paths:
            for page in range(max_pages):
                try:
                    items = self.fetch_list(self._page_url(path, page))
                except HttpFetchError:
                    break
                for item in items:
                    if item.url not in seen and _matches(item, query):
                        seen.add(item.url)
                        results.append(item)
        return results


class GSEIAdapter(HttpSourceAdapter):
    paths = ("/html/1336/", "/html/1662/", "/html/1657/")

    def _page_url(self, path: str, page: int) -> str:
        base = _absolute(self.definition.base_url, path)
        return base if page == 0 else urljoin(base, f"list-{page + 1}.html")

    def fetch_list(self, listing_url: str, **kwargs: Any) -> list[RawListingItem]:
        response = self._get(listing_url, namespace=f"source:{self.source_id}:list", expire=300.0)
        soup = BeautifulSoup(response.text, "lxml")
        items: list[RawListingItem] = []
        for anchor in soup.select("a[href]"):
            title = _clean(anchor.get_text(" ", strip=True))
            url = _absolute(listing_url, anchor.get("href"))
            if not title or not re.search(r"/html/\d+/\d{4}-\d{2}-\d{2}/content-\d+\.html", url, re.I):
                continue
            parent = _clean(anchor.parent.get_text(" ", strip=True) if anchor.parent else "")
            items.append(RawListingItem(title=title, url=url, published_at=_date_from_text(parent), metadata={"content": parent}))
        return items

    def search(self, query: str, **kwargs: Any) -> list[RawListingItem]:
        max_pages = int(kwargs.get("max_pages", self.definition.max_pages))
        result: list[RawListingItem] = []
        seen: set[str] = set()
        for path in self.paths:
            for page in range(max_pages):
                try:
                    items = self.fetch_list(self._page_url(path, page))
                except HttpFetchError:
                    break
                for item in items:
                    if item.url not in seen and _matches(item, query):
                        seen.add(item.url)
                        result.append(item)
        return result


class JsonSearchAdapter(HttpSourceAdapter):
    endpoint = ""
    listing_paths: tuple[str, ...] = ()
    page_size = 25

    def _category_for_url(self, listing_url: str) -> str:
        return ""

    def _build_payload(self, query: str, page: int, category: str) -> dict[str, Any]:
        raise NotImplementedError

    def _api_items(self, listing_url: str, query: str, page: int) -> list[RawListingItem]:
        self._get(listing_url, namespace=f"source:{self.source_id}:page", expire=900.0)
        category = self._category_for_url(listing_url)
        body = self._build_payload(query, page, category)
        response = self.http.post_json(
            self.endpoint,
            body,
            headers={"Referer": listing_url, "X-Requested-With": "XMLHttpRequest"},
            cache_namespace=f"source:{self.source_id}:api",
            cache_expire=300.0,
        )
        rows = _json_records(response.json())
        result: list[RawListingItem] = []
        for row in rows:
            item = _row_item(row, self.definition.base_url)
            if item:
                result.append(item)
        return result

    def fetch_list(self, listing_url: str, **kwargs: Any) -> list[RawListingItem]:
        return self._api_items(listing_url, str(kwargs.get("query", "光伏")), int(kwargs.get("page", 0)))

    def search(self, query: str, **kwargs: Any) -> list[RawListingItem]:
        max_pages = int(kwargs.get("max_pages", self.definition.max_pages))
        result: list[RawListingItem] = []
        seen: set[str] = set()
        for listing_path in self.listing_paths:
            listing_url = _absolute(self.definition.base_url, listing_path)
            for page in range(max_pages):
                try:
                    items = self.fetch_list(listing_url, query=query, page=page)
                except HttpFetchError:
                    break
                for item in items:
                    if item.url not in seen:
                        seen.add(item.url)
                        result.append(item)
        return result


class ShaanxiGGZYAdapter(JsonSearchAdapter):
    endpoint = "https://www.sxggzyjy.cn/inteligentsearch_new/rest/esinteligentsearch/getFullTextDataNew"
    page_size = 10
    listing_paths = (
        "/jydt/001001/001001001/001001001001/subPage.html",
        "/jydt/001001/001001004/001001004001/subPage.html",
    )

    def _category_for_url(self, listing_url: str) -> str:
        match = re.search(r"/(\d{12})/subPage", listing_url)
        return match.group(1) if match else "001001001001"

    def _build_payload(self, query: str, page: int, category: str) -> dict[str, Any]:
        return {
            "esdsid": "1", "token": "", "pn": page * self.page_size, "rn": self.page_size,
            "sdt": "", "edt": "", "wd": query, "inc_wd": "", "exc_wd": "",
            "fields": "title", "cnum": "001", "sort": '{"webdate":"0"}', "ssort": "title",
            "cl": 1000, "cutIngore": "title;linkurl", "terminal": "",
            "condition": [{"fieldName": "categorynum", "equal": category, "isLike": True, "likeType": 2}],
            "time": [], "highlights": "title", "statistics": None, "unionCondition": None,
            "accuracy": "", "noParticiple": "1", "searchRange": [], "isBusiness": "1",
        }


class QinghaiGGZYAdapter(JsonSearchAdapter):
    endpoint = "https://www.qhggzyjy.gov.cn/inteligentsearch/rest/inteligentSearch/getFullTextData"
    listing_paths = (
        "/ggzy/jyxx/001001/001001001/transinfo_list.html",
        "/ggzy/jyxx/001002/001002001/transinfo_list.html",
    )

    def _category_for_url(self, listing_url: str) -> str:
        match = re.search(r"/(\d{9})/transinfo", listing_url)
        return match.group(1) if match else "001001001"

    def _build_payload(self, query: str, page: int, category: str) -> dict[str, Any]:
        return {
            "token": "", "pn": page * self.page_size, "rn": self.page_size, "sdt": "", "edt": "",
            "wd": quote(query, safe=""), "fields": "title", "cnum": "001;002;003;004;005;006;007;008;009;010",
            "sort": '{"showdate":"0"}', "ssort": "title", "cl": 200, "terminal": "",
            "condition": [{"fieldName": "categorynum", "isLike": True, "likeType": 2, "equal": category}],
            "time": None, "highlights": "title", "statistics": None, "unionCondition": None,
            "accuracy": "100", "noParticiple": "0", "isBusiness": "1",
        }

    def fetch_detail(self, detail_url: str, **kwargs: Any) -> DetailPayload | None:
        payload = super().fetch_detail(detail_url, **kwargs)
        if payload:
            payload.attachments = _extract_attachments(payload.html, detail_url)
        return payload


class XinjiangGGZYAdapter(JsonSearchAdapter):
    endpoint = "https://ggzy.xinjiang.gov.cn/inteligentsearchnew/rest/esinteligentsearch/getFullTextDataNew"
    listing_paths = ("/xinjiangggzy_new/jyxx/001001/trade_info.html",)

    def _category_for_url(self, listing_url: str) -> str:
        return "001001"

    def _build_payload(self, query: str, page: int, category: str) -> dict[str, Any]:
        return {
            "esdsid": "1", "token": "", "pn": page * self.page_size, "rn": self.page_size,
            "sdt": "", "edt": "", "wd": quote(query, safe=""), "inc_wd": "", "exc_wd": "",
            "fields": "title", "cnum": "001", "sort": '{"webdate":"0"}', "ssort": "title",
            "cl": 1000, "cutIngore": "title;linkurl", "terminal": "",
            "condition": [{"fieldName": "categorynum", "equal": category, "isLike": True, "likeType": 2}],
            "time": [], "highlights": "title", "statistics": None, "unionCondition": None,
            "accuracy": "", "noParticiple": "1", "searchRange": [], "isBusiness": "1",
        }

    def _row_item(self, row: dict[str, Any]) -> RawListingItem | None:
        item = _row_item(row, self.definition.base_url)
        if not item:
            return None
        if item.url.startswith("https://ggzy.xinjiang.gov.cn/jyxx/"):
            item = RawListingItem(item.title, item.url.replace("/jyxx/", "/xinjiangggzy_new/jyxx/", 1), item.published_at, item.metadata)
        return item

    def _api_items(self, listing_url: str, query: str, page: int) -> list[RawListingItem]:
        self._get(listing_url, namespace=f"source:{self.source_id}:page", expire=900.0)
        body = self._build_payload(query, page, self._category_for_url(listing_url))
        response = self.http.post_json(self.endpoint, body, headers={"Referer": listing_url, "X-Requested-With": "XMLHttpRequest"}, cache_namespace=f"source:{self.source_id}:api", cache_expire=300.0)
        result: list[RawListingItem] = []
        for row in _json_records(response.json()):
            item = self._row_item(row)
            if item:
                result.append(item)
        return result


class BingtuanGGZYAdapter(JsonSearchAdapter):
    endpoint = "https://ggzy.xjbt.gov.cn/inteligentsearch/rest/esinteligentsearch/getFullTextDataNew"
    page_size = 15
    listing_paths = (
        "/jygk/004001/construction_project.html",
        "/jygk/004002/construction_project.html",
    )

    def _category_for_url(self, listing_url: str) -> str:
        match = re.search(r"/(00400[12])/", listing_url)
        return match.group(1) if match else "004001"

    def _build_payload(self, query: str, page: int, category: str) -> dict[str, Any]:
        return {
            "esdsid": "1", "token": "", "pn": page * self.page_size, "rn": self.page_size,
            "sdt": "1970-01-01 00:00:00", "edt": "2999-12-31 23:59:59", "wd": quote(query, safe=""), "inc_wd": "", "exc_wd": "",
            "fields": "title;content", "cnum": "001", "sort": '{"webdate":"0"}', "ssort": "title",
            "cl": 500, "terminal": "",
            "condition": [{"fieldName": "categorynum", "equal": category, "notEqual": None, "equalList": None, "notEqualList": None, "isLike": True, "likeType": 2}],
            "time": "", "highlights": "", "statistics": None, "unionCondition": [],
            "accuracy": "100", "noParticiple": "1", "searchRange": None, "isBusiness": "1",
        }


class NingxiaGGZYAdapter(JsonSearchAdapter):
    endpoint = "https://ggzyjy.fzggw.nx.gov.cn/interface_wz/rest/esinteligentsearch/getFullTextDataNew"
    listing_paths = (
        "/dzjy/001001/001001001/trade_infomation.html",
        "/dzjy/001001/001001002/trade_infomation.html",
    )

    def _category_for_url(self, listing_url: str) -> str:
        match = re.search(r"/(00100100[12])/trade", listing_url)
        return match.group(1) if match else "001001001"

    def _build_payload(self, query: str, page: int, category: str) -> dict[str, Any]:
        return {
            "token": "", "pn": page * self.page_size, "rn": self.page_size, "sdt": "", "edt": "",
            "wd": quote(query, safe=""), "fields": "title", "cnum": "", "sort": '{"webdate":"0","id":"0"}',
            "ssort": "title", "cl": 10000, "terminal": "",
            "condition": [{"fieldName": "categorynum", "equal": category, "isLike": True, "likeType": 2}],
            "time": None, "highlights": "title", "statistics": None, "unionCondition": [],
            "accuracy": "", "noParticiple": "0", "searchRange": [], "isBusiness": "1", "noWd": True,
        }


class NingxiaGovernmentPurchaseAdapter(HttpSourceAdapter):
    paths = (
        "contents/CGGG/ZBGG/index.jsp?cid=2028&sid=2000",
        "contents/CGGG/ZHBGG/index.jsp?cid=2029&sid=2000",
        "contents/CGGG/GZGG/index.jsp?cid=2027&sid=2000",
    )

    def _page_url(self, path: str, page: int) -> str:
        url = _absolute(self.definition.base_url, path)
        return url if page == 0 else f"{url}&page={page}"

    def fetch_list(self, listing_url: str, **kwargs: Any) -> list[RawListingItem]:
        response = self._get(listing_url, namespace=f"source:{self.source_id}:list", expire=300.0)
        soup = BeautifulSoup(response.text, "lxml")
        result: list[RawListingItem] = []
        for anchor in soup.select("a[href]"):
            href = anchor.get("href", "")
            title = _clean(anchor.get_text(" ", strip=True))
            if not title or "content.jsp" not in href:
                continue
            url = _absolute(listing_url, href)
            parent = _clean(anchor.parent.get_text(" ", strip=True) if anchor.parent else "")
            result.append(RawListingItem(title=title, url=url, published_at=_date_from_text(parent), metadata={"content": parent}))
        return result

    def search(self, query: str, **kwargs: Any) -> list[RawListingItem]:
        max_pages = int(kwargs.get("max_pages", self.definition.max_pages))
        result: list[RawListingItem] = []
        seen: set[str] = set()
        for path in self.paths:
            for page in range(max_pages):
                try:
                    items = self.fetch_list(self._page_url(path, page))
                except HttpFetchError:
                    break
                for item in items:
                    if item.url not in seen and _matches(item, query):
                        seen.add(item.url)
                        result.append(item)
        return result


class NationalGGZYAdapter(HttpSourceAdapter):
    endpoint = "https://www.ggzy.gov.cn/information/pubTradingInfo/getTradList"

    def fetch_list(self, listing_url: str, **kwargs: Any) -> list[RawListingItem]:
        page = int(kwargs.get("page", 1))
        query = str(kwargs.get("query", "光伏"))
        payload = {
            "DEAL_CLASSIFY": "01", "DEAL_TIME": "02", "PAGENUMBER": str(page),
            "PROVINCE": "", "CITY": "", "DEAL_STAGE": "", "DEAL_TYPE": "",
            "KEYWORD": query,
        }
        response = self.http.request("POST", self.endpoint, data=payload, headers={"Referer": listing_url, "X-Requested-With": "XMLHttpRequest"}, cache_namespace=f"source:{self.source_id}:api", cache_expire=300.0)
        rows = _json_records(response.json())
        return [item for row in rows if (item := _row_item(row, self.definition.base_url))]

    def search(self, query: str, **kwargs: Any) -> list[RawListingItem]:
        max_pages = int(kwargs.get("max_pages", self.definition.max_pages))
        result: list[RawListingItem] = []
        seen: set[str] = set()
        listing_url = _absolute(self.definition.base_url, "/deal/dealList.html?HEADER_DEAL_TYPE=01")
        self._get(listing_url, namespace=f"source:{self.source_id}:page", expire=900.0)
        for page in range(1, max_pages + 1):
            try:
                items = self.fetch_list(listing_url, query=query, page=page)
            except HttpFetchError:
                break
            for item in items:
                if item.url not in seen and _matches(item, query):
                    seen.add(item.url)
                    result.append(item)
        return result


class CEBPubServiceAdapter(HttpSourceAdapter):
    """中国招标投标公共服务平台 bulletin 搜索页。

    列表中的链接由页面 JavaScript 以短 ID 打开，若当前平台未提供可直接访问的详情 URL，
    仍保存检索结果和短 ID，后续可通过页面脚本升级详情解析，不伪造详情地址。
    """

    categories = ("bulletin", "change")

    def _list_url(self, category: str, query: str, page: int, days: int = 30) -> str:
        return (
            f"https://bulletin.cebpubservice.com/xxfbcmses/search/{category}.html?"
            f"searchDate={datetime.now().date().isoformat()}&dates={days}&categoryId=88&industryName="
            f"&area=&status=&publishMedia=&sourceInfo=&showStatus=&word={quote(query, safe='')}&page={page}"
        )

    def fetch_list(self, listing_url: str, **kwargs: Any) -> list[RawListingItem]:
        response = self._get(listing_url, namespace=f"source:{self.source_id}:list", expire=300.0)
        soup = BeautifulSoup(response.text, "lxml")
        result: list[RawListingItem] = []
        for anchor in soup.select("a[href]"):
            title = _clean(anchor.get_text(" ", strip=True))
            href = anchor.get("href", "")
            if not title or "urlOpen" not in href:
                continue
            match = re.search(r"urlOpen\(['\"]([^'\"]+)", href)
            if not match:
                continue
            short_id = match.group(1)
            parent = _clean(anchor.parent.get_text(" ", strip=True) if anchor.parent else "")
            detail_url = f"https://ctbpsp.com/#/bulletinDetail?uuid={short_id}&inpvalue=&dataSource=0&tenderAgency="
            result.append(RawListingItem(title=title, url=detail_url, published_at=_date_from_text(parent), metadata={"content": parent, "ceb_id": short_id, "listing_url": listing_url}))
        return result

    def search(self, query: str, **kwargs: Any) -> list[RawListingItem]:
        max_pages = int(kwargs.get("max_pages", self.definition.max_pages))
        result: list[RawListingItem] = []
        seen: set[str] = set()
        for category in self.categories:
            for page in range(1, max_pages + 1):
                url = self._list_url(category, query, page, int(kwargs.get("since_days", 30)))
                try:
                    items = self.fetch_list(url)
                except HttpFetchError:
                    break
                for item in items:
                    if item.url not in seen:
                        seen.add(item.url)
                        result.append(item)
        return result

    def fetch_detail(self, detail_url: str, **kwargs: Any) -> DetailPayload | None:
        # hash 只是页面 JavaScript 的内部 ID，不将猜测出的 URL 当作事实来源。
        return super().fetch_detail(detail_url, **kwargs)

    def health_check(self) -> AdapterHealth:
        if not self.definition.crawl_enabled:
            return AdapterHealth(self.source_id, "DISABLED", "配置未启用抓取")
        return AdapterHealth(self.source_id, "ACTIVE", "公开检索页可访问；详情使用页面短 ID 保留")


class PowerChinaAdapter(HttpSourceAdapter):
    """中国电建阳光采购网公开公告 API。

    该站首页是 Vue 应用，但公告列表和详情实际由公开 JSON 接口提供。
    这里直接使用已核验的接口，不启动浏览器，也不猜测前端渲染后的详情地址。
    """

    api_base = "https://bid.powerchina.cn/newcbs/recpro-newmember"
    list_endpoint = f"{api_base}/BidAnnouncementSummary/list"
    detail_endpoint = f"{api_base}/BidAnnouncementSummary/getInfo"
    pdf_endpoint = f"{api_base}/BidAnnouncementSummary/downloadPdf"
    listing_path = "/consult/notice"
    page_size = 20

    @staticmethod
    def _detail_url(item_id: str) -> str:
        return f"https://bid.powerchina.cn/notice/detail?id={quote(item_id, safe='')}"

    @staticmethod
    def _metadata(row: dict[str, Any]) -> dict[str, Any]:
        """把 API 字段映射到统一 Extractor 能识别的字段名。"""

        metadata = dict(row)
        metadata.update(
            {
                "projectname": row.get("projectName") or row.get("projectname"),
                "projectnum": row.get("projectNumber") or row.get("projectNum"),
                "tendercode": row.get("tenderCode") or row.get("tenderNumber"),
                "owner": row.get("procuringEntity") or row.get("tenderer"),
                "purchaser": row.get("procuringEntity") or row.get("purchaser"),
                "published_at": row.get("publishTime") or row.get("createTime"),
                "registration_deadline": row.get("registrationDeadline"),
                "bid_deadline": row.get("submissionDeadline"),
                "open_time": row.get("bidOpenTime"),
                "powerchina_id": row.get("id"),
            }
        )
        return metadata

    def _build_payload(self, query: str, page: int, since_days: int) -> dict[str, Any]:
        cutoff = now_shanghai() - timedelta(days=max(1, since_days))
        return {
            "pageNum": page,
            "pageSize": self.page_size,
            "announcementType": "招采公告",
            "companyType": "3",
            "keyWords": query,
            "publishTime": cutoff.strftime("%Y-%m-%d"),
            "publishTimeType": "1",
            "time": int(now_shanghai().timestamp() * 1000),
        }

    def fetch_list(self, listing_url: str, **kwargs: Any) -> list[RawListingItem]:
        query = str(kwargs.get("query") or "")
        page = max(1, int(kwargs.get("page", 1)))
        since_days = max(1, int(kwargs.get("since_days", self.definition.lookback_days)))
        response = self.http.post_json(
            self.list_endpoint,
            self._build_payload(query, page, since_days),
            headers={"Referer": "https://bid.powerchina.cn/", "X-Requested-With": "XMLHttpRequest"},
            cache_namespace=f"source:{self.source_id}:api:list",
            cache_expire=300.0,
        )
        rows = _json_records(response.json())
        items: list[RawListingItem] = []
        for row in rows:
            item_id = str(row.get("id") or row.get("systemId") or "").strip()
            title = _clean(row.get("title") or row.get("projectName"))
            if not item_id or not title:
                continue
            published = _parse_date(row.get("publishTime") or row.get("createTime"))
            metadata = self._metadata(row)
            metadata["content"] = title
            items.append(
                RawListingItem(
                    title=title,
                    url=self._detail_url(item_id),
                    published_at=published,
                    metadata=metadata,
                )
            )
        return items

    def search(self, query: str, **kwargs: Any) -> list[RawListingItem]:
        max_pages = max(1, int(kwargs.get("max_pages", self.definition.max_pages)))
        since_days = max(1, int(kwargs.get("since_days", self.definition.lookback_days)))
        result: list[RawListingItem] = []
        seen: set[str] = set()
        listing_url = _absolute(self.definition.base_url, self.listing_path)
        for page in range(1, max_pages + 1):
            items = self.fetch_list(listing_url, query=query, page=page, since_days=since_days)
            for item in items:
                if item.url not in seen and _matches(item, query):
                    seen.add(item.url)
                    result.append(item)
        return result

    def fetch_detail(self, detail_url: str, **kwargs: Any) -> DetailPayload | None:
        parsed = parse_qs(urlparse(detail_url).query)
        item_id = (parsed.get("id") or [""])[0].strip()
        if not item_id:
            return super().fetch_detail(detail_url, **kwargs)
        response = self.http.get(
            f"{self.detail_endpoint}/{quote(item_id, safe='')}",
            headers={"Referer": "https://bid.powerchina.cn/"},
            cache_namespace=f"source:{self.source_id}:api:detail",
            cache_expire=900.0,
        )
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise HttpFetchError(
                "中国电建详情 API 未返回公告数据",
                url=response.url,
                status_code=response.status_code,
                health_reason="PARSER_ERROR",
            )
        html = str(data.get("announcementContent") or "")
        visible = _visible_text(html) if html else ""
        metadata = self._metadata(data)
        metadata["content"] = visible
        prefix_parts = [
            f"发布日期：{metadata.get('published_at') or ''}",
            f"报名截止时间：{metadata.get('registration_deadline') or ''}",
            f"投标截止时间：{metadata.get('bid_deadline') or ''}",
            f"开标时间：{metadata.get('open_time') or ''}",
        ]
        text = " ".join(part for part in prefix_parts if not part.endswith("："))
        text = f"{text} {visible}".strip()
        attachments = _extract_attachments(html, detail_url)
        if data.get("pdfId") or data.get("id"):
            pdf_url = f"{self.pdf_endpoint}?id={quote(str(data.get('id') or item_id), safe='')}"
            if not any(link.url == pdf_url for link in attachments):
                attachments.append(AttachmentLink(url=pdf_url, file_name=f"powerchina_{item_id}.pdf", mime_type="application/pdf"))
        return DetailPayload(
            title=_clean(data.get("title") or data.get("projectName") or item_id),
            url=detail_url,
            html=html,
            text=text,
            metadata=metadata,
            attachments=attachments,
        )


class ChnEnergyEZhaoAdapter(HttpSourceAdapter):
    """国能 e 招公开公告适配器。

    国能 e 招的公开公告列表是静态 HTML 分页，详情页和附件链接也在同一公开
    域名下。这里按实际页面路径抓取，不把登录后的投标操作误认为公开数据。
    """

    category_paths = (
        # 资格预审、招标公告、非招标公告和变更公告。
        "/bidweb/001/001001/001001001/moreinfo.html",
        "/bidweb/001/001001/001001002/moreinfo.html",
        "/bidweb/001/001001/001001003/moreinfo.html",
        "/bidweb/001/001002/001002001/moreinfo.html",
        "/bidweb/001/001002/001002002/moreinfo.html",
        "/bidweb/001/001002/001002003/moreinfo.html",
        "/bidweb/001/001003/001003001/moreinfo.html",
        "/bidweb/001/001003/001003002/moreinfo.html",
        "/bidweb/001/001003/001003003/moreinfo.html",
        "/bidweb/001/001004/001004001/moreinfo.html",
        "/bidweb/001/001004/001004002/moreinfo.html",
        "/bidweb/001/001004/001004003/moreinfo.html",
    )

    @staticmethod
    def _page_url(path: str, page: int) -> str:
        if page <= 1:
            return path
        parsed = urlparse(path)
        parent = parsed.path.rsplit("/", 1)[0].rstrip("/")
        suffix = f"/{page}.html"
        return f"{parent}{suffix}" + (f"?{parsed.query}" if parsed.query else "")

    @staticmethod
    def _list_item(anchor: Any, listing_url: str) -> RawListingItem | None:
        title = _clean(anchor.get("title") or anchor.get_text(" ", strip=True))
        url = _absolute(listing_url, anchor.get("href"))
        if not title or not url or not re.search(r"/bidweb/", url, re.I):
            return None
        container = anchor.find_parent("li") or anchor.parent
        context = _clean(container.get_text(" ", strip=True) if container else title)
        author = container.select_one(".author") if container else None
        code = _clean(author.get_text(" ", strip=True)) if author else ""
        metadata: dict[str, Any] = {"content": context, "tendercode": code or None, "category_url": listing_url}
        return RawListingItem(
            title=title,
            url=url,
            published_at=_date_from_text(context),
            metadata=metadata,
        )

    def fetch_list(self, listing_url: str, **kwargs: Any) -> list[RawListingItem]:
        response = self._get(
            listing_url,
            namespace=f"source:{self.source_id}:list",
            expire=300.0,
            headers={"Referer": self.definition.base_url},
        )
        soup = BeautifulSoup(response.text, "lxml")
        result: list[RawListingItem] = []
        seen: set[str] = set()
        anchors = soup.select(".right-items a.infolink, .right-item a.infolink, a.infolink")
        for anchor in anchors:
            item = self._list_item(anchor, listing_url)
            if item is not None and item.url not in seen:
                seen.add(item.url)
                result.append(item)
        return result

    def search(self, query: str, **kwargs: Any) -> list[RawListingItem]:
        max_pages = max(1, int(kwargs.get("max_pages", self.definition.max_pages)))
        result: list[RawListingItem] = []
        seen: set[str] = set()
        for category_path in self.category_paths:
            for page in range(1, max_pages + 1):
                listing_url = self._page_url(category_path, page)
                try:
                    items = self.fetch_list(listing_url)
                except HttpFetchError:
                    # 一个公告栏目失败不能吞掉其他栏目；上层会保留具体错误。
                    break
                for item in items:
                    if item.url not in seen and _matches(item, query):
                        seen.add(item.url)
                        result.append(item)
        return result


class DatangAdapter(HttpSourceAdapter):
    """大唐公开公告接口适配器（cweme.cn）。"""

    api_base = "https://www.cweme.cn/cweme-index/indexController"
    list_endpoint = f"{api_base}/getList"
    detail_endpoint = f"{api_base}/fzggDetail"
    detail_page = "https://www.cweme.cn/cweme-index/webpage/jsp/zbggDetail.jsp"
    page_size = 10

    @staticmethod
    def _detail_url(item_id: str) -> str:
        return f"https://www.cweme.cn/cweme-index/webpage/jsp/zbggDetail.jsp?id={quote(item_id, safe='')}"

    @staticmethod
    def _metadata(row: dict[str, Any]) -> dict[str, Any]:
        metadata = dict(row)
        metadata.update(
            {
                "projectname": row.get("projectname") or row.get("project_name") or row.get("message_title"),
                "projectnum": row.get("projectnum") or row.get("project_no") or row.get("message_no"),
                "tendercode": row.get("tendercode") or row.get("tender_no") or row.get("message_no"),
                "owner": row.get("bid_tenderer") or row.get("tenderer") or row.get("owner"),
                "purchaser": row.get("bid_tenderer") or row.get("purchaser"),
                "tenderer": row.get("bid_tenderer") or row.get("tenderer"),
                "published_at": row.get("publish_time") or row.get("published_at"),
                "bid_deadline": row.get("deadline") or row.get("bid_deadline"),
                "open_time": row.get("open_time"),
            }
        )
        return metadata

    def _post_form(self, endpoint: str, payload: dict[str, Any], *, namespace: str, expire: float) -> HttpResponse:
        return self.http.request(
            "POST",
            endpoint,
            data=payload,
            headers={
                "Referer": "https://www.cweme.cn/cweme-index/webpage/jsp/zbggList.jsp",
                "X-Requested-With": "XMLHttpRequest",
            },
            cache_namespace=namespace,
            cache_expire=expire,
        )

    def fetch_list(self, listing_url: str, **kwargs: Any) -> list[RawListingItem]:
        query = str(kwargs.get("query") or "")
        page = max(1, int(kwargs.get("page", 1)))
        since_days = max(1, int(kwargs.get("since_days", self.definition.lookback_days)))
        cutoff = now_shanghai() - timedelta(days=since_days)
        message_type = str(kwargs.get("message_type") or "0")
        payload = {
            "limit": str(self.page_size),
            "page": str(page),
            "messagetype": message_type,
            "message_title": query,
            "bid_tenderer": "",
            "message_no": "",
            "pro_bidding_mothod": "",
            "startDate": cutoff.strftime("%Y-%m-%d"),
            "endDate": now_shanghai().strftime("%Y-%m-%d"),
        }
        response = self._post_form(self.list_endpoint, payload, namespace=f"source:{self.source_id}:api:list", expire=300.0)
        rows = _json_records(response.json())
        result: list[RawListingItem] = []
        for row in rows:
            item_id = str(row.get("id") or row.get("gg_id") or "").strip()
            title = _clean(row.get("message_title") or row.get("title"))
            if not item_id or not title:
                continue
            published = _parse_date(row.get("publish_time") or row.get("published_at"))
            metadata = self._metadata(row)
            metadata["content"] = _clean(" ".join(str(row.get(key) or "") for key in ("message_title", "message_no", "bid_tenderer", "pro_bidding_mothod", "deadline")))
            result.append(RawListingItem(title=title, url=self._detail_url(item_id), published_at=published, metadata=metadata))
        return result

    def search(self, query: str, **kwargs: Any) -> list[RawListingItem]:
        max_pages = max(1, int(kwargs.get("max_pages", self.definition.max_pages)))
        since_days = max(1, int(kwargs.get("since_days", self.definition.lookback_days)))
        result: list[RawListingItem] = []
        seen: set[str] = set()
        # 0=招标公告，1=变更/补充类公告；变更不能被普通搜索遗漏。
        for message_type in ("0", "1"):
            for page in range(1, max_pages + 1):
                items = self.fetch_list(
                    self.detail_page,
                    query=query,
                    page=page,
                    since_days=since_days,
                    message_type=message_type,
                )
                for item in items:
                    if item.url not in seen and _matches(item, query):
                        seen.add(item.url)
                        result.append(item)
        return result

    def fetch_detail(self, detail_url: str, **kwargs: Any) -> DetailPayload | None:
        item_id = (parse_qs(urlparse(detail_url).query).get("id") or [""])[0].strip()
        if not item_id:
            return super().fetch_detail(detail_url, **kwargs)
        response = self._post_form(self.detail_endpoint, {"id": item_id}, namespace=f"source:{self.source_id}:api:detail", expire=900.0)
        payload = response.json()
        # 当前接口直接返回公告对象；部分部署版本会再包一层 data。
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) and isinstance(payload, dict) and (payload.get("id") or payload.get("message_title")):
            data = payload
        if not isinstance(data, dict):
            raise HttpFetchError(
                "大唐详情接口未返回公告数据",
                url=response.url,
                status_code=response.status_code,
                health_reason="PARSER_ERROR",
            )
        metadata = self._metadata(data)
        content_candidates = (
            data.get("message_content"), data.get("content"), data.get("pro_overvier"),
            data.get("project_overview"), data.get("pro_quali_examin"), data.get("qualification"),
        )
        content = next((_clean(value) for value in content_candidates if _clean(value)), "")
        html_content = str(next((value for value in content_candidates if isinstance(value, str) and "<" in value and ">" in value), "") or "")
        visible = _visible_text(html_content) if html_content else content
        prefix = " ".join(
            f"{label}：{_clean(data.get(key))}"
            for label, key in (("发布日期", "publish_time"), ("投标截止时间", "deadline"), ("项目编号", "message_no"), ("招标人", "bid_tenderer"))
            if _clean(data.get(key))
        )
        text = f"{prefix} {visible}".strip()
        attachments = _extract_attachments(html_content, detail_url) if html_content else []
        pdf_url = _absolute(detail_url, str(data.get("pdf_url") or ""))
        if pdf_url and not any(link.url == pdf_url for link in attachments):
            parsed = urlparse(pdf_url)
            name = unquote(Path(parsed.path).name) or f"datang_{item_id}.pdf"
            attachments.append(AttachmentLink(url=pdf_url, file_name=name, mime_type="application/pdf"))
        metadata["content"] = text
        return DetailPayload(
            title=_clean(data.get("message_title") or data.get("title") or item_id),
            url=detail_url,
            html=html_content,
            text=text,
            metadata=metadata,
            attachments=attachments,
        )


class ShanxiChangzhiGGZYAdapter(HttpSourceAdapter):
    """山西长治市公共资源交易中心公开 HTML 适配器。"""

    listing_paths = (
        "/front/notice/list?type=ZBGG&xmlx=JSGC",
        "/front/notice/list?type=ZBGG&xmlx=ZFCG",
    )

    def fetch_list(self, listing_url: str, **kwargs: Any) -> list[RawListingItem]:
        response = self._get(listing_url, namespace=f"source:{self.source_id}:list", expire=300.0)
        soup = BeautifulSoup(response.text, "lxml")
        result: list[RawListingItem] = []
        seen: set[str] = set()
        for anchor in soup.select("a[href*='/front/notice/detail']"):
            title = _clean(anchor.get("title") or anchor.get_text(" ", strip=True))
            url = _absolute(listing_url, anchor.get("href"))
            if not title or not url or url in seen:
                continue
            container = anchor.find_parent("li") or anchor.parent
            context = _clean(container.get_text(" ", strip=True) if container else title)
            seen.add(url)
            result.append(RawListingItem(title=title, url=url, published_at=_date_from_text(context), metadata={"content": context}))
        return result

    def search(self, query: str, **kwargs: Any) -> list[RawListingItem]:
        max_pages = max(1, int(kwargs.get("max_pages", self.definition.max_pages)))
        result: list[RawListingItem] = []
        seen: set[str] = set()
        for path in self.listing_paths:
            for page in range(1, max_pages + 1):
                # 当前公开页面的第一页已核验；不猜测分页参数，避免把错误 URL 当作覆盖。
                if page > 1:
                    break
                for item in self.fetch_list(_absolute(self.definition.base_url, path)):
                    if item.url not in seen and _matches(item, query):
                        seen.add(item.url)
                        result.append(item)
        return result


class ShaanxiGovernmentPurchaseAdapter(HttpSourceAdapter):
    listing_url = "https://www.ccgp-shaanxi.gov.cn/cms-sx/site/shanxi/xxgg/index.html?result=result"
    endpoint = "https://www.ccgp-shaanxi.gov.cn/freecms/rest/v1/notice/selectInfoMoreChannel.do"

    def fetch_list(self, listing_url: str, **kwargs: Any) -> list[RawListingItem]:
        self._get(listing_url, namespace=f"source:{self.source_id}:page", expire=300.0)
        raise HttpFetchError("陕西政府采购公开接口要求验证码，未绕过验证码", url=self.endpoint, status_code=200)

    def search(self, query: str, **kwargs: Any) -> list[RawListingItem]:
        return self.fetch_list(self.listing_url, query=query)

    def health_check(self) -> AdapterHealth:
        return AdapterHealth(self.source_id, "NEEDS_ATTENTION", "公开列表接口当前要求动态验证码，未绕过")


class GansuGGZYAdapter(HttpSourceAdapter):
    """甘肃公共资源交易平台入口适配器；入口若返回 412/拦截则独立降级。"""

    def fetch_list(self, listing_url: str, **kwargs: Any) -> list[RawListingItem]:
        response = self._get(listing_url, namespace=f"source:{self.source_id}:list", expire=300.0)
        soup = BeautifulSoup(response.text, "lxml")
        result: list[RawListingItem] = []
        for anchor in soup.select("a[href]"):
            title = _clean(anchor.get_text(" ", strip=True))
            url = _absolute(listing_url, anchor.get("href"))
            if title and url and any(word in title for word in ("光伏", "储能", "新能源")):
                result.append(RawListingItem(title=title, url=url, published_at=_date_from_text(_clean(anchor.parent.get_text(" ", strip=True) if anchor.parent else ""))))
        return result

    def search(self, query: str, **kwargs: Any) -> list[RawListingItem]:
        return self.fetch_list(self.definition.base_url, query=query)


def build_adapter(definition: SourceDefinition) -> SourceAdapter:
    if definition.adapter_level.upper() == "CUSTOM_BROWSER":
        return CustomBrowserAdapter(definition)
    if definition.adapter_level.upper() == "GENERIC_HTML" or definition.adapter.startswith("generic:"):
        from tender_ai.sources.generic import GenericSourceAdapter

        return GenericSourceAdapter(definition)
    mapping: dict[str, type[SourceAdapter]] = {
        "ccgp": CCGPAdapter,
        "ggzy_national": NationalGGZYAdapter,
        "cebpubservice": CEBPubServiceAdapter,
        "shaanxi_ggzy": ShaanxiGGZYAdapter,
        "ccgp_shaanxi": ShaanxiGovernmentPurchaseAdapter,
        "gansu_ggzy": GansuGGZYAdapter,
        "gsei": GSEIAdapter,
        "qinghai_ggzy": QinghaiGGZYAdapter,
        "ningxia_ggzy": NingxiaGGZYAdapter,
        "ccgp_ningxia": NingxiaGovernmentPurchaseAdapter,
        "xinjiang_ggzy": XinjiangGGZYAdapter,
        "bingtuan_ggzy": BingtuanGGZYAdapter,
        "powerchina": PowerChinaAdapter,
        "chnenergy_e_zhao": ChnEnergyEZhaoAdapter,
        "datang": DatangAdapter,
        "shanxi_changzhi_ggzy": ShanxiChangzhiGGZYAdapter,
    }
    adapter_type = mapping.get(definition.adapter, ConfiguredSourceAdapter)
    return adapter_type(definition)  # type: ignore[call-arg]


class ConfiguredSourceAdapter(SourceAdapter):
    def __init__(self, definition: SourceDefinition):
        self.definition = definition
        self.source_id = definition.source_id

    def search(self, query: str, **kwargs: Any) -> list[Any]:
        return []

    def fetch_list(self, listing_url: str, **kwargs: Any) -> list[Any]:
        return []

    def fetch_detail(self, detail_url: str, **kwargs: Any) -> Any | None:
        return None

    def fetch_attachments(self, detail: Any, **kwargs: Any) -> list[Any]:
        return []

    def normalize(self, payload: Any, **kwargs: Any) -> Any | None:
        return None

    def health_check(self) -> AdapterHealth:
        return AdapterHealth(self.source_id, self.definition.status, "来源已登记但尚未配置适配器")


class CustomBrowserAdapter(ConfiguredSourceAdapter):
    """浏览器三级适配器的安全占位；只有站点确实需要 JS 时才启用。"""

    def health_check(self) -> AdapterHealth:
        return AdapterHealth(self.source_id, "NEEDS_ATTENTION", "此来源标记为 Custom Browser，尚未自动启动浏览器")


__all__ = [
    "BingtuanGGZYAdapter", "CCGPAdapter", "CEBPubServiceAdapter", "ChnEnergyEZhaoAdapter", "ConfiguredSourceAdapter", "CustomBrowserAdapter",
    "DatangAdapter", "GSEIAdapter", "GansuGGZYAdapter", "HttpSourceAdapter", "NationalGGZYAdapter", "PowerChinaAdapter",
    "NingxiaGGZYAdapter", "NingxiaGovernmentPurchaseAdapter", "QinghaiGGZYAdapter",
    "ShaanxiGGZYAdapter", "ShaanxiGovernmentPurchaseAdapter", "ShanxiChangzhiGGZYAdapter", "XinjiangGGZYAdapter",
    "build_adapter",
]
