# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Ex: 米家设备的 Windows 桌面控制端（扩展版）
# Copyright (C) 2026 MiHome-Ex contributors
"""JSON 持久化公共基础：数据目录解析、旧位置迁移、原子写入。

打包形态曾把配置写在 exe 同目录，装进 Program Files 等受保护位置后
写入失败且被静默吞掉，表现为设置每次启动重置；现统一写入
%LOCALAPPDATA%\\MiHome-Ex\\。历史位置（exe 同目录、旧项目名
MiHome-Windows）的已有文件按优先级一次性迁移：exe 同目录优先于
旧数据目录（后者可能已经历过一次 exe 同目录迁移，内容更旧）。
开发形态仍为仓库根目录（文件均已 gitignore）。
"""

import json
import os
import sys
from pathlib import Path

_APP_DIR_NAME = "MiHome-Ex"
_LEGACY_DIR_NAME = "MiHome-Windows"
# 旧版写在 exe 同目录的全部数据文件，发现即迁移
_KNOWN_FILES = ("settings.json", "tray.json", "workbench.json", "devices_cache.json")
_migrated = False


def data_dir() -> Path:
    from app import is_packaged

    if is_packaged():
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / _APP_DIR_NAME
    return Path(__file__).resolve().parents[2]


def data_file(filename: str) -> Path:
    """数据文件完整路径，首次访问时尝试从历史位置迁移。"""
    global _migrated
    if getattr(sys, "frozen", False) and not _migrated:
        _migrated = True
        _migrate_legacy_files()
    return data_dir() / filename


def _migrate_legacy_files() -> None:
    legacy_dir = Path(sys.executable).resolve().parent
    target_dir = data_dir()
    if legacy_dir == target_dir:
        return
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in _KNOWN_FILES:
            target = target_dir / name
            if target.exists():
                continue
            # exe 同目录优先：旧数据目录的文件若曾经历过 exe 同目录
            # 迁移，其内容一定不新于 exe 同目录的现役文件
            legacy = legacy_dir / name
            if legacy.exists():
                # copyfile 保留旧文件，新位置写坏时下次启动还能再迁
                target.write_bytes(legacy.read_bytes())
                continue
            legacy_appdir = target_dir.parent / _LEGACY_DIR_NAME / name
            if legacy_appdir.exists():
                target.write_bytes(legacy_appdir.read_bytes())
    except OSError:
        pass


def read_json(path: Path, default: dict) -> dict:
    """读取 JSON dict；文件缺失、损坏或类型不对时返回 default。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    if not isinstance(raw, dict):
        return default
    return raw


def write_json(path: Path, data: dict) -> None:
    """原子写入：先写同目录临时文件再 os.replace，进程被杀不会留半截文件。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass
