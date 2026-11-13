# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""桌面挂件（Widgets Board）的本地持久化。

与 tray.json / workbench.json 同目录，单独文件 board.json。
与托盘快捷面板的设备清单**相互独立**：托盘是"临时呼出、用完即走"，
Board 是"常驻抬眼即用"，两者设备诉求不同（Board 放常开的灯与空调，
托盘放偶尔查看的摄像头），互不牵连。

持久化字段：
    enabled  是否启用桌面挂件（开启即启动时显示，关闭则整块不出现）
    devices  设备 did 列表，顺序即展示顺序
    columns  单排最多放几张设备卡片（1–9）
    opacity  窗口透明度（0.30–1.00，拖动滑块时实时写入）
    geom     窗口位置与宽高，恢复上次摆放
    collapsed 折叠为细横条的开关
    bg       背景色（"#rrggbb"）；None = 跟随主题
    topmost  窗口是否置顶（默认 False：与普通窗口一样会被别的窗口盖住）
    scenes   场景快照（scene_id/name/home_id/home_name），与 devices 并列
    order    设备与场景的混排顺序，元素 {"type":"device|scene","id":...}
    locked   是否锁定（锁定后隐藏标题栏、禁止拖动、形态固定）
"""

import re

from app.core import _json_store

_VERSION = 1
_FILENAME = "board.json"

_OPACITY_MIN, _OPACITY_MAX = 0.30, 1.00
_COLUMN_MIN, _COLUMN_MAX = 1, 9
# 只接受 #rrggbb：写盘的值一律来自取色器或预设色板，格式不对按"跟随主题"处理
_BG_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")
# 场景快照字段：与 tray_store 保持一致，执行只认 scene_id/home_id
_SCENE_KEYS = ("scene_id", "name", "home_id", "home_name")


def _empty() -> dict:
    return {
        "version": _VERSION,
        # 桌面挂件总开关：开启即"常驻且启动时显示"，关闭则整块挂件不存在。
        # 它同时替代了旧版的两个字段（autostart / visible）：
        # 两者叠在一起时"勾了自动显示却关不掉"这类歧义都从这里来。
        "enabled": False,
        "devices": [],
        "columns": 3,
        "opacity": 0.92,
        "geom": None,   # [x, y, w, h]
        "collapsed": False,
        # None = 跟随主题（默认，与 0.3.1 之前的行为一致）
        "bg": None,
        # 默认**不置顶**：常驻置顶会把别的窗口挡住，用户要求可关
        "topmost": False,
        # 场景快照与混排顺序（旧数据没有这两个字段时按"只有设备"处理）
        "scenes": [],
        "order": [],
        # 锁定：隐藏标题栏 + 禁止拖动 + 形态固定，默认关
        "locked": False,
    }


def _read_raw() -> dict:
    raw = _json_store.read_json(_json_store.data_file(_FILENAME), _empty())
    if raw.get("version") != _VERSION:
        return _empty()
    devices = raw.get("devices")
    if not isinstance(devices, list) or not all(isinstance(d, str) for d in devices):
        return _empty()
    _migrate_legacy_flags(raw)
    for k, v in _empty().items():
        raw.setdefault(k, v)
    return raw


def _migrate_legacy_flags(raw: dict) -> None:
    """把旧版的 autostart / visible 折算成 enabled，并清掉旧键。

    旧语义：autostart=每次启动强制显示；visible=上次是否开着。两者任一为真
    都说明用户想要挂件，折算成 enabled=True；都没有则保持关闭（不会因为
    升级就凭空弹出一块面板）。
    """
    if "enabled" in raw:
        raw.pop("autostart", None)
        raw.pop("visible", None)
        return
    raw["enabled"] = bool(raw.get("autostart") or raw.get("visible"))
    raw.pop("autostart", None)
    raw.pop("visible", None)


def _write_raw(raw: dict) -> None:
    _json_store.write_json(_json_store.data_file(_FILENAME), raw)


def _clamp(value: float, lo: float, hi: float, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(v, lo), hi)


def load_devices() -> list[str]:
    """Board 设备 did 列表，按添加顺序。"""
    return list(_read_raw().get("devices", []))


def save_devices(dids: list[str]) -> None:
    raw = _read_raw()
    seen: set[str] = set()
    uniq: list[str] = []
    for d in dids:
        s = str(d)
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    raw["devices"] = uniq
    _write_raw(raw)


def get_columns() -> int:
    """单排最多放几张卡片，仅认 1–9，其余值回退 3。"""
    try:
        value = int(_read_raw().get("columns", 3))
    except (TypeError, ValueError):
        return 3
    return value if _COLUMN_MIN <= value <= _COLUMN_MAX else 3


def set_columns(value: int) -> None:
    raw = _read_raw()
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = 3
    raw["columns"] = min(max(v, _COLUMN_MIN), _COLUMN_MAX)
    _write_raw(raw)


def get_opacity() -> float:
    """窗口透明度，范围 0.30–1.00，越界钳制。"""
    return round(_clamp(_read_raw().get("opacity", 0.92), _OPACITY_MIN, _OPACITY_MAX, 0.92), 2)


def set_opacity(value: float) -> None:
    raw = _read_raw()
    raw["opacity"] = round(_clamp(value, _OPACITY_MIN, _OPACITY_MAX, 0.92), 2)
    _write_raw(raw)


def get_geometry() -> tuple[int, int, int, int] | None:
    """上次摆放的位置与尺寸；无记录或格式不对时返回 None。"""
    geom = _read_raw().get("geom")
    if not isinstance(geom, list) or len(geom) != 4:
        return None
    try:
        x, y, w, h = (int(v) for v in geom)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return x, y, w, h


def set_geometry(x: int, y: int, w: int, h: int) -> None:
    raw = _read_raw()
    raw["geom"] = [int(x), int(y), int(w), int(h)]
    _write_raw(raw)


def get_collapsed() -> bool:
    """是否处于折叠（细横条）状态。"""
    return bool(_read_raw().get("collapsed", False))


def set_collapsed(value: bool) -> None:
    raw = _read_raw()
    raw["collapsed"] = bool(value)
    _write_raw(raw)


def get_enabled() -> bool:
    """是否启用桌面挂件。

    开启 = 启动时自动显示并常驻（用户点 × 只是本次收起，下次启动仍会打开）；
    关闭 = 挂件完全不出现，主界面与托盘里的挂件入口一并隐藏。
    """
    return bool(_read_raw().get("enabled", False))


def set_enabled(value: bool) -> None:
    raw = _read_raw()
    raw["enabled"] = bool(value)
    _write_raw(raw)


def get_bg() -> str | None:
    """挂件背景色；None 表示跟随主题（默认）。

    只认 `#rrggbb`，非法值（手工改坏、旧版本遗留）一律当作跟随主题，
    不让一个坏字符串把挂件刷成透明或全黑。
    """
    value = _read_raw().get("bg")
    if isinstance(value, str) and _BG_PATTERN.match(value):
        return value.lower()
    return None


def set_bg(value: str | None) -> None:
    raw = _read_raw()
    if value is None:
        raw["bg"] = None
    elif isinstance(value, str) and _BG_PATTERN.match(value):
        raw["bg"] = value.lower()
    else:
        # 非法输入不写盘：宁可不改，也不要把配色改坏
        return
    _write_raw(raw)


def get_topmost() -> bool:
    """挂件是否置顶；默认 False（与普通窗口一致，会被其他窗口盖住）。"""
    return bool(_read_raw().get("topmost", False))


def set_topmost(value: bool) -> None:
    raw = _read_raw()
    raw["topmost"] = bool(value)
    _write_raw(raw)


# ---------- 场景 ----------

def load_scenes() -> list[dict]:
    """挂件上的场景快照；非法条目直接丢弃（与 tray_store 同款校验）。"""
    raw = _read_raw().get("scenes")
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if not str(item.get("scene_id") or ""):
            continue
        result.append({k: str(item.get(k) or "") for k in _SCENE_KEYS})
    return result


def save_scenes(scenes: list[dict]) -> None:
    raw = _read_raw()
    seen: set[str] = set()
    uniq: list[dict] = []
    for item in scenes:
        sid = str(item.get("scene_id") or "")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        uniq.append({k: str(item.get(k) or "") for k in _SCENE_KEYS})
    raw["scenes"] = uniq
    _write_raw(raw)


# ---------- 设备与场景的混排顺序 ----------

def load_order() -> list[dict]:
    """设备与场景混排后的展示顺序，元素 {"type","id"}。

    旧数据没有 order 字段时合成（设备在前、场景在后），这样升级后
    用户看到的内容与顺序和升级前一致，不需要重新排序。
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
    return ([{"type": "device", "id": d} for d in load_devices()] +
            [{"type": "scene", "id": s["scene_id"]} for s in load_scenes()])


def save_order(order: list[dict]) -> None:
    raw = _read_raw()
    seen: set[tuple[str, str]] = set()
    uniq: list[dict] = []
    for item in order:
        if not isinstance(item, dict):
            continue
        t = str(item.get("type") or "")
        i = str(item.get("id") or "")
        if t not in ("device", "scene") or not i or (t, i) in seen:
            continue
        seen.add((t, i))
        uniq.append({"type": t, "id": i})
    raw["order"] = uniq
    _write_raw(raw)


# ---------- 锁定 ----------

def get_locked() -> bool:
    """挂件是否锁定（隐藏标题栏、禁止拖动、形态固定）；默认 False。"""
    return bool(_read_raw().get("locked", False))


def set_locked(value: bool) -> None:
    raw = _read_raw()
    raw["locked"] = bool(value)
    _write_raw(raw)