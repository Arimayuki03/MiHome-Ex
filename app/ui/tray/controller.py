# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""系统托盘控制器：常驻图标 + 菜单 + 快捷窗口编排。"""

import time

from PySide6.QtCore import QPoint, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QCursor, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from app.core.jobs import JobExecutor
from app.core.models import DeviceInfo
from app.core.service import MijiaService
from app.ui.tray.cuktech_hover import CuktechHoverPopup
from app.ui.tray.quick_window import TrayQuickWindow

# 托盘图标悬停判定：光标须连续停留在图标几何内该时长才弹数据
_HOVER_DELAY_MS = 500
# 弹窗可见期间的状态刷新周期（SSE 推送为主，此轮询兜底）
_POPUP_POLL_MS = 2000


class TrayController:
    """托盘图标控制器：常驻图标 + 快捷窗口 + 右键菜单。"""

    def __init__(self, service: MijiaService, jobs: JobExecutor, main_window):
        self._service = service
        self._jobs = jobs
        self._main = main_window
        self._pending_show = False
        # 快捷窗口设为独立顶层窗口，不随主窗口模态对话框被阻塞
        self._create_quick_window()

        # 使用自定义 tray_icon.png 作为托盘图标，提供多尺寸确保清晰
        from app import resource_path
        _icon_path = str(resource_path("app/ui/tray_icon.png"))
        _tray_icon = QIcon(_icon_path)
        _tray_icon.addFile(_icon_path, QSize(16, 16))
        _tray_icon.addFile(_icon_path, QSize(32, 32))
        _tray_icon.addFile(_icon_path, QSize(48, 48))
        self._tray = QSystemTrayIcon(_tray_icon, main_window)
        self._tray.setToolTip("米家 - MiHome for Windows")
        self._tray.activated.connect(self._on_activated)

        # ---- 悬停数据弹窗（CUKTECH 充电器四口功率 + 总功率） ----
        # 托盘图标位于系统 Shell 区域，Qt 收不到其 enter/leave 事件；
        # 以低频轮询光标是否落在 QSystemTrayIcon.geometry() 内判定悬停，
        # 连续 _HOVER_DELAY_MS 才弹出，避免扫过任务栏即弹
        self._hover_popup = CuktechHoverPopup()
        self._hover_timer = QTimer(main_window)
        self._hover_timer.setInterval(150)
        self._hover_timer.timeout.connect(self._check_hover)
        self._hover_timer.start()
        self._hover_since_ms: float | None = None  # None=当前不在悬停
        # 弹窗可见期间兜底拉状态；SSE 推送活络时由 main_window 喂数，
        # 轮询照跑但读数幂等，代价可忽略
        self._popup_poll_timer = QTimer(main_window)
        self._popup_poll_timer.setInterval(_POPUP_POLL_MS)
        self._popup_poll_timer.timeout.connect(self._poll_popup_status)
        self._popup_poll_in_flight = False

        menu = QMenu()
        menu.setObjectName("appMenu")
        act_show = QAction("显示主窗口", menu)
        act_show.triggered.connect(self._show_main)
        menu.addAction(act_show)
        act_manage = QAction("管理托盘设备", menu)
        act_manage.triggered.connect(self._emit_manage)
        menu.addAction(act_manage)
        act_settings = QAction("设置", menu)
        act_settings.triggered.connect(self._on_settings)
        menu.addAction(act_settings)
        menu.addSeparator()
        act_quit = QAction("退出", menu)
        act_quit.triggered.connect(self._quit)
        menu.addAction(act_quit)
        self._tray.setContextMenu(menu)

        # 快捷窗口的信号接线统一在 _create_quick_window 内做一次，
        # 此处重复连接曾导致每次触发弹两次对话框

        # 托盘图标常驻，无需可用性检查也尝试显示
        self._tray.show()
        # 托盘图标跟系统配色（任务栏底色由系统决定），与应用主题设置无关
        from PySide6.QtGui import QGuiApplication
        self.apply_system_icon_theme(
            QGuiApplication.styleHints().colorScheme())

    def apply_system_icon_theme(self, scheme) -> None:
        """按系统配色切换托盘图标：浅色任务栏用深色图形，反之亦然。

        scheme 接受 Qt.ColorScheme 或 \"dark\"/\"light\" 字符串。
        """
        from PySide6.QtCore import Qt

        if isinstance(scheme, str):
            is_light = scheme == "light"
        else:
            is_light = scheme == Qt.ColorScheme.Light
        if is_light:
            icon_file = "app/ui/tray_icon_light.png"
        else:
            icon_file = "app/ui/tray_icon.png"
        from app import resource_path
        path = str(resource_path(icon_file))
        icon = QIcon(path)
        icon.addFile(path, QSize(16, 16))
        icon.addFile(path, QSize(32, 32))
        icon.addFile(path, QSize(48, 48))
        self._tray.setIcon(icon)

    def _create_quick_window(self) -> None:
        """创建快捷窗口并接线；主题切换时整窗重建复用。"""
        self._quick = TrayQuickWindow(self._service, self._jobs, None)
        self._quick.manage_requested.connect(self._on_manage)
        self._quick.open_device_requested.connect(self._on_open_device)
        self._quick.open_main_requested.connect(self._show_main)

    def _emit_manage(self) -> None:
        # 经方法转发而非构造期绑定信号：retheme 重建窗口后菜单仍指向新窗口
        self._quick.manage_requested.emit()

    def _on_activated(self, reason) -> None:
        if reason != QSystemTrayIcon.ActivationReason.Trigger:  # 仅左键单击
            return
        quick = self._quick
        # 正在播放呼出/隐藏动画时，终止旧动画并以带动画方式切换，避免直接 hide 丢失关闭动画
        if quick.is_animating():
            quick.abort_toggle_animation()
            if quick.isVisible() or quick.is_explicitly_visible():
                quick.hide_animated()
            else:
                self._show_quick_with_status()
            return
        if quick.isVisible():
            quick.hide_animated()
        else:
            self._show_quick_with_status()

    def _show_quick_with_status(self) -> None:
        """呼出快捷窗口并兜底拉一次 CUKTECH 快照。

        SSE 推送仅在主窗口启动流后才活络；主窗口隐藏（常驻托盘）时
        流仍在，但快照兜底保证冷启动/流未建时大卡也有读数。
        """
        self._jobs.submit(
            self._service.cuktech_status,
            on_success=self._quick.set_cuktech_status,
            on_error=lambda _e: None,
        )
        self._quick.show_near_tray()

    def _show_main(self) -> None:
        self._main.show()
        self._main.raise_()
        self._main.activateWindow()
        if self._main.isMinimized():
            self._main.showNormal()

    def _quit(self) -> None:
        self._tray.hide()
        # 标记为强制退出，避免 closeEvent 拦截为隐藏到托盘
        self._main.request_force_quit()
        self._main.close()
        # quitOnLastWindowClosed 为 False 时需显式退出事件循环
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _on_manage(self) -> None:
        self._main.show_tray_manager()

    def _on_settings(self) -> None:
        self._main.show_settings()

    def _on_open_device(self, did: str) -> None:
        # 仅打开设备详情，不呼出主窗口，避免主窗口隐藏时卡死
        self._quick.hide()
        # 直接创建详情对话框，parent 设为 None 使其独立于主窗口显隐
        dev = next((d for d in self._main.devices() if d.did == did), None)
        if dev is None:
            return
        from app.ui.device_dialog import DeviceDetailDialog
        if dev.source == "local" and dev.is_cuktech:
            # CUKTECH 充电器不经米家 spec 工作台（service._get_device
            # 对本地设备无 spec 会报错），与主窗口 _on_open_device 同款：
            # 经 panel_factory 装专用面板并启动 SSE 实时流
            def _make_cuktech_panel(parent):
                from app.ui.cuktech_panel import CuktechPanel
                return CuktechPanel(self._service, self._jobs, dev, parent)

            dialog = DeviceDetailDialog(
                self._service, self._jobs, dev, None,
                panel_factory=_make_cuktech_panel)
            try:
                self._main._maybe_start_cuktech_stream()
                self._main._open_cuktech_panels.append(dialog.panel)
            except AttributeError:
                pass
        else:
            dialog = DeviceDetailDialog(self._service, self._jobs, dev, None)
        dialog.load()
        dialog.exec()
        if dev.source == "local" and dev.is_cuktech:
            # 详情关闭：面板移出 SSE 推送投递名单（主窗口侧清理死引用）
            try:
                self._main._open_cuktech_panels.remove(dialog.panel)
            except (AttributeError, ValueError):
                pass
        dialog.deleteLater()

    def set_devices(self, devices: list[DeviceInfo], known_power: dict[str, bool | None]) -> None:
        self._quick.set_devices(devices, known_power)

    def set_metrics(self, metrics: dict[str, str | None]) -> None:
        self._quick.set_metrics(metrics)
        if self._pending_show:
            # 重建前窗口正显示着：设备填充完毕后再呼出，避免空窗口闪现
            self._pending_show = False
            self._quick.show_near_tray()

    def retheme(self) -> None:
        """主题切换：整个快捷窗口重建。

        快捷窗口的内联样式散布在窗框/音频栏/语音条/设备行/工具条
        多处，逐控件补样式总有遗漏（曾反复出现新旧色混杂残留）；
        整窗重建让全部样式天然取新调色板。设备与开关状态由主窗口
        在本调用之后立刻 set_devices 推送，显示状态在此登记。
        """
        was_visible = self._quick.isVisible() or self._quick.is_explicitly_visible()
        self._quick.abort_toggle_animation()
        self._quick.hide()
        self._quick.deleteLater()
        self._create_quick_window()
        self._pending_show = was_visible
        # 悬停弹窗样式集中、控件引用固定，直接原窗重刷
        self._hover_popup.retheme()

    def hide_quick(self) -> None:
        self._quick.hide()

    # ---------- 悬停数据弹窗（CUKTECH 充电器） ----------

    def _check_hover(self) -> None:
        """低频轮询光标是否落在托盘图标几何内；连续停留达阈值才弹窗。

        图标几何以全局屏幕坐标为准（QSystemTrayIcon.geometry 常为空
        rect 的环境直接静默，不弹也无害）；快捷窗口已呼出或弹窗正在
        显示时跳过新的弹出判定。弹窗展示期间光标离开图标不隐藏——
        用户正移入弹窗读数，点击别处/失焦由 ToolTip 窗口本身无焦点
        特性兜底，靠 _hide_hover_popup 的定时复核收尾。
        """
        if self._tray is None or not self._tray.isVisible():
            self._hover_since_ms = None
            return
        quick_open = self._quick.isVisible() or self._quick.is_explicitly_visible()
        if quick_open and not self._hover_popup.isVisible():
            self._hover_since_ms = None
            return
        geo = self._tray.geometry()
        entered = (
            not geo.isNull()
            and geo.contains(QCursor.pos())
        )
        if entered:
            now_ms = time.monotonic() * 1000.0
            if self._hover_since_ms is None:
                self._hover_since_ms = now_ms
                return
            if (now_ms - self._hover_since_ms >= _HOVER_DELAY_MS
                    and not self._hover_popup.isVisible()
                    and not self._popup_poll_timer.isActive()):
                self._show_hover_popup()
        else:
            self._hover_since_ms = None
            if self._hover_popup.isVisible():
                # 弹窗已出现：光标离开图标后再离开「图标+弹窗」联合区域
                # 才收，允许从图标移进弹窗读数
                union = self._hover_popup.frameGeometry().united(geo)
                if not union.contains(QCursor.pos()):
                    self._hide_hover_popup()

    def _show_hover_popup(self) -> None:
        """在托盘图标下方弹出数据弹窗并开始状态拉取。"""
        geo = self._tray.geometry()
        popup = self._hover_popup
        popup.adjustSize()
        if geo.isNull():
            from PySide6.QtGui import QGuiApplication
            screen = QGuiApplication.primaryScreen()
            if screen is None:
                return
            avail = screen.availableGeometry()
            x = avail.right() - popup.width() - 16
            y = avail.bottom() - popup.height() - 48
            pos = QPoint(max(avail.left(), x), max(avail.top(), y))
        else:
            pos = QPoint(
                geo.center().x() - popup.width() // 2, geo.bottom() + 4)
        popup.move(pos)
        popup.show()
        self._popup_poll_timer.start()
        self._poll_popup_status()

    def _hide_hover_popup(self) -> None:
        self._hover_popup.hide()
        self._popup_poll_timer.stop()
        self._hover_since_ms = None

    def _poll_popup_status(self) -> None:
        """弹窗可见期的状态兜底拉取（与 SSE 推送共存，读数幂等）。"""
        if self._popup_poll_in_flight:
            return
        self._popup_poll_in_flight = True
        self._jobs.submit(
            self._service.cuktech_status,
            on_success=self._apply_popup_status,
            on_error=lambda _e: self._apply_popup_status(None),
        )

    def _apply_popup_status(self, status: dict | None) -> None:
        self._popup_poll_in_flight = False
        if status is not None:
            self._hover_popup.set_status(status)

    def push_cuktech_status(self, payload: dict) -> None:
        """主窗口 SSE status 事件转发入口（实时刷新悬停弹窗与快捷窗口）。"""
        self._quick.set_cuktech_status(payload)
        if self._hover_popup.isVisible():
            self._hover_popup.set_status(payload)

    def push_cuktech_port(self, payload: dict) -> None:
        """主窗口 SSE port_update 事件转发入口（单口增量）。"""
        self._quick.push_cuktech_port(payload)
        if self._hover_popup.isVisible():
            self._hover_popup.push_port_update(payload)

    def set_tray_visible(self, visible: bool) -> None:
        """按设置开关托盘图标显隐：开启时常驻图标可见，关闭时隐藏。"""
        try:
            self._tray.setVisible(visible)
        except Exception:
            pass

    def is_available(self) -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

