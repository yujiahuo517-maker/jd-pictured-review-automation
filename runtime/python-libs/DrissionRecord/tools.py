# -*- coding:utf-8 -*-
from pathlib import Path
from re import search, sub
from typing import Iterable


def get_first_dict_csv(data):
    return data[0]['data'][0] if data and data[0]['data'] and isinstance(data[0]['data'][0], dict) else None


def get_first_dict_xlsx(data):
    return data[0]['data'][0] if data and data[0]['type'] == 'data' and data[0]['data'] and isinstance(
        data[0]['data'][0], dict) else None


def is_single_data(data):
    return not isinstance(data, Iterable) or isinstance(data, str)


def is_1D_data(data):
    if isinstance(data, dict):
        return True
    for i in data:
        return is_single_data(i)
    return True


def remove_end_Nones(in_list):
    h = []
    flag = True
    for i in in_list[::-1]:
        if flag:
            if i in (None, ''):
                continue
            else:
                flag = False
        h.append(i)
    return h[::-1]


def process_content_xlsx(content):
    if isinstance(content, (str, int, float, type(None))):
        data = content
    elif hasattr(content, 'value'):
        data = content.value
    else:
        data = str(content)

    if isinstance(data, str):
        data = sub(r'[\000-\010]|[\013-\014]|[\016-\037]', '', data)

    return data


def process_content_json(content):
    if isinstance(content, (str, int, float, type(None))):
        return content
    elif hasattr(content, 'value'):
        return content.value
    else:
        return str(content)


def data2str(content):
    if isinstance(content, str):
        return content
    elif content is None:
        return ''
    elif hasattr(content, 'value'):
        return str(content.value)
    else:
        return str(content)


def process_nothing(content):
    return content


def do_nothing(*args, **kwargs):
    return


def get_usable_path(path, is_file=True, parents=True):
    path = Path(path)
    parent = path.parent
    if parents:
        parent.mkdir(parents=True, exist_ok=True)
    path = parent / make_valid_name(path.name)
    name = path.stem if path.is_file() else path.name
    ext = path.suffix if path.is_file() else ''

    first_time = True

    while path.exists() and path.is_file() == is_file:
        r = search(r'(.*)_(\d+)$', name)

        if not r or (r and first_time):
            src_name, num = name, '1'
        else:
            src_name, num = r.group(1), int(r.group(2)) + 1

        name = f'{src_name}_{num}'
        path = parent / f'{name}{ext}'
        first_time = None

    return path


def make_valid_name(full_name):
    # ----------------去除前后空格----------------
    full_name = full_name.strip()

    # ----------------去除不允许存在的字符----------------
    if search(r'[<>/\\|:*?\n"]', full_name):
        full_name = sub(r'<', '＜', full_name)
        full_name = sub(r'>', '＞', full_name)
        full_name = sub(r'/', '／', full_name)
        full_name = sub(r'\\', '＼', full_name)
        full_name = sub(r'\|', '｜', full_name)
        full_name = sub(r':', '：', full_name)
        full_name = sub(r'\*', '＊', full_name)
        full_name = sub(r'\?', '？', full_name)
        full_name = sub(r'\n', '', full_name)
        full_name = sub(r'"(.*?)"', r'“\1”', full_name)
        full_name = sub(r'"', '“', full_name)

    # ----------------使总长度不大于255个字符（一个汉字是2个字符）----------------
    r = search(r'(.*)(\.[^.]+$)', full_name)  # 拆分文件名和后缀名
    if r:
        name, ext = r.group(1), r.group(2)
        ext_long = len(ext)
    else:
        name, ext = full_name, ''
        ext_long = 0

    while get_long(name) > 255 - ext_long:
        name = name[:-1]

    return f'{name}{ext}'.rstrip('.')


def get_long(txt):
    txt_len = len(txt)
    return int((len(txt.encode('utf-8')) - txt_len) / 2 + txt_len)


def get_real_row(row, max_row):
    if row <= 0:
        row = max_row + row + 1
    return 1 if row < 1 else row


def no_left_right(handler, data):
    return data


def add_left_right(handler, data):
    if isinstance(data, dict):
        if isinstance(handler.left, dict):
            data = {**handler.left, **data}
        if isinstance(handler.right, dict):
            data = {**data, **handler.right}
        return data

    else:
        return_list = []
        for i in (handler.left, data, handler.right):
            if isinstance(i, dict):
                return_list.extend(list(i.values()))
            elif not i:
                pass
            else:
                return_list.extend(list(i))
        return return_list


def get_key_cols(cols, header):
    if cols is True:
        return True
    elif isinstance(cols, (int, str)):
        cols = header.get_num(cols)
        return [cols] if cols else []
    elif isinstance(cols, (list, tuple)):
        res = []
        for i in cols:
            i = header.get_num(i)
            if i:
                res.append(i)
        return res
    else:
        raise TypeError('col值只能是int或str。')
