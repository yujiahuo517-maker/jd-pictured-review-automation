# -*- coding:utf-8 -*-
from pathlib import Path
from typing import Union, Any, List, Tuple, Dict

from .cell_style import CellStyle
from .recorder import Recorder


class Setter(object):
    _recorder: Recorder = ...

    def __init__(self, recorder: Recorder): ...

    def cache_size(self, size: int) -> Setter:
        """设置缓存大小
        :param size: 缓存大小
        :return: 设置对象自己
        """
        ...

    def path(self, path: Union[str, Path], file_type: str = None) -> Setter:
        """设置文件路径
        :param path: 文件路径
        :param file_type: 不为None时指定文件类型，而不读取文件后缀名，记录二进制数据时输入 'byte' 或 'b'
        :return: 设置对象自己
        """
        ...

    def file_type(self, file_type: str) -> Setter:
        """指定文件类型
        :param file_type: 文件类型，记录二进制数据时输入 'byte' 或 'b'
        :return: 设置对象自己
        """
        ...

    def show_msg(self, on_off: bool = False) -> Setter:
        """设置是否显示运行信息
        :param on_off: bool表示开关
        :return: 设置对象自己
        """
        ...

    def auto_backup(self,
                    interval: int = None,
                    folder: Union[str, Path] = None,
                    overwrite: bool = None) -> Setter:
        """设置自动备份
        :param interval: 自动保存多少次时触发备份，为0表示不自动备份，为None时不修改已设置值（初始为0）
        :param folder: 备份文件存放文件夹路径，为None时不修改已设置值（初始为 'backup'）
        :param overwrite: 是否覆盖同名文件，为False时每个文件名都添加当前时间，为None时不修改已设置值（初始为False）
        :return: 设置对象自己
        """
        ...

    def table(self, name: Union[str, bool]) -> Setter:
        """设置默认表名
        :param name: 表名
        :return: 设置对象自己
        """
        ...

    def auto_new_header(self, on_off: bool = True) -> Setter:
        """数据中有表头不存在的列时是否自动添加到表头，只有xlsx、csv和db格式有效
        :param on_off: bool表示开关
        :return: 设置对象自己
        """
        ...

    def left(self, data: Any) -> Setter:
        """设置在数据左边补充的列
        :param data: 列表、元组或字符串，为字符串时则补充一列
        :return: 设置对象自己
        """
        ...

    def right(self, data: Any) -> Setter:
        """设置在数据右边补充的列
        :param data: 列表、元组或字符串，为字符串时则补充一列
        :return: 设置对象自己
        """
        ...

    def encoding(self, encoding: str) -> Setter:
        """设置文本类型文件的编码
        :param encoding: 编码格式
        :return: 设置对象自己
        """
        ...

    def header(self, header: Union[list, tuple],
               to_file: bool = True,
               row: int = None,
               table: Union[str, None, True] = None) -> Setter:
        """设置表头值
        :param header: 表头，列表或元组
        :param to_file: 是否写入到文件
        :param row: 指定写入文件的行号，不改变对象已设置的header_row属性，to_file为False时无效
        :param table: 表名，只xlsx格式文件有效，为True表示活动数据表，为None表示不改变设置
        :return: 设置对象自己
        """
        ...

    def header_row(self, num: int, table: Union[str, None, True] = None) -> Setter:
        """设置哪一行作为表头
        :param num: 行号
        :param table: 表名，为True表示活动数据表，为None表示不改变设置
        :return: 设置对象自己
        """
        ...

    def delimiter(self, delimiter: str) -> Setter:
        """设置csv文件分隔符
        :param delimiter: 分隔符
        :return: 设置对象自己
        """
        ...

    def quote_char(self, quote_char: str) -> Setter:
        """设置csv文件引用符
        :param quote_char: 引用符
        :return: 设置对象自己
        """
        ...

    def follow_styles(self, on_off: bool = True) -> Setter:
        """设置是否跟随上一行的样式，只有xlsx格式有效
        :param on_off: True或False
        :return: 设置对象自己
        """
        ...

    def new_row_height(self, height: float) -> Setter:
        """设置新行行高，只有xlsx格式有效
        :param height: 行高，传入None清空设置
        :return: 设置对象自己
        """
        ...

    def new_row_styles(self,
                       styles: Union[CellStyle, List[CellStyle], Tuple[CellStyle, ...],
                       Dict[Union[str, int], CellStyle], None]) -> Setter:
        """设置新行样式，只有xlsx格式有效，可传入多个，传入None则取消
        :param styles: 传入CellStyle对象设置整个新行，传入CellStyle对象组成的列表设置多个，传入None清空设置
        :return: 设置对象自己
        """
        ...

    def data_col(self, col: Union[str, int]) -> Setter:
        """设置默认填充数据的列
        :param col: 表头名或列序号，列序号从1开始，负数表示从后往前数，0表示新列（表头长度后一列），用Col('A')输入列号
        :return: 设置对象自己
        """
        ...

    def link_style(self, style: Union[CellStyle, True] = True) -> Setter:
        """设置单元格的链接样式
        :param style: CellStyle对象，为True时使用内置的默认样式
        :return: 设置对象自己
        """
        ...
