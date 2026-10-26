"""从宿主 llm_usage（ModelUsage）读取成功调用与消耗，插件内聚合。"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any, Dict, List, Optional


CHECKPOINT_VERSION = 1
_RING_LIMIT = 50
_AGG_FIELDS = ("requests", "tokens", "weighted_tokens", "cost", "input_tokens", "output_tokens")
_SCOPE_FIELDS = ("provider", "model", "feature", "function")


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


def _scope_key(view: Dict[str, Any]) -> str:
    return "|".join(str(view.get(field) or "") for field in _SCOPE_FIELDS)


def _fold_view(
    groups: Dict[str, Dict[str, Any]],
    total: Dict[str, float],
    view: Dict[str, Any],
) -> None:
    """把单行视图累加进分组聚合（与 aggregate_usage_rows 同一套口径）。"""

    key = _scope_key(view)
    item = groups.get(key)
    if item is None:
        item = {field: str(view.get(field) or "") for field in _SCOPE_FIELDS}
        item.update(_empty_total())
        groups[key] = item
    values = {
        "requests": 1.0,
        "tokens": float(view.get("tokens") or 0),
        "input_tokens": float(view.get("input_tokens") or 0),
        "output_tokens": float(view.get("output_tokens") or 0),
        "cost": float(view.get("cost") or 0),
        "weighted_tokens": float(view.get("input_tokens") or 0) + float(view.get("output_tokens") or 0),
    }
    for name, value in values.items():
        item[name] = float(item[name]) + value
        total[name] = float(total[name]) + value


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
        _fold_view(groups, total, view)

    window_rows.sort(key=lambda item: float(item.get("ts") or 0), reverse=True)
    return {
        "total": total,
        "groups": list(groups.values()),
        "rows": rows,
        "window_rows": window_rows,
    }


_FRAME_RETRY_FLOOR = 2000
_retry_warned_at = 0.0


def _coerce_usage_rows(result: Any) -> Optional[List[Dict[str, Any]]]:
    """把宿主返回值整理成行列表；无法识别时返回 None。"""

    if isinstance(result, list):
        return [row for row in result if isinstance(row, dict)]
    if isinstance(result, dict):
        nested = result.get("result")
        if isinstance(nested, list):
            return [row for row in nested if isinstance(row, dict)]
    return None


async def _query_model_usage(database: Any, *, limit: int, logger: Any = None) -> List[Dict[str, Any]]:
    """读取最近的 ModelUsage 行。

    RPC 单帧上限 16MB（``plugin_runtime.transport.base.MAX_FRAME_SIZE``，约 3.7 万行
    ModelUsage），行数过多时宿主直接拒绝该帧。这里按上限减半重试，避免整批丢弃被当成
    「零用量」——那会让限速静默失效。
    """

    global _retry_warned_at

    requested = max(100, int(limit or 5000))
    attempt = requested
    while True:
        error: Any = None
        rows: Optional[List[Dict[str, Any]]] = None
        try:
            result = await database.query(
                model_name="ModelUsage",
                query_type="get",
                order_by=["-id"],
                limit=attempt,
            )
        except Exception as exc:
            error = exc
        else:
            rows = _coerce_usage_rows(result)
            if rows is None:
                error = result.get("error") if isinstance(result, dict) else type(result).__name__

        if error is None:
            if attempt < requested and logger is not None:
                now = time.time()
                if now - _retry_warned_at >= 3600:
                    _retry_warned_at = now
                    logger.warning(
                        "hold_on 单次查询被 RPC 帧上限压缩到 %d 行（[stats].usage_limit=%d），"
                        "当日统计可能偏小；建议把 usage_limit 调到 30000 以内",
                        attempt,
                        requested,
                    )
            return rows or []

        if attempt <= _FRAME_RETRY_FLOOR:
            if logger is not None:
                logger.warning("hold_on 读取 ModelUsage 失败: %s", error)
            return []

        nxt = max(_FRAME_RETRY_FLOOR, attempt // 2)
        if logger is not None:
            logger.warning(
                "hold_on 读取 ModelUsage 失败（limit=%d）: %s；降级到 %d 行重试", attempt, error, nxt
            )
        attempt = nxt


def _row_id(row: Dict[str, Any]) -> int:
    try:
        return int(row.get("id") or 0)
    except (TypeError, ValueError):
        return 0


class IncrementalUsageStore:
    """增量跟踪 ModelUsage，聚合在插件内存中完成。

    两种工作模式：

    - checkpoint 模式：从状态文件恢复已聚合的用量基线（覆盖 ``[anchor_ts, …]``），
      启动后只按 id 重放 ``last_seen_id`` 之后的新行并折算进基线，无需全量播种；
      预算周期跨越锚点（如零点）时自动重设基线。
    - 账本模式（默认/回退）：内存保留窗口内全部行，聚合时按时间窗过滤，
      行为与 3.3.x 一致。
    """

    def __init__(
        self,
        *,
        max_rows: int = 5000,
        retention_seconds: float = 7 * 24 * 3600,
        checkpoint: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._rows: Dict[int, Dict[str, Any]] = {}
        self._fallback: Optional[List[Dict[str, Any]]] = None  # 行无 id 时退化为全量缓存
        self._last_id = -1
        self._max_rows = max(100, int(max_rows or 5000))
        self._retention = max(3600.0, float(retention_seconds))
        self._required_since = 0.0
        self._logger: Any = None
        self._cap_warned_at = 0.0
        self._lock = asyncio.Lock()
        # checkpoint 模式状态
        self._checkpoint_mode = False
        self._base_anchor = 0.0
        self._base_groups: Dict[str, Dict[str, Any]] = {}
        self._base_total: Dict[str, float] = _empty_total()
        self._ring: List[Dict[str, Any]] = []
        self._last_export_at = 0.0
        self._no_id_warned_at = 0.0
        self._last_seed_short = False
        if isinstance(checkpoint, dict):
            self._load_checkpoint(checkpoint)

    # ------------------------------------------------------------------
    # checkpoint 装载 / 导出
    # ------------------------------------------------------------------

    @property
    def checkpoint_active(self) -> bool:
        return self._checkpoint_mode

    def _load_checkpoint(self, ckpt: Dict[str, Any]) -> None:
        try:
            if int(ckpt.get("version") or 0) != CHECKPOINT_VERSION:
                return
            anchor = float(ckpt.get("anchor_ts") or 0)
            last_id = int(ckpt.get("last_seen_id") or 0)
            aggregates = ckpt.get("aggregates")
            if anchor <= 0 or last_id < 0 or not isinstance(aggregates, dict):
                return
            raw_groups = aggregates.get("groups")
            raw_total = aggregates.get("total")
            if not isinstance(raw_groups, list) or not isinstance(raw_total, dict):
                return
            groups: Dict[str, Dict[str, Any]] = {}
            for group in raw_groups:
                if not isinstance(group, dict):
                    continue
                item: Dict[str, Any] = {field: str(group.get(field) or "") for field in _SCOPE_FIELDS}
                item.update({name: float(group.get(name) or 0) for name in _AGG_FIELDS})
                groups[_scope_key(item)] = item
            total = {name: float(raw_total.get(name) or 0) for name in _AGG_FIELDS}
            ring: List[Dict[str, Any]] = []
            for raw in ckpt.get("recent_rows") or []:
                if not isinstance(raw, dict):
                    continue
                view = dict(raw)
                view["ts"] = float(view.get("ts") or 0)
                if view["ts"] > 0:
                    ring.append(view)
            ring.sort(key=lambda item: float(item.get("ts") or 0), reverse=True)
            self._base_anchor = anchor
            self._last_id = last_id
            self._base_groups = groups
            self._base_total = total
            self._ring = ring[:_RING_LIMIT]
            self._checkpoint_mode = True
        except Exception:
            self._checkpoint_mode = False
            self._base_anchor = 0.0
            self._base_groups = {}
            self._base_total = _empty_total()
            self._ring = []

    def export_checkpoint(self, *, config_hash: str = "", force: bool = False) -> Optional[Dict[str, Any]]:
        """导出 checkpoint；非强制导出按 5 分钟节流。

        账本模式仅在覆盖查询窗口起点（或最近一次播种已取尽全表）时导出，
        否则返回 None 以免把缺失区间的误差固化进 checkpoint。
        """

        now = time.time()
        if not force and now - self._last_export_at < 300:
            return None
        if self._checkpoint_mode:
            checkpoint = {
                "version": CHECKPOINT_VERSION,
                "anchor_ts": float(self._base_anchor),
                "last_seen_id": int(max(0, self._last_id)),
                "synced_at": now,
                "config_hash": str(config_hash or ""),
                "aggregates": {
                    "total": dict(self._base_total),
                    "groups": [dict(group) for group in self._base_groups.values()],
                },
                "recent_rows": [dict(row) for row in self._ring],
            }
            self._last_export_at = now
            return checkpoint
        if self._required_since <= 0 or not self._rows:
            return None
        if self._oldest_ts() > self._required_since + 1.0 and not self._last_seed_short:
            return None
        rows = list(self._rows.values())
        agg = aggregate_usage_rows(rows, start_ts=self._required_since, end_ts=now + 86400)
        ordered = sorted(
            rows,
            key=lambda row: float(parse_timestamp(row.get("timestamp")) or 0),
            reverse=True,
        )
        recent: List[Dict[str, Any]] = []
        for row in ordered[:_RING_LIMIT]:
            view = usage_row_view(row)
            view.pop("raw", None)
            recent.append(view)
        checkpoint = {
            "version": CHECKPOINT_VERSION,
            "anchor_ts": float(self._required_since),
            "last_seen_id": int(max(0, self._last_id)),
            "synced_at": now,
            "config_hash": str(config_hash or ""),
            "aggregates": {"total": agg["total"], "groups": agg["groups"]},
            "recent_rows": recent,
        }
        self._last_export_at = now
        return checkpoint

    # ------------------------------------------------------------------
    # 刷新
    # ------------------------------------------------------------------

    async def refresh(
        self,
        ctx: Any,
        *,
        full_limit: int,
        incremental_limit: int = 500,
        since_ts: float = 0.0,
        rebase: bool = True,
    ) -> None:
        """拉取新记录合并进内存；无新行时不做任何重计算。

        ``since_ts``：当前查询窗口起点（如预算周期开始时刻）。账本模式下窗口起点
        之后的记录不会因行数上限被淘汰；checkpoint 模式下窗口起点越过基线锚点时
        触发基线重设。

        ``rebase``：是否允许 checkpoint 模式按 ``since_ts`` 重设基线。只有「预算周期
        起点推进」（如跨零点）才应置 True；``since_ts`` 只是任意回查起点（如错误停模
        结束时刻）时必须置 False，否则会把当日基线截断到该时刻、统计瞬间归零。
        """

        logger = getattr(ctx, "logger", None)
        database = getattr(ctx, "db", None)
        if database is None:
            if logger is not None:
                logger.warning("hold_on 缺少 ctx.db，无法读取 llm_usage")
            return
        self._logger = logger
        if self._checkpoint_mode:
            async with self._lock:
                await self._refresh_checkpoint(
                    database,
                    since_ts=since_ts,
                    rebase=rebase,
                    full_limit=full_limit,
                    incremental_limit=incremental_limit,
                    logger=logger,
                )
            return
        moved_back = False
        if since_ts:
            since = max(0.0, float(since_ts))
            moved_back = self._required_since > 0 and since < self._required_since
            self._required_since = since
        async with self._lock:
            incremental = bool(self._rows) or self._fallback is not None
            limit = (
                max(100, int(incremental_limit or 500))
                if incremental
                else max(100, int(full_limit or 5000))
            )
            rows = await _query_model_usage(database, limit=limit, logger=logger)
            if not incremental:
                self._last_seed_short = len(rows) < limit
            if not rows:
                return
            if all(_row_id(row) <= 0 for row in rows):
                # 数据行没有可用 id，无法增量：退化为缓存最近一批
                self._fallback = rows
                return
            self._fallback = None
            prev_last_id = self._last_id
            _, min_new_id = self._merge_rows(rows)
            if (
                incremental
                and prev_last_id >= 0
                and len(rows) >= limit
                and min_new_id > prev_last_id + 1
            ):
                # 增量窗口截断：两次刷新之间新增行数超过窗口，中间行已丢失，立即全量补齐
                full_rows = await _query_model_usage(
                    database, limit=max(100, int(full_limit or 5000)), logger=logger
                )
                self._last_seed_short = len(full_rows) < max(100, int(full_limit or 5000))
                if full_rows:
                    self._merge_rows(full_rows)
            if self._required_since > 0 and (not incremental or moved_back):
                await self._ensure_seed_coverage(database, full_limit=full_limit, logger=logger)

    async def _refresh_checkpoint(
        self,
        database: Any,
        *,
        since_ts: float,
        full_limit: int,
        incremental_limit: int,
        logger: Any,
        rebase: bool = True,
    ) -> None:
        # 查询窗口起点越过基线锚点（预算周期跨越，如零点）：重设基线，只聚合新周期内的行
        if rebase and since_ts and float(since_ts) > self._base_anchor + 1.0:
            await self._rebase_checkpoint(
                database, new_anchor=float(since_ts), full_limit=full_limit, logger=logger
            )
            return
        limit = max(100, int(incremental_limit or 500))
        rows = await _query_model_usage(database, limit=limit, logger=logger)
        if not rows:
            return
        if all(_row_id(row) <= 0 for row in rows):
            self._warn_no_id_once(logger)
            return
        self._fallback = None
        if len(rows) >= limit and min(_row_id(row) for row in rows) > self._last_id + 1:
            # 停机/节流期间新增行数超过增量窗口：扩大一次补缺口
            bigger = max(100, int(full_limit or 5000))
            rows = await _query_model_usage(database, limit=bigger, logger=logger)
            if not rows:
                return
            if len(rows) >= bigger and min(_row_id(row) for row in rows) > self._last_id + 1:
                # 缺口仍无法覆盖（长期停机）：放弃 checkpoint，回退全量重播重建账本
                if logger is not None:
                    logger.info("hold_on 用量 checkpoint 缺口过大，回退全量重播重建账本")
                self._invalidate_checkpoint()
                self._merge_rows(rows, backfill=True)
                await self._ensure_seed_coverage(database, full_limit=full_limit, logger=logger)
                return
        self._merge_checkpoint_rows(rows)

    async def _rebase_checkpoint(
        self,
        database: Any,
        *,
        new_anchor: float,
        full_limit: int,
        logger: Any,
    ) -> None:
        bigger = max(100, int(full_limit or 5000), 40000)
        rows = await _query_model_usage(database, limit=bigger, logger=logger)
        self._last_seed_short = len(rows) < bigger  # 未取满即整表已读完，此时不存在覆盖缺口
        self._base_anchor = new_anchor
        self._base_groups = {}
        self._base_total = _empty_total()
        max_id = self._last_id
        oldest = 0.0
        views: List[Dict[str, Any]] = []
        for row in rows:
            rid = _row_id(row)
            if rid <= 0:
                continue
            if rid > max_id:
                max_id = rid
            ts = float(parse_timestamp(row.get("timestamp")) or 0)
            if ts > 0 and (oldest == 0.0 or ts < oldest):
                oldest = ts
            if ts >= new_anchor:
                view = usage_row_view(row)
                view.pop("raw", None)
                views.append(view)
                _fold_view(self._base_groups, self._base_total, view)
        self._last_id = max_id
        self._append_ring(views)
        self._last_export_at = 0.0  # 基线已重设，尽快导出新 checkpoint
        if rows and not self._last_seed_short and oldest > new_anchor + 1.0 and logger is not None:
            now = time.time()
            if now - self._cap_warned_at >= 3600:
                self._cap_warned_at = now
                logger.warning(
                    "hold_on 新周期用量窗口起点 (%s) 未能完全覆盖（最早行 %s），当日统计可能偏小；"
                    "请调大 [stats].usage_limit（建议大于单日全供应商请求总量）",
                    datetime.fromtimestamp(new_anchor).strftime("%m-%d %H:%M"),
                    datetime.fromtimestamp(oldest).strftime("%m-%d %H:%M"),
                )

    def _merge_checkpoint_rows(self, rows: List[Dict[str, Any]]) -> None:
        """合并带 id 的新行（查询结果按 id 倒序返回）。

        跳过判断必须基于合并前的 ``_last_id``：倒序遍历时若边合并边更新 ``_last_id``，
        第二批起的较小 id 会被误判为旧行，导致每次刷新只并入最新 1 行。
        """

        prev_last_id = self._last_id
        max_id = prev_last_id
        views: List[Dict[str, Any]] = []
        for row in rows:
            rid = _row_id(row)
            if rid <= 0 or rid <= prev_last_id:
                continue
            view = usage_row_view(row)
            view.pop("raw", None)
            if float(view.get("ts") or 0) >= self._base_anchor:
                views.append(view)
                _fold_view(self._base_groups, self._base_total, view)
            if rid > max_id:
                max_id = rid
        self._last_id = max_id
        self._append_ring(views)

    def _append_ring(self, views: List[Dict[str, Any]]) -> None:
        if not views:
            return
        self._ring.extend(views)
        self._ring.sort(key=lambda item: float(item.get("ts") or 0), reverse=True)
        del self._ring[_RING_LIMIT:]

    def _invalidate_checkpoint(self) -> None:
        anchor = self._base_anchor
        self._checkpoint_mode = False
        self._base_anchor = 0.0
        self._base_groups = {}
        self._base_total = _empty_total()
        self._ring = []
        if anchor > 0:
            self._required_since = anchor  # 供回退后的账本播种覆盖判断

    def _warn_no_id_once(self, logger: Any) -> None:
        now = time.time()
        if now - self._no_id_warned_at < 3600:
            return
        self._no_id_warned_at = now
        if logger is not None:
            logger.warning(
                "hold_on ModelUsage 行缺少可用 id，checkpoint 模式下无法增量合并，已忽略该批数据"
            )

    async def _ensure_seed_coverage(self, database: Any, *, full_limit: int, logger: Any) -> None:
        """全量播种后检查内存覆盖是否到达查询窗口起点，不足则扩量重拉一次。

        覆盖判断带 1 秒容差：timestamp 经 isoformat 往返有微秒级浮点噪声，
        恰好压在窗口起点上的行不应触发无意义的扩量重拉与告警。
        """
        epsilon = 1.0
        oldest = self._oldest_ts()
        if oldest <= 0 or oldest <= self._required_since + epsilon:
            return
        bigger_limit = max(100, int(full_limit or 5000), 40000)
        if logger is not None:
            logger.info(
                "hold_on 用量种子窗口不足（最早行 %.0fs < 窗口起点 %.0fs），扩量至 %d 行重拉",
                oldest,
                self._required_since,
                bigger_limit,
            )
        full_rows = await _query_model_usage(database, limit=bigger_limit, logger=logger)
        self._last_seed_short = len(full_rows) < bigger_limit
        if full_rows:
            self._merge_rows(full_rows, backfill=True)
        oldest = self._oldest_ts()
        if oldest > 0 and oldest > self._required_since + epsilon and logger is not None:
            logger.warning(
                "hold_on 内存用量窗口起点 (%s) 仍晚于查询窗口起点 (%s)，当日统计可能偏小；"
                "请调大 [stats].usage_limit（建议大于单日全供应商请求总量）",
                datetime.fromtimestamp(oldest).strftime("%m-%d %H:%M"),
                datetime.fromtimestamp(self._required_since).strftime("%m-%d %H:%M"),
            )

    def _oldest_ts(self) -> float:
        if not self._rows:
            return 0.0
        return min(float(parse_timestamp(row.get("timestamp")) or 0) for row in self._rows.values())

    def _merge_rows(self, rows: List[Dict[str, Any]], *, backfill: bool = False) -> tuple[int, int]:
        """合并带 id 的行（查询结果按 id 倒序返回）；返回 (新增行数, 本批最小新 id，无新增时为 0)。

        跳过判断基于合并前的 _last_id，避免倒序遍历时把尚未合并的较小 id 误判为旧行。
        backfill=True 用于种子扩量回填：种子可能只含最新 N 行（id 不连续），需按 id 去重后全部并入。
        """

        prev_last_id = self._last_id
        new_rows = 0
        min_new_id = 0
        for row in rows:
            rid = _row_id(row)
            if rid <= 0:
                continue
            if not backfill and rid <= prev_last_id:
                continue
            if rid not in self._rows:
                new_rows += 1
                if min_new_id == 0 or rid < min_new_id:
                    min_new_id = rid
            self._rows[rid] = row
            if rid > self._last_id:
                self._last_id = rid
        if new_rows:
            self._prune()
        return new_rows, min_new_id

    def _prune(self) -> None:
        """按时间淘汰旧行；有查询窗口时窗口内行不因容量上限被丢弃。"""

        if not self._rows:
            return
        cutoff = datetime.now().timestamp() - self._retention
        floor = cutoff
        tolerance = 0.0
        if self._required_since > 0:
            floor = max(floor, self._required_since)
            # 窗口起点带 1 秒容差：timestamp 经 isoformat 往返有微秒级噪声，
            # 恰好压在起点上的行不应被误判为窗口外而丢弃
            tolerance = 1.0
        stale = [
            rid
            for rid, row in self._rows.items()
            if float(parse_timestamp(row.get("timestamp")) or 0) < floor - tolerance
        ]
        for rid in stale:
            del self._rows[rid]
        if len(self._rows) <= self._max_rows:
            return
        if self._required_since > 0:
            # 窗口内行数超出上限：宁可超容量也不丢当日数据，每小时提醒调大 usage_limit
            now = time.time()
            if self._logger is not None and now - self._cap_warned_at >= 3600:
                self._cap_warned_at = now
                self._logger.warning(
                    "hold_on 内存用量窗口内行数 (%d) 超过 [stats].usage_limit (%d)，已临时保留全部行；"
                    "建议调大 [stats].usage_limit（建议大于单日全供应商请求总量）",
                    len(self._rows),
                    self._max_rows,
                )
            return
        ordered = sorted(
            self._rows.items(),
            key=lambda item: float(parse_timestamp(item[1].get("timestamp")) or 0),
            reverse=True,
        )
        self._rows = dict(ordered[: self._max_rows])

    def aggregate(self, start_ts: float, end_ts: float) -> Dict[str, Any]:
        if self._checkpoint_mode:
            # 基线聚合覆盖 [anchor, ∞)。预算/状态查询的起点即周期起点，直接返回基线；
            # 起点早于 anchor（错误停模 anchor 回查）时少算 [start, anchor) 区间，
            # 起点晚于 anchor 时无法按时间切片、会多算 [anchor, start)——两者均为低精度场景。
            rows = [
                dict(row)
                for row in self._ring
                if start_ts <= float(row.get("ts") or 0) < end_ts
            ]
            rows.sort(key=lambda item: float(item.get("ts") or 0), reverse=True)
            return {
                "total": dict(self._base_total),
                "groups": [dict(group) for group in self._base_groups.values()],
                "rows": [],
                "window_rows": rows,
            }
        rows = self._fallback if self._fallback is not None else list(self._rows.values())
        return aggregate_usage_rows(rows, start_ts=float(start_ts), end_ts=float(end_ts))
