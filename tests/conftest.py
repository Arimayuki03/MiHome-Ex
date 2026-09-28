# SPDX-License-Identifier: GPL-3.0-or-later
"""pytest 共享 fixture。

app/theme 两个 fixture 原先依赖的 conftest 从未入库，全套测试
collect 阶段即失败；此处按各测试文件的使用约定补齐。
"""
import pytest

from PySide6.QtWidgets import QApplication

from app.ui.theme_service import apply_theme


@pytest.fixture(scope="session")
def app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(params=["dark", "light"])
def theme(request, app):
    apply_theme(request.param)
    yield request.param
