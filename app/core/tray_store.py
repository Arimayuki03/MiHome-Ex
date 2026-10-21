# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Ex: 米家设备的 Windows 桌面控制端（扩展版）
# Copyright (C) 2026 MiHome-Ex contributors
"""托盘快捷项的本地持久化。

与 workbench.json 同目录，单独文件 tray.json，默认空列表，由用户
在管理对话框中自主添加。devices/scenes 记录成员集合（设备存 did；
场景存必要快照 scene_id/name/home_id/home_name，执行只认 id 与
home_id，快照名保证离线/未刷新时快捷窗口仍可显示）；order 记录
两类项目混排后的统一展示顺序，快捷窗口按它渲染。

v2 起每台设备可带 span（占用的横向格数，1=半宽、2=整行；托盘
网格为 1/2 列）——
    {"version": 2, "devices": [{"did": "xx", "span": 2}, ...],
     "scenes": [...], "order": [...]}
v1 的 str 列表、旧 fork 的 dict 条目与上游无 span 的 str 列表均可在
读取时归一化，用户无需手工迁移。
"""

from app.core import _json_store

_VERSION = 2
_FILENAME = "tray.json"
_SCENE_KEYS = ("scene_id", "name", "home_id", "home_name")


def _empty() -> dict:
    return {"version": _VERSION, "devices": [], "scenes": [], "order": []}


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


def _normalize_scene(item) -> dict | None:
    """场景快照规整；scene_id 为空的条目丢弃。"""
    if not isinstance(item, dict) or not str(item.get("scene_id") or ""):
        return None
    return {k: str(item.get(k) or "") for k in _SCENE_KEYS}


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
    raw["scenes"] = [
        s for s in (_normalize_scene(item) for item in raw.get("scenes") or [])
        if s
    ] if isinstance(raw.get("scenes"), list) else []
    raw["order"] = raw.get("order") if isinstance(raw.get("order"), list) else []
    return raw


def _write_raw(raw: dict) -> None:
    _json_store.write_json(_json_store.data_file(_FILENAME), raw)


def load() -> list[str]:
    """托盘设备 did 列表，按 devices 中的相对顺序。"""
    return [e["did"] for e in _read_raw().get("devices", [])]


def load_entries() -> list[dict]:
    """托盘设备条目（含 span），按 devices 中的相对顺序。"""
    return [dict(e) for e in _read_raw().get("devices", [])]


def span_of(did: str) -> int:
    """设备占用的横向格数（未知设备返回 1）。"""
    for entry in _read_raw().get("devices", []):
        if entry["did"] == str(did):
            return int(entry.get("span") or 1)
    return 1


def load_scenes() -> list[dict]:
    """托盘的场景快照列表；非法条目直接丢弃。"""
    return [dict(s) for s in _read_raw().get("scenes", [])]


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


def save_scenes(scenes: list[dict]) -> None:
    raw = _read_raw()
    seen: set[str] = set()
    uniq: list[dict] = []
    for item in scenes:
        scene = _normalize_scene(item)
        if scene is None or scene["scene_id"] in seen:
            continue
        seen.add(scene["scene_id"])
        uniq.append(scene)
    raw["scenes"] = uniq
    _write_raw(raw)


def load_order() -> list[dict]:
    """设备与场景混排后的统一展示顺序，元素为 {"type","id"}。

    旧数据没有 order 字段时合成（设备在前、场景在后），与历史快捷
    窗口的显示顺序一致，用户无需重新排序。
    """
    raw = _read_raw().get("order")
    if isinstance(raw, list):
        result = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            t = str(item.get("type") or "")
            i = str(item.get("id") or "")
            if t in ("device", "scene") and i:
                result.append({"type": t, "id": i})
        if result:
            return result
    return ([{"type": "device", "id": d} for d in load()] +
            [{"type": "scene", "id": s["scene_id"]} for s in load_scenes()])


def save_order(order: list[dict]) -> None:
    raw = _read_raw()
    seen: set[tuple[str, str]] = set()
    uniq: list[dict] = []
    for item in order:
        t = str(item.get("type") or "")
        i = str(item.get("id") or "")
        if t not in ("device", "scene") or not i or (t, i) in seen:
            continue
        seen.add((t, i))
        uniq.append({"type": t, "id": i})
    raw["order"] = uniq
    _write_raw(raw)


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
