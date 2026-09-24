# -*- coding:utf-8 -*-
from .base_handler import BaseHandler


class ByteHandler(BaseHandler):
    __END = (0, 2)

    def __init__(self, recorder):
        super().__init__(recorder)
        self.type = 'byte'

    def add_data(self, data, **kwargs):
        if not isinstance(data, bytes):
            raise TypeError('只能接受bytes类型数据。')
        coord = kwargs['coord']
        if coord is not None and not (isinstance(coord, int) and coord >= 0):
            raise ValueError('seek参数只能接受None或大于等于0的整数。')
        self.data.append((data, coord))

    def record(self):
        if not self._recorder._file_exists and not self._recorder._path.exists():
            with open(self.path, 'wb'):
                pass

        with open(self.path, 'rb+') as f:
            previous = None
            for i in self.data:
                loc = ByteHandler.__END if i[1] is None else (i[1], 0)
                if not (previous == loc == ByteHandler.__END):
                    f.seek(loc[0], loc[1])
                    previous = loc
                f.write(i[0])

    def clear(self):
        self.data = []
        self.data_count = 0
