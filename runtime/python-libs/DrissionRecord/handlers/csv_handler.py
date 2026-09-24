# -*- coding:utf-8 -*-
from csv import reader as csv_reader, writer as csv_writer

from .base_handler import SheetLikeHandler, TextLikeHandler
from ..header import Header, ZeroHeader
from ..tools import data2str, get_first_dict_csv, get_key_cols, get_real_row


class CSVHandler(SheetLikeHandler, TextLikeHandler):
    def __init__(self, recorder):
        super().__init__(recorder)
        self.type = 'csv'
        self._header = None
        self._header_row = 1
        self._data_col = 1

    @property
    def delimiter(self):
        return self._recorder._settings.get('delimiter', ',')

    @property
    def quote_char(self):
        return self._recorder._settings.get('quote_char', '"')

    @property
    def header(self):
        return self._get_header()

    @property
    def header_row(self):
        return self._header_row

    @property
    def data_col(self):
        return self._data_col

    def add_data(self, data, coord, **kwargs):
        coord = self._parse_coord(coord)
        data = self._handle_data(data)
        if coord == self.data[-1]['coord'] and coord[0] == 0:
            self.data[-1]['data'].extend(data)
        else:
            self.data.append({'data': data, 'coord': coord})

        if self._fast and coord[0]:
            self._fast = False
            self._record_method = self._record_slow

    def clear(self):
        self.data = [{'data': [], 'coord': (0, 1)}]
        self.data_count = 0

    def rows(self, cols=True, sign_col=True, signs=None, deny_sign=False, count=None, begin_row=None, end_row=None):
        header = self.header
        if sign_col is not True:
            sign_col = header.get_num(sign_col) or 1
        cols = get_key_cols(cols, header)
        if not begin_row:
            begin_row = self._header_row + 1
        return get_csv_rows(self, header=header, cols=cols, begin_row=begin_row, end_row=end_row or 0,
                            sign_col=sign_col, sign=signs, deny_sign=deny_sign, count=count)

    def set_header(self, header, to_file=True, row=None, **kwargs):
        if not isinstance(header, (list, tuple)):
            raise ValueError('header必须为list或tuple格式。')

        self._recorder.record()
        row = row or self._header_row
        with self._recorder._lock:
            header = Header(header)
            self._header = header
            if to_file:
                self._header2file(header, row)

        return self._recorder._setter

    def set_header_row(self, num, **kwargs):
        if num < 0:
            raise ValueError('num不能小于0。')
        self._recorder.record()
        with self._recorder._lock:
            self._header_row = num
            self._header = ZeroHeader() if num == 0 else None
        return self._recorder._setter

    def set_data_col(self, col):
        if col is None:
            self._data_col = 0
        elif not isinstance(col, (int, str)) or col == '':
            raise TypeError('col值只能是int、None或非空str。')
        else:
            self._data_col = col
        return self._recorder._setter

    def set_delimiter(self, delimiter):
        self._recorder._settings['delimiter'] = delimiter
        return self._recorder._setter

    def set_quote_char(self, quote_char):
        self._recorder._settings['quote_char'] = quote_char
        return self._recorder._setter

    def _record_fast(self):
        file, new_csv = get_csv(self._recorder)
        writer = csv_writer(file, delimiter=self.delimiter, quotechar=self.quote_char)
        get_and_set_csv_header(self, new_csv, file, writer)
        rewrite_method = 'make_insert_list_rewrite' if self.auto_new_header else 'make_insert_list'

        rewrite = False
        for d in self.data:
            col = self._header._get_num(d['coord'][1])
            for data in d['data']:
                data, rewrite = self._header.__getattribute__(rewrite_method)(data, 'csv', rewrite)
                data = [None] * (col - 1) + data
                writer.writerow(data)
        file.close()

        if rewrite:
            self._header2file(self._header, self._header_row)

    def _record_slow(self):
        file, new_csv = get_csv(self._recorder)
        writer = csv_writer(file, delimiter=self.delimiter, quotechar=self.quote_char)
        get_and_set_csv_header(self, new_csv, file, writer)
        file.seek(0)
        reader = csv_reader(file, delimiter=self.delimiter, quotechar=self.quote_char)
        lines = list(reader)
        lines_count = len(lines)
        header = self._header

        rewrite = False
        method = 'make_change_list_rewrite' if self.auto_new_header else 'make_change_list'
        for i in self.data:
            data = i['data']
            row = get_real_row(i['coord'][0], lines_count)
            col = header._get_num(i['coord'][1])
            for r, da in enumerate(data, row):
                add_rows = r - lines_count
                if add_rows > 0:  # 若行数不够，填充行数
                    [lines.append([]) for _ in range(add_rows)]
                    lines_count += add_rows
                row_num = r - 1
                lines[row_num], rewrite = self._header.__getattribute__(method)(lines[row_num], da, col,
                                                                                'csv', rewrite)

        if rewrite:
            [lines.append([]) for _ in range(self._header_row - lines_count)]  # 若行数不够，填充行数
            lines[self._header_row - 1] = list(header.num_key.values())

        file.close()
        writer = csv_writer(open(self.path, 'w', encoding=self.encoding, newline=''),
                            delimiter=self.delimiter, quotechar=self.quote_char)
        writer.writerows(lines)

    def _header2file(self, header, row):
        if not self.path:
            raise FileNotFoundError('未指定文件。')
        from csv import writer
        if self._recorder._file_exists or self._recorder._path.exists():
            with open(self.path, 'r', newline='', encoding=self.encoding) as f:
                lines = f.readlines()
                content1 = lines[:row - 1]
                content2 = lines[row:]

            with open(self.path, 'w', newline='', encoding=self.encoding) as f:
                f.write("".join(content1))
                csv_write = writer(f, delimiter=self.delimiter, quotechar=self.quote_char)
                con_len = len(content1)
                if con_len < row - 1:
                    for _ in range(row - con_len - 1):
                        csv_write.writerow([])
                csv_write.writerow([data2str(i) for i in header.values()])

            with open(self.path, 'a+', newline='', encoding=self.encoding) as f:
                f.write("".join(content2))

        else:
            self._recorder._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, 'w', newline='', encoding=self.encoding) as f:
                csv_write = writer(f, delimiter=self.delimiter, quotechar=self.quote_char)
                for _ in range(row - 1):
                    csv_write.writerow([])
                csv_write.writerow([data2str(i) for i in header.values()])

        self._recorder._file_exists = True

    def _get_header(self):
        if self._header is not None:
            return self._header
        if not self.path or not self._recorder._path.exists():
            return None
        from csv import reader
        with open(self.path, 'r', newline='', encoding=self.encoding) as f:
            u = reader(f, delimiter=self.delimiter, quotechar=self.quote_char)
            try:
                for _ in range(self.header_row):
                    header = next(u)
            except StopIteration:  # 文件是空的
                header = []
        self._header = Header(header)
        return self._header


def get_csv(recorder):
    new_csv = not recorder._file_exists and not recorder._path.exists()
    return open(recorder.path, 'a+', newline='', encoding=recorder._handler.encoding), new_csv


def get_and_set_csv_header(handler, new_csv, file, writer):
    if not handler._header_row:
        return

    if new_csv:
        if handler._header:
            for _ in range(handler._header_row - 1):
                writer.writerow([])
            writer.writerow([data2str(i) for i in handler._header.values()])

        elif handler._header is None and handler.data_count:
            data = get_first_dict_csv(handler.data)
            if data:
                handler._header = Header([h for h in data.keys() if isinstance(h, str)])
            else:
                handler._header = Header()
            if handler._header:
                writer.writerow([data2str(i) for i in handler._header.values()])
        else:
            handler._header = Header()

    elif handler._header is None:  # 从文件读取表头
        file.seek(0)
        reader = csv_reader(file, delimiter=handler.delimiter, quotechar=handler.quote_char)
        header = []
        try:
            for _ in range(handler._header_row):
                header = next(reader)
        except StopIteration:
            pass
        handler._header = Header(header)
        file.seek(2)


def get_csv_rows(handler, header, cols, begin_row, end_row, sign_col, sign, deny_sign, count):
    sign = ['' if i is None else str(i) for i in sign]
    if begin_row>1:
        begin_row -= 1
    res = []
    with open(handler.path, 'r', encoding=handler.encoding) as f:
        try:
            for i in range(begin_row):
                next(f)
        except StopIteration:
            return res
        reader = csv_reader(f, delimiter=handler.delimiter, quotechar=handler.quote_char)

        if sign_col is True:  # 获取所有行
            header_len = len(header)
            if count or end_row:
                end = min(count + begin_row, end_row) if count and end_row else (end_row or count + begin_row)
            else:
                end = False
            method = get_csv_rows_key_is_True if cols is True else get_csv_rows_key_not_True
            for ind, line in enumerate(reader, begin_row + 1):
                if end and ind > end:
                    break
                method(line, res, header, ind, cols, header_len)

        else:  # 获取符合条件的行
            if sign_col > 1:
                sign_col -= 1
            get_csv_rows_with_count(reader, begin_row, end_row, sign_col, sign, deny_sign,
                                    cols, res, header, count)

    return res


def get_csv_rows_key_is_True(line, res, header, ind, cols, header_len):
    if not line:
        res.append(header.make_row_data(ind, {col: '' for col in range(1, header_len + 1)}))
    else:
        line_len = len(line)
        x = max(header_len, line_len)
        res.append(header.make_row_data(ind, {col: line[col - 1] if col <= line_len else ''
                                              for col in range(1, x + 1)}))


def get_csv_rows_key_not_True(line, res, header, ind, cols, header_len):
    x = len(line) + 1
    res.append(header.make_row_data(ind, {col: line[col - 1] if col < x else '' for col in cols}))


def get_csv_rows_with_count(lines, begin_row, end_row, sign_col, sign, deny_sign, cols, res, header, count):
    got = 0
    header_len = len(header)
    for ind, line in enumerate(lines, begin_row + 1):
        if (end_row and ind > end_row) or (count and got == count):
            break
        row_sign = '' if sign_col > len(line) - 1 else line[sign_col]
        if (row_sign not in sign) if deny_sign else (row_sign in sign):
            if cols is True:  # 获取整行
                if not line:
                    res.append(header.make_row_data(ind, {col: '' for col in range(1, header_len + 1)}))
                else:
                    line_len = len(line)
                    x = max(header_len, line_len)
                    res.append(header.make_row_data(ind, {col: line[col - 1] if col <= line_len else ''
                                                          for col in range(1, x + 1)}))
            else:  # 只获取对应的列
                x = len(line) + 1
                res.append(header.make_row_data(ind, {col: line[col - 1] if col < x else '' for col in cols}))
            got += 1
