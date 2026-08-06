"""
稍，稍等一下！（hold_on）

统计模型出错率；按模型/厂商/功能 + 错误类型阈值在 LATE 阶段 abort。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import HookMode, HookOrder

from .modules.error_watch import ErrorSnapshotWatcher, resolve_watch_roots
from .modules.model_discover import (
    discover_models,
    discovery_to_config_sections,
    write_simple_toml,
)
from .modules.policy import FeatureModels, HoldEvent, HoldOnPolicy, ThresholdRule
from .modules.state import HoldOnState
from .modules.usage_sync import fetch_req_cnt_by_model

PLUGIN_DIR = Path(__file__).resolve().parent


# ─── 配置 ───────────────────────────────────────────────────────


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="2.0.2", description="配置版本")
    auto_detect_models: bool = Field(
        default=True,
        description="加载时从宿主 model_config.toml 同步",
    )
    model_config_path: str = Field(
        default="",
        description="可选：宿主 model_config.toml 绝对路径；空则自动搜索",
    )


class StatsConfig(PluginConfigBase):
    __ui_label__ = "统计"
    __ui_icon__ = "bar-chart-2"
    __ui_order__ = 1

    window_seconds: int = Field(
        default=600,
        description="/稍等 展示的统计窗口秒数",
    )
    usage_fetch_limit: int = Field(
        default=10000,
        description="从宿主 llm_usage 拉取成功记录的最大条数（按 id 倒序）",
    )


class CatalogModelConfig(PluginConfigBase):
    __ui_label__ = "模型"
    __ui_icon__ = "cpu"
    __ui_order__ = 0

    name: str = Field(default="", description="逻辑模型名")
    provider: str = Field(default="", description="厂商名")


class CatalogFeatureConfig(PluginConfigBase):
    __ui_label__ = "功能"
    __ui_icon__ = "layers"
    __ui_order__ = 0

    feature: str = Field(default="", description="功能名，如 replyer / planner")
    models: list[str] = Field(default_factory=list, description="该功能使用的逻辑模型列表")


class CatalogConfig(PluginConfigBase):
    __ui_label__ = "模型目录"
    __ui_icon__ = "book"
    __ui_order__ = 2

    models: list[CatalogModelConfig] = Field(
        default_factory=list,
        description="模型↔厂商；auto_detect_models 时自动同步",
    )
    features: list[CatalogFeatureConfig] = Field(
        default_factory=list,
        description="功能→模型；用于 scope=feature 的规则",
    )


class ThresholdRuleConfig(PluginConfigBase):
    __ui_label__ = "阈值规则"
    __ui_icon__ = "filter"
    __ui_order__ = 0

    scope: str = Field(
        default="model",
        description="计数范围：model / provider / feature",
    )
    name: str = Field(
        default="",
        description="目标名；空表示该 scope 下按实际命中目标各自计数",
    )
    error_type: str = Field(
        default="*",
        description="错误类型：429 / 502 / 5xx / timeout / * 等",
    )
    window_seconds: int = Field(default=60, description="滑动窗口秒数")
    threshold: int = Field(default=5, description="窗口内达到该次数则停模")
    hold_seconds: int = Field(default=90, description="触发后停模秒数")


class RulesConfig(PluginConfigBase):
    __ui_label__ = "全局停止"
    __ui_icon__ = "shield-off"
    __ui_order__ = 3

    items: list[ThresholdRuleConfig] = Field(
        default_factory=list,
        description="错误阈值规则列表",
    )


class ErrorWatchConfig(PluginConfigBase):
    __ui_label__ = "错误快照监听"
    __ui_icon__ = "eye"
    __ui_order__ = 4

    enabled: bool = Field(default=True, description="监听宿主失败快照 JSON")
    interval_seconds: float = Field(default=2.0, description="扫描间隔秒")
    roots: list[str] = Field(
        default_factory=list,
        description="额外监听目录；默认 logs/maisaka_prompt/llm_error",
    )


class NotifyConfig(PluginConfigBase):
    __ui_label__ = "通知转发"
    __ui_icon__ = "corner-up-right"
    __ui_order__ = 5

    enabled: bool = Field(default=False, description="触发全局停止时是否转发到指定会话")
    target_type: str = Field(
        default="group",
        description="目标类型：group / private / stream_id",
    )
    group_id: str = Field(default="", description="目标群号（target_type=group）")
    user_id: str = Field(default="", description="目标私聊用户 ID（target_type=private）")
    stream_id: str = Field(default="", description="目标 stream_id（target_type=stream_id）")
    platform: str = Field(default="qq", description="解析群/私聊时的平台")
    prefix: str = Field(default="[hold_on] ", description="通知前缀")


class PermissionConfig(PluginConfigBase):
    __ui_label__ = "权限"
    __ui_icon__ = "shield"
    __ui_order__ = 6

    whitelist: list[str] = Field(default_factory=list, description="管理命令白名单 user_id 或 platform:user_id")
    notify_permission_denied: bool = Field(default=True, description="无权限时是否提示")


class HoldOnConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    stats: StatsConfig = Field(default_factory=StatsConfig)
    catalog: CatalogConfig = Field(default_factory=CatalogConfig)
    rules: RulesConfig = Field(default_factory=RulesConfig)
    error_watch: ErrorWatchConfig = Field(default_factory=ErrorWatchConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    permission: PermissionConfig = Field(default_factory=PermissionConfig)


# ─── 插件 ───────────────────────────────────────────────────────


class HoldOnPlugin(MaiBotPlugin):
    """稍，稍等一下！"""

    config_model = HoldOnConfig

    async def on_load(self) -> None:
        self.state = HoldOnState(Path(self.ctx.paths.data_dir) / "hold_on_state.json")
        await self._maybe_auto_fill_catalog()
        self.policy = self._build_policy()
        self._watcher: Optional[ErrorSnapshotWatcher] = None

        if bool(self.config.error_watch.enabled) and bool(self.config.plugin.enabled):
            roots = resolve_watch_roots(self.config.error_watch.roots)
            self._watcher = ErrorSnapshotWatcher(
                roots=roots,
                interval_seconds=float(self.config.error_watch.interval_seconds or 2.0),
                on_error=self._on_snapshot_error,
                logger=self.ctx.logger,
            )
            self._watcher.start()

        self.ctx.logger.info(
            "hold_on 已加载：enabled=%s rules=%s models=%s features=%s",
            bool(self.config.plugin.enabled),
            len(self.config.rules.items or []),
            len(self.config.catalog.models or []),
            [f.feature for f in (self.config.catalog.features or []) if f.feature],
        )

    async def on_unload(self) -> None:
        if self._watcher is not None:
            await self._watcher.stop()
            self._watcher = None

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        del scope, config_data, version
        self.policy = self._build_policy()
        self.ctx.logger.info("hold_on 配置已更新")

    async def _maybe_auto_fill_catalog(self) -> None:
        if not bool(self.config.plugin.auto_detect_models):
            return

        extra = []
        if str(self.config.plugin.model_config_path or "").strip():
            extra.append(str(self.config.plugin.model_config_path).strip())
        result = discover_models(extra)
        if result is None:
            self.ctx.logger.warning("hold_on 未找到 model_config.toml，跳过自动检测")
            return

        sections = discovery_to_config_sections(result)
        self.config.catalog.models = [
            CatalogModelConfig(
                name=str(item.get("name") or "").strip(),
                provider=str(item.get("provider") or "").strip(),
            )
            for item in sections["catalog"]["models"]
            if str(item.get("name") or "").strip()
        ]
        self.config.catalog.features = [
            CatalogFeatureConfig(
                feature=str(item.get("feature") or "").strip(),
                models=list(item.get("models") or []),
            )
            for item in sections["catalog"]["features"]
            if str(item.get("feature") or "").strip()
        ]

        try:
            self._persist_config()
        except Exception as exc:
            self.ctx.logger.warning("hold_on 自动检测配置写盘失败: %s", exc)

        self.ctx.logger.info(
            "hold_on 已同步 catalog：source=%s models=%s features=%s",
            result.source_path,
            len(self.config.catalog.models or []),
            [f.feature for f in (self.config.catalog.features or [])],
        )

    def _config_payload(self) -> Dict[str, Any]:
        return {
            "plugin": {
                "enabled": bool(self.config.plugin.enabled),
                "config_version": "2.0.2",
                "auto_detect_models": bool(self.config.plugin.auto_detect_models),
                "model_config_path": str(self.config.plugin.model_config_path or ""),
            },
            "stats": {
                "window_seconds": int(self.config.stats.window_seconds or 600),
                "usage_fetch_limit": int(self.config.stats.usage_fetch_limit or 10000),
            },
            "catalog": {
                "models": [
                    {"name": m.name, "provider": m.provider}
                    for m in (self.config.catalog.models or [])
                    if m.name
                ],
                "features": [
                    {"feature": f.feature, "models": list(f.models or [])}
                    for f in (self.config.catalog.features or [])
                    if f.feature
                ],
            },
            "rules": {
                "items": [
                    {
                        "scope": str(r.scope or "model"),
                        "name": str(r.name or ""),
                        "error_type": str(r.error_type or "*"),
                        "window_seconds": int(r.window_seconds or 60),
                        "threshold": int(r.threshold or 0),
                        "hold_seconds": int(r.hold_seconds or 90),
                    }
                    for r in (self.config.rules.items or [])
                ],
            },
            "error_watch": {
                "enabled": bool(self.config.error_watch.enabled),
                "interval_seconds": float(self.config.error_watch.interval_seconds or 2.0),
                "roots": list(self.config.error_watch.roots or []),
            },
            "notify": {
                "enabled": bool(self.config.notify.enabled),
                "target_type": str(self.config.notify.target_type or "group"),
                "group_id": str(self.config.notify.group_id or ""),
                "user_id": str(self.config.notify.user_id or ""),
                "stream_id": str(self.config.notify.stream_id or ""),
                "platform": str(self.config.notify.platform or "qq"),
                "prefix": str(self.config.notify.prefix or "[hold_on] "),
            },
            "permission": {
                "whitelist": list(self.config.permission.whitelist or []),
                "notify_permission_denied": bool(self.config.permission.notify_permission_denied),
            },
        }

    def _persist_config(self) -> None:
        config_path = PLUGIN_DIR / "config.toml"
        write_simple_toml(config_path, self._config_payload())

    def _build_policy(self) -> HoldOnPolicy:
        rules = [
            ThresholdRule(
                scope=str(r.scope or "model").strip().lower() or "model",
                name=str(r.name or "").strip(),
                error_type=str(r.error_type or "*").strip() or "*",
                window_seconds=int(r.window_seconds or 60),
                threshold=int(r.threshold or 0),
                hold_seconds=int(r.hold_seconds or 90),
            )
            for r in (self.config.rules.items or [])
            if int(r.threshold or 0) > 0
        ]
        features = [
            FeatureModels(
                feature=str(f.feature or "").strip(),
                models=tuple(str(x).strip() for x in (f.models or []) if str(x).strip()),
            )
            for f in (self.config.catalog.features or [])
            if str(f.feature or "").strip()
        ]
        model_providers = {
            str(m.name).strip(): str(m.provider or "").strip()
            for m in (self.config.catalog.models or [])
            if str(m.name or "").strip()
        }
        return HoldOnPolicy(
            self.state,
            rules=rules,
            features=features,
            model_providers=model_providers,
            stats_window_seconds=int(self.config.stats.window_seconds or 600),
        )

    def _active(self) -> bool:
        return bool(self.config.plugin.enabled)

    async def _host_success_counts(self) -> Dict[str, Dict[str, Any]]:
        """宿主 llm_usage → REQ_CNT_BY_MODEL（与 maibot_statistic 同源）。"""

        return await fetch_req_cnt_by_model(
            self.ctx.database,
            window_seconds=float(self.config.stats.window_seconds or 600),
            limit=int(self.config.stats.usage_fetch_limit or 10000),
            logger=self.ctx.logger,
        )

    async def _on_snapshot_error(
        self,
        *,
        model_name: str = "",
        provider: str = "",
        message: str = "",
        error_type: str = "",
        error: Optional[dict] = None,
        source_path: str = "",
        **_: Any,
    ) -> None:
        if not self._active():
            return
        event = self.policy.on_error(
            model_name=model_name,
            provider=provider,
            message=message,
            error_type=error_type,
            error=error,
        )
        self.ctx.logger.warning(
            "hold_on 记录错误：model=%s provider=%s type=%s path=%s hold=%s",
            model_name,
            provider,
            error_type,
            Path(source_path).name if source_path else "",
            bool(event),
        )
        if event is not None:
            await self._notify_hold(event)

    # ─── 通知转发 ────────────────────────────────────────────────

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
            stream_id = str(cfg.stream_id or "").strip()
            if not stream_id:
                self.ctx.logger.warning("hold_on 通知 target_type=stream_id 但 stream_id 为空")
            return stream_id

        if target_type == "group":
            group_id = str(cfg.group_id or "").strip()
            if not group_id:
                self.ctx.logger.warning("hold_on 通知 target_type=group 但 group_id 为空")
                return ""
            found = await self.ctx.chat.get_stream_by_group_id(group_id=group_id, platform=platform)
            stream_id = self._pick_stream_id(found)
            if stream_id:
                return stream_id
            opened = await self.ctx.chat.open_session(
                platform=platform,
                chat_type="group",
                group_id=group_id,
            )
            return self._pick_stream_id(opened)

        if target_type == "private":
            user_id = str(cfg.user_id or "").strip()
            if not user_id:
                self.ctx.logger.warning("hold_on 通知 target_type=private 但 user_id 为空")
                return ""
            found = await self.ctx.chat.get_stream_by_user_id(user_id=user_id, platform=platform)
            stream_id = self._pick_stream_id(found)
            if stream_id:
                return stream_id
            opened = await self.ctx.chat.open_session(
                platform=platform,
                chat_type="private",
                user_id=user_id,
            )
            return self._pick_stream_id(opened)

        self.ctx.logger.warning("hold_on 未知 notify.target_type=%r", target_type)
        return ""

    async def _notify_hold(self, event: HoldEvent) -> None:
        if not bool(self.config.notify.enabled):
            self.ctx.logger.debug("hold_on 新停模但 notify.enabled=false，已跳过转发")
            return
        text = (
            f"触发稍等停模：\n"
            f"- [{event.scope}:{event.name or '*'}] "
            f"类型 {event.error_type or '*'} "
            f"{event.count}/{event.threshold} @ {event.window_seconds}s\n"
            f"- {event.reason}"
        )
        prefix = str(self.config.notify.prefix or "")
        outbound = f"{prefix}{text}" if prefix else text
        try:
            target = await self._resolve_notify_stream_id()
        except Exception as exc:
            self.ctx.logger.error("hold_on 解析通知目标失败: %s", exc, exc_info=True)
            return
        if not target:
            cfg = self.config.notify
            self.ctx.logger.warning(
                "hold_on 通知未配置有效目标 target_type=%s group_id=%r",
                cfg.target_type,
                cfg.group_id,
            )
            return
        try:
            sent = await self.ctx.send.text(outbound, target)
            if not sent:
                self.ctx.logger.warning("hold_on 通知发送失败 target=%s", target)
            else:
                self.ctx.logger.info("hold_on 已转发停模通知 target=%s", target)
        except Exception as exc:
            self.ctx.logger.error("hold_on 通知发送异常: %s", exc, exc_info=True)

    # ─── 权限 / 命令辅助 ─────────────────────────────────────────

    async def _is_admin(self, platform: str, user_id: str) -> bool:
        uid = str(user_id or "").strip()
        if not uid:
            return False
        scoped = f"{platform}:{uid}" if platform else uid
        whitelist = {str(x).strip() for x in (self.config.permission.whitelist or []) if str(x).strip()}
        if uid in whitelist or scoped in whitelist:
            return True
        try:
            perm = await self.ctx.config.get("plugin.permission")
        except Exception:
            perm = None
        if isinstance(perm, list):
            masters = {str(x).strip().lower() for x in perm if str(x).strip()}
            if scoped.lower() in masters:
                return True
        return False

    async def _send(self, stream_id: str, text: str) -> tuple[bool, str, bool]:
        if stream_id and text:
            await self.ctx.send.text(text, stream_id)
        return True, text, True

    async def _deny(self, stream_id: str) -> tuple[bool, str, bool]:
        if self.config.permission.notify_permission_denied:
            return await self._send(stream_id, "权限不足。")
        return True, "", True

    @staticmethod
    def _message_plain(message: Any) -> str:
        if isinstance(message, dict):
            return str(message.get("processed_plain_text") or "").strip()
        return ""

    @staticmethod
    def _looks_like_command(text: str) -> bool:
        return str(text or "").strip().startswith("/")

    # ─── Hooks ───────────────────────────────────────────────────

    @HookHandler(
        "chat.receive.after_process",
        name="hold_on_receive_abort",
        description="错误阈值停模时 LATE abort 入站",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
    )
    async def handle_receive_after_process(self, message: Any = None, **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        if not self._active() or not self.policy.is_holding():
            return {"action": "continue"}

        text = self._message_plain(message)
        if self._looks_like_command(text):
            return {"action": "continue"}

        hold = self.state.hold_info()
        self.ctx.logger.info(
            "hold_on 停模 abort：剩余=%ss reason=%.80s text=%.40r",
            int(hold.remaining_seconds()),
            hold.reason,
            text,
        )
        return {"action": "abort"}

    # ─── 命令 ────────────────────────────────────────────────────

    @Command(
        "holdon_status",
        description="稍等状态与出错率",
        pattern=r"/稍等\s*$",
    )
    async def cmd_status(self, **kwargs: Any):
        stream_id = kwargs.get("stream_id", "")
        if not await self._is_admin(kwargs.get("platform", ""), kwargs.get("user_id", "")):
            return await self._deny(stream_id)
        host_success = await self._host_success_counts()
        return await self._send(stream_id, self.policy.status_text(host_success=host_success))

    @Command(
        "holdon_clear",
        description="解除稍等",
        pattern=r"/解除\s*$",
    )
    async def cmd_clear(self, **kwargs: Any):
        stream_id = kwargs.get("stream_id", "")
        if not await self._is_admin(kwargs.get("platform", ""), kwargs.get("user_id", "")):
            return await self._deny(stream_id)
        cleared_hold = self.state.clear_hold()
        if cleared_hold:
            return await self._send(stream_id, "已解除。")
        return await self._send(stream_id, "当前未停止响应。")


def create_plugin() -> HoldOnPlugin:
    return HoldOnPlugin()
