# -*- coding:utf-8 -*-
"""
@Author   : g1879
@Contact  : g1879@qq.com
@Website  : https://DrissionPage.cn/DrissionRecord
@Copyright: (c) 2025 by g1879, Inc. All Rights Reserved.
"""
from .recorder import Recorder
from .header import Col

__version__ = '2.0.2'


def CellStyle():
    from .cell_style import CellStyle
    return CellStyle()
