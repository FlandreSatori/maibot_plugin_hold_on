"""错误阈值规则 → 停模（连续触发线性增长停止时长）。"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    hold_max_seconds: int = 3600


@dataclass(frozen=True)
class HoldEvent:
    reason: str
    scope: str = ""
    name: str = ""
    error_type: str = ""
    count: int = 0
    threshold: int = 0
    window_seconds: int = 0
    hold_seconds: int = 0
    streak: int = 1
    distribution: Dict[str, int] = field(default_factory=dict)


SCOPE_LABELS = {
    "feature": "功能",
    "model": "模型",
    "provider": "厂商",
}


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

    def features_of(self, model_name: str, feature: str = "") -> List[str]:
        name = str(model_name or "").strip()
        found = [feat for feat, models in self.features.items() if name and name in models]
        feat = str(feature or "").strip()
        if feat and feat not in found:
            found.append(feat)
        return found

    def on_error(
        self,
        *,
        model_name: str,
        provider: str = "",
        feature: str = "",
        message: str = "",
        error_type: str = "",
        error: Optional[dict] = None,
    ) -> Optional[HoldEvent]:
        model = str(model_name or "").strip()
        if not model:
            return None
        prov = str(provider or "").strip() or self.provider_of(model)
        feat = str(feature or "").strip()
        etype = str(error_type or "").strip() or classify_error(message, error=error)
        self.state.record_error(
            model=model,
            provider=prov,
            feature=feat,
            error_type=etype,
            message=message,
        )
        return self._evaluate_rules(
            model=model,
            provider=prov,
            feature=feat,
            error_type=etype,
        )

    @staticmethod
    def scope_label(scope: str) -> str:
        key = str(scope or "").strip().lower()
        return SCOPE_LABELS.get(key, key or "目标")

    @staticmethod
    def linear_hold_seconds(rule: ThresholdRule, streak: int) -> int:
        """第 N 次连续触发（中间无成功调用）= N × base，并封顶。"""

        base = max(1, int(rule.hold_seconds or 90))
        multiplier = max(1, int(streak or 1))
        seconds = base * multiplier
        cap = max(base, int(rule.hold_max_seconds or 3600))
        return min(cap, seconds)

    def _evaluate_rules(
        self,
        *,
        model: str,
        provider: str,
        feature: str,
        error_type: str,
    ) -> Optional[HoldEvent]:
        triggered: Optional[HoldEvent] = None
        for rule in self.rules:
            hit = self._rule_hit_count(
                rule,
                model=model,
                provider=provider,
                feature=feature,
                error_type=error_type,
            )
            if hit is None:
                continue
            target_name, count = hit
            if count < int(rule.threshold):
                continue

            # 仅在「错误停模」进行中跳过；限速停模不挡错误加档（额度用尽时常一直占着 is_holding）
            if self.state.is_error_holding():
                continue

            streak = self.state.bump_error_hold_streak()
            hold_seconds = self.linear_hold_seconds(rule, streak)
            reason = (
                f"{self.scope_label(rule.scope)}:{target_name or '*'} "
                f"在 {rule.window_seconds}s 内达到 {count}/{rule.threshold}"
            )
            self.state.activate_hold(
                seconds=float(hold_seconds),
                reason=reason,
                rule_scope=rule.scope,
                rule_name=target_name,
                error_type=rule.error_type or error_type,
                source="error",
            )
            if triggered is None:
                dist = self.state.error_type_distribution(
                    scope=rule.scope,
                    target=target_name,
                    window_seconds=float(rule.window_seconds),
                    feature_models={k: list(v) for k, v in self.features.items()},
                )
                triggered = HoldEvent(
                    reason=reason,
                    scope=rule.scope,
                    name=target_name,
                    error_type=rule.error_type or error_type,
                    count=count,
                    threshold=int(rule.threshold),
                    window_seconds=int(rule.window_seconds),
                    hold_seconds=hold_seconds,
                    streak=streak,
                    distribution=dist,
                )
        return triggered

    def _rule_hit_count(
        self,
        rule: ThresholdRule,
        *,
        model: str,
        provider: str,
        feature: str,
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
            feats = self.features_of(model, feature=feature)
            if want_name:
                if want_name not in feats and want_name != feature:
                    return None
                feat_names = [want_name]
            else:
                feat_names = feats or ([feature] if feature else [])
            if not feat_names:
                return None
            best: Optional[Tuple[str, int]] = None
            for feat in feat_names:
                models = list(self.features.get(feat) or ())
                count = self.state.count_errors(
                    window_seconds=window,
                    error_type=rule.error_type,
                    models=models or None,
                    feature=feat,
                )
                if best is None or count > best[1]:
                    best = (feat, count)
            return best

        return None

    def is_holding(self) -> bool:
        return self.state.is_holding()

    @staticmethod
    def format_distribution(distribution: Dict[str, int]) -> str:
        if not distribution:
            return "无详细错误分布"
        parts = [
            f"{name} x{count}"
            for name, count in sorted(distribution.items(), key=lambda x: (-x[1], x[0]))
        ]
        return "，".join(parts)
