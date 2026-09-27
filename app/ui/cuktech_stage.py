# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Ex: 米家设备的 Windows 桌面控制端（扩展版）
# Copyright (C) 2026 MiHome-Ex contributors
"""CUKTECH 充电器观感件三件套：设备渲染舞台、端口卡网格、功率占比条。

仿上游 cuktech-ble-server/web 前端（index.html/phone.html）的视觉件，
坐标与素材逐像素对齐原始设计；三个组件全部纯展示、零网络请求，数据
由调用方喂入（update_state 对应 SSE init/status 整帧，update_port 对应
SSE port_update 单口增量）。取色一律 SiColors 动态代理（retheme 重求值
内联样式即可）；素材图片不随主题换底图（上游 index 版行为：充电中底
图固定用 dark 一张，index.html:149）；素材定位经 app.resource_path
兼容 Nuitka 打包。

坐标纪律（勿改）：设备舞台以上游 .device-box 的 360×481 盒内坐标为
唯一真源（index.html:151-166 内联 top:36/68/100/133px + pbg/pval 偏
移，绘制时统一经 _to_canvas 下沉 51.5px 转可见画布）——两张功率条素
材的"环"是照着 360px 宽的设备图调的，自行换算百分比一定对不齐（盒内
坐标已用素材 alpha 包围盒逐像素核对：PIL 实测四口蓝环中心与条中心逐
口吻合，见 _BAR_Y 注释）。整块按 min(w/360, h/328) 等比缩放居中，任
何控件尺寸下不裁切不变形。

模块分节：
- 常量与端口元数据（PORT_COLORS 四口身份色抄自上游 index.css:34-37）
- DeviceStageWidget —— 设备渲染舞台（unconnected/充电中底图 + 径向
  光晕 + 逐口功率条叠加 + 场景徽标 + 上下浮动动画）
- PortCardGrid —— 2×2 端口卡网格（小图标随开关切换 + 大功率读数 +
  负载条 + V·A/协议徽标行 + themed_switch 开关；点卡片发 port_clicked，
  点开关发 port_toggle，增量更新不重建卡）
- MiniPhoneShareBar —— 四色堆叠占比条 + 标签行（每口 W 与占比%，仿
  上游 cardPortShare）
"""

from PySide6.QtCore import (
    QEasingCurve,
    QObject,
    Property,
    QPropertyAnimation,
    QRectF,
    QPointF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app import resource_path
from app.ui.si_theme import SiColors, themed_switch

# ----------------------------------------------------------------------------
# 常量与端口元数据
# ----------------------------------------------------------------------------

_ASSET_DIR = "app/ui/assets/cuktech"

# 舞台逻辑坐标系 = 上游 .device-box 缩放盒原始 360×481（index.css:698-706）：
# 可见画布（.device-frame 360×328，index.css:693-697）相对盒顶**下沉 51.5px**
# （.device-box top:51.5px）。历史版把画布当成盒坐标原点、丢了这 51.5px，
# 功率条整体上飘 51.5 逻辑 px 与 USB 口错位——所有叠加元素一律存盒内
# 坐标（照抄 index.html 内联 px），绘制时统一 +51.5 转画布，勿再心算。
_STAGE_W = 360.0            # 舞台逻辑宽
_STAGE_H = 481.0            # .device-box 整高
_BOX_TOP = 51.5             # 盒相对可见画布的下沉量（index.css:701）
_CANVAS_H = 328.0           # 可见画布高（.device-frame，index.css:696）
_DEVICE_TOP = -128.0        # 充电中底图 top（上游 .device-img top:-128px）
_SRC_ASPECT = 985.0 / 1080.0  # 底图源高宽比（985/1080，3x 素材）
_GLOW_RADIUS = 130.0        # 光晕半径（上游 .dark-glow-ani 260px 直径）
_FLOAT_AMP = 4.0            # 浮动上浮幅度（上游 deviceFloat translateY(-4px)）
_SRC_SCALE = 3.0            # 素材相对逻辑坐标的超采样倍数（1080px 源 → 360 逻辑）

# unconnected 空载图（盒内系）：360×307（.unconnected-img，index.css:727），
# margin -37px 抬升可见内容中心与充电中对齐（index.css:732-735 注释）。
# 注意：-37 是 .device-box 盒内坐标（index.html 里该 img 是 device-box
# 子元素），转画布必须经 _to_canvas（+51.5 下沉）→ 画布 top=14.5。
# 历史版把 -37 当画布坐标直画，设备顶出画布上沿 6.3 逻辑 px——
# 布局把舞台压矮时内容顶格贴边、底部大片留白，视觉即"底部被裁"。
_UNCONNECTED_TOP = -37.0
_UNCONNECTED_H = 307.0

# 逐口功率条（盒内坐标 = .usb-overlay-ani left:134.5px + .usb-mod-ani 内联
# top 36/68/100/133 + .usb-pbg 内联偏移，index.html:151-166）。
# PIL 实测交叉验证（main_charger_dark_ad1204_all.png，src/3）：四口蓝色
# 环中心 y = 103.3/135.7/168/203.5 ↔ 条中心 103.5/135.5/167.5/203（逐口
# 吻合）；机身右缘 x=239.7 ↔ 条起点 151.5/147。
_BAR_X = {1: 151.5, 2: 151.5, 3: 151.5, 4: 147.0}
_BAR_Y = {1: 34.0, 2: 66.0, 3: 98.0, 4: 131.5}
_BAR_SIZE = {1: (220.0, 36.0), 2: (220.0, 36.0), 3: (220.0, 36.0),
             4: (230.0, 40.0)}
_BAR_SRC = {
    1: "main_card_usb_c_rectangle.png",
    2: "main_card_usb_c_rectangle.png",
    3: "main_card_usb_c_rectangle.png",
    4: "main_card_usb_a_rectangle.png",
}
# 功率文本（.usb-pval）：overlay 内 left:140px（→ 盒内 x 274.5，机身右缘
# 239.7 外侧、条尾 371.5 内侧）+ top:4px + 70px 宽居中 + 17px 粗体
# （index.html:153 内联 + index.css:788-794）；行高 22 容 17px 字。
_PVAL_X = 274.5
_PVAL_DY = 4.0
_PVAL_W = 70.0
_PVAL_H = 22.0

# 场景徽标（盒内系）：.scene-badge-ani top:-40px 相对 wrap-inner
# （index.css:767-779）→ 画布 y = -40 + 51.5 = 11.5
_BADGE_Y = -40.0
_BADGE_ICON = 20.0
_BADGE_H = 20.0
_BADGE_GAP = 5.0

# 充电中底图：index 版不随主题换（index.html:149 固定 dark 一张）
_IMG_CHARGING = "main_charger_dark_ad1204_all.png"
_IMG_UNCONNECTED = "main_card_ad1204u_unconnected.png"


def _asset(name: str) -> str:
    """素材绝对路径（Nuitka standalone 下数据文件在 exe 同级目录）。"""
    return str(resource_path(f"{_ASSET_DIR}/{name}"))


def _asset_pixmap(name: str) -> QPixmap:
    """加载素材原始位图（未标注 devicePixelRatio，调用方按需标注）。"""
    return QPixmap(_asset(name))


def _hi_dpi_pixmap(name: str, dpr: float) -> QPixmap:
    """按 devicePixelRatio 标注素材：源是 3x 超采样图，高分屏不糊。

    setDevicePixelRatio(3×dpr) 后，drawPixmap 的目标矩形用逻辑 px，
    Qt 自动取用整倍物理像素（offscreen/1x 屏取 3x=整图，2x 屏取 1.5x）。
    """
    pm = _asset_pixmap(name)
    if not pm.isNull():
        pm.setDevicePixelRatio(_SRC_SCALE * dpr)
    return pm


# 端口号 -> 展示名（与 cuktech_panel._PORT_LABELS 同源文案）
_PORT_NAMES: dict[int, str] = {1: "C1", 2: "C2", 3: "C3", 4: "USB-A"}
# 端口号 -> 素材 key（main_card_port_{key}_{on|off}.png）
_PORT_KEYS: dict[int, str] = {1: "c1", 2: "c2", 3: "c3", 4: "a"}
# 端口号 -> 满载功率（W）：负载条分母（上游 app.js PORT_MAX_W）
_PORT_MAX_W: dict[int, float] = {1: 120, 2: 120, 3: 44, 4: 33}

# 四口身份色：抄自上游 index.css:34-37（米家端口色，双主题同值）。
# 负载条填充、充电中卡片描边、占比条分段共用；亮端口色不作小字文字色。
PORT_COLORS: dict[int, str] = {
    1: "#FF7A00",  # --port-c1 橙
    2: "#46B4FF",  # --port-c2 蓝
    3: "#89D8F3",  # --port-c3 浅蓝
    4: "#FFD24B",  # --port-a  黄
}

# 充电器模式（PIID 5）-> 徽标（图标素材名 + 中文模式名）。
# 2 号徽标图标名是 apple（上游 app.js SCENE_BADGE_IMG），文字取 zh-CN 术语。
# 1 号素材本身即 "AI" 字样，文字留空避免重复绘制。
SCENE_BADGES: dict[int, tuple[str, str]] = {
    1: ("ai", ""),
    2: ("apple", "数码生态"),
    3: ("single", "单口"),
    4: ("balance", "均衡"),
}

_PORTS = (1, 2, 3, 4)


def _fmt_watts(value: float) -> str:
    return f"{float(value):.1f}W"


def _load_pct(power: float, port: int) -> int:
    """负载百分比：空载给 0；有输出钳制 2-99（满载也留 1% 圆角不被裁）。

    上游 app.js loadPct 同款：分母是本口上限 PORT_MAX_W，不是全端口合计。
    """
    if not power > 0:
        return 0
    max_w = _PORT_MAX_W.get(port, 100)
    return min(99, max(2, round(power / max_w * 100)))


def _sync_switch(switch, checked: bool) -> None:
    """程序化同步开关状态（setChecked 会发 toggled，必须屏蔽信号）。

    SiSwitchRefactor 的自绘进度与 checked 状态是两套存储，同步补齐
    （cuktech_panel._sync_switch 同款写法）。
    """
    switch.blockSignals(True)
    try:
        switch.setChecked(checked)
        switch.progress = 1.0 if checked else 0.0
        try:
            switch.progress_ani.setCurrentValue(1.0 if checked else 0.0)
        except Exception:
            pass
    finally:
        switch.blockSignals(False)


def _port_state(ports: dict, port: int) -> dict:
    """取单口状态 dict（缺键给安全默认值，渲染路径不炸）。"""
    entry = ports.get(str(port))
    if not isinstance(entry, dict):
        entry = ports.get(port)
    return entry if isinstance(entry, dict) else {}


def _total_watts(ports: dict) -> float:
    """totalW = Σ(enabled 且 power>0 的口)，上游 updateDeviceContainer 同款。"""
    total = 0.0
    for entry in ports.values():
        if isinstance(entry, dict) and entry.get("enabled", True) is not False:
            total += float(entry.get("power") or 0.0)
    return total


def _mix_color(base: str, mix: str, ratio: float) -> str:
    """sRGB 通道线性混色（上游 color-mix(in srgb, A ratio, B) 的近似）。"""
    a, b = QColor(base), QColor(mix)
    r = round(a.red() * ratio + b.red() * (1 - ratio))
    g = round(a.green() * ratio + b.green() * (1 - ratio))
    bl = round(a.blue() * ratio + b.blue() * (1 - ratio))
    return QColor(r, g, bl).name()


# ----------------------------------------------------------------------------
# DeviceStageWidget —— 设备渲染舞台
# ----------------------------------------------------------------------------


class _FloatAnimator(QObject):
    """浮动动画驱动：0 → -4 → 0 往复（QPropertyAnimation 需要标准属性）。"""

    def __init__(self, target: "DeviceStageWidget"):
        super().__init__(target)
        self._target = target
        self._value = 0.0

    def read(self) -> float:
        return self._value

    def write(self, value: float) -> None:
        self._value = float(value)
        if self._target is not None:
            self._target.update()

    # QPropertyAnimation 需要 Qt 属性系统可见的 Property
    value = Property(float, read, write)


class DeviceStageWidget(QWidget):
    """设备渲染舞台：unconnected 空载图 ↔ 充电中底图 + 光晕 + 功率条 + 徽标。

    仿上游 §3.1 决策树：totalW>0 显示充电中底图（浮动动画 + 蓝色径向
    光晕 + 逐口功率条 + 场景徽标），totalW==0 回到 unconnected 图并隐藏
    全部叠加层。整块按 360×481 逻辑坐标绘制，按容器宽度等比缩放（上游
    --device-scale 0.75/0.68 档的等价物：宽度变化自动 fit）。

    纯展示组件：update_state 整帧喂入、update_port 单口增量、set_scene
    切徽标；素材不随主题换底图，retheme 只需重绘（取色在 paint 时动态读）。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ports: dict = {}
        self._scene: int = 1
        self._charging: bool = False
        self._float_t: float = 0.0  # 浮动动画当前偏移（逻辑 px，负=上浮）

        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)

        # 浮动动画：上下 3s 循环（上游 deviceFloat 3s ease-in-out infinite）
        self._animator = _FloatAnimator(self)
        self._float_ani = QPropertyAnimation(self._animator, b"value", self)
        self._float_ani.setStartValue(0.0)
        self._float_ani.setKeyValueAt(0.5, -_FLOAT_AMP)
        self._float_ani.setEndValue(0.0)
        self._float_ani.setDuration(3000)
        self._float_ani.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._float_ani.setLoopCount(-1)

    # ---------- 数据注入 ----------

    def update_state(self, status: dict) -> None:
        """整帧注入（/api/status 或 SSE init/status）：重算 totalW 与场景徽标。"""
        ports = status.get("ports") or {}
        if not isinstance(ports, dict):
            ports = {}
        self._ports = dict(ports)
        self._refresh_charging()
        settings = status.get("settings") or {}
        try:
            mode = int(settings.get("5"))
        except (TypeError, ValueError):
            mode = 0
        if mode in SCENE_BADGES:
            self._scene = mode
        self.update()

    def update_port(self, port_id: int, port_data: dict) -> None:
        """单口增量（SSE port_update）：合并进缓存后只重算 totalW 与该口条。"""
        if not isinstance(port_data, dict):
            return
        ports = dict(self._ports)
        ports[str(port_id)] = dict(port_data)
        self._ports = ports
        self._refresh_charging()
        self.update()

    def set_scene(self, mode: int) -> None:
        """场景徽标切换（PIID 5：1=AI 2=数码生态 3=单口 4=均衡）。"""
        if mode in SCENE_BADGES:
            self._scene = mode
            self.update()

    def _refresh_charging(self) -> None:
        charging = _total_watts(self._ports) > 0
        if charging == self._charging:
            return
        self._charging = charging
        if charging:
            if self._float_ani.state() != QPropertyAnimation.State.Running:
                self._float_ani.start()
        else:
            self._float_ani.stop()
            self._float_t = 0.0
            self._animator.write(0.0)

    # ---------- 尺寸与缩放 ----------

    def _stage_scale(self) -> tuple[float, float, float, float]:
        """等比 fit:360×328 画布按 min(w/360, h/328) 缩放并在控件内居中。

        返回 (scale, offset_x, offset_y, dpr)。历史版只按宽度 fit,面板把
        舞台压窄到 360 以下时画布超宽被裁(用户截图:机身只剩右半)——
        高度也参与约束后,任何控件尺寸下画布完整可见、永不裁切变形。
        """
        w = max(float(self.width()), 1.0)
        h = max(float(self.height()), 1.0)
        scale = max(min(w / _STAGE_W, h / _CANVAS_H), 0.5)
        ox = (w - _STAGE_W * scale) / 2.0
        oy = (h - _CANVAS_H * scale) / 2.0
        return scale, ox, oy, self.devicePixelRatioF()

    def _to_canvas(self, x: float, y: float) -> tuple[float, float]:
        """盒内逻辑坐标(上游 .device-box 系)→ 可见画布坐标(下沉 51.5px)。"""
        return x, y + _BOX_TOP

    def height_for_width(self, width: int) -> int:
        """布局协作:给定宽度返回画布等比高度。"""
        return round(float(width) / _STAGE_W * _CANVAS_H)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 (Qt 命名约定)
        return True

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt 命名约定)
        return QSize(round(_STAGE_W), round(_CANVAS_H))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 (Qt 命名约定)
        return QSize(round(_STAGE_W * 0.5), round(_CANVAS_H * 0.5))

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        """宽度变化时把最小高度钉在当前宽度的等比高上。

        QGridLayout 只按子件 minimumSize(高 164)算行高下限,水平
        策略 Expanding + 垂直 Fixed 的 hfw 值不参与行收缩保护——面板
        双栏模式里右侧端口卡最小高 206 曾把舞台行压到 206(scale
        0.628),画布矮于 fit 值时内容贴顶、底部留白,视觉即"底部被
        裁"。这里把 minHeight 动态钉到 hfw(当前宽),行高恢复 303,
        任何面板宽高下画布完整可见。堆叠模式 setFixedSize(400,364)
        的 364 == hfw(400),钉值一致无冲突。
        """
        super().resizeEvent(event)
        self.setMinimumHeight(self.height_for_width(max(self.width(), 1)))

    def _ensure_min_height(self) -> None:
        """showEvent 前置补钉(首帧 resize 可能是 no-op 不触发 resizeEvent)。"""
        self.setMinimumHeight(self.height_for_width(max(self.width(), 1)))

    def showEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        super().showEvent(event)
        self._ensure_min_height()

    # ---------- 绘制 ----------

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            scale, ox, oy, dpr = self._stage_scale()
            painter.save()
            painter.translate(ox, oy)
            painter.scale(scale, scale)
            if self._charging:
                self._paint_charging(painter, dpr)
            else:
                self._paint_unconnected(painter, dpr)
            painter.restore()
        finally:
            painter.end()

    def _paint_glow(self, painter: QPainter, dy: float) -> None:
        """蓝色径向光晕(上游 .dark-glow-ani:rgba(70,180,255) 径向渐变,
        居中于 device-box:盒内 240.5 → 画布 292)。"""
        cy, _ = self._to_canvas(0.0, _STAGE_H / 2 + dy)
        center = QPointF(_STAGE_W / 2, cy)
        gradient = QRadialGradient(center, _GLOW_RADIUS)
        gradient.setColorAt(0.0, QColor(70, 180, 255, 38))
        gradient.setColorAt(0.4, QColor(70, 180, 255, 13))
        gradient.setColorAt(0.7, QColor(70, 180, 255, 0))
        gradient.setColorAt(1.0, QColor(70, 180, 255, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(gradient))
        painter.drawEllipse(center, _GLOW_RADIUS, _GLOW_RADIUS)

    def _paint_charging(self, painter: QPainter, dpr: float) -> None:
        """充电中:光晕 → 底图 → 徽标 → 功率条(全部随浮动偏移位移)。

        底图按上游 .device-img 的 object-fit:contain 语义绘制:1080×985
        源图(360×328.33 逻辑)放进 360×481 盒 → 等比缩放居中,盒内
        top = -128 + (481-328.33)/2 = -51.67;盒内坐标必须经 _to_canvas
        (+51.5 下沉)→ 画布 top=-0.17。历史版把盒内 -51.67 当画布坐标
        直画,整图上飘 51.5 逻辑 px:四口蓝环与功率条错位 51.5、机身
        顶部溢出画布被裁——PIL 实测环中心与条中心逐口吻合的结论只在
        "盒内 y"坐标系成立,画布绘制必须先下沉(见 _paint_port_bars)。
        """
        dy = self._animator.read()
        self._paint_glow(painter, dy)
        base = _hi_dpi_pixmap(_IMG_CHARGING, dpr)
        if not base.isNull():
            draw_h = _STAGE_W * _SRC_ASPECT          # 328.33 逻辑高
            draw_top, _ = self._to_canvas(
                0.0, _DEVICE_TOP + (_STAGE_H - draw_h) / 2.0 + dy)
            painter.drawPixmap(
                QRectF(0.0, draw_top, _STAGE_W, draw_h),
                base, QRectF(0.0, 0.0, base.width(), base.height()))
        self._paint_badge(painter, dpr, dy)
        self._paint_port_bars(painter, dpr, dy)

    def _paint_unconnected(self, painter: QPainter, dpr: float) -> None:
        """空载:unconnected 图(盒内 -37px 经 _to_canvas 下沉 → 画布 14.5)。"""
        pm = _hi_dpi_pixmap(_IMG_UNCONNECTED, dpr)
        if pm.isNull():
            return
        top, _ = self._to_canvas(0.0, _UNCONNECTED_TOP)
        painter.drawPixmap(
            QRectF(0.0, top, _STAGE_W, _UNCONNECTED_H),
            pm, QRectF(0.0, 0.0, pm.width(), pm.height()))

    def _paint_badge(self, painter: QPainter, dpr: float, dy: float) -> None:
        """场景徽标：设备图上方（图标 + 场景名，文字用主题前景墨）。"""
        icon_name, text = SCENE_BADGES.get(self._scene, SCENE_BADGES[1])
        icon = _hi_dpi_pixmap(f"main_card_scene_icon_{icon_name}.png", dpr)
        if icon.isNull():
            return
        y, _ = self._to_canvas(0.0, _BADGE_Y + dy)
        if not text:
            x = (_STAGE_W - _BADGE_ICON) / 2
            painter.drawPixmap(QRectF(x, y, _BADGE_ICON, _BADGE_ICON),
                               icon, QRectF(0.0, 0.0, icon.width(), icon.height()))
            return
        font = QFont("Microsoft YaHei UI", 9)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        text_w = metrics.horizontalAdvance(text)
        total_w = _BADGE_ICON + _BADGE_GAP + text_w
        x = (_STAGE_W - total_w) / 2
        painter.drawPixmap(QRectF(x, y, _BADGE_ICON, _BADGE_ICON),
                           icon, QRectF(0.0, 0.0, icon.width(), icon.height()))
        painter.setPen(QColor(SiColors.TEXT_PRIMARY))
        baseline = y + (_BADGE_H + metrics.ascent() - metrics.descent()) / 2
        painter.drawText(QPointF(x + _BADGE_ICON + _BADGE_GAP, baseline), text)

    def _paint_port_bars(self, painter: QPainter, dpr: float, dy: float) -> None:
        """逐口功率条 + 功率文本（盒内坐标经 _to_canvas 转画布）。

        条位置照抄 index.html:151-166 内联坐标（.usb-overlay-ani
        left:134.5px + 模块 top 36/68/100/133 + pbg 偏移）；功率文本
        照 .usb-pval 内联 left:140px——条尾 371.5 内、机身右缘 239.7 外
        悬空，逐口贴口部（用户截图错版把文本塞在条中心）。条底图仅
        活跃口绘制（mod.active），空载口文本 0W 照旧（app.js:1703-1706）。
        """
        entries = []
        for port in _PORTS:
            entry = _port_state(self._ports, port)
            enabled = entry.get("enabled", True) is not False
            power = float(entry.get("power") or 0.0)
            entries.append((port, enabled and power > 0, power))
        # 第一遍：条底图（usb_c/a_rectangle）
        for port, active, _power in entries:
            if not active:
                continue
            src = _hi_dpi_pixmap(_BAR_SRC[port], dpr)
            if src.isNull():
                continue
            bx, by = self._to_canvas(_BAR_X[port], _BAR_Y[port] + dy)
            painter.drawPixmap(
                QRectF(bx, by, _BAR_SIZE[port][0], _BAR_SIZE[port][1]),
                src, QRectF(0.0, 0.0, src.width(), src.height()))
        # 第二遍：功率文本（{W}W，17px 粗体白色——素材条内恒为亮字）
        font = QFont("Microsoft YaHei UI", 8)
        font.setBold(True)
        font.setPixelSize(17)
        painter.setFont(font)
        painter.setPen(QColor("#FFFFFF"))
        for port, _active, power in entries:
            text = _fmt_watts(power) if _active else "0W"
            tx, ty = self._to_canvas(_PVAL_X, _BAR_Y[port] + dy + _PVAL_DY)
            painter.drawText(
                QRectF(tx, ty, _PVAL_W, _PVAL_H),
                Qt.AlignmentFlag.AlignCenter, text)

    def retheme(self) -> None:
        """主题切换：素材与底图不变（index 版行为），重绘即可。"""
        self.update()


# ----------------------------------------------------------------------------
# PortCardGrid —— 2×2 端口卡网格
# ----------------------------------------------------------------------------


class _LoadBar(QWidget):
    """负载条（自绘）：轨道 + 端口色填充，空载整条淡出（连轨道一起）。

    上游 .port-load：高 5px 圆角条，填充色取端口身份色；空载换 divider
    色（"进度卡在 0%" 与 "这个口没插东西" 不是一个意思）。
    """

    def __init__(self, port: int, parent=None):
        super().__init__(parent)
        self._port = port
        self._pct = 0
        self._idle = True
        self.setFixedHeight(6)

    def set_value(self, power: float) -> None:
        """按功率刷新：pct=loadPct(power/本口上限)，空载淡出。"""
        self._pct = _load_pct(power, self._port)
        self._idle = not power > 0
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = QRectF(0.0, 0.0, self.width(), self.height())
            path = QPainterPath()
            path.addRoundedRect(rect, 3.0, 3.0)
            painter.setClipPath(path)
            if self._idle:
                # 空载：连轨道一起淡掉（上游 .port-load.is-idle → divider 色）
                painter.fillRect(rect, QColor(SiColors.LINE))
            else:
                painter.fillRect(rect, QColor(SiColors.WINDOW_BG))
                fill_w = rect.width() * self._pct / 100.0
                painter.fillRect(QRectF(0.0, 0.0, fill_w, rect.height()),
                                 QColor(PORT_COLORS[self._port]))
        finally:
            painter.end()


class PortCardGrid(QWidget):
    """2×2 端口卡网格：图标 + 名称 + 开关 + 大功率读数 + 负载条 + V·A/协议。

    仿上游 §3.3 portGrid：每口一卡，SSE port_update 走增量更新
    （updatePortDOM 思路：只改对应卡的 label/条/图标，不重建布局）。
    点卡片（开关除外）发 port_clicked（端口详情弹窗是后续任务，先留
    信号）；点开关发 port_toggle(port, on)，由面板调 service，本组件
    不发请求。充电中卡片描边用端口色（与前景墨 70% 混色，上游
    .port-card.is-charging 同款）；负载条上限 C1/C2=120W C3=44W A=33W。
    """

    port_clicked = Signal(int)          # 端口号 1-4
    port_toggle = Signal(int, bool)     # (端口号, 开关目标态)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cards: dict[int, dict] = {}
        grid = QVBoxLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(10)
        rows = [(1, 2), (3, 4)]
        for row_ports in rows:
            row = QHBoxLayout()
            row.setSpacing(10)
            for port in row_ports:
                card = self._build_card(port)
                row.addWidget(card["frame"], stretch=1)
                self._cards[port] = card
            grid.addLayout(row)
        self._apply_inline_styles()

    # ---------- 单卡构建 ----------

    def _build_card(self, port: int) -> dict:
        frame = QFrame()
        frame.setObjectName("propCard")
        frame.setAttribute(Qt.WA_StyledBackground, True)
        frame.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(4)

        # 头部：端口小图标（on/off 两版随开关切换）+ 端口名 + 开关
        header = QHBoxLayout()
        header.setSpacing(6)
        icon_label = QLabel()
        icon_label.setFixedSize(34, 21)
        icon_label.setScaledContents(True)
        name = QLabel(_PORT_NAMES[port])
        name.setFont(QFont("Microsoft YaHei UI", 10, QFont.Weight.DemiBold))
        switch = themed_switch()
        switch.toggled.connect(
            lambda on, p=port, s=switch: self._on_switch(p, on, s))
        header.addWidget(icon_label)
        header.addWidget(name)
        header.addStretch(1)
        header.addWidget(switch, alignment=Qt.AlignmentFlag.AlignVCenter)

        # 大功率读数（15pt，上游 port-power-value 24px 的桌面档）
        power = QLabel("-- W")
        power.setFont(QFont("Microsoft YaHei UI", 15, QFont.Weight.DemiBold))

        # 负载条 + 副行（V·A 与协议徽标）
        load = _LoadBar(port)
        sub = QHBoxLayout()
        sub.setSpacing(6)
        va = QLabel("--")
        va.setFont(QFont("Microsoft YaHei UI", 9))
        proto = QLabel("—")
        proto.setFont(QFont("Microsoft YaHei UI", 8))
        sub.addWidget(va)
        sub.addStretch(1)
        sub.addWidget(proto)

        lay.addLayout(header)
        lay.addWidget(power)
        lay.addWidget(load)
        lay.addLayout(sub)

        card = {
            "frame": frame, "icon": icon_label, "name": name,
            "switch": switch, "power": power, "load": load,
            "va": va, "proto": proto, "port": port, "charging": None,
        }
        # 点卡片（开关除外）发 port_clicked：开关是自绘 QPushButton 子
        # 控件，点击被它自己消费；能到 frame.mouseReleaseEvent 的都是
        # 卡片空白区（DeviceCard.mouseReleaseEvent 同款思路）
        frame.mouseReleaseEvent = (  # type: ignore[method-assign]
            self._make_card_click(frame, port))
        return card

    def _make_card_click(self, frame: QFrame, port: int):
        """生成卡片点击槽：左键释放发 port_clicked（开关区域到不了这里）。"""
        original = QFrame.mouseReleaseEvent

        def handler(ev) -> None:
            if ev.button() == Qt.MouseButton.LeftButton:
                self.port_clicked.emit(port)
            original(frame, ev)

        return handler

    # ---------- 数据注入（增量更新，不重建卡） ----------

    def update_state(self, status: dict) -> None:
        """整帧注入：四口各走一遍增量渲染路径（与单口更新天然一致）。"""
        ports = status.get("ports") or {}
        if not isinstance(ports, dict):
            ports = {}
        for port in _PORTS:
            self._render_port(port, _port_state(ports, port))

    def update_port(self, port_id: int, port_data: dict) -> None:
        """单口增量（SSE port_update）：只更新对应卡，不动其它卡。"""
        if isinstance(port_data, dict) and port_id in self._cards:
            self._render_port(port_id, port_data)

    def _render_port(self, port: int, entry: dict) -> None:
        """渲染单口（对齐上游 updatePortDOM：文本/条/图标/描边就地改）。"""
        card = self._cards[port]
        enabled = entry.get("enabled", True) is not False
        power = float(entry.get("power") or 0.0)
        voltage = float(entry.get("voltage") or 0.0)
        current = float(entry.get("current") or 0.0)
        protocol = str(entry.get("protocol") or "idle")

        card["power"].setText(_fmt_watts(power))
        card["load"].set_value(power)
        card["va"].setText(f"{voltage:.1f}V · {current:.1f}A")
        proto_active = protocol not in ("", "idle")
        card["proto"].setText(protocol if protocol else "—")
        self._style_proto(card["proto"], proto_active)
        if card["switch"].isChecked() != enabled:
            _sync_switch(card["switch"], enabled)
        self._set_port_icon(card, port, enabled)
        # 充电中描边用端口色（仅充电态变化时重写样式表，避免每秒重设）
        charging = power > 0
        if card["charging"] != charging:
            card["charging"] = charging
            self._style_card(card, charging)

    def _set_port_icon(self, card: dict, port: int, enabled: bool) -> None:
        """端口小图标 on/off 两版随开关切换（上游 main_card_port_{key}_{on|off}）。

        素材是白色线稿（on）/深灰实底（off）：暗色主题直接用；浅色主题
        on 版反相成深色线稿（上游 html[data-appearance=light] invert 同款）。
        """
        pm = _asset_pixmap(
            f"main_card_port_{_PORT_KEYS[port]}_{'on' if enabled else 'off'}.png")
        if not pm.isNull():
            pm = pm.copy()  # 避免缓存影响（QPixmap 按文件名共享需重开）
            pm.setDevicePixelRatio(pm.width() / 26.0)  # 78px 源 → 26 逻辑 px
        card["icon"].setPixmap(pm)

    # ---------- 内联样式（retheme 重求值） ----------

    def _style_proto(self, label: QLabel, active: bool) -> None:
        """协议徽标：非 idle 高亮（主题青淡染胶囊），idle 灰置。"""
        if active:
            label.setStyleSheet(
                f"color: {SiColors.THEME}; background: transparent;"
                f" border: 1px solid {SiColors.THEME}; border-radius: 9px;"
                f" padding: 1px 6px;")
        else:
            label.setStyleSheet(
                f"color: {SiColors.TEXT_MUTED}; background: transparent;"
                f" border: 1px solid {SiColors.LINE}; border-radius: 9px;"
                f" padding: 1px 6px;")

    def _style_card(self, card: dict, charging: bool) -> None:
        """卡片描边：充电中用端口色与前景墨的 70% 混色，否则中性（全局 QSS）。"""
        if charging:
            border = _mix_color(PORT_COLORS[card["port"]],
                                SiColors.TEXT_PRIMARY, 0.7)
            card["frame"].setStyleSheet(
                f"QFrame#propCard {{ background: {SiColors.CARD};"
                f" border: 1px solid {border}; border-radius: 14px; }}")
            card["power"].setStyleSheet(
                f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")
        else:
            card["frame"].setStyleSheet("")  # 回落 propCard 全局 QSS
            card["power"].setStyleSheet(
                f"color: {SiColors.TEXT_SECONDARY}; background: transparent;")

    def _apply_inline_styles(self) -> None:
        """retheme 重求值内联样式（名称/副行/协议徽标/描边/负载条）。"""
        for port, card in self._cards.items():
            card["name"].setStyleSheet(
                f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")
            card["va"].setStyleSheet(
                f"color: {SiColors.TEXT_SECONDARY}; background: transparent;")
            # 协议徽标按当前文字是否为占位重取亮灭态
            self._style_proto(card["proto"],
                              card["proto"].text() not in ("", "—", "idle"))
            # 充电态描边以缓存为准重写（浅/深主题底色不同）
            card["charging"] = None
            card["load"].update()

    def retheme(self) -> None:
        """主题切换：重求值内联样式；素材图标路径不变，负载条重绘。"""
        self._apply_inline_styles()
        self.update()

    # ---------- 交互 ----------

    def _on_switch(self, port: int, on: bool, switch) -> None:
        """开关点击：发 port_toggle 由面板调 service；本组件不发请求。"""
        self.port_toggle.emit(port, on)


# ----------------------------------------------------------------------------
# MiniPhoneShareBar —— 四色堆叠占比条 + 标签行
# ----------------------------------------------------------------------------


class _ShareBar(QWidget):
    """四色堆叠条（自绘）：各段宽度=该口功率占比，颜色=端口身份色。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._segments: list[tuple[float, str]] = []  # (占比 0-1, 颜色)
        self.setFixedHeight(8)

    def set_segments(self, segments: list[tuple[float, str]]) -> None:
        self._segments = list(segments)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = QRectF(0.0, 0.0, self.width(), self.height())
            path = QPainterPath()
            path.addRoundedRect(rect, 4.0, 4.0)
            painter.setClipPath(path)
            # 轨道底色（上游 #powerDist 的 --track，空载时整条可见）
            painter.fillRect(rect, QColor(SiColors.LINE))
            x = 0.0
            for frac, color in self._segments:
                w = rect.width() * frac
                if w > 0.5:
                    painter.fillRect(QRectF(x, 0.0, w, rect.height()),
                                     QColor(color))
                x += w
        finally:
            painter.end()


class MiniPhoneShareBar(QWidget):
    """端口功率占比条：一条四色堆叠条 + 标签行（每口 W 与占比%）。

    仿上游 cardPortShare（index.html:326-329 + app.js renderPortShare）：
    占比分子只算 enabled 且 power>0 的口，空载口占比按 0 计（分母为
    活跃口合计，全空载时显示 0%）；标签行文字一律中性墨色、空载口压暗
    （亮端口色当小字对比度不够），端口身份由四色圆点承载。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._labels: list[QLabel] = []
        self._dots: list[QLabel] = []
        self._active: list[bool] = [False, False, False, False]
        self._bar = _ShareBar()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(5)
        lay.addWidget(self._bar)
        row = QHBoxLayout()
        row.setSpacing(8)
        for port in _PORTS:
            item = QHBoxLayout()
            item.setSpacing(4)
            dot = QLabel("●")
            dot.setFont(QFont("Microsoft YaHei UI", 8))
            label = QLabel(f"{_PORT_NAMES[port]} —")
            label.setFont(QFont("Microsoft YaHei UI", 8))
            item.addWidget(dot)
            item.addWidget(label)
            row.addLayout(item)
            self._dots.append(dot)
            self._labels.append(label)
        row.addStretch(1)
        lay.addLayout(row)
        self._apply_inline_styles()

    # ---------- 数据注入 ----------

    def update_state(self, status: dict) -> None:
        """整帧注入：重算四口占比并刷新条与标签（仅展示，不发请求）。"""
        ports = status.get("ports") or {}
        if not isinstance(ports, dict):
            ports = {}
        watts: list[float] = []
        for port in _PORTS:
            entry = _port_state(ports, port)
            enabled = entry.get("enabled", True) is not False
            power = float(entry.get("power") or 0.0)
            watts.append(power if enabled and power > 0 else 0.0)
        total = sum(watts) or 1.0
        self._bar.set_segments(
            [(w / total, PORT_COLORS[port]) for port, w in zip(_PORTS, watts)])
        for i, (port, w) in enumerate(zip(_PORTS, watts)):
            pct = round(w / total * 100)
            self._labels[i].setText(
                f"{_PORT_NAMES[port]} {_fmt_watts(w)} · {pct}%")
            self._active[i] = w > 0
        self._apply_inline_styles()

    # ---------- 内联样式（retheme 重求值） ----------

    def _apply_inline_styles(self) -> None:
        """圆点=端口身份色（恒亮）；标签文字中性墨，空载口整项压暗。"""
        for i, port in enumerate(_PORTS):
            self._dots[i].setStyleSheet(
                f"color: {PORT_COLORS[port]}; background: transparent;")
            self._labels[i].setStyleSheet(
                f"color: {SiColors.TEXT_PRIMARY if self._active[i] else SiColors.TEXT_MUTED};"
                f" background: transparent;")
        self._bar.update()

    def retheme(self) -> None:
        """主题切换：重求值内联样式并重绘堆叠条轨道色。"""
        self._apply_inline_styles()
        self._bar.update()
