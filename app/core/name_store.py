# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""产品页中文名缓存。

无公开 spec 的设备（蓝牙手环、牙刷等）只能从 miot-spec 产品页取本地化
商品名，而该页面抓取慢且经常超时。成功结果落盘后跨会话复用，避免每次
启动都重新踩一遍超时；失败不写盘，下次启动还有机会重试。
"""

import json

from app.core import _json_store

_FILENAME = "product_names.json"


def load() -> dict[str, str]:
    """model -> 中文商品名；文件缺失或损坏返回空表。"""
    path = _json_store.data_file(_FILENAME)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(model): name for model, name in raw.items()
            if isinstance(name, str) and name}


def save(names: dict[str, str]) -> None:
    """整表覆盖写入；只传成功解析出的名字。"""
    _json_store.write_json(_json_store.data_file(_FILENAME), names)
