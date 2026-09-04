"""Discovery 中“有价值但不是直接组件支架采购”的线索识别。

这类结果不能被当作当前支架采购公告，也不能因为不满足严格的招标关键词
而被丢弃。这里使用可审计的关键词规则做分类；最终项目事实仍然必须以
官方公告、附件和 Evidence 为准。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from tender_ai.discovery.contracts import SearchResult


_ENERGY_TERMS = (
    "光伏",
    "太阳能",
    "风电",
    "储能",
    "新能源",
    "光热",
)
_PROJECT_TERMS = (
    "项目",
    "工程",
    "基地",
    "电站",
    "一体化",
    "MW",
    "MWh",
)
_DIRECT_RACK_TERMS = (
    "光伏支架采购",
    "光伏支架招标",
    "组件支架采购",
    "组件支架招标",
    "支架采购招标",
    "支架供货",
    "支架类采购",
)
_ADJACENT_STRUCTURE_TERMS = (
    "箱变用钢结构平台",
    "箱变钢结构平台",
    "箱变平台",
    "变压器平台",
    "设备钢平台",
    "钢结构平台采购",
    "设备平台采购",
)
_COMPLETED_TERMS = (
    "竣工验收",
    "完工验收",
    "已竣工",
    "已完工",
    "已投产",
    "投产发电",
    "建成投运",
    "已建成",
    "全容量并网",
    "正式投产",
)
_INSTALLATION_TERMS = (
    "支架安装",
    "支架施工",
    "支架已安装",
    "安装支架",
)
_EARLY_STAGE_TERMS = (
    "可研",
    "可行性研究",
    "地形测绘",
    "测绘",
    "勘测",
    "初设",
    "初步设计",
    "规划",
    "前期",
    "资源评估",
    "地质勘察",
    "环评",
    "核准",
    "项目建议书",
)


def _text(result: SearchResult) -> str:
    metadata = result.metadata or {}
    values = [result.title, result.snippet]
    for key in (
        "project_name",
        "projectname",
        "project",
        "name",
        "owner",
        "purchaser",
        "tenderer",
        "project_type",
        "announcement_type",
    ):
        value = metadata.get(key)
        if value:
            values.append(str(value))
    return " ".join(values)


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term.casefold() in text.casefold() for term in terms)


def _has_positive_direct_rack_signal(text: str) -> bool:
    """识别肯定的直接支架采购，避免“未发现支架采购”误判。"""

    lowered = text.casefold()
    negations = ("未发现", "未找到", "没有", "无新的", "暂无", "尚未发现", "未见")
    for term in _DIRECT_RACK_TERMS:
        start = 0
        needle = term.casefold()
        while True:
            index = lowered.find(needle, start)
            if index < 0:
                break
            context = lowered[max(0, index - 18):index]
            if not any(negation.casefold() in context for negation in negations):
                return True
            start = index + len(needle)
    return False


def _identity(result: SearchResult) -> str:
    metadata = result.metadata or {}
    for key in ("project_name", "projectname", "项目名称", "project", "name"):
        value = str(metadata.get(key) or "").strip()
        if len(value) >= 4:
            return re.sub(r"\s+", " ", value)[:160]
    title = re.sub(r"^[【\[][^】\]]+[】\]]\s*", "", result.title).strip()
    title = re.split(r"\s*[-|｜丨_]\s*", title, maxsplit=1)[0]
    title = re.sub(r"(?:招标|采购|询比|资格预审|中标)(?:公告|结果|候选人公示)?\s*$", "", title).strip()
    return title[:160]


def _follow_up_queries(identity: str, lead_type: str) -> list[str]:
    if not identity:
        return []
    queries = {
        "PROJECT_LEVEL_EPC": (
            f'"{identity}" 支架 设备材料',
            f'"{identity}" 招标文件',
            f'"{identity}" 组件支架',
        ),
        "EARLY_STAGE_TRACKING": (
            f'"{identity}" 光伏支架 采购',
            f'"{identity}" EPC 招标',
            f'"{identity}" 招标公告',
        ),
        "COMPLETED_INSTALLATION_SIGNAL": (
            f'"{identity}" 支架 采购',
            f'"{identity}" 二期 扩建 招标',
            f'"{identity}" 运维 招标',
        ),
        "ADJACENT_STRUCTURE": (
            f'"{identity}" 光伏支架',
            f'"{identity}" 组件支架',
            f'"{identity}" 招标公告',
        ),
    }
    return list(queries.get(lead_type, ()))


def classify_valuable_lead(result: SearchResult) -> dict[str, Any] | None:
    """识别应当单独上报的项目级或相近结构件线索。

    返回值中的 ``direct_component_rack_procurement`` 固定为 False，表示当前
    证据不能直接证明这是组件支架采购。返回 None 表示普通结果或已经是明确
    的直接组件支架采购结果，不需要进入这张“价值线索”清单。
    """

    text = _text(result)
    if not _has_any(text, _ENERGY_TERMS) or not _has_any(text, _PROJECT_TERMS):
        return None

    direct_rack = _has_positive_direct_rack_signal(text)
    # 明确的组件支架采购公告走普通招标项目路径，避免在两个栏目重复呈现。
    if direct_rack:
        return None

    if _has_any(text, _ADJACENT_STRUCTURE_TERMS):
        lead_type = "ADJACENT_STRUCTURE"
        label = "相近结构件采购"
        reason = "涉及箱变/设备钢结构平台等相近结构件，可能与新能源项目相关，但不是光伏组件支架。"
        scope_warning = "不得按组件支架采购、支架供货或当前支架机会统计。"
    elif _has_any(text, _COMPLETED_TERMS):
        lead_type = "COMPLETED_INSTALLATION_SIGNAL"
        label = "已建成项目/历史安装信号"
        reason = "项目已竣工、验收、投产或并网，公开内容中的支架信息属于历史安装线索。"
        scope_warning = "不代表当前窗口存在新的支架采购公告；需要另行追查二期、扩建、运维或新采购。"
    elif _has_any(text, ("EPC", "工程总承包")):
        lead_type = "PROJECT_LEVEL_EPC"
        label = "项目级EPC机会"
        reason = "项目级EPC/工程总承包可能把支架纳入设备材料或分包范围，但当前线索未证明存在独立支架采购。"
        scope_warning = "不得改写成独立组件支架采购；应继续查EPC招标文件、设备材料清单和分包公告。"
    elif _has_any(text, _EARLY_STAGE_TERMS):
        lead_type = "EARLY_STAGE_TRACKING"
        label = "前期跟踪项目"
        reason = "项目处于可研、测绘、勘测、规划或其他前期阶段，适合提前跟踪后续EPC和设备采购释放。"
        scope_warning = "当前阶段通常不是组件支架采购；不得据此判断已有可报名支架标的。"
    else:
        return None

    identity = _identity(result)
    return {
        "lead_type": lead_type,
        "lead_label": label,
        "project_identity": identity,
        "direct_component_rack_procurement": False,
        "procurement_status": "NOT_ESTABLISHED",
        "reason": reason,
        "scope_warning": scope_warning,
        "follow_up_queries": _follow_up_queries(identity, lead_type),
        "source_title": result.title[:256],
        "source_url": result.url,
        "source_domain": urlparse(result.url).netloc.lower().split(":", 1)[0],
        "source_text": (result.snippet or "")[:1500],
        "provider": result.provider,
        "published_at": result.published_at,
    }


def build_valuable_lead(
    result: SearchResult,
    *,
    canonical_url: str | None = None,
    source_level: str | None = None,
    region: str | None = None,
) -> dict[str, Any] | None:
    """将分类结果补齐为可直接写入报告的、可追溯线索记录。"""

    lead = classify_valuable_lead(result)
    if lead is None:
        return None
    lead["canonical_url"] = canonical_url or result.url
    lead["source_level"] = source_level
    lead["region"] = region
    metadata = result.metadata or {}
    for output_key, candidates in {
        "project_code": ("project_code", "projectnum", "项目编号"),
        "tender_code": ("tender_code", "tendercode", "message_no", "招标编号"),
        "owner": ("owner", "purchaser", "tenderer", "招标人", "采购人"),
    }.items():
        for key in candidates:
            if metadata.get(key):
                lead[output_key] = str(metadata[key])[:256]
                break
    return lead


def valuable_lead_key(lead: dict[str, Any]) -> str:
    """用于同一结果重复命中时去重；不同来源仍保留各自出处。"""

    return "|".join(
        str(lead.get(key) or "")
        for key in ("lead_type", "canonical_url", "source_url")
    )


__all__ = [
    "build_valuable_lead",
    "classify_valuable_lead",
    "valuable_lead_key",
]
