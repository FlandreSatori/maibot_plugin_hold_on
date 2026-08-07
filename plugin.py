"""稍，稍等一下！: LLM 消耗监控与入站限速。"""
from __future__ import annotations
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, Literal, Optional
from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import HookMode, HookOrder
from .modules.controller import BudgetRule, Decision, LimitRule, check_budget, check_static
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
        return await aggregate_usage(self.ctx, start, end, self.config.stats.usage_limit)
    def _period(self, now: datetime, item: BudgetRuleConfig) -> tuple[datetime, datetime]:
        def parse(value: str) -> time:
            hour, minute = (int(x) for x in value.split(":", 1))
            return time(hour, minute)
        start_t, end_t = parse(item.start_time), parse(item.end_time)
        start = datetime.combine(now.date(), start_t)
        end = datetime.combine(now.date(), end_t)
        if end <= start:
            end += timedelta(days=1)
        if now < start:
            start -= timedelta(days=1)
            end -= timedelta(days=1)
        return start, end
    def _decision(self, metrics: Dict[str, Any], now: datetime) -> Optional[Decision]:
        if self.config.budget.enabled and self.config.budget.items:
            start, end = self._period(now)
            for item in self.config.budget.items:
                rule = BudgetRule(item.scope, item.target, item.metric, item.amount, start, end, item.input_weight, item.output_weight, item.strategy, item.overshoot_ratio)
                decision = check_budget(metrics["groups"], rule, now)
                if decision: return decision
            return None
        if self.config.static_limits.enabled:
            for item in self.config.static_limits.items:
                rule = LimitRule(item.scope, item.target, item.metric, item.window_seconds, item.limit, item.input_weight, item.output_weight)
                decision = check_static(metrics["groups"], rule)
                if decision: return decision
        return None
    async def _check(self) -> Optional[Decision]:
        now = datetime.now()
        start = now - timedelta(seconds=self.config.stats.window_seconds)
        return self._decision(await self._usage(start, now), now)
    async def _on_snapshot_error(self, *, model_name: str = "", provider: str = "", message: str = "", error_type: str = "", error: Optional[dict] = None, source_path: str = "", **_: Any) -> None:
        if not self._active(): return
        self.state.record_error(model=model_name, provider=provider, error_type=error_type, message=message)
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
        if not await self._is_admin(kwargs.get("platform", ""), kwargs.get("user_id", "")): return await self._send(stream_id, "权限不足。")
        now = datetime.now(); metrics = await self._usage(now - timedelta(seconds=self.config.stats.window_seconds), now); decision = self._decision(metrics, now)
        return await self._send(stream_id, f"【稍等状态】\n成功调用: {int(metrics['total']['requests'])}\nToken: {int(metrics['total']['tokens'])}\n成本: {metrics['total']['cost']:.4f}\n状态: {'限制中' if self.state.is_holding() or decision else '正常'}")
    @Command("holdon_clear", description="解除限制", pattern=r"/解除\s*$")
    async def cmd_clear(self, **kwargs: Any):
        stream_id = kwargs.get("stream_id", "")
        if not await self._is_admin(kwargs.get("platform", ""), kwargs.get("user_id", "")): return await self._send(stream_id, "权限不足。")
        self.state.clear_hold(); return await self._send(stream_id, "已解除。")

def create_plugin() -> HoldOnPlugin: return HoldOnPlugin()
