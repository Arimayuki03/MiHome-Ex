# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Ex: 米家设备的 Windows 桌面控制端（扩展版）
# Copyright (C) 2026 MiHome-Ex contributors
"""托盘项目管理对话框：设备/场景勾选加入 + 统一排序页拖拽排序。

设备页可同时按关键字与房间筛选候选，房间选项带家庭名前缀；场景不属于
任何房间，切到场景页时房间下拉收起，避免把场景整页藏空。
"""

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

import qtawesome as qta

from app.core import settings_store, tray_store
from app.core.models import DeviceInfo, SceneInfo
from app.ui.overlay_dialog import OverlayDialog
from app.ui.room_filter import make_room_combo, refresh_room_combo, room_key
from app.ui.si_theme import (
    SiColors,
    check_toggle_qss,
    primary_button_qss,
    secondary_button_qss,
)
from app.ui.tray.draggable_row import _DraggableRow


class TrayManagerDialog(OverlayDialog):
    """托盘快捷项管理：设备与场景双选项卡，拖拽排序 + 勾选加入。"""

    def __init__(self, devices: list[DeviceInfo], parent=None, *,
                 scenes: list[SceneInfo] | None = None,
                 scene_loader=None, jobs=None):
        # 与设备详情/添加抽屉同款观感：无边框圆角面板；
        # 但面板铺满窗口并带可拖拽标题栏，保持可移动性，无遮罩
        super().__init__(parent, overlay=False)
        self.setWindowTitle("管理托盘项目")
        self._drag_did: str | None = None
        self.setAcceptDrops(True)
        self._devices = list(devices)
        self._scenes = scenes
        self._scene_loader = scene_loader
        self._jobs = jobs
        self._scene_loading = False
        self._scene_filled = False
        self._scene_map: dict[str, SceneInfo] = {}
        self._scene_rows: list[tuple[str, QFrame]] = []
        self._scene_checks: dict[str, QPushButton] = {}
        # 统一顺序：设备与场景混排后的展示顺序，排序页拖拽即时改写
        self._view = "list"
        self._order: list[dict] = tray_store.load_order()
        self._snap_map: dict[str, dict] = {s["scene_id"]: s for s in tray_store.load_scenes()}
        self._sort_rows: dict[str, _DraggableRow] = {}

        lay = QVBoxLayout(self._panel)
        lay.setContentsMargins(16, 12, 16, 16)
        lay.setSpacing(12)

        # 顶部标题栏：标题 + 关闭按钮，整个标题栏区域可拖动窗口
        title_bar = QFrame(self._panel)
        title_bar.setObjectName("trayTitleBar")
        title_bar.setAttribute(Qt.WA_StyledBackground, True)
        title_bar.setStyleSheet("QFrame#trayTitleBar { background: transparent; }")
        title_bar.setCursor(Qt.OpenHandCursor)
        self._title_bar = title_bar
        self._header_drag_pos = None
        # 标题栏整个区域可拖拽窗口：事件过滤器捕获，避免子控件吃掉事件
        title_bar.installEventFilter(self)
        header = QHBoxLayout(title_bar)
        header.setContentsMargins(4, 2, 4, 2)
        header.setSpacing(8)
        header_title = QLabel("管理托盘项目")
        header_title.setFont(QFont("Microsoft YaHei UI", 12, QFont.Weight.DemiBold))
        header_title.setStyleSheet(f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")
        header.addWidget(header_title)
        header.addStretch(1)
        lay.addWidget(title_bar)

        # 分类选项卡：设备 / 场景；默认设备
        tab_row = QHBoxLayout()
        tab_row.setContentsMargins(0, 0, 0, 0)
        tab_row.setSpacing(8)
        self._tab_dev = self._make_tab_button("设备")
        self._tab_scene = self._make_tab_button("场景")
        self._tab_dev.clicked.connect(lambda: self._switch_tab(0))
        self._tab_scene.clicked.connect(lambda: self._switch_tab(1))
        self._tab_dev.setChecked(True)
        tab_row.addWidget(self._tab_dev)
        tab_row.addWidget(self._tab_scene)
        tab_row.addStretch(1)
        # 排序入口固定在选项卡行最右端：与设备/场景的添加管理分离
        self._sort_btn = QPushButton("排序")
        self._sort_btn.setIcon(qta.icon('mdi.sort', color=SiColors.TEXT_PRIMARY))
        self._sort_btn.setCursor(Qt.PointingHandCursor)
        self._sort_btn.setFixedHeight(30)
        self._sort_btn.setStyleSheet(
            f"QPushButton {{ background: {SiColors.CTRL_BG};"
            f" border: 1px solid {SiColors.CTRL_BORDER}; border-radius: 8px;"
            f" padding: 0 14px; color: {SiColors.TEXT_PRIMARY}; font-weight: 600; }}"
            f"QPushButton:hover {{ background: {SiColors.CTRL_BG_HOVER}; }}")
        self._sort_btn.clicked.connect(self._toggle_sort_view)
        tab_row.addWidget(self._sort_btn)
        lay.addLayout(tab_row)

        # fork 的设备 span 说明并入上游场景/排序页文案，功能入口不变
        self._subtitle = QLabel(
            "勾选加入托盘快捷窗口，可长按拖拽排序\n"
            "在托盘中右键设备卡片可切换占半行/整行（充电器整行显示实时功率）")
        self._subtitle.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY}; background: transparent; font-size: 9pt;")
        lay.addWidget(self._subtitle)

        # 顶部搜索：设备多时靠滚动找太慢。提示词按当前页给，
        # 设备页不出现"场景"、场景页不出现"设备"，避免误导搜索范围
        self._search = QLineEdit()
        self._search.setPlaceholderText("搜索设备 / 房间…")
        self._search.setClearButtonEnabled(True)
        self._search.setFixedHeight(28)
        self._search.setStyleSheet(
            f"QLineEdit {{ background: {SiColors.SURFACE}; border: 1px solid {SiColors.LINE};"
            f" border-radius: 8px; padding: 2px 10px; color: {SiColors.TEXT_PRIMARY};"
            " font-size: 9pt; }"
            f"QLineEdit:focus {{ border-color: {SiColors.THEME}; }}")
        self._search.textChanged.connect(self._apply_filter)
        self._filter_row = self._make_filter_row()
        lay.addWidget(self._filter_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        host = QWidget()
        host.setStyleSheet("background: transparent;")
        self._host = host
        self._list_lay = QVBoxLayout(host)
        self._list_lay.setContentsMargins(0, 0, 0, 0)
        self._list_lay.setSpacing(8)
        scroll.setWidget(host)
        lay.addWidget(scroll, stretch=1)
        # 与工作台一致：容器与对话框均接受拖拽，确保事件不被 viewport 拦截
        host.setAcceptDrops(True)
        scroll.viewport().setAcceptDrops(True)
        self._scroll = scroll

        # 场景页：与设备页同构的滚动容器，内容按状态动态填充
        scene_scroll = QScrollArea()
        scene_scroll.setWidgetResizable(True)
        scene_scroll.setFrameShape(QFrame.NoFrame)
        scene_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scene_host = QWidget()
        scene_host.setStyleSheet("background: transparent;")
        self._scene_lay = QVBoxLayout(scene_host)
        self._scene_lay.setContentsMargins(0, 0, 0, 0)
        self._scene_lay.setSpacing(8)
        scene_scroll.setWidget(scene_host)
        scene_scroll.hide()
        lay.addWidget(scene_scroll, stretch=1)
        self._scene_scroll = scene_scroll

        # 排序页：只列已加入托盘的项目（设备+场景混排），长按拖拽调整
        sort_scroll = QScrollArea()
        sort_scroll.setWidgetResizable(True)
        sort_scroll.setFrameShape(QFrame.NoFrame)
        sort_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        sort_host = QWidget()
        sort_host.setStyleSheet("background: transparent;")
        self._sort_lay = QVBoxLayout(sort_host)
        self._sort_lay.setContentsMargins(0, 0, 0, 0)
        self._sort_lay.setSpacing(8)
        sort_scroll.setWidget(sort_host)
        sort_scroll.hide()
        lay.addWidget(sort_scroll, stretch=1)
        sort_host.setAcceptDrops(True)
        sort_scroll.viewport().setAcceptDrops(True)
        self._sort_scroll = sort_scroll
        self._sort_host = sort_host

        self._checks: dict[str, QPushButton] = {}
        selected = list(tray_store.load())
        dev_map = {d.did: d for d in devices}
        # 已选设备按 tray_store 顺序排列，未选设备在线优先+按名排序追加
        ordered = [dev_map[d] for d in selected if d in dev_map]
        sel_set = set(selected)
        unordered = sorted(
            [d for d in devices if d.did not in sel_set],
            key=lambda d: (0 if d.online else 1, d.name))
        all_devs = ordered + unordered

        for dev in all_devs:
            row = QFrame()
            row.setObjectName("trayRow")
            row.setAttribute(Qt.WA_StyledBackground, True)
            row.setFixedHeight(48)
            row.setMaximumWidth(290)
            from PySide6.QtWidgets import QSizePolicy as _SP2
            row.setSizePolicy(_SP2.Preferred, _SP2.Fixed)
            row.setStyleSheet(self._row_style())
            rl = QHBoxLayout(row)
            rl.setContentsMargins(10, 8, 16, 8)
            name = QLabel(f"{dev.name}  ·  {dev.room_name}")
            name.setStyleSheet(f"color: {SiColors.TEXT_PRIMARY if dev.online else f'{SiColors.OFFLINE_TEXT}'}; background: transparent;")
            rl.addWidget(name, stretch=1)
            btn = QPushButton("✓" if dev.did in selected else "+")
            btn.setFixedSize(28, 28)
            btn.setCheckable(True)
            btn.setChecked(dev.did in selected)
            # 文字必须随勾选态同步：QSS 只负责背景色，固定文字会导致
            # 绿底显示加号 / 暗底显示对勾的错位（与详情页 set_added 一致）
            btn.toggled.connect(lambda checked, b=btn: b.setText("✓" if checked else "+"))
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(check_toggle_qss(28))
            self._checks[dev.did] = btn
            rl.addWidget(btn)
            row._did = dev.did  # type: ignore[attr-defined]
            # 搜索匹配用：设备名 + 房间名，大小写不敏感
            row._search_text = f"{dev.name} {dev.room_name}".lower()
            # 房间筛选用：与下拉项的 item data 同一种键（家庭 + 房间）
            row._room_key = room_key(dev)
            self._list_lay.addWidget(row)
        self._list_lay.addStretch(1)

        # 房间下拉的选项来自实际设备：一台都没有房间归属时整块收起，
        # 把整行宽度让给搜索框
        self._has_room_options = refresh_room_combo(self._room_combo, all_devs)
        self._room_combo.setVisible(self._has_room_options)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        cancel.setStyleSheet(secondary_button_qss())
        ok = QPushButton("保存")
        ok.setCursor(Qt.PointingHandCursor)
        # 主按钮文字固定白色（不随深/浅主题翻转）
        ok.setStyleSheet(primary_button_qss())
        ok.clicked.connect(self.accept)
        btn_row.addWidget(cancel)
        btn_row.addWidget(ok)
        lay.addLayout(btn_row)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # 事件已被标题栏过滤器处理，这里只处理空白面板（可忽略）
        self._header_drag_pos = None
        super().mousePressEvent(event)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        # 标题栏拖拽：在 eventFilter 中捕获鼠标事件，子控件（标题/按钮）
        # 的事件也会先经过此处，统一处理窗口移动
        if obj is self._title_bar and event.type() in (
            QEvent.MouseButtonPress, QEvent.MouseMove, QEvent.MouseButtonRelease
        ):
            if event.type() == QEvent.MouseButtonPress:
                if event.button() == Qt.LeftButton:
                    self._header_drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                return True
            if event.type() == QEvent.MouseMove:
                if self._header_drag_pos is not None and event.buttons() & Qt.LeftButton:
                    self.move(event.globalPosition().toPoint() - self._header_drag_pos)
                return True
            if event.type() == QEvent.MouseButtonRelease:
                self._header_drag_pos = None
                return False  # 放行，让按钮点击生效
        return super().eventFilter(obj, event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self._header_drag_pos = None
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._header_drag_pos = None
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # 面板铺满整个窗口，保留四角圆角观感
        self._panel.setGeometry(self.rect())

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        parent = self.parentWidget()
        if parent is not None and parent.window() is not None and parent.window().isVisible():
            self._center_on_screen(340, 475)
            self.raise_()
            self._fade_in()
            return
        # 主窗口隐藏（托盘独立弹出）时稍高一些
        self._center_on_screen(340, 520)
        self.raise_()
        self._fade_in()

    # ---------- 排序页 ----------

    @staticmethod
    def _row_style(highlight: bool = False) -> str:
        """行样式：普通态带 hover，拖拽命中态描边高亮。"""
        if highlight:
            return (f"QFrame#trayRow {{ background: {SiColors.CARD_HOVER};"
                    f" border: 2px solid {SiColors.THEME}; border-radius: 10px; }}")
        return (f"QFrame#trayRow {{ background: {SiColors.CARD}; border: 1px solid {SiColors.LINE}; border-radius: 10px; }}"
                f"QFrame#trayRow:hover {{ background: {SiColors.CARD_HOVER}; }}")

    def _toggle_sort_view(self) -> None:
        self._switch_view("list" if self._view == "sort" else "sort")

    def _switch_view(self, view: str) -> None:
        self._view = view
        is_list = view == "list"
        self._tab_dev.setVisible(is_list)
        self._tab_scene.setVisible(is_list)
        self._filter_row.setVisible(is_list)
        self._sync_room_visibility()
        self._scroll.setVisible(is_list and self._tab_dev.isChecked())
        self._scene_scroll.setVisible(is_list and not self._tab_dev.isChecked())
        self._sort_scroll.setVisible(not is_list)
        if is_list:
            self._sort_btn.setText("排序")
            self._sort_btn.setIcon(qta.icon('mdi.sort', color=SiColors.TEXT_PRIMARY))
        else:
            self._sort_btn.setText("返回")
            self._sort_btn.setIcon(qta.icon('mdi.arrow-left', color=SiColors.TEXT_PRIMARY))
            self._rebuild_sort_view()
        self._apply_filter(self._search.text())
        self._update_subtitle()

    def _make_filter_row(self) -> QWidget:
        """搜索框 + 房间筛选下拉：两个条件取交集，都只隐藏行不动勾选。"""
        self._room_combo = make_room_combo()
        self._room_combo.currentIndexChanged.connect(
            lambda _index: self._apply_filter(self._search.text()))
        # 还没有设备清单，先按「没有可筛房间」处理
        self._has_room_options = False
        holder = QWidget()
        holder.setStyleSheet("background: transparent;")
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(self._search, stretch=1)
        row.addWidget(self._room_combo)
        return holder

    def _sync_room_visibility(self) -> None:
        """房间下拉只对设备页生效：场景不属于任何房间，切过去就收起。"""
        self._room_combo.setVisible(
            self._has_room_options and self._view == "list"
            and self._tab_dev.isChecked())

    def _apply_filter(self, text: str) -> None:
        """按关键字与房间隐藏不匹配的行（只隐藏，不改勾选状态）。

        房间条件只在设备页生效：场景行没有房间归属，若切到场景页仍按
        房间过滤，会把场景整页藏空。
        """
        keyword = (text or "").strip().lower()
        room = self._room_combo.currentData()
        on_device_page = self._view == "list" and self._tab_dev.isChecked()

        def _match(widget, use_room: bool) -> bool:
            if keyword and keyword not in getattr(widget, "_search_text", ""):
                return False
            if use_room and room is not None:
                return getattr(widget, "_room_key", None) == room
            return True

        for i in range(self._list_lay.count()):
            item = self._list_lay.itemAt(i)
            widget = item.widget() if item else None
            if widget is not None and getattr(widget, "_did", None):
                widget.setVisible(_match(widget, on_device_page))
        for _sid, row in self._scene_rows:
            row.setVisible(_match(row, False))

    def _update_subtitle(self) -> None:
        if self._view == "sort":
            self._subtitle.setText("长按拖拽调整托盘项目的显示顺序")
        elif self._tab_scene.isChecked():
            self._subtitle.setText("勾选加入托盘快捷窗口，点击行内按钮可立即执行场景")
        else:
            self._subtitle.setText("勾选加入托盘快捷窗口")

    def _rebuild_sort_view(self) -> None:
        """按当前勾选集合重建排序页：已加入的项目按统一顺序混排。"""
        self._clear_sort_layout()
        checked = self._checked_items()
        keys = {(it["type"], it["id"]) for it in checked}
        order = [it for it in self._order if (it["type"], it["id"]) in keys]
        seen = {(it["type"], it["id"]) for it in order}
        # 新勾选但还没进过 order 的项目追加到末尾
        order += [it for it in checked if (it["type"], it["id"]) not in seen]
        self._order = order
        if not order:
            # 空态：托盘还没加入任何项目，给一句引导而非空白页
            hint = QLabel("暂无已加入托盘的项目\n先在设备或场景页勾选添加")
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hint.setWordWrap(True)
            hint.setStyleSheet(
                f"color: {SiColors.TEXT_MUTED}; background: transparent; font-size: 9pt;")
            self._sort_lay.addStretch(1)
            self._sort_lay.addWidget(hint)
            self._sort_lay.addStretch(1)
            return
        dev_map = {d.did: d for d in self._devices}
        for item in order:
            row = self._make_sort_row(item, dev_map)
            if row is None:
                continue
            self._sort_rows[item["id"]] = row
            self._sort_lay.addWidget(row)
        self._sort_lay.addStretch(1)

    def _make_sort_row(self, item: dict, dev_map: dict) -> _DraggableRow | None:
        if item["type"] == "device":
            dev = dev_map.get(item["id"])
            if dev is None:
                return None
            text = f"{dev.name}  ·  {dev.room_name}"
            color = SiColors.TEXT_PRIMARY if dev.online else SiColors.OFFLINE_TEXT
        else:
            # 最新场景名优先，未加载时回退已存快照（快照保证离线可显示）
            scene = next(
                (s for s in (self._scenes or []) if s.scene_id == item["id"]),
                None)
            snap = self._snap_map.get(item["id"])
            if scene is None and snap is None:
                return None
            if scene is not None:
                text = f"{scene.name}  ·  {scene.home_name}" if scene.home_name else scene.name
            else:
                text = f"{snap['name']}  ·  {snap['home_name']}" if snap["home_name"] else snap["name"]
            color = SiColors.TEXT_PRIMARY
        row = _DraggableRow(item["id"])
        row.setObjectName("trayRow")
        row.setAttribute(Qt.WA_StyledBackground, True)
        row.setFixedHeight(48)
        row.setMaximumWidth(290)
        from PySide6.QtWidgets import QSizePolicy as _SP4
        row.setSizePolicy(_SP4.Preferred, _SP4.Fixed)
        row.setStyleSheet(self._row_style())
        rl = QHBoxLayout(row)
        rl.setContentsMargins(10, 8, 16, 8)
        handle = QLabel()
        handle.setPixmap(qta.icon('mdi.drag', color=SiColors.TEXT_MUTED).pixmap(14, 14))
        handle.setStyleSheet("background: transparent;")
        rl.addWidget(handle)
        name = QLabel(text)
        name.setStyleSheet(f"color: {color}; background: transparent;")
        rl.addWidget(name, stretch=1)
        row.dragStarted.connect(self._on_drag_start)
        row.dragFinished.connect(self._on_drag_finish)
        return row

    def _clear_sort_layout(self) -> None:
        while self._sort_lay.count():
            lay_item = self._sort_lay.takeAt(0)
            if w := lay_item.widget():
                w.deleteLater()
        self._sort_rows.clear()

    def _checked_items(self) -> list[dict]:
        """当前勾选的托盘项目（设备按列表顺序、场景按场景页顺序）。"""
        items = []
        for i in range(self._list_lay.count()):
            lay_item = self._list_lay.itemAt(i)
            w = lay_item.widget() if lay_item else None
            did = getattr(w, "_did", None)
            if did and self._checks.get(did) is not None and self._checks[did].isChecked():
                items.append({"type": "device", "id": did})
        if self._scene_filled:
            for sid, _row in self._scene_rows:
                btn = self._scene_checks.get(sid)
                if btn is not None and btn.isChecked():
                    items.append({"type": "scene", "id": sid})
        else:
            # 场景页没呈现过确认列表：已存快照就是当前勾选
            for s in tray_store.load_scenes():
                items.append({"type": "scene", "id": s["scene_id"]})
        return items

    def _sync_order_from_layout(self) -> None:
        """把排序页当前布局顺序回写为待保存顺序。

        拖拽只改动布局，不回写的话 selected_order 会按进页时的旧顺序
        输出，用户拖完点保存后托盘顺序纹丝不动。
        """
        pos = {rid: i for i, rid in enumerate(self._sort_layout_ids())}
        self._order.sort(key=lambda it: pos.get(it["id"], len(pos)))

    def selected_order(self) -> list[dict]:
        """最终统一顺序：排序页顺序过滤到最终勾选集合，新勾选追加末尾。"""
        if self._sort_rows:
            # 排序页构建过：以当前布局顺序为准（拖拽结果即时回写）
            self._sync_order_from_layout()
        checked = self._checked_items()
        keys = {(it["type"], it["id"]) for it in checked}
        order = [it for it in self._order if (it["type"], it["id"]) in keys]
        seen = {(it["type"], it["id"]) for it in order}
        order += [it for it in checked if (it["type"], it["id"]) not in seen]
        return order

    def selected_dids(self) -> list[str]:
        """兼容旧调用：只返回勾选设备 did（顺序跟随统一 order）。"""
        return [it["id"] for it in self.selected_order()
                if it["type"] == "device"]

    def _sort_layout_ids(self) -> list[str]:
        """排序页当前布局顺序（供保存与测试读取）。"""
        result = []
        for i in range(self._sort_lay.count()):
            lay_item = self._sort_lay.itemAt(i)
            w = lay_item.widget() if lay_item else None
            rid = getattr(w, "_did", None)
            if rid:
                result.append(rid)
        return result

    def _on_drag_start(self, did: str) -> None:
        self._drag_did = did

    def _on_drag_finish(self) -> None:
        self._drag_did = None
        self._reset_row_styles()

    def _reset_row_styles(self) -> None:
        for r in self._sort_rows.values():
            r.setStyleSheet(self._row_style())

    def _sort_drop_pos(self, point):
        """对话框坐标 → 排序页内容坐标（viewport 外再按 viewport 映射补正）。"""
        pos = self._sort_host.mapFrom(self, point)
        if not self._sort_host.rect().contains(pos):
            vp = self._sort_scroll.viewport()
            pos = self._sort_host.mapFrom(vp, vp.mapFrom(self, point))
        return pos

    def dragEnterEvent(self, e) -> None:  # noqa: N802
        if e.mimeData().hasText() and e.mimeData().text() in self._sort_rows:
            self._drag_did = e.mimeData().text()
            e.acceptProposedAction()
        elif self._drag_did:
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e) -> None:  # noqa: N802
        if e.mimeData().hasText() and e.mimeData().text() in self._sort_rows:
            self._drag_did = e.mimeData().text()
        if not self._drag_did:
            e.ignore()
            return
        pos = self._sort_drop_pos(e.position().toPoint())
        target_id, _after = self._hit_insert(pos)
        for rid, row in self._sort_rows.items():
            if rid == self._drag_did:
                continue
            row.setStyleSheet(self._row_style(highlight=rid == target_id))
        e.acceptProposedAction()

    def dragLeaveEvent(self, e) -> None:  # noqa: N802
        self._reset_row_styles()
        super().dragLeaveEvent(e)

    def dropEvent(self, e) -> None:  # noqa: N802
        self._reset_row_styles()
        did_text = e.mimeData().text() if e.mimeData().hasText() else self._drag_did
        if not did_text or did_text not in self._sort_rows:
            e.ignore()
            return
        self._drag_did = did_text
        self._apply_drop(did_text, self._sort_drop_pos(e.position().toPoint()))
        self._drag_did = None
        e.acceptProposedAction()

    def _hit_insert(self, pos) -> tuple[str | None, bool]:
        """落点目标：返回 (目标行 id, 是否插到目标之后)；id 为 None 表示列表末尾。

        半区模型：落在行上半区插其前、下半区插其后；行间空隙归入其下最近的
        行（插其前）；只有所有行下方才算末尾。按 y 坐标排序遍历而不依赖
        dict 插入顺序——拖拽重排后布局顺序与 dict 顺序会脱节。
        """
        rows = sorted(self._sort_rows.items(), key=lambda kv: kv[1].y())
        for rid, row in rows:
            g = row.geometry()
            if pos.y() < g.top():
                return rid, False
            if g.top() <= pos.y() < g.bottom():
                return rid, pos.y() > g.center().y()
        return None, True

    def _apply_drop(self, did: str, pos) -> None:
        """按落点坐标应用排序。可独立调用以便离屏测试。"""
        src_row = self._sort_rows.get(did)
        if src_row is None:
            return
        target_id, after = self._hit_insert(pos)
        if target_id == did:
            # 原地放下：保持原位不动
            return
        # 先移除源行再取目标索引：removeWidget 会让后续条目前移，
        # 用移除前算的索引插入会偏一位
        self._sort_lay.removeWidget(src_row)
        src_row.show()
        if target_id is None:
            # 所有行下方：移到末尾（最后一个 widget 之后、stretch 之前）
            self._sort_lay.insertWidget(max(0, self._sort_lay.count() - 1), src_row)
            self._sync_order_from_layout()
            return
        dst_row = self._sort_rows[target_id]
        dst_idx = self._sort_lay.indexOf(dst_row)
        if dst_idx < 0:
            return
        if after:
            dst_idx += 1
        self._sort_lay.insertWidget(dst_idx, src_row)
        self._sync_order_from_layout()

    # ---------- 场景页 ----------

    def _make_tab_button(self, text: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setCheckable(True)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedHeight(30)
        btn.setStyleSheet(
            f"QPushButton {{ background: {SiColors.CTRL_BG};"
            f" border: 1px solid {SiColors.CTRL_BORDER}; border-radius: 8px;"
            f" padding: 0 18px; color: {SiColors.TEXT_PRIMARY}; font-weight: 600; }}"
            f"QPushButton:hover {{ background: {SiColors.CTRL_BG_HOVER}; }}"
            f"QPushButton:checked {{ background: {SiColors.THEME};"
            f" border-color: {SiColors.THEME}; color: {SiColors.ON_THEME_TEXT}; }}"
            f"QPushButton:checked:hover {{ background: {SiColors.THEME_CHECKED_HOVER};"
            f" border-color: {SiColors.THEME_CHECKED_HOVER}; }}")
        return btn

    def _switch_tab(self, index: int) -> None:
        is_dev = index == 0
        self._tab_dev.setChecked(is_dev)
        self._tab_scene.setChecked(not is_dev)
        is_list = self._view == "list"
        self._scroll.setVisible(is_dev and is_list)
        self._scene_scroll.setVisible(not is_dev and is_list)
        self._search.setPlaceholderText(
            "搜索设备 / 房间…" if is_dev else "搜索场景 / 家庭…")
        self._update_subtitle()
        if not is_dev:
            self._ensure_scenes()
        self._sync_room_visibility()
        # 场景行可能是切页后才建出来的，关键字要重新应用一遍
        self._apply_filter(self._search.text())

    def _ensure_scenes(self) -> None:
        """场景页内容准备：按开关与数据状态选择 提示/加载/列表。"""
        if not settings_store.get_show_scene_tab():
            self._fill_scene_status(
                "场景功能未开启",
                "请先在 设置 → 主题界面 中开启「显示场景选项卡」，再回到此处添加场景")
            return
        if self._scenes is not None:
            if not self._scene_filled:
                self._fill_scene_list()
            return
        if self._scene_loading:
            self._fill_scene_status("正在加载场景…", "")
            return
        if self._scene_loader is None or self._jobs is None:
            self._fill_scene_status("场景数据不可用", "")
            return
        self._scene_loading = True
        self._fill_scene_status("正在加载场景…", "")
        self._jobs.submit(
            self._scene_loader,
            on_success=self._on_scenes_loaded,
            on_error=self._on_scenes_failed,
        )

    def _on_scenes_loaded(self, scenes: list[SceneInfo]) -> None:
        import shiboken6

        if not shiboken6.isValid(self):
            return
        self._scene_loading = False
        self._scenes = scenes
        if self._view == "sort":
            # 排序页开着：用最新场景名刷新行（快照名可能已过期）
            self._rebuild_sort_view()
        if self._tab_scene.isChecked():
            # 场景页（可能隐藏中）同步填充，切回列表即可见
            self._fill_scene_list()

    def _on_scenes_failed(self, error: Exception) -> None:
        import shiboken6

        if not shiboken6.isValid(self):
            return
        self._scene_loading = False
        if self._tab_scene.isChecked():
            self._fill_scene_status("场景加载失败", str(error))

    def _clear_scene_layout(self) -> None:
        while self._scene_lay.count():
            item = self._scene_lay.takeAt(0)
            if w := item.widget():
                w.deleteLater()
        self._scene_rows.clear()
        self._scene_checks.clear()
        self._scene_map.clear()

    def _fill_scene_status(self, title: str, detail: str) -> None:
        self._clear_scene_layout()
        box = QWidget()
        box.setStyleSheet("background: transparent;")
        bx = QVBoxLayout(box)
        bx.setContentsMargins(14, 0, 14, 0)
        bx.setSpacing(6)
        t = QLabel(title)
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        t.setFont(QFont("Microsoft YaHei UI", 10, QFont.Weight.DemiBold))
        t.setStyleSheet(f"color: {SiColors.TEXT_SECONDARY}; background: transparent;")
        bx.addWidget(t)
        if detail:
            d = QLabel(detail)
            d.setAlignment(Qt.AlignmentFlag.AlignCenter)
            d.setWordWrap(True)
            d.setStyleSheet(f"color: {SiColors.TEXT_MUTED}; background: transparent; font-size: 8pt;")
            bx.addWidget(d)
        self._scene_lay.addStretch(1)
        self._scene_lay.addWidget(box)
        self._scene_lay.addStretch(1)

    def _fill_scene_list(self) -> None:
        self._clear_scene_layout()
        scenes = self._scenes or []
        if not scenes:
            # 服务端已确认无场景：置已填充，保存时按空列表清掉失效快照
            self._scene_filled = True
            self._fill_scene_status("暂无场景", "可在米家 APP「智能」中创建手动场景后再回来添加")
            return
        self._scene_filled = True
        self._scene_map = {s.scene_id: s for s in scenes}
        # 已选场景按存储顺序排列，未选按名排序追加（同设备页规则）
        saved_ids = [s["scene_id"] for s in tray_store.load_scenes()]
        saved_set = set(saved_ids)
        ordered = [self._scene_map[s] for s in saved_ids if s in self._scene_map]
        unordered = sorted(
            [s for s in scenes if s.scene_id not in saved_set],
            key=lambda s: s.name)
        for scene in ordered + unordered:
            row, btn = self._make_scene_row(scene, scene.scene_id in saved_set)
            self._scene_checks[scene.scene_id] = btn
            self._scene_rows.append((scene.scene_id, row))
            self._scene_lay.addWidget(row)
        self._scene_lay.addStretch(1)
        self._apply_filter(self._search.text())

    def _make_scene_row(self, scene: SceneInfo, selected: bool) -> tuple[QFrame, QPushButton]:
        row = QFrame()
        row.setObjectName("trayRow")
        row.setAttribute(Qt.WA_StyledBackground, True)
        row.setFixedHeight(48)
        row.setMaximumWidth(290)
        from PySide6.QtWidgets import QSizePolicy as _SP3
        row.setSizePolicy(_SP3.Preferred, _SP3.Fixed)
        row.setStyleSheet(self._row_style())
        rl = QHBoxLayout(row)
        rl.setContentsMargins(10, 8, 16, 8)
        text = f"{scene.name}  ·  {scene.home_name}" if scene.home_name else scene.name
        name = QLabel(text)
        name.setStyleSheet(f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")
        rl.addWidget(name, stretch=1)
        btn = QPushButton("✓" if selected else "+")
        btn.setFixedSize(28, 28)
        btn.setCheckable(True)
        btn.setChecked(selected)
        btn.toggled.connect(lambda checked, b=btn: b.setText("✓" if checked else "+"))
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(check_toggle_qss(28))
        rl.addWidget(btn)
        row._search_text = f"{scene.name} {scene.home_name} 场景".lower()
        return row, btn

    def selected_scene_data(self) -> list[dict]:
        """按当前场景列表顺序返回已勾选的场景快照（tray_store 存储格式）。

        场景页未呈现过确认列表（未开启 / 加载中 / 加载失败）时返回
        已存快照原值：用户只来调整设备时不应静默清掉托盘场景。
        """
        if not self._scene_filled:
            return tray_store.load_scenes()
        result = []
        for sid, _row in self._scene_rows:
            btn = self._scene_checks.get(sid)
            scene = self._scene_map.get(sid)
            if btn is not None and btn.isChecked() and scene is not None:
                result.append({
                    "scene_id": scene.scene_id,
                    "name": scene.name,
                    "home_id": scene.home_id,
                    "home_name": scene.home_name,
                })
        return result




