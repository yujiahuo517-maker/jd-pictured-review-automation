# -*- coding:utf-8 -*-
from typing import List, Any

from .base_handler import TextLikeHandler
from ..data import *
from ..recorder import Recorder


class TXTHandler(TextLikeHandler):
    def __init__(self, recorder: Recorder):
        """
        :param recorder: Recorder对象
        """
        ...

    def rows(self, **kwargs) -> List[RowData, RowDict, RowList, RowStr]:
        """返回符合条件的行数据，可指定只要某些列
        :param count: 获取多少条数据，为None获取所有
        :param begin_row: 数据开始的行，None表示header_row后面一行
        :param end_row: 数据结束的行，None表示最后一行
        :return: 数据对象组成的列表
        """
        ...

    def _handle_data(self, data:Any) -> list:
        """格式化数据，以列表格式返回"""
        ...

    def _record_fast(self) -> None:
        """执行快速记录到文件"""
        ...

    def _record_slow(self) -> None:
        """执行慢速记录到文件"""
        ...


def slowData2list(data_lst, lines) -> None:
    """处理要写入文件的数据
    :param data_lst: 数据组成的列表
    :param lines: 初始文件行
    :return: None
    """
    ...
