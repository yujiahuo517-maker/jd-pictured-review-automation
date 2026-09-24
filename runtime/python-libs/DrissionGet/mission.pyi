# -*- coding:utf-8 -*-
from pathlib import Path
from typing import Union, List, Optional

from DrissionRecord import Recorder
from requests import Session
from requests.structures import CaseInsensitiveDict

from .drissionGet import DrissionGet


class MissionData(object):
    """保存任务数据的对象"""
    url: str = ...
    save_path: Path = ...
    rename: Optional[str] = ...
    suffix: Optional[str] = ...
    file_exists: str = ...
    split: bool = ...
    kwargs: dict = ...
    offset: int = ...

    def __init__(self, url: str, save_path: Path, rename: Optional[str], suffix: Optional[str],
                 file_exists: str, split: bool, kwargs: dict, offset: int = 0):
        """保存任务数据的对象
        :param url: 下载文件url
        :param save_path: 保存文件夹
        :param rename: 文件重命名
        :param suffix: 文件重命名后缀名
        :param file_exists: 存在重名文件时处理方式
        :param split: 是否允许分块下载
        :param kwargs: requests其它参数
        :param offset: 文件存储偏移量
        """
        ...


class BaseTask(object):
    """任务类基类"""
    _DONE: str = ...
    RESULT_TEXTS: dict = ...

    _id: str = ...
    state: str = ...
    result: Union[str, False, None] = ...
    info: str = ...

    def __init__(self, ID: Union[int, str]):
        """任务类基类
        :param ID: 任务id
        """
        ...

    @property
    def id(self) -> Union[int, str]:
        """返回任务或子任务id"""
        ...

    @property
    def data(self):
        """返回任务数据"""
        ...

    @property
    def is_done(self) -> bool:
        """返回任务是否结束"""
        ...

    def set_states(self,
                   result: Union[bool, str, None] = None,
                   info: str = None,
                   state: str = 'done') -> None:
        """设置任务结果值
        :param result: 结果：'success', 'skipped', 'canceled', False, None
        :param info: 任务信息
        :param state: 任务状态：'waiting', 'running', 'done'
        :return: None
        """
        ...


class Mission(BaseTask):
    """任务类"""
    file_name: Optional[str] = ...
    _data: MissionData = ...
    _path: Optional[Path] = ...
    _recorder: Optional[Recorder] = ...
    size: Optional[float] = ...
    done_tasks_count: int = ...
    tasks_count: int = ...
    tasks: List[Task] = ...
    owner: DrissionGet = ...
    session: Session = ...
    headers: CaseInsensitiveDict = ...
    method: str = ...
    encoding: Optional[str] = ...
    _overwrote: bool = ...

    def __init__(self, ID: int, owner: DrissionGet, file_url: str, save_path: Path, rename: str,
                 suffix: str, file_exists: str, split: bool, encoding: Optional[str], kwargs: dict):
        """任务类
        :param ID: 任务id
        :param owner: 所属DrissionGet对象
        :param file_url: 文件网址
        :param save_path: 保存文件夹路径
        :param rename: 重命名
        :param suffix: 重命名后缀名
        :param file_exists: 存在同名文件处理方式
        :param split: 是否分块下载
        :param encoding: 编码格式
        :param kwargs: 连接参数
        """
        ...

    def __repr__(self) -> str:
        ...

    @property
    def data(self) -> MissionData:
        """返回任务数据"""
        ...

    @property
    def path(self) -> Union[str, Path]:
        """返回文件保存路径"""
        ...

    @property
    def recorder(self) -> Recorder:
        """返回记录器对象"""
        ...

    @property
    def rate(self) -> Optional[float]:
        """返回下载进度百分比"""
        ...

    def cancel(self) -> None:
        """取消该任务，停止未下载完的task"""
        ...

    def del_file(self) -> None:
        """删除下载的文件"""
        ...

    def wait(self, show: bool = True, timeout: float = 0) -> tuple:
        """等待当前任务完成
        :param show: 是否显示下载进度
        :param timeout: 超时时间
        :return: 任务结果和信息组成的tuple
        """
        ...

    def _set_session(self) -> Session:
        """复制Session对象，并设置cookies"""
        ...

    def _handle_kwargs(self, url: str, kwargs: dict) -> dict:
        """处理接收到的参数
        :param url: 要访问的url
        :param kwargs: 传入的参数dict
        :return: 处理后的参数dict
        """
        ...

    def _set_path(self, path: Path) -> None:
        """设置文件保存路径"""
        ...

    def _set_done(self, result: Union[bool, str, None], info: str) -> None:
        """设置一个任务为done状态
        :param result: 结果：'success'、'skipped'、'canceled'、False、None
        :param info: 任务信息
        :return: None
        """
        ...

    def _a_task_done(self, is_success: bool, info: str) -> None:
        """当一个task完成时调用
        :param is_success: 该task是否成功
        :param info: 该task传入的信息
        :return: None
        """
        ...

    def _break_mission(self, result: Union[bool, str, None], info: str) -> None:
        """中止该任务，停止未下载完的task
        :param result: 结果：'success'、'skipped'、'canceled'、False、None
        :param info: 任务信息
        :return: None
        """
        ...


class Task(BaseTask):
    """子任务类"""
    mission: Mission = ...
    range: Optional[list] = ...
    size: Optional[int] = ...
    _downloaded_size: int = 0

    def __init__(self, mission: Mission, range_: Optional[list], ID: str, size: Optional[int]):
        """子任务类
        :param mission: 父任务对象
        :param range_: 读取文件数据范围
        :param ID: 任务id
        """
        ...

    def __repr__(self) -> str:
        ...

    @property
    def mid(self) -> int:
        """返回父任务id"""
        ...

    @property
    def data(self) -> MissionData:
        """返回任务数据对象"""
        ...

    @property
    def path(self) -> str:
        """返回文件保存路径"""
        ...

    @property
    def file_name(self) -> str:
        """返回文件名"""
        ...

    @property
    def rate(self) -> Optional[float]:
        """返回下载进度百分比"""
        ...

    def add_data(self, data: bytes, seek: int = None) -> None:
        """把数据输入到记录器
        :param data: 文件字节数据
        :param seek: 在文件中的位置，None表示最后
        :return: None
        """
        ...

    def clear_cache(self) -> None:
        """清除以接收但未写入硬盘的缓存"""
        ...

    def _set_done(self, result: Union[bool, str, None], info: str) -> None:
        """设置一个子任务为done状态
        :param result: 结果：'success'、'skipped'、'canceled'、False、None
        :param info: 任务信息
        :return: None
        """
        ...


def format_headers(headers: CaseInsensitiveDict): ...
