# -*- coding:utf-8 -*-
from json import load, dump, JSONDecodeError

from .base_handler import TextLikeHandler, HasLeftRight
from ..tools import is_single_data, process_content_json


class JSONHandler(TextLikeHandler, HasLeftRight):
    def __init__(self, recorder):
        super().__init__(recorder)
        self.type = 'json'

    def add_data(self, data, coord=None, **kwargs):
        coord = _parse_coord(coord)
        data = self._handle_data(data)
        self.data.append({'data': data, 'coord': coord})

    def record(self):
        if not self._recorder._file_exists and not self._recorder._path.exists():
            with open(self.path, 'w', encoding=self.encoding):
                pass
            if isinstance(self.data[0]['coord'][0], int):
                data = []
            elif isinstance(self.data[0]['coord'][0], str):
                data = {}
            else:
                raise TypeError('coord内元素只能是int或str。')
        else:
            with open(self.path, 'r', encoding=self.encoding) as f:
                try:
                    data = load(f)
                except JSONDecodeError:
                    if isinstance(self.data[0]['coord'][0], int):
                        data = []
                    elif isinstance(self.data[0]['coord'][0], str):
                        data = {}
                    else:
                        raise TypeError('coord内元素只能是int或str。')

        for i in self.data:
            r, k = nav(data, i['coord'])
            if isinstance(r, dict):
                if isinstance(k, str):
                    r[k] = i['data']
                else:
                    raise TypeError(f"字典中的索引需使用str，而非 {k}")
            elif isinstance(r, list):
                if k == 0:
                    r.append(i['data'])
                else:
                    while len(r) < k:
                        r.append(None)
                    r[k - 1] = i['data']

        with open(self.path, 'w', encoding=self.encoding) as f:
            dump(data, f, ensure_ascii=False, indent=2, default=process_content_json)

    def rows(self, **kwargs):
        with open(self.path, 'r', encoding=self.encoding) as f:
            return load(f)

    def clear(self):
        self.data = []
        self.data_count = 0

    def _handle_data(self, data):
        return data if is_single_data(data) else self._add_lr_method(self, data)


def _parse_coord(coord):
    if isinstance(coord, (list, tuple)):
        return coord
    elif isinstance(coord, (int, str)):
        return [coord]
    elif coord is None:
        return [0]
    else:
        raise TypeError(f'coord参数只能是int、str、list或tuple，现在是{coord}')


def nav(data, parts):
    current = data
    parent = None
    last_key = None
    for i, part in enumerate(parts):
        parent = current
        last_key = part
        is_0 = False
        if isinstance(part, int):
            if part > 0:
                part -= 1
            elif part == 0:
                is_0 = True
        if i == len(parts) - 1:
            break

        if isinstance(current, dict):
            if not isinstance(part, str):
                raise TypeError(f"字典中的索引需使用str，而非 {part}")
            if part not in current:
                current[part] = {} if isinstance(parts[i + 1], str) else []
            current = current[part]

        elif isinstance(current, list):
            if isinstance(part, int):
                if is_0:
                    current.append({} if isinstance(parts[i + 1], str) else [])
                    current = current[-1]
                elif len(current) < part:
                    while len(current) < part+1:
                        current.append(None)
                    current[-1] = {} if isinstance(parts[i + 1], str) else []
                    current = current[-1]
                else:
                    current = current[part]
            else:
                raise TypeError(f"列表索引必须是int，而非 {part}")

        else:
            raise TypeError(f"无法导航至 {current}")

    return parent, last_key
