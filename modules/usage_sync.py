"""从宿主 llm_usage（ModelUsage）读取成功调用次数。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

import time


def parse_timestamp(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return value.timestamp()
    text = str(value).strip()
    if not text:
        return None
    try:
        normalized = text.replace("Z", "+00:00")
        if "T" not in normalized and " " in normalized:
            normalized = normalized.replace(" ", "T", 1)
        return datetime.fromisoformat(normalized).timestamp()
    except Exception:
        return None


def model_key_from_usage_row(row: Dict[str, Any]) -> str:
    assign = str(row.get("model_assign_name") or "").strip()
    if assign:
        return assign
    return str(row.get("model_name") or "").strip() or "unknown"


def aggregate_req_cnt_by_model(
    rows: List[Dict[str, Any]],
    *,
    window_seconds: float,
    now_ts: Optional[float] = None,
) -> Dict[str, Dict[str, Any]]:
    ts_now = time.time() if now_ts is None else float(now_ts)
    cutoff = ts_now - max(0.0, float(window_seconds))
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = parse_timestamp(row.get("timestamp"))
        if ts is None or ts < cutoff:
            continue
        key = model_key_from_usage_row(row)
        provider = str(row.get("model_api_provider_name") or "").strip()
        bucket = out.setdefault(key, {"success": 0, "provider": provider})
        bucket["success"] = int(bucket["success"]) + 1
        if provider and not bucket.get("provider"):
            bucket["provider"] = provider
    return out


async def fetch_req_cnt_by_model(
    database: Any,
    *,
    window_seconds: float,
    limit: int = 10000,
    logger: Any = None,
) -> Dict[str, Dict[str, Any]]:
    try:
        result = await database.query(
            model_name="ModelUsage",
            query_type="get",
            order_by=["-id"],
            limit=max(100, int(limit or 10000)),
        )
    except Exception as exc:
        if logger is not None:
            logger.warning("hold_on 读取 ModelUsage 失败: %s", exc)
        return {}

    if not isinstance(result, list):
        if logger is not None:
            logger.warning("hold_on ModelUsage 返回非列表: %s", type(result).__name__)
        return {}

    rows = [r for r in result if isinstance(r, dict)]
    return aggregate_req_cnt_by_model(rows, window_seconds=window_seconds)
