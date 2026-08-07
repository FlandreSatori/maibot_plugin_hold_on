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
    input_weight: float = Field(default=1.0, ge=0, description="输入 token 计权倍数")
    output_weight: float = Field(default=1.0, ge=0, description="输出 token 计权倍数")

class CatalogModel(PluginConfigBase):
    name: str = Field(default="", description="模型别名")
    provider: str = Field(default="", description="厂商名")
class CatalogFeature(PluginConfigBase):
    feature: str = Field(default="", description="功能名，对应 task_name")
    models: list[str] = Field(default_factory=list, description="功能使用的模型别名")
class CatalogConfig(PluginConfigBase):
    __ui_label__ = "自动探测目录"
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
    __ui_label__ = "静态速率限制"
    __ui_icon__ = "gauge"
    __ui_order__ = 3
    enabled: bool = Field(default=True, description="动态预算关闭时启用静态限制")
    items: list[StaticLimit] = Field(default_factory=list, description="厂商、模型或功能的限制列表")

class BudgetRuleConfig(PluginConfigBase):
    scope: Literal["provider", "model", "feature"] = Field(default="model", description="预算范围")
    target: str = Field(default="", description="预算目标名")
    metric: Literal["tokens", "cost"] = Field(default="cost", description="预算指标")
    amount: float = Field(default=100, gt=0, description="周期总额度")
    start_time: str = Field(default="08:00", description="每日周期开始，HH:MM")
    end_time: str = Field(default="22:00", description="每日周期结束，HH:MM")
    strategy: Literal["strict", "balanced", "lenient"] = Field(default="strict", description="超速策略")
    overshoot_ratio: float = Field(default=0.1, ge=0, le=10, description="允许超出计划曲线的比例")
    input_weight: float = Field(default=1.0, ge=0, description="输入 token 倍率")
    output_weight: float = Field(default=1.0, ge=0, description="输出 token 倍率")
class BudgetConfig(PluginConfigBase):
    __ui_label__ = "动态预算"
    __ui_icon__ = "wallet"
    __ui_order__ = 4
    enabled: bool = Field(default=False, description="启用后只使用动态预算，不叠加静态限制")
    items: list[BudgetRuleConfig] = Field(default_factory=list, description="每日成本或 token 预算")

class ErrorWatchConfig(PluginConfigBase):
    __ui_label__ = "错误统计"
    __ui_icon__ = "alert-triangle"
    __ui_order__ = 5
    enabled: bool = Field(default=True, description="监听 schema v3 错误快照")
    interval_seconds: float = Field(default=2.0, ge=0.5, description="扫描间隔秒数")
    roots: list[str] = Field(default_factory=list, description="额外错误目录")

class NotifyConfig(PluginConfigBase):
    __ui_label__ = "通知"
    __ui_icon__ = "corner-up-right"
    __ui_order__ = 6
    enabled: bool = Field(default=False, description="停止入站时转发通知")
    target_type: Literal["group", "private", "stream_id"] = Field(default="group", description="通知目标类型")
    group_id: str = Field(default="", description="群号")
    user_id: str = Field(default="", description="用户 ID")
    stream_id: str = Field(default="", description="stream_id")
    platform: str = Field(default="qq", description="平台")
    prefix: str = Field(default="[hold_on] ", description="通知前缀")
class PermissionConfig(PluginConfigBase):
    __ui_label__ = "权限"
    __ui_icon__ = "shield"
    __ui_order__ = 7
    whitelist: list[str] = Field(default_factory=list, description="管理命令白名单")
    notify_permission_denied: bool = Field(default=True, description="无权限时是否提示")
class HoldOnConfig(PluginConfigBase):
    plugin: PluginConfig = Field(default_factory=PluginConfig)
    stats: StatsConfig = Field(default_factory=StatsConfig)
    catalog: CatalogConfig = Field(default_factory=CatalogConfig)
    static_limits: StaticLimitsConfig = Field(default_factory=StaticLimitsConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    error_watch: ErrorWatchConfig = Field(default_factory=ErrorWatchConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    permission: PermissionConfig = Field(default_factory=PermissionConfig)

class HoldOnPlugin(MaiBotPlugin):
    config_model = HoldOnConfig
    async def on_load(self) -> None:
        self.state = HoldOnState(Path(self.ctx.paths.data_dir) / "hold_on_state.json")
        self._catalog = await self._discover_catalog()
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
    async def _discover_catalog(self) -> Dict[str, Any]:
        extra = [self.config.plugin.model_config_path] if self.config.plugin.model_config_path.strip() else []
        result = discover_models(extra)
        if result is None:
            return {"models": [(m.name, m.provider) for m in self.config.catalog.models], "features": {f.feature: tuple(f.models) for f in self.config.catalog.features}}
        return {"models": [(m.name, m.provider) for m in result.models], "features": {f.feature: tuple(f.models) for f in result.features}}
    def _active(self) -> bool:
        return bool(self.config.plugin.enabled)
    async def _usage(self, start: datetime, end: datetime) -> Dict[str, Any]:
        return await aggregate_usage(
            self.ctx,
            start,
            end,
            self.config.stats.usage_limit,
            input_weight=float(self.config.stats.input_weight),
            output_weight=float(self.config.stats.output_weight),
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

    def _decision(self, metrics: Dict[str, Any], now: datetime) -> Optional[Decision]:
        if self.config.budget.enabled and self.config.budget.items:
            for item in self.config.budget.items:
                start, end = self._period(now, item)
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
                    item.overshoot_ratio,
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
    async def _on_snapshot_error(self, *, model_name: str = "", provider: str = "", feature: str = "", message: str = "", error_type: str = "", error: Optional[dict] = None, source_path: str = "", **_: Any) -> None:
        if not self._active():
            return
        self.state.record_error(
            model=model_name,
            provider=provider,
            feature=feature,
            error_type=error_type,
            message=message,
        )

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
                item.overshoot_ratio,
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
            "统计：",
        ]
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
            suffix = self._rate_suffix(scope=scope, target=target, groups=groups, now=now)
            lines.append(
                f"- {target}: 成功 {int(success['requests'])} / 失败 {fails} ，"
                f"token {int(success['tokens'])} ，成本 {success['cost']:.5f}"
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
        if not self._active() or self._message_plain(message).startswith("/"): return {"action": "continue"}
        decision = await self._check()
        if decision:
            self.state.activate_hold(seconds=60, reason=decision.reason, rule_scope=decision.scope, rule_name=decision.target, error_type=decision.metric)
        return {"action": "abort"} if self.state.is_holding() else {"action": "continue"}
    @staticmethod
    def _message_plain(message: Any) -> str:
        return str(message.get("processed_plain_text") or "") if isinstance(message, dict) else ""
    @Command("holdon_status", description="查看当前消耗与限制", pattern=r"/稍等\s*$")
    async def cmd_status(self, **kwargs: Any):
        stream_id = kwargs.get("stream_id", "")
        if not await self._is_admin(kwargs.get("platform", ""), kwargs.get("user_id", "")):
            return await self._send(stream_id, "权限不足。")
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
        if not await self._is_admin(kwargs.get("platform", ""), kwargs.get("user_id", "")): return await self._send(stream_id, "权限不足。")
        self.state.clear_hold(); return await self._send(stream_id, "已解除。")

def create_plugin() -> HoldOnPlugin: return HoldOnPlugin()
