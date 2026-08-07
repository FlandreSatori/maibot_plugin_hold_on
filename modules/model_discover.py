"""从宿主 model_config.toml 发现任务→模型与厂商映射。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence


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
