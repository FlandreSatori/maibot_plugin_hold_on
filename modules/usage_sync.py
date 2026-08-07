"""从宿主 llm_usage（ModelUsage）读取成功调用与消耗，插件内聚合。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional


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


def feature_from_usage_row(row: Dict[str, Any]) -> str:
    task = str(row.get("task_name") or "").strip()
    if task:
        return task
    return str(row.get("request_type") or "").strip()


def _empty_total() -> Dict[str, float]:
    return {
        "requests": 0,
        "tokens": 0,
        "weighted_tokens": 0.0,
        "cost": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
    }


def aggregate_usage_rows(
    rows: List[Dict[str, Any]],
    *,
    start_ts: float,
    end_ts: float,
    input_weight: float = 1.0,
    output_weight: float = 1.0,
) -> Dict[str, Any]:
    """把 ModelUsage 行聚合成 total + groups（按 provider/model/feature）。"""

    total = _empty_total()
    groups: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = parse_timestamp(row.get("timestamp"))
        if ts is None or ts < start_ts or ts > end_ts:
            continue

        scope = {
            "provider": str(row.get("model_api_provider_name") or "").strip(),
            "model": model_key_from_usage_row(row),
            "feature": feature_from_usage_row(row),
            "function": str(row.get("request_type") or "").strip(),
        }
        key = "|".join(scope.values())
        item = groups.setdefault(key, {**scope, **_empty_total()})

        input_tokens = int(row.get("prompt_tokens") or 0)
        output_tokens = int(row.get("completion_tokens") or 0)
        tokens = int(row.get("total_tokens") or (input_tokens + output_tokens))
        cost = float(row.get("cost") or 0.0)
        weighted = input_tokens * float(input_weight) + output_tokens * float(output_weight)
        values = {
            "requests": 1,
            "tokens": tokens,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost": cost,
            "weighted_tokens": weighted,
        }
        for name, value in values.items():
            item[name] = float(item[name]) + value
            total[name] = float(total[name]) + value

    return {"total": total, "groups": list(groups.values()), "rows": rows}


async def _query_model_usage(database: Any, *, limit: int, logger: Any = None) -> List[Dict[str, Any]]:
    try:
        result = await database.query(
            model_name="ModelUsage",
            query_type="get",
            order_by=["-id"],
            limit=max(100, int(limit or 5000)),
        )
    except Exception as exc:
        if logger is not None:
            logger.warning("hold_on 读取 ModelUsage 失败: %s", exc)
        return []

    if isinstance(result, list):
        return [r for r in result if isinstance(r, dict)]
    if isinstance(result, dict):
        nested = result.get("result")
        if isinstance(nested, list):
            return [r for r in nested if isinstance(r, dict)]
        if result.get("success") is False and logger is not None:
            logger.warning("hold_on 读取 ModelUsage 失败: %s", result.get("error"))
    elif logger is not None:
        logger.warning("hold_on ModelUsage 返回非列表: %s", type(result).__name__)
    return []


async def aggregate_usage(
    ctx: Any,
    start: datetime,
    end: datetime,
    limit: int = 5000,
    *,
    input_weight: float = 1.0,
    output_weight: float = 1.0,
) -> Dict[str, Any]:
    """通过 ctx.db 拉取 ModelUsage，再按 [start, end] 过滤并聚合。"""

    logger = getattr(ctx, "logger", None)
    database = getattr(ctx, "db", None)
    if database is None:
        if logger is not None:
            logger.warning("hold_on 缺少 ctx.db，无法读取 llm_usage")
        return {"total": _empty_total(), "groups": [], "rows": []}

    rows = await _query_model_usage(database, limit=limit, logger=logger)
    return aggregate_usage_rows(
        rows,
        start_ts=start.timestamp(),
        end_ts=end.timestamp(),
        input_weight=input_weight,
        output_weight=output_weight,
    )
