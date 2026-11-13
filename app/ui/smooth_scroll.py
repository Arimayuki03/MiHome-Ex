# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""全局平滑滚动。

原生 QScrollArea 的滚轮是逐条跳变，视觉上像抽帧。这里在应用层统一
拦截滚轮事件，用缓动动画推进滚动条，让所有列表/页面获得一致的线性滚动
手感；滚动条本身的拖动与键盘翻页不受影响。
"""

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QEvent,
    QObject,
    QPropertyAnimation,
)
from PySide6.QtWidgets import QAbstractScrollArea


class SmoothScrollFilter(QObject):
    """把滚轮的单格跳变转成短促的缓动滚动。"""

    # 动画时长与单格像素步长：太短仍有抽帧感，太长会拖泥带水
    _DURATION_MS = 240
    _STEP_PX = 110

    def __init__(self, app):
        super().__init__(app)
        # 滚动条 -> 动画；同一控件连续滚动时复用动画对象
        self._anims: dict[int, QPropertyAnimation] = {}
        app.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() != QEvent.Type.Wheel:
            return False
        area = self._scroll_area(obj)
        if area is None:
            return False
        bar = area.verticalScrollBar()
        if bar is None or bar.maximum() <= bar.minimum():
            return False
        # 鼠标滚轮给的是 120 的整数倍角度增量；触摸板给的是像素增量，
        # 两条路径都要接住，否则触摸板仍是原生跳变
        angle = event.angleDelta().y()
        pixel = event.pixelDelta().y()
        if angle:
            step = int(angle / 120.0 * self._STEP_PX)
        elif pixel:
            step = pixel
        else:
            return False

        # 连续滚动时以上一段动画的终点为基准：用滚动条当前值会把
        # 还没走完的行程吃掉，连滚几格就会明显落后
        base = bar.value()
        running = self._anims.get(id(bar))
        if running is not None and running.state() == QAbstractAnimation.State.Running:
            base = running.endValue()
        target = max(bar.minimum(), min(bar.maximum(), base - step))
        if target == base:
            # 已经到边界，吞掉事件避免空跑动画
            return True
        self._animate(bar, target)
        return True

    @staticmethod
    def _scroll_area(obj):
        """滚轮事件落在 viewport 上，向上找到它所属的滚动区。"""
        node = obj
        while node is not None:
            if isinstance(node, QAbstractScrollArea):
                return node
            # 事件源不一定是 widget，取不到父控件就停
            node = node.parentWidget() if hasattr(node, "parentWidget") else None
        return None

    def _animate(self, bar, target: int) -> None:
        self._drop_dead()
        key = id(bar)
        anim = self._anims.get(key)
        if anim is None or anim.targetObject() is not bar:
            anim = QPropertyAnimation(bar, b"value", self)
            anim.setDuration(self._DURATION_MS)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._anims[key] = anim
        anim.stop()
        anim.setStartValue(bar.value())
        anim.setEndValue(target)
        anim.start()

    def _drop_dead(self) -> None:
        """滚动条随页面销毁后清掉对应动画，避免字典越攒越大。"""
        dead = [key for key, anim in self._anims.items()
                if anim.targetObject() is None]
        for key in dead:
            self._anims.pop(key, None)
