# -*- coding:utf-8 -*-
from pathlib import Path
from queue import Queue
from threading import Lock
from typing import Union, Tuple, Any, Literal, Optional

from DrissionPage import SessionOptions
from DrissionPage._base.base import BasePage
from DrissionPage._units.downloader import DownloadMission
from DrissionRecord import Recorder
from requests import Session, Response
from requests.structures import CaseInsensitiveDict

from ._funcs import FileExistsSetter, PathSetter, BlockSizeSetter
from .mission import Task, Mission, BaseTask
from .setter import Setter

FILE_EXISTS = Literal['add', 'skip', 'rename', 'overwrite', 'a', 's', 'r', 'o']


class DrissionGet(object):
    file_exists: FileExistsSetter = ...
    save_path: PathSetter = ...
    block_size: BlockSizeSetter = ...

    _roads: int = ...
    _setter: Optional[Setter] = ...
    _print_mode: Optional[str] = ...
    _log_mode: Optional[str] = ...
    _logger: Optional[Recorder] = ...
    _retry: Optional[int] = ...
    _interval: Optional[float] = ...
    page: Optional[BasePage] = ...
    _waiting_list: Queue = ...
    _session: Session = ...
    _running_count: int = ...
    _missions_num: int = ...
    _missions: dict = ...
    _threads: dict = ...
    _timeout: Union[int, float, None] = ...
    _stop_printing: bool = ...
    _lock: Lock = ...
    split: bool = ...
    _encoding: Optional[str] = ...

    def __init__(self,
                 save_path: Union[str, Path] = None,
                 roads: int = 10,
                 driver: Union[Session, BasePage, SessionOptions] = None,
                 file_exists: FILE_EXISTS = 'rename'):
        """
        :param save_path: 文件保存路径
        :param roads: 可同时运行的线程数
        :param driver: 使用的Session对象，或配置对象、页面对象等
        :param file_exists: 有同名文件名时的处理方式，可选 'skip', 'overwrite', 'rename', 'add', 's', 'o', 'r', 'a'
        """
        ...

    def __call__(self,
                 url: str,
                 save_path: Union[str, Path, None] = None,
                 rename: str = None,
                 suffix: Optional[str] = None,
                 file_exists: FILE_EXISTS = None,
                 show_msg: bool = True,
                 split: bool = False,
                 timeout: Optional[float] = None,
                 params: Optional[dict] = ...,
                 data: Any = ...,
                 json: Any = ...,
                 headers: Optional[dict] = ...,
                 cookies: Any = ...,
                 files: Any = ...,
                 auth: Any = ...,
                 allow_redirects: bool = ...,
                 proxies: Optional[dict] = ...,
                 hooks: Any = ...,
                 stream: Any = ...,
                 verify: Any = ...,
                 cert: Any = ...) -> tuple:
        """以阻塞的方式下载一个文件并返回结果
        :param url: 文件网址
        :param save_path: 保存路径
        :param rename: 重命名的文件名
        :param suffix: 指定后缀名
        :param file_exists: 遇到同名文件时的处理方式，可选 'skip', 'overwrite', 'rename', 'add', 's', 'o', 'r', 'a'，默认跟随实例属性
        :param split: 是否分块下载，为None使用设置
        :param show_msg: 是否打印进度
        :param kwargs: 连接参数
        :return: 任务结果和信息组成的tuple
        """
        ...

    @property
    def set(self) -> Setter:
        """用于设置打印和记录模式的对象"""
        ...

    @property
    def roads(self) -> int:
        """可同时运行的线程数"""
        ...

    @property
    def retry(self) -> int:
        """返回连接失败时重试次数"""
        ...

    @property
    def interval(self) -> float:
        """返回连接失败时重试间隔"""
        ...

    @property
    def timeout(self) -> float:
        """返回连接超时时间"""
        ...

    @property
    def waiting_list(self) -> Queue:
        """返回等待队列"""
        ...

    @property
    def session(self) -> Session:
        """返回用于保存默认连接设置的Session对象"""
        ...

    @property
    def is_running(self) -> bool:
        """返回是否有线程还在运行"""
        ...

    @property
    def missions(self) -> dict:
        """用list方式返回所有任务对象"""
        ...

    @property
    def encoding(self) -> Optional[str]:
        ...

    def add(self,
            url: str,
            save_path: Union[str, Path, None] = None,
            rename: str = None,
            suffix: str = None,
            file_exists: FILE_EXISTS = None,
            split: bool = None,
            timeout: Optional[float] = None,
            params: Optional[dict] = ...,
            data: Any = None,
            json: Union[dict, str, None] = ...,
            headers: Optional[dict] = ...,
            cookies: Any = ...,
            files: Any = ...,
            auth: Any = ...,
            allow_redirects: bool = ...,
            proxies: Optional[dict] = ...,
            hooks: Any = ...,
            stream: Any = ...,
            verify: Any = ...,
            cert: Any = ...) -> Mission:
        """添加一个下载任务并将其返回
        :param url: 文件网址
        :param save_path: 保存路径
        :param rename: 重命名的文件名
        :param suffix: 重命名的文件后缀名
        :param file_exists: 遇到同名文件时的处理方式，可选 'skip', 'overwrite', 'rename', 'add', 's', 'o', 'r', 'a'，默认跟随实例属性
        :param split: 是否允许多线程分块下载，为None则使用对象属性
        :param kwargs: 连接参数
        :return: 任务对象
        """
        ...

    def download(self,
                 url: str,
                 save_path: Union[str, Path, None] = None,
                 rename: str = None,
                 suffix: str = None,
                 file_exists: FILE_EXISTS = None,
                 split: bool = False,
                 show_msg: bool = None,
                 timeout: Optional[float] = None,
                 params: Optional[dict] = ...,
                 data: Any = ...,
                 json: Any = ...,
                 headers: Optional[dict] = ...,
                 cookies: Any = ...,
                 files: Any = ...,
                 auth: Any = ...,
                 allow_redirects: bool = ...,
                 proxies: Optional[dict] = ...,
                 hooks: Any = ...,
                 stream: Any = ...,
                 verify: Any = ...,
                 cert: Any = ...) -> tuple:
        """以阻塞的方式下载一个文件并返回结果
        :param url: 文件网址
        :param save_path: 保存路径
        :param rename: 重命名的文件名
        :param suffix: 重命名的文件后缀名
        :param file_exists: 遇到同名文件时的处理方式，可选 'skip', 'overwrite', 'rename', 'add', 's', 'o', 'r', 'a'，默认跟随实例属性
        :param split: 是否分块下载，为None使用设置
        :param show_msg: 是否打印进度，为None使用设置
        :param kwargs: 连接参数
        :return: 任务结果和信息组成的tuple
        """
        ...

    def by_browser(self,
                   url: str,
                   save_path: Union[str, Path] = None,
                   rename: str = None, suffix: str = None,
                   timeout: float = None,
                   file_exists: Literal['rename', 'overwrite', 'skip', 'r', 'o', 's'] = None) -> Union[
        DownloadMission, False]:
        """触发浏览器下载
        :param url: 下载地址
        :param save_path: 保存路径，为None保存在原来设置的，如未设置保存到当前路径
        :param rename: 重命名文件名
        :param suffix: 指定文件后缀
        :param timeout: 等待下载触发的超时时间，为None则使用页面对象设置
        :param file_exists: 可在 'rename', 'overwrite', 'skip', 'r', 'o', 's'中选择
        :return: DownloadMission对象，任务无触发时返回False
        """
        ...

    def _run_or_wait(self, mission: BaseTask) -> None:
        """接收任务，有空线程则运行，没有则进入等待队列
        :param mission: 任务对象
        :return: None
        """
        ...

    def _run(self, ID: int, mission: BaseTask) -> None:
        """线程函数
        :param ID: 线程id
        :param mission: 任务对象，Mission或Task
        :return: None
        """
        ...

    def get_mission(self, mission_or_id: Union[int, Mission]) -> Mission:
        """根据id值获取一个任务
        :param mission_or_id: 任务或任务id
        :return: 任务对象
        """
        ...

    def get_failed_missions(self) -> list:
        """返回失败任务列表"""
        ...

    def wait(self,
             mission: Union[int, Mission] = None,
             show: bool = False,
             timeout: float = None) -> Optional[tuple]:
        """等待所有或指定任务完成
        :param mission: 任务对象或任务id，为None时等待所有任务结束
        :param show: 是否显示进度
        :param timeout: 超时时间，None或0为无限
        :return: 任务结果和信息组成的tuple
        """
        ...

    def cancel(self) -> None:
        """取消所有等待中或执行中的任务"""
        ...

    def show(self, asyn: bool = True, keep: bool = False) -> None:
        """实时显示所有线程进度
        :param asyn: 是否以异步方式显示
        :param keep: 任务列表为空时是否保持显示
        :return: None
        """
        ...

    def _show(self, wait: float, keep: bool = False) -> None:
        """实时显示所有线程进度
        :param wait: 超时时间（秒）
        :param keep: 任务列表为空时是否保持显示
        :return: None
        """
        ...

    def _connect(self, url: str, session: Session, _headers: CaseInsensitiveDict,
                 method: str, encoding: Optional[str], **kwargs) -> Tuple[Union[Response, None], str]:
        """生成response对象
        :param url: 目标url
        :param session: 用于连接的Session对象
        :param _headers: 内置的headers参数
        :param method: 请求方式
        :param encoding: 编码格式
        :param kwargs: 连接参数
        :return: tuple，第一位为Response或None，第二位为出错信息或'Success'
        """
        ...

    def _get_usable_thread(self) -> Optional[int]:
        """获取可用线程，没有则返回None"""
        ...

    def _stop_show(self) -> None:
        """设置停止打印的变量"""
        ...

    def _when_mission_done(self, mission: Mission) -> None:
        """当任务完成时执行的操作
        :param mission: 完结的任务
        :return: None
        """
        ...

    def _download(self,
                  mission_or_task: Union[Mission, Task],
                  thread_id: int) -> None:
        """此方法是执行下载的线程方法，用于根据任务下载文件
        :param mission_or_task: 下载任务对象
        :param thread_id: 线程号
        :return: None
        """
        ...


def handle_mission(mission: Mission, url: str) -> tuple:
    """对任务进行预处理，创建连接、获取信息
    :param mission: 下载任务对象
    :param url: 文件url
    :return: (是否继续, Response对象, 文件大小)
    """
    ...


def do_download_(r, task: Task, show, size) -> None:
    """执行download()触发的下载任务
    :param r: Response对象
    :param task: 任务对象
    :param show: 是否打印进度
    :param size: 文件大小
    :return: None
    """
    ...


def do_download(r: Response, task: Task, first: bool = False) -> None:
    """执行下载任务
    :param r: Response对象
    :param task: 任务对象
    :param first: 是否第一个分块
    :return: None
    """
    ...
