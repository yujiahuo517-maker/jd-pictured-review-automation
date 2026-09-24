# -*- coding:utf-8 -*-
from DrissionRecord import Recorder
from requests import Session

from ._funcs import get_file_exists_mode, get_save_path


class Setter(object):
    def __init__(self, owner):
        self._owner = owner

    @property
    def if_file_exists(self):
        return FileExists(self)

    def log(self, path='DrissionGet_log.csv', failed_only=False):
        if not path:
            self._owner._logger = self._owner._log_mode = None
            return self
        if self._owner._logger:
            self._owner._logger.set.path(path)
        else:
            self._owner._logger = Recorder(path, cache_size=1)
        self._owner._log_mode = 'failed' if failed_only else 'all'
        return self

    def show_msg(self, on_off=True, failed_only=False):
        self._owner._print_mode = 'failed' if failed_only else 'all' if on_off else None
        return self

    def driver(self, driver):
        if driver is None:
            self._owner._session = Session()
            return self

        elif isinstance(driver, Session):
            self._owner._session = driver
            return self

        _type = str(type(driver))
        if _type.startswith('<SessionOptions '):
            self._owner._session, headers = driver.make_session()
            self._owner._session.headers = headers
            return self

        else:
            self._owner._session = driver.session if hasattr(driver, 'session') else Session()
            self._owner.page = driver
        return self

    def roads(self, num):
        if self._owner.is_running:
            print('有任务未完成时不能改变roads。')
            return self
        if num != self._owner.roads:
            self._owner._roads = num
            self._owner._threads = {i: None for i in range(num)}
        return self

    def retry(self, times):
        if not isinstance(times, int) or times < 0:
            raise TypeError('times参数只能接受int格式且不能小于0。')
        self._owner._retry = times
        return self

    def interval(self, seconds):
        if not isinstance(seconds, (int, float)) or seconds < 0:
            raise TypeError('seconds参数只能接受int或float格式且不能小于0。')
        self._owner._interval = seconds
        return self

    def timeout(self, seconds):
        if not isinstance(seconds, (int, float)) or seconds < 0:
            raise TypeError('seconds参数只能接受int或float格式且不能小于0。')
        self._owner._timeout = seconds
        return self

    def save_path(self, path):
        self._owner.save_path = get_save_path(path)
        return self

    def split(self, on_off=True):
        self._owner.split = on_off
        return self

    def block_size(self, size):
        self._owner.block_size = size
        return self

    def proxies(self, http=None, https=None):
        self._owner._session.proxies = {'http': http, 'https': https}
        return self

    def encoding(self, encoding):
        self._owner._encoding = encoding if encoding else None
        return self


class FileExists(object):

    def __init__(self, setter):
        self._setter = setter

    def __call__(self, mode):
        self._setter._owner.file_exists = get_file_exists_mode(mode)
        return self._setter

    def skip(self):
        self._setter._owner.file_exists = 'skip'
        return self._setter

    def rename(self):
        self._setter._owner.file_exists = 'rename'
        return self._setter

    def overwrite(self):
        self._setter._owner.file_exists = 'overwrite'
        return self._setter

    def add(self):
        self._setter._owner.file_exists = 'add'
        return self._setter
