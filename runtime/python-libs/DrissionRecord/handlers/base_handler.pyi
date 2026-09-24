# -*- coding:utf-8 -*-
from collections.abc import Callable
from pathlib import Path
from typing import Union, Optional, List, Any, Tuple

from ..cell_style import CellStyle
from ..data import *
from ..header import Header
from ..recorder import Recorder


class BaseHandler(object):
    data: Union[list, dict] = ...
    data_count: int = ...
    type: str = ...
    _recorder: Recorder = ...

    def __init__(self, recorder: Recorder):
        """
        :param recorder: Recorder对象
        """
        ...

    def __getattr__(self, item: str): ...

    @property
    def path(self) -> Path:
        """返回文件路径"""
        ...

    @property
    def delimiter(self) -> str:
        """返回csv文件分隔符"""
        ...

    @property
    def quote_char(self) -> str:
        """返回csv文件引用符"""
        ...

    @property
    def header(self) -> Header:
        """返回表头，支持csv、xlsx和db格式文件"""
        ...

    @property
    def header_row(self) -> int:
        """返回表头行号"""
        ...

    @property
    def right(self) -> Union[list, dict, None]:
        """返回自动拼接到行右边的数据，支持csv、xlsx、db、jsonl格式文件"""
        ...

    @property
    def left(self) -> Union[list, dict, None]:
        """返回自动拼接到行左边的数据，支持csv、xlsx、db、jsonl格式文件"""
        ...

    @property
    def table(self) -> Optional[str]:
        """返回当前使用的表名，支持xlsx和db格式"""
        ...

    @property
    def tables(self) -> List[str]:
        """返回所有表名，支持xlsx和db格式"""
        ...

    @property
    def encoding(self) -> str:
        """返回文件编码格式，支持txt、csv、json、jsonl文件格式"""
        ...

    @property
    def data_col(self) -> Union[int, str]:
        """返回默认写入数据的列，支持xlsx和csv文件格式"""
        ...

    def record(self) -> Path:
        """写入数据到文件，返回文件绝对路径"""
        ...

    def clear(self) -> None:
        """清空已保存的数据"""
        ...

    def run_sql(self, sql: str,
                single: bool,
                commit: bool) -> Union[None, list, tuple]:
        """执行sql语句并返回结果，只支持db文件
        :param sql: sql语句
        :param single: 是否只获取一个结果
        :param commit: 是否提交到数据库
        :return: 查找到的结果，没有结果时返回None
        """
        ...

    def rows(self,
             cols: Union[str, int, list, tuple, True] = True,
             sign_col: Union[str, int, True] = True,
             signs: Any = None,
             deny_sign: bool = False,
             count: int = None,
             begin_row: Optional[int] = None,
             end_row: Optional[int] = None) -> List[RowData, RowDict, RowList, RowStr]:
        """返回符合条件的行数据，可指定只要某些列。txt格式只有count、begin_row、end_row有效，不支持byte文件
        :param cols: 要获取的列，可以是多列，传入表头值或列序号，要用列号用Col('a')，为True获取所有列
        :param sign_col: 用于筛选数据的列，传入表头值或列序号，要用列号用Col('a')，为True获取所有行
        :param signs: 按这个值筛选目标行，可用list, tuple, set设置多个
        :param deny_sign: 是否反向匹配sign，即筛选值不是sign的行
        :param count: 获取多少条数据，为None获取所有
        :param begin_row: 数据开始的行，None表示header_row后面一行
        :param end_row: 数据结束的行，None表示最后一行
        :return: 数据对象组成的列表
        """
        ...

    def add(self, add_method: str, **data) -> None:
        """调用添加数据到缓存的方法
        :param add_method: 添加数据的方法，'add_data'、'add_link'等
        :param data: 数据参数
        :return: None
        """
        ...

    def add_data(self,
                 data: Any,
                 coord: Union[list, Tuple[Union[None, int], Union[None, int, str]], str, int],
                 table: Union[str, bool]) -> None:
        """添加数据，可一次添加多条数据
        :param data: 插入的数据，任意格式，可以为二维数据
        :param coord: 要添加数据的坐标，'A3'、(3, Col('A'))或(3, '表头')格式坐标，行号，json为路径列表
        :param table: 要写入的数据表，仅支持xlsx格式。为None表示用set.table()方法设置的值，为True表示活动的表格
        :return: None
        """
        ...

    def add_link(self,
                 link: Optional[str],
                 coord: Union[int, str, tuple],
                 content: Any,
                 table: Union[str, True, None]) -> None:
        """为单元格设置超链接，仅xlsx格式时有效
        :param link: 超链接，为None时删除链接
        :param coord: 单元格坐标，格式：'A3'、(3, Col('A'))或(3, '表头')格式坐标，行号
        :param content: 单元格文本
        :param table: 数据表名，仅支持xlsx格式。为None表示用set.table()方法设置的值，为Ture表示活动的表格
        :return: None
        """
        ...

    def add_img(self,
                img_path: Union[None, str, Path, dict],
                coord: Union[int, str, tuple],
                width: float,
                height: float,
                table: Union[str, True, None]) -> None:
        """向单元格设置图片，仅xlsx格式时有效
        :param img_path: 图片路径
        :param coord: 单元格坐标，格式：'A3'、(3, Col('A'))或(3, '表头')格式坐标，行号
        :param width: 图片宽
        :param height: 图片高
        :param table: 数据表名，仅支持xlsx格式。为None表示用set.table()方法设置的值，为Ture表示活动的表格
        :return: None
        """
        ...

    def add_styles(self,
                   styles: Union[CellStyle, dict, list, tuple, None],
                   coord: Union[str, tuple],
                   rows: Union[int, str, tuple, list],
                   cols: Union[int, str, tuple, list],
                   replace: bool,
                   table: Union[str, True, None]) -> None:
        """为单元格设置样式，可批量设置范围内的单元格，仅xlsx格式时有效
        :param styles: CellStyle对象，可用列表传入多个；为None则清除单元格样式；可用dict设置指定多个单元格样式，此时coord、rows、cols参数无效
        :param coord: 单元格坐标，str表示单个单元格'A1'或连续单元格'A1:C5'，tuple为单个单元格坐标(1, '表头')
        :param rows: 整行设置，int表示行号，str为'1:3'格式，可用列表传入多行
        :param cols: 整列设置，int表示列序号，str表示表头值，长度为2的tuple传入连续多列的起止列，可用列表传入多列
        :param replace: 是否直接覆盖所有已有样式，如为False只替换设置的属性
        :param table: 数据表名，仅支持xlsx格式。为None表示用set.table()方法设置的值，为Ture表示活动的表格
        :return: None
        """
        ...

    def add_rows_height(self, height: Union[float, dict],
                        rows: Union[int, str, list, tuple, True],
                        table: Union[str, True, None]) -> None:
        """设置行高，可设置多行，仅xlsx格式时有效
        :param height: 行高，为dict（{1:30, 3:50}）时可为每行指定行高，此时rows参数无效
        :param rows: 行号，可指定多行（1、'1:4'、[1, 2, 3]），为Ture设置所有行
        :param table: 数据表名，仅支持xlsx格式。为None表示用set.table()方法设置的值，为Ture表示活动的表格
        :return: None
        """
        ...

    def add_cols_width(self, width: Union[float, dict],
                       cols: Union[int, str, list, tuple, True],
                       table: Union[str, True, None]) -> None:
        """设置列宽，可设置多列，仅xlsx格式时有效
        :param width: 列宽，为dict（{1:30, '表头值':50}）时可为每列指定行高，此时cols参数无效
        :param cols: 用int表示列序号，str表示表头值，用Col('A')输入列号，用tuple设置连续起止列，用list指定离散列，为Ture设置所有列
        :param table: 数据表名，仅支持xlsx格式。为None表示用set.table()方法设置的值，为Ture表示活动的表格
        :return: None
        """
        ...

    def set_table(self, name: str) -> None:
        """设置当前操作的表格，支持xlsx和db文件格式
        :param name: 表名
        :return: None
        """
        ...

    def set_auto_new_header(self, on_off: bool) -> None:
        """设置写入数据时如表头不存在，是否自动添加，支持xlsx、csv、db文件格式
        :param on_off: bool表示开或关
        :return: None
        """
        ...

    def set_left(self, data: Union[dict, list]) -> None:
        """设置左边数据
        :param data: 数据值
        :return: None
        """
        ...

    def set_right(self, data: Union[dict, list]) -> None:
        """设置右边数据
        :param data: 数据值
        :return: None
        """
        ...

    def set_encoding(self, encoding: str) -> None:
        """设置文件编码，支持csv、txt、json、jsonl文件格式
        :param encoding: 编码
        :return: None
        """
        ...

    def set_header(self, header: Union[list, tuple],
                   table: Union[str, None, True],
                   to_file: bool,
                   row: int) -> None:
        """设置表头，支持xlsx和csv文件
        :param header: 表头，列表或元组
        :param table: 表名，只xlsx格式文件有效，为True表示活动数据表，为None表示不改变设置
        :param to_file: 是否写入到文件
        :param row: 指定写入文件的行号，不改变对象已设置的header_row属性，to_file为False时无效
        :return: None
        """
        ...

    def set_header_row(self, num: int,
                       table: Union[str, None, True]) -> None:
        """设置表头行号，支持xlsx和csv文件
        :param num: 行号
        :param table: 表名，为True表示活动数据表，为None表示不改变设置
        :return: None
        """
        ...

    def set_delimiter(self, delimiter: str) -> None:
        """设置csv文件分隔符
        :param delimiter: 分隔符
        :return: None
        """
        ...

    def set_quote_char(self, quote_char: str) -> None:
        """设置csv文件引用符
        :param quote_char: 引用符
        :return: None
        """
        ...

    def set_follow_styles(self, on_off: bool) -> None:
        """设置xlsx文件新行是否跟随上一行格式
        :param on_off: bool表示开关
        :return: None
        """
        ...

    def set_new_row_height(self, height: float) -> None:
        """设置xlsx文件新行行高
        :param height: 行高
        :return: None
        """
        ...

    def set_new_row_styles(self, styles: Union[CellStyle, List[CellStyle], None]) -> None:
        """设置xlsx文件新行样式，可传入多个，传入None则取消
        :param styles: 传入CellStyle对象设置整个新行，传入CellStyle对象组成的列表设置多个，传入None清空设置
        :return: None
        """
        ...

    def set_data_col(self, col: Union[str, int]) -> None:
        """设置默认填充数据的列，支持xlsx和csv文件
        :param col: 表头名或列序号，列序号从1开始，负数表示从后往前数，0表示新列（表头长度后一列），用Col('A')输入列号
        :return: None
        """
        ...

    def set_link_style(self, style: Union[CellStyle, True]) -> None:
        """设置单元格的链接样式
        :param style: CellStyle对象，为True时使用内置的默认样式
        :return: None
        """
        ...


class HasLeftRight(object):
    _recorder: Recorder = ...

    @property
    def _add_lr_method(self) -> Callable:
        """返回执行添加左右数据的方法"""
        ...


class SheetLikeHandler(BaseHandler):

    def __init__(self, recorder: Recorder):
        """
        :param recorder: Recorder对象
        """
        ...

    @property
    def auto_new_header(self) -> bool:
        """返回写入数据时如表头不存在，是否自动添加的设置，支持xlsx、csv、db文件格式"""
        ...

    def _parse_coord(self, coord) -> Tuple[int, Union[int, str]]:
        """格式化坐标，将坐标以(1, 1)格式返回（csv、xlsx）
        :param coord: 初始坐标
        :return: 坐标tuple
        """
        ...

    def _handle_data(self, data) -> list:
        """格式化数据，以列表或二维列表格式返回（csv、xlsx、db）
        :param data: 初始数据
        :return: 数据组成的列表
        """
        ...


class TextLikeHandler(BaseHandler):
    _fast: bool = ...
    _record_method: Callable = ...


def parse_coord(coord: Union[int, str, list, tuple, None],
                data_col: int = 1) -> Tuple[Optional[int], Optional[int]]:
    """处理坐标格式
    :param coord: 'A3'格式坐标、(3, 1)或(3, '列名')格式坐标、行号
    :param data_col: 列号，用于只传入行号的情况
    :return: 坐标tuple：(行, 列)坐标中的None表示新行或列
    """
    ...
