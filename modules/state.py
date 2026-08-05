"""限流计数与禁用状态（内存 + 可选落盘）。"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class DisableEntry:
    """一条禁用记录。"""

    scope: str  # global / provider / model
    key: str
    until_ts: float
    reason: str = ""
    source: str = ""  # ratelimit / error / manual / feature_kill
    error_streak: int = 0
    created_ts: float = field(default_factory=time.time)

    def remaining_seconds(self, now: Optional[float] = None) -> float:
        ts = time.time() if now is None else now
        return max(0.0, float(self.until_ts) - ts)

    def is_active(self, now: Optional[float] = None) -> bool:
        return self.remaining_seconds(now) > 0


class HoldOnState:
    """滑动窗口计数 + 禁用表。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._hits: Dict[str, List[float]] = {}
        self._disables: Dict[str, DisableEntry] = {}
        self._global_stop: bool = False
        self._global_stop_reason: str = ""
        self._freq_adjusted_chats: Dict[str, float] = {}
        self.load()

    @staticmethod
    def _disable_id(scope: str, key: str) -> str:
        return f"{scope}:{key}"

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
            now = time.time()
            disables = raw.get("disables") or []
            if isinstance(disables, list):
                for item in disables:
                    if not isinstance(item, dict):
                        continue
                    entry = DisableEntry(
                        scope=str(item.get("scope") or ""),
                        key=str(item.get("key") or ""),
                        until_ts=float(item.get("until_ts") or 0),
                        reason=str(item.get("reason") or ""),
                        source=str(item.get("source") or ""),
                        error_streak=int(item.get("error_streak") or 0),
                        created_ts=float(item.get("created_ts") or now),
                    )
                    if entry.scope and entry.key and entry.is_active(now):
                        self._disables[self._disable_id(entry.scope, entry.key)] = entry
            self._global_stop = bool(raw.get("global_stop"))
            self._global_stop_reason = str(raw.get("global_stop_reason") or "")
            freq = raw.get("freq_adjusted_chats") or {}
            if isinstance(freq, dict):
                self._freq_adjusted_chats = {
                    str(k): float(v) for k, v in freq.items() if str(k).strip()
                }

    def save(self) -> None:
        with self._lock:
            self._prune_locked(time.time())
            payload = {
                "disables": [asdict(v) for v in self._disables.values()],
                "global_stop": self._global_stop,
                "global_stop_reason": self._global_stop_reason,
                "freq_adjusted_chats": dict(self._freq_adjusted_chats),
                "saved_at": time.time(),
            }
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)

    def _prune_locked(self, now: float) -> None:
        expired = [k for k, v in self._disables.items() if not v.is_active(now)]
        for key in expired:
            self._disables.pop(key, None)
        # 全局停模若仅由已过期禁用触发，不自动清；需手动或 feature 检查后清

    def record_hit(self, bucket: str, now: Optional[float] = None) -> int:
        """记录一次命中，返回窗口内次数。"""

        ts = time.time() if now is None else now
        key = str(bucket or "").strip()
        if not key:
            return 0
        with self._lock:
            window = self._hits.setdefault(key, [])
            cutoff = ts - 60.0
            window[:] = [t for t in window if t > cutoff]
            window.append(ts)
            return len(window)

    def window_count(self, bucket: str, now: Optional[float] = None) -> int:
        ts = time.time() if now is None else now
        key = str(bucket or "").strip()
        if not key:
            return 0
        with self._lock:
            window = self._hits.get(key) or []
            cutoff = ts - 60.0
            alive = [t for t in window if t > cutoff]
            self._hits[key] = alive
            return len(alive)

    def get_disable(self, scope: str, key: str) -> Optional[DisableEntry]:
        with self._lock:
            self._prune_locked(time.time())
            return self._disables.get(self._disable_id(scope, key))

    def is_disabled(self, scope: str, key: str) -> bool:
        entry = self.get_disable(scope, key)
        return bool(entry and entry.is_active())

    def set_disable(
        self,
        *,
        scope: str,
        key: str,
        seconds: float,
        reason: str,
        source: str,
        error_streak: Optional[int] = None,
    ) -> DisableEntry:
        now = time.time()
        with self._lock:
            did = self._disable_id(scope, key)
            prev = self._disables.get(did)
            streak = int(error_streak) if error_streak is not None else (prev.error_streak if prev else 0)
            until = now + max(0.0, float(seconds))
            if prev and prev.is_active(now) and prev.until_ts > until:
                until = prev.until_ts
            entry = DisableEntry(
                scope=scope,
                key=key,
                until_ts=until,
                reason=reason,
                source=source,
                error_streak=streak,
                created_ts=prev.created_ts if prev else now,
            )
            self._disables[did] = entry
            self.save()
            return entry

    def clear_disable(self, scope: str, key: str) -> bool:
        with self._lock:
            did = self._disable_id(scope, key)
            existed = did in self._disables
            self._disables.pop(did, None)
            if existed:
                self.save()
            return existed

    def clear_all(self) -> int:
        with self._lock:
            n = len(self._disables)
            self._disables.clear()
            self._global_stop = False
            self._global_stop_reason = ""
            chats = list(self._freq_adjusted_chats.keys())
            self._freq_adjusted_chats.clear()
            self.save()
            return n + (1 if chats else 0)

    def list_active(self) -> List[DisableEntry]:
        with self._lock:
            now = time.time()
            self._prune_locked(now)
            return sorted(self._disables.values(), key=lambda e: e.until_ts)

    def get_error_streak(self, scope: str, key: str) -> int:
        entry = self.get_disable(scope, key)
        return int(entry.error_streak) if entry else 0

    def bump_error_streak(self, scope: str, key: str) -> int:
        with self._lock:
            did = self._disable_id(scope, key)
            prev = self._disables.get(did)
            streak = (prev.error_streak if prev else 0) + 1
            if prev:
                prev.error_streak = streak
            else:
                self._disables[did] = DisableEntry(
                    scope=scope,
                    key=key,
                    until_ts=0,
                    reason="",
                    source="",
                    error_streak=streak,
                )
            return streak

    def reset_error_streak(self, scope: str, key: str) -> None:
        with self._lock:
            did = self._disable_id(scope, key)
            prev = self._disables.get(did)
            if prev:
                prev.error_streak = 0

    @property
    def global_stop(self) -> bool:
        with self._lock:
            return self._global_stop

    @property
    def global_stop_reason(self) -> str:
        with self._lock:
            return self._global_stop_reason

    def set_global_stop(self, enabled: bool, reason: str = "") -> None:
        with self._lock:
            self._global_stop = bool(enabled)
            self._global_stop_reason = str(reason or "") if enabled else ""
            self.save()

    def mark_freq_adjusted(self, chat_id: str, previous: float) -> None:
        cid = str(chat_id or "").strip()
        if not cid:
            return
        with self._lock:
            if cid not in self._freq_adjusted_chats:
                self._freq_adjusted_chats[cid] = float(previous)
                self.save()

    def pop_freq_adjusted(self) -> Dict[str, float]:
        with self._lock:
            out = dict(self._freq_adjusted_chats)
            self._freq_adjusted_chats.clear()
            self.save()
            return out

    def freq_adjusted_chats(self) -> Dict[str, float]:
        with self._lock:
            return dict(self._freq_adjusted_chats)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            now = time.time()
            self._prune_locked(now)
            return {
                "global_stop": self._global_stop,
                "global_stop_reason": self._global_stop_reason,
                "disables": [
                    {
                        **asdict(e),
                        "remaining_seconds": round(e.remaining_seconds(now), 1),
                    }
                    for e in sorted(self._disables.values(), key=lambda x: x.until_ts)
                ],
            }
