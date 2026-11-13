# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Ex: 米家设备的 Windows 桌面控制端（扩展版）
# Copyright (C) 2026 MiHome-Ex contributors
"""扫码登录对话框。

上游的登录二维码只往终端打印 ASCII 字符，图形界面无法直接复用，
因此这里用适配层拆出的两步流程：先取 loginUrl 渲染成图片展示，
再在后台长轮询等待扫码结果。

窗口沿用应用统一的扁平化观感：无边框圆角面板、半透明遮罩、淡入淡出；
二维码单独放在白色卡片里，扫码识别所需的黑白对比度不随主题变化。
"""

import io
import logging
import time

import qrcode
import qtawesome as qta
from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QFont, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from app.ui.overlay_dialog import OverlayDialog
from app.ui.si_theme import (
    PRIMARY_TEXT,
    SiColors,
    primary_button_qss,
)

logger = logging.getLogger(__name__)

# 取二维码的超时上限：正常 1~5 秒，慢网十几秒。超过这个时间仍未拿到数据，
# 说明网络被墙/代理失效/链路异常，必须给用户一句话和重试按钮，
# 而不是让「正在获取登录二维码…」一直转下去（曾有版本在这里无限转圈，
# 用户完全无法判断是网慢还是卡死）。
_QR_TIMEOUT_MS = 40_000

# 面板宽度固定、高度随内容自适应：重试按钮显隐时不留与内容无关的空洞
_PANEL_W = 400
_QR_CARD = 312
_QR_SIZE = 272

# 二维码必须保持白底黑码：卡片底色、描边与占位图标不跟主题走，
# 否则浅色主题下黑底白码、深色主题下灰底都会直接扫不出来
_QR_CARD_BG = "#ffffff"
_QR_CARD_BORDER = "rgba(0, 0, 0, 0.06)"
_QR_PLACEHOLDER = "#c9cbd2"


class LoginDialog(OverlayDialog):
    """模态登录窗：三态切换 获取中 -> 待扫码 -> 失败可重试。

    凭据仍有效时上游直接返回免登录结论，本窗口立即关闭。
    """

    def __init__(self, service, jobs, parent=None):
        super().__init__(parent, modal=Qt.WindowModality.ApplicationModal)
        self._service = service
        self._jobs = jobs
        # 登录数据要在 begin 与 wait 两次调用间传递
        self._login_data = None
        # 登录轮次：串行队列无法取消任务，「重新获取」后旧长轮询
        # （最长阻塞两分钟）完成时必须被丢弃，否则会用过期凭据误关窗口
        self._generation = 0
        # 本轮开始时刻（monotonic），用于把"取二维码花了多久"写进日志
        self._started_at = 0.0
        # 状态文字随主题重绘，缓存原文与语义级别
        self._status_text = ""
        self._status_error = False
        # 二维码原图：标签尺寸变化时按新尺寸重采样，避免放大后发糊
        self._qr_pixmap = None

        self.setWindowTitle("登录米家账号")

        # 登录窗关掉就等于退出程序，遮罩误点不该触发退出：
        # 只保留右上角关闭按钮这一条明确路径
        if self._overlay is not None:
            self._overlay.mousePressEvent = lambda _e: None

        panel = self._panel
        panel.setFixedWidth(_PANEL_W)

        lay = QVBoxLayout(panel)
        lay.setContentsMargins(24, 18, 24, 18)
        lay.setSpacing(12)

        # ---- 标题栏：图标 + 品牌名 + 副标题 + 关闭按钮 ----
        head = QHBoxLayout()
        head.setSpacing(10)

        self._icon = QLabel()
        self._icon.setFixedSize(30, 30)
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        head.addWidget(self._icon)

        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        self._brand = QLabel("米家 - Windows")
        self._brand.setFont(QFont("Microsoft YaHei UI", 13, QFont.Weight.DemiBold))
        title_col.addWidget(self._brand)
        self._subtitle = QLabel("扫码登录")
        title_col.addWidget(self._subtitle)
        head.addLayout(title_col, stretch=1)
        head.addWidget(self._make_close_button(),
                       alignment=Qt.AlignmentFlag.AlignTop)
        lay.addLayout(head)

        # ---- 二维码卡片：白底独立于主题，保证任何配色下都能扫 ----
        self._qr_card = QFrame()
        self._qr_card.setObjectName("qrCard")
        self._qr_card.setFixedSize(_QR_CARD, _QR_CARD)
        card_lay = QVBoxLayout(self._qr_card)
        card_lay.setContentsMargins(20, 20, 20, 20)
        self._qr_label = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self._qr_label.setFixedSize(_QR_SIZE, _QR_SIZE)
        card_lay.addWidget(self._qr_label,
                           alignment=Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._qr_card, alignment=Qt.AlignmentFlag.AlignHCenter)

        # ---- 状态文字：最小高度保证单行态不跳动，长错误信息可换行撑高 ----
        self._status_label = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self._status_label.setWordWrap(True)
        self._status_label.setMinimumHeight(38)
        lay.addWidget(self._status_label)

        # ---- 重试按钮 ----
        self._retry_btn = QPushButton(" 重新获取二维码")
        self._retry_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._retry_btn.setIconSize(QSize(16, 16))
        self._retry_btn.setFixedHeight(36)
        self._retry_btn.clicked.connect(self._begin_login)
        self._retry_btn.hide()
        lay.addWidget(self._retry_btn,
                      alignment=Qt.AlignmentFlag.AlignHCenter)

        # 取二维码的看门狗：超时就把"一直转圈"换成明确提示 + 重试
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.setInterval(_QR_TIMEOUT_MS)
        self._watchdog.timeout.connect(self._on_qr_timeout)

        self._apply_styles()
        self._panel.adjustSize()

    def exec_and_wait(self) -> int:
        """启动登录流程并阻塞至完成或用户关闭窗口。"""
        self._begin_login()
        return self.exec()

    # ---------- 流程编排 ----------

    def _begin_login(self) -> None:
        self._generation += 1
        generation = self._generation
        self._set_retry_visible(False)
        self._set_status("正在获取登录二维码…", error=False)
        self._show_placeholder()
        self._started_at = time.monotonic()
        logger.info("登录: 开始获取二维码 (第 %d 轮)", generation)
        self._watchdog.start()
        self._jobs.submit(
            self._service.qr_login_begin,
            on_success=lambda data: self._on_qr_data(data, generation),
            on_error=lambda e: self._on_failed(e, generation),
        )

    def _on_qr_timeout(self) -> None:
        """看门狗：这么久还没拿到二维码，说明网络侧出了问题。"""
        # 窗口已关闭时看门狗可能才到期，不该再去改一个隐藏面板的状态
        if not self.isVisible():
            return
        logger.warning("登录: %d 秒内未取到二维码，判定网络异常", _QR_TIMEOUT_MS // 1000)
        self._set_status(
            f"获取二维码超时（已等 {_QR_TIMEOUT_MS // 1000} 秒）。"
            "请检查网络是否正常、代理是否可用，然后点击下方按钮重试。",
            error=True,
        )
        self._set_retry_visible(True)

    def _on_qr_data(self, data: dict | None, generation: int) -> None:
        elapsed = time.monotonic() - self._started_at
        self._watchdog.stop()
        logger.info("登录: 第 %d 轮取到登录数据（%.1fs）", generation, elapsed)
        if generation != self._generation or not self.isVisible():
            logger.info(
                "登录: 第 %d 轮结果已过期被丢弃（当前轮 %d，窗口可见=%s）",
                generation, self._generation, self.isVisible())
            return
        if data is None:
            # 本地凭据自动刷新成功，无需扫码
            logger.info("登录: 本地凭据刷新成功，无需扫码")
            self.accept()
            return
        self._login_data = data
        try:
            self._show_qr_image(data["loginUrl"])
        except Exception as exc:
            # 渲染失败必须显性化：否则界面停在「正在获取二维码…」，
            # 用户只会看到一直转圈，既不知道原因也没法重试
            logger.exception("登录: 二维码渲染失败")
            self._set_status(f"二维码生成失败：{exc}（可点击下方按钮重试）", error=True)
            self._set_retry_visible(True)
            return
        logger.info("登录: 二维码已显示（%.1fs）", time.monotonic() - self._started_at)
        self._set_status("请使用米家 APP 扫描二维码", error=False)
        self._jobs.submit(
            lambda: self._service.qr_login_wait(self._login_data),
            on_success=lambda _: self._on_login_success(generation),
            on_error=lambda e: self._on_failed(e, generation),
        )

    def _on_login_success(self, generation: int) -> None:
        if generation != self._generation or not self.isVisible():
            return
        logger.info("登录: 扫码成功，凭据已保存")
        self.accept()

    def _on_failed(self, error: Exception, generation: int) -> None:
        self._watchdog.stop()
        logger.warning("登录: 第 %d 轮失败: %s", generation, error)
        if generation != self._generation or not self.isVisible():
            return
        self._set_status(str(error), error=True)
        self._set_retry_visible(True)

    # ---------- 绘制 ----------

    def _show_qr_image(self, login_url: str) -> None:
        qr = qrcode.QRCode(border=2, box_size=6)
        qr.add_data(login_url)
        image = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        pixmap = QPixmap()
        if not pixmap.loadFromData(buffer.getvalue()):
            # 解码失败必须显性化，否则卡片会静默变成一片空白
            raise ValueError("二维码图片解码失败")
        self._qr_pixmap = pixmap
        self._apply_qr_pixmap()

    def _apply_qr_pixmap(self) -> None:
        """按标签当前尺寸重采样二维码：窗口缩放后依然保持清晰。"""
        if self._qr_pixmap is None:
            return
        self._qr_label.setPixmap(
            self._qr_pixmap.scaled(
                self._qr_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _show_placeholder(self) -> None:
        """加载态：白卡片里放淡色二维码图标，避免出现一块纯白空洞。"""
        self._qr_pixmap = None
        self._qr_label.setPixmap(
            qta.icon("mdi.qrcode", color=_QR_PLACEHOLDER).pixmap(96, 96))

    def _set_status(self, text: str, error: bool) -> None:
        self._status_text = text
        self._status_error = error
        self._status_label.setText(text)
        color = SiColors.ERROR_TEXT if error else SiColors.TEXT_SECONDARY
        self._status_label.setStyleSheet(
            f"color: {color}; background: transparent; font-size: 9pt;")
        # 文案长度会改变标签高度，面板跟着重算并重新居中
        self._panel.adjustSize()
        self._center_panel()

    # ---------- 样式 ----------

    def _apply_styles(self) -> None:
        """主题相关内联样式：构造与 retheme 共用。"""
        self._icon.setPixmap(
            qta.icon("mdi.qrcode-scan", color=SiColors.THEME).pixmap(26, 26))
        self._icon.setStyleSheet("background: transparent;")
        self._brand.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")
        self._subtitle.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY}; background: transparent;"
            " font-size: 9pt;")
        # ID 选择器限定在卡片本身：QLabel 也继承 QFrame，
        # 用类型选择器会给二维码标签再套一层白底和描边
        self._qr_card.setStyleSheet(
            f"QFrame#qrCard {{ background: {_QR_CARD_BG};"
            f" border: 1px solid {_QR_CARD_BORDER};"
            " border-radius: 14px; }")
        self._retry_btn.setStyleSheet(primary_button_qss("8px 20px"))
        # 主按钮文字固定白色，图标也要同色；ON_THEME_TEXT 在浅色强调色下是深色
        self._retry_btn.setIcon(qta.icon("mdi.refresh", color=PRIMARY_TEXT))
        self._set_status(self._status_text, self._status_error)

    def retheme(self) -> None:
        super().retheme()
        self._apply_styles()

    # ---------- 布局 ----------

    def _center_panel(self) -> None:
        x = (self.width() - self._panel.width()) // 2
        y = (self.height() - self._panel.height()) // 2
        self._panel.move(x, y)

    def _set_retry_visible(self, visible: bool) -> None:
        """重试按钮只在失败态出现；显隐后重算面板高度并重新居中，
        避免底部残留一条与内容无关的空白。"""
        shown = not self._retry_btn.isHidden()
        if shown == visible:
            return
        self._retry_btn.setVisible(visible)
        self._panel.adjustSize()
        self._center_panel()

    def done(self, result) -> None:  # noqa: N802
        # 关窗后看门狗不该再触发：它改的是已隐藏面板的状态
        self._watchdog.stop()
        super().done(result)

    def resizeEvent(self, event) -> None:  # noqa: N802
        # 遮罩铺满与淡入由 OverlayDialog 处理，这里只负责面板居中
        super().resizeEvent(event)
        self._place_overlay()
        self._center_panel()
        self._apply_qr_pixmap()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._fill_parent_window():
            self.raise_()
            self._fade_in()
            return
        # 无可见父窗口（托盘路径）：铺满可用屏幕
        from PySide6.QtGui import QGuiApplication

        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            self.setGeometry(screen.availableGeometry())
        self.raise_()
        self._fade_in()
