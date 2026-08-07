"""宿主 llm_usage 聚合能力代理。"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Dict, List


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def row_scope(row: Dict[str, Any]) -> Dict[str, str]:
    return {
        "provider": str(row.get("provider_name") or ""),
        "model": str(row.get("model_alias") or row.get("model_identifier") or "unknown"),
        "feature": str(row.get("task_name") or ""),
        "function": str(row.get("request_type") or ""),
    }


def rows_to_metrics(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = {"requests": 0, "tokens": 0, "weighted_tokens": 0.0, "cost": 0.0, "input_tokens": 0, "output_tokens": 0}
    groups: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        scope = row_scope(row)
        key = "|".join(scope.values())
        item = groups.setdefault(key, {**scope, **{k: 0 for k in total}})
        values = {
            "requests": int(row.get("successful_requests") or 0),
            "tokens": int(row.get("total_tokens") or 0),
            "input_tokens": int(row.get("prompt_tokens") or 0),
            "output_tokens": int(row.get("completion_tokens") or 0),
            "cost": float(row.get("cost_cny") or 0.0),
        }
        values["weighted_tokens"] = float(values["input_tokens"] + values["output_tokens"])
        for k, v in values.items():
            item[k] += v
            total[k] += v
    return {"total": total, "groups": list(groups.values()), "rows": rows}


async def aggregate_usage(ctx: Any, start: datetime, end: datetime, limit: int = 5000) -> Dict[str, Any]:
    result = await ctx.call_capability(
        "statistics.llm_usage.aggregate",
        start_time=iso_utc(start),
        end_time=iso_utc(end),
        limit=limit,
    )
    if not isinstance(result, dict) or not result.get("success"):
        raise RuntimeError(str((result or {}).get("error") if isinstance(result, dict) else result))
    return rows_to_metrics(list(result.get("rows") or []))
