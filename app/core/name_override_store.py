# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""设备自定义名称的本地缓存。

用户改过的名字只存本地，不回写米家云端：设备名属于账号数据，本地
覆盖既能满足个性化，又不会影响手机端与其它客户端。键是 did，设备
换型号或重配后 did 变化，旧条目自然失效。
"""

import json

from app.core import _json_store

_FILENAME = "device_names.json"

# 进程内缓存：读取方分布在开关轮询回调里（每几秒一次），每次都读盘
# 纯属浪费；写入时同步更新，保证读到的始终是最新值
_cache: dict[str, str] | None = None


def _read() -> dict[str, str]:
    global _cache
    if _cache is not None:
        return _cache
    path = _json_store.data_file(_FILENAME)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    _cache = {str(did): name for did, name in raw.items()
              if isinstance(name, str) and name.strip()}
    return _cache


def get(did: str) -> str | None:
    """该设备的自定义名称，未设置返回 None。"""
    return _read().get(did)


def load_all() -> dict[str, str]:
    """did -> 自定义名称的完整映射；返回副本，调用方改动不影响缓存。"""
    return dict(_read())


def set_name(did: str, name: str) -> None:
    """写入自定义名称；传空串表示恢复默认（删除条目）。"""
    names = dict(_read())
    clean = name.strip()
    if clean:
        names[did] = clean
    else:
        names.pop(did, None)
    global _cache
    _cache = names
    _json_store.write_json(_json_store.data_file(_FILENAME), names)
