# -*- coding:utf-8 -*-
from json import dumps, loads, JSONDecodeError

from .base_handler import TextLikeHandler, HasLeftRight
from ..data import *
from ..tools import process_content_json, get_real_row, is_single_data


class JSONLHandler(TextLikeHandler, HasLeftRight):
    def __init__(self, recorder):
        super().__init__(recorder)
        self.type = 'jsonl'

    def rows(self, cols=True, sign_col=True, signs=None, deny_sign=False, count=None, begin_row=None, end_row=None):
        if cols is not True and isinstance(cols, (int, str)):
            cols = (cols,)
        if not begin_row:
            begin_row = 1
        return get_jsonl_rows(self, cols=cols, begin_row=begin_row, end_row=end_row or 0,
                              sign_col=sign_col, sign=signs, deny_sign=deny_sign, count=count)

    def _handle_data(self, data):
        return [data] if is_single_data(data) else [self._add_lr_method(self, data)]

    def _record_fast(self):
        with open(self.path, 'a+', encoding=self.encoding) as f:
            for data in self.data:
                for d in data['data']:
                    f.write(handle_jsonl_data(d))

    def _record_slow(self):
        if not self._recorder._file_exists and not self._recorder._path.exists():
            with open(self.path, 'w', encoding=self.encoding):
                pass
        with open(self.path, 'r', encoding=self.encoding) as f:
            lines = f.readlines()
            handle_txt_lines(self.data, lines)
        with open(self.path, 'w', encoding=self.encoding) as f:
            f.writelines(lines)


def handle_txt_lines(data_lst, lines):
    lines_len = len(lines)
    for data in data_lst:
        num = get_real_row(data['coord'], lines_len)
        data_end = num + len(data['data'])
        if lines_len < data_end:
            diff = data_end - lines_len - 1
            [lines.append('\n') for _ in range(diff)]
            lines_len += diff
        for num, i in enumerate(data['data'], num - 1):
            lines[num] = f'{handle_jsonl_data(i)}\n'


def handle_jsonl_data(data):
    if data is None:
        return ''
    elif isinstance(data, str):
        return data
    else:
        return dumps(data, ensure_ascii=False, default=process_content_json)


def get_jsonl_rows(handler, cols, begin_row, end_row, sign_col, sign, deny_sign, count):
    begin_row -= 1
    res = []
    with open(handler.path, 'r', encoding=handler.encoding) as f:
        try:
            for i in range(begin_row):
                next(f)
        except StopIteration:
            return res

        if sign_col is True:  # 获取所有行
            if count or end_row:
                end = min(count + begin_row, end_row) if count and end_row else (end_row or count + begin_row)
            else:
                end = False
            method = get_jsonl_row_key_is_True if cols is True else get_jsonl_row_key_not_True
            for ind, line in enumerate(f, begin_row + 1):
                if end and ind > end:
                    break
                method(handle_line_jsonl(line), res, ind, cols)

        else:  # 获取符合条件的行
            get_jsonl_rows_with_count(f, begin_row, end_row, sign_col, sign, deny_sign,
                                      cols, res, count, handle_line_jsonl)

    return res


def get_jsonl_row_key_is_True(line, res, ind, cols):
    res.append(data2DataWithRow(line, ind))


def get_jsonl_row_key_not_True(line, res, ind, cols):
    if isinstance(line, dict):
        new_line = {}
        keys = list(line.keys())
        for col in cols:
            if isinstance(col, str):
                new_line[col] = line.get(col, None)
            elif isinstance(col, int):
                n_col = col - 1 if col > 0 else col
                try:
                    new_line[col] = line.get(keys[n_col], None)
                except (IndexError, TypeError):
                    new_line[col] = None
        res.append(RowDict(ind, new_line))

    elif isinstance(line, list):
        new_line = []
        for col in cols:
            try:
                if col > 0:
                    col -= 1
                new_line.append(line[col])
            except (IndexError, TypeError):
                pass
        res.append(RowList(ind, new_line))

    else:
        res.append(data2DataWithRow(line, ind) if 1 in cols else RowList(ind, []))


def get_jsonl_rows_with_count(lines, begin_row, end_row, sign_col, sign, deny_sign, cols, res, count, method):
    got = 0
    for ind, line in enumerate(lines, begin_row + 1):
        if (end_row and ind > end_row) or (count and got == count):
            break
        line = method(line)

        if isinstance(line, list) and isinstance(sign_col, int):
            if sign_col > 0:
                sign_col -= 1
            try:
                row_val = line[sign_col]
            except IndexError:
                continue

        elif isinstance(line, dict):
            if isinstance(sign_col, str):
                row_val = line.get(sign_col, None)

            elif isinstance(sign_col, int):
                if sign_col > 0:
                    sign_col -= 1
                try:
                    row_val = list(line.values())[sign_col]
                except IndexError:
                    continue

            else:
                continue

        elif sign_col == 1:  # 只有一个单独数据
            row_val = line

        else:
            continue

        if (row_val not in sign) if deny_sign else (row_val in sign):
            if cols is True:  # 获取整行
                get_jsonl_row_key_is_True(line, res, ind, cols)
            else:  # 只获取对应的列
                get_jsonl_row_key_not_True(line, res, ind, cols)
            got += 1


def handle_line_jsonl(line):
    line = line.strip()
    if not line:
        return None
    try:
        return loads(line)
    except JSONDecodeError:
        return line


def data2DataWithRow(data, row):
    if isinstance(data, dict):
        data = RowDict(row, data)
    elif isinstance(data, list):
        data = RowList(row, data)
    elif isinstance(data, str):
        data = RowStr(data)
        data.row = row
    elif isinstance(data, int):
        data = RowStr(data)
        data.row = row
    elif isinstance(data, float):
        data = RowFloat(data)
        data.row = row
    elif data is None:
        data = RowNone(row)
    return data
