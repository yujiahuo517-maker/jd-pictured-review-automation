# -*- coding:utf-8 -*-
from pathlib import Path
from typing import Union, Any, List, Iterable, Literal

from .handlers.base_handler import BaseHandler
from .header import Header

REWRITE_METHOD = Literal['make_num_dict_rewrite', 'make_num_dict']


def get_first_dict_csv(data: list) -> dict:
    """判断数据集第一条是否dict，如果第一条是二维数据，判断其第一条是否dict，是则返回它
    :param data: 数据列表
    :return: 第一条dict格式数据
    """
    ...


def get_first_dict_xlsx(data: list) -> dict:
    """判断数据集第一条是否dict，如果第一条是二维数据，判断其第一条是否dict，是则返回它
    :param data: 数据列表
    :return: 第一条dict格式数据
    """
    ...


def remove_end_Nones(in_list: list) -> list:
    """去除列表后面所有None
    :param in_list: 要处理的list
    :return: 处理后的列表
    """
    ...


def get_usable_path(path: Union[str, Path], is_file: bool = True, parents: bool = True) -> Path:
    """检查文件或文件夹是否有重名，并返回可以使用的路径
    :param path: 文件或文件夹路径
    :param is_file: 目标是文件还是文件夹
    :param parents: 是否创建目标路径
    :return: 可用的路径，Path对象
    """
    ...


def make_valid_name(full_name: str) -> str:
    """获取有效的文件名
    :param full_name: 文件名
    :return: 可用的文件名
    """
    ...


def get_long(txt: str) -> int:
    """返回字符串中字符个数（一个汉字是2个字符）
    :param txt: 字符串
    :return: 字符个数
    """
    ...


def process_content_xlsx(content: Any) -> Union[None, int, str, float]:
    """处理单个单元格要写入的数据
    :param content: 未处理的数据内容
    :return: 处理后的数据
    """
    ...


def process_content_json(content: Any) -> Union[None, int, str, float]:
    """处理单个单元格要写入的数据
    :param content: 未处理的数据内容
    :return: 处理后的数据
    """
    ...


def data2str(content: Any) -> str:
    """处理单个单元格要写入的数据，以str格式输出
    :param content: 未处理的数据内容
    :return: 处理后的数据
    """
    ...


def process_nothing(content: Any) -> Any:
    """不处理直接返回数据"""
    ...


def get_real_row(row: int, max_row: int) -> int:
    """获取返回真正写入文件的行号
    :param row: 输入的行号
    :param max_row: 最大行号
    :return: 真正的行号
    """
    ...


def no_left_right(handler, data): ...


def add_left_right(handler: BaseHandler, data: Iterable) -> Union[list, dict]:
    """将传入的一维数据转换为列表或字典形式，添加前后列数据
    :param handler: Recorder对象
    :param data: 要处理的数据
    :return: 转变成列表或字典形式的数据
    """
    ...


def do_nothing(*args, **kwargs) -> None:
    """什么都不干"""
    ...


def get_key_cols(cols: Union[str, int, list, tuple, bool], header: Header) -> List[int]:
    """获取作为关键字的列，可以是多列
    :param cols: 列号或列名，或它们组成的list或tuple
    :param header: Header格式
    :return: 列序号列表
    """
    ...


def is_single_data(data: Any) -> bool:
    """判断数据是否独立数据"""
    ...


def is_1D_data(data: Any) -> bool:
    """判断传入数据是否一维数据"""
    ...
