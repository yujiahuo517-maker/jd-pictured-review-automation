# -*- coding:utf-8 -*-
from typing import Optional, Union, Tuple

from .header import Header


class RowData(dict):
    header: Header = ...
    row: int = ...
    _None_val: Optional[''] = ...

    def __init__(self, row: int, header: Header, None_val: Optional[''], seq: dict):
        """xlsx和csv文件行数据对象
        :param row: 行号
        :param header: Header对象
        :param None_val: 无数据时是None还是''
        :param seq: 数据内容，{列序号: 内容}
        """
        ...

    def col(self, key_or_num: Union[int, str], as_num: bool = True) -> Union[int, str]:
        """返回数据中指定列的列号或列序号
        :param key_or_num: 为int时表示列序号，为str时表示表头值
        :param as_num: 列以列号还是列序号形式返回
        :return: 返回列（'A'或1）
        """
        ...

    def coord(self, key_or_num: Union[int, str], col_num: bool = False) -> Tuple[int, Union[str, int]]:
        """返回数据中指定列的坐标
        :param key_or_num: 为int时表示列序号，为str时表示表头值
        :param col_num: 列以列号还是列序号形式返回
        :return: 返回(行号, 列号)
        """
        ...


class RowDict(dict):
    row: int = ...

    def __init__(self, row: int, seq: dict):
        """json或jsonl格式文件中，dict格式行产生的数据
        :param row: 行号
        :param seq: 行数据，dict格式
        """
        ...

    def __getitem__(self, item):
        """获取数据内容
        :param item: 为str时获取指定key的值，为int时表示序号，从1开始，0和1都为第一个元素
        :return: 数据内容，无指定数据时返回None
        """
        ...


class RowList(list):
    row: int = ...

    def __init__(self, row: int, seq: list):
        """json或jsonl格式文件中，list格式行产生的数据
        :param row: 行号
        :param seq: 行数据，list格式
        """
        ...

    def __getitem__(self, item):
        """获取数据内容
        :param item: 序号，从0开始
        :return: 数据内容，无指定数据时返回None
        """
        ...


class RowStr(str):
    row: Optional[int] = ...


class RowInt(int):
    row: Optional[int] = ...


class RowFloat(float):
    row: Optional[int] = ...


class RowNone(object):
    row: int = ...

    def __init__(self, row: int):
        """
        :param row: 行号
        """
        ...
