# -*- coding:utf-8 -*-
from sqlite3 import Connection, Cursor
from typing import Optional

from .base_handler import SheetLikeHandler


class DBHandler(SheetLikeHandler):
    _conn: Optional[Connection] = ...
    _cur: Optional[Cursor] = ...
    _table: Optional[str] = ...

    def _connect(self) -> None:
        """连接数据库"""
        ...

    def _close_connection(self) -> None:
        """关闭数据库 """
        ...

    def _to_database(self,
                     data_list: list,
                     table: str,
                     tables: dict) -> None:
        """把数据批量写入指定数据表
        :param data_list: 要写入的数据组成的列表
        :param table: 要写入数据的数据表名称
        :param tables: 数据库中数据表和列信息
        :return: None
        """
        ...


def ok_list_db(data_list: list) -> list:
    """格式化要写入db的数据
    :param data_list: 数据组成的列表
    :return: 格式化后的列表
    """
    ...


def add_db_col(cur: Cursor, table: str, key: str) -> None:
    """为数据库添加一列
    :param cur: 连接对象
    :param table: 表名
    :param key: 列名
    :return: None
    """
    ...
