# -*- coding:utf-8 -*-
from pathlib import Path
from threading import Lock
from time import sleep

from .handlers.base_handler import BaseHandler
from .setter import Setter
from .tools import make_valid_name, get_usable_path


class Recorder(object):
    def __init__(self, path=None, cache_size=1000, file_type=None):
        self._lock = Lock()
        self._cache_size = cache_size or 0
        self._path = None
        self._pause_add = False  # 文件写入时暂停接收输入
        self._pause_write = False  # 标记文件正在被一个线程写入
        self._setter = Setter(self)
        self._file_exists = False
        self._backup_path = 'backup'
        self._backup_times = 0
        self._backup_interval = 0  # 多少次就自动保存
        self._backup_overwrite = False
        self._settings = {}
        self._show_msg = True
        if path:
            self.set.path(path, file_type)
        else:
            self._handler = BaseHandler(self)

    @property
    def cache_size(self):
        return self._cache_size

    @property
    def path(self):
        return self._path

    @property
    def set(self):
        return self._setter

    def add_data(self, data, coord=None, table=None):
        self._handler.add('add_data', data=data, coord=coord, table=table)

    def add_link(self, link, coord, content=None, table=None):
        self._handler.add('add_link', link=link, coord=coord, content=content, table=table)

    def add_img(self, img_path, coord, width=None, height=None, table=None):
        self._handler.add('add_img', img_path=img_path, coord=coord, width=width, height=height, table=table)

    def add_styles(self, styles, coord=None, rows=None, cols=None, replace=True, table=None):
        self._handler.add('add_styles', styles=styles, coord=coord, rows=rows, cols=cols, replace=replace, table=table)

    def add_rows_height(self, height, rows=True, table=None):
        self._handler.add('add_rows_height', height=height, rows=rows, table=table)

    def add_cols_width(self, width, cols=True, table=None):
        self._handler.add('add_cols_width', width=width, cols=cols, table=table)

    def rows(self, cols=True, sign_col=True, signs=None, deny_sign=False, count=None, begin_row=None, end_row=None):
        if not self._path or not self._path.exists():
            raise RuntimeError('未指定文件路径或文件不存在。')
        if not isinstance(signs, (list, tuple, set)):
            signs = (signs,)
        return self._handler.rows(cols=cols, sign_col=sign_col, signs=signs, deny_sign=deny_sign, count=count,
                                  begin_row=begin_row, end_row=end_row)

    def record(self):
        if not self._handler.data_count:
            return self._path
        if not self._path:
            raise ValueError('保存路径为空。')

        with self._lock:
            if self._backup_interval and self._backup_times >= self._backup_interval:
                self.backup(folder=self._backup_path, overwrite=self._backup_overwrite)

            self._pause_add = True  # 写入文件前暂缓接收数据
            if self._show_msg:
                print(f'{self.path} 开始写入文件，切勿关闭进程。')

            self._path.parent.mkdir(parents=True, exist_ok=True)
            while True:
                try:
                    while self._pause_write:  # 等待其它线程写入结束
                        sleep(.01)

                    self._pause_write = True
                    self._handler.record()
                    break

                except PermissionError:
                    print('\r文件被打开，保存失败，请关闭，程序会自动重试。', end='')

                except Exception:
                    try:
                        with open('failed_data.txt', 'a+', encoding='utf-8') as f:
                            f.write(str(self.data) + '\n')
                        print('保存失败的数据已保存到failed_data.txt。')
                        from traceback import print_exc
                        print_exc()
                    except ImportError:
                        self._handler.clear()
                        return None
                    except:
                        print('未保存数据：', self.data)
                        self._handler.clear()
                        return None
                    self._handler.clear()
                    return None

                finally:
                    self._pause_write = False
                    self._pause_add = False

                sleep(.3)

            if self._show_msg:
                print(f'{self.path} 写入文件结束。')
            self.clear()
            self._pause_add = False

        if self._backup_interval:
            self._backup_times += 1
        self._file_exists = True
        return self._path

    def backup(self, folder=None, name=None, overwrite=None):
        if not self._path:
            raise RuntimeError('实用backup()前应先设置文件路径。')
        src_path = self._path
        if not self._file_exists:
            if not src_path.exists():
                return None
            self._file_exists = True

        if overwrite is None:
            overwrite = self._backup_overwrite
        folder = Path(folder if folder else self._backup_path)
        folder.mkdir(parents=True, exist_ok=True)
        if not name:
            name = src_path.name
        elif not name.endswith(src_path.suffix):
            name = f'{name}{src_path.suffix}'
        path = folder / make_valid_name(name)
        if not overwrite and path.exists():
            from datetime import datetime
            name = f'{path.stem}_{datetime.now().strftime("%Y%m%d%H%M%S")}{path.suffix}'
            path = get_usable_path(folder / name)

        from shutil import copy
        copy(self._path, path)
        self._backup_times = 0
        return path.resolve()

    def delete(self):
        if self._path:
            with self._lock:
                self._path.unlink(missing_ok=True)
                self._file_exists = False

    def __getattr__(self, item):
        return self._handler.__getattr__(item)
