"""稍，稍等一下！: LLM 消耗监控与入站限速。"""
from __future__ import annotations
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, Literal, Optional
from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import HookMode, HookOrder
from .modules.controller import (
    BudgetRule,
    Decision,
    LimitRule,
    budget_progress,
    check_budget,
    check_static,
    static_progress,
)
from .modules.error_watch import ErrorSnapshotWatcher, resolve_watch_roots
from .modules.model_discover import discover_models
from .modules.policy import FeatureModels, HoldEvent, HoldOnPolicy, ThresholdRule
from .modules.state import HoldOnState
from .modules.usage_sync import aggregate_usage

class PluginConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0
    enabled: bool = Field(default=True, description="是否启用消耗限速")
    config_version: str = Field(default="3.0.0", description="配置版本")
    auto_detect_models: bool = Field(default=True, description="自动同步模型、厂商和功能")
    model_config_path: str = Field(default="", description="宿主 model_config.toml 路径，留空自动查找")

class StatsConfig(PluginConfigBase):
    __ui_label__ = "统计"
    __ui_icon__ = "bar-chart-2"
    __ui_order__ = 1
    window_seconds: int = Field(default=3600, ge=60, description="控制统计窗口秒数")
    usage_limit: int = Field(default=5000, ge=100, le=5000, description="单次聚合最多读取的成功记录数")

class CatalogModel(PluginConfigBase):
    name: str = Field(default="", description="模型别名")
    provider: str = Field(default="", description="厂商名")
class CatalogFeature(PluginConfigBase):
    feature: str = Field(default="", description="功能名，对应 task_name")
    models: list[str] = Field(default_factory=list, description="功能使用的模型别名")
class CatalogConfig(PluginConfigBase):
    __ui_label__ = "模型列表"
    __ui_icon__ = "book"
    __ui_order__ = 2
    models: list[CatalogModel] = Field(default_factory=list, description="自动同步的模型与厂商")
    features: list[CatalogFeature] = Field(default_factory=list, description="自动同步的功能与模型")

class StaticLimit(PluginConfigBase):
    scope: Literal["provider", "model", "feature"] = Field(default="model", description="限制范围")
    target: str = Field(default="", description="目标名，留空表示各目标分别计算")
    metric: Literal["requests", "tokens", "cost"] = Field(default="requests", description="限制指标")
    window_seconds: int = Field(default=60, ge=1, description="滑动窗口秒数")
    limit: float = Field(default=10, gt=0, description="窗口内允许值")
    input_weight: float = Field(default=1.0, ge=0, description="输入 token 倍率")
    output_weight: float = Field(default=1.0, ge=0, description="输出 token 倍率")
class StaticLimitsConfig(PluginConfigBase):
    __ui_label__ = "静态限制"
    __ui_icon__ = "gauge"
    __ui_order__ = 3
    enabled: bool = Field(default=False, description="动态预算关闭时启用静态限制")
    items: list[StaticLimit] = Field(default_factory=list, description="厂商、模型或功能的限制列表")

class BudgetRuleConfig(PluginConfigBase):
    scope: Literal["provider", "model", "feature"] = Field(default="model", description="预算范围")
    target: str = Field(default="", description="预算目标名")
    metric: Literal["tokens", "cost"] = Field(default="cost", description="预算指标")
    amount: float = Field(default=100, gt=0, description="周期总额度")
    start_time: str = Field(default="08:00", description="每日周期开始，HH:MM")
    end_time: str = Field(default="22:00", description="每日周期结束，HH:MM")
    strategy: Literal["strict", "balanced"] = Field(
        default="strict",
        description="超速策略：strict 不超速；balanced 允许在 overshoot_time 秒内超速",
    )
    overshoot_time: float = Field(default=300, ge=0, description="balanced 下允许超速的秒数")
    off_hours: Literal["hold", "continue"] = Field(
        default="continue",
        description="预算时段外：hold=停止响应；continue=不控速，继续花完剩余额度（用尽后仍停止）",
        json_schema_extra={"label": "时段外行为"},
    )
    input_weight: float = Field(default=1.0, ge=0, description="输入 token 倍率")
    output_weight: float = Field(default=1.0, ge=0, description="输出 token 倍率")
class BudgetConfig(PluginConfigBase):
    __ui_label__ = "动态预算"
    __ui_icon__ = "wallet"
    __ui_order__ = 4
    enabled: bool = Field(default=True, description="启用后只使用动态预算，不叠加静态限制")
    items: list[BudgetRuleConfig] = Field(default_factory=list, description="每日成本或 token 预算")

class ErrorThresholdRule(PluginConfigBase):
    scope: Literal["provider", "model", "feature"] = Field(default="feature", description="计数范围")
    name: str = Field(default="", description="目标名；空表示按实际命中目标各自计数")
    error_type: str = Field(default="*", description="错误类型：429 / 5xx / timeout / *")
    window_seconds: int = Field(default=120, ge=1, description="滑动窗口秒数")
    threshold: int = Field(default=5, ge=1, description="窗口内达到该次数则停入站")
    hold_seconds: int = Field(default=90, ge=1, description="停止秒数")
    hold_max_seconds: int = Field(default=3600, ge=1, description="停止秒数上限")
class ErrorRulesConfig(PluginConfigBase):
    __ui_label__ = "错误阈值"
    __ui_icon__ = "shield-off"
    __ui_order__ = 5
    enabled: bool = Field(default=True, description="错误次数达限时停止入站；与消耗限速可同时生效")
    items: list[ErrorThresholdRule] = Field(
        default_factory=lambda: [
            ErrorThresholdRule(
                scope="feature",
                name="",
                error_type="*",
                window_seconds=120,
                threshold=5,
                hold_seconds=90,
                hold_max_seconds=3600,
            )
        ],
        description="错误阈值规则列表",
    )

class ErrorWatchConfig(PluginConfigBase):
    __ui_label__ = "错误统计"
    __ui_icon__ = "alert-triangle"
    __ui_order__ = 6
    enabled: bool = Field(default=True, description="监听 schema v3 错误快照")
    interval_seconds: float = Field(default=2.0, ge=0.5, description="扫描间隔秒数")
    roots: list[str] = Field(default_factory=list, description="额外错误目录")

class NotifyConfig(PluginConfigBase):
    __ui_label__ = "通知"
    __ui_icon__ = "corner-up-right"
    __ui_order__ = 7
    enabled: bool = Field(default=False, description="停止入站时转发通知")
    target_type: Literal["group", "private", "stream_id"] = Field(default="group", description="通知目标类型")
    group_id: str = Field(default="", description="群号")
    user_id: str = Field(default="", description="用户 ID")
    stream_id: str = Field(default="", description="stream_id")
    platform: str = Field(default="qq", description="平台")
    prefix: str = Field(default="[hold_on]", description="通知前缀")
class PermissionConfig(PluginConfigBase):
    __ui_label__ = "权限"
    __ui_icon__ = "shield"
    __ui_order__ = 8
    whitelist: list[str] = Field(default_factory=list, description="管理命令白名单")
    notify_permission_denied: bool = Field(default=True, description="无权限时是否提示")
class HoldOnConfig(PluginConfigBase):
    plugin: PluginConfig = Field(default_factory=PluginConfig)
    stats: StatsConfig = Field(default_factory=StatsConfig)
    catalog: CatalogConfig = Field(default_factory=CatalogConfig)
    static_limits: StaticLimitsConfig = Field(default_factory=StaticLimitsConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    error_rules: ErrorRulesConfig = Field(default_factory=ErrorRulesConfig)
    error_watch: ErrorWatchConfig = Field(default_factory=ErrorWatchConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    permission: PermissionConfig = Field(default_factory=PermissionConfig)

class HoldOnPlugin(MaiBotPlugin):
    config_model = HoldOnConfig

    async def on_load(self) -> None:
        self.state = HoldOnState(Path(self.ctx.paths.data_dir) / "hold_on_state.json")
        self._catalog = await self._discover_catalog()
        self.policy = self._build_policy()
        self._watcher: Optional[ErrorSnapshotWatcher] = None
        if self.config.error_watch.enabled and self.config.plugin.enabled:
            self._watcher = ErrorSnapshotWatcher(
                roots=resolve_watch_roots(self.config.error_watch.roots),
                interval_seconds=self.config.error_watch.interval_seconds,
                on_error=self._on_snapshot_error,
                logger=self.ctx.logger,
            )
            self._watcher.start()

    async def on_unload(self) -> None:
        if self._watcher:
            await self._watcher.stop()
            self._watcher = None

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        del scope, config_data, version
        self._catalog = await self._discover_catalog()
        self.policy = self._build_policy()

    async def _discover_catalog(self) -> Dict[str, Any]:
        extra = [self.config.plugin.model_config_path] if self.config.plugin.model_config_path.strip() else []
        result = discover_models(extra)
        if result is None:
            return {
                "models": [(m.name, m.provider) for m in self.config.catalog.models],
                "features": {f.feature: tuple(f.models) for f in self.config.catalog.features},
            }
        return {
            "models": [(m.name, m.provider) for m in result.models],
            "features": {f.feature: tuple(f.models) for f in result.features},
        }

    def _build_policy(self) -> HoldOnPolicy:
        catalog = self._catalog or {}
        features = [
            FeatureModels(feature=str(name), models=tuple(models or ()))
            for name, models in (catalog.get("features") or {}).items()
            if str(name or "").strip()
        ]
        for item in self.config.catalog.features or []:
            feat = str(item.feature or "").strip()
            if not feat:
                continue
            if any(f.feature == feat for f in features):
                continue
            features.append(FeatureModels(feature=feat, models=tuple(item.models or ())))
        model_providers = {
            str(name): str(provider or "")
            for name, provider in (catalog.get("models") or [])
            if str(name or "").strip()
        }
        for item in self.config.catalog.models or []:
            name = str(item.name or "").strip()
            if name and name not in model_providers:
                model_providers[name] = str(item.provider or "")
        rules: list[ThresholdRule] = []
        if self.config.error_rules.enabled:
            rules = [
                ThresholdRule(
                    scope=str(r.scope or "model"),
                    name=str(r.name or "").strip(),
                    error_type=str(r.error_type or "*").strip() or "*",
                    window_seconds=int(r.window_seconds or 60),
                    threshold=int(r.threshold or 0),
                    hold_seconds=int(r.hold_seconds or 90),
                    hold_max_seconds=int(r.hold_max_seconds or 3600),
                )
                for r in (self.config.error_rules.items or [])
                if int(r.threshold or 0) > 0
            ]
        return HoldOnPolicy(
            self.state,
            rules=rules,
            features=features,
            model_providers=model_providers,
            stats_window_seconds=int(self.config.stats.window_seconds or 600),
        )

    def _active(self) -> bool:
        return bool(self.config.plugin.enabled)
    async def _usage(self, start: datetime, end: datetime) -> Dict[str, Any]:
        return await aggregate_usage(
            self.ctx,
            start,
            end,
            self.config.stats.usage_limit,
        )

    def _period(self, now: datetime, item: BudgetRuleConfig) -> tuple[datetime, datetime]:
        def parse(value: str) -> time:
            hour, minute = (int(x) for x in value.split(":", 1))
            return time(hour, minute)

        start = datetime.combine(now.date(), parse(item.start_time))
        end = datetime.combine(now.date(), parse(item.end_time))
        if end <= start:
            end += timedelta(days=1)
        if now < start:
            start -= timedelta(days=1)
            end -= timedelta(days=1)
        return start, end

    def _in_budget_hours(self, now: datetime, item: BudgetRuleConfig) -> bool:
        start, end = self._period(now, item)
        return start <= now <= end

    def _seconds_until_next_budget_start(self, now: datetime, item: BudgetRuleConfig) -> float:
        def parse(value: str) -> time:
            hour, minute = (int(x) for x in value.split(":", 1))
            return time(hour, minute)

        candidate = datetime.combine(now.date(), parse(item.start_time))
        if now >= candidate:
            candidate += timedelta(days=1)
        return max(60.0, (candidate - now).total_seconds())

    def _decision(self, metrics: Dict[str, Any], now: datetime) -> Optional[Decision]:
        if self.config.budget.enabled and self.config.budget.items:
            for item in self.config.budget.items:
                start, end = self._period(now, item)
                in_hours = self._in_budget_hours(now, item)
                if not in_hours and str(item.off_hours or "continue") == "hold":
                    hold_seconds = self._seconds_until_next_budget_start(now, item)
                    return Decision(
                        True,
                        f"{item.scope}:{item.target or '*'} 预算时段外停止响应",
                        str(item.scope),
                        str(item.target or ""),
                        str(item.metric),
                        0.0,
                        0.0,
                        0.0,
                        "off_hours",
                        hold_seconds,
                    )
                # 时段内：按策略控速；时段外 continue：只拦总额（计划曲线已到 100%）
                rule = BudgetRule(
                    item.scope,
                    item.target,
                    item.metric,
                    item.amount,
                    start,
                    end,
                    item.input_weight,
                    item.output_weight,
                    item.strategy,
                    item.overshoot_time,
                )
                decision = check_budget(metrics["groups"], rule, now)
                if decision:
                    return decision
            return None
        if self.config.static_limits.enabled:
            for item in self.config.static_limits.items:
                rule = LimitRule(
                    item.scope,
                    item.target,
                    item.metric,
                    item.window_seconds,
                    item.limit,
                    item.input_weight,
                    item.output_weight,
                )
                decision = check_static(metrics["groups"], rule)
                if decision:
                    return decision
        return None

    async def _check(self) -> Optional[Decision]:
        now = datetime.now()
        if self.config.budget.enabled and self.config.budget.items:
            starts = [self._period(now, item)[0] for item in self.config.budget.items]
            return self._decision(await self._usage(min(starts), now), now)
        start = now - timedelta(seconds=self.config.stats.window_seconds)
        return self._decision(await self._usage(start, now), now)
    async def _maybe_reset_error_streak(self) -> None:
        """解除/到期后若出现成功调用，清零连续错误停模档位。"""
        if self.state.error_hold_streak <= 0:
            return
        if self.state.is_holding():
            return
        anchor = float(self.state.hold_ended_ts or 0)
        if anchor <= 0:
            return
        start = datetime.fromtimestamp(anchor)
        metrics = await self._usage(start, datetime.now())
        if int((metrics.get("total") or {}).get("requests") or 0) > 0:
            self.state.reset_error_hold_streak()

    async def _on_snapshot_error(
        self,
        *,
        model_name: str = "",
        provider: str = "",
        feature: str = "",
        message: str = "",
        error_type: str = "",
        error: Optional[dict] = None,
        source_path: str = "",
        **_: Any,
    ) -> None:
        if not self._active():
            return
        await self._maybe_reset_error_streak()
        if not self.config.error_rules.enabled:
            self.state.record_error(
                model=model_name,
                provider=provider,
                feature=feature,
                error_type=error_type,
                message=message,
            )
            return
        event = self.policy.on_error(
            model_name=model_name,
            provider=provider,
            feature=feature,
            message=message,
            error_type=error_type,
            error=error,
        )
        self.ctx.logger.warning(
            "hold_on 记录错误：model=%s provider=%s feature=%s type=%s path=%s hold=%s",
            model_name,
            provider,
            feature,
            error_type,
            Path(source_path).name if source_path else "",
            bool(event),
        )
        if event is not None:
            await self._notify_hold(event)

    @staticmethod
    def _pick_stream_id(payload: Any) -> str:
        if payload is None:
            return ""
        if isinstance(payload, str):
            return payload.strip()
        if not isinstance(payload, dict):
            return ""
        for key in ("stream_id", "session_id"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value
        nested = payload.get("stream")
        if isinstance(nested, dict):
            for key in ("stream_id", "session_id"):
                value = str(nested.get(key) or "").strip()
                if value:
                    return value
        return ""

    async def _resolve_notify_stream_id(self) -> str:
        cfg = self.config.notify
        target_type = str(cfg.target_type or "group").strip().lower()
        platform = str(cfg.platform or "qq").strip() or "qq"
        if target_type == "stream_id":
            return str(cfg.stream_id or "").strip()
        if target_type == "group":
            group_id = str(cfg.group_id or "").strip()
            if not group_id:
                return ""
            found = await self.ctx.chat.get_stream_by_group_id(group_id=group_id, platform=platform)
            stream_id = self._pick_stream_id(found)
            if stream_id:
                return stream_id
            opened = await self.ctx.chat.open_session(platform=platform, chat_type="group", group_id=group_id)
            return self._pick_stream_id(opened)
        if target_type == "private":
            user_id = str(cfg.user_id or "").strip()
            if not user_id:
                return ""
            found = await self.ctx.chat.get_stream_by_user_id(user_id=user_id, platform=platform)
            stream_id = self._pick_stream_id(found)
            if stream_id:
                return stream_id
            opened = await self.ctx.chat.open_session(platform=platform, chat_type="private", user_id=user_id)
            return self._pick_stream_id(opened)
        return ""

    async def _notify_hold(self, event: HoldEvent) -> None:
        if not bool(self.config.notify.enabled):
            return
        label = HoldOnPolicy.scope_label(event.scope)
        dist = HoldOnPolicy.format_distribution(dict(event.distribution or {}))
        text = (
            "停止响应：\n"
            f"- {label}:{event.name or '*'} 在 {event.window_seconds}s 内达到 "
            f"{event.count}/{event.threshold}，停止 {event.hold_seconds}s\n"
            f"-> {dist}"
        )
        await self._send_notify(text)

    async def _notify_rate_limit(self, decision: Decision) -> None:
        if not bool(self.config.notify.enabled):
            return
        label = HoldOnPolicy.scope_label(decision.scope)
        name = decision.target or "*"
        hold_seconds = int(max(1.0, float(decision.hold_seconds or 60)))
        if decision.kind == "off_hours":
            detail = f"{label}:{name} 预算时段外停止响应，停止 {hold_seconds}s"
        else:
            detail = (
                f"{label}:{name} {decision.metric} "
                f"{decision.actual:g}/{decision.limit:g}，停止 {hold_seconds}s"
            )
        text = f"停止响应：\n- {detail}\n-> {decision.reason}"
        await self._send_notify(text)

    async def _send_notify(self, text: str) -> None:
        prefix = str(self.config.notify.prefix or "[hold_on]").rstrip()
        outbound = f"{prefix}{text}"
        try:
            target = await self._resolve_notify_stream_id()
        except Exception as exc:
            self.ctx.logger.error("hold_on 解析通知目标失败: %s", exc, exc_info=True)
            return
        if not target:
            self.ctx.logger.warning("hold_on 通知未配置有效目标")
            return
        try:
            sent = await self.ctx.send.text(outbound, target)
            if sent:
                self.ctx.logger.info("hold_on 已转发停模通知 target=%s", target)
            else:
                self.ctx.logger.warning("hold_on 通知发送失败 target=%s", target)
        except Exception as exc:
            self.ctx.logger.error("hold_on 通知发送异常: %s", exc, exc_info=True)

    def _active_limit_specs(self) -> list[tuple[str, str]]:
        """返回当前生效限制目标列表：(scope, target)。target 为空表示该 scope 下各目标分别统计。"""
        specs: list[tuple[str, str]] = []
        if self.config.budget.enabled and self.config.budget.items:
            for item in self.config.budget.items:
                specs.append((str(item.scope), str(item.target or "").strip()))
            return specs
        if self.config.static_limits.enabled:
            for item in self.config.static_limits.items:
                specs.append((str(item.scope), str(item.target or "").strip()))
        return specs

    def _sum_scope_metrics(self, groups: list[Dict[str, Any]], scope: str, target: str) -> Dict[str, float]:
        total = {"requests": 0.0, "tokens": 0.0, "cost": 0.0}
        for group in groups:
            if str(group.get(scope) or "") != target:
                continue
            total["requests"] += float(group.get("requests") or 0)
            total["tokens"] += float(group.get("tokens") or 0)
            total["cost"] += float(group.get("cost") or 0)
        return total

    def _status_targets(self, metrics: Dict[str, Any]) -> list[tuple[str, str]]:
        groups = list(metrics.get("groups") or [])
        seen: set[tuple[str, str]] = set()
        ordered: list[tuple[str, str]] = []
        for scope, target in self._active_limit_specs():
            if target:
                key = (scope, target)
                if key not in seen:
                    seen.add(key)
                    ordered.append(key)
                continue
            for group in groups:
                name = str(group.get(scope) or "").strip()
                if not name:
                    continue
                key = (scope, name)
                if key not in seen:
                    seen.add(key)
                    ordered.append(key)
        return ordered

    def _find_budget_rule(self, scope: str, target: str, now: datetime) -> Optional[BudgetRule]:
        for item in self.config.budget.items:
            if str(item.scope) != scope:
                continue
            configured = str(item.target or "").strip()
            if configured and configured != target:
                continue
            start, end = self._period(now, item)
            return BudgetRule(
                item.scope,
                target,
                item.metric,
                item.amount,
                start,
                end,
                item.input_weight,
                item.output_weight,
                item.strategy,
                item.overshoot_time,
            )
        return None

    def _find_static_rule(self, scope: str, target: str) -> Optional[LimitRule]:
        for item in self.config.static_limits.items:
            if str(item.scope) != scope:
                continue
            configured = str(item.target or "").strip()
            if configured and configured != target:
                continue
            return LimitRule(
                item.scope,
                target,
                item.metric,
                item.window_seconds,
                item.limit,
                item.input_weight,
                item.output_weight,
            )
        return None

    @staticmethod
    def _format_amount(value: float, metric: str) -> str:
        if metric == "cost":
            return f"{value:.5f}"
        if metric == "requests":
            return f"{int(value)}"
        return f"{value:.0f}"

    @staticmethod
    def _format_rate(value: float, metric: str) -> str:
        unit = {"cost": "成本/秒", "tokens": "token/秒", "requests": "次/秒"}.get(metric, "/秒")
        if metric == "cost":
            return f"{value:.5f}{unit}"
        if value >= 10:
            return f"{value:.1f}{unit}"
        return f"{value:.2f}{unit}"

    def _rate_suffix(
        self,
        *,
        scope: str,
        target: str,
        groups: list[Dict[str, Any]],
        now: datetime,
    ) -> str:
        if self.config.budget.enabled and self.config.budget.items:
            rule = self._find_budget_rule(scope, target, now)
            if rule is None:
                return ""
            progress = budget_progress(groups, rule, now)
            remain_label = "成本" if rule.metric == "cost" else "token"
            return (
                f" ，剩余{remain_label} {self._format_amount(progress['remaining'], rule.metric)}"
                f" ，实际速度 {self._format_rate(progress['actual_speed'], rule.metric)}"
                f" ，计划速度 {self._format_rate(progress['recover_speed'], rule.metric)}"
            )

        rule = self._find_static_rule(scope, target)
        if rule is None:
            return ""
        progress = static_progress(groups, rule)
        remain_label = {"cost": "成本", "tokens": "token", "requests": "次数"}.get(rule.metric, rule.metric)
        return (
            f" ，剩余{remain_label} {self._format_amount(progress['remaining'], rule.metric)}"
            f" ，实际速度 {self._format_rate(progress['actual_speed'], rule.metric)}"
            f" ，限额速度 {self._format_rate(progress['plan_speed'], rule.metric)}"
        )

    def _status_text(
        self,
        *,
        start: datetime,
        end: datetime,
        metrics: Dict[str, Any],
        holding: bool,
        now: Optional[datetime] = None,
    ) -> str:
        now = now or end
        lines = [
            f"【{start:%m-%d}: {start:%H:%M} ~ {end:%H:%M}】",
            f"状态：{'停止响应' if holding else '正常响应'}",
        ]
        if holding:
            hold = self.state.hold_info()
            if hold.reason:
                lines.append(f"原因：{hold.reason}")
            rem = int(hold.remaining_seconds())
            if rem > 0:
                lines.append(f"剩余停止：{rem}s")
        rate_hits = self.state.count_rate_limits(start_ts=start.timestamp(), end_ts=end.timestamp())
        lines.append(f"限速触发：{rate_hits} 次")
        lines.append("统计：")
        feature_models = {
            str(f.feature): list(f.models or [])
            for f in (self.config.catalog.features or [])
            if str(f.feature or "").strip()
        }
        groups = list(metrics.get("groups") or [])
        targets = self._status_targets(metrics)
        if not targets:
            lines.append("- （当前没有生效的速率限制目标）")
            return "\n".join(lines)

        for scope, target in targets:
            success = self._sum_scope_metrics(groups, scope, target)
            fails = self.state.count_errors_for_scope(
                scope=scope,
                target=target,
                start_ts=start.timestamp(),
                end_ts=end.timestamp(),
                feature_models=feature_models,
            )
            hits = self.state.count_rate_limits(
                start_ts=start.timestamp(),
                end_ts=end.timestamp(),
                scope=scope,
                target=target,
            )
            suffix = self._rate_suffix(scope=scope, target=target, groups=groups, now=now)
            lines.append(
                f"- {target}: 成功 {int(success['requests'])} / 失败 {fails} ，"
                f"限速 {hits} ，token {int(success['tokens'])} ，成本 {success['cost']:.5f}"
                f"{suffix}"
            )
        return "\n".join(lines)

    async def _is_admin(self, platform: str, user_id: str) -> bool:
        uid = str(user_id or "").strip()
        scoped = f"{platform}:{uid}" if platform else uid
        return uid in {str(x).strip() for x in self.config.permission.whitelist} or scoped in self.config.permission.whitelist
    async def _send(self, stream_id: str, text: str) -> tuple[bool, str, bool]:
        if stream_id: await self.ctx.send.text(text, stream_id)
        return True, text, True
    @HookHandler("chat.receive.after_process", name="hold_on_receive_abort", description="消耗达到限制时 LATE abort", mode=HookMode.BLOCKING, order=HookOrder.LATE)
    async def handle_receive_after_process(self, message: Any = None, **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        if not self._active() or self._message_plain(message).startswith("/"):
            return {"action": "continue"}
        await self._maybe_reset_error_streak()
        decision = await self._check()
        if decision:
            newly = self.state.activate_hold(
                seconds=float(decision.hold_seconds or 60),
                reason=decision.reason,
                rule_scope=decision.scope,
                rule_name=decision.target,
                error_type=decision.metric,
            )
            if newly:
                self.state.record_rate_limit(
                    scope=decision.scope,
                    target=decision.target,
                    metric=decision.metric,
                    kind=decision.kind,
                    reason=decision.reason,
                )
                await self._notify_rate_limit(decision)
        return {"action": "abort"} if self.state.is_holding() else {"action": "continue"}

    @staticmethod
    def _message_plain(message: Any) -> str:
        return str(message.get("processed_plain_text") or "") if isinstance(message, dict) else ""

    @Command("holdon_status", description="查看当前消耗与限制", pattern=r"/稍等\s*$")
    async def cmd_status(self, **kwargs: Any):
        stream_id = kwargs.get("stream_id", "")
        if not await self._is_admin(kwargs.get("platform", ""), kwargs.get("user_id", "")):
            return await self._send(stream_id, "权限不足。")
        await self._maybe_reset_error_streak()
        now = datetime.now()
        if self.config.budget.enabled and self.config.budget.items:
            start = min(self._period(now, item)[0] for item in self.config.budget.items)
            end = max(self._period(now, item)[1] for item in self.config.budget.items)
            display_end = min(now, end)
        else:
            start = now - timedelta(seconds=self.config.stats.window_seconds)
            display_end = now
        metrics = await self._usage(start, now)
        decision = self._decision(metrics, now)
        holding = self.state.is_holding() or bool(decision)
        return await self._send(
            stream_id,
            self._status_text(
                start=start,
                end=display_end,
                metrics=metrics,
                holding=holding,
                now=now,
            ),
        )

    @Command("holdon_clear", description="解除限制", pattern=r"/解除\s*$")
    async def cmd_clear(self, **kwargs: Any):
        stream_id = kwargs.get("stream_id", "")
        if not await self._is_admin(kwargs.get("platform", ""), kwargs.get("user_id", "")):
            return await self._send(stream_id, "权限不足。")
        self.state.clear_hold()
        return await self._send(stream_id, "已解除。")


def create_plugin() -> HoldOnPlugin:
    return HoldOnPlugin()
