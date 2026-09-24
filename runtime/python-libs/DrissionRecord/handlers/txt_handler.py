# -*- coding:utf-8 -*-
from .base_handler import TextLikeHandler
from ..data import RowStr
from ..tools import get_real_row, data2str


class TXTHandler(TextLikeHandler):
    def __init__(self, recorder):
        super().__init__(recorder)
        self.type = 'txt'

    def rows(self, **kwargs):
        begin_row, end_row, count = kwargs['begin_row'] or 1, kwargs['end_row'], kwargs['count']
        begin_row -= 1
        res = []
        with open(self._recorder.path, 'r', encoding=self.encoding) as f:
            try:
                for i in range(begin_row):
                    next(f)
            except StopIteration:
                return res

            got = 0
            for ind, line in enumerate(f, begin_row + 1):
                if (end_row and ind > end_row) or (count and got == count):
                    break
                t = RowStr(line.strip('\n'))
                t.row = ind
                res.append(t)
                got += 1

        return res

    def _handle_data(self, data):
        if not isinstance(data, (list, tuple)):
            data = [data]
        elif not data:
            data = ['']
        return data

    def _record_fast(self):
        with open(self.path, 'a+', encoding=self.encoding) as f:
            for data in self.data:
                for d in data['data']:
                    f.write(f'{data2str(d)}\n')

    def _record_slow(self):
        if not self._recorder._file_exists and not self._recorder._path.exists():
            with open(self.path, 'w', encoding=self.encoding):
                pass
        with open(self.path, 'r', encoding=self.encoding) as f:
            lines = f.readlines()
            slowData2list(self.data, lines)
        with open(self.path, 'w', encoding=self.encoding) as f:
            f.writelines(lines)


def slowData2list(data_lst, lines):
    for data in data_lst:
        if data['coord'] == 0:
            [lines.extend([f'{dd}\n' for dd in data2str(d).split('\n')]) for d in data['data']]
            continue

        lines_len = len(lines)
        num = get_real_row(data['coord'], lines_len)
        cur_data = []
        [cur_data.extend(data2str(d).split('\n')) for d in data['data']]

        data_end = num + len(cur_data)
        if lines_len < data_end:
            diff = data_end - lines_len - 1
            [lines.append('\n') for _ in range(diff)]
            lines_len += diff
        for num, i in enumerate(cur_data, num - 1):
            lines[num] = f'{i}\n'
