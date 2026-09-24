# -*- coding:utf-8 -*-
from pathlib import Path
from typing import Union, Literal, Optional

from DrissionPage import SessionOptions
from DrissionPage._base.base import BasePage
from requests import Session

from .drissionGet import DrissionGet

FILE_EXISTS = Literal['add', 'skip', 'rename', 'overwrite', 'a', 's', 'r', 'o']


class Setter(object):
    _owner: DrissionGet = ...

    def __init__(self, owner: DrissionGet):
        """
        :param owner: DrissionGet对象
        """
        ...

    @property
    def if_file_exists(self) -> FileExists:
        """返回用于设置文件同名策略的对象"""
        ...

    def log(self, path: Union[str, Path, None] = 'DrissionGet_log.csv', failed_only: bool = False) -> Setter:
        """设置下载完成日志
        :param path: 日志文件路径，为None或False不记录
        :param failed_only: 是否只记录失败任务
        :return: Setter对象自己
        """
        ...

    def show_msg(self, on_off: bool = True, failed_only: bool = False) -> Setter:
        """设置是否打印下载信息
        :param on_off: bool代表开关
        :param failed_only: 是否只显示失败任务，此设置仅多线程任务时生效
        :return: Setter对象自己
        """
        ...

    def driver(self, driver: Union[Session, BasePage, SessionOptions]) -> Setter:
        """设置驱动
        :param driver: Session对象或DrissionPage的页面对象
        :return: Setter对象自己
        """
        ...

    def roads(self, num: int) -> Setter:
        """设置可同时运行的线程数
        :param num: 线程数量
        :return: Setter对象自己
        """
        ...

    def retry(self, times: int) -> Setter:
        """设置连接失败时重试次数
        :param times: 重试次数
        :return: Setter对象自己
        """
        ...

    def interval(self, seconds: float) -> Setter:
        """设置连接失败时重试间隔
        :param seconds: 连接失败时重试间隔（秒）
        :return: Setter对象自己
        """
        ...

    def timeout(self, seconds: float) -> Setter:
        """设置连接超时时间
        :param seconds: 超时时间（秒）
        :return: Setter对象自己
        """
        ...

    def save_path(self, path: Union[str, Path]) -> Setter:
        """设置文件保存路径
        :param path: 文件路径，可以是str或Path
        :return: Setter对象自己
        """
        ...

    def split(self, on_off: bool = True) -> Setter:
        """设置大文件是否分块下载
        :param on_off: bool代表开关
        :return: Setter对象自己
        """
        ...

    def block_size(self, size: Union[str, int]) -> Setter:
        """设置分块大小
        :param size: 单位为字节，可用'K'、'M'、'G'为单位，如'50M'
        :return: Setter对象自己
        """
        ...

    def proxies(self, http: str = None, https: str = None) -> Setter:
        """设置代理地址及端口，例：'127.0.0.1:1080'
        :param http: http代理地址及端口
        :param https: https代理地址及端口
        :return: Setter对象自己
        """
        ...

    def encoding(self, encoding: Optional[str]) -> Setter:
        """设置编码
        :param encoding: 编码名称，传入None取消之前的设置
        :return: Setter对象自己
        """
        ...


class FileExists(object):
    """用于设置存在同名文件时处理方法"""
    _setter: Setter = ...

    def __init__(self, setter: Setter):
        """
        :param setter: Setter对象
        """
        ...

    def __call__(self, mode: FILE_EXISTS) -> Setter:
        """设置文件存在时的处理方式
        :param mode: 'skip', 'rename', 'overwrite', 'add', 's', 'r', 'o', 'a'
        :return: Setter 对象
        """
        ...

    def skip(self) -> Setter:
        """设为跳过"""
        ...

    def rename(self) -> Setter:
        """设为重命名，文件名后加序号"""
        ...

    def overwrite(self) -> Setter:
        """设为覆盖"""
        ...

    def add(self) -> Setter:
        """设为追加"""
        ...
