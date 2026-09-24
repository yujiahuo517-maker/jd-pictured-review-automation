# -*- coding:utf-8 -*-
from pathlib import Path
from threading import Lock
from typing import Union, Optional

from requests import Session, Response

FILE_EXISTS_MODE: dict = ...


def copy_session(session: Session) -> Session:
    """复制输入Session对象，返回一个新的
    :param session: 被复制的Session对象
    :return: 新Session对象
    """
    ...


class BlockSizeSetter(object):
    def __set__(self, block_size, val: Union[str, int]): ...

    def __get__(self, block_size, objtype=None) -> int: ...


class PathSetter(object):
    def __set__(self, save_path, val: Union[str, Path]): ...

    def __get__(self, save_path, objtype=None): ...


class FileExistsSetter(object):
    def __set__(self, file_exists, mode: str): ...

    def __get__(self, file_exists, objtype=None): ...


def get_save_path(path: Union[str, Path]) -> Path:
    """获取绝对路径，去除Windows系统非法字符
    :param path: 输入的路径
    :return: Path格式路径
    """
    ...


def get_file_exists_mode(mode: str) -> str:
    """获取文件重名时处理策略名称
    :param mode: 输入
    :return: 标准字符串
    """
    ...


def set_charset(response: Response, encoding: Optional[str]) -> Response:
    """设置Response对象的编码
    :param response: Response对象
    :param encoding: 指定的编码格式
    :return: 设置编码后的Response对象
    """
    ...


def get_file_info(response: Response, save_path: Path, rename: str = None, suffix: str = None,
                  file_exists: str = None, encoding: Optional[str] = None, lock: Lock = None) -> dict:
    """获取文件信息，大小单位为byte
    包括：size、path、skip
    :param response: Response对象
    :param save_path: 目标文件夹Path对象
    :param rename: 重命名
    :param suffix: 重命名后缀名
    :param file_exists: 存在重名文件时的处理方式
    :param encoding: 编码格式
    :param lock: 线程锁
    :return: 文件大小、完整路径、是否跳过、是否覆盖
    """
    ...


def get_file_name(response: Response, encoding: str) -> str:
    """从headers或url中获取文件名，如果获取不到，生成一个随机文件名
    :param response: 返回的response
    :param encoding: 在headers获取时指定编码格式
    :return: 下载文件的文件名
    """
    ...


def set_session_cookies(session: Session, cookies: list) -> None:
    """设置Session对象的cookies
    :param session: Session对象
    :param cookies: cookies信息
    :return: None
    """
    ...
