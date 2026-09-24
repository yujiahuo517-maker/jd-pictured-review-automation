# -*- coding: utf-8 -*-
"""**MCP 返回体保护** —— 大列表别原样塞回 MCP。

2026-08-24 实撞：`yx_ms_export_fetch` 返回秒杀可报清单 4483 行 ≈ **435KB**，
这个 MCP 工具**卡死 >20 分钟**（同一个函数在库层只要 1.1s）⇒ 是传输/体积问题，不是接口问题。
当天我在 yx_server 里就地写了一份"只回样例 + 落盘路径"的补丁；随后审查发现同一形状的隐患
还有至少三处（`osw_product_all` 默认 2000 行全字段、`ge_ssm_sku` 全量 rows 无截断、
`jzt_swa_ad_all` 上限 5000 行），**再各写一份就是第四次重复发明**——所以收在这里。

设计取舍：
· **不改变"拿得到全量"这件事**：`with_rows=True` 原样返回；落盘的工具照旧给 `file`/`path`。
· **省略要显式说出来**：把 `rows` 换成一句人话 + 另给 `rows_sample` / `rows_count`，
  绝不悄悄截断成"看起来是全部"的短列表——那正是本包最忌讳的静默错误。
· 小结果集（≤sample）原样返回，别为几行也加一层壳。
"""
from __future__ import annotations

from typing import Any


def cap_rows(payload: Any, key: str = "rows", with_rows: bool = False,
             sample: int = 5, where: str = None) -> Any:
    """把 `payload[key]` 这个大列表换成"样例 + 条数 + 去哪拿全量"。

    `where`：全量在哪（落盘路径的键名，或一句取法提示），会写进省略说明里。
    返回同一个 dict（就地改），非 dict / 非 list / `with_rows=True` 一律原样。
    """
    if with_rows or not isinstance(payload, dict):
        return payload
    rows = payload.get(key)
    if not isinstance(rows, list) or len(rows) <= max(0, int(sample)):
        return payload
    n = len(rows)
    payload[key + "_sample"] = rows[:max(0, int(sample))]
    payload[key + "_count"] = n
    payload[key] = ("已省略 %d 行（MCP 体积保护，实测 4483 行≈435KB 会把工具卡死）：%s"
                    % (n, where or "要全量请传 with_rows=True，或读返回里的落盘路径"))
    return payload
