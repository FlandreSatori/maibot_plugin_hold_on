"""监听宿主失败快照 JSON，捕获硬错误（403/502/503 等）。"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Callable, Awaitable, Dict, List, Optional, Set


ErrorHandler = Callable[..., Awaitable[None]]


class ErrorSnapshotWatcher:
    """轮询 ``logs/llm_request`` 与 ``logs/maisaka_prompt/llm_error``。"""

    def __init__(
        self,
        *,
        roots: List[Path],
        interval_seconds: float,
        on_error: ErrorHandler,
        logger: Any,
    ) -> None:
        self._roots = [Path(p) for p in roots if p]
        self._interval = max(0.5, float(interval_seconds or 2.0))
        self._on_error = on_error
        self._logger = logger
        self._seen: Set[str] = set()
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._bootstrapped = False

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="hold_on_error_watch")

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except Exception:
                task.cancel()

    async def _loop(self) -> None:
        # 启动时只记录已有文件，避免重启后批量误伤
        if not self._bootstrapped:
            for path in self._iter_snapshot_files():
                self._seen.add(str(path.resolve()))
            self._bootstrapped = True
            self._logger.info(
                "hold_on 错误快照监听已启动：roots=%s interval=%.1fs seen=%s",
                [str(r) for r in self._roots],
                self._interval,
                len(self._seen),
            )
        while not self._stop.is_set():
            try:
                await self._scan_once()
            except Exception as exc:
                self._logger.warning("hold_on 扫描错误快照失败: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                continue

    def _iter_snapshot_files(self) -> List[Path]:
        files: List[Path] = []
        for root in self._roots:
            if not root.exists():
                continue
            try:
                if root.is_file() and root.suffix.lower() == ".json":
                    files.append(root)
                    continue
                # llm_request: 扁平 *.json
                for path in root.glob("*.json"):
                    if path.is_file():
                        files.append(path)
                # maisaka_prompt/llm_error/**/*.json
                for path in root.rglob("*.json"):
                    if path.is_file() and path not in files:
                        files.append(path)
            except Exception as exc:
                self._logger.debug("hold_on 列举快照目录失败 %s: %s", root, exc)
        return files

    async def _scan_once(self) -> None:
        for path in self._iter_snapshot_files():
            key = str(path.resolve())
            if key in self._seen:
                continue
            # 略等写入完成
            try:
                age = time.time() - path.stat().st_mtime
                if age < 0.2:
                    continue
            except OSError:
                continue
            self._seen.add(key)
            if len(self._seen) > 5000:
                # 粗暴裁剪：保留后半
                self._seen = set(list(self._seen)[-2500:])
            payload = self._read_json(path)
            if not payload:
                continue
            extracted = self._extract_error(payload, path)
            if not extracted:
                continue
            try:
                await self._on_error(**extracted)
            except Exception as exc:
                self._logger.warning("hold_on 处理错误快照失败 %s: %s", path.name, exc)

    @staticmethod
    def _read_json(path: Path) -> Optional[Dict[str, Any]]:
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _extract_error(self, payload: Dict[str, Any], path: Path) -> Optional[Dict[str, Any]]:
        # 标准 llm_request 失败快照
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        model_info = payload.get("model_info") if isinstance(payload.get("model_info"), dict) else {}
        api_provider = payload.get("api_provider") if isinstance(payload.get("api_provider"), dict) else {}

        model_name = str(
            model_info.get("name")
            or payload.get("model_name")
            or payload.get("model")
            or ""
        ).strip()
        provider = str(
            api_provider.get("name")
            or payload.get("provider")
            or payload.get("api_provider_name")
            or ""
        ).strip()

        message_parts: List[str] = []
        if error:
            message_parts.append(str(error.get("message") or ""))
            if error.get("type"):
                message_parts.append(str(error.get("type")))
            body = error.get("response_body")
            if body is not None:
                message_parts.append(json.dumps(body, ensure_ascii=False) if not isinstance(body, str) else body)
        for key in ("message", "error_message", "detail", "title", "what_you_should_do"):
            value = payload.get(key)
            if value:
                message_parts.append(str(value))

        message = "\n".join(p for p in message_parts if p).strip() or "模型请求失败"

        # 只处理失败快照目录 / 带 error 字段的文件，避免误伤普通 prompt dump
        path_text = str(path).replace("\\", "/").lower()
        in_error_dir = ("/llm_request" in path_text) or ("/llm_error" in path_text)
        if not in_error_dir and not error:
            return None
        if not model_name:
            return None

        from .error_classify import classify_error

        retry_after = self._find_retry_after(payload, error)
        error_type = classify_error(message, error=error, payload=payload)
        return {
            "model_name": model_name,
            "provider": provider,
            "message": message,
            "error_type": error_type,
            "error": error,
            "payload": payload,
            "retry_after": retry_after,
            "source_path": str(path),
        }

    @staticmethod
    def _find_retry_after(payload: Dict[str, Any], error: Dict[str, Any]) -> Optional[float]:
        candidates: List[Any] = []
        body = error.get("response_body") if error else None
        if isinstance(body, dict):
            candidates.append(body.get("retry_after"))
            candidates.append(body.get("retry-after"))
        candidates.append(payload.get("retry_after"))
        for item in candidates:
            if item is None:
                continue
            try:
                return float(item)
            except (TypeError, ValueError):
                continue
        return None


def resolve_watch_roots(configured: SequenceLike, cwd: Optional[Path] = None) -> List[Path]:
    """解析监听根目录。"""

    roots: List[Path] = []
    for item in configured or []:
        text = str(item or "").strip()
        if not text:
            continue
        roots.append(Path(text))
    base = Path(cwd or Path.cwd()).resolve()
    defaults = [
        base / "logs" / "llm_request",
        base / "logs" / "maisaka_prompt" / "llm_error",
        base / "MaiBot" / "logs" / "llm_request",
        base / "MaiBot" / "logs" / "maisaka_prompt" / "llm_error",
    ]
    for path in defaults:
        if path not in roots:
            roots.append(path)
    # 去重保序
    uniq: List[Path] = []
    seen: Set[str] = set()
    for path in roots:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(path)
    return uniq


# 兼容类型别名（避免 typing 循环）
SequenceLike = Any
