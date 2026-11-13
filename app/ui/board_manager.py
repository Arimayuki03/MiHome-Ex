# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""桌面挂件的管理对话框：勾选设备/场景 + 列数 / 透明度 / 背景色 / 置顶。

设备与场景**分页展示**（与托盘管理一致）：设备页只列设备，场景页只列场景，
不再混在一张长列表里靠标签区分。设备页顶部按关键字与房间两个条件筛选，
房间选项带家庭名前缀以区分不同家庭的同名房间。

本窗只决定"挂件上放哪些"，**不提供排序**：顺序直接在挂件面板上按住卡片
拖动调整。两个地方都能排序必然互相覆盖，留一个入口最不容易出错。

透明度、列数、背景色、置顶都是**拖动/点击即生效**（所见即所得），点「取消」
也会保留这些调整；点「保存」只决定设备/场景清单是否写入。
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QColorDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.core import board_store
from app.core.models import DeviceInfo
from app.ui.check_box import SiCheckBox
from app.ui.overlay_dialog import OverlayDialog
from app.ui.room_filter import (
    apply_room_combo_theme,
    make_room_combo,
    refresh_room_combo,
    room_key,
)
from app.ui.si_theme import (
    SiColors,
    check_toggle_qss,
    primary_button_qss,
    secondary_button_qss,
)

_COL_MIN, _COL_MAX = 1, 9
_OPACITY_MIN, _OPACITY_MAX = 30, 100  # 滑块刻度用整数百分比

# 预设底色：黑 / 深灰 / 石墨 / 白 / 米白 / 墨蓝。
# 前三个是"常驻桌面不刺眼"的主流选择，白色给浅色桌面，米白/墨蓝给配色党。
_PRESET_BG = (
    ("纯黑", "#000000"),
    ("深夜灰", "#1e1f22"),
    ("石墨", "#2c2e33"),
    ("墨蓝", "#16202e"),
    ("白", "#ffffff"),
    ("米白", "#f5f1e8"),
)
_SWATCH = 26  # 色块边长


class BoardManagerDialog(OverlayDialog):
    """挂件设备管理：勾选 + 拖拽排序 + 每行列数 + 透明度 + 背景色 + 置顶。"""

    # 实时预览：拖动/点击过程中直接改 Board 窗口
    opacity_preview = Signal(float)
    columns_preview = Signal(int)
    # None = 跟随主题；其它为 "#rrggbb"
    background_preview = Signal(object)
    topmost_preview = Signal(bool)
    lock_preview = Signal(bool)

    def __init__(self, devices: list[DeviceInfo], parent=None, scenes=None):
        # 与托盘管理同款观感：铺满窗口的可拖拽面板，无遮罩
        super().__init__(parent, overlay=False)
        self.setWindowTitle("管理桌面挂件")
        # 当前选中的底色：None=跟随主题。色块与取色器共用这一个状态
        self._bg: str | None = board_store.get_bg()
        # 可添加的场景：调用方传入（主窗口缓存/在线拉的）；None 表示暂无
        self._scenes_available: list[dict] = list(scenes or [])
        # 场景行在 _rows/_checks 里的 key 前缀，避免与设备 did 冲突
        self._SCENE_PREFIX = "scene:"
        # 先建空的勾选/行表：列数分区在设备列表之前构建，其提示文案
        # 需要按已勾选数量计算，构造期必须能安全查空表
        self._checks: dict[str, QPushButton] = {}
        self._rows: dict[str, QWidget] = {}
        # 两页各自的展示顺序（设备 did / scene:<sid>），保存时按它输出
        self._device_keys: list[str] = []
        self._scene_keys: list[str] = []
        self._scene_tags: list[QLabel] = []
        # 打开时的混排顺序：保存时保留用户已在挂件上拖好的相对顺序，
        # 新勾选的追加到末尾（本窗不再排序，避免覆盖面板上的拖拽结果）
        self._initial_order: list[dict] = board_store.load_order()

        lay = QVBoxLayout(self._panel)
        lay.setContentsMargins(16, 12, 16, 16)
        lay.setSpacing(12)

        # ---- 标题栏（整条可拖动窗口） ----
        title_bar = QFrame(self._panel)
        title_bar.setObjectName("boardTitleBar")
        title_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        title_bar.setStyleSheet("QFrame#boardTitleBar { background: transparent; }")
        title_bar.setCursor(Qt.CursorShape.OpenHandCursor)
        self._title_bar = title_bar
        self._header_drag_pos = None
        title_bar.installEventFilter(self)
        header = QHBoxLayout(title_bar)
        header.setContentsMargins(4, 2, 4, 2)
        header.setSpacing(8)
        header_title = QLabel("管理桌面挂件")
        header_title.setFont(QFont("Microsoft YaHei UI", 12, QFont.Weight.DemiBold))
        header_title.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")
        header.addWidget(header_title)
        header.addStretch(1)
        lay.addWidget(title_bar)

        # ---- 三个页签：设备 / 场景 / 外观 ----
        # 设备与场景分开列（与托盘管理一致）：混在一张长列表里，用户很难
        # 一眼看出"我到底放了哪些"。外观页只放即时生效的样式项。
        lay.addWidget(self._make_tab_bar())

        self._stack = QStackedWidget()
        self._stack.addWidget(self._make_device_page())
        self._stack.addWidget(self._make_scene_page())
        self._stack.addWidget(self._make_style_page())
        lay.addWidget(self._stack, stretch=1)
        self._set_page(0)

        self._fill_device_rows(devices)
        self._fill_scene_rows()
        # 行已建好，重算列数提示（构造期构建列数分区时列表还是空的）
        self._refresh_columns_ui()

        lay.addWidget(self._make_button_row())

    # ---------- 页签与两个页面 ----------

    def _make_tab_bar(self) -> QWidget:
        """设备 / 场景 在左，「外观」靠右，按钮尺寸与托盘管理一致。"""
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self._tab_buttons: list[QPushButton] = []
        for text in ("设备", "场景"):
            btn = self._make_tab_button(text)
            self._tab_buttons.append(btn)
            row.addWidget(btn)
        row.addStretch(1)
        # 外观是低频设置项：跟设备/场景这种"列表页"分开，靠在最右侧
        style_btn = self._make_tab_button("外观")
        self._tab_buttons.append(style_btn)
        row.addWidget(style_btn)
        for idx, btn in enumerate(self._tab_buttons):
            btn.clicked.connect(lambda _c=False, i=idx: self._set_page(i))
        # 页面在 _stack 建好后才切换（_set_page 要用到 _stack）
        return holder

    def _make_tab_button(self, text: str) -> QPushButton:
        """分段按钮：与 TrayManagerDialog 的页签同一套尺寸与配色。"""
        btn = QPushButton(text)
        btn.setCheckable(True)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedHeight(30)
        btn.setStyleSheet(self._tab_qss(False))
        return btn

    def _set_page(self, index: int) -> None:
        index = 0 if index < 0 else min(index, len(self._tab_buttons) - 1)
        for i, btn in enumerate(self._tab_buttons):
            btn.setChecked(i == index)
            btn.setStyleSheet(self._tab_qss(i == index))
        self._stack.setCurrentIndex(index)

    def _tab_qss(self, active: bool) -> str:
        """与托盘管理的页签同一套观感（紧凑、不拉伸）。"""
        if active:
            return (
                f"QPushButton {{ background: {SiColors.THEME}; border: 1px solid {SiColors.THEME};"
                f" border-radius: 8px; padding: 0 18px; color: {SiColors.ON_THEME_TEXT};"
                " font-weight: 600; }")
        return (
            f"QPushButton {{ background: {SiColors.CTRL_BG};"
            f" border: 1px solid {SiColors.CTRL_BORDER};"
            f" border-radius: 8px; padding: 0 18px; color: {SiColors.TEXT_PRIMARY};"
            " font-weight: 600; }"
            f"QPushButton:hover {{ background: {SiColors.CTRL_BG_HOVER}; }}")

    def _make_device_page(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)

        self._subtitle = QLabel(
            "勾选设备加入挂件；顺序可直接在挂件面板上按住卡片拖动调整")
        self._subtitle.setWordWrap(True)
        self._subtitle.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY}; background: transparent; font-size: 9pt;")
        box.addWidget(self._subtitle)

        self._search = QLineEdit()
        self._search.setPlaceholderText("搜索设备 / 房间…")
        self._search.setClearButtonEnabled(True)
        self._search.setFixedHeight(28)
        self._search.setStyleSheet(self._search_qss())
        self._search.textChanged.connect(self._apply_device_filter)
        # 搜索行右侧放房间下拉：两台名字相近的设备分属不同房间时，
        # 光靠关键字还得从名字里认房间，按房间收窄比反复改关键字快
        self._room_combo = make_room_combo()
        self._room_combo.currentIndexChanged.connect(
            lambda _index: self._apply_device_filter(self._search.text()))
        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(8)
        search_row.addWidget(self._search, stretch=1)
        search_row.addWidget(self._room_combo)
        box.addLayout(search_row)

        scroll, host = self._make_list_scroll()
        self._host = host
        self._list_lay = QVBoxLayout(host)
        # 右侧留 10px：卡片直接贴到滚动条上会显得挤（用户实测反馈），
        # 与托盘管理的间距保持一致
        self._list_lay.setContentsMargins(0, 0, 10, 0)
        self._list_lay.setSpacing(8)
        box.addWidget(scroll, stretch=1)
        self._scroll = scroll
        return page

    def _make_scene_page(self) -> QWidget:
        """场景页：与设备页同构，只列场景。"""
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)

        self._scene_subtitle = QLabel(
            "勾选场景加入挂件；顺序与设备一致，直接在工作台面板上拖动卡片调整")
        self._scene_subtitle.setWordWrap(True)
        self._scene_subtitle.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY}; background: transparent; font-size: 9pt;")
        box.addWidget(self._scene_subtitle)

        self._scene_search = QLineEdit()
        self._scene_search.setPlaceholderText("搜索场景 / 家庭…")
        self._scene_search.setClearButtonEnabled(True)
        self._scene_search.setFixedHeight(28)
        self._scene_search.setStyleSheet(self._search_qss())
        self._scene_search.textChanged.connect(self._apply_scene_filter)
        box.addWidget(self._scene_search)

        scroll, host = self._make_list_scroll()
        self._scene_host = host
        self._scene_list_lay = QVBoxLayout(host)
        self._scene_list_lay.setContentsMargins(0, 0, 10, 0)
        self._scene_list_lay.setSpacing(8)
        box.addWidget(scroll, stretch=1)
        self._scene_scroll = scroll
        return page

    @staticmethod
    def _make_list_scroll() -> tuple[QScrollArea, QWidget]:
        """列表滚动区：无边框、无横向滚动条（行按视口宽度铺满）。"""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        host = QWidget()
        host.setStyleSheet("background: transparent;")
        scroll.setWidget(host)
        return scroll, host

    def _make_style_page(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)
        box.addWidget(self._make_columns_section())
        box.addWidget(self._make_opacity_section())
        box.addWidget(self._make_background_section())
        box.addWidget(self._make_topmost_section())
        box.addWidget(self._make_lock_section())
        # 本页所有改动都即时生效，容易让人以为要按「保存」——
        # 一句话说清「保存」只管设备清单
        self._style_hint = QLabel("以上改动点击即生效，无需点「保存」；「保存」只保存挂件内容")
        self._style_hint.setWordWrap(True)
        self._style_hint.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY}; background: transparent; font-size: 8pt;")
        box.addWidget(self._style_hint)
        box.addStretch(1)
        return page

    @staticmethod
    def _search_qss() -> str:
        return (
            f"QLineEdit {{ background: {SiColors.SURFACE}; border: 1px solid {SiColors.LINE};"
            f" border-radius: 8px; padding: 2px 10px; color: {SiColors.TEXT_PRIMARY};"
            " font-size: 9pt; }"
            f"QLineEdit:focus {{ border-color: {SiColors.THEME}; }}")

    # ---------- 列表构建 ----------

    def _fill_device_rows(self, devices: list[DeviceInfo]) -> None:
        """填充设备页：已选在前（按挂件当前顺序），其余按"在线优先+名称"排。"""
        selected = board_store.load_devices()
        dev_map = {d.did: d for d in devices}
        placed: list[str] = []
        for entry in self._initial_order:
            if entry.get("type") != "device":
                continue
            did = entry.get("id")
            if did in selected and did in dev_map and did not in placed:
                placed.append(did)
        # 顺序表遗漏的已选设备（旧数据兜底），离线也保留：
        # 否则保存时会被静默丢掉
        for did in selected:
            if did in dev_map and did not in placed:
                placed.append(did)
        for did in placed:
            self._add_row(dev_map[did], True)
        # 未选设备全部列出（含离线，名字标灰）：离线设备也可能要放进挂件
        # 长期摆放，藏起来只会让用户以为"设备丢了"。在线优先、同状态按名排
        for dev in sorted((d for d in devices if d.did not in selected),
                          key=lambda d: (0 if d.online else 1, d.name)):
            self._add_row(dev, False)
        self._list_lay.addStretch(1)
        # 房间下拉的选项来自实际设备：一台都没有房间归属时整块收起，
        # 把整行宽度让给搜索框
        has_rooms = refresh_room_combo(self._room_combo, devices)
        self._room_combo.setVisible(has_rooms)

    def _fill_scene_rows(self) -> None:
        """填充场景页：已选（含快照）在前，其余可用场景按名排后。"""
        selected = board_store.load_scenes()
        placed: set[str] = set()
        by_id = {s["scene_id"]: s for s in self._scenes_available}
        for entry in self._initial_order:
            if entry.get("type") != "scene":
                continue
            sid = entry.get("id")
            if sid in placed:
                continue
            snap = next((s for s in selected if s["scene_id"] == sid), None)
            scene = by_id.get(sid) or snap
            if scene is None:
                continue
            self._add_scene_row(scene, True)
            placed.add(sid)
        # 已选但顺序表里没有的场景（旧数据兜底）
        for snap in selected:
            if snap["scene_id"] not in placed:
                self._add_scene_row(by_id.get(snap["scene_id"], snap), True)
                placed.add(snap["scene_id"])
        for scene in sorted(self._scenes_available, key=lambda s: s.get("name", "")):
            if scene.get("scene_id") and scene["scene_id"] not in placed:
                self._add_scene_row(scene, False)
                placed.add(scene["scene_id"])
        self._scene_list_lay.addStretch(1)

    def _make_row_base(self) -> tuple[QFrame, QHBoxLayout]:
        """两页共用的行外壳：撑满视口宽度，右侧只留一点点边距。"""
        row = QFrame()
        row.setObjectName("boardRow")
        row.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row.setFixedHeight(48)
        # 之前这里是定宽 290 + 右侧 16px 内边距：面板比它宽时，
        # 卡片右边缘到滚动条之间会空出一大条（用户实测反馈太远）
        row.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._reset_row_style(row)
        rl = QHBoxLayout(row)
        rl.setContentsMargins(10, 8, 10, 8)
        return row, rl

    def _make_check_button(self, key: str, checked: bool) -> QPushButton:
        btn = QPushButton("✓" if checked else "+")
        btn.setFixedSize(28, 28)
        btn.setCheckable(True)
        btn.setChecked(checked)
        # 文字必须随勾选态同步：QSS 只管背景色，固定文字会绿底加号/暗底对勾
        btn.toggled.connect(lambda c, b=btn: b.setText("✓" if c else "+"))
        # 勾选数量变了，「N 项 → M 行」的提示要跟着变
        btn.toggled.connect(lambda _c: self._refresh_columns_ui())
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(check_toggle_qss(28))
        self._checks[key] = btn
        return btn

    def _add_row(self, dev: DeviceInfo, checked: bool) -> None:
        row, rl = self._make_row_base()
        name = QLabel(f"{dev.name}  ·  {dev.room_name}")
        name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        name.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY if dev.online else SiColors.OFFLINE_TEXT};"
            " background: transparent;")
        rl.addWidget(name, stretch=1)
        rl.addWidget(self._make_check_button(dev.did, checked))
        self._rows[dev.did] = row
        self._device_keys.append(dev.did)
        # 搜索匹配用：设备名 + 房间名，大小写不敏感
        row._search_text = f"{dev.name} {dev.room_name}".lower()
        # 房间筛选用：与下拉项的 item data 同一种键（家庭 + 房间）
        row._room_key = room_key(dev)
        # 已选但因离线只出现在列表里的设备，标出来免得以为它在线
        row._is_offline = not dev.online
        self._list_lay.addWidget(row)

    def _add_scene_row(self, scene: dict, checked: bool) -> None:
        """场景行：与设备行同构，标题后带「场景」标记，key 用 scene: 前缀。"""
        sid = str(scene.get("scene_id") or "")
        key = self._SCENE_PREFIX + sid
        row, rl = self._make_row_base()
        home = str(scene.get("home_name") or "")
        label = QLabel(f"{scene.get('name') or '场景'}  ·  {home or '手动场景'}")
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        label.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")
        rl.addWidget(label, stretch=1)
        tag = QLabel("场景")
        tag.setStyleSheet(
            f"color: {SiColors.THEME}; background: transparent;"
            " border: 1px solid " + SiColors.THEME + "; border-radius: 6px;"
            " padding: 0 4px; font-size: 8pt;")
        rl.addWidget(tag)
        self._scene_tags.append(tag)
        rl.addWidget(self._make_check_button(key, checked))
        self._rows[key] = row
        self._scene_keys.append(sid)
        row._search_text = f"{scene.get('name') or ''} {home} 场景".lower()
        row._is_offline = False
        row._is_scene = True
        row._scene = scene
        self._scene_list_lay.addWidget(row)

    def _apply_device_filter(self, text: str) -> None:
        """设备页：关键字与房间两个条件取交集，只隐藏行、不动勾选状态。"""
        self._filter_rows(self._device_keys, text, use_room=True)

    def _apply_scene_filter(self, text: str) -> None:
        """场景页搜索：同样只隐藏行；场景没有房间归属，不按房间过滤。"""
        self._filter_rows(self._scene_keys, text, use_room=False)

    def _filter_rows(self, keys: list[str], text: str,
                     *, use_room: bool) -> None:
        keyword = (text or "").strip().lower()
        room = self._room_combo.currentData() if use_room else None
        for key in keys:
            row = self._rows.get(key)
            if row is None:
                continue
            visible = not keyword or keyword in getattr(row, "_search_text", "")
            if visible and room is not None:
                visible = getattr(row, "_room_key", None) == room
            row.setVisible(visible)

    # ---------- 列数与透明度 ----------

    def _make_columns_section(self) -> QWidget:
        """单排最多几张卡片：分段按钮组（1–9），选中即预览。"""
        box = QFrame()
        box.setObjectName("boardSection")
        box.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        box.setStyleSheet(
            f"QFrame#boardSection {{ background: {SiColors.CARD};"
            f" border: 1px solid {SiColors.LINE}; border-radius: 10px; }}")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(12, 8, 12, 10)
        lay.setSpacing(6)
        # 标题行右侧放「N 项 → M 行」：按钮行占满宽度，提示放同一行会挤断
        title_row = QHBoxLayout()
        title_row.setSpacing(6)
        title = QLabel("单排最多设备卡片数")
        title.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent; font-size: 9pt;")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self._columns_hint = QLabel()
        self._columns_hint.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY}; background: transparent; font-size: 8pt;")
        title_row.addWidget(self._columns_hint)
        lay.addLayout(title_row)
        row = QHBoxLayout()
        # 9 个按钮要塞进 304px 的可用宽：28px 按钮 + 3px 间距 = 276px
        row.setSpacing(3)
        self._col_buttons: list[QPushButton] = []
        self._columns = board_store.get_columns()
        for n in range(_COL_MIN, _COL_MAX + 1):
            btn = QPushButton(str(n))
            btn.setFixedSize(28, 26)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, v=n: self._set_columns(v))
            self._col_buttons.append(btn)
            row.addWidget(btn)
        row.addStretch(1)
        lay.addLayout(row)
        self._refresh_columns_ui()
        return box

    def _set_columns(self, value: int) -> None:
        self._columns = value
        board_store.set_columns(value)
        self._refresh_columns_ui()
        self.columns_preview.emit(self._columns)

    def _refresh_columns_ui(self) -> None:
        # 注意：非 f-string 的字面段里不能写 {{ }}，否则样式表括号不配平
        active = (
            f"QPushButton {{ background: {SiColors.THEME};"
            f" border: 1px solid {SiColors.THEME}; border-radius: 8px;"
            f" color: {SiColors.ON_THEME_TEXT}; font-weight: 600; }}")
        idle = (
            f"QPushButton {{ background: {SiColors.CTRL_BG};"
            f" border: 1px solid {SiColors.CTRL_BORDER}; border-radius: 8px;"
            f" color: {SiColors.TEXT_PRIMARY}; }}"
            f"QPushButton:hover {{ background: {SiColors.CTRL_BG_HOVER}; }}")
        for i, btn in enumerate(self._col_buttons):
            btn.setStyleSheet(active if i + _COL_MIN == self._columns else idle)
        n = len(self.selected_dids()) + len(self.selected_scenes())
        if n == 0:
            self._columns_hint.setText("尚未添加内容")
        else:
            rows = -(-n // max(1, self._columns))
            self._columns_hint.setText(f"{n} 项 → {rows} 行")

    def _make_opacity_section(self) -> QWidget:
        """透明度：滑块拖动即实时改 Board 窗口（所见即所得）。"""
        box = QFrame()
        box.setObjectName("boardSection")
        box.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        box.setStyleSheet(
            f"QFrame#boardSection {{ background: {SiColors.CARD};"
            f" border: 1px solid {SiColors.LINE}; border-radius: 10px; }}")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(12, 8, 12, 10)
        lay.setSpacing(6)
        head = QHBoxLayout()
        title = QLabel("透明度")
        title.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent; font-size: 9pt;")
        head.addWidget(title)
        self._opacity_hint = QLabel()
        self._opacity_hint.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY}; background: transparent; font-size: 8pt;")
        head.addStretch(1)
        head.addWidget(self._opacity_hint)
        lay.addLayout(head)
        self._opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self._opacity_slider.setRange(_OPACITY_MIN, _OPACITY_MAX)
        value = board_store.get_opacity()
        self._opacity_slider.setValue(round(value * 100))
        self._opacity_slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self._opacity_slider.valueChanged.connect(self._on_opacity_changed)
        lay.addWidget(self._opacity_slider)
        self._refresh_opacity_hint(self._opacity_slider.value())
        return box

    def _on_opacity_changed(self, value: int) -> None:
        self._refresh_opacity_hint(value)
        # 落盘由对话框负责（与列数一致），预览信号只驱动挂件窗口，
        # 这样即使挂件创建失败/已隐藏，透明度的设置也不会丢
        board_store.set_opacity(value / 100.0)
        self.opacity_preview.emit(value / 100.0)

    def _refresh_opacity_hint(self, value: int) -> None:
        self._opacity_hint.setText(
            f"{value}%　鼠标移上去自动恢复不透明" if value < 100 else "100%")

    def opacity(self) -> float:
        return self._opacity_slider.value() / 100.0

    def columns(self) -> int:
        return self._columns

    # ---------- 背景色 ----------

    def _make_section_box(self) -> tuple[QFrame, QVBoxLayout]:
        """分区外框（与列数/透明度两个分区同款），返回框与内部布局。"""
        box = QFrame()
        box.setObjectName("boardSection")
        box.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        box.setStyleSheet(
            f"QFrame#boardSection {{ background: {SiColors.CARD};"
            f" border: 1px solid {SiColors.LINE}; border-radius: 10px; }}")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(12, 8, 12, 10)
        lay.setSpacing(6)
        return box, lay

    def _make_background_section(self) -> QWidget:
        """背景色：「背景色」单独一行做标题（右侧放当前值提示），
        下一行排「跟随主题 / 自定义…」和预设色块，视觉层级更清楚。
        """
        box, lay = self._make_section_box()

        # 标题行：标题单独一行，右端是当前取值提示（跟随主题 / 色号）
        title_row = QHBoxLayout()
        title_row.setSpacing(6)
        title = QLabel("背景色")
        title.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent; font-size: 9pt;")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self._bg_hint = QLabel()
        self._bg_hint.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY}; background: transparent; font-size: 8pt;")
        title_row.addWidget(self._bg_hint)
        lay.addLayout(title_row)

        # 控件行 1：跟随主题 / 自定义
        self._bg_chips: list[QPushButton] = []
        # 索引 0 固定是「跟随主题」：refresh 时按它判断当前是否为自动模式
        auto = self._make_text_chip("跟随主题", "深浅色主题切换时挂件自动适配")
        auto.clicked.connect(lambda: self._choose_bg(None))
        self._bg_chips.append(auto)
        custom = self._make_text_chip("自定义颜色", "打开取色器，选任意颜色")
        custom.clicked.connect(self._choose_custom_bg)
        mode_row = QHBoxLayout()
        mode_row.setSpacing(6)
        mode_row.addWidget(auto)
        mode_row.addWidget(custom)
        mode_row.addStretch(1)
        lay.addLayout(mode_row)

        # 控件行 2：预设色块（单独一行，挤在一起会被裁切）
        row = QHBoxLayout()
        row.setSpacing(6)
        for label, color in _PRESET_BG:
            chip = self._make_color_chip(color, label)
            chip.clicked.connect(lambda _c=False, v=color: self._choose_bg(v))
            row.addWidget(chip)
            self._bg_chips.append(chip)
        row.addStretch(1)
        lay.addLayout(row)
        self._refresh_bg_ui()
        return box

    def _make_color_chip(self, color: str, tip: str) -> QPushButton:
        chip = QPushButton()
        chip.setCheckable(True)
        chip.setFixedSize(_SWATCH, _SWATCH)
        chip.setToolTip(tip)
        chip.setCursor(Qt.CursorShape.PointingHandCursor)
        chip.setStyleSheet(
            f"QPushButton {{ background: {color}; border: 2px solid {SiColors.LINE};"
            " border-radius: 13px; }"
            f"QPushButton:checked {{ border: 2px solid {SiColors.THEME}; }}")
        return chip

    def _make_text_chip(self, text: str, tip: str) -> QPushButton:
        chip = QPushButton(text)
        chip.setCheckable(True)
        chip.setFixedHeight(_SWATCH)
        chip.setToolTip(tip)
        chip.setCursor(Qt.CursorShape.PointingHandCursor)
        chip.setStyleSheet(self._text_chip_qss())
        return chip

    @staticmethod
    def _text_chip_qss() -> str:
        return (
            f"QPushButton {{ background: {SiColors.SURFACE}; border: 2px solid transparent;"
            " border-radius: 13px; padding: 0 10px; font-size: 8pt;"
            f" color: {SiColors.TEXT_PRIMARY}; }}"
            f"QPushButton:hover {{ background: {SiColors.BTN_HOVER}; }}"
            f"QPushButton:checked {{ border: 2px solid {SiColors.THEME}; }}")

    def _choose_bg(self, color: str | None) -> None:
        """选中某个底色：立刻落盘并预览到真实挂件窗口。"""
        self._bg = color
        board_store.set_bg(color)
        self._refresh_bg_ui()
        self.background_preview.emit(color)

    def _choose_custom_bg(self) -> None:
        initial = QColor(self._bg) if self._bg else QColor(SiColors.WINDOW_BG)
        picked = QColorDialog.getColor(initial, self, "选择挂件背景色")
        if picked.isValid():
            self._choose_bg(picked.name())

    def _refresh_bg_ui(self) -> None:
        # 自动 + 预设色块按当前值勾选；自定义颜色时所有色块都不选中，
        # 提示里显示具体色号（用户自己挑的颜色不在预设里）
        current = (self._bg or "").lower()
        for chip in self._bg_chips:
            chip.setChecked(False)
        if self._bg is None:
            self._bg_chips[0].setChecked(True)
            self._bg_hint.setText("跟随主题")
        else:
            for idx, (_label, color) in enumerate(_PRESET_BG, start=1):
                if color.lower() == current:
                    self._bg_chips[idx].setChecked(True)
                    break
            self._bg_hint.setText(current)

    # ---------- 窗口置顶 ----------

    def _make_topmost_section(self) -> QWidget:
        """置顶勾选框：不写解释文字（用户都懂"置顶"），说明放进 tooltip。"""
        box, lay = self._make_section_box()
        self._topmost_check = SiCheckBox("窗口置顶")
        self._topmost_check.setChecked(board_store.get_topmost())
        self._topmost_check.setToolTip(
            "关闭（默认）：会被其他窗口盖住；开启：始终浮在最上层")
        self._topmost_check.toggled.connect(self._on_topmost_toggled)
        lay.addWidget(self._topmost_check)
        return box

    def _make_lock_section(self) -> QWidget:
        """锁定：隐藏标题栏、禁止拖动、形态固定（解锁入口在挂件上/右键/托盘）。"""
        box, lay = self._make_section_box()
        self._lock_check = SiCheckBox("锁定挂件（隐藏标题栏、固定位置）")
        self._lock_check.setChecked(board_store.get_locked())
        self._lock_check.setToolTip(
            "锁定后不能拖动、不能收起/展开；鼠标移到挂件上会浮出小锁，"
            "右键菜单与托盘菜单里也能解锁")
        self._lock_check.toggled.connect(self._on_lock_toggled)
        lay.addWidget(self._lock_check)
        return box

    def _on_lock_toggled(self, checked: bool) -> None:
        board_store.set_locked(checked)
        self.lock_preview.emit(bool(checked))

    def locked(self) -> bool:
        return self._lock_check.isChecked()

    def _on_topmost_toggled(self, checked: bool) -> None:
        board_store.set_topmost(checked)
        self.topmost_preview.emit(bool(checked))

    def topmost(self) -> bool:
        return self._topmost_check.isChecked()

    # ---------- 底部按钮 ----------

    @staticmethod
    def _plain_btn_qss(primary: bool) -> str:
        # 主按钮文字固定白色（保存/确定这类"内容文字"不随主题翻转）
        if primary:
            return primary_button_qss()
        return secondary_button_qss()

    def _make_button_row(self) -> QWidget:
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setStyleSheet(self._plain_btn_qss(False))
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        ok = QPushButton("保存")
        ok.setCursor(Qt.CursorShape.PointingHandCursor)
        ok.setStyleSheet(self._plain_btn_qss(True))
        ok.clicked.connect(self.accept)
        row.addWidget(ok)
        self._cancel_btn = cancel
        self._ok_btn = ok
        holder = QWidget()
        holder.setLayout(row)
        return holder

    # ---------- 行样式与结果 ----------

    def _reset_row_style(self, row) -> None:
        row.setStyleSheet(
            f"QFrame#boardRow {{ background: {SiColors.CARD};"
            f" border: 1px solid {SiColors.LINE}; border-radius: 10px; }}"
            f"QFrame#boardRow:hover {{ background: {SiColors.CARD_HOVER}; }}")

    def _checked_keys(self, keys: list[str]) -> list[str]:
        return [k for k in keys
                if (btn := self._checks.get(k)) is not None and btn.isChecked()]

    def _checked_scene_ids(self) -> list[str]:
        """已勾选的场景 id（场景页顺序）。勾选按钮的 key 带 scene: 前缀。"""
        return [sid for sid in self._scene_keys
                if (btn := self._checks.get(self._SCENE_PREFIX + sid)) is not None
                and btn.isChecked()]

    def selected_dids(self) -> list[str]:
        """已勾选的设备 did（设备页顺序）。"""
        return self._checked_keys(self._device_keys)

    def selected_scenes(self) -> list[dict]:
        """已勾选的场景快照（与 board.json 同结构，场景页顺序）。"""
        result = []
        for sid in self._checked_scene_ids():
            row = self._rows.get(self._SCENE_PREFIX + sid)
            scene = getattr(row, "_scene", None)
            if scene:
                result.append(dict(scene))
        return result

    def ordered_items(self) -> list[dict]:
        """设备与场景的混排顺序（写入 board.json 的 order）。

        本窗不再提供排序，但**不能顺手把顺序按"设备在前、场景在后"重写**：
        用户可能已在挂件面板上拖好了交错顺序。这里保留打开时的既有顺序，
        只把新勾选的项目按"设备页顺序 + 场景页顺序"追加到末尾。
        """
        checked = ([{"type": "device", "id": k} for k in self.selected_dids()]
                   + [{"type": "scene", "id": k} for k in self._checked_scene_ids()])
        keys = {(e["type"], e["id"]) for e in checked}
        order = [e for e in self._initial_order
                 if (e.get("type"), e.get("id")) in keys]
        seen = {(e["type"], e["id"]) for e in order}
        order += [e for e in checked if (e["type"], e["id"]) not in seen]
        return order

    # ---------- 窗口与拖拽 ----------

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self._title_bar and event.type() in (
            event.Type.MouseButtonPress, event.Type.MouseMove,
            event.Type.MouseButtonRelease,
        ):
            if event.type() == event.Type.MouseButtonPress:
                if event.button() == Qt.MouseButton.LeftButton:
                    self._header_drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                return True
            if event.type() == event.Type.MouseMove:
                if self._header_drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
                    self.move(event.globalPosition().toPoint() - self._header_drag_pos)
                return True
            if event.type() == event.Type.MouseButtonRelease:
                self._header_drag_pos = None
                return False
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._header_drag_pos = None
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self._header_drag_pos = None
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._header_drag_pos = None
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._panel.setGeometry(self.rect())

    def retheme(self) -> None:
        """主题切换：面板内所有内联样式都是构造期求值，须逐控件重设。

        分组重设而非遍历 findChildren：勾选钮、列数钮、底部按钮三套
        观感不同，统一套一种会把列数高亮和保存按钮刷错。
        """
        super().retheme()
        for row in self._rows.values():
            self._reset_row_style(row)
        for edit in (self._search, self._scene_search):
            edit.setStyleSheet(self._search_qss())
        # 房间下拉：配色与自绘箭头都要跟着换
        apply_room_combo_theme(self._room_combo)
        for btn in self._checks.values():
            btn.setText("✓" if btn.isChecked() else "+")
            btn.setStyleSheet(check_toggle_qss(28))
        self._refresh_columns_ui()
        for label in (self._columns_hint, self._opacity_hint, self._bg_hint,
                      self._style_hint):
            label.setStyleSheet(
                f"color: {SiColors.TEXT_SECONDARY}; background: transparent; font-size: 8pt;")
        for label in (self._subtitle, self._scene_subtitle):
            label.setStyleSheet(
                f"color: {SiColors.TEXT_SECONDARY}; background: transparent; font-size: 9pt;")
        for i, btn in enumerate(self._tab_buttons):
            btn.setStyleSheet(self._tab_qss(i == self._stack.currentIndex()))
        for box in self.findChildren(QFrame):
            if box.objectName() == "boardSection":
                box.setStyleSheet(
                    f"QFrame#boardSection {{ background: {SiColors.CARD};"
                    f" border: 1px solid {SiColors.LINE}; border-radius: 10px; }}")
        # 背景色块与置顶勾选框的内联样式同样要重刷（色块内容色不变）
        for chip in self._bg_chips:
            if chip.text():
                chip.setStyleSheet(self._text_chip_qss())
        for tag in self._scene_tags:
            tag.setStyleSheet(
                f"color: {SiColors.THEME}; background: transparent;"
                f" border: 1px solid {SiColors.THEME}; border-radius: 6px;"
                " padding: 0 4px; font-size: 8pt;")
        # 自绘复选框的颜色在 paint 时取当前调色板，重绘即可跟随主题
        self._lock_check.update()
        self._topmost_check.update()
        for btn in (self._cancel_btn, self._ok_btn):
            btn.setStyleSheet(self._plain_btn_qss(btn is self._ok_btn))

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # 设置项挪进「外观」页后，设备页可以把整块高度让给列表
        self._center_on_screen(360, 620)
        self.raise_()
        self._fade_in()