# 测试入口

本项目同时使用**脚本式断言**和 **pytest** 两种测试入口。直接执行所有 `tests/*_test.py` 并不总是有效：pytest 风格的文件没有 `if __name__ == "__main__"` 时，直接运行只会导入模块并以 0 退出，**不会执行任何断言**。

以下命令均从仓库根目录执行，`python` 指项目虚拟环境中的解释器（例如 `.venv\Scripts\python.exe`）。

## 环境变量

UI 测试需要离屏渲染：

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
```

测试不应连接真实米家账号、真实设备或真实 BLE 链路；涉及持久化的用例应把用户数据目录隔离到临时目录。

## 脚本式入口

```powershell
# mijiaAPI 接口兼容自检
python -m tests.smoke_test

# 核心与 CUKTECH 回归
python tests/cuktech_client_test.py
python tests/facade_test.py
python tests/cuktech_events_test.py
python tests/cuktech_credentials_test.py
python tests/cuktech_stage_script.py
python tests/hide_no_func_test.py

# UI / 主题 / 托盘回归
python tests/cuktech_panel_test.py
python tests/cuktech_history_test.py
python tests/cuktech_port_detail_test.py
python tests/cuktech_quality_test.py
python tests/cuktech_limits_test.py
python tests/cuktech_protocols_test.py
python tests/cuktech_stream_ui_test.py
python tests/cuktech_visuals_test.py
python tests/tray_hover_test.py
python tests/tray_span_test.py
python tests/theme_script.py

# 上游集成、场景/托盘与发布安全
python tests/upstream_integration_test.py
python tests/scene_tray_test.py
python tests/release_safety_test.py
```

`upstream_integration_test.py`、`scene_tray_test.py` 和 `release_safety_test.py` 按脚本式入口运行；其中 `release_safety_test.py` 也提供 pytest 函数，但 CI 的发布门禁用例使用上面的直接运行方式。

`hide_no_func_test.py` 是脚本式入口，**不要**用 `python -m pytest tests/hide_no_func_test.py` 代替：该文件没有 pytest 测试函数，pytest 只会收集到 0 项。

## pytest 入口

以下三个文件是 pytest 风格，必须通过 pytest 模块运行：

```powershell
python -m pytest tests/ble_server_manager_test.py -q
python -m pytest tests/cuktech_stage_pending_test.py -q
python -m pytest tests/tray_audio_bar_test.py -q
```

尤其不要用 `python tests/ble_server_manager_test.py`、`python tests/tray_audio_bar_test.py` 判断通过——它们会以 0 退出但不执行断言。

`ble_server_manager_test.py` 已改为自包含：测试使用 `tmp_path`/`monkeypatch` 伪造开发态和打包态路径，探活也使用内存桩，**不需要本机存在 `cuktech-ble-server` 仓库，也不会启动真实 BLE 后端**。真实发布构建由 CI 单独固定检出 v1.2.0 服务端源码，仅用于编译发布包，与本地单元测试的前置条件无关。

## 说明

- `_repro_hourly_clip.py`、`_repro_scroll_settings.py`、`tab_flash_probe.py` 是诊断脚本，不属于发布门禁。
- CI 在 Windows 上以 Python 3.10 / 3.12 双矩阵运行测试；本地版本、依赖与真实硬件条件可能不同。
- 自动化测试通过不等于真实米家账号、真实设备、蓝牙链路或 Windows GUI 已通过人工验证；发布说明不应把离线测试写成真机验收。
