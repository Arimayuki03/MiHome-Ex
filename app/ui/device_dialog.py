# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Ex: 米家设备的 Windows 桌面控制端（扩展版）
# Copyright (C) 2026 MiHome-Ex contributors
"""设备详情抽屉：与添加功能同款的侧滑抽屉。

不再作为独立窗口弹出，而是以遮罩 + 右侧面板的形式覆盖在主窗口
之上，观感与添加功能抽屉一致；内部默认装载 WorkbenchPanel，
panel_factory 非空时改由其构造面板内容（CUKTECH 充电器等专用
面板共用同一遮罩壳），面板需提供 show_device/retheme 接口。

壳尺寸：面板可声明类特性 ``PREFERS_LARGE_DIALOG = True``（如
CuktechPanel，内容高需 920×760 大壳）；未声明的面板维持通用
900×640。尺寸在 resizeEvent 统一收敛：屏幕过窄/过矮时自适应收缩，
小屏（如 1.5× 缩放的 768 高笔记本）不超过可用几何。
"""

from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout

from app.core.jobs import JobExecutor
from app.core.models import DeviceInfo
from app.core.service import MijiaService
from app.ui.overlay_dialog import OverlayDialog
from app.ui.workbench_panel import WorkbenchPanel

# 通用详情壳尺寸与专用面板大壳尺寸（专用面板按 PREFERS_LARGE_DIALOG
# 特性声明；屏高不足 820 时自动回落通用尺寸，避免小屏溢出）
_DIALOG_SIZE = (900, 640)
_DIALOG_SIZE_LARGE = (920, 760)


class DeviceDetailDialog(OverlayDialog):
    def __init__(self, service: MijiaService, jobs: JobExecutor,
                 device: DeviceInfo, parent=None, panel_factory=None):
        super().__init__(parent)
        self.device = device

        outer = QVBoxLayout(self._panel)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)

        # 顶部标题栏：设备信息已由工作台内部展示，此处仅保留关闭按钮
        header = QHBoxLayout()
        header.addStretch(1)
        header.addWidget(self._make_close_button())
        outer.addLayout(header)

        # 工作台：panel_factory 提供专用面板（如 CUKTECH 充电器）
        if panel_factory is not None:
            self._workbench = panel_factory(self._panel)
        else:
            self._workbench = WorkbenchPanel(service, jobs, self._panel)
        outer.addWidget(self._workbench, stretch=1)

    @property
    def panel(self):
        return self._workbench

    def load(self) -> None:
        self._workbench.show_device(self.device.did, online=self.device.online, device=self.device)

    def retheme(self) -> None:
        """主题切换：面板底色 + 工作台头部按钮与功能区块重建。"""
        super().retheme()
        self._workbench.retheme()

    def _dialog_size(self) -> tuple[int, int]:
        """壳尺寸：面板声明大壳特性且屏幕装得下才放大（小屏回落通用）。"""
        pw, ph = _DIALOG_SIZE
        if getattr(self._workbench, "PREFERS_LARGE_DIALOG", False):
            pw, ph = _DIALOG_SIZE_LARGE
        from PySide6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            # 屏幕放不下大壳（含 40 边距）时整体回落通用壳
            if (pw, ph) == _DIALOG_SIZE_LARGE and (
                    geo.height() < ph + 40 or geo.width() < pw + 40):
                pw, ph = _DIALOG_SIZE
        return pw, ph

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._place_overlay()
        # 居中面板：按面板特性取尺寸，窗口过窄时自适应收缩
        pw, ph = self._dialog_size()
        pw = min(pw, max(320, self.width() - 40))
        ph = min(ph, self.height() - 40)
        x = (self.width() - pw) // 2
        y = (self.height() - ph) // 2
        self._panel.setGeometry(x, y, pw, ph)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._fill_parent_window():
            self.raise_()
            self._fade_in()
            return
        # 托盘独立弹出：铺满可用屏幕（遮罩盖住全屏，避免只罩一小块、
        # 周围露出桌面壁纸的突兀观感），面板由 resizeEvent 居中放置
        from PySide6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            self.setGeometry(screen.availableGeometry())
        self.raise_()
        self._fade_in()


