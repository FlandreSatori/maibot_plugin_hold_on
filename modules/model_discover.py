"""从宿主 model_config.toml 发现任务→模型与厂商映射。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


@dataclass
class DiscoveredModel:
    name: str
    provider: str = ""


@dataclass
class DiscoveredFeature:
    feature: str
    models: List[str] = field(default_factory=list)


@dataclass
class DiscoveryResult:
    models: List[DiscoveredModel] = field(default_factory=list)
    features: List[DiscoveredFeature] = field(default_factory=list)
    providers: List[str] = field(default_factory=list)
    source_path: str = ""


def candidate_model_config_paths(extra: Sequence[str] | None = None) -> List[Path]:
    """常见宿主路径候选。"""

    roots: List[Path] = []
    for item in extra or []:
        text = str(item or "").strip()
        if text:
            roots.append(Path(text))

    cwd = Path.cwd().resolve()
    defaults = [
        cwd / "config" / "model_config.toml",
        cwd / "MaiBot" / "config" / "model_config.toml",
        cwd.parent / "config" / "model_config.toml",
        cwd.parent / "MaiBot" / "config" / "model_config.toml",
    ]
    # 从本插件目录向上找
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        defaults.append(parent / "config" / "model_config.toml")
        defaults.append(parent / "MaiBot" / "config" / "model_config.toml")

    uniq: List[Path] = []
    seen = set()
    for path in [*roots, *defaults]:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(path)
    return uniq


def find_model_config(extra: Sequence[str] | None = None) -> Optional[Path]:
    for path in candidate_model_config_paths(extra):
        if path.is_file():
            return path
    return None


def discover_from_toml(path: Path) -> DiscoveryResult:
    raw = path.read_bytes()
    data = tomllib.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        return DiscoveryResult(source_path=str(path))

    models_raw = data.get("models") or []
    models: List[DiscoveredModel] = []
    providers: List[str] = []
    if isinstance(models_raw, list):
        for item in models_raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            provider = str(item.get("api_provider") or "").strip()
            if not name:
                continue
            models.append(DiscoveredModel(name=name, provider=provider))
            if provider and provider not in providers:
                providers.append(provider)

    task_cfg = data.get("model_task_config") or {}
    features: List[DiscoveredFeature] = []
    if isinstance(task_cfg, dict):
        for feature, cfg in task_cfg.items():
            if not isinstance(cfg, dict):
                continue
            model_list = cfg.get("model_list") or []
            names = [str(x).strip() for x in model_list if str(x).strip()]
            if names:
                features.append(DiscoveredFeature(feature=str(feature), models=names))

    return DiscoveryResult(
        models=models,
        features=features,
        providers=providers,
        source_path=str(path),
    )


def discover_models(extra_paths: Sequence[str] | None = None) -> Optional[DiscoveryResult]:
    path = find_model_config(extra_paths)
    if path is None:
        return None
    try:
        return discover_from_toml(path)
    except Exception:
        return None


def discovery_to_config_sections(
    result: DiscoveryResult,
    *,
    default_model_rpm: int = 10,
    default_model_disable: int = 90,
    default_provider_rpm: int = 30,
    default_provider_disable: int = 90,
) -> Dict[str, Any]:
    """转为可写入 config.toml / 内存配置的结构。"""

    return {
        "limits": {
            "providers": [
                {
                    "name": name,
                    "max_requests_per_minute": default_provider_rpm,
                    "disable_seconds": default_provider_disable,
                }
                for name in result.providers
            ],
            "models": [
                {
                    "name": m.name,
                    "provider": m.provider,
                    "max_requests_per_minute": default_model_rpm,
                    "disable_seconds": default_model_disable,
                }
                for m in result.models
            ],
        },
        "feature_kill": {
            "features": [
                {"feature": f.feature, "models": list(f.models)} for f in result.features
            ],
        },
    }


def write_simple_toml(path: Path, data: Dict[str, Any]) -> None:
    """写入本插件够用的 TOML（不依赖 tomlkit）。"""

    lines: List[str] = []

    def emit_value(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            return str(value)
        if isinstance(value, list):
            inner = ", ".join(emit_value(v) for v in value)
            return f"[{inner}]"
        text = str(value).replace("\\", "\\\\").replace('"', '\\"')
        return f'"{text}"'

    def emit_table(prefix: str, table: Dict[str, Any]) -> None:
        scalars = {k: v for k, v in table.items() if not isinstance(v, (dict, list))}
        list_of_tables = {
            k: v
            for k, v in table.items()
            if isinstance(v, list) and v and all(isinstance(i, dict) for i in v)
        }
        nested = {k: v for k, v in table.items() if isinstance(v, dict)}
        plain_lists = {
            k: v
            for k, v in table.items()
            if isinstance(v, list) and k not in list_of_tables
        }

        if prefix:
            lines.append(f"[{prefix}]")
        for key, value in scalars.items():
            lines.append(f"{key} = {emit_value(value)}")
        for key, value in plain_lists.items():
            lines.append(f"{key} = {emit_value(value)}")
        if scalars or plain_lists:
            lines.append("")

        for key, items in list_of_tables.items():
            array_prefix = f"{prefix}.{key}" if prefix else key
            for item in items:
                lines.append(f"[[{array_prefix}]]")
                for ik, iv in item.items():
                    lines.append(f"{ik} = {emit_value(iv)}")
                lines.append("")

        for key, child in nested.items():
            child_prefix = f"{prefix}.{key}" if prefix else key
            emit_table(child_prefix, child)

    for key, value in data.items():
        if isinstance(value, dict):
            emit_table(key, value)
        else:
            lines.append(f"{key} = {emit_value(value)}")
            lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def merge_discovery_into_config_dict(
    existing: Dict[str, Any],
    sections: Dict[str, Any],
) -> Dict[str, Any]:
    """用发现结果覆盖 limits / feature_kill.features（保留其余段）。"""

    out = dict(existing or {})
    limits = out.setdefault("limits", {})
    if not isinstance(limits, dict):
        limits = {}
        out["limits"] = limits
    limits["models"] = sections.get("limits", {}).get("models", [])
    limits["providers"] = sections.get("limits", {}).get("providers", [])

    feature_kill = out.setdefault("feature_kill", {})
    if not isinstance(feature_kill, dict):
        feature_kill = {}
        out["feature_kill"] = feature_kill
    feature_kill["features"] = sections.get("feature_kill", {}).get("features", [])
    if "enabled" not in feature_kill:
        feature_kill["enabled"] = True

    return out
