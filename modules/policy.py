"""错误阈值规则 → 停模。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .error_classify import classify_error, error_type_matches
from .state import HoldOnState


@dataclass(frozen=True)
class FeatureModels:
    feature: str
    models: Tuple[str, ...]


@dataclass(frozen=True)
class ThresholdRule:
    scope: str  # model / provider / feature
    name: str = ""
    error_type: str = "*"
    window_seconds: int = 60
    threshold: int = 5
    hold_seconds: int = 90


@dataclass(frozen=True)
class HoldEvent:
    reason: str
    scope: str = ""
    name: str = ""
    error_type: str = ""
    count: int = 0
    threshold: int = 0
    window_seconds: int = 0


class HoldOnPolicy:
    def __init__(
        self,
        state: HoldOnState,
        *,
        rules: Sequence[ThresholdRule],
        features: Sequence[FeatureModels],
        model_providers: Dict[str, str],
        stats_window_seconds: int = 600,
    ) -> None:
        self.state = state
        self.rules = [r for r in rules if int(r.threshold or 0) > 0]
        self.features = {f.feature: tuple(f.models) for f in features if f.feature}
        self.model_providers = {str(k): str(v) for k, v in (model_providers or {}).items() if k}
        self.stats_window_seconds = max(60, int(stats_window_seconds or 600))
        for model, provider in self.model_providers.items():
            self.state.register_model_provider(model, provider)

    def provider_of(self, model_name: str) -> str:
        name = str(model_name or "").strip()
        if not name:
            return ""
        return self.model_providers.get(name) or self.state.provider_of(name)

    def features_of(self, model_name: str) -> List[str]:
        name = str(model_name or "").strip()
        if not name:
            return []
        return [feat for feat, models in self.features.items() if name in models]

    def on_error(
        self,
        *,
        model_name: str,
        provider: str = "",
        message: str = "",
        error_type: str = "",
        error: Optional[dict] = None,
    ) -> Optional[HoldEvent]:
        model = str(model_name or "").strip()
        if not model:
            return None
        prov = str(provider or "").strip() or self.provider_of(model)
        etype = str(error_type or "").strip() or classify_error(message, error=error)
        self.state.record_error(
            model=model,
            provider=prov,
            error_type=etype,
            message=message,
        )
        return self._evaluate_rules(model=model, provider=prov, error_type=etype)

    def _evaluate_rules(
        self, *, model: str, provider: str, error_type: str
    ) -> Optional[HoldEvent]:
        triggered: Optional[HoldEvent] = None
        for rule in self.rules:
            hit = self._rule_hit_count(rule, model=model, provider=provider, error_type=error_type)
            if hit is None:
                continue
            target_name, count = hit
            if count < int(rule.threshold):
                continue
            reason = (
                f"{rule.scope}:{target_name or '*'} 类型 {rule.error_type or '*'} "
                f"在 {rule.window_seconds}s 内达到 {count}/{rule.threshold}"
            )
            newly = self.state.activate_hold(
                seconds=float(rule.hold_seconds or 90),
                reason=reason,
                rule_scope=rule.scope,
                rule_name=target_name,
                error_type=rule.error_type or error_type,
            )
            if newly and triggered is None:
                triggered = HoldEvent(
                    reason=reason,
                    scope=rule.scope,
                    name=target_name,
                    error_type=rule.error_type or error_type,
                    count=count,
                    threshold=int(rule.threshold),
                    window_seconds=int(rule.window_seconds),
                )
        return triggered

    def _rule_hit_count(
        self,
        rule: ThresholdRule,
        *,
        model: str,
        provider: str,
        error_type: str,
    ) -> Optional[Tuple[str, int]]:
        if not error_type_matches(rule.error_type, error_type):
            return None

        scope = str(rule.scope or "model").strip().lower()
        want_name = str(rule.name or "").strip()
        window = max(1, int(rule.window_seconds or 60))

        if scope == "model":
            if want_name and want_name != model:
                return None
            target = want_name or model
            count = self.state.count_errors(
                window_seconds=window,
                error_type=rule.error_type,
                model=target,
            )
            return target, count

        if scope == "provider":
            if not provider:
                return None
            if want_name and want_name != provider:
                return None
            target = want_name or provider
            count = self.state.count_errors(
                window_seconds=window,
                error_type=rule.error_type,
                provider=target,
            )
            return target, count

        if scope == "feature":
            feats = self.features_of(model)
            if not feats:
                return None
            if want_name:
                if want_name not in feats:
                    return None
                feat_names = [want_name]
            else:
                feat_names = feats
            best: Optional[Tuple[str, int]] = None
            for feat in feat_names:
                models = list(self.features.get(feat) or ())
                if not models:
                    continue
                count = self.state.count_errors(
                    window_seconds=window,
                    error_type=rule.error_type,
                    models=models,
                )
                if best is None or count > best[1]:
                    best = (feat, count)
            return best

        return None

    def is_holding(self) -> bool:
        return self.state.is_holding()

    def status_text(self, host_success: Dict[str, Dict]) -> str:
        snap = self.state.snapshot(
            stats_window=float(self.stats_window_seconds),
            host_success=host_success,
        )
        lines = ["【稍等状态】"]
        if snap["holding"]:
            hold = snap["hold"]
            lines.append(f"停模中: 是（剩余 {int(hold['remaining'])}s）")
            if hold.get("reason"):
                lines.append(f"原因: {hold['reason']}")
        else:
            lines.append("停模中: 否")

        lines.append(f"统计窗口: {int(snap['stats_window'])}s")
        models = snap.get("models") or []
        if not models:
            lines.append("暂无模型调用统计。")
            return "\n".join(lines)

        lines.append("模型出错率:")
        for item in models:
            ok = int(item.get("success") or 0)
            err = int(item.get("error") or 0)
            rate = float(item.get("error_rate") or 0.0)
            provider = str(item.get("provider") or "")
            label = str(item.get("model") or "")
            if provider:
                label = f"{label} ({provider})"
            lines.append(f"- {label}: 成功 {ok} / 失败 {err}  出错率 {rate:.1f}%")
            by_type = item.get("by_type") or {}
            if by_type:
                parts = [f"{k}×{v}" for k, v in sorted(by_type.items(), key=lambda x: (-x[1], x[0]))]
                lines.append(f"  类型: {', '.join(parts)}")
            for reason in (item.get("recent_reasons") or [])[:3]:
                short = " ".join(str(reason).split())
                if len(short) > 120:
                    short = short[:120] + "…"
                lines.append(f"  · {short}")
        return "\n".join(lines)
