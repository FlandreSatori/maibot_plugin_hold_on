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
    overshoot_time: float = 300.0

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
    progress = budget_progress(groups, rule, now)
    if progress["actual"] > progress["allowance"]:
        return Decision(
            True,
            f"{rule.scope}:{rule.target or '*'} {rule.metric} 预算超速 {progress['actual']:g}/{progress['allowance']:g}",
            rule.scope,
            rule.target,
            rule.metric,
            progress["actual"],
            progress["allowance"],
            progress["remaining"],
        )
    return None


def budget_progress(groups: Iterable[Dict[str, Any]], rule: BudgetRule, now: datetime) -> Dict[str, float]:
    duration = max(1.0, (rule.end - rule.start).total_seconds())
    elapsed = max(0.0, min((now - rule.start).total_seconds(), duration))
    remaining_seconds = max(0.0, (rule.end - now).total_seconds())
    plan_speed = rule.amount / duration
    expected = plan_speed * elapsed
    actual = 0.0
    for group in groups:
        if matches(group, rule.scope, rule.target):
            actual += metric_value(group, rule.metric, rule.input_weight, rule.output_weight)
    if rule.strategy == "balanced":
        # 允许最多超前 overshoot_time 秒的计划额度
        ahead = plan_speed * max(0.0, float(rule.overshoot_time or 0.0))
        allowance = min(rule.amount, expected + ahead)
    else:
        # strict：不允许超过当前时刻计划曲线
        allowance = expected
    remaining = max(0.0, rule.amount - actual)
    actual_speed = actual / elapsed if elapsed > 0 else 0.0
    recover_speed = remaining / remaining_seconds if remaining_seconds > 0 else 0.0
    return {
        "actual": actual,
        "expected": expected,
        "allowance": allowance,
        "remaining": remaining,
        "elapsed": elapsed,
        "remaining_seconds": remaining_seconds,
        "plan_speed": plan_speed,
        "actual_speed": actual_speed,
        "recover_speed": recover_speed,
    }


def static_progress(groups: Iterable[Dict[str, Any]], rule: LimitRule) -> Dict[str, float]:
    actual = 0.0
    for group in groups:
        if matches(group, rule.scope, rule.target):
            actual += metric_value(group, rule.metric, rule.input_weight, rule.output_weight)
    window = max(1.0, float(rule.window_seconds))
    return {
        "actual": actual,
        "limit": float(rule.limit),
        "remaining": max(0.0, float(rule.limit) - actual),
        "actual_speed": actual / window,
        "plan_speed": float(rule.limit) / window,
    }