"""限流 / 禁用 / 功能全灭策略。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from .state import HoldOnState


@dataclass(frozen=True)
class NamedLimit:
    name: str
    max_requests_per_minute: int = 0
    disable_seconds: int = 90
    provider: str = ""


@dataclass(frozen=True)
class FeatureModels:
    feature: str
    models: Tuple[str, ...]


@dataclass(frozen=True)
class PolicyEvent:
    """策略新触发事件（用于通知转发，避免重复刷屏）。"""

    kind: str  # rpm_global / rpm_provider / rpm_model / error_model / global_stop
    reason: str
    scope: str = ""
    key: str = ""


class HoldOnPolicy:
    """根据配置与状态判定是否拦截、禁用多久。"""

    def __init__(
        self,
        state: HoldOnState,
        *,
        global_max_rpm: int,
        global_disable_seconds: int,
        provider_limits: Sequence[NamedLimit],
        model_limits: Sequence[NamedLimit],
        features: Sequence[FeatureModels],
        error_base_seconds: int,
        error_max_seconds: int,
        error_exponential: bool,
        error_multiplier: float,
        feature_kill_enabled: bool,
    ) -> None:
        self.state = state
        self.global_max_rpm = max(0, int(global_max_rpm or 0))
        self.global_disable_seconds = max(0, int(global_disable_seconds or 0))
        self.provider_limits = {p.name: p for p in provider_limits if p.name}
        self.model_limits = {m.name: m for m in model_limits if m.name}
        self.model_provider = {
            m.name: m.provider for m in model_limits if m.name and m.provider
        }
        self.features = {f.feature: tuple(f.models) for f in features if f.feature}
        self.error_base_seconds = max(1, int(error_base_seconds or 60))
        self.error_max_seconds = max(self.error_base_seconds, int(error_max_seconds or 3600))
        self.error_exponential = bool(error_exponential)
        self.error_multiplier = max(1.0, float(error_multiplier or 2.0))
        self.feature_kill_enabled = bool(feature_kill_enabled)

    def provider_of(self, model_name: str) -> str:
        name = str(model_name or "").strip()
        if not name:
            return ""
        if name in self.model_provider:
            return self.model_provider[name]
        limit = self.model_limits.get(name)
        return str(limit.provider or "").strip() if limit else ""

    def register_model_provider(self, model_name: str, provider: str) -> None:
        model = str(model_name or "").strip()
        prov = str(provider or "").strip()
        if model and prov and model not in self.model_provider:
            self.model_provider[model] = prov

    def is_model_blocked(self, model_name: str) -> bool:
        name = str(model_name or "").strip()
        if not name:
            return False
        if self.state.global_stop:
            return True
        if self.state.is_disabled("model", name):
            return True
        provider = self.provider_of(name)
        if provider and self.state.is_disabled("provider", provider):
            return True
        if self.state.is_disabled("global", "all"):
            return True
        return False

    def note_request(self, *, model_name: str = "", provider: str = "") -> List[PolicyEvent]:
        """记录一次请求；若**新**触达限流则返回事件列表。"""

        if self.state.global_stop or self.state.is_disabled("global", "all"):
            return []

        model = str(model_name or "").strip()
        prov = str(provider or "").strip() or self.provider_of(model)

        if model and self.state.is_disabled("model", model):
            return []
        if prov and self.state.is_disabled("provider", prov):
            return []

        events: List[PolicyEvent] = []

        global_count = self.state.record_hit("global")
        if self.global_max_rpm > 0 and global_count > self.global_max_rpm:
            reason = f"全局 RPM 超限（{global_count}/{self.global_max_rpm}）"
            events.append(
                PolicyEvent(kind="rpm_global", reason=reason, scope="global", key="all")
            )
            if self._activate_global_stop(reason, source="ratelimit"):
                events.append(
                    PolicyEvent(kind="global_stop", reason=reason, scope="global", key="all")
                )
            return events

        if prov:
            plimit = self.provider_limits.get(prov)
            if plimit and plimit.max_requests_per_minute > 0:
                count = self.state.record_hit(f"provider:{prov}")
                if count > plimit.max_requests_per_minute:
                    seconds = plimit.disable_seconds or self.global_disable_seconds or 90
                    reason = f"厂商 RPM 超限（{count}/{plimit.max_requests_per_minute}）"
                    self.state.set_disable(
                        scope="provider",
                        key=prov,
                        seconds=seconds,
                        reason=reason,
                        source="ratelimit",
                    )
                    events.append(
                        PolicyEvent(kind="rpm_provider", reason=reason, scope="provider", key=prov)
                    )
                    kill = self._maybe_feature_kill()
                    if kill is not None:
                        events.append(kill)
                    return events

        if model:
            mlimit = self.model_limits.get(model)
            if mlimit and mlimit.max_requests_per_minute > 0:
                count = self.state.record_hit(f"model:{model}")
                if count > mlimit.max_requests_per_minute:
                    seconds = mlimit.disable_seconds or self.global_disable_seconds or 90
                    reason = f"模型 RPM 超限（{count}/{mlimit.max_requests_per_minute}）"
                    self.state.set_disable(
                        scope="model",
                        key=model,
                        seconds=seconds,
                        reason=reason,
                        source="ratelimit",
                        error_streak=0,
                    )
                    events.append(
                        PolicyEvent(kind="rpm_model", reason=reason, scope="model", key=model)
                    )
                    kill = self._maybe_feature_kill()
                    if kill is not None:
                        events.append(kill)
                    return events
            else:
                self.state.record_hit(f"model:{model}")

        return []

    def compute_error_disable_seconds(self, streak: int, retry_after: Optional[float] = None) -> float:
        streak = max(1, int(streak or 1))
        if self.error_exponential:
            seconds = self.error_base_seconds * math.pow(self.error_multiplier, streak - 1)
        else:
            seconds = float(self.error_base_seconds)
        seconds = min(float(self.error_max_seconds), float(seconds))
        if retry_after is not None:
            try:
                seconds = max(seconds, float(retry_after))
            except (TypeError, ValueError):
                pass
        return seconds

    def disable_for_error(
        self,
        *,
        model_name: str,
        provider: str = "",
        message: str = "",
        retry_after: Optional[float] = None,
    ) -> List[PolicyEvent]:
        """任意模型错误均禁用；返回新触发的通知事件（错误禁用 / 全局停模）。"""

        model = str(model_name or "").strip()
        if not model:
            return []
        prov = str(provider or "").strip() or self.provider_of(model)
        self.register_model_provider(model, prov)
        was_disabled = self.state.is_disabled("model", model)
        streak = self.state.bump_error_streak("model", model)
        seconds = self.compute_error_disable_seconds(streak, retry_after)
        detail = self._short(message) if message else "模型请求失败"
        reason = f"模型错误（第 {streak} 次）: {detail}"
        self.state.set_disable(
            scope="model",
            key=model,
            seconds=seconds,
            reason=reason,
            source="error",
            error_streak=streak,
        )
        events: List[PolicyEvent] = []
        # 仅在「新进入禁用」时通知，避免连击延长时刷屏
        if not was_disabled:
            events.append(
                PolicyEvent(kind="error_model", reason=reason, scope="model", key=model)
            )
        kill = self._maybe_feature_kill()
        if kill is not None:
            events.append(kill)
        return events

    def on_success(self, model_name: str) -> None:
        model = str(model_name or "").strip()
        if model:
            self.state.reset_error_streak("model", model)

    def _activate_global_stop(self, reason: str, *, source: str) -> bool:
        """置全局停模；若为新触发返回 True。"""

        already = bool(self.state.global_stop or self.state.is_disabled("global", "all"))
        self.state.set_global_stop(True, reason)
        self.state.set_disable(
            scope="global",
            key="all",
            seconds=self.global_disable_seconds or 90,
            reason=reason,
            source=source,
        )
        return not already

    def _maybe_feature_kill(self) -> Optional[PolicyEvent]:
        if not self.feature_kill_enabled or not self.features:
            return None
        for feature, models in self.features.items():
            names = [m for m in models if str(m).strip()]
            if not names:
                continue
            if all(self.state.is_disabled("model", m) for m in names):
                reason = f"功能 {feature} 的全部模型均已禁用: {', '.join(names)}"
                if self._activate_global_stop(reason, source="feature_kill"):
                    return PolicyEvent(
                        kind="global_stop",
                        reason=reason,
                        scope="global",
                        key="all",
                    )
                return None
        return None

    def refresh_feature_kill(self) -> None:
        if not self.state.global_stop:
            return
        source_entry = self.state.get_disable("global", "all")
        if source_entry and source_entry.source not in ("feature_kill", "ratelimit", ""):
            return
        if self.feature_kill_enabled and self.features:
            for _feature, models in self.features.items():
                names = [m for m in models if str(m).strip()]
                if names and all(self.state.is_disabled("model", m) for m in names):
                    return
        if self.state.is_disabled("global", "all"):
            return
        if source_entry is None or not source_entry.is_active():
            self.state.set_global_stop(False)

    def any_configured_models_available(self) -> bool:
        """若配置了 feature 模型，是否仍有任一可用。"""

        names: List[str] = []
        for models in self.features.values():
            names.extend(m for m in models if m)
        if not names:
            return True
        return any(not self.is_model_blocked(m) for m in names)

    @staticmethod
    def _short(text: str, limit: int = 160) -> str:
        normalized = " ".join(str(text or "").split())
        if len(normalized) <= limit:
            return normalized
        return normalized[:limit] + "…"

    def status_text(self) -> str:
        self.refresh_feature_kill()
        snap = self.state.snapshot()
        lines = ["【稍等状态】"]
        if snap["global_stop"]:
            lines.append(f"全局停模: 是（{snap['global_stop_reason'] or '无原因'}）")
        else:
            lines.append("全局停模: 否")
        disables = snap.get("disables") or []
        if not disables:
            lines.append("当前无禁用项。")
            return "\n".join(lines)
        lines.append(f"禁用项（{len(disables)}）:")
        for item in disables:
            rem = item.get("remaining_seconds", 0)
            lines.append(
                f"- [{item.get('scope')}] {item.get('key')} "
                f"剩余 {rem}s | source={item.get('source')} | {item.get('reason')}"
            )
        return "\n".join(lines)
