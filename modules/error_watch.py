"""监听宿主失败快照 JSON（schema_version=3，目录 logs/maisaka_prompt/llm_error）。"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from .error_classify import classify_error

ErrorHandler = Callable[..., Awaitable[None]]


class ErrorSnapshotWatcher:
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
                for path in root.rglob("*.json"):
                    if path.is_file():
                        files.append(path)
            except Exception as exc:
                self._logger.debug("hold_on 列举快照目录失败 %s: %s", root, exc)
        return files

    async def _scan_once(self) -> None:
        for path in self._iter_snapshot_files():
            key = str(path.resolve())
            if key in self._seen:
                continue
            try:
                if time.time() - path.stat().st_mtime < 0.2:
                    continue
            except OSError:
                continue
            self._seen.add(key)
            if len(self._seen) > 5000:
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
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _extract_error(self, payload: Dict[str, Any], path: Path) -> Optional[Dict[str, Any]]:
        path_text = str(path).replace("\\", "/").lower()
        if "/llm_error" not in path_text:
            return None

        attempt = self._latest_failed_attempt(payload)
        error = attempt.get("error") if isinstance(attempt.get("error"), dict) else None
        if not error:
            return None

        model_info = attempt.get("model_info") if isinstance(attempt.get("model_info"), dict) else {}
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        api_provider = attempt.get("api_provider") if isinstance(attempt.get("api_provider"), dict) else {}

        model_name = str(
            attempt.get("model_name")
            or model_info.get("name")
            or metadata.get("model_name")
            or ""
        ).strip()
        provider = str(
            attempt.get("provider_name")
            or api_provider.get("name")
            or metadata.get("provider_name")
            or ""
        ).strip()
        if not model_name:
            return None

        message_parts: List[str] = [
            str(error.get("message") or ""),
            str(error.get("type") or ""),
        ]
        if error.get("status_code") is not None:
            message_parts.append(str(error.get("status_code")))
        body = error.get("response_body")
        if body is not None:
            message_parts.append(
                json.dumps(body, ensure_ascii=False) if not isinstance(body, str) else body
            )
        message = "\n".join(p for p in message_parts if p).strip() or "模型请求失败"
        feature = str(
            attempt.get("task_name")
            or attempt.get("request_type")
            or metadata.get("task_name")
            or metadata.get("request_type")
            or ""
        ).strip()

        return {
            "model_name": model_name,
            "provider": provider,
            "feature": feature,
            "message": message,
            "error_type": classify_error(message, error=error),
            "error": error,
            "source_path": str(path),
        }

    @staticmethod
    def _latest_failed_attempt(payload: Dict[str, Any]) -> Dict[str, Any]:
        attempts = payload.get("attempts")
        if not isinstance(attempts, list) or not attempts:
            return {}
        for item in reversed(attempts):
            if not isinstance(item, dict):
                continue
            err = item.get("error")
            if isinstance(err, dict) and (err.get("message") or err.get("status_code") or err.get("type")):
                return item
        return {}


def resolve_watch_roots(configured: Any, cwd: Optional[Path] = None) -> List[Path]:
    roots: List[Path] = []
    for item in configured or []:
        text = str(item or "").strip()
        if text:
            roots.append(Path(text))
    base = Path(cwd or Path.cwd()).resolve()
    defaults = [
        base / "logs" / "maisaka_prompt" / "llm_error",
        base / "MaiBot" / "logs" / "maisaka_prompt" / "llm_error",
    ]
    for path in defaults:
        if path not in roots:
            roots.append(path)
    uniq: List[Path] = []
    seen: Set[str] = set()
    for path in roots:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(path)
    return uniq
