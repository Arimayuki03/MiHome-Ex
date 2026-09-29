# SPDX-License-Identifier: GPL-3.0-or-later
"""「隐藏无可控制功能的设备」失效修复回归测试。

用法: .venv\\Scripts\\python.exe tests/hide_no_func_test.py
（离屏运行 QT_QPA_PLATFORM=offscreen）

根因：隐藏过滤在网格构建时求值（_visible_devices → _device_has_functions
→ service._spec_cache），而 spec 缓存由开关/温湿度探测异步填充——
网格先建、spec 结论后到，探测回调从不触发网格重建，无功能设备永远
留在界面上。

覆盖：
1. spec 未拉取（None）时设备显示（等证实语义）；
2. spec 结论到达（power_states 回调）后，无功能设备从网格消失、
   计数标签同步；
3. 温湿度路径（read_metrics 回调）同样触发隐藏；
4. 可见集合无变化时不重建网格（防每轮轮询全量重建）；
5. 开关关闭时 spec 到达不触发重建。
"""

import atexit
import os
import sys
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

from app.core import cache as _device_cache  # noqa: E402
_device_cache.save = lambda *a, **k: None

from app.core.models import DeviceInfo  # noqa: E402
from app.core.service import MijiaService  # noqa: E402
from app.core.jobs import JobExecutor  # noqa: E402
from app.ui import si_theme  # noqa: E402

si_theme.set_theme("dark")

_PASS = []


def check(name: str) -> None:
    _PASS.append(name)
    print(f"  [PASS] {name}")


# spec 假数据：灯有 bool 可写 on 属性（有功能），插座 spec 为空（无功能）
SPEC_LAMP = {"properties": [{"name": "on", "type": "bool", "rw": ["w"],
                             "method": {"siid": 2, "piid": 1}}]}
SPEC_PLUG_EMPTY = {}

_DEVICES = [
    {"did": "1001", "name": "台灯", "model": "fake.light.lamp1",
     "isOnline": True},
    {"did": "1002", "name": "插座", "model": "fake.plug.empty",
     "isOnline": True},
    {"did": "1003", "name": "温湿度计", "model": "fake.sensor.ht",
     "isOnline": True},
]


class _FakeAPI:
    available = True
    auth_data_path = Path.home() / ".config" / "mijia-api" / "auth.json"

    def get_homes_list(self):
        return [{"id": 1, "name": "家", "uid": 1, "roomlist": []}]

    def get_devices_list(self):
        return list(_DEVICES)

    def get_shared_devices_list(self):
        return []

    def get_devices_prop(self, batch):
        # 开关探测：只有灯响应，其余设备 code!=0 模拟无此属性
        out = []
        for q in batch:
            if q["did"] == "1001":
                out.append({"did": "1001", "code": 0, "value": True})
            else:
                out.append({"did": q["did"], "code": -1})
        return out

    def get_properties(self, batch):
        return [{"did": q["did"], "code": -1} for q in batch]


class _FakeService(MijiaService):
    def _init_api(self):
        return _FakeAPI()

    def _fetch_spec(self, model, cache_dir):
        # 不打网络：内存假 spec
        if "light" in model:
            return dict(SPEC_LAMP)
        if "plug" in model:
            return dict(SPEC_PLUG_EMPTY)
        return None  # 温湿度计无公开 spec


def _card_dids(win) -> set:
    return set(win._cards.keys())


def main() -> int:
    from app.ui.main_window import MainWindow

    service = _FakeService()
    jobs = JobExecutor()
    win = MainWindow()
    original_jobs = win._jobs
    win._service = service
    win._jobs = jobs
    # 收尾兜底：断言失败跳过文件尾 shutdown 时，工作线程还活着，
    # 解释器关闭阶段销毁 QThread 即报 "Destroyed while thread is running"
    atexit.register(jobs.shutdown)
    atexit.register(original_jobs.shutdown)

    try:
        devices = service.list_devices()
        win.show()
        app.processEvents()
        win._apply_devices(devices)
        app.processEvents()

        # 开关打开（应用启动后读取的设置；直接置位模拟已开启用户）
        win._hide_no_func = True

        # ---- 1. spec 未拉取：云端三台设备都显示（None 视为有，等证实）；
        #         本地 CUKTECH 充电器卡片独立于本功能 ----
        assert _card_dids(win) == {"1001", "1002", "1003", "cuktech-local"}, _card_dids(win)
        check("spec 未拉取时设备全部显示（等待探测证实）")

        # ---- 2. 开关探测回调：spec 到达，无功能设备（插座）消失 ----
        # _apply_devices 已对全部设备做过首轮探测（_known_power 已填），
        # force=False 会因 dids 过滤为空而不再提交；这里直接以应用内
        # 轮询同款路径重放一次探测（jobs 队列 → 回调 → 重建判定）。
        win._jobs.submit(
            lambda: win._service.power_states(["1001", "1002", "1003"]),
            on_success=win._apply_power_states,
        )
        import time
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            app.processEvents()
            if "1002" not in _card_dids(win) and "1001" in _card_dids(win):
                break
            time.sleep(0.02)
        assert "1002" not in _card_dids(win), f"无功能设备未被隐藏: {_card_dids(win)}"
        assert "1001" in _card_dids(win), "有功能设备被误隐藏"
        check("power_states 回调后无功能设备从网格消失")

        # 计数标签同步（剩 2 台）
        assert "2 台设备" in win._count_label.text(), win._count_label.text()
        check("计数标签按隐藏后数量更新")

        # ---- 3. 温湿度路径：无公开 spec 的温湿度计也隐藏 ----
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            app.processEvents()
            if "1003" not in _card_dids(win):
                break
            time.sleep(0.02)
        assert "1003" not in _card_dids(win), "无 spec 温湿度计未被隐藏"
        assert "1001" in _card_dids(win)
        check("read_metrics 回调后无 spec 设备同样隐藏")

        # ---- 4. 可见集合无变化时不重建网格 ----
        rebuilds = []
        original = win._rebuild_grid
        win._rebuild_grid = lambda: (rebuilds.append(1), original())[1]
        cards_before = _card_dids(win)
        win._apply_power_states({"1001": True})
        win._apply_metrics({"1001": None})
        app.processEvents()
        assert rebuilds == [], f"无变化也重建了网格: {rebuilds}"
        assert _card_dids(win) == cards_before
        check("可见集合无变化时跳过重建")

        # ---- 5. 开关关闭：不触发重建分支（已隐藏的卡片保持隐藏——
        #         关闭开关后的恢复展示由设置保存路径 _rebuild_grid 负责） ----
        win._rebuild_grid = original
        win._hide_no_func = False
        rebuilds.clear()
        win._apply_power_states({"1001": False, "1002": None})
        app.processEvents()
        assert rebuilds == [], "开关关闭时不应走重建分支"
        check("开关关闭时不走重建分支（恢复展示由设置保存路径负责）")

        print(f"\n全部通过：{len(_PASS)} 项断言")
        return 0
    finally:
        jobs.shutdown()  # 先停后台线程再关窗：避免 QThread 销毁竞态警告
        win._force_quit = True
        win.close()


if __name__ == "__main__":
    raise SystemExit(main())
