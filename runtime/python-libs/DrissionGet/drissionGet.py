# -*- coding:utf-8 -*-
from copy import copy
from pathlib import Path
from queue import Queue
from threading import Thread, Lock
from time import sleep, perf_counter

from requests.structures import CaseInsensitiveDict

from ._funcs import (FileExistsSetter, PathSetter, BlockSizeSetter, set_charset, get_file_info, get_file_exists_mode,
                     get_save_path)
from .mission import Task, Mission
from .setter import Setter


class DrissionGet(object):
    file_exists = FileExistsSetter()
    save_path = PathSetter()
    block_size = BlockSizeSetter()

    def __init__(self, save_path='.', roads=10, driver=None, file_exists='rename'):
        self._roads = roads
        self._missions = {}
        self._threads = {i: None for i in range(self._roads)}
        self._waiting_list = Queue()
        self._missions_num = 0
        self._running_count = 0  # 正在运行的任务数
        self._stop_printing = False  # 用于控制显示线程停止
        self._lock = Lock()
        self.page = None
        self._retry = None
        self._interval = None
        self._timeout = None
        self._encoding = None

        self._setter = None
        self._print_mode = 'all'
        self._log_mode = None
        self._logger = None

        self.save_path = save_path
        self.file_exists = file_exists
        self.split = True
        self.block_size = '50M'
        self.set.driver(driver)

    def __call__(self, url=None, save_path=None, rename=None, suffix=None,
                 file_exists=None, split=False, show_msg=True, file_url=None, **kwargs):
        return self.download(url=url, save_path=save_path, rename=rename, suffix=suffix,
                             file_exists=file_exists, show_msg=show_msg, **kwargs)

    @property
    def set(self):
        if self._setter is None:
            self._setter = Setter(self)
        return self._setter

    @property
    def roads(self):
        return self._roads

    @property
    def retry(self):
        if self._retry is not None:
            return self._retry
        elif self.page is not None:
            return self.page.retry_times
        else:
            return 3

    @property
    def interval(self):
        if self._interval is not None:
            return self._interval
        elif self.page is not None:
            return self.page.retry_interval
        else:
            return 5

    @property
    def timeout(self):
        if self._timeout is not None:
            return self._timeout
        elif self.page is not None:
            return self.page.timeout
        else:
            return 20

    @property
    def waiting_list(self):
        return self._waiting_list

    @property
    def session(self):
        return self._session

    @property
    def is_running(self):
        return self._running_count > 0

    @property
    def missions(self):
        return self._missions

    @property
    def encoding(self):
        """返回指定的编码格式"""
        return self._encoding

    def add(self, url=None, save_path=None, rename=None, suffix=None, file_exists=None, split=None,
            file_url=None, **kwargs):
        if not url and file_url:
            url = file_url
        self._missions_num += 1
        self._running_count += 1
        if file_exists is not None:
            file_exists = get_file_exists_mode(file_exists)
        mission = Mission(self._missions_num, self, url, get_save_path(save_path) if save_path else self.save_path,
                          rename, suffix, file_exists or self.file_exists,
                          self.split if split is None else split, self._encoding, kwargs)
        self._missions[self._missions_num] = mission

        self._run_or_wait(mission)
        return mission

    def download(self, url=None, save_path=None, rename=None, suffix=None, file_exists=None, split=False,
                 show_msg=None, file_url=None, **kwargs):
        if not url and file_url:
            url = file_url
        self._missions_num += 1
        self._running_count += 1
        if file_exists is not None:
            file_exists = get_file_exists_mode(file_exists)
        mission = Mission(self._missions_num, self, url, get_save_path(save_path) if save_path else self.save_path,
                          rename, suffix, file_exists or self.file_exists,
                          self.split if split is None else split, self._encoding, kwargs)
        self._missions[self._missions_num] = mission

        tmp, self._print_mode = self._print_mode, None
        if show_msg is None:
            show_msg = True if tmp else False

        if split:
            self._run_or_wait(mission)
            r = mission.wait(show=show_msg)
            self._print_mode = tmp
            return r

        ok, r, file_size = handle_mission(mission, url)
        if show_msg:
            print(f'url：{url}')
            print(f'文件名：{mission.file_name}')
            print(f'目标路径：{mission.path}')
            if not mission.size:
                print('下载中 ', end='')

        if ok:
            task1 = Task(mission, None, '1/1', file_size)
            mission.tasks.append(task1)
            do_download_(r, task1, show_msg, file_size)

        if show_msg:
            if mission.result is False:
                print(f'下载失败 {mission.info}')
            elif mission.result == 'success':
                print('\r100% ', end='')
                print(f'{"已覆盖" if mission._overwrote else "下载完成"} {mission.info}')
            elif mission.result == 'skipped':
                print(f'已跳过 {mission.info}')
            print()

        self._print_mode = tmp
        return mission.result, mission.info

    def by_browser(self, url, save_path=None, rename=None, suffix=None, timeout=None, file_exists=None):
        if hasattr(self.page, '_download_by_browser'):
            return self.page._download_by_browser(url=url, save_path=save_path, rename=rename, suffix=suffix,
                                                  timeout=timeout, file_exists=file_exists)
        else:
            raise RuntimeError('非浏览器调用无法使用浏览器下载。')

    def _run_or_wait(self, mission):
        thread_id = self._get_usable_thread()
        if thread_id is None:
            self._waiting_list.put(mission)
        else:
            thread = Thread(target=self._run, args=(thread_id, mission), daemon=False)
            self._threads[thread_id] = {'thread': thread, 'mission': None}
            thread.start()

    def _run(self, ID, mission):
        while True:
            if not mission:  # 如果没有任务，就从等候列表中取一个
                if self._waiting_list.empty():
                    break
                else:
                    try:
                        mission = self._waiting_list.get(True, .5)
                    except Exception:
                        self._waiting_list.task_done()
                        break

            self._threads[ID]['mission'] = mission
            self._download(mission, ID)
            mission = None

        self._threads[ID] = None

    def get_mission(self, mission_or_id):
        return self._missions[mission_or_id] if isinstance(mission_or_id, int) else mission_or_id

    def get_failed_missions(self):
        return [i for i in self._missions.values() if i.result is False]

    def wait(self, mission=None, show=False, timeout=None):
        timeout = 0 if timeout is None else timeout
        if mission:
            return self.get_mission(mission).wait(show, timeout)

        else:
            if show:
                self.show(False)
            else:
                end_time = perf_counter() + timeout
                while self.is_running and (perf_counter() < end_time or timeout == 0):
                    sleep(0.1)

    def cancel(self):
        for m in self._missions.values():
            m.cancel()

    def show(self, asyn=True, keep=False):
        if asyn:
            Thread(target=self._show, args=(2, keep)).start()
        else:
            self._show(0.1, keep)

    def _show(self, wait, keep=False):
        self._stop_printing = False

        if keep:
            Thread(target=self._stop_show).start()

        end_time = perf_counter() + wait
        while not self._stop_printing and (keep or self.is_running or perf_counter() < end_time):
            print(f'\033[K', end='')
            print(f'等待任务数：{self._waiting_list.qsize()}')
            for k, v in self._threads.items():
                m = v['mission'] if v else None
                if m:
                    items = (m.mission.rate, m.mid) if isinstance(m, Task) else (m.rate, m.id)
                    path = f'M{items[1]} {items[0]}% {m}'
                else:
                    path = '空闲'
                print(f'\033[K', end='')
                print(f'线程{k}：{path}')

            print(f'\033[{self.roads + 1}A\r', end='')
            sleep(0.4)

        print(f'\033[1B', end='')
        for i in range(self.roads):
            print(f'\033[K', end='')
            print(f'线程{i}：空闲')

        print()

    def _connect(self, url, session, _headers, method, encoding, **kwargs):
        kwargs = CaseInsensitiveDict(kwargs)
        if 'headers' in kwargs:  # 不知道为什么添加这个才能正常使用多线程
            kwargs['headers'] = CaseInsensitiveDict({**_headers, **kwargs['headers']})
        else:
            kwargs['headers'] = _headers

        r = err = None
        for i in range(self.retry + 1):
            try:
                if method == 'get':
                    r = session.get(url, **kwargs)
                elif method == 'post':
                    r = session.post(url, **kwargs)

                if r:
                    return set_charset(r, encoding), 'Success'

            except Exception as e:
                err = e

            if r and r.status_code in (403, 404):
                break
            if i < self.retry:
                sleep(self.interval)

        # 返回失败结果
        if r is None:
            return None, '连接失败' if err is None else err
        if not r.ok:
            return r, f'状态码：{r.status_code}'
        return None

    def _get_usable_thread(self):
        for k, v in self._threads.items():
            if v is None:
                return k
        return None

    def _stop_show(self):
        input()
        self._stop_printing = True

    def _when_mission_done(self, mission):
        self._running_count -= 1
        if self._print_mode == 'all' or (self._print_mode == 'failed' and mission.result is False):
            print(f'[{mission.RESULT_TEXTS[mission.result]}] {mission.info} {mission.data.url}')
        if self._log_mode == 'all' or (self._log_mode == 'failed' and mission.result is False):
            self._logger.add_data((mission.RESULT_TEXTS[mission.result], mission.data.url, mission.info,
                                   mission.data.save_path, mission.data.rename, mission.data.kwargs))
        mission.session.close()

    def _download(self, mission_or_task, thread_id):
        if mission_or_task.is_done:
            return
        if mission_or_task.state == 'cancel':
            mission_or_task.state = 'done'
            return

        file_url = mission_or_task.data.url
        if isinstance(mission_or_task, Task):
            kwargs = copy(mission_or_task.data.kwargs)
            kwargs['headers']['Range'] = f"bytes={mission_or_task.range[0]}-{mission_or_task.range[1]}"
            r, inf = self._connect(file_url, mission_or_task.mission.session, mission_or_task.mission.headers,
                                   mission_or_task.mission.method, mission_or_task.mission.encoding, **kwargs)

            if r:
                do_download(r, mission_or_task, False)
            else:
                mission_or_task._set_done(False, inf)

            return

        # ===================开始处理mission====================
        mission = mission_or_task
        ok, r, file_size = handle_mission(mission, file_url)
        if not ok:
            return

        # -------------------设置分块任务-------------------
        first = False
        if mission.data.split and file_size and file_size > self.block_size and r.headers.get(
                'Accept-Ranges') == 'bytes':
            first = True
            chunks = [[s, min(s + self.block_size, file_size) - 1] for s in range(0, file_size, self.block_size)]
            chunks[-1][-1] = ''
            chunks_len = len(chunks)

            task1 = Task(mission, chunks[0], f'1/{chunks_len}', chunks[0][1] - chunks[0][0])
            mission.tasks_count = chunks_len
            mission.tasks = [task1]

            for ind, chunk in enumerate(chunks[1:], 2):
                s = file_size - chunk[0] if chunks_len == ind else chunk[1] - chunk[0]
                task = Task(mission, chunk, f'{ind}/{chunks_len}', s)
                mission.tasks.append(task)
                self._run_or_wait(task)

        else:  # 不分块
            task1 = Task(mission, None, '1/1', file_size)
            mission.tasks.append(task1)

        self._threads[thread_id]['mission'] = task1
        do_download(r, task1, first)


def handle_mission(mission, url):
    mission.info = '下载中'
    mission.state = 'running'
    kwargs = mission.data.kwargs

    rename = mission.data.rename
    suffix = mission.data.suffix
    file_exists = mission.data.file_exists
    mission.data.save_path.mkdir(parents=True, exist_ok=True)

    if file_exists == 'skip' and rename:
        tmp = mission.data.save_path / rename
        if tmp.exists() and tmp.is_file():
            mission.file_name = rename
            mission._set_path(mission.data.save_path / rename)
            mission._set_done('skipped', str(mission.path))
            return False, None, None

    r, inf = mission.owner._connect(url, mission.session, mission.headers, mission.method, mission.encoding, **kwargs)

    if mission.is_done:
        return False, None, None
    if not r:
        mission._break_mission(result=False, info=inf)
        return False, None, None

    # -------------------获取文件信息-------------------
    file_info = get_file_info(r, mission.data.save_path, rename, suffix, file_exists,
                              mission.encoding, mission.owner._lock)
    file_size = file_info['size']
    full_path = file_info['path']
    mission._set_path(full_path)
    mission.size = file_size
    mission._overwrote = file_info['overwrite']

    if file_info['skip']:
        mission._set_done('skipped', str(mission.path))
        return False, None, None

    full_Path = Path(full_path)
    if file_exists == 'add' and full_Path.exists():
        mission.data.offset = full_Path.stat().st_size

    return True, r, file_size


def do_download_(r, task, show, size):
    if task.is_done or task.mission.is_done:
        return

    task.set_states(result=None, info='下载中', state='running')
    block_size = 524288  # 512k
    result = None

    try:
        if show and size:
            for chunk in r.iter_content(chunk_size=block_size):
                if task.state in ('cancel', 'done'):
                    result = 'canceled'
                    task.clear_cache()
                    break
                if chunk:
                    task.add_data(chunk, None)
                    print(f'\r{task.rate}% ', end='')

        else:
            for chunk in r.iter_content(chunk_size=block_size):
                if task.state in ('cancel', 'done'):
                    result = 'canceled'
                    task.clear_cache()
                    break
                if chunk:
                    task.add_data(chunk, None)

    except Exception as e:
        result, info = False, f'{r.status_code} {e}'

    else:
        result = 'success' if result is None else result
        info = str(task.path)

    finally:
        r.close()

    task._set_done(result=result, info=info)


def do_download(r, task, first=False):
    if task.is_done or task.mission.is_done:
        return

    task.set_states(result=None, info='下载中', state='running')
    block_size = 524288  # 512k
    result = None

    try:
        if first:  # 分块是第一块
            if task.range[1] <= block_size or task.range[1] % block_size != 0:
                r_content = r.iter_content(chunk_size=task.range[1] + 1)
                task.add_data(next(r_content), seek=0 + task.mission.data.offset)
                if task.state in ('cancel', 'done'):
                    result = 'canceled'
                    task.clear_cache()

            else:
                blocks = task.range[1] // block_size
                remainder = task.range[1] % block_size
                r_content = r.iter_content(chunk_size=block_size)
                for b in range(blocks):
                    task.add_data(next(r_content), seek=b * block_size + task.mission.data.offset)
                    if task.state in ('cancel', 'done'):
                        result = 'canceled'
                        task.clear_cache()
                        break

                if task.state in ('cancel', 'done'):
                    result = 'canceled'
                    task.clear_cache()
                else:
                    task.add_data(next(r_content)[:remainder + 1], blocks * block_size + task.mission.data.offset)

        else:
            if task.range is None:  # 不分块
                for chunk in r.iter_content(chunk_size=block_size):
                    if task.state in ('cancel', 'done'):
                        result = 'canceled'
                        task.clear_cache()
                        break
                    if chunk:
                        task.add_data(chunk, None)

            elif task.range[1] == '':  # 结尾的数据块
                begin = task.range[0]
                for chunk in r.iter_content(chunk_size=block_size):
                    if task.state in ('cancel', 'done'):
                        result = 'canceled'
                        task.clear_cache()
                        break
                    if chunk:
                        task.add_data(chunk, seek=begin + task.mission.data.offset)
                        begin += len(chunk)

            else:  # 有始末数字的数据块
                begin, end = task.range
                num = (end - begin) // block_size
                for ind, chunk in enumerate(r.iter_content(chunk_size=block_size), 1):
                    if task.state in ('cancel', 'done'):
                        result = 'canceled'
                        task.clear_cache()
                        break
                    if chunk:
                        task.add_data(chunk, seek=begin + task.mission.data.offset)
                        if ind <= num:
                            begin += block_size

    except Exception as e:
        result, info = False, f'{r.status_code} {e}'

    else:
        result = 'success' if result is None else result
        info = str(task.path)

    finally:
        r.close()

    task._set_done(result=result, info=info)
