"""从宿主 llm_usage（ModelUsage）读取成功调用与消耗，插件内聚合。"""

from __future__ import annotations

import asyncio
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


def usage_row_view(row: Dict[str, Any]) -> Dict[str, Any]:
    """把 ModelUsage 行整理成便于展示/匹配的结构。"""

    ts = parse_timestamp(row.get("timestamp"))
    input_tokens = int(row.get("prompt_tokens") or 0)
    output_tokens = int(row.get("completion_tokens") or 0)
    tokens = int(row.get("total_tokens") or (input_tokens + output_tokens))
    cache_hit = int(row.get("prompt_cache_hit_tokens") or 0)
    cache_miss = int(row.get("prompt_cache_miss_tokens") or 0)
    return {
        "ts": float(ts or 0),
        "provider": str(row.get("model_api_provider_name") or "").strip(),
        "model": model_key_from_usage_row(row),
        "feature": feature_from_usage_row(row),
        "function": str(row.get("request_type") or "").strip(),
        "tokens": tokens,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_hit_tokens": cache_hit,
        "cache_miss_tokens": cache_miss,
        "cache_enabled": bool(row.get("prompt_cache_enabled")),
        "cost": float(row.get("cost") or 0.0),
        "time_cost": float(row.get("time_cost") or 0.0),
        "raw": row,
    }


def aggregate_usage_rows(
    rows: List[Dict[str, Any]],
    *,
    start_ts: float,
    end_ts: float,
) -> Dict[str, Any]:
    """把 ModelUsage 行聚合成 total + groups（按 provider/model/feature）。"""

    total = _empty_total()
    groups: Dict[str, Dict[str, Any]] = {}
    window_rows: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        view = usage_row_view(row)
        ts = float(view.get("ts") or 0)
        if ts <= 0 or ts < start_ts or ts >= end_ts:
            continue
        window_rows.append(view)

        scope = {
            "provider": str(view.get("provider") or ""),
            "model": str(view.get("model") or ""),
            "feature": str(view.get("feature") or ""),
            "function": str(view.get("function") or ""),
        }
        key = "|".join(scope.values())
        item = groups.setdefault(key, {**scope, **_empty_total()})

        values = {
            "requests": 1,
            "tokens": float(view.get("tokens") or 0),
            "input_tokens": float(view.get("input_tokens") or 0),
            "output_tokens": float(view.get("output_tokens") or 0),
            "cost": float(view.get("cost") or 0),
            "weighted_tokens": float(view.get("input_tokens") or 0) + float(view.get("output_tokens") or 0),
        }
        for name, value in values.items():
            item[name] = float(item[name]) + value
            total[name] = float(total[name]) + value

    window_rows.sort(key=lambda item: float(item.get("ts") or 0), reverse=True)
    return {
        "total": total,
        "groups": list(groups.values()),
        "rows": rows,
        "window_rows": window_rows,
    }


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
) -> Dict[str, Any]:
    """通过 ctx.db 拉取 ModelUsage，再按 [start, end] 过滤并聚合。"""

    logger = getattr(ctx, "logger", None)
    database = getattr(ctx, "db", None)
    if database is None:
        if logger is not None:
            logger.warning("hold_on 缺少 ctx.db，无法读取 llm_usage")
        return {"total": _empty_total(), "groups": [], "rows": [], "window_rows": []}

    rows = await _query_model_usage(database, limit=limit, logger=logger)
    return aggregate_usage_rows(
        rows,
        start_ts=start.timestamp(),
        end_ts=end.timestamp(),
    )


def _row_id(row: Dict[str, Any]) -> int:
    try:
        return int(row.get("id") or 0)
    except (TypeError, ValueError):
        return 0


class IncrementalUsageStore:
    """增量跟踪 ModelUsage：首次全量播种，之后只合并 id 更大的新行。

    避免每次检查都拉取全部 5000 行记录；聚合在插件内存中完成。
    """

    def __init__(
        self,
        *,
        max_rows: int = 5000,
        retention_seconds: float = 7 * 24 * 3600,
    ) -> None:
        self._rows: Dict[int, Dict[str, Any]] = {}
        self._fallback: Optional[List[Dict[str, Any]]] = None  # 行无 id 时退化为全量缓存
        self._last_id = -1
        self._max_rows = max(100, int(max_rows or 5000))
        self._retention = max(3600.0, float(retention_seconds))
        self._lock = asyncio.Lock()

    async def refresh(
        self,
        ctx: Any,
        *,
        full_limit: int,
        incremental_limit: int = 500,
    ) -> None:
        """拉取新记录合并进内存；无新行时不做任何重计算。"""

        logger = getattr(ctx, "logger", None)
        database = getattr(ctx, "db", None)
        if database is None:
            if logger is not None:
                logger.warning("hold_on 缺少 ctx.db，无法读取 llm_usage")
            return
        async with self._lock:
            if not self._rows and self._fallback is None:
                limit = max(100, int(full_limit or 5000))
            else:
                limit = max(100, int(incremental_limit or 500))
            rows = await _query_model_usage(database, limit=limit, logger=logger)
            if not rows:
                return
            if all(_row_id(row) <= 0 for row in rows):
                # 数据行没有可用 id，无法增量：退化为缓存最近一批
                self._fallback = rows
                return
            self._fallback = None
            new_rows = 0
            for row in rows:
                rid = _row_id(row)
                if rid <= self._last_id:
                    continue
                self._rows[rid] = row
                if rid > self._last_id:
                    self._last_id = rid
                new_rows += 1
            if new_rows:
                self._prune()

    def _prune(self) -> None:
        """按时间淘汰旧行；超出容量时优先丢弃最旧的。"""

        if not self._rows:
            return
        cutoff = datetime.now().timestamp() - self._retention
        stale = [
            rid
            for rid, row in self._rows.items()
            if float(parse_timestamp(row.get("timestamp")) or 0) < cutoff
        ]
        for rid in stale:
            del self._rows[rid]
        if len(self._rows) > self._max_rows:
            ordered = sorted(
                self._rows.items(),
                key=lambda item: float(parse_timestamp(item[1].get("timestamp")) or 0),
                reverse=True,
            )
            self._rows = dict(ordered[: self._max_rows])

    def aggregate(self, start_ts: float, end_ts: float) -> Dict[str, Any]:
        rows = self._fallback if self._fallback is not None else list(self._rows.values())
        return aggregate_usage_rows(rows, start_ts=float(start_ts), end_ts=float(end_ts))
