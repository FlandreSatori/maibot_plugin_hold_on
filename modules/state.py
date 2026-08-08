"""错误统计与停模状态（内存 + 落盘）。"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class StatEvent:
    """一次失败事件。"""

    ts: float
    model: str = ""
    provider: str = ""
    feature: str = ""
    error_type: str = ""
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> Optional["StatEvent"]:
        # 忽略历史 success 事件
        kind = str(raw.get("kind") or "error").strip().lower()
        if kind and kind != "error":
            return None
        return cls(
            ts=float(raw.get("ts") or 0),
            model=str(raw.get("model") or ""),
            provider=str(raw.get("provider") or ""),
            feature=str(raw.get("feature") or ""),
            error_type=str(raw.get("error_type") or ""),
            message=str(raw.get("message") or ""),
        )


@dataclass
class HoldInfo:
    until_ts: float = 0.0
    error_until_ts: float = 0.0  # 仅错误阈值停模；与限速 until 分离，避免档位被卡住
    reason: str = ""
    rule_scope: str = ""
    rule_name: str = ""
    error_type: str = ""
    activated_ts: float = field(default_factory=time.time)

    def remaining_seconds(self, now: Optional[float] = None) -> float:
        ts = time.time() if now is None else now
        return max(0.0, float(self.until_ts) - ts)

    def is_active(self, now: Optional[float] = None) -> bool:
        return self.remaining_seconds(now) > 0


class HoldOnState:
    """错误滑动窗口 + 全局停模。"""

    def __init__(self, path: Path, *, max_events: int = 5000) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._events: List[StatEvent] = []
        self._max_events = max(500, int(max_events or 5000))
        self._hold = HoldInfo()
        self._model_provider: Dict[str, str] = {}
        self._error_hold_streak: int = 0
        self._hold_ended_ts: float = 0.0  # 最近一次「错误停模」结束时间
        self._streak_scope: str = ""
        self._streak_target: str = ""
        self._rate_limit_events: List[Dict[str, Any]] = []
        self.load()

    def load(self) -> None:
        with self._lock:
            if not self._path.exists():
                return
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                return
            if not isinstance(raw, dict):
                return
            events = raw.get("events") or []
            if isinstance(events, list):
                loaded: List[StatEvent] = []
                for item in events:
                    if not isinstance(item, dict):
                        continue
                    event = StatEvent.from_dict(item)
                    if event is not None:
                        loaded.append(event)
                self._events = loaded[-self._max_events :]
            hold = raw.get("hold")
            if isinstance(hold, dict):
                self._hold = HoldInfo(
                    until_ts=float(hold.get("until_ts") or 0),
                    error_until_ts=float(hold.get("error_until_ts") or 0),
                    reason=str(hold.get("reason") or ""),
                    rule_scope=str(hold.get("rule_scope") or ""),
                    rule_name=str(hold.get("rule_name") or ""),
                    error_type=str(hold.get("error_type") or ""),
                    activated_ts=float(hold.get("activated_ts") or time.time()),
                )
            mapping = raw.get("model_provider") or {}
            if isinstance(mapping, dict):
                self._model_provider = {
                    str(k).strip(): str(v).strip()
                    for k, v in mapping.items()
                    if str(k).strip() and str(v).strip()
                }
            self._error_hold_streak = max(0, int(raw.get("error_hold_streak") or 0))
            self._hold_ended_ts = float(raw.get("hold_ended_ts") or 0)
            self._streak_scope = str(raw.get("streak_scope") or self._hold.rule_scope or "")
            self._streak_target = str(raw.get("streak_target") or self._hold.rule_name or "")
            rate_events = raw.get("rate_limit_events") or []
            if isinstance(rate_events, list):
                loaded_rate: List[Dict[str, Any]] = []
                for item in rate_events:
                    if not isinstance(item, dict):
                        continue
                    loaded_rate.append(
                        {
                            "ts": float(item.get("ts") or 0),
                            "scope": str(item.get("scope") or ""),
                            "target": str(item.get("target") or ""),
                            "metric": str(item.get("metric") or ""),
                            "kind": str(item.get("kind") or "rate"),
                            "reason": str(item.get("reason") or ""),
                        }
                    )
                self._rate_limit_events = loaded_rate[-self._max_events :]

    def save(self) -> None:
        with self._lock:
            self._prune_locked(time.time(), keep_seconds=7 * 24 * 3600)
            payload = {
                "events": [e.to_dict() for e in self._events[-self._max_events :]],
                "hold": {
                    "until_ts": self._hold.until_ts,
                    "error_until_ts": self._hold.error_until_ts,
                    "reason": self._hold.reason,
                    "rule_scope": self._hold.rule_scope,
                    "rule_name": self._hold.rule_name,
                    "error_type": self._hold.error_type,
                    "activated_ts": self._hold.activated_ts,
                },
                "model_provider": dict(self._model_provider),
                "error_hold_streak": int(self._error_hold_streak),
                "hold_ended_ts": float(self._hold_ended_ts),
                "streak_scope": str(self._streak_scope or ""),
                "streak_target": str(self._streak_target or ""),
                "rate_limit_events": list(self._rate_limit_events[-self._max_events :]),
                "saved_at": time.time(),
            }
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)

    def _prune_locked(self, now: float, keep_seconds: float = 7 * 24 * 3600) -> None:
        cutoff = now - max(3600.0, float(keep_seconds))
        self._events = [e for e in self._events if e.ts >= cutoff]
        self._rate_limit_events = [e for e in self._rate_limit_events if float(e.get("ts") or 0) >= cutoff]
        if len(self._events) > self._max_events:
            self._events = self._events[-self._max_events :]
        if len(self._rate_limit_events) > self._max_events:
            self._rate_limit_events = self._rate_limit_events[-self._max_events :]

    def register_model_provider(self, model: str, provider: str) -> None:
        model = str(model or "").strip()
        provider = str(provider or "").strip()
        if not model or not provider:
            return
        with self._lock:
            if self._model_provider.get(model) != provider:
                self._model_provider[model] = provider
                self.save()

    def provider_of(self, model: str) -> str:
        model = str(model or "").strip()
        with self._lock:
            return str(self._model_provider.get(model) or "")

    def record_error(
        self,
        *,
        model: str,
        provider: str = "",
        feature: str = "",
        error_type: str = "other",
        message: str = "",
    ) -> StatEvent:
        model = str(model or "").strip()
        provider = str(provider or "").strip() or (self.provider_of(model) if model else "")
        event = StatEvent(
            ts=time.time(),
            model=model,
            provider=provider,
            feature=str(feature or "").strip(),
            error_type=str(error_type or "other").strip() or "other",
            message=str(message or "").strip()[:300],
        )
        with self._lock:
            if model and provider:
                self._model_provider[model] = provider
            self._events.append(event)
            self._prune_locked(event.ts)
            self.save()
            return event

    def events_since(self, window_seconds: float, now: Optional[float] = None) -> List[StatEvent]:
        ts = time.time() if now is None else now
        cutoff = ts - max(0.0, float(window_seconds))
        with self._lock:
            return [e for e in self._events if e.ts >= cutoff]

    def events_between(self, start_ts: float, end_ts: float) -> List[StatEvent]:
        start = float(start_ts)
        end = float(end_ts)
        with self._lock:
            return [e for e in self._events if start <= e.ts <= end]

    def latest_error_for_specs(
        self,
        *,
        specs: list[tuple[str, str]],
        feature_models: Optional[Dict[str, List[str]]] = None,
        start_ts: float = 0.0,
        end_ts: Optional[float] = None,
    ) -> Optional[StatEvent]:
        """最近一条命中监听目标的错误事件。"""

        end = time.time() if end_ts is None else float(end_ts)
        start = float(start_ts or 0)
        normalized = [(str(scope or "").strip().lower(), str(target or "").strip()) for scope, target in specs]
        if not normalized:
            return None
        feature_map = feature_models or {}
        latest: Optional[StatEvent] = None
        with self._lock:
            for event in reversed(self._events):
                if event.ts < start or event.ts > end:
                    continue
                for scope, target in normalized:
                    matched = False
                    if scope == "provider":
                        matched = (not target) or event.provider == target
                    elif scope == "model":
                        matched = (not target) or event.model == target
                    elif scope == "feature":
                        models = {
                            str(m).strip()
                            for m in (feature_map.get(target) or [])
                            if str(m).strip()
                        } if target else set()
                        matched = (not target) or event.feature == target or (bool(models) and event.model in models)
                    if matched:
                        return event
        return latest

    def count_errors_for_scope(
        self,
        *,
        scope: str,
        target: str,
        start_ts: float,
        end_ts: float,
        feature_models: Optional[Dict[str, List[str]]] = None,
    ) -> int:
        want = str(target or "").strip()
        if not want:
            return 0
        scope_key = str(scope or "").strip().lower()
        models = {
            str(m).strip()
            for m in ((feature_models or {}).get(want) or [])
            if str(m).strip()
        }
        total = 0
        for event in self.events_between(start_ts, end_ts):
            if scope_key == "provider" and event.provider == want:
                total += 1
            elif scope_key == "model" and event.model == want:
                total += 1
            elif scope_key == "feature":
                if event.feature == want or (models and event.model in models):
                    total += 1
        return total

    def count_errors(
        self,
        *,
        window_seconds: float,
        error_type: str = "",
        model: str = "",
        provider: str = "",
        feature: str = "",
        models: Optional[List[str]] = None,
        now: Optional[float] = None,
    ) -> int:
        from .error_classify import error_type_matches

        events = self.events_since(window_seconds, now=now)
        model_set = {str(m).strip() for m in (models or []) if str(m).strip()}
        want_model = str(model or "").strip()
        want_provider = str(provider or "").strip()
        want_feature = str(feature or "").strip()
        total = 0
        for event in events:
            if want_model and event.model != want_model:
                continue
            if want_provider and event.provider != want_provider:
                continue
            if want_feature:
                by_feature = event.feature == want_feature
                by_model = bool(model_set) and event.model in model_set
                if not by_feature and not by_model:
                    continue
            elif model_set and event.model not in model_set:
                continue
            if not error_type_matches(error_type, event.error_type):
                continue
            total += 1
        return total

    @property
    def error_hold_streak(self) -> int:
        with self._lock:
            return int(self._error_hold_streak)

    @property
    def hold_ended_ts(self) -> float:
        with self._lock:
            return float(self._hold_ended_ts or 0)

    def bump_error_hold_streak(self) -> int:
        """新一次错误停模触发时 +1，返回当前档位（从 1 开始）。"""
        with self._lock:
            self._error_hold_streak = max(0, int(self._error_hold_streak)) + 1
            self.save()
            return int(self._error_hold_streak)

    def reset_error_hold_streak(self) -> None:
        with self._lock:
            if self._error_hold_streak or self._hold_ended_ts or self._streak_scope or self._streak_target:
                self._error_hold_streak = 0
                self._hold_ended_ts = 0.0
                self._streak_scope = ""
                self._streak_target = ""
                self.save()

    def mark_hold_ended(self) -> None:
        """解除或到期后保留 streak，等待成功调用再清零。"""
        with self._lock:
            self._hold_ended_ts = time.time()
            self.save()

    def streak_target(self) -> tuple[str, str]:
        with self._lock:
            return str(self._streak_scope or ""), str(self._streak_target or "")

    def _sync_expiry_locked(self, now: float) -> None:
        """清理到期停模；仅错误停模到期时写入 hold_ended_ts。"""
        changed = False
        hold = self._hold
        if float(hold.error_until_ts or 0) > 0 and float(hold.error_until_ts) <= now:
            if self._error_hold_streak > 0:
                self._hold_ended_ts = now
            hold = HoldInfo(
                until_ts=float(hold.until_ts or 0),
                error_until_ts=0.0,
                reason=str(hold.reason or ""),
                rule_scope=str(hold.rule_scope or ""),
                rule_name=str(hold.rule_name or ""),
                error_type=str(hold.error_type or ""),
                activated_ts=float(hold.activated_ts or now),
            )
            self._hold = hold
            changed = True
        if float(hold.until_ts or 0) > 0 and float(hold.until_ts) <= now:
            self._hold = HoldInfo(
                until_ts=0.0,
                error_until_ts=0.0,
                reason="",
                rule_scope=str(hold.rule_scope or ""),
                rule_name=str(hold.rule_name or ""),
                error_type=str(hold.error_type or ""),
                activated_ts=float(hold.activated_ts or now),
            )
            changed = True
        if changed:
            self.save()

    def is_error_holding(self, now: Optional[float] = None) -> bool:
        ts = time.time() if now is None else float(now)
        with self._lock:
            self._sync_expiry_locked(ts)
            return float(self._hold.error_until_ts or 0) > ts

    def is_holding(self, now: Optional[float] = None) -> bool:
        ts = time.time() if now is None else float(now)
        with self._lock:
            self._sync_expiry_locked(ts)
            return float(self._hold.until_ts or 0) > ts

    def hold_info(self) -> HoldInfo:
        with self._lock:
            self._sync_expiry_locked(time.time())
            return HoldInfo(
                until_ts=self._hold.until_ts,
                error_until_ts=self._hold.error_until_ts,
                reason=self._hold.reason,
                rule_scope=self._hold.rule_scope,
                rule_name=self._hold.rule_name,
                error_type=self._hold.error_type,
                activated_ts=self._hold.activated_ts,
            )

    def record_rate_limit(
        self,
        *,
        scope: str = "",
        target: str = "",
        metric: str = "",
        kind: str = "rate",
        reason: str = "",
    ) -> None:
        event = {
            "ts": time.time(),
            "scope": str(scope or ""),
            "target": str(target or ""),
            "metric": str(metric or ""),
            "kind": str(kind or "rate"),
            "reason": str(reason or "")[:300],
        }
        with self._lock:
            self._rate_limit_events.append(event)
            self._prune_locked(event["ts"])
            self.save()

    def count_rate_limits(
        self,
        *,
        start_ts: float,
        end_ts: float,
        scope: str = "",
        target: str = "",
    ) -> int:
        start = float(start_ts)
        end = float(end_ts)
        want_scope = str(scope or "").strip()
        want_target = str(target or "").strip()
        total = 0
        with self._lock:
            for event in self._rate_limit_events:
                ts = float(event.get("ts") or 0)
                if ts < start or ts > end:
                    continue
                if want_scope and str(event.get("scope") or "") != want_scope:
                    continue
                if want_target and str(event.get("target") or "") != want_target:
                    continue
                total += 1
        return total

    def activate_hold(
        self,
        *,
        seconds: float,
        reason: str,
        rule_scope: str = "",
        rule_name: str = "",
        error_type: str = "",
        source: str = "rate",
    ) -> bool:
        """激活或延长停模；source=error 时单独记录错误停模截止时间。若整体停模为新触发返回 True。"""

        now = time.time()
        seconds = max(0.0, float(seconds))
        source_key = str(source or "rate").strip().lower() or "rate"
        with self._lock:
            self._sync_expiry_locked(now)
            was_active = float(self._hold.until_ts or 0) > now
            until = now + seconds
            if was_active and float(self._hold.until_ts) > until:
                until = float(self._hold.until_ts)

            error_until = float(self._hold.error_until_ts or 0)
            scope = str(rule_scope or self._hold.rule_scope or "")
            name = str(rule_name or self._hold.rule_name or "")
            etype = str(error_type or self._hold.error_type or "")
            if source_key == "error":
                error_until = now + seconds
                self._streak_scope = scope
                self._streak_target = name
            elif error_until <= now:
                error_until = 0.0

            self._hold = HoldInfo(
                until_ts=until,
                error_until_ts=error_until if error_until > now else 0.0,
                reason=str(reason or ""),
                rule_scope=scope,
                rule_name=name,
                error_type=etype,
                activated_ts=self._hold.activated_ts if was_active else now,
            )
            self.save()
            return not was_active

    def clear_hold(self) -> bool:
        with self._lock:
            now = time.time()
            had = float(self._hold.until_ts or 0) > now or float(self._hold.error_until_ts or 0) > now
            had_error = float(self._hold.error_until_ts or 0) > now
            scope = str(self._hold.rule_scope or "")
            name = str(self._hold.rule_name or "")
            etype = str(self._hold.error_type or "")
            self._hold = HoldInfo(rule_scope=scope, rule_name=name, error_type=etype)
            if had:
                if had_error or self._error_hold_streak > 0:
                    self._hold_ended_ts = now
                self.save()
            return had

    def error_type_distribution(
        self,
        *,
        scope: str,
        target: str,
        window_seconds: float,
        feature_models: Optional[Dict[str, List[str]]] = None,
        now: Optional[float] = None,
    ) -> Dict[str, int]:
        from .error_classify import error_type_matches

        events = self.events_since(window_seconds, now=now)
        want = str(target or "").strip()
        scope_key = str(scope or "").strip().lower()
        models = {
            str(m).strip()
            for m in ((feature_models or {}).get(want) or [])
            if str(m).strip()
        }
        dist: Dict[str, int] = {}
        for event in events:
            matched = False
            if scope_key == "provider" and event.provider == want:
                matched = True
            elif scope_key == "model" and event.model == want:
                matched = True
            elif scope_key == "feature":
                matched = event.feature == want or (bool(models) and event.model in models)
            if not matched:
                continue
            key = str(event.error_type or "other").strip() or "other"
            dist[key] = int(dist.get(key) or 0) + 1
        return dist

    def snapshot(
        self,
        *,
        stats_window: float,
        host_success: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        """错误来自本地窗口；成功次数必须来自宿主 llm_usage。"""

        now = time.time()
        events = self.events_since(stats_window, now=now)
        models: Dict[str, Dict[str, Any]] = {}

        for event in events:
            key = event.model or "(unknown)"
            bucket = models.setdefault(
                key,
                {
                    "model": key,
                    "provider": event.provider,
                    "success": 0,
                    "error": 0,
                    "by_type": {},
                    "recent_reasons": [],
                },
            )
            if event.provider and not bucket.get("provider"):
                bucket["provider"] = event.provider
            bucket["error"] += 1
            t = event.error_type or "other"
            by_type = bucket["by_type"]
            by_type[t] = int(by_type.get(t) or 0) + 1
            if event.message:
                reasons: List[str] = bucket["recent_reasons"]
                if event.message not in reasons:
                    reasons.append(event.message)
                    if len(reasons) > 5:
                        del reasons[:-5]

        for key, info in host_success.items():
            name = str(key or "").strip() or "unknown"
            bucket = models.setdefault(
                name,
                {
                    "model": name,
                    "provider": str((info or {}).get("provider") or ""),
                    "success": 0,
                    "error": 0,
                    "by_type": {},
                    "recent_reasons": [],
                },
            )
            bucket["success"] = int((info or {}).get("success") or 0)
            provider = str((info or {}).get("provider") or "").strip()
            if provider:
                bucket["provider"] = provider

        model_rows = []
        for item in models.values():
            ok = int(item["success"])
            err = int(item["error"])
            total = ok + err
            item["total"] = total
            item["error_rate"] = (err / total * 100.0) if total else 0.0
            model_rows.append(item)
        model_rows.sort(key=lambda x: (-int(x["error"]), -int(x["success"]), str(x["model"])))

        hold = self.hold_info()
        return {
            "holding": hold.is_active(now),
            "hold": {
                "remaining": hold.remaining_seconds(now),
                "reason": hold.reason,
                "rule_scope": hold.rule_scope,
                "rule_name": hold.rule_name,
                "error_type": hold.error_type,
            },
            "stats_window": float(stats_window),
            "models": model_rows,
            "event_count": len(events),
        }
