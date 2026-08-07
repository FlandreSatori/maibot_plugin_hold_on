"""静态速率与动态预算控制。"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, Optional

@dataclass(frozen=True)
class LimitRule:
    scope: str
    target: str
    metric: str
    window_seconds: int
    limit: float
    input_weight: float = 1.0
    output_weight: float = 1.0

@dataclass(frozen=True)
class BudgetRule:
    scope: str
    target: str
    metric: str
    amount: float
    start: datetime
    end: datetime
    input_weight: float = 1.0
    output_weight: float = 1.0
    strategy: str = "strict"
    overshoot_ratio: float = 0.0

@dataclass(frozen=True)
class Decision:
    blocked: bool
    reason: str = ""
    scope: str = ""
    target: str = ""
    metric: str = ""
    actual: float = 0.0
    limit: float = 0.0
    remaining: float = 0.0


def metric_value(group: Dict[str, Any], metric: str, input_weight: float = 1.0, output_weight: float = 1.0) -> float:
    if metric == "requests": return float(group.get("requests") or 0)
    if metric == "cost": return float(group.get("cost") or 0)
    return float(group.get("input_tokens") or 0) * input_weight + float(group.get("output_tokens") or 0) * output_weight


def matches(group: Dict[str, Any], scope: str, target: str) -> bool:
    return not target or str(group.get(scope) or "") == target


def check_static(groups: Iterable[Dict[str, Any]], rule: LimitRule) -> Optional[Decision]:
    for group in groups:
        if matches(group, rule.scope, rule.target):
            value = metric_value(group, rule.metric, rule.input_weight, rule.output_weight)
            if value >= rule.limit:
                return Decision(True, f"{rule.scope}:{rule.target or group.get(rule.scope)} {rule.metric} {value:g}/{rule.limit:g}", rule.scope, str(rule.target or group.get(rule.scope) or ""), rule.metric, value, rule.limit)
    return None


def check_budget(groups: Iterable[Dict[str, Any]], rule: BudgetRule, now: datetime) -> Optional[Decision]:
    elapsed = max(0.0, min((now - rule.start).total_seconds(), (rule.end - rule.start).total_seconds()))
    duration = max(1.0, (rule.end - rule.start).total_seconds())
    expected = rule.amount * elapsed / duration
    actual = 0.0
    for group in groups:
        if matches(group, rule.scope, rule.target):
            actual += metric_value(group, rule.metric, rule.input_weight, rule.output_weight)
    allowance = rule.amount * (1.0 + max(0.0, rule.overshoot_ratio))
    if rule.strategy == "strict": allowance = expected
    elif rule.strategy == "balanced": allowance = expected + (rule.amount - expected) * max(0.0, min(rule.overshoot_ratio, 1.0))
    remaining = max(0.0, rule.amount - actual)
    if actual > allowance:
        return Decision(True, f"{rule.scope}:{rule.target or '*'} {rule.metric} 预算超速 {actual:g}/{allowance:g}", rule.scope, rule.target, rule.metric, actual, allowance, remaining)
    return None
