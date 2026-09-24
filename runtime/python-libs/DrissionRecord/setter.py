# -*- coding:utf-8 -*-
from importlib import import_module
from pathlib import Path

from .tools import make_valid_name

__TYPES__ = {
    'txt': ('.handlers.txt_handler', 'TXTHandler'),
    'csv': ('.handlers.csv_handler', 'CSVHandler'),
    'xlsx': ('.handlers.xlsx_handler', 'XLSXHandler'),
    'db': ('.handlers.db_handler', 'DBHandler'),
    'byte': ('.handlers.byte_handler', 'ByteHandler'),
    'json': ('.handlers.json_handler', 'JSONHandler'),
    'jsonl': ('.handlers.jsonl_handler', 'JSONLHandler'),
}


class Setter(object):
    def __init__(self, recorder):
        self._recorder = recorder

    # ----------保存在Recorder----------

    def cache_size(self, size):
        if not isinstance(size, int) or size < 0:
            raise TypeError('cache_size值只能是int，且必须>=0')
        self._recorder._cache_size = size
        return self

    def path(self, path, file_type=None):
        if self._recorder._path:
            self._recorder.record()
        p = Path(path)
        self._recorder._path = (p.parent / make_valid_name(p.name)).resolve()
        self._recorder._file_exists = False
        self.file_type(file_type or p.suffix)
        return self

    def file_type(self, file_type):
        if not self._recorder._path:
            raise RuntimeError('指定文件类型前请先指定文件路径。')
        file_type = file_type.lower()
        type_txt = __TYPES__['byte'] if file_type in ('b', 'byte') else __TYPES__.get(file_type.lstrip('.'),
                                                                                      __TYPES__['txt'])
        handler = import_module(type_txt[0], package='DrissionRecord').__getattribute__(type_txt[1])
        self._recorder._handler = handler(self._recorder)
        return self

    def show_msg(self, on_off=False):
        self._recorder._show_msg = on_off
        return self

    def auto_backup(self, interval=None, folder=None, overwrite=None):
        if folder is not None:
            self._recorder._backup_path = folder
        if isinstance(overwrite, bool):
            self._recorder._backup_overwrite = overwrite
        if interval is not None:
            self._recorder._backup_interval = interval
        return self

    def __getattr__(self, item):
        return self._recorder._handler.__getattr__(f'set_{item}')
