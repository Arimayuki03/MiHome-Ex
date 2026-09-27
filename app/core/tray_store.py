# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""托盘快捷设备的本地持久化。

与 workbench.json 同目录，单独文件 tray.json，默认空列表，由用户
在管理对话框中自主添加；顺序即展示顺序。

v2 起每台设备可带 span（占用的横向格数，1=半宽、2=整行；托盘
网格为 1/2 列）——
    {"version": 2, "devices": [{"did": "xx", "span": 2}, ...]}
v1 的 str 列表读取时自动迁移为 span=1。
"""

from app.core import _json_store

_VERSION = 2
_FILENAME = "tray.json"


def _empty() -> dict:
    return {"version": _VERSION, "devices": []}


def _normalize_entry(entry) -> dict | None:
    """单条设备记录规整：v1 str / v2 dict -> {"did", "span"}。"""
    if isinstance(entry, str):
        return {"did": entry, "span": 1}
    if isinstance(entry, dict) and isinstance(entry.get("did"), str):
        try:
            span = max(1, min(int(entry.get("span") or 1), 4))
        except (TypeError, ValueError):
            span = 1
        return {"did": entry["did"], "span": span}
    return None


def _read_raw() -> dict:
    raw = _json_store.read_json(_json_store.data_file(_FILENAME), _empty())
    devices = raw.get("devices")
    if not isinstance(devices, list):
        return _empty()
    entries = [e for e in (_normalize_entry(d) for d in devices) if e]
    if not entries and devices:
        return _empty()
    raw["version"] = _VERSION
    raw["devices"] = entries
    return raw


def _write_raw(raw: dict) -> None:
    _json_store.write_json(_json_store.data_file(_FILENAME), raw)


def load() -> list[str]:
    """托盘设备 did 列表，按添加顺序。"""
    return [e["did"] for e in _read_raw().get("devices", [])]


def load_entries() -> list[dict]:
    """托盘设备条目（含 span），按添加顺序：[{"did", "span"}, ...]。"""
    return [dict(e) for e in _read_raw().get("devices", [])]


def span_of(did: str) -> int:
    """设备占用的横向格数（未知设备返回 1）。"""
    for entry in _read_raw().get("devices", []):
        if entry["did"] == str(did):
            return int(entry.get("span") or 1)
    return 1


def save(dids: list[str]) -> None:
    raw = _read_raw()
    # 去重保序；保留已有设备的 span（管理对话框保存只管勾选集合）
    spans = {e["did"]: e.get("span", 1) for e in raw.get("devices", [])}
    seen: set[str] = set()
    uniq: list[dict] = []
    for d in dids:
        s = str(d)
        if s not in seen:
            seen.add(s)
            uniq.append({"did": s, "span": spans.get(s, 1)})
    raw["devices"] = uniq
    _write_raw(raw)


def set_span(did: str, span: int) -> None:
    """设置单台设备占用的横向格数（1-4，落盘时钳到合理值）。"""
    did = str(did)
    raw = _read_raw()
    for entry in raw.get("devices", []):
        if entry["did"] == did:
            entry["span"] = max(1, min(int(span), 4))
            _write_raw(raw)
            return


def add(did: str) -> None:
    dids = load()
    if str(did) not in dids:
        dids.append(str(did))
        save(dids)


def remove(did: str) -> None:
    dids = load()
    s = str(did)
    if s in dids:
        dids.remove(s)
        save(dids)


def contains(did: str) -> bool:
    return str(did) in load()
