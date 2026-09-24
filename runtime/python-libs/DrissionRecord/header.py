# -*- coding:utf-8 -*-
from typing import Iterable

from .data import RowData
from .tools import data2str, process_content_xlsx, process_nothing, remove_end_Nones


class BaseHeader(object):
    _NUM_KEY = {}
    _KEY_NUM = {}
    _CONTENT_FUNCS = {'csv': data2str,
                      'xlsx': process_content_xlsx,
                      None: process_nothing}

    def __new__(cls, header=None):
        if not cls._NUM_KEY:
            for i in range(1, 18279):
                col = _get_column_letter(i)
                cls._NUM_KEY[i] = col
                cls._KEY_NUM[col] = i
        return object.__new__(cls)

    @property
    def _str_num(self):
        return Header._KEY_NUM

    @property
    def _num_str(self):
        return Header._NUM_KEY

    def __iter__(self):
        return iter(self.key_num)


class Header(BaseHeader):
    def __init__(self, header=None):
        if isinstance(header, (list, tuple)):
            self._NUM_KEY = {c: str(i) if i not in ('', None) else c
                             for c, i in enumerate(remove_end_Nones(header), start=1)}
        elif isinstance(header, dict):
            self._NUM_KEY = {c: str(v) if v not in ('', None) else None for c, v in enumerate(header.keys())}
        elif isinstance(header, Iterable):
            self._NUM_KEY = {c: str(i) if i not in ('', None) else c
                             for c, i in enumerate(remove_end_Nones(list(header)), start=1)}
        else:
            self._NUM_KEY = {}
            self._KEY_NUM = {}
            return
        self._KEY_NUM = {c: h for h, c in self._NUM_KEY.items()} if self._NUM_KEY else {}

    @property
    def key_num(self):
        return self._KEY_NUM

    @property
    def num_key(self):
        return self._NUM_KEY

    def values(self):
        return self.num_key.values()

    def items(self):
        return self.num_key.items()

    def make_row_data(self, row, row_values, None_val=None):
        data = {self.get_key(col): val for col, val in row_values.items()}
        return RowData(row, self, None_val, data)

    def make_insert_list(self, data, file_type, rewrite):  # 修改时记得ZeroHeader对应方法
        if isinstance(data, dict):
            data = self.make_num_dict(data, file_type)[0]
            data = [data.get(i, None) for i in range(1, max(max(data), len(self.num_key)) + 1)] if data else []
        else:
            data = [self._CONTENT_FUNCS[file_type](v) for v in data]
        return data, False

    def make_insert_list_rewrite(self, data, file_type, rewrite):
        if isinstance(data, dict):
            data, rewrite, header_len = self.make_num_dict_rewrite(data, file_type, rewrite)
            data = [data.get(i, None) for i in range(1, max(max(data), header_len) + 1)]
        else:
            data = [self._CONTENT_FUNCS[file_type](v) for v in data]
        return data, rewrite

    def make_change_list(self, line_data, data, col, file_type, rewrite):
        if isinstance(data, dict):
            data = self.make_num_dict(data, file_type)[0]
            raw_data = {c: v for c, v in enumerate(line_data, 1)}
            raw_data = {**raw_data, **data}
            line_data = [raw_data.get(c, None) for c in range(1, max(raw_data) + 1)]
        else:
            line_data.extend([''] * (col - len(line_data) + len(data) - 1))  # 若列数不够，填充空列
            for k, j in enumerate(data):  # 填充数据
                line_data[col + k - 1] = self._CONTENT_FUNCS[file_type](j)
        return line_data, False

    def make_change_list_rewrite(self, line_data, data, col, file_type, rewrite):
        if isinstance(data, dict):
            data, rewrite, header_len = self.make_num_dict_rewrite(data, file_type, rewrite)
            raw_data = {c: v for c, v in enumerate(line_data, 1)}
            raw_data = {**raw_data, **data}
            line_data = [raw_data.get(c, None) for c in range(1, max(raw_data) + 1)]
        else:
            line_data.extend([''] * (col - len(line_data) + len(data) - 1))  # 若列数不够，填充空列
            for k, j in enumerate(data):  # 填充数据
                line_data[col + k - 1] = self._CONTENT_FUNCS[file_type](j)
        return line_data, rewrite

    def make_num_dict(self, *keys):
        data = keys[0]
        file_type = keys[1]
        val = {}
        for k, v in data.items():
            num = self.get_num(k)
            if num:
                val[num] = self._CONTENT_FUNCS[file_type](v)
        return val, False, 0

    def make_num_dict_rewrite(self, *keys):
        data, file_type, rewrite = keys
        val = {}
        header_len = len(self.num_key)
        for k, v in data.items():
            if isinstance(k, str) and k not in self.key_num:
                header_len += 1
                self.key_num[k] = header_len
                self.num_key[header_len] = k
                rewrite = True
            num = self.get_num(k)
            if num:
                val[num] = self._CONTENT_FUNCS[file_type](v)
        return val, rewrite, header_len

    def get_key(self, key_or_num):
        if isinstance(key_or_num, str):
            return key_or_num
        key = self[key_or_num]
        return key_or_num if key is None else key

    def get_col(self, key_or_num):
        return ZeroHeader()[self._get_num(key_or_num)]

    def get_num(self, key_or_num):  # 修改时记得ZeroHeader
        if isinstance(key_or_num, int):
            return self._num2num(key_or_num)
        elif isinstance(key_or_num, str):
            return self.key_num.get(key_or_num, None)
        else:
            raise TypeError(f'col值只能是int或str。当前值：{key_or_num}')

    def _get_num(self, key_or_num):
        return self.get_num(key_or_num) or self.get_num(Col(key_or_num))

    def _num2num(self, num):
        if num > 0:
            return num
        elif num < 0:
            l = len(self)
            return num % l + 1 if -num <= l else None
        else:
            return len(self) + 1

    def __getitem__(self, item):
        if isinstance(item, str):
            return self.key_num.get(item)
        elif isinstance(item, int) and item != 0:
            return self.num_key.get(self._num2num(item), None)
        else:
            raise ValueError('值只能时str或int，且不能为0。')

    def __len__(self):
        return len(self.num_key)

    def __repr__(self):
        return str(self.num_key)

    def __bool__(self):
        return True if self.num_key else False


class ZeroHeader(Header):
    _OBJ = None

    def __new__(cls):
        super().__new__(cls)
        if cls._OBJ is None:
            cls._OBJ = object.__new__(cls)
        return cls._OBJ

    def __init__(self):
        return

    def get_num(self, col):
        if isinstance(col, int) and col > 0:
            return col
        elif isinstance(col, str):
            return self.key_num.get(col.upper(), None)
        else:
            raise TypeError(f'表头行为0时，col值只能str或大于0的int。当前值：{col}')

    def make_insert_list(self, data, file_type, rewrite):
        if isinstance(data, dict):
            val = self.make_num_dict(data, file_type)[0]
            data = [val.get(c, None) for c in range(1, max(val) + 1)] if val else []
        else:
            data = [self._CONTENT_FUNCS[file_type](v) for v in data]
        return data, False

    def make_insert_list_rewrite(self, data, file_type, rewrite):
        return self.make_insert_list(data, file_type, rewrite)

    def make_num_dict_rewrite(self, *keys):
        data, file_type, rewrite = keys
        return self.make_num_dict(data, file_type)

    def get_col(self, key_or_num):
        return self[key_or_num] if isinstance(key_or_num, int) else key_or_num

    def _num2num(self, num):
        if num > 0:
            return num if num <= len(self) else None
        elif num < 0:
            return num % len(self) + 1 if -num <= len(self) else None
        else:
            raise ValueError('列序号不能为0。')

    def _get_num(self, key_or_num):
        return self.get_num(key_or_num) or 1

    def __getitem__(self, item):
        return self.num_key.get(item, None) if isinstance(item, int) else self.key_num.get(item.upper(), None)

    def __len__(self):
        return 0


def Col(key):
    try:
        return ZeroHeader().key_num[key.upper()]
    except KeyError:
        raise KeyError(f'无此表头项：{key}')


def _get_column_letter(col_idx):
    letters = []
    while col_idx > 0:
        col_idx, remainder = divmod(col_idx, 26)
        if remainder == 0:
            remainder = 26
            col_idx -= 1
        letters.append(chr(remainder + 64))
    return ''.join(reversed(letters))
