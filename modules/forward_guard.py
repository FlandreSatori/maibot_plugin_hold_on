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
    def _count_in_seg(seg: Any) -> int:
        # direct image segment
        if _is_image_segment(seg):
            return 1
        # forward segment: try to inspect nested nodes
        if _is_forward_segment(seg):
            data = seg.get("data")
            total = 0
            # list of nodes
            if isinstance(data, list):
                for node in data:
                    if not isinstance(node, dict):
                        continue
                    # node may wrap a message or raw_message
                    nested_raw = node.get("raw_message") or node.get("message") or node.get("data")
                    if isinstance(nested_raw, dict):
                        nested_raw = nested_raw.get("raw_message") or nested_raw.get("message") or nested_raw
                    if isinstance(nested_raw, list):
                        for nseg in nested_raw:
                            total += _count_in_seg(nseg)
                        continue
                    # node itself might be an image segment
                    if _is_image_segment(node):
                        total += 1
                return total
            # dict-like forward data
            if isinstance(data, dict):
                # try common nested keys
                nested = data.get("raw_message") or data.get("message") or data.get("data")
                if isinstance(nested, list):
                    for nseg in nested:
                        total += _count_in_seg(nseg)
                else:
                    # if data itself looks like an image segment
                    if _is_image_segment(data):
                        total += 1
                return total
        return 0

    return sum(_count_in_seg(seg) for seg in raw_message)


def should_abort_for_forward_images(message: Any, *, threshold: int = 0) -> bool:
    """若消息是合并转发且图片数达到阈值，则返回 True。"""
    if threshold <= 0:
        return False
    if not message_has_forward_context(message):
        return False
    return count_images_in_message(message) >= threshold
