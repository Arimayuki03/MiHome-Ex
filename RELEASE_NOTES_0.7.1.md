# MiHome-Ex 0.7.1 更新内容

0.7.0 的补丁版本，只修复酷态科（CUKTECH）充电器面板打不开的问题，其余功能与 0.7.0 一致。

## 修复

- **酷态科面板打不开**：0.7.0 在打开设备详情时，对所有面板无条件连接 `name_changed` / `metrics_config_changed` 信号；CUKTECH 专用面板只提供 `metrics_updated`，点击充电器卡片会抛 `AttributeError: 'CuktechPanel' object has no attribute 'name_changed'`，异常发生在面板显示之前，表现为点击无反应。现改为按面板实际能力接线，并补上对称的断开逻辑。
- **回归测试**：`tests/cuktech_stream_ui_test.py` 新增「CUKTECH 详情面板可打开」断言（桩住 `exec` 后走真实 `_on_open_device` 路径），避免同类问题再次漏过测试。

## 兼容性

- 版本三处同步为 `0.7.1`；发布标签为 `v0.7.1`。
- 内置 CUKTECH BLE 服务端固定点不变：v2.1.0 提交 `6eb8f990be1cbb69ec73d7917b0b850974eba784`。
- 米家设备控制、桌面挂件、托盘、定时开关、远程/本机 BLE 部署方式与 0.7.0 完全一致，无需迁移配置或缓存。
