"""每个来源独立的浏览器 Profile 路径；默认 HTTP/API 运行，不自动启动浏览器。"""

from __future__ import annotations

import re
from pathlib import Path

from tender_ai.config_loader import APP_ROOT


BROWSER_PROFILE_ROOT = APP_ROOT.parent / "data" / "browser_profiles"
DEFAULT_BROWSER = "Microsoft Edge"


def browser_profile_path(source_id: str, *, create: bool = False) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", source_id).strip("._") or "source"
    path = BROWSER_PROFILE_ROOT / safe_id
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


__all__ = ["BROWSER_PROFILE_ROOT", "DEFAULT_BROWSER", "browser_profile_path"]
