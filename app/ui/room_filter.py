# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""挂件与托盘管理对话框共用的房间筛选下拉框。

两处候选列表列的都是全量设备，跨家庭混在一起，所以选项文字带上家庭名
前缀：两个家庭各有一间「卧室」时，只写房间名无法分辨是哪一间。

筛选只作用于设备列表。场景不属于任何房间，调用方在场景页把下拉框收起
即可，不要让它挡住场景搜索。
"""

from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import QComboBox

from app.core.models import DeviceInfo
from app.ui.si_theme import SiColors, apply_combo_qss, themed_combo

ALL_ROOMS_TEXT = "全部房间"
# 设备没有房间归属时数据层兜底成这个值，与主界面房间选项卡口径一致：
# 它不是一个"房间"，不列进可筛选项
_UNKNOWN_ROOM = "未知"

# 下拉框与搜索框同高，二者并排成一行
_COMBO_HEIGHT = 28
# 宽度贴当前选中项的文字，只保留下限保证可点、上限给搜索框留位置
_COMBO_MIN_WIDTH = 100
_COMBO_MAX_WIDTH = 176
# 左右内边距 + 右侧自绘下拉箭头的占位，估算宽度时补上
_COMBO_CHROME = 46

RoomKey = tuple[str, str]


def room_key(device: DeviceInfo) -> RoomKey:
    """设备所属的「家庭 + 房间」，作为下拉项的匹配键。

    用元组而不是拼字符串：家庭成员名里带分隔符时拼串会撞键。
    """
    return ((device.home_name or "").strip(), (device.room_name or "").strip())


def room_label(home_name: str, room_name: str) -> str:
    """下拉项显示文本：有家庭名就带上，家庭未知时只显示房间名。"""
    home = (home_name or "").strip()
    room = (room_name or "").strip()
    if not home or home == _UNKNOWN_ROOM:
        return room
    return f"{home} · {room}"


def room_options(devices: list[DeviceInfo]) -> list[tuple[str, RoomKey]]:
    """按设备清单聚合房间选项，返回 [(显示文本, 匹配键)]，按文本排序。

    同一房间只出现一次；未知房间的设备只在「全部房间」下可见，
    与主界面房间选项卡的处理保持一致。
    """
    labels: dict[RoomKey, str] = {}
    for dev in devices:
        key = room_key(dev)
        if not key[1] or key[1] == _UNKNOWN_ROOM:
            continue
        labels.setdefault(key, room_label(*key))
    return sorted(((label, key) for key, label in labels.items()),
                  key=lambda item: item[0])


# 9pt 正文的度量器建一次就够：切换房间、重建选项时会反复量文本，
# 每次新建一份 QFont 是白白开销
_METRICS: QFontMetrics | None = None


def _metrics() -> QFontMetrics:
    global _METRICS
    if _METRICS is None:
        font = QFont("Microsoft YaHei UI")
        font.setPointSize(9)
        _METRICS = QFontMetrics(font)
    return _METRICS


def _text_width(text: str) -> int:
    """量一段文字在 9pt 下的像素宽度。

    QSS 的 font-size 不影响 fontMetrics，所以不能直接问控件，要按正文
    磅数单独取一份字体来量。个别环境里字体度量会偏小甚至归零（精简
    字体库、图标字体注册后引擎重建），所以再按全角/半角字宽估一份，
    两者取大：宁可宽几个像素，也不能窄到把文字裁掉。
    """
    measured = _metrics().horizontalAdvance(text)
    # 全角按 1em、半角按半 em 粗估，作为异常环境下的下限
    estimate = int(round(sum(12.0 if ord(ch) > 0x2E7F else 6.5 for ch in text)))
    return max(measured, estimate)


def _clamp_width(text_width: int) -> int:
    return max(_COMBO_MIN_WIDTH, min(_COMBO_MAX_WIDTH, text_width + _COMBO_CHROME))


def _fit_to_current(combo: QComboBox) -> None:
    """宽度贴着当前选中项的文字。

    按最长选项定宽会让「全部房间」这类短文本右侧空出一大片，看着像
    控件坏了；房间名长时又撑宽搜索框。所以宽度只跟当前显示的这一条走。
    """
    width = _clamp_width(_text_width(combo.currentText()))
    # 宽度没变就别碰 setFixedWidth：同样会触发一次布局失效，
    # 键盘上下切换选项时会连带整行重排
    if width == combo.minimumWidth() == combo.maximumWidth():
        return
    combo.setFixedWidth(width)


def _fit_popup_to_widest(combo: QComboBox) -> None:
    """弹层至少容得下最长的选项。

    本体只贴当前文字，弹层不能跟着变窄，否则选「全部房间」时展开的
    列表会把长房间名截掉。Qt 默认就按内容撑宽弹层，这里再设一道最小
    宽度，防止字体度量异常时弹层退化成与本体同宽。
    """
    widest = max((_text_width(combo.itemText(i))
                  for i in range(combo.count())), default=0)
    combo.view().setMinimumWidth(_clamp_width(widest))


def make_room_combo() -> QComboBox:
    """房间下拉框：先只有「全部房间」，选项由 refresh_room_combo 填充。"""
    combo = themed_combo([ALL_ROOMS_TEXT])
    combo.setFixedHeight(_COMBO_HEIGHT)
    _fit_to_current(combo)
    combo.currentIndexChanged.connect(lambda _index: _fit_to_current(combo))
    combo.setToolTip("按房间筛选下方设备")
    return combo


def refresh_room_combo(combo: QComboBox, devices: list[DeviceInfo]) -> bool:
    """按设备清单重建选项并尽量保留当前选中，返回是否还有可选房间。

    设备刷新（重新登录、换家庭）时房间可能整体变化，选中项消失就退回
    「全部房间」，不能停在一个不存在的房间上把列表清空。
    """
    options = room_options(devices)
    keep = combo.currentData()
    combo.blockSignals(True)
    try:
        combo.clear()
        # 首项固定是「全部房间」，其 data 为 None 表示不筛选
        combo.addItem(ALL_ROOMS_TEXT, None)
        for label, key in options:
            combo.addItem(label, key)
        index = 0
        if keep is not None:
            for i in range(1, combo.count()):
                if combo.itemData(i) == keep:
                    index = i
                    break
        combo.setCurrentIndex(index)
    finally:
        combo.blockSignals(False)
    _fit_to_current(combo)
    _fit_popup_to_widest(combo)
    return bool(options)


def apply_room_combo_theme(combo: QComboBox) -> None:
    """主题切换后重设配色：下拉框的内联样式是构造期求值的。"""
    apply_combo_qss(combo)
    set_arrow = getattr(combo, "set_arrow_color", None)
    if set_arrow is not None:
        set_arrow(SiColors.TEXT_SECONDARY)
