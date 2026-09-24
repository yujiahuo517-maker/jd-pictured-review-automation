# -*- coding:utf-8 -*-
from pathlib import Path
from threading import Lock
from typing import Any, Optional, Union, List, Tuple, Literal

from .cell_style import CellStyle
from .data import RowData, RowStr
from .handlers.base_handler import BaseHandler
from .header import Header
from .setter import Setter

FILE_TYPE = Literal['txt', 'csv', 'json', 'jsonl', 'db', 'byte', 'b', '.txt', '.csv', '.json', '.jsonl', '.db']


class Recorder(object):
    _handler: BaseHandler = ...
    _lock: Lock = ...
    _cache_size: int = ...
    _path: Optional[Path] = ...
    _pause_add: bool = ...
    _pause_write: bool = ...
    _setter: Optional[Setter] = ...
    _file_exists: bool = ...
    _backup_path: str = ...
    _backup_times: int = ...
    _backup_interval: int = ...
    _backup_overwrite: bool = ...
    _settings: dict = ...
    _show_msg: bool = ...

    def __init__(self, path: Union[str, Path] = None,
                 cache_size: int = 1000,
                 file_type: Union[str, FILE_TYPE] = None):
        """用于缓存并记录数据，可在达到一定数量时自动记录，以降低文件读写次数，减少开销
        :param path: 保存的文件路径
        :param cache_size: 每接收多少条记录写入文件，0为不自动写入
        :param file_type: 指定文件类型，可选：'txt'、'csv'、'json'、'jsonl'、'db'、'byte'、'b'，
                          除 'byte' 和'b'，字符串前面可以加'.'。如为None则从path参数读取，path为None时本参数无效
        """
        ...

    @property
    def path(self) -> Path:
        """返回文件路径"""
        ...

    @property
    def cache_size(self) -> int:
        """缓存数据条数上限"""
        ...

    @property
    def type(self) -> str:
        """返回文件类型"""
        ...

    @property
    def data(self) -> Union[dict, list]:
        """返回当前保存在缓存的数据"""
        ...

    @property
    def set(self) -> Setter:
        """返回用于设置属性的对象"""
        ...

    @property
    def encoding(self) -> str:
        """返回编码格式，csv、json、jsonl、txt使用"""
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
        """返回表头，支持csv、xlsx和db格式"""
        ...

    @property
    def header_row(self) -> int:
        """返回表头所在行号，支持xlsx、csv"""
        ...

    @property
    def data_col(self) -> int:
        """返回默认记录数据列序号，支持xlsx、csv"""
        ...

    @property
    def right(self) -> Union[dict, list]:
        """返回当前right内容"""
        ...

    @property
    def left(self) -> Union[dict, list]:
        """返回当前left内容"""
        ...

    @property
    def table(self) -> Optional[str]:
        """返回当前使用的表名"""
        ...

    @property
    def tables(self) -> list:
        """返回所有表名"""
        ...

    def add_data(self,
                 data: Any,
                 coord: Union[list, Tuple[Union[None, int], Union[None, int, str]], str, int] = None,
                 table: Union[str, bool] = None) -> None:
        """添加数据，可一次添加多条数据
        :param data: 插入的数据，任意格式，可以为二维数据
        :param coord: 要添加数据的坐标、行号或路径，不同文件类型有所不同
        :param table: 要写入的数据表，仅支持xlsx和db格式。为None表示用set.table()方法设置的值，为True表示活动的表格
        :return: None
        """
        ...

    def add_link(self,
                 link: Optional[str],
                 coord: Union[int, str, tuple],
                 content: Any = None,
                 table: Union[str, True, None] = None) -> None:
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
                width: float = None,
                height: float = None,
                table: Union[str, True, None] = None) -> None:
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
                   coord: Union[str, tuple] = None,
                   rows: Union[int, str, tuple, list] = None,
                   cols: Union[int, str, tuple, list] = None,
                   replace: bool = True,
                   table: Union[str, True, None] = None) -> None:
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
                        rows: Union[int, str, list, tuple, True] = True,
                        table: Union[str, True, None] = None) -> None:
        """设置行高，可设置多行，仅xlsx格式时有效
        :param height: 行高，为dict（{1:30, 3:50}）时可为每行指定行高，此时rows参数无效
        :param rows: 行号，可指定多行（1、'1:4'、[1, 2, 3]），为Ture设置所有行
        :param table: 数据表名，仅支持xlsx格式。为None表示用set.table()方法设置的值，为Ture表示活动的表格
        :return: None
        """
        ...

    def add_cols_width(self, width: Union[float, dict],
                       cols: Union[int, str, list, tuple, True] = True,
                       table: Union[str, True, None] = None) -> None:
        """设置列宽，可设置多列，仅xlsx格式时有效
        :param width: 列宽，为dict（{1:30, '表头值':50}）时可为每列指定行高，此时cols参数无效
        :param cols: 用int表示列序号，str表示表头值，用Col('A')输入列号，用tuple设置连续起止列，用list指定离散列，为Ture设置所有列
        :param table: 数据表名，仅支持xlsx格式。为None表示用set.table()方法设置的值，为Ture表示活动的表格
        :return: None
        """
        ...

    def rows(self,
             cols: Union[str, int, list, tuple, True] = True,
             sign_col: Union[str, int, True] = True,
             signs: Any = None,
             deny_sign: bool = False,
             count: int = None,
             begin_row: Optional[int] = None,
             end_row: Optional[int] = None) -> List[Union[RowData, RowStr]]:
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

    def run_sql(self, sql: str, single: bool = True, commit: bool = False) -> Union[None, list]:
        """执行sql语句并返回结果，只支持db文件
        :param sql: sql语句
        :param single: 是否只获取一个结果
        :param commit: 是否提交到数据库
        :return: 查找到的结果，没有结果时返回None
        """
        ...

    def record(self) -> Path:
        """记录数据，返回文件路径"""
        ...

    def clear(self) -> None:
        """清空缓存中的数据"""
        ...

    def backup(self,
               folder: Union[str, Path, None] = None,
               name: str = None,
               overwrite: bool = None) -> Optional[Path]:
        """把当前文件备份到指定路径
        :param folder: 文件夹路径，为None使用内置路径（初始 'backup'）
        :param name: 保存的文件名，可不含后缀，为None使用内置路径文件名
        :param overwrite: 是否覆盖同名文件，为False时每次备份文件名添加当前时间，为None使用内置设置
        """
        ...

    def delete(self) -> None:
        """删除所指向的文件"""
        ...

    def _set_left_right(self, is_left: bool, data: Union[dict, list]) -> None:
        """设置left或right数据
        :param is_left: 是否left
        :param data: 数据
        :return: None
        """
        ...
