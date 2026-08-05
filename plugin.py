"""
稍，稍等一下！（hold_on）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import HookMode, HookOrder

from .modules.error_watch import ErrorSnapshotWatcher, resolve_watch_roots
from .modules.model_discover import (
    discover_models,
    discovery_to_config_sections,
    write_simple_toml,
)
from .modules.policy import FeatureModels, HoldOnPolicy, NamedLimit, PolicyEvent
from .modules.state import HoldOnState

PLUGIN_DIR = Path(__file__).resolve().parent


# ─── 配置 ───────────────────────────────────────────────────────


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.3.2", description="配置版本")
    auto_detect_models: bool = Field(
        default=True,
        description="加载时自动检测启用模型，保留同名模型的配置",
    )
    model_config_path: str = Field(
        default="",
        description="可选：宿主 model_config.toml 绝对路径；空则自动搜索",
    )


class GlobalConfig(PluginConfigBase):
    __ui_label__ = "全局限流"
    __ui_icon__ = "gauge"
    __ui_order__ = 1

    enabled: bool = Field(default=True, description="是否启用限流与禁用逻辑（总开关）")
    # 全局 RPM 无实际意义，默认关闭；
    max_requests_per_minute: int = Field(
        default=0,
        description="【已弃用】全局每分钟请求上限；请保持 0，改用厂商/模型 RPM",
    )
    disable_seconds: int = Field(
        default=90,
        description="全局禁用秒数",
    )


class ProviderLimitConfig(PluginConfigBase):
    __ui_label__ = "厂商限流"
    __ui_icon__ = "server"
    __ui_order__ = 0

    name: str = Field(default="", description="厂商名")
    max_requests_per_minute: int = Field(default=30, description="每分钟上限（0=不限）")
    disable_seconds: int = Field(default=90, description="超限禁用秒数")


class ModelLimitConfig(PluginConfigBase):
    __ui_label__ = "模型限流"
    __ui_icon__ = "cpu"
    __ui_order__ = 0

    name: str = Field(default="", description="模型名")
    provider: str = Field(default="", description="厂商名")
    max_requests_per_minute: int = Field(default=10, description="每分钟上限（0=不限）")
    disable_seconds: int = Field(default=90, description="超限禁用秒数")


class LimitsConfig(PluginConfigBase):
    __ui_label__ = "厂商与模型限流"
    __ui_icon__ = "sliders"
    __ui_order__ = 2

    providers: list[ProviderLimitConfig] = Field(default_factory=list, description="厂商限流列表")
    models: list[ModelLimitConfig] = Field(default_factory=list, description="模型限流列表")


class ErrorDisableConfig(PluginConfigBase):
    __ui_label__ = "错误自动禁用"
    __ui_icon__ = "alert-triangle"
    __ui_order__ = 3

    enabled: bool = Field(default=True, description="监听到模型失败快照即禁用该模型（不区分错误类型）")
    base_seconds: int = Field(default=60, description="首次错误禁用秒数")
    max_seconds: int = Field(default=3600, description="指数退避封顶秒数")
    exponential: bool = Field(default=True, description="是否指数退避")
    multiplier: float = Field(default=2.0, description="指数倍率")


class FeatureModelsConfig(PluginConfigBase):
    __ui_label__ = "功能模型组"
    __ui_icon__ = "layers"
    __ui_order__ = 0

    feature: str = Field(default="", description="功能名，如 replyer / planner")
    models: list[str] = Field(default_factory=list, description="该功能使用的逻辑模型列表")


class FeatureKillConfig(PluginConfigBase):
    __ui_label__ = "功能停用触发全局停用"
    __ui_icon__ = "shield-off"
    __ui_order__ = 4

    enabled: bool = Field(default=True, description="某项功能下所有模型错误时，全局禁用LLM")
    features: list[FeatureModelsConfig] = Field(
        default_factory=list,
        description="功能→模型列表；开启 auto_detect_models 时每次加载自动同步",
    )


class ErrorWatchConfig(PluginConfigBase):
    __ui_label__ = "错误快照监听"
    __ui_icon__ = "eye"
    __ui_order__ = 5

    enabled: bool = Field(default=True, description="监听宿主失败快照 JSON")
    interval_seconds: float = Field(default=2.0, description="扫描间隔秒")
    roots: list[str] = Field(
        default_factory=list,
        description="额外监听目录；默认同 cwd 下 logs/llm_request 与 logs/maisaka_prompt/llm_error",
    )


class NotifyConfig(PluginConfigBase):
    """RPM / 全局禁用触发时转发通知（参考 redirect_err）。"""

    __ui_label__ = "通知转发"
    __ui_icon__ = "corner-up-right"
    __ui_order__ = 6

    enabled: bool = Field(default=False, description="触发 RPM 或新进入全局停模时是否转发到指定会话")
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
    __ui_order__ = 7

    whitelist: list[str] = Field(default_factory=list, description="管理命令白名单 user_id 或 platform:user_id")
    notify_permission_denied: bool = Field(default=True, description="无权限时是否提示")


class HoldOnConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    global_limit: GlobalConfig = Field(default_factory=GlobalConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    error_disable: ErrorDisableConfig = Field(default_factory=ErrorDisableConfig)
    feature_kill: FeatureKillConfig = Field(default_factory=FeatureKillConfig)
    error_watch: ErrorWatchConfig = Field(default_factory=ErrorWatchConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    permission: PermissionConfig = Field(default_factory=PermissionConfig)


# ─── 插件 ───────────────────────────────────────────────────────


class HoldOnPlugin(MaiBotPlugin):
    """稍，稍等一下！"""

    config_model = HoldOnConfig

    async def on_load(self) -> None:
        self.state = HoldOnState(Path(self.ctx.paths.data_dir) / "hold_on_state.json")
        await self._maybe_auto_fill_models()
        self.policy = self._build_policy()
        self._watcher: Optional[ErrorSnapshotWatcher] = None
        self._last_selected_model: Dict[str, str] = {}

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
            "hold_on 已加载：enabled=%s provider_limits=%s model_limits=%s features=%s",
            bool(self.config.plugin.enabled),
            len(self.config.limits.providers or []),
            len(self.config.limits.models or []),
            [f.feature for f in (self.config.feature_kill.features or []) if f.feature],
        )

    async def on_unload(self) -> None:
        if self._watcher is not None:
            await self._watcher.stop()
            self._watcher = None

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        del scope, config_data, version
        self.policy = self._build_policy()
        self.ctx.logger.info("hold_on 配置已更新")

    async def _maybe_auto_fill_models(self) -> None:
        """加载时按宿主 model_config 同步启用模型列表。"""

        if not bool(self.config.plugin.auto_detect_models):
            return

        extra = []
        if str(self.config.plugin.model_config_path or "").strip():
            extra.append(str(self.config.plugin.model_config_path).strip())
        result = discover_models(extra)
        if result is None:
            self.ctx.logger.warning("hold_on 未找到 model_config.toml，跳过自动检测")
            return

        sections = discovery_to_config_sections(
            result,
            default_model_rpm=10,
            default_model_disable=90,
            default_provider_rpm=30,
            default_provider_disable=90,
        )
        prev_models = {
            str(m.name).strip(): m for m in (self.config.limits.models or []) if str(m.name or "").strip()
        }
        prev_providers = {
            str(p.name).strip(): p for p in (self.config.limits.providers or []) if str(p.name or "").strip()
        }

        self.config.limits.models = []
        for item in sections["limits"]["models"]:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            old = prev_models.get(name)
            self.config.limits.models.append(
                ModelLimitConfig(
                    name=name,
                    provider=str(item.get("provider") or (old.provider if old else "") or ""),
                    max_requests_per_minute=int(
                        old.max_requests_per_minute
                        if old is not None
                        else item.get("max_requests_per_minute") or 10
                    ),
                    disable_seconds=int(
                        old.disable_seconds if old is not None else item.get("disable_seconds") or 90
                    ),
                )
            )

        self.config.limits.providers = []
        for item in sections["limits"]["providers"]:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            old = prev_providers.get(name)
            self.config.limits.providers.append(
                ProviderLimitConfig(
                    name=name,
                    max_requests_per_minute=int(
                        old.max_requests_per_minute
                        if old is not None
                        else item.get("max_requests_per_minute") or 30
                    ),
                    disable_seconds=int(
                        old.disable_seconds if old is not None else item.get("disable_seconds") or 90
                    ),
                )
            )

        self.config.feature_kill.features = [
            FeatureModelsConfig(
                feature=str(item.get("feature") or ""),
                models=list(item.get("models") or []),
            )
            for item in sections["feature_kill"]["features"]
            if str(item.get("feature") or "").strip()
        ]

        try:
            self._persist_auto_filled_config()
        except Exception as exc:
            self.ctx.logger.warning("hold_on 自动检测配置写盘失败: %s", exc)

        self.ctx.logger.info(
            "hold_on 已同步启用模型：source=%s models=%s features=%s",
            result.source_path,
            len(self.config.limits.models or []),
            [f.feature for f in (self.config.feature_kill.features or [])],
        )

    def _persist_auto_filled_config(self) -> None:
        """把当前内存中的同步结果写回插件目录 config.toml。"""

        config_path = PLUGIN_DIR / "config.toml"
        existing: Dict[str, Any] = {}
        if config_path.exists():
            try:
                import tomllib

                existing = tomllib.loads(config_path.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
        if not isinstance(existing, dict):
            existing = {}

        # 去掉已废弃字段，避免写回干扰
        plugin_existing = existing.get("plugin") if isinstance(existing.get("plugin"), dict) else {}
        plugin_existing.pop("models_auto_filled", None)

        payload = {
            "plugin": {
                "enabled": bool(self.config.plugin.enabled),
                "config_version": "1.3.2",
                "auto_detect_models": bool(self.config.plugin.auto_detect_models),
                "model_config_path": str(self.config.plugin.model_config_path or ""),
            },
            "global_limit": {
                "enabled": bool(self.config.global_limit.enabled),
                # 全局 RPM 已停用，写盘固定为 0
                "max_requests_per_minute": 0,
                "disable_seconds": int(self.config.global_limit.disable_seconds or 90),
            },
            "limits": {
                "providers": [
                    {
                        "name": p.name,
                        "max_requests_per_minute": int(p.max_requests_per_minute or 0),
                        "disable_seconds": int(p.disable_seconds or 90),
                    }
                    for p in (self.config.limits.providers or [])
                    if p.name
                ],
                "models": [
                    {
                        "name": m.name,
                        "provider": m.provider,
                        "max_requests_per_minute": int(m.max_requests_per_minute or 0),
                        "disable_seconds": int(m.disable_seconds or 90),
                    }
                    for m in (self.config.limits.models or [])
                    if m.name
                ],
            },
            "error_disable": {
                "enabled": bool(self.config.error_disable.enabled),
                "base_seconds": int(self.config.error_disable.base_seconds or 60),
                "max_seconds": int(self.config.error_disable.max_seconds or 3600),
                "exponential": bool(self.config.error_disable.exponential),
                "multiplier": float(self.config.error_disable.multiplier or 2.0),
            },
            "feature_kill": {
                "enabled": bool(self.config.feature_kill.enabled),
                "features": [
                    {"feature": f.feature, "models": list(f.models or [])}
                    for f in (self.config.feature_kill.features or [])
                    if f.feature
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
        # 保留 existing 中本插件未管理的顶层键（若有）
        for key, value in existing.items():
            if key not in payload:
                payload[key] = value
        write_simple_toml(config_path, payload)

    def _build_policy(self) -> HoldOnPolicy:
        providers = [
            NamedLimit(
                name=str(p.name or "").strip(),
                max_requests_per_minute=int(p.max_requests_per_minute or 0),
                disable_seconds=int(p.disable_seconds or 90),
            )
            for p in (self.config.limits.providers or [])
            if str(p.name or "").strip()
        ]
        models = [
            NamedLimit(
                name=str(m.name or "").strip(),
                provider=str(m.provider or "").strip(),
                max_requests_per_minute=int(m.max_requests_per_minute or 0),
                disable_seconds=int(m.disable_seconds or 90),
            )
            for m in (self.config.limits.models or [])
            if str(m.name or "").strip()
        ]
        features = [
            FeatureModels(
                feature=str(f.feature or "").strip(),
                models=tuple(str(x).strip() for x in (f.models or []) if str(x).strip()),
            )
            for f in (self.config.feature_kill.features or [])
            if str(f.feature or "").strip()
        ]
        return HoldOnPolicy(
            self.state,
            # 全局 RPM 已停用：始终按 0 处理，避免旧配置误伤
            global_max_rpm=0,
            global_disable_seconds=int(self.config.global_limit.disable_seconds or 90),
            provider_limits=providers,
            model_limits=models,
            features=features,
            error_base_seconds=int(self.config.error_disable.base_seconds or 60),
            error_max_seconds=int(self.config.error_disable.max_seconds or 3600),
            error_exponential=bool(self.config.error_disable.exponential),
            error_multiplier=float(self.config.error_disable.multiplier or 2.0),
            feature_kill_enabled=bool(self.config.feature_kill.enabled),
        )

    def _active(self) -> bool:
        return bool(self.config.plugin.enabled) and bool(self.config.global_limit.enabled)

    def _should_global_stop(self) -> bool:
        self.policy.refresh_feature_kill()
        return bool(self.state.global_stop or self.state.is_disabled("global", "all"))

    async def _on_snapshot_error(
        self,
        *,
        model_name: str = "",
        provider: str = "",
        message: str = "",
        retry_after: Optional[float] = None,
        source_path: str = "",
        **_: Any,
    ) -> None:
        if not self._active() or not bool(self.config.error_disable.enabled):
            return
        events = self.policy.disable_for_error(
            model_name=model_name,
            provider=provider,
            message=message,
            retry_after=retry_after,
        )
        self.ctx.logger.warning(
            "hold_on 错误禁用：model=%s provider=%s path=%s events=%s",
            model_name,
            provider,
            Path(source_path).name if source_path else "",
            [e.kind for e in events],
        )
        await self._notify_events(events)

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
            self.ctx.logger.info(
                "hold_on 通知未找到已有群会话，尝试 open_session group_id=%s platform=%s found=%r",
                group_id,
                platform,
                found,
            )
            opened = await self.ctx.chat.open_session(
                platform=platform,
                chat_type="group",
                group_id=group_id,
            )
            stream_id = self._pick_stream_id(opened)
            if not stream_id:
                self.ctx.logger.warning(
                    "hold_on 通知无法解析群会话 group_id=%s platform=%s opened=%r",
                    group_id,
                    platform,
                    opened,
                )
            return stream_id

        if target_type == "private":
            user_id = str(cfg.user_id or "").strip()
            if not user_id:
                self.ctx.logger.warning("hold_on 通知 target_type=private 但 user_id 为空")
                return ""
            found = await self.ctx.chat.get_stream_by_user_id(user_id=user_id, platform=platform)
            stream_id = self._pick_stream_id(found)
            if stream_id:
                return stream_id
            self.ctx.logger.info(
                "hold_on 通知未找到已有私聊会话，尝试 open_session user_id=%s platform=%s found=%r",
                user_id,
                platform,
                found,
            )
            opened = await self.ctx.chat.open_session(
                platform=platform,
                chat_type="private",
                user_id=user_id,
            )
            stream_id = self._pick_stream_id(opened)
            if not stream_id:
                self.ctx.logger.warning(
                    "hold_on 通知无法解析私聊会话 user_id=%s platform=%s opened=%r",
                    user_id,
                    platform,
                    opened,
                )
            return stream_id

        self.ctx.logger.warning("hold_on 未知 notify.target_type=%r", target_type)
        return ""

    @staticmethod
    def _event_label(kind: str) -> str:
        return {
            "rpm_global": "全局限流",
            "rpm_provider": "厂商限流",
            "rpm_model": "模型限流",
            "error_model": "模型错误禁用",
            "global_stop": "全局停模",
        }.get(kind, kind)

    async def _notify_events(self, events: List[PolicyEvent]) -> None:
        if not events:
            return
        if not bool(self.config.notify.enabled):
            self.ctx.logger.debug(
                "hold_on 有策略事件但 notify.enabled=false，已跳过转发 events=%s",
                [e.kind for e in events],
            )
            return
        # 同批合并为一条，避免 RPM+全局停模连发两条
        lines = ["触发稍等策略："]
        seen: set[str] = set()
        for event in events:
            key = f"{event.kind}:{event.reason}"
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"- [{self._event_label(event.kind)}] {event.reason}")
        text = "\n".join(lines)
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
                "hold_on 通知未配置有效目标，已跳过发送 target_type=%s group_id=%r user_id=%r stream_id=%r",
                cfg.target_type,
                cfg.group_id,
                cfg.user_id,
                cfg.stream_id,
            )
            return
        try:
            sent = await self.ctx.send.text(outbound, target)
            if not sent:
                self.ctx.logger.warning("hold_on 通知发送失败 target=%s", target)
            else:
                self.ctx.logger.info("hold_on 已转发通知 target=%s events=%s", target, [e.kind for e in events])
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
        """命令类消息放行，供本插件及其他插件的 @Command 处理。"""

        return str(text or "").strip().startswith("/")

    # ─── Hooks ───────────────────────────────────────────────────

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="hold_on_replyer_count",
        description="对选中模型做限流计数（不改写请求）",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
    )
    async def handle_replyer_before_model(
        self,
        session_id: str = "",
        selected_model_name: str = "",
        requested_model_name: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        del kwargs
        if not self._active() or self._should_global_stop():
            return {"action": "continue"}

        model = str(selected_model_name or requested_model_name or "").strip()
        if session_id and model:
            self._last_selected_model[str(session_id)] = model
        if model:
            events = self.policy.note_request(model_name=model)
            if events:
                self.ctx.logger.warning(
                    "hold_on 限流触发：model=%s events=%s",
                    model,
                    [(e.kind, e.reason) for e in events],
                )
                await self._notify_events(events)
        return {"action": "continue"}

    @HookHandler(
        "maisaka.replyer.after_response",
        name="hold_on_replyer_success",
        description="成功时重置错误连击（不改写响应）",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
    )
    async def handle_replyer_after_response(
        self,
        session_id: str = "",
        requested_model_name: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        del kwargs
        if not self._active():
            return {"action": "continue"}
        model = str(
            requested_model_name
            or self._last_selected_model.get(str(session_id), "")
            or ""
        ).strip()
        if model:
            self.policy.on_success(model)
        return {"action": "continue"}

    @HookHandler(
        "chat.receive.after_process",
        name="hold_on_receive_abort",
        description="全局停模时 abort 入站（晚序：先让其他插件有机会截获）",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
    )
    async def handle_receive_after_process(self, message: Any = None, **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        if not self._active() or not self._should_global_stop():
            return {"action": "continue"}

        text = self._message_plain(message)
        # 放行命令：本插件 /稍等 /解除，以及其他插件的 /xxx
        if self._looks_like_command(text):
            return {"action": "continue"}

        self.ctx.logger.info("hold_on 全局停模：abort 入站 text=%.40r", text)
        return {"action": "abort"}

    # ─── 命令 ────────────────────────────────────────────────────

    @Command(
        "holdon_status",
        description="稍等状态",
        pattern=r"/稍等\s*$",
    )
    async def cmd_status(self, **kwargs: Any):
        stream_id = kwargs.get("stream_id", "")
        if not await self._is_admin(kwargs.get("platform", ""), kwargs.get("user_id", "")):
            return await self._deny(stream_id)
        return await self._send(stream_id, self.policy.status_text())

    @Command(
        "holdon_clear",
        description="解除稍等",
        pattern=r"/解除\s*$",
    )
    async def cmd_clear(self, **kwargs: Any):
        stream_id = kwargs.get("stream_id", "")
        if not await self._is_admin(kwargs.get("platform", ""), kwargs.get("user_id", "")):
            return await self._deny(stream_id)
        n = self.state.clear_all()
        return await self._send(stream_id, f"已全局解除稍等（清理 {n} 项）。")


def create_plugin() -> HoldOnPlugin:
    return HoldOnPlugin()
