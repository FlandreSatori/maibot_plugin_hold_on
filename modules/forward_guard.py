from __future__ import annotations

from typing import Any


def _is_forward_segment(seg: Any) -> bool:
    if not isinstance(seg, dict):
        return False
    seg_type = str(seg.get("type", "")).strip().lower()
    if seg_type == "forward":
        return True
    data = seg.get("data")
    if isinstance(data, dict):
        inner_type = str(data.get("type", "")).strip().lower()
        return inner_type == "forward"
    return False


def _is_image_segment(seg: Any) -> bool:
    if not isinstance(seg, dict):
        return False
    seg_type = str(seg.get("type", "")).strip().lower()
    if seg_type == "image":
        return True
    data = seg.get("data")
    if isinstance(data, dict):
        inner_type = str(data.get("type", "")).strip().lower()
        return inner_type == "image"
    return False


def message_has_forward_context(message: Any) -> bool:
    """判断消息是否为合并转发上下文。"""
    if not isinstance(message, dict):
        return False
    raw_message = message.get("raw_message")
    if isinstance(raw_message, list):
        for seg in raw_message:
            if _is_forward_segment(seg):
                return True
    text = str(message.get("processed_plain_text", "") or "")
    return "【合并转发消息:" in text or "【合并转发消息：" in text


def count_images_in_message(message: Any) -> int:
    """统计消息里出现的图片段数量。"""
    if not isinstance(message, dict):
        return 0
    raw_message = message.get("raw_message")
    if not isinstance(raw_message, list):
        return 0
    return sum(1 for seg in raw_message if _is_image_segment(seg))


def should_abort_for_forward_images(message: Any, *, threshold: int = 0) -> bool:
    """若消息是合并转发且图片数达到阈值，则返回 True。"""
    if threshold <= 0:
        return False
    if not message_has_forward_context(message):
        return False
    return count_images_in_message(message) >= threshold
