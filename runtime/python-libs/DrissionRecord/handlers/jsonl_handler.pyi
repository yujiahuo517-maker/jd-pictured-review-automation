# -*- coding:utf-8 -*-
from typing import Any, Callable, Union, Optional, List, TextIO

from .base_handler import TextLikeHandler, BaseHandler
from ..data import *


class JSONLHandler(TextLikeHandler):

    @property
    def _add_lr_method(self) -> Callable:
        """向数据添加left和right数据的方法"""
        ...

    def _handle_data(self, data: Any) -> list:
        """格式化数据，以列表格式返回
        :param data: 初始数据
        :return: 数据组成的列表
        """
        ...

    def _record_fast(self) -> None:
        """执行快速记录到文件"""
        ...

    def _record_slow(self) -> None:
        """执行慢速记录到文件"""
        ...


def handle_txt_lines(data_lst: list, lines: list) -> None:
    """txt、json、jsonl格式处理修改中间行时的逻辑
    :param data_lst: 数据总列表
    :param lines: readlines()从文件读取的原数据列表
    :return: None
    """
    ...


def handle_jsonl_data(data: Union[dict, list]) -> None:
    """处理jsonl格式单个数据的方法，对应handle_txt_lines()的method
    :param data: 要写入的数据
    :return: None
    """
    ...


def get_jsonl_rows(handler: BaseHandler, cols: Union[list, True],
                   begin_row: Optional[int], end_row: Optional[int],
                   sign_col: Union[str, int, bool], sign: Any,
                   deny_sign: bool, count: int) -> List[RowData]:
    """获取csv文件指定行数据
    :param handler: BaseHandler对象
    :param cols: 要获取的列，为True获取所有，可指定多列
    :param begin_row: 开始行号
    :param end_row: 结束行号，None为最后一行
    :param sign_col: 作为条件的列
    :param sign: 按这个值筛选目标行，可设置多个
    :param deny_sign: 是否反向匹配sign，即筛选指不是sign的行
    :param count: 获取多少条数据，为None获取所有
    :return: 获取到的数据列表
    """
    ...


def get_jsonl_row_key_is_True(line: Union[list, dict], res: list, ind: int, cols: list) -> None:
    """把jsonl文件一行所有列数据加入到结果集合中
    :param line: dict或list格式的行数据
    :param res: 获取到的所有行数据
    :param ind: 行号
    :param cols: 仅用于与另一个方法对应
    :return: None
    """
    ...


def get_jsonl_row_key_not_True(line: Union[list, dict], res: list, ind: int, cols: list) -> None:
    """把jsonl文件一行指定列数据加入到结果集合中
    :param line: dict或list格式的行数据
    :param res: 获取到的所有行数据
    :param ind: 行号
    :param cols: 要获取的列，为str时用于dict数据，为int时可用于list和dict（序号从1开始）数据
    :return: None
    """
    ...


def get_jsonl_rows_with_count(lines: Union[TextIO, list], begin_row: Optional[int], end_row: Optional[int],
                              sign_col: Union[str, int, bool], sign: Any, deny_sign: bool,
                              cols: Union[list, True], res: list, count: int, method: Callable) -> None:
    """执行从jsonl或json文件中获取数据，有指定数量。两种文件区别在于行数据传入格式
    :param lines: jsonl文件传入文件读取对象，json文件传入读取到的list
    :param begin_row: 开始行号
    :param end_row: 结束行号，None为最后一行
    :param sign_col: 作为条件的列
    :param sign: 按这个值筛选目标行，可设置多个
    :param deny_sign: 是否反向匹配sign，即筛选指不是sign的行
    :param cols: 要获取的列，为True获取所有，可指定多列
    :param res: 保存最终数据的list
    :param count: 获取多少条数据，为None获取所有
    :param method: 读取每行数据的方法，jsonl和json不一样
    :return: None
    """
    ...


def handle_line_jsonl(line: str) -> Union[dict, list]:
    """读取数据时从文本解析json
    :param line: 一行数据
    :return: dict或list格式的数据
    """
    ...


def data2DataWithRow(data: Union[str, int, float, dict, list, None],
                     row: int) -> Union[RowInt, RowDict, RowList, RowStr, RowFloat, RowNone]:
    """把普通数据包装成行数据类型
    :param data: 原数据
    :param row: 行号
    :return: 行数据类型
    """
    ...
