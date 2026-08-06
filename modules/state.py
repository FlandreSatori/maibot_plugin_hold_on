"""错误统计与停模状态（内存 + 可选落盘）。"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class StatEvent:
    """一次成功或失败事件。"""

    ts: float
    kind: str  # success / error
    model: str = ""
    provider: str = ""
    error_type: str = ""
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "StatEvent":
        return cls(
            ts=float(raw.get("ts") or 0),
            kind=str(raw.get("kind") or ""),
            model=str(raw.get("model") or ""),
            provider=str(raw.get("provider") or ""),
            error_type=str(raw.get("error_type") or ""),
            message=str(raw.get("message") or ""),
        )


@dataclass
class HoldInfo:
    until_ts: float = 0.0
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
    """滑动窗口事件 + 全局停模。"""

    def __init__(self, path: Path, *, max_events: int = 5000) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._events: List[StatEvent] = []
        self._max_events = max(500, int(max_events or 5000))
        self._hold = HoldInfo()
        self._model_provider: Dict[str, str] = {}
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
                    if isinstance(item, dict):
                        loaded.append(StatEvent.from_dict(item))
                self._events = loaded[-self._max_events :]
            hold = raw.get("hold")
            if isinstance(hold, dict):
                self._hold = HoldInfo(
                    until_ts=float(hold.get("until_ts") or 0),
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

    def save(self) -> None:
        with self._lock:
            self._prune_locked(time.time(), keep_seconds=7 * 24 * 3600)
            payload = {
                "events": [e.to_dict() for e in self._events[-self._max_events :]],
                "hold": {
                    "until_ts": self._hold.until_ts,
                    "reason": self._hold.reason,
                    "rule_scope": self._hold.rule_scope,
                    "rule_name": self._hold.rule_name,
                    "error_type": self._hold.error_type,
                    "activated_ts": self._hold.activated_ts,
                },
                "model_provider": dict(self._model_provider),
                "saved_at": time.time(),
            }
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)

    def _prune_locked(self, now: float, keep_seconds: float = 7 * 24 * 3600) -> None:
        cutoff = now - max(3600.0, float(keep_seconds))
        self._events = [e for e in self._events if e.ts >= cutoff]
        if len(self._events) > self._max_events:
            self._events = self._events[-self._max_events :]

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

    def record_success(self, *, model: str, provider: str = "") -> None:
        model = str(model or "").strip()
        if not model:
            return
        provider = str(provider or "").strip() or self.provider_of(model)
        event = StatEvent(ts=time.time(), kind="success", model=model, provider=provider)
        with self._lock:
            if provider:
                self._model_provider[model] = provider
            self._events.append(event)
            self._prune_locked(event.ts)
            self.save()

    def record_error(
        self,
        *,
        model: str,
        provider: str = "",
        error_type: str = "other",
        message: str = "",
    ) -> StatEvent:
        model = str(model or "").strip()
        provider = str(provider or "").strip() or (self.provider_of(model) if model else "")
        event = StatEvent(
            ts=time.time(),
            kind="error",
            model=model,
            provider=provider,
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

    def count_errors(
        self,
        *,
        window_seconds: float,
        error_type: str = "",
        model: str = "",
        provider: str = "",
        models: Optional[List[str]] = None,
        now: Optional[float] = None,
    ) -> int:
        """按条件统计窗口内错误次数。"""

        from .error_classify import error_type_matches

        events = self.events_since(window_seconds, now=now)
        model_set = {str(m).strip() for m in (models or []) if str(m).strip()}
        want_model = str(model or "").strip()
        want_provider = str(provider or "").strip()
        total = 0
        for event in events:
            if event.kind != "error":
                continue
            if want_model and event.model != want_model:
                continue
            if model_set and event.model not in model_set:
                continue
            if want_provider and event.provider != want_provider:
                continue
            if not error_type_matches(error_type, event.error_type):
                continue
            total += 1
        return total

    def is_holding(self, now: Optional[float] = None) -> bool:
        with self._lock:
            if not self._hold.is_active(now):
                if self._hold.until_ts:
                    self._hold = HoldInfo()
                    self.save()
                return False
            return True

    def hold_info(self) -> HoldInfo:
        with self._lock:
            return HoldInfo(
                until_ts=self._hold.until_ts,
                reason=self._hold.reason,
                rule_scope=self._hold.rule_scope,
                rule_name=self._hold.rule_name,
                error_type=self._hold.error_type,
                activated_ts=self._hold.activated_ts,
            )

    def activate_hold(
        self,
        *,
        seconds: float,
        reason: str,
        rule_scope: str = "",
        rule_name: str = "",
        error_type: str = "",
    ) -> bool:
        """激活或延长停模；若为新触发（此前未在停模）返回 True。"""

        now = time.time()
        seconds = max(0.0, float(seconds))
        with self._lock:
            was_active = self._hold.is_active(now)
            until = now + seconds
            if was_active and self._hold.until_ts > until:
                until = self._hold.until_ts
            self._hold = HoldInfo(
                until_ts=until,
                reason=str(reason or ""),
                rule_scope=str(rule_scope or ""),
                rule_name=str(rule_name or ""),
                error_type=str(error_type or ""),
                activated_ts=self._hold.activated_ts if was_active else now,
            )
            self.save()
            return not was_active

    def clear_hold(self) -> bool:
        with self._lock:
            had = self._hold.is_active() or bool(self._hold.until_ts)
            self._hold = HoldInfo()
            if had:
                self.save()
            return had

    def clear_all(self) -> int:
        with self._lock:
            n = len(self._events) + (1 if self._hold.until_ts else 0)
            self._events.clear()
            self._hold = HoldInfo()
            self.save()
            return n

    def snapshot(self, *, stats_window: float = 600.0) -> Dict[str, Any]:
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
            if event.kind == "success":
                bucket["success"] += 1
            elif event.kind == "error":
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

        model_rows = []
        for item in models.values():
            ok = int(item["success"])
            err = int(item["error"])
            total = ok + err
            rate = (err / total * 100.0) if total else 0.0
            item["total"] = total
            item["error_rate"] = rate
            model_rows.append(item)
        model_rows.sort(key=lambda x: (-int(x["error"]), str(x["model"])))

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
