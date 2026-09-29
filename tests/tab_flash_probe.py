# SPDX-License-Identifier: GPL-3.0-or-later
"""诊断脚本：抓取点击房间 tab 时短暂出现的顶层窗口。

用法: .venv\\Scripts\\python.exe tests/tab_flash_probe.py
（离屏运行 QT_QPA_PLATFORM=offscreen）

在应用级事件过滤器里监视 QEvent.Show / QEvent.WindowActivate：点击
「全屋」tab（含 CUKTECH 卡片）与「客厅」tab（不含）各一次，把每次
show 出来的顶层窗口类名/对象名/窗口标志打印出来，定位"短暂弹窗"。
"""

import os
import sys
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QEvent, QObject  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

app = QApplication.instance() or QApplication([])

from app.core import cache as _device_cache  # noqa: E402
_device_cache.save = lambda *a, **k: None

from app.core.models import DeviceInfo  # noqa: E402
from app.core.service import MijiaService  # noqa: E402
from app.core.jobs import JobExecutor  # noqa: E402
from app.ui import si_theme  # noqa: E402

si_theme.set_theme("dark")

_shown = []


class ShowProbe(QObject):
    def eventFilter(self, obj, event) -> bool:
        et = event.type()
        if et in (QEvent.Show, QEvent.WindowActivate, QEvent.WindowBlocked):
            if isinstance(obj, QWidget) and obj.isWindow():
                name = "shown" if et == QEvent.Show else (
                    "activated" if et == QEvent.WindowActivate else "blocked")
                _shown.append(
                    f"{name}: {type(obj).__name__} objName={obj.objectName()!r} "
                    f"title={obj.windowTitle()!r} visible={obj.isVisible()} "
                    f"flags=0x{int(obj.windowFlags()):x} parent={type(obj.parentWidget()).__name__ if obj.parentWidget() else None}")
        return False


def main() -> int:
    from app.ui.main_window import MainWindow

    service = MijiaService()
    jobs = JobExecutor()
    win = MainWindow()
    win._service = service
    win._jobs = jobs

    probe = ShowProbe()
    app.installEventFilter(probe)

    try:
        devices = service.list_devices()
        win.show()
        app.processEvents()
        win._apply_devices(devices)
        app.processEvents()

        _shown.clear()

        print("== 点击「全屋」tab ==")
        win._select_room("全屋")
        for _ in range(20):
            app.processEvents()
        for line in _shown:
            print("  ", line)
        if not _shown:
            print("   (无顶层窗口 show/activate)")

        _shown.clear()
        print("== 点击「本地」tab ==")
        win._select_room("本地")
        for _ in range(20):
            app.processEvents()
        for line in _shown:
            print("  ", line)
        if not _shown:
            print("   (无顶层窗口 show/activate)")

        _shown.clear()
        print("== 点击「客厅」tab ==")
        win._select_room("客厅")
        for _ in range(20):
            app.processEvents()
        for line in _shown:
            print("  ", line)
        if not _shown:
            print("   (无顶层窗口 show/activate)")

        # 再看 topLevelWidgets 中隐藏的疑似弹窗
        print("== 当前全部顶层窗口 ==")
        for w in QApplication.topLevelWidgets():
            if w.isWindow():
                print(f"   {type(w).__name__} objName={w.objectName()!r} "
                      f"visible={w.isVisible()} title={w.windowTitle()!r}")
        return 0
    finally:
        win._force_quit = True
        win.close()
        jobs.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
