# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""更新检查与提示对话框的离屏回归测试。

覆盖版本比较、release 字段解析、Markdown 更新日志渲染与兜底文案。
与 theme_test 一样禁写运行数据（isolate 临时目录）。
"""

import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from tests._isolate import isolate

isolate()

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

app = QApplication([])

from app.ui.theme_service import apply_theme

apply_theme("dark")

from app.core.update_checker import ReleaseInfo, is_newer

# ---------- 1. 版本比较 ----------
assert is_newer("v0.5.0", "0.2.0"), "0.5.0 应比 0.2.0 新"
assert not is_newer("v0.2.0", "0.2.0"), "同版本不应判定为新"
assert is_newer("0.10.0", "0.9.9"), "两位数版本段应按数值比较"
assert is_newer("v1.0.0-beta", "0.9.0"), "预发布后缀不应参与比较"
assert not is_newer("0.9", "0.9.0"), "位数不同按 0 补齐后相等"
print("1. 版本比较 OK")

# ---------- 2. release 字段 ----------
info = ReleaseInfo(
    tag="v0.5.0",
    url="https://example.com/release",
    name="0.5.0 - 优化",
    body="## 更新内容\n\n- **桌面挂件**：新增筛选\n- 修复若干问题",
    published_at="2026-10-22T12:00:00Z",
)
assert info.body.startswith("## 更新内容")
print("2. release 字段 OK")

# ---------- 3. 更新对话框：Markdown 渲染 ----------
from app.ui.update_dialog import UpdateDialog

dlg = UpdateDialog(info, None)
dlg.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
dlg.show()
app.processEvents()

assert "发现新版本" in dlg._title.text()
assert dlg._brand.text() == "米家 - Windows"
# 品牌名与标题同样字号、同样高亮，一眼看出是哪个软件在更新
assert dlg._brand.font().pointSizeF() == dlg._title.font().pointSizeF()
# 右上角 × 已去掉，由底部「稍后提醒」承担关闭
assert not hasattr(dlg, "_close_btn"), "更新对话框不应再有关闭按钮"
assert "v0.5.0" in dlg._version_line.text(), dlg._version_line.text()
assert "2026-10-22" in dlg._meta.text(), dlg._meta.text()
plain = dlg._notes.toPlainText()
assert "桌面挂件" in plain, f"更新日志未渲染: {plain!r}"
assert "**" not in plain, f"Markdown 粗体未渲染: {plain!r}"
assert "更新内容" in plain
assert dlg._download_btn.text().strip() == "前往下载"
assert dlg._later_btn.text() == "稍后提醒"
print("3. 更新对话框 Markdown 渲染 OK")

# ---------- 4. 无正文兜底 ----------
empty = ReleaseInfo(tag="v0.6.0", url="https://example.com", name="", body="")
dlg2 = UpdateDialog(empty, None)
dlg2.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
dlg2.show()
app.processEvents()
fallback = dlg2._notes.toPlainText()
assert "未提供更新说明" in fallback, f"空正文兜底文案缺失: {fallback!r}"
assert "v0.6.0" in dlg2._version_line.text()
print("4. 空正文兜底 OK")

# ---------- 5. 主题切换重渲染 ----------
apply_theme("light")
dlg.retheme()
plain_light = dlg._notes.toPlainText()
assert "桌面挂件" in plain_light, "切换主题后更新日志丢失"
print("5. 主题切换重渲染 OK")

dlg.done(0)
dlg2.done(0)
app.processEvents()
print("UPDATE REGRESSION ALL PASS")
