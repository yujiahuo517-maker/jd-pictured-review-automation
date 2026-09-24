# -*- coding:utf-8 -*-
from pathlib import Path
from typing import Dict, Optional, Callable, Tuple, Literal, Union, Any, List, Iterable

from openpyxl import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from .base_handler import SheetLikeHandler, BaseHandler
from ..cell_style import CellStyleCopier, CellStyle
from ..data import RowData
from ..header import Header
from ..recorder import Recorder

REWRITE_METHOD = Literal['make_num_dict_rewrite', 'make_num_dict']


class XLSXHandler(SheetLikeHandler):
    type: str = ...
    _header: Dict[Optional[str], Optional[Header]] = ...
    _header_row: Dict[Optional[str], int] = ...
    _data_col: int = ...
    _table: Optional[str] = ...
    _None_header_is_newest: Optional[bool] = ...
    _None_header_row_is_newest: Optional[bool] = ...
    _methods: Dict[str, Callable] = ...

    def __init__(self, recorder: Recorder):
        """"""
        ...

    @property
    def follow_styles(self) -> bool:
        """"""
        ...

    @property
    def new_row_height(self) -> Optional[float]:
        """"""
        ...

    @property
    def new_row_styles(self) -> Union[CellStyle, List[CellStyle], None]:
        """"""
        ...

    @property
    def link_style(self) -> Optional[CellStyle]:
        """"""
        ...

    @property
    def header_row(self) -> Dict[Optional[str], Optional[Header]]:
        ...

    def _get_header(self, ws: Worksheet = None) -> Header:
        """获取当前指定的table的header
        :param ws: Worksheet对象
        :return: Header对象
        """
        ...


def set_xlsx_header(recorder: Recorder,
                    header: Header,
                    table: str,
                    row: int) -> None:
    """设置xlsx文件的表头
    :param recorder: Recorder对象
    :param header: 表头列表或元组
    :param table: 工作表名称
    :param row: 行号
    :return: None
    """
    ...


def line2ws(ws: Worksheet, header: Header, row: int, col: int, data: Union[dict, list], rewrite_method: REWRITE_METHOD,
            rewrite: bool) -> bool:
    """把一行数据写入数据表，不设置样式
    :param ws: Worksheet对象
    :param header: Header对象
    :param row: 行号
    :param col: 列序号
    :param data: 行数据
    :param rewrite_method: 'make_num_dict_rewrite'或'make_num_dict'
    :param rewrite: 是否重写表头
    :return: 是否重写表头
    """
    ...


def line2ws_follow(ws: Worksheet, header: Header, row: int, col: int, data: Union[dict, list],
                   rewrite_method: REWRITE_METHOD, rewrite: bool, styles: Dict[int, CellStyleCopier],
                   height: Optional[float], new_row: bool) -> bool:
    """把一行数据写入数据表，并设置样式
    :param ws: Worksheet对象
    :param header: Header对象
    :param row: 行号
    :param col: 列序号
    :param data: 行数据
    :param rewrite_method: 'make_num_dict_rewrite'或'make_num_dict'
    :param rewrite: 是否重写表头
    :param styles: 样式对象了列表
    :param height: 行高，仅新行时有效
    :param new_row: 是否新行
    :return: 是否重写表头
    """
    ...


def data2ws(recorder: Recorder, ws: Worksheet, data: dict, coord: Tuple[int, int],
            header: Header, rewrite: bool, rewrite_method: REWRITE_METHOD) -> bool:
    """数据写入数据表
    :param recorder: Recorder对象
    :param ws: Worksheet对象
    :param data: 标准数据 {'type': 'data', 'data': [(1, 2, 3, 4)], 'coord': (0, 1)}
    :param coord: 要写入的坐标
    :param header: Header对象
    :param rewrite: 是否重写表头
    :param rewrite_method: 'make_num_dict_rewrite'或'make_num_dict'
    :return: 是否重写表头
    """
    ...


def data2ws_follow(handler: BaseHandler, ws: Worksheet, data: dict, coord: Tuple[int, int],
                   header: Header, rewrite: bool, rewrite_method: REWRITE_METHOD) -> None:
    """数据写入数据表，跟随上一行样式
    :param handler: BaseHandler对象
    :param ws: Worksheet对象
    :param data: 标准数据 {'type': 'data', 'data': [(1, 2, 3, 4)], 'coord': (0, 1)}
    :param coord: 要写入的坐标
    :param header: Header对象
    :param rewrite: 是否重写表头
    :param rewrite_method: 'make_num_dict_rewrite'或'make_num_dict'
    :return: 是否重写表头
    """
    ...


def data2ws_style(handler: BaseHandler, ws: Worksheet, data: dict, coord: Tuple[int, int],
                  header: Header, rewrite: bool, rewrite_method: REWRITE_METHOD) -> None:
    """数据写入数据表，并设置指定样式
    :param handler: BaseHandler对象
    :param ws: Worksheet对象
    :param data: 标准数据 {'type': 'data', 'data': [(1, 2, 3, 4)], 'coord': (0, 1)}
    :param coord: 要写入的坐标
    :param header: Header对象
    :param rewrite: 是否重写表头
    :param rewrite_method: 'make_num_dict_rewrite'或'make_num_dict'
    :return: 是否重写表头
    """
    ...


def styles2new_row(ws, styles, height, row):
    """"""
    ...


def styles2ws(**kwargs) -> None:
    """把样式写入数据表"""
    ...


def link2ws(**kwargs) -> None:
    """把擦后入到单元格"""
    ...


def img2ws(**kwargs) -> None:
    """把图片到单元格"""
    ...


def width2ws(**kwargs) -> None:
    """把列宽设置到数据表"""
    ...


def height2ws(**kwargs) -> None:
    """把行高设置到数据表"""
    ...


def get_wb(recorder: Recorder) -> Tuple[Workbook, bool]:
    """获取Workbook对象
    :param recorder: Recorder对象
    :return: (Workbook对象, 是否新文件)
    """
    ...


def get_ws(wb: Workbook, table: Optional[str], tables: List[str], new_file: bool) -> Tuple[Worksheet, bool]:
    """获取Worksheet对象
    :param wb: Workbook对象
    :param table: 表名，None代表活动表格
    :param tables: 工作簿所有表名组成的列表
    :param new_file: 是否新文件
    :return: (Worksheet对象, 是否新文件)
    """
    ...


def handle_new_sheet(handler: BaseHandler, ws: Worksheet, data: list) -> int:
    """从设置或第一条dict数据获取表头并向新表写入
    :param handler: BaseHandler对象
    :param ws: 数据表对象
    :param data: 对应数据表的数据列表
    :return: 开始写数据的行的前一行
    """
    ...


def get_ws_real_coord(coord: tuple, ws: Worksheet, header: Header) -> Tuple[int, int]:
    """返回真正写入xlsx文件的坐标
    :param coord: 已初步格式化的坐标，如(1, 2)、(0, 3)、(-3, -2)
    :param ws: Worksheet对象
    :param header: Header对象
    :return: 真正写入文件的坐标，tuple格式
    """
    ...


def get_xlsx_rows(header: Header, cols: Union[list, True],
                  begin_row: Optional[int], end_row: Optional[int],
                  sign_col: Union[str, int, bool], sign: Any,
                  deny_sign: bool, count: int, ws: Worksheet) -> List[RowData]:
    """获取xlsx文件指定行数据
    :param header: Header对象
    :param cols: 要获取的列，为True获取所有，可指定多列
    :param begin_row: 开始行号
    :param end_row: 结束行号，None为最后一行
    :param sign_col: 作为条件的列
    :param sign: 按这个值筛选目标行，可设置多个
    :param deny_sign: 是否反向匹配sign，即筛选指不是sign的行
    :param count: 获取多少条数据，为None获取所有
    :param ws: Worksheet对象
    :return: 获取到的数据列表
    """
    ...


def get_xlsx_rows_with_count(cols: Union[list, True], deny_sign: bool, header: Header, rows: Iterable,
                             begin_row: Optional[int], end_row: Optional[int],
                             sign_col: Union[str, int, bool], sign: Any, count: int) -> List[RowData]:
    """执行从xlsx中获取数据，有指定数量
    :param cols: 要获取的列，True为所有
    :param deny_sign: 是否反向匹配sign，即筛选指不是sign的行
    :param header: Header对象
    :param rows: 行组成的列表
    :param begin_row: 开始行号
    :param end_row: 结束行号，None为最后一行
    :param sign_col: 用于筛选数据的列
    :param sign: 用于筛选数据的值
    :param count: 数据总条数
    :return: 数据对象列表
    """
    ...


def get_xlsx_rows_without_count(cols: Union[list, True], deny_sign: bool, header: Header, rows: Iterable,
                                begin_row: Optional[int], end_row: Optional[int],
                                sign_col: Union[str, int, bool], sign: Any) -> List[RowData]:
    """执行从xlsx中获取全部数据
    :param cols: 要获取的列，True为所有
    :param deny_sign: 是否反向匹配sign，即筛选指不是sign的行
    :param header: Header对象
    :param rows: 行组成的列表
    :param begin_row: 开始行号
    :param end_row: 结束行号，None为最后一行
    :param sign_col: 用于筛选数据的列
    :param sign: 用于筛选数据的值
    :return: 数据对象列表
    """
    ...


def get_tables(path: Path) -> List[str]:
    """获取所有表格名称"""
    ...
