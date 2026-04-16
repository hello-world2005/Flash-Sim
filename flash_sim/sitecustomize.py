# -*- coding: utf-8 -*-
"""运行目录兼容修正。

当工作目录位于 flash_sim/ 时，`python -m flash_sim.cli` 默认找不到上一级的
顶层包目录。Python 启动时会自动尝试导入 sitecustomize，这里将仓库根目录加入
sys.path，使包模式执行在该目录下也可用。
"""

from __future__ import annotations

import os
import sys

_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_CURRENT_DIR)

if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
