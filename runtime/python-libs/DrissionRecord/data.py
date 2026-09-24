# -*- coding:utf-8 -*-
class RowData(dict):
    def __init__(self, row, header, None_val, seq):
        self.header = header
        self.row = row
        self._None_val = None_val
        super().__init__(seq)

    def __getitem__(self, item):
        if isinstance(item, int):
            ite = self.header[item]
            return self.get(item, self._None_val) if ite is None else self.get(ite, self._None_val)
        return self.get(item, self._None_val)

    def col(self, key_or_num, as_num=True):
        return self.header.get_num(key_or_num) if as_num else self.header.get_col(key_or_num)

    def coord(self, key_or_num, col_num=False):
        return self.row, self.col(key_or_num, col_num)


class RowDict(dict):
    def __init__(self, row, seq):
        self.row = row
        super().__init__(seq)

    def __getitem__(self, item):
        if isinstance(item, int):
            if item > 0:
                item -= 1
            try:
                return list(self.values())[item]
            except IndexError:
                return None
        return self.get(item, None)


class RowList(list):
    def __init__(self, row, seq):
        self.row = row
        super().__init__(seq)

    def __getitem__(self, item):
        try:
            return super().__getitem__(item)
        except (IndexError, TypeError):
            return None


class RowStr(str):
    def __init__(self, value):
        super().__init__()
        self.value = value
        self.row = None


class RowInt(int):
    pass


class RowFloat(float):
    pass


class RowNone(object):
    def __init__(self, row):
        self.row = row

    def __bool__(self):
        return False

    def __eq__(self, other):
        return other is None or isinstance(other, RowNone)

    def __hash__(self):
        return hash(None)

    def __repr__(self):
        return 'None'
