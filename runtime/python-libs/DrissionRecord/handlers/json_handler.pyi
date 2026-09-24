# -*- coding:utf-8 -*-
from typing import Any, Union

from .base_handler import TextLikeHandler


class JSONHandler(TextLikeHandler):

    def _handle_data(self, data: Any) -> Any:
        """如果是独立数据直接返回，否则为其添加左右数据再返回
        :param data: 初始数据
        :return: 处理后的数据
        """
        ...


def _parse_coord(coord: Any) -> Union[list, tuple]:
    """解析坐标
    :param coord: 初始坐标
    :return: 处理后的坐标
    """
    ...


def nav(data: Any, parts: list) -> tuple:
    """在json数据中获取指定位置数据的坐标
    :param data: 在这个数据中定位
    :param parts: 位置信息组成的列表
    :return: 返回父元素和最后一个索引组成的tuple
    """
    ...
