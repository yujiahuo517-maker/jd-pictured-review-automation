# -*- coding:utf-8 -*-
from _csv import Reader, Writer
from typing import TextIO, Union, Optional, Any, List, Tuple

from .base_handler import SheetLikeHandler, TextLikeHandler
from ..data import RowData
from ..header import Header


class CSVHandler(SheetLikeHandler, TextLikeHandler):
    _header: Optional[Header] = ...
    _header_row: int = ...
    _data_col: int = ...

    def _header2file(self, header: Header, row: int) -> None:
        """将header数据写入文件
        :param header: Header对象
        :param row: 表头所在行号
        :return: None
        """
        ...


def get_csv(recorder) -> Tuple[TextIO, bool]:
    """获取csv文件对象及是否新文件
    :param recorder: Recorder对象
    :return: (文件对象, 是否新文件)
    """
    ...


def get_and_set_csv_header(handler: CSVHandler,
                           new_csv: bool,
                           file: TextIO,
                           writer: Writer) -> None:
    """从csv获取表头或把已获取的表头设置到新csv
    :param handler: CSVHandler对象
    :param new_csv: 是否新csv文件
    :param file: 文件对象
    :param writer: csv Writer对象
    :return: None
    """
    ...


def get_csv_rows(handler: CSVHandler,
                 header: Header,
                 cols: Union[list, True],
                 begin_row: Optional[int],
                 end_row: Optional[int],
                 sign_col: Union[str, int, bool],
                 sign: Any,
                 deny_sign: bool,
                 count: int) -> List[RowData]:
    """获取csv文件指定行数据
    :param handler: CSVHandler对象
    :param header: Header对象
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


def get_csv_rows_key_is_True(line: Union[list, dict],
                             res: list,
                             header: Header,
                             ind: int,
                             cols: list,
                             header_len: int) -> None:
    """把csv文件一行所有列数据加入到结果集合中
    :param line: 行数据
    :param res: 保存最终结果的列表
    :param header: Header对象
    :param ind: 行号
    :param cols: 没有作用
    :param header_len: 表头长度
    :return: None
    """
    ...


def get_csv_rows_key_not_True(line: Union[list, dict],
                              res: list,
                              header: Header,
                              ind: int,
                              cols: list,
                              header_len: int) -> None:
    """把csv文件一行指定列数据加入到结果集合中
    :param line: 行数据
    :param res: 保存最终结果的列表
    :param header: Header对象
    :param ind: 行号
    :param cols: 要获取的列，True为所有
    :param header_len: 没有作用
    :return: None
    """
    ...


def get_csv_rows_with_count(lines: Reader,
                            begin_row: Optional[int],
                            end_row: Optional[int],
                            sign_col: Union[str, int, bool],
                            sign: Any,
                            deny_sign: bool,
                            cols: Union[list, True],
                            res: list,
                            header: Header, count: int) -> List[RowData]:
    """执行从csv中获取数据，有指定数量
    :param lines: csv Reader对象
    :param begin_row: 开始行号
    :param end_row: 结束行号，None为最后一行
    :param sign_col: 用于筛选数据的列
    :param sign: 用于筛选数据的值
    :param deny_sign: 是否反向匹配sign，即筛选指不是sign的行
    :param cols: 要获取的列，True为所有
    :param res: 结果列表
    :param header: Header对象
    :param count: 数据总条数
    :return: 数据对象列表
    """
    ...
