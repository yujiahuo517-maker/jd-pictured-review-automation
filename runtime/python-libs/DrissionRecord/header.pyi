# -*- coding:utf-8 -*-
from typing import Iterable, Union, Optional, Dict, Tuple, Any

from .data import RowData


class BaseHeader(object):
    _NUM_KEY: dict = ...
    _KEY_NUM: dict = ...
    _CONTENT_FUNCS: dict = ...

    @property
    def key_num(self) -> Dict[str, int]:
        """{str: int}格式的表头数据"""
        ...

    @property
    def num_key(self) -> Dict[int, str]:
        """{int: str}格式的表头数据"""
        ...

    def __iter__(self): ...


class Header(BaseHeader):

    def __init__(self, header: Iterable = None): ...

    def __getitem__(self, item: Union[int, str]): ...

    def __len__(self) -> int: ...

    def values(self):
        """返回所有表头值组成的列表"""
        ...

    def items(self):
        """(1, '表头值1')格式返回表头"""
        ...

    def make_row_data(self, row: int, row_values: dict, None_val: Optional[''] = None) -> RowData:
        """生成RowData对象
        :param row: 行号
        :param row_values: {列序号: 值}
        :param None_val: 空值是None还是''
        :return: RowData对象
        """
        ...

    def make_insert_list(self, data, file_type: Optional[str], rewrite: bool) -> Tuple[list, bool]:
        """生成写入文件list格式的新行数据
        :param data: 待处理行数据
        :param file_type: 文件类型，用于选择处理方法
        :param rewrite: 只用于对齐参数
        :return: 处理后的行数据
        """
        ...

    def make_change_list(self, line_data, data, col: int,
                         file_type: Optional[str], rewrite: bool) -> Tuple[list, bool]:
        """生产写入文件list格式的原有行数据
        :param line_data: 原有行数据
        :param data: 待处理行数据
        :param col: 要写入的列
        :param file_type: 文件类型，用于选择处理方法
        :param rewrite: 只用于对齐参数
        :return: (处理后的行数据, 是否重写表头)
        """
        ...

    def make_insert_list_rewrite(self, data, file_type: Optional[str], rewrite: bool) -> Tuple[list, bool]:
        """生产写入文件list格式的新行数据
        :param data: 待处理行数据
        :param rewrite: 是否需要重写表头
        :param file_type: 文件类型，用于选择处理方法
        :return: (处理后的行数据, 是否重写表头)
        """
        ...

    def make_change_list_rewrite(self, line_data, data, col: int, file_type, rewrite: bool) -> Tuple[list, bool]:
        """生产写入文件list格式的原有行数据
        :param line_data: 原有行数据
        :param data: 待处理行数据
        :param col: 要写入的列
        :param rewrite: 是否需要重写表头
        :param file_type: 文件类型，用于选择处理方法
        :return: (处理后的行数据, 是否重写表头)
        """
        ...

    def make_num_dict(self, *keys) -> Tuple[Dict[int, Any], bool, int]:
        """生成{int: val}的行数据，不考虑是否重写表头
        :return: (处理后的行数据, 是否重写表头, 表头长度)
        """
        ...

    def make_num_dict_rewrite(self, *keys) -> Tuple[Dict[int, Any], bool, int]:
        """生成{int: val}的行数据，虑是否重写表头
        :return: (处理后的行数据, 是否重写表头, 表头长度)
        """
        ...

    def get_key(self, key_or_num: Union[int, str]) -> Union[str, int]:
        """返回指定列序号对应的表头值，如该列没有值，返回列序号
        :param key_or_num: 列序号
        :return: 表头值或列序号
        """
        ...

    def get_col(self, key_or_num: Union[int, str]) -> str:
        """返回指定列序号或表头值对应的列号，先匹配表头，找不到就匹配·列号，无指定表头值时报错
        :param key_or_num: 表头值或列序号
        :return: 列号'A'
        """
        ...

    def get_num(self, key_or_num: Union[int, str]) -> Optional[int]:
        """返回指定列序号或表头值对应的列序号，找不到表头值时返回None
        :param key_or_num: 列号、表头值
        :return: 列号int
        """
        ...

    def _get_num(self, key_or_num: Union[int, str]) -> int:
        """内部使用，返回指定列序号或表头值对应的列序号，先按表头匹配，找不到再按列号匹配，都找不到就报错
        :param key_or_num: 列号、表头值
        :return: 列号int
        """
        ...

    def _num2num(self, num: int) -> int:
        """处理负数列序号，返回真实列序号，超出范围返回None，为0返回新列
        :param num: 列序号
        :return: 真实列号
        """
        ...


class ZeroHeader(Header):
    _OBJ: ZeroHeader = ...

    def _get_num(self, key_or_num: Union[int, str]) -> int:
        """返回指定列序号或表头值对应的列序号，找不到表头值时返回1
        :param key_or_num: 列号、表头值
        :return: 列号int
        """
        ...


def Col(key: str) -> int:
    """输入列号，输出列序号
    :param key: 列号'A'
    :return: 第几列
    """
    ...


def _get_column_letter(col_idx):
    letters = []
    while col_idx > 0:
        col_idx, remainder = divmod(col_idx, 26)
        if remainder == 0:
            remainder = 26
            col_idx -= 1
        letters.append(chr(remainder + 64))
    return ''.join(reversed(letters))
