# -*- coding:utf-8 -*-
from time import sleep, perf_counter
from urllib.parse import quote, urlparse

from DrissionRecord import Recorder
from requests.structures import CaseInsensitiveDict

from ._funcs import copy_session, set_session_cookies


class MissionData(object):
    def __init__(self, url, save_path, rename, suffix, file_exists, split, kwargs, offset=0):
        self.url = quote(url, safe='-_.~!*\'"();:@&=+$,/\\?#[]%')
        self.save_path = save_path
        self.rename = rename
        self.suffix = suffix
        self.file_exists = file_exists
        self.split = split
        self.kwargs = kwargs
        self.offset = offset


class BaseTask(object):
    _DONE = 'done'
    RESULT_TEXTS = {'success': '成功', 'skipped': '跳过', 'canceled': '取消', False: '失败', None: '未知'}

    def __init__(self, ID):
        self._id = ID
        self.state = 'waiting'  # 'waiting', 'running', 'done'
        self.result = None  # 'success', 'skipped', 'canceled', False, None
        self.info = '等待下载'  # 信息

    @property
    def id(self):
        return self._id

    @property
    def data(self):
        return

    @property
    def is_done(self):
        return self.state in ('done', 'cancel')

    def set_states(self, result=None, info=None, state='done'):
        self.result = result
        self.info = info
        self.state = state


class Mission(BaseTask):
    def __init__(self, ID, owner, file_url, save_path, rename, suffix, file_exists, split, encoding, kwargs):
        super().__init__(ID)
        self.owner = owner
        self.size = None

        self.tasks = []
        self.tasks_count = 1
        self.done_tasks_count = 0

        self.file_name = None
        self._path = None  # 文件完整路径，Path对象
        self._recorder = None
        self.encoding = encoding

        self._set_session()
        kwargs = self._handle_kwargs(file_url, kwargs)
        self._data = MissionData(file_url, save_path, rename, suffix, file_exists, split, kwargs)
        self._overwrote = False
        self.method = 'post' if (self._data.kwargs.get('data', None) is not None or
                                 self._data.kwargs.get('json', None) is not None) else 'get'

    def __repr__(self):
        return f'<Mission {self.id} {self.info} {self.file_name}>'

    @property
    def data(self):
        return self._data

    @property
    def path(self):
        return self._path

    @property
    def recorder(self):
        if self._recorder is None:
            self._recorder = Recorder(cache_size=100)
            self._recorder.set.show_msg(False)
        return self._recorder

    @property
    def rate(self):
        if not self.size:
            return None
        c = 0
        for t in self.tasks:
            c += t._downloaded_size if t._downloaded_size else 0
        return round((c / self.size) * 100, 2)

    def cancel(self) -> None:
        self._break_mission('canceled', '已取消')

    def del_file(self):
        if self.path and self.path.exists():
            try:
                self.path.unlink()
            except Exception:
                pass

    def wait(self, show=True, timeout=0):
        if show:
            print(f'url：{self.data.url}')
            t2 = perf_counter()
            while self.file_name is None and perf_counter() - t2 < 4:
                sleep(0.01)
            print(f'文件名：{self.file_name}')
            print(f'目标路径：{self.path}')
            if not self.size:
                print('未知大小 ', end='')

            t1 = perf_counter()
            while not self.is_done and (perf_counter() - t1 < timeout or timeout == 0):
                try:
                    rate = round((self.path.stat().st_size / self.size) * 100, 2)
                    print(f'\r{rate}% ', end='')
                except (FileNotFoundError, TypeError, ZeroDivisionError):
                    pass
                sleep(0.1)

            if self.result is False:
                print(f'下载失败 {self.info}')
            elif self.result == 'success':
                print('\r100% ', end='')
                print(f'{"已覆盖" if self._overwrote else "下载完成"} {self.info}')
            elif self.result == 'skipped':
                print(f'已跳过 {self.info}')
            print()

        else:
            t1 = perf_counter()
            while not self.is_done and (perf_counter() - t1 < timeout or timeout == 0):
                sleep(0.1)

        return self.result, self.info

    def _set_session(self):
        session = copy_session(self.owner.session)
        headers = session.headers
        session.headers = None
        if self.owner.page:
            set_session_cookies(session, self.owner.page.cookies())
            if hasattr(self.owner.page, '_headers'):
                headers = CaseInsensitiveDict({**self.owner.page._headers, **headers})
            headers.update({"User-Agent": self.owner.page.user_agent})
        self.session = session
        self.headers = headers

    def _handle_kwargs(self, url, kwargs):
        if 'timeout' not in kwargs:
            kwargs['timeout'] = self.owner.timeout

        headers = CaseInsensitiveDict(kwargs['headers']) if 'headers' in kwargs else CaseInsensitiveDict()

        parsed_url = urlparse(url)
        hostname = parsed_url.hostname
        scheme = parsed_url.scheme

        if not ('Referer' in headers or 'Referer' in self.headers):
            headers['Referer'] = self.owner.page.url if self.owner.page and self.owner.page.url \
                else f'{scheme}://{hostname}'
        if not ('Host' in headers or 'Host' in self.headers):
            headers['Host'] = hostname
        kwargs['headers'] = format_headers(headers)
        if not kwargs['headers']['Host']:
            kwargs['headers'].pop('Host')
        if not kwargs['headers']['Referer']:
            kwargs['headers'].pop('Referer')

        return kwargs

    def _set_path(self, path):
        self.file_name = path.name
        self._path = path
        self.recorder.set.path(path, file_type='b')

    def _set_done(self, result, info):
        if result == 'skipped':
            self.set_states(result=result, info=info, state=self._DONE)

        elif result == 'canceled' or result is False:
            self.recorder.clear()
            self.set_states(result=result, info=info, state=self._DONE)

        elif result == 'success':
            self.recorder.record()
            if self.size and self.path.stat().st_size < self.size:
                self.del_file()
                self.set_states(False, '下载失败', self._DONE)
            else:
                self.set_states('success', info, self._DONE)

        self.owner._when_mission_done(self)

    def _a_task_done(self, is_success, info):
        if self.is_done:
            return

        if is_success is False:
            self._break_mission(False, info)
            return

        self.done_tasks_count += 1
        if self.done_tasks_count == self.tasks_count:
            self._set_done('success', info)

    def _break_mission(self, result, info):
        if self.is_done:
            return

        for task in self.tasks:
            if not task.is_done:
                task.set_states(result=result, info=info, state='cancel')

        while any((not i.is_done for i in self.tasks)):
            sleep(.3)

        self._set_done(result, info)
        self.del_file()


class Task(BaseTask):
    def __init__(self, mission, range_, ID, size):
        super().__init__(ID)
        self.mission = mission
        self.range = range_
        self.size = size
        self._downloaded_size = 0

    def __repr__(self):
        return f'<Task M{self.mid} T{self._id} {self.rate}% {self.info} {self.file_name}>'

    @property
    def mid(self):
        return self.mission.id

    @property
    def data(self):
        return self.mission.data

    @property
    def path(self):
        return self.mission.path

    @property
    def file_name(self):
        return self.mission.file_name

    @property
    def rate(self):
        return round((self._downloaded_size / self.size) * 100, 2) if self.size else None

    def add_data(self, data, seek=None):
        self._downloaded_size += len(data)
        self.mission.recorder.add_data(data, coord=seek)

    def clear_cache(self):
        self.mission.recorder.clear()

    def _set_done(self, result, info):
        self.set_states(result=result, info=info, state=self._DONE)
        self.mission._a_task_done(result, info)


def format_headers(headers):
    for k, v in headers.items():
        if k in (':method', ':scheme', ':authority', ':path'):
            headers.pop(k)
        elif v not in (None, False, True):
            headers[k] = str(v)
    return headers
