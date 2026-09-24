# -*- coding:utf-8 -*-
from sqlite3 import connect

from .base_handler import SheetLikeHandler
from ..data import RowData
from ..header import Header
from ..tools import process_content_json


class DBHandler(SheetLikeHandler):
    def __init__(self, recorder):
        super().__init__(recorder)
        self.type = 'db'
        self._conn = None
        self._cur = None
        self._table = None

    @property
    def tables(self):
        self._connect()
        self._cur.execute("select name from sqlite_master where type='table'")
        tables = self._cur.fetchall()
        self._close_connection()
        return [i[0] for i in tables]

    @property
    def table(self):
        return self._table

    def add_data(self, data, **kwargs):
        table = kwargs['table'] or self.table
        if not isinstance(table, str):
            raise RuntimeError('未指定数据库表名。')
        data = self._handle_data(data)
        self.data.setdefault(table, []).extend(data)

    def record(self):
        if not self._recorder._file_exists:
            self._recorder._path.parent.mkdir(parents=True, exist_ok=True)
        self._connect()
        self._cur.execute("select name from sqlite_master where type='table'")
        tables = {t[0]: [] for t in self._cur.fetchall()}

        for table, data in self.data.items():
            if table in tables:
                self._cur.execute(f"PRAGMA table_info(`{table}`)")
                tables[table] = [i[1] for i in self._cur.fetchall()]
            else:
                if isinstance(data[0], list):
                    self._close_connection()
                    raise TypeError('新建表格首次须接收数据需为dict格式。')
                self._cur.execute(f"CREATE TABLE `{table}` (`{'`,`'.join(data[0].keys())}`)")
                tables[table] = list(data[0])
            if isinstance(data[0], dict):
                curr_keys = set(data[0].keys())
            else:
                curr_keys = len(data[0])
            data_list = []
            table_cols_set = set(tables[table])
            table_cols_long = len(tables[table])

            for d in data:
                if isinstance(d, dict):
                    tmp_keys = set(d.keys())
                    diff = tmp_keys - table_cols_set
                    if diff:
                        if self.auto_new_header:
                            for key in tmp_keys - table_cols_set:
                                add_db_col(self._cur, table, key)
                                table_cols_set.add(key)
                                tables[table].append(key)
                            table_cols_long = len(tables[table])
                        else:
                            for key in diff:
                                d.pop(key)
                                tmp_keys.remove(key)

                    d = {k: process_content_json(d[k]) for k in tables[table] if k in d}

                else:
                    tmp_keys = len(d)
                    if table_cols_long > tmp_keys:
                        d = ok_list_db(d)
                        d.extend([None] * (table_cols_long - tmp_keys))
                    else:
                        self._close_connection()
                        raise RuntimeError('数据个数大于列数（注意left和right属性）。')

                if tmp_keys != curr_keys:
                    self._to_database(data_list, table, tables)
                    curr_keys = tmp_keys
                    data_list = []

                data_list.append(d)

            if data_list:
                self._to_database(data_list, table, tables)

        self._conn.commit()
        self._close_connection()

    def rows(self, cols=True, sign_col=True, signs=None, deny_sign=False, count=None, begin_row=None, end_row=None):
        self._connect()
        table = self._table
        if not table:
            raise RuntimeError('使用rows()前必须先用set.table()指定表名。')
        self._cur.execute(f"PRAGMA table_info(`{table}`)")
        all_cols = [i[1] for i in self._cur.fetchall()]
        header = Header(all_cols)

        if cols is True:
            cols_txt = '*'
            cols = list(header)
        else:
            if isinstance(cols, (str, int)):
                cols = (cols,)
            elif not isinstance(cols, (list, tuple)):
                raise TypeError('cols必须为list、tuple、str或int。')
            header_len = len(header)
            cols = [header.get_key(i) for i in cols if isinstance(i, str) or i <= header_len]
            cols_txt = '`' + '`,`'.join(cols) + '`'

        sql = f'select {cols_txt} from `{table}`'
        if sign_col is not True:
            if not isinstance(signs, (list, tuple)):
                signs = (signs,)
            sign_col = header.get_key(sign_col)
            if isinstance(sign_col, int):
                raise RuntimeError('cols指定的列数超出表头。')
            is_not = 'not ' if deny_sign else ''
            all_signs = []
            for s in signs:
                if isinstance(s, str):
                    all_signs.append(f"'{s}'")
                elif s is None:
                    all_signs.append('null')
                else:
                    all_signs.append(str(s))
            all_signs = ', '.join(all_signs)
            sql = f'{sql} where {sign_col} {is_not}in ({all_signs})'

        begin_row = begin_row or 1
        if end_row and end_row > begin_row:
            sql = f"{sql} limit {end_row - 1}"
        if begin_row > 1:
            sql = f"{sql} offset {begin_row - 1}"

        self._cur.execute(sql)
        res = self._cur.fetchall()
        if count and count < len(res):
            res = res[:count]
        r = [RowData(0, header, None, dict(zip(cols, i))) for i in res]
        self._close_connection()
        return r

    def run_sql(self, sql, single=True, commit=False):
        self._connect()
        self._cur.execute(sql)
        r = self._cur.fetchone() if single else self._cur.fetchall()
        if commit:
            self._conn.commit()
        self._close_connection()
        return r

    def clear(self):
        self.data = {}
        self.data_count = 0

    def set_table(self, name):
        if '`' in name:
            raise ValueError('table名称不能包含字符"`"。')
        self._table = name
        return self._recorder._setter

    def _get_header(self):
        if not self._table:
            return None
        self._connect()
        try:
            self._cur.execute(f"PRAGMA table_info(`{self._table}`)")
            columns = self._cur.fetchall()
            return Header([col[1] for col in columns])
        finally:
            self._close_connection()

    def _connect(self):
        self._conn = connect(self.path)
        self._cur = self._conn.cursor()
        self._recorder._file_exists = True

    def _close_connection(self):
        if self._conn is not None:
            try:
                self._cur.close()
                self._conn.close()
            except:
                pass

    def _to_database(self, data_list, table, tables):
        if isinstance(data_list[0], dict):
            question_masks = ','.join('?' * len(data_list[0]))
            keys_txt = '`' + '`,`'.join(data_list[0]) + '`'
            values = [list(i.values()) for i in data_list]
            sql = f'INSERT INTO `{table}` ({keys_txt}) values ({question_masks})'

        else:
            question_masks = ','.join('?' * len(tables[table]))
            values = data_list
            sql = f'INSERT INTO `{table}` values ({question_masks})'

        self._cur.executemany(sql, values)


def ok_list_db(data_list):
    if isinstance(data_list, (dict, Header)):
        data_list = data_list.values()
    return [process_content_json(i) for i in data_list]


def add_db_col(cur, table, key):
    sql = f'ALTER TABLE `{table}` ADD COLUMN `{key}`'
    cur.execute(sql)
