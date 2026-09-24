# -*- coding:utf-8 -*-
from re import match
from time import sleep

from ..tools import no_left_right, is_single_data, is_1D_data, add_left_right
from ..header import ZeroHeader, Header


class BaseHandler(object):
    def __init__(self, recorder):
        self._recorder = recorder
        self.type = ''
        self.clear()

    @property
    def path(self):
        return self._recorder._path

    def record(self):
        raise RuntimeError('记录数据前需先指定文件路径。')

    def clear(self):
        self.data = [{'data': [], 'coord': 0}]
        self.data_count = 0

    def rows(self, cols=True, sign_col=True, signs=None, deny_sign=False, count=None, begin_row=None, end_row=None):
        raise RuntimeError(f'{self.type}文件不支持rows()属性。')

    def add(self, add_method, **data):
        while self._recorder._pause_add:  # 等待其它线程写入结束
            sleep(.01)
        self.__getattribute__(add_method)(**data)
        self.data_count += 1
        if 0 < self._recorder._cache_size <= self.data_count:
            self._recorder.record()

    def add_data(self, data, coord, table):
        raise RuntimeError('请先指定文件路径。')

    def add_link(self, link, coord, content=None, table=None):
        raise TypeError('仅xlsx文件格式支持add_link()。')

    def add_img(self, img_path, coord, width=None, height=None, table=None):
        raise TypeError('仅xlsx文件格式支持add_img()。')

    def add_styles(self, styles, coord=None, rows=None, cols=None, replace=True, table=None):
        raise TypeError('仅xlsx文件格式支持add_styles()。')

    def add_rows_height(self, height, rows=True, table=None):
        raise TypeError('仅xlsx文件格式支持add_rows_height()。')

    def add_cols_width(self, width, cols=True, table=None):
        raise TypeError('仅xlsx文件格式支持add_cols_width()。')

    def __getattr__(self, item):
        try:
            return self.__getattribute__(item)
        except AttributeError:
            if item.startswith('set_'):
                raise TypeError(f'{self.type}文件不支持set.{item[4:]}()方法。')
            else:
                raise TypeError(f'{self.type}文件不支持{item}。')


class HasLeftRight(object):

    @property
    def right(self):
        return self._recorder._settings.get('right', None)

    @property
    def left(self):
        return self._recorder._settings.get('left', None)

    @property
    def _add_lr_method(self):
        return self._recorder._settings.get('add_lr_method', no_left_right)

    def set_left(self, data):
        return self._set_left_right(True, data)

    def set_right(self, data):
        return self._set_left_right(False, data)

    def _set_left_right(self, is_left, data):
        if isinstance(data, (list, dict)):
            data = data
        elif isinstance(data, tuple):
            data = list(data)
        elif data is not None:
            data = [data]
        if is_left:
            self._recorder._settings['left'] = data
        else:
            self._recorder._settings['right'] = data
        if self.left or self.right:
            self._recorder._settings['add_lr_method'] = add_left_right
        else:
            self._recorder._settings['add_lr_method'] = no_left_right

        return self._recorder._setter


class SheetLikeHandler(BaseHandler, HasLeftRight):
    # xlsx, db, csv
    @property
    def header(self):
        return self._get_header()

    @property
    def auto_new_header(self):
        return self._recorder._settings.get('auto_new_header', False)

    def set_auto_new_header(self, on_off=True):
        self._recorder.record()
        self._recorder._settings['auto_new_header'] = on_off
        return self._recorder._setter

    def _parse_coord(self, coord):
        return parse_coord(coord, self.data_col)

    def _handle_data(self, data):
        if is_single_data(data):  # int, float, str, None
            data = [self._add_lr_method(self, (data,))]
        elif is_1D_data(data):  # list, dict, tuple
            data = [self._add_lr_method(self, data)]
        else:  # 二维数组
            data = [self._add_lr_method(self, (d,)) if is_single_data(d)
                    else self._add_lr_method(self, d) for d in data]
        return data

    def _get_header(self) -> Header:
        """获取当前表格的表头"""
        ...


class TextLikeHandler(BaseHandler):
    def __init__(self, recorder):
        super().__init__(recorder)
        self._fast = True
        self._record_method = self._record_fast

    @property
    def encoding(self):
        return self._recorder._settings.get('encoding', 'utf-8')

    def set_encoding(self, encoding):
        self._recorder._settings['encoding'] = encoding
        return self._recorder._setter

    def add_data(self, data, coord=None, **kwargs):
        coord = self._parse_coord(coord)
        data = self._handle_data(data)
        if coord == self.data[-1]['coord'] == 0:
            self.data[-1]['data'].extend(data)
        else:
            self.data.append({'data': data, 'coord': coord})

        if self._fast and coord:
            self._fast = False
            self._record_method = self._record_slow

    def _parse_coord(self, coord):
        coord = coord or 0
        if not isinstance(coord, int):
            raise TypeError('txt文件的coord的参数必须是int。')
        return coord

    def record(self):
        self._record_method()
        self._record_method = self._record_fast
        self._fast = True

    def _record_fast(self):
        pass

    def _record_slow(self):
        pass


def parse_coord(coord, data_col=1):
    if not coord:  # 新增一行，列为data_col
        return_coord = 0, data_col

    elif isinstance(coord, int):
        return_coord = coord, data_col

    elif isinstance(coord, str):  # 'A3'格式
        m = match(r'^[$]?([A-Za-z]{1,3})[$]?(-?\d+)$', coord)
        if not m:
            raise ValueError(f'{coord} 坐标格式不正确。')
        y, x = m.groups()
        return_coord = int(x), ZeroHeader()[y] or 1

    elif isinstance(coord, (tuple, list)) and len(coord) == 2:
        if isinstance(coord[0], int):
            x = int(coord[0])
        elif coord[0] is None:
            x = 0
        else:
            raise TypeError('行格式不正确。')

        if isinstance(coord[1], (str, int)):
            y = coord[1]
        elif coord[1] is None:
            y = 0
        else:
            raise TypeError('列格式不正确。')

        return_coord = x, y

    else:
        raise ValueError(f'{coord} 坐标格式不正确。')

    return return_coord
