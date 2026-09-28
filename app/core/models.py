# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Ex: 米家设备的 Windows 桌面控制端（扩展版）
# Copyright (C) 2026 MiHome-Ex contributors
"""界面层专用的数据模型。

刻意与 mijiaAPI 的原始 dict / 对象结构解耦：上游返回格式变化时只需调整
core.service 适配层，界面层永远只依赖这里的稳定类型。
"""

from dataclasses import dataclass
from typing import Literal

# 设备来源：cloud=米家云端（mijiaAPI 拉取），local=本地服务（如 CUKTECH 充电器 BLE 网关）
DeviceSource = Literal["cloud", "local"]


@dataclass
class DeviceInfo:
    """设备列表条目，只保留界面展示需要的最小字段集。"""

    did: str
    name: str
    model: str
    home_name: str
    room_name: str
    online: bool
    # 设备来源，默认 cloud：旧构造点与旧缓存文件（缺 source 键）反序列化时不报错
    source: DeviceSource = "cloud"

    @property
    def is_cuktech(self) -> bool:
        """CUKTECH 充电器判定：本地源的功能面板、专用控件共用同一标准。

        真机（2026-09-26）上报的 device_model 形如 "njcuk.fitting.ad1204_"，
        既不含 "cuktech" 也不以 "cu" 开头，故按米家生态链型号特征匹配：
        含 "njcuk" 或 "cuktech"（大小写不敏感）即归入充电器类别。
        本地源（source="local"）的设备固定为充电器，不受型号脏数据影响
        （真实 model 尾部可能带控制字符，如 "\\x03"）。
        """
        if self.source == "local":
            return True
        model = self.model.lower()
        return "cuktech" in model or "njcuk" in model


@dataclass
class PropInfo:
    """单个属性的元数据，控件工厂据此决定生成哪种控件。

    range 来自米家 spec 的 valueRange；value_list 为枚举选项，
    每项含 value/description，可能附带 desc_zh_cn 中文描述。
    """

    name: str
    desc: str
    type: str  # bool / int / uint / float / string
    readable: bool
    writable: bool
    range: tuple | None = None  # (min, max, step)
    value_list: list | None = None


@dataclass
class ActionInfo:
    name: str
    desc: str


@dataclass
class DeviceDetail:
    """点击某台设备后加载的控制面板所需全部信息。"""

    did: str
    name: str
    model: str
    props: list[PropInfo]
    actions: list[ActionInfo]


def is_speaker(device: DeviceInfo) -> bool:
    """小爱音箱判定：音频控制栏、语音入口共用同一标准。

    用包含匹配而非限定 xiaomi 前缀，第三方音箱（模型含
    wifispeaker）同样具备音量/静音与文本指令能力。
    """
    return "wifispeaker" in device.model
