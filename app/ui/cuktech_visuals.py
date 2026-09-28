# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Ex: 米家设备的 Windows 桌面控制端（扩展版）
# Copyright (C) 2026 MiHome-Ex contributors
"""CUKTECH 面板观感组件二件套：充电器模式图标排 + 增强版功率曲线。

仿上游 cuktech-ble-server web 前端（dev-notes/web-frontend-features.md
§3.2 场景模式卡、§3.5 功率曲线、§2 图表采样档位）的两个独立 QWidget。
本模块只负责「喂入数据 → 渲染 + 发信号」：不发任何网络请求，请求由
面板层监听信号后经 service 门面发出。

- SceneButtonRow —— 4 个圆形充电器模式图标按钮（素材
  main_charger_{dark,light}_{ai,mac,single,balance}_{on,off}.png，
  路径随 SiColors 主题 + 选中态切换），下带文案标签、标题行右侧当前
  模式名、卡底说明文字。交互对齐上游 setScene 乐观更新协议：
  set_scene_ui() 是唯一选中态入口（面板可在乐观更新期跳过 SSE 回报），
  点击只发 scene_selected(mode) 信号，不 POST。
- MultiMetricCurve —— 增强版功率曲线。一次 /api/chart 响应
  （power/voltage/current 三组序列）全量暂存，指标 W/V/A/Σ 与时间档位
  切换零请求；30分/60分/120分/24小时四档对应上游 chart-config.js 的
  HISTORY_INTERVALS（interval 20/20/30/300）。Σ 模式画总功率单线，
  其余指标画 4 条端口线（不堆叠，各自对 Y 轴——堆叠会让 Y 轴读不出
  单口功率，见上游 initChart 注释）。自定义图例圆点行、尾部全零桶
  裁掉、峰值标注、空态「暂无数据」、retheme() 全支持。

取色一律 SiColors 动态代理（构造期求值的内联样式在 retheme() 重设），
端口四色抄上游 index.css 的 --port-c1/c2/c3/a，总功率线抄
--total-line（深 #C084FC / 浅 #7C3AED）。
"""

from typing import Any

import math

import numpy as np  # SiliconUI 已带依赖；此处仅读图标 alpha 通道

from PySide6.QtCore import QMargins, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from app import resource_path
from app.ui.si_theme import SiColors, current_theme

# ----------------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------------

# 场景素材目录与 16 张图标路径规则（app.js:95-98 sceneIconSrc）：
# main_charger_{theme}_{img}_{on|off}.png；2 号模式的图名是 mac 不是 eco
_SCENE_IMG_DIR = "app/ui/assets/cuktech"
_SCENE_IMGS: tuple[str, ...] = ("ai", "mac", "single", "balance")
_SCENE_META: tuple[tuple[int, str, str, str], ...] = (
    # (mode, img, 标签, 说明) 文案照抄 zh-CN.js:74-83
    (1, "ai", "AI模式", "自动识别设备智能匹配最优充电功率"),
    (2, "mac", "数码生态", "多口同时充电均衡分配功率"),
    (3, "single", "单口模式", "单口最大功率输出优先C1口"),
    (4, "balance", "均衡模式", "多个端口均衡分配充电功率"),
)
_SCENE_BUTTON_SIZE = 56  # 圆形按钮直径

# 端口四色：抄上游 index.css --port-c1/c2/c3/a（两种主题同值）
_PORT_COLORS: tuple[str, ...] = ("#FF7A00", "#46B4FF", "#89D8F3", "#FFD24B")
# Σ 总功率线色：抄上游 index.css --total-line（深/浅两套）
_TOTAL_LINE_COLOR = {"dark": "#C084FC", "light": "#7C3AED"}

# 时间档位：hours -> (按钮文案, interval 秒)。
# interval 抄上游 chart-config.js:2-3
# HISTORY_INTERVALS = { 30: 20, 60: 20, 90: 20, 120: 30, 1440: 300 }
# （上游 90 分钟档桌面端不暴露，四档对齐 index.html 的时间按钮组）
_RANGE_DEFS: tuple[tuple[float, str, int], ...] = (
    (0.5, "30分", 20),
    (1.0, "60分", 20),
    (2.0, "120分", 30),
    (24.0, "24小时", 300),
)

# 指标档位：key -> (按钮文案, Y 轴单位, 序列名)
_METRIC_POWER = "power"
_METRIC_VOLTAGE = "voltage"
_METRIC_CURRENT = "current"
_METRIC_TOTAL = "total"

# 曲线绘制几何（对齐 PowerCurveWidget 的边距约定）
_CURVE_MARGIN_R = 10
_CURVE_MARGIN_T = 20  # 顶部留给峰值标注
_CURVE_MARGIN_B = 22  # 底部留 X 轴首尾时间刻度
# Y 轴数值带宽：左缘固定留白画刻度文字（W/V/A），绘图区从其右起
_Y_AXIS_WIDTH = 42.0

# 曲线点数上限：超出等距抽稀（对齐 PowerCurveWidget 的上限约定）
_CURVE_MAX_POINTS = 288  # 上游 MAX_POINTS_MAP 的最大档位


def _scene_icon_path(mode: int, active: bool) -> str:
    """场景图标绝对路径：main_charger_{theme}_{img}_{on|off}.png。

    必须经 resource_path 解析：相对路径按进程 CWD 解析，安装版从
    快捷方式/注册表启动时 CWD 不一定是安装目录，会整批加载失败
    （模式图标空白圆圈）；cuktech_stage._asset 同款约定。
    """
    meta = next((m for m in _SCENE_META if m[0] == mode), _SCENE_META[0])
    return str(resource_path(
        f"{_SCENE_IMG_DIR}/main_charger_{current_theme()}_{meta[1]}_"
        f"{'on' if active else 'off'}.png"
    ))


def _centered_icon_pixmap(path: str) -> QPixmap:
    """把场景图标按「实心圆」重居中裁成方形再转 QPixmap。

    上游素材画布 270×270 上下、实心圆 r=75px 的圆心普遍偏 (-12, -18)px，
    且圆外还有一圈 30~40px 才衰减完的柔光——整张画布塞进 QIcon 时圆
    明显偏小且偏左上（56px 按钮里圆只有 ~27px）。这里以 alpha≥200 的
    实心圆外接方为基准，向四周各扩 1/4 圆径的柔光边距后裁成以圆心为
    中心的正方形；web 端 CSS 因 object-fit 的裁切方向不同不受此影响，
    故只处理 Qt 读的这一份路径，不动素材文件。
    """
    image = QImage(path)
    if image.isNull():
        return QPixmap(path)
    argb = image.convertToFormat(QImage.Format.Format_ARGB32)
    # bits() 指向 Qt 私有内存，必须显式拷成 numpy 数组（np.asarray 拿不到）
    raw = memoryview(argb.constBits()).cast("B")
    alpha = (
        np.frombuffer(raw, dtype=np.uint8)
        .reshape(argb.height(), argb.bytesPerLine())[:, : argb.width() * 4]
        .reshape(argb.height(), argb.width(), 4)[:, :, 3]
    )
    solid = np.argwhere(alpha >= 200)
    if solid.size == 0:
        return QPixmap.fromImage(image)
    (top, left), (bottom, right) = solid.min(0), solid.max(0)
    radius = max(bottom - top, right - left) / 2
    cx, cy = (left + right) / 2, (top + bottom) / 2
    half = radius * 1.25  # 圆径一半 + 1/4 圆径柔光边
    side = int(math.ceil(half * 2))
    return QPixmap.fromImage(image.copy(
        int(round(cx - half)), int(round(cy - half)), side, side))


def _scene_label(mode: int) -> str:
    meta = next((m for m in _SCENE_META if m[0] == mode), _SCENE_META[0])
    return meta[2]


def _scene_desc(mode: int) -> str:
    meta = next((m for m in _SCENE_META if m[0] == mode), _SCENE_META[0])
    return meta[3]


def _nice_scale(raw_span: float) -> tuple[float, float, int]:
    """按数据上界选好读的 Y 轴刻度：返回 (步长, 轴上界, 分段数)。

    仿常见图表 nice-number 逻辑：raw = raw_span / 4，取
    mag = 10^floor(log10(raw))，在 {1, 2, 5}×mag 里选「分段数 2~4
    （即水平网格线 3~5 条）且头部浪费最少」的一个——轴上界 =
    分段数 × 步长 ≥ raw_span（功率 10/20/50、电压 5/10、电流
    0.5/1 之类；不用 2.5 系步长，保证功率档刻度恒为整数）。
    raw_span <= 0 时给 (1.0, 1.0, 1) 兜底。
    """
    if raw_span <= 0:
        return (1.0, 1.0, 1)
    raw = raw_span / 4
    mag = 10.0 ** math.floor(math.log10(raw))
    best: tuple[float, float, int] | None = None
    for mult in (1.0, 2.0, 5.0):
        step = mult * mag
        divisions = int(math.ceil(raw_span / step - 1e-9))
        if not 2 <= divisions <= 4:
            continue
        span = divisions * step
        # 头部浪费同小者胜，平手取分段更多（刻度更细）的
        if best is None or (span, -divisions) < (best[1], -best[2]):
            best = (step, span, divisions)
    if best is not None:
        return best
    step = 10.0 * mag
    divisions = max(1, int(math.ceil(raw_span / step - 1e-9)))
    return (step, divisions * step, divisions)


def _tick_decimals(step: float) -> int:
    """步长需要的小数位（0.5 -> 1，0.05 -> 2，25 -> 0）。"""
    if step >= 1.0:
        return 0
    return max(0, int(-math.floor(math.log10(step))))


def _fmt_tick(value: float, decimals: int) -> str:
    """刻度数值文本：固定小数位，恒无 "-0"（0 线必标且不显负零）。"""
    text = f"{value:.{decimals}f}"
    if text.startswith("-") and float(text) == 0.0:
        text = text[1:]
    return text


# ----------------------------------------------------------------------------
# SceneButtonRow —— 充电器模式图标按钮排
# ----------------------------------------------------------------------------


class SceneButtonRow(QWidget):
    """充电器模式卡：4 个圆形图标按钮 + 当前模式名 + 卡底说明文字。

    交互对齐上游 app.js setScene 乐观更新协议：组件自己不发请求，
    点击只发 :attr:`scene_selected`；选中态统一从 :meth:`set_scene_ui`
    进入——面板在乐观更新后调用它即可立即高亮，SSE settings 回报是否
    回填（以及上游 isRecent() 式的 3s 保护期跳过）由面板自行决定，
    组件不持有计时器。

    图标素材 16 张随「SiColors 当前主题 × 选中态」选路径
    （main_charger_{dark|light}_{ai|mac|single|balance}_{on|off}.png）；
    选中态额外画一圈高亮描边（主题色圆环）。
    """

    scene_selected = Signal(int)  # 用户点击的充电器模式（1-4），不发请求

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mode: int = 1  # 当前选中模式（乐观值；上游 lastScene 缺省 1）

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        # 头行：右对齐当前模式名（sceneCurrent）。左标题「充电器模式」由
        # 宿主面板的分区标题行提供（_build_section_title），组件内不再
        # 重复渲染一份，避免卡内出现两行同文案
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addStretch(1)
        self._current = QLabel(_scene_label(self._mode))
        self._current.setStyleSheet(self._current_style())
        head.addWidget(self._current)
        lay.addLayout(head)

        # 图标按钮排：圆形按钮 + 下方文案标签（scene-btn 结构）
        row = QHBoxLayout()
        row.setSpacing(18)
        row.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._buttons: dict[int, QPushButton] = {}
        self._labels: dict[int, QLabel] = {}
        for mode, _img, label, _desc in _SCENE_META:
            btn = QPushButton(self)
            btn.setCheckable(False)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedSize(_SCENE_BUTTON_SIZE, _SCENE_BUTTON_SIZE)
            # 图标按内容区内切（半径 28 含 2px 描边 + 2px padding，
            # 内容区 52px；QIcon 会自动缩放 270×271 素材，保比例居中）
            btn.setIconSize(btn.size().grownBy(QMargins(-4, -4, -4, -4)))
            btn.setStyleSheet(self._button_style(False))
            btn.clicked.connect(lambda _c=False, m=mode: self._on_clicked(m))
            col = QVBoxLayout()
            col.setContentsMargins(0, 0, 0, 0)
            col.setSpacing(2)
            col.addWidget(btn, 0, Qt.AlignmentFlag.AlignHCenter)
            lab = QLabel(label)
            lab.setStyleSheet(self._label_style())
            lab.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            col.addWidget(lab)
            row.addLayout(col)
            self._buttons[mode] = btn
            self._labels[mode] = lab
        lay.addLayout(row)

        # 卡底说明文字（sceneDesc，zh-CN.js 四句随选中态换）
        self._desc = QLabel(_scene_desc(self._mode))
        self._desc.setWordWrap(True)
        self._desc.setStyleSheet(self._desc_style())
        lay.addWidget(self._desc)

        self._refresh_icons()

    # ---------- 公开接口 ----------

    def set_scene_ui(self, mode: int) -> None:
        """喂入当前场景值刷新选中态（唯一选中态入口，不发请求）。

        面板约定：用户点击后的乐观更新与 SSE settings 回报都走这里；
        上游 markLocal() 的 3s 保护（isRecent 期间回报不回滚乐观值）
        由面板在调用侧决定是否跳过，本组件不设时序状态。
        """
        if mode not in (1, 2, 3, 4) or mode == self._mode:
            return
        self._mode = int(mode)
        self._refresh()

    def current_scene(self) -> int:
        """当前乐观选中值（面板轮询对账用）。"""
        return self._mode

    def retheme(self) -> None:
        """主题切换：重刷 16 张图标路径与全部内联样式文字色。"""
        self._current.setStyleSheet(self._current_style())
        self._desc.setStyleSheet(self._desc_style())
        for mode, lab in self._labels.items():
            lab.setStyleSheet(self._label_style())
            self._buttons[mode].setStyleSheet(
                self._button_style(mode == self._mode))
        self._refresh_icons()

    # ---------- 内部 ----------

    def _on_clicked(self, mode: int) -> None:
        # 乐观更新：立即高亮并广播；不发请求（上游 setScene 的 UI 半边）
        self._mode = mode
        self._refresh()
        self.scene_selected.emit(mode)

    def _refresh(self) -> None:
        """按当前 _mode 同步图标 on/off、描边、当前模式名与说明文字。"""
        self._refresh_icons()
        for mode, btn in self._buttons.items():
            btn.setStyleSheet(self._button_style(mode == self._mode))
        self._current.setText(_scene_label(self._mode))
        self._desc.setText(_scene_desc(self._mode))

    def _refresh_icons(self) -> None:
        for mode, btn in self._buttons.items():
            btn.setIcon(
                _centered_icon_pixmap(
                    _scene_icon_path(mode, mode == self._mode)))

    # ---------- 内联样式（构造期求值，retheme 重设） ----------

    def _current_style(self) -> str:
        return (
            f"color: {SiColors.THEME}; font-size: 9pt; "
            "background: transparent;"
        )

    def _label_style(self) -> str:
        return (
            f"color: {SiColors.TEXT_SECONDARY}; font-size: 8pt; "
            "background: transparent;"
        )

    def _desc_style(self) -> str:
        return (
            f"color: {SiColors.TEXT_MUTED}; font-size: 8pt; "
            "background: transparent;"
        )

    def _button_style(self, active: bool) -> str:
        # 圆形按钮：QSS border-radius 拉圆；选中态画主题色描边
        border = (
            f"2px solid {SiColors.THEME}" if active
            else f"1px solid {SiColors.LINE}"
        )
        bg = SiColors.CARD if active else "transparent"
        return (
            f"QPushButton {{ background: {bg}; border: {border}; "
            f"border-radius: {_SCENE_BUTTON_SIZE // 2}px; "
            "padding: 2px; }"
            f"QPushButton:hover {{ border-color: {SiColors.THEME_HOVER}; }}"
        )


# ----------------------------------------------------------------------------
# MultiMetricCurve —— 增强版功率曲线
# ----------------------------------------------------------------------------


class _RangeButton(QPushButton):
    """时间/指标档位小按钮：选中态主题青底深字（对齐 themed_tab_button）。"""

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def refresh_style(self, active: bool) -> None:
        if active:
            self.setStyleSheet(
                f"QPushButton {{ background: {SiColors.THEME}; "
                f"color: {SiColors.ON_THEME_TEXT}; border: none; "
                "border-radius: 9px; padding: 2px 10px; font-size: 8pt; }"
            )
        else:
            self.setStyleSheet(
                f"QPushButton {{ background: transparent; "
                f"color: {SiColors.TEXT_SECONDARY}; border: none; "
                "border-radius: 9px; padding: 2px 10px; font-size: 8pt; }"
                f"QPushButton:hover {{ background: {SiColors.BTN_HOVER}; "
                f"color: {SiColors.TEXT_PRIMARY}; }}"
            )


class MultiMetricCurve(QWidget):
    """增强版功率曲线：时间档位 + W/V/A/Σ 指标切换 + 多线自绘。

    数据契约：:meth:`set_chart_data` 接收一次 ``GET /api/chart`` 的
    响应体（hours/interval 已由请求方决定）——
    ``labels`` + ``datasets.{power,voltage,current}`` 三组序列全量
    暂存；之后指标与时间按钮只切视图，**零请求**（上游
    app.js:433-437 同款设计）。power 固定 5 条（C1/C2/C3/A/Total），
    voltage/current 各 4 条（cuktech-api.md §7）。

    - 指标「功率/电压/电流」画 4 条端口线，**不堆叠**、各自对 Y 轴
      （上游 initChart 长注释：堆叠会把 Y 轴读数变成累计值）；颜色为
      上游 index.css 的 --port-c1/c2/c3/a。
    - 指标「总功率(Σ)」画 power[4]（Total）单条线 + 填充，线色
      --total-line（深 #C084FC / 浅 #7C3AED）。
    - 时间档位四档按钮（30分/60分/120分/24小时）发
      :attr:`range_changed`（hours, interval），interval 逐档抄上游
      chart-config.js 的 HISTORY_INTERVALS（20/20/30/300）。
    - 观感对齐现有 PowerCurveWidget：水平网格线、填充、峰值标注、
      空态「暂无数据」；另补尾部全零桶裁掉（上游 updateChart 清洗）、
      Y 轴左缘数值带（nice 步长刻度 + 单位 W/V/A，0 线必标）与
      X 轴首/中/末时间刻度。
    """

    range_changed = Signal(float, int)  # (hours, interval 秒)

    def __init__(self, parent=None):
        super().__init__(parent)
        # 一次响应全存：切指标零请求（上游 _lastChartPayload 同款）
        self._labels: list[str] = []
        self._series: dict[str, list[list[float]]] = {
            "power": [], "voltage": [], "current": []}
        self._total: list[float] = []
        self._metric: str = _METRIC_POWER
        self._hours: float = 1.0
        # 悬浮态：命中桶索引（-1 无）与该桶的最近数据点索引
        self._hover_index: int = -1
        self._hover_point: int = -1
        self.setMinimumHeight(230)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self.setMouseTracking(True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        # ---- 档位行：时间四档 | 弹簧 | 指标四档 ----
        bar = QHBoxLayout()
        bar.setSpacing(4)
        self._range_buttons: dict[float, _RangeButton] = {}
        for hours, text, _interval in _RANGE_DEFS:
            btn = _RangeButton(text)
            btn.clicked.connect(
                lambda _c=False, h=hours: self._select_range(h))
            bar.addWidget(btn)
            self._range_buttons[hours] = btn
        bar.addStretch(1)
        self._metric_buttons: dict[str, _RangeButton] = {}
        for key, text in (
            (_METRIC_POWER, "功率"), (_METRIC_VOLTAGE, "电压"),
            (_METRIC_CURRENT, "电流"), (_METRIC_TOTAL, "总功率"),
        ):
            btn = _RangeButton(text)
            btn.clicked.connect(
                lambda _c=False, k=key: self._select_metric(k))
            bar.addWidget(btn)
            self._metric_buttons[key] = btn
        lay.addLayout(bar)

        # ---- 图例圆点行（自定义；Σ 模式只剩一项，上游 renderChartLegend） ----
        legend = QHBoxLayout()
        legend.setContentsMargins(2, 0, 0, 0)
        legend.setSpacing(12)
        self._legend_host = QWidget(self)
        self._legend_host.setLayout(legend)
        self._legend_dots: list[QLabel] = []
        self._legend_texts: list[QLabel] = []
        for _i in range(5):  # 最多 4 端口 + 1 总功率
            dot = QLabel(self._legend_host)
            dot.setFixedSize(8, 8)
            text = QLabel(self._legend_host)
            text.setStyleSheet(self._legend_text_style())
            legend.addWidget(dot)
            legend.addWidget(text)
            legend.addSpacing(4)
            self._legend_dots.append(dot)
            self._legend_texts.append(text)
        legend.addStretch(1)
        lay.addWidget(self._legend_host)
        lay.addStretch(1)

        self._sync_buttons()

    # ---------- 公开接口 ----------

    def set_chart_data(self, payload: dict) -> None:
        """整包替换 /api/chart 数据（不发请求）。

        payload 结构见 cuktech-api.md §7：labels + datasets 三组序列。
        先按上游 updateChart（app.js:645-654）裁掉尾部全零桶（最后一个
        桶往往还没采到数据，画出来会拖着一条贴地的假零线），裁剪同步
        作用于三组序列；空/坏载荷进空态。
        """
        labels = payload.get("labels") if isinstance(payload, dict) else None
        datasets = payload.get("datasets") if isinstance(payload, dict) else None
        if not isinstance(labels, list) or not isinstance(datasets, dict):
            self._clear()
            return

        power = self._extract_series(datasets.get("power"))
        voltage = self._extract_series(datasets.get("voltage"))
        current = self._extract_series(datasets.get("current"))
        if not power:
            self._clear()
            return

        # 尾部全零桶裁掉：power 全端口（不含 Total 也无妨，端口全零即
        # 该桶未采数）与 V/A 序列同步缩到同一长度，labels 一并裁
        n = len(power[0])
        trim = n
        while trim > 1 and all(ds[trim - 1] == 0.0 for ds in power):
            trim -= 1
        self._labels = [str(x) for x in labels[:trim]]
        self._series = {
            "power": [ds[:trim] for ds in power],
            "voltage": [ds[:trim] for ds in voltage],
            "current": [ds[:trim] for ds in current],
        }
        self._total = list(power[4]) if len(power) > 4 else \
            [sum(vals) for vals in zip(*self._series["power"])]
        self._total = self._total[:trim]
        self.update()
        self._render_legend()

    def set_range(self, hours: float) -> None:
        """程序化选时间档（不发 range_changed；面板回填用）。"""
        hours = float(hours)
        if hours in self._range_buttons:
            self._hours = hours
            self._sync_buttons()

    def current_range(self) -> float:
        return self._hours

    def current_metric(self) -> str:
        return self._metric

    def retheme(self) -> None:
        """主题切换：档位/图例重设内联样式，曲线绘制期动态取色重绘。"""
        self._sync_buttons()
        for text in self._legend_texts:
            text.setStyleSheet(self._legend_text_style())
        self._render_legend()
        self.update()

    # ---------- 槽与内部 ----------

    def _select_range(self, hours: float) -> None:
        if hours == self._hours:
            return
        self._hours = hours
        self._sync_buttons()
        interval = self._interval_for(hours)
        self.range_changed.emit(hours, interval)

    def _select_metric(self, key: str) -> None:
        if key not in self._metric_buttons or key == self._metric:
            return
        self._metric = key
        self._sync_buttons()
        self._render_legend()
        self.update()

    @staticmethod
    def _interval_for(hours: float) -> int:
        for h, _text, interval in _RANGE_DEFS:
            if h == hours:
                return interval
        return 20

    def _clear(self) -> None:
        self._labels = []
        self._series = {"power": [], "voltage": [], "current": []}
        self._total = []
        self._hover_index = -1
        self._hover_point = -1
        self._render_legend()
        self.update()

    # ---------- 悬浮数据提示（上游 interaction mode:'index' 同交互） ----------

    def _chart_rect(self) -> QRectF:
        """绘图区几何（paintEvent 与命中测试共用一套口径）。"""
        chart_top = min(48.0, self.height() * 0.3)
        return QRectF(
            _Y_AXIS_WIDTH, chart_top,
            self.width() - _Y_AXIS_WIDTH - _CURVE_MARGIN_R,
            self.height() - chart_top - _CURVE_MARGIN_B)

    def _nearest_point(self, x: float) -> int:
        """x 坐标 -> 最近的桶索引；不在绘图区或无数据返回 -1。"""
        if not self._labels:
            return -1
        rect = self._chart_rect()
        if x < rect.left() or x > rect.right():
            return -1
        n = len(self._labels)
        frac = (x - rect.left()) / max(rect.width(), 1e-6)
        return max(0, min(round(frac * max(n - 1, 1)), n - 1))

    def _tooltip_text(self, index: int) -> str:
        """悬浮文案：桶时间标题 + 当前行各序列读数（Σ 模式附总功率）。

        对齐上游 tooltip（mode:'index' 列出全部数据集 + Total footer），
        仅列出当前指标实际绘制的线，零值照列（与上游填 0 口径一致）。
        """
        if not 0 <= index < len(self._labels):
            return ""
        lines = [self._labels[index]]
        unit = self._unit()
        decimals = {_METRIC_POWER: 1, _METRIC_VOLTAGE: 2,
                    _METRIC_CURRENT: 2, _METRIC_TOTAL: 1}[self._metric]
        for name, _color, data in self._active_series():
            value = data[index] if index < len(data) else 0.0
            lines.append(f"{name}: {value:.{decimals}f}{unit}")
        if self._metric != _METRIC_TOTAL:
            total = self._total[index] if index < len(self._total) else 0.0
            lines.append(f"总功率: {total:.1f}W")
        return "\n".join(lines)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        idx = self._nearest_point(event.position().x())
        if idx != self._hover_index:
            self._hover_index = idx
            self.update()
        if idx >= 0:
            self._hover_point = idx
            QToolTip.showText(event.globalPosition().toPoint(),
                              self._tooltip_text(idx), self)
        else:
            QToolTip.hideText()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        self._hover_index = -1
        self.update()
        QToolTip.hideText()
        super().leaveEvent(event)

    def hover_index(self) -> int:
        """当前悬浮桶索引（无悬浮 -1）；测试断言用。"""
        return self._hover_index

    def _sync_buttons(self) -> None:
        for hours, btn in self._range_buttons.items():
            btn.refresh_style(hours == self._hours)
        for key, btn in self._metric_buttons.items():
            btn.refresh_style(key == self._metric)

    @staticmethod
    def _extract_series(raw: Any) -> list[list[float]]:
        """datasets 的一条组序列 -> [[float,...],...]，坏项静默丢弃。"""
        if not isinstance(raw, list):
            return []
        out: list[list[float]] = []
        for ds in raw:
            if not isinstance(ds, dict):
                continue
            data = ds.get("data")
            if not isinstance(data, list):
                continue
            pts: list[float] = []
            for v in data:
                try:
                    pts.append(float(v))
                except (TypeError, ValueError):
                    pts.append(0.0)
            out.append(pts)
        return out

    def _active_series(self) -> list[tuple[str, str, list[float]]]:
        """当前指标要画的 (名称, 颜色, 数据) 列表。"""
        if self._metric == _METRIC_TOTAL:
            return [("总功率", _TOTAL_LINE_COLOR[current_theme()],
                     self._total)]
        series = self._series.get(self._metric) or []
        names = ("C1", "C2", "C3", "USB-A")
        out: list[tuple[str, str, list[float]]] = []
        for i in range(min(4, len(series))):
            out.append((names[i], _PORT_COLORS[i], series[i]))
        return out

    def _unit(self) -> str:
        return {_METRIC_POWER: "W", _METRIC_VOLTAGE: "V",
                _METRIC_CURRENT: "A", _METRIC_TOTAL: "W"}[self._metric]

    def _render_legend(self) -> None:
        """图例圆点行：Σ 模式一项、其余指标四项（上游 renderChartLegend）。"""
        active = self._active_series()
        for i, (dot, text) in enumerate(
                zip(self._legend_dots, self._legend_texts)):
            if i < len(active):
                name, color, _data = active[i]
                dot.setStyleSheet(
                    f"background: {color}; border-radius: 4px;")
                text.setText(name)
                text.setVisible(bool(name))
                dot.setVisible(True)
            else:
                text.setText("")
                text.setVisible(False)
                dot.setVisible(False)

    def _legend_text_style(self) -> str:
        return (
            f"color: {SiColors.TEXT_SECONDARY}; font-size: 8pt; "
            "background: transparent;"
        )

    # ---------- 绘制 ----------

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            # 顶部档位行(~26px) + 图例行(~14px) + 两段 spacing(8px) 约 48，
            # 曲线区从其下起画；控件过矮时按比例收缩避免吃满全高。
            # 左缘固定留 _Y_AXIS_WIDTH 数值带（Y 轴刻度文字），带内不画
            # 任何底/竖线——透出卡片底色即轴带背景，保持简洁
            rect = self._chart_rect()
            if rect.height() < 20:
                rect.setHeight(20)
            active = self._active_series()
            if not self._labels or not active or not any(
                    ds for _n, _c, ds in active):
                self._paint_empty(painter, rect)
            else:
                self._paint_chart(painter, rect, active)
        finally:
            painter.end()

    def _paint_empty(self, painter: QPainter, rect: QRectF) -> None:
        painter.setPen(QColor(SiColors.TEXT_MUTED))
        painter.setFont(QFont("Microsoft YaHei UI", 9))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "暂无数据")

    def _paint_chart(self, painter: QPainter, rect: QRectF,
                     active: list[tuple[str, str, list[float]]]) -> None:
        # 曲线区底部基线之上画图；水平网格线画在 nice 刻度值上（3~5 条），
        # 左缘数值带标对应读数（W/V/A，0 线必标）
        painter.setFont(QFont("Microsoft YaHei UI", 8))

        # ---- Y 轴 nice 刻度：步长按数据最大值取整到好读的值 ----
        values = [v for _n, _c, ds in active for v in ds]
        step, y_max, divisions = _nice_scale(max(max(values), 1.0) * 1.08)
        # 数值格式：功率/Σ 取整 W，电压 1 位小数，电流 2 位小数；
        # 步长本身需要的小数位（如电流 0.5 步）与之取 max
        decimals = max({_METRIC_POWER: 0, _METRIC_VOLTAGE: 1,
                        _METRIC_CURRENT: 2, _METRIC_TOTAL: 0}[self._metric],
                       _tick_decimals(step))
        unit = self._unit()
        fm = painter.fontMetrics()
        tick_pen = QPen(QColor(SiColors.LINE), 1)
        for k in range(divisions + 1):
            value = y_max - step * k
            y = rect.top() + rect.height() * k / divisions
            if k:  # k=0 是上边界，只标数不画线
                painter.setPen(tick_pen)
                painter.drawLine(QPointF(rect.left(), y),
                                 QPointF(rect.right(), y))
            painter.setPen(QColor(SiColors.TEXT_MUTED))
            painter.drawText(
                QRectF(0.0, y - fm.height() / 2, rect.left() - 6, fm.height()),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"{_fmt_tick(value, decimals)}{unit}")

        n = len(self._labels)

        def _x(i: int) -> float:
            return rect.left() + rect.width() * i / max(n - 1, 1)

        def _y(v: float) -> float:
            return rect.bottom() - rect.height() * min(v, y_max) / y_max

        # 抽稀步长：点数超上限按等距取点（PowerCurveWidget 同款纪律）
        stride = max(1, (n + _CURVE_MAX_POINTS - 1) // _CURVE_MAX_POINTS)

        is_total = self._metric == _METRIC_TOTAL
        for idx, (name, color, data) in enumerate(active):
            if not data:
                continue
            pts = data[::stride]
            path = QPainterPath(QPointF(_x(0), _y(pts[0])))
            for i in range(1, len(pts)):
                path.lineTo(QPointF(
                    _x(i * stride), _y(pts[i])))
            if is_total:
                # Σ 模式：单线 + 主题化淡填充（上游 Total 线 fill:true）
                fill = QPainterPath(path)
                fill.lineTo(_x((len(pts) - 1) * stride), rect.bottom())
                fill.lineTo(rect.left(), rect.bottom())
                fill.closeSubpath()
                fill_color = QColor(color)
                fill_color.setAlpha(40)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(fill_color)
                painter.drawPath(fill)
            line_pen = QPen(QColor(color), 2 if is_total else 1.5)
            line_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            line_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(line_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
            # 峰值标注：每条线一个圆点（首个端口或 Σ 线附文字，避免拥挤）
            peak = max(pts)
            pi = pts.index(peak)
            px, py = _x(pi * stride), _y(peak)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color))
            painter.drawEllipse(QPointF(px, py), 3.0, 3.0)
            if is_total or idx == 0:
                label = f"{name} 峰值 {peak:.1f}{unit}"
                painter.setPen(QColor(SiColors.TEXT_PRIMARY))
                metrics = painter.fontMetrics()
                tx = px - metrics.horizontalAdvance(label) / 2
                tx = max(float(rect.left()),
                         min(tx, rect.right() - metrics.horizontalAdvance(label)))
                baseline = py - 8
                if baseline - metrics.ascent() < rect.top():
                    baseline = py + metrics.ascent() + 4
                painter.drawText(QPointF(tx, baseline), label)

        # 基线 + X 轴首/中/末时间刻度（labels 来自 /api/chart 桶时间）
        painter.setPen(QPen(QColor(SiColors.LINE), 1))
        painter.drawLine(QPointF(rect.left(), rect.bottom()),
                         QPointF(rect.right(), rect.bottom()))
        painter.setPen(QColor(SiColors.TEXT_MUTED))
        if self._labels:
            mid = (n - 1) // 2
            painter.drawText(
                QRectF(rect.left(), rect.bottom() + 2, 90, 14),
                Qt.AlignmentFlag.AlignLeft, self._labels[0])
            if 0 < mid < n - 1:
                painter.drawText(
                    QRectF(_x(mid) - 45, rect.bottom() + 2, 90, 14),
                    Qt.AlignmentFlag.AlignHCenter, self._labels[mid])
            painter.drawText(
                QRectF(rect.right() - 90, rect.bottom() + 2, 90, 14),
                Qt.AlignmentFlag.AlignRight, self._labels[-1])

        # 悬浮指示：命中桶画竖参考线 + 各线交点圆点（上游 index 模式观感）
        if 0 <= self._hover_index < n:
            hx = _x(self._hover_index)
            guide_pen = QPen(QColor(SiColors.TEXT_MUTED), 1)
            guide_pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(guide_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawLine(QPointF(hx, rect.top()),
                             QPointF(hx, rect.bottom()))
            for _name, color, data in active:
                if self._hover_index >= len(data):
                    continue
                value = data[self._hover_index]
                painter.setPen(Qt.PenStyle.NoPen)
                dot_color = QColor(color)
                dot_color.setAlpha(230)
                painter.setBrush(dot_color)
                painter.drawEllipse(
                    QPointF(hx, _y(min(value, y_max))), 3.0, 3.0)
