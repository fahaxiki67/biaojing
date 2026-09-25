# -*- coding: utf-8 -*-
"""`python -m biaojing.screen` 入口：转发到 screen_cli。

实现主体在 biaojing.screen_cli；本模块只提供更顺口的 -m 入口名。
"""

import sys

from .screen_cli import run

if __name__ == "__main__":
    sys.exit(run())
