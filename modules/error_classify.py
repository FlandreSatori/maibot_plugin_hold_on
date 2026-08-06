"""从失败快照归类报错类型。"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional


_STATUS_RE = re.compile(r"\b([1-5]\d{2})\b")


def classify_error(
    message: str = "",
    *,
    error: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> str:
    """归类为稳定短标签：429 / 403 / 401 / 5xx / timeout / connection / other 等。"""

    error = error if isinstance(error, dict) else {}
    payload = payload if isinstance(payload, dict) else {}

    candidates: list[str] = []
    for key in ("status", "status_code", "http_status", "code"):
        value = error.get(key)
        if value is not None:
            candidates.append(str(value))
        value = payload.get(key)
        if value is not None:
            candidates.append(str(value))

    err_type = str(error.get("type") or payload.get("error_type") or "").strip()
    if err_type:
        candidates.append(err_type)

    body = error.get("response_body")
    if isinstance(body, dict):
        candidates.append(json.dumps(body, ensure_ascii=False))
    elif body is not None:
        candidates.append(str(body))

    blob = " ".join(candidates + [str(message or "")]).lower()

    for code in ("429", "403", "401", "408", "500", "502", "503", "504"):
        if code in blob:
            return code

    match = _STATUS_RE.search(blob)
    if match:
        code = match.group(1)
        if code.startswith("5"):
            return "5xx"
        if code.startswith("4"):
            return code
        return code

    if any(x in blob for x in ("rate_limit", "rate limit", "too many requests", "rpm")):
        return "429"
    if any(x in blob for x in ("timeout", "timed out", "deadline exceeded")):
        return "timeout"
    if any(x in blob for x in ("connection", "connect error", "network", "dns", "ssl")):
        return "connection"
    if any(x in blob for x in ("unauthorized", "forbidden", "invalid api key", "authentication")):
        return "401"
    if err_type:
        normalized = re.sub(r"[^a-z0-9_]+", "_", err_type.lower()).strip("_")
        return normalized[:40] or "other"
    return "other"


def error_type_matches(rule_type: str, actual: str) -> bool:
    """规则 error_type 是否匹配实际类型。``*`` / ``any`` / 空 = 任意。"""

    want = str(rule_type or "").strip().lower()
    got = str(actual or "").strip().lower()
    if not want or want in ("*", "any", "all"):
        return True
    if want == got:
        return True
    if want == "5xx" and (got == "5xx" or (got.isdigit() and got.startswith("5"))):
        return True
    if want == "4xx" and got.isdigit() and got.startswith("4"):
        return True
    return False
