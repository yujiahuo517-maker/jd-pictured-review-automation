"""统一截断闸 —— 分页/上限一处收敛，宁可报错不静默少算。

## 今天实撞的 3 处截断（都不会报错，都会让结果偏小）
| 位置 | 表现 |
|---|---|
| ge 券批次页表格 | **只渲染 50 行**，页头小字写着「仅展示50条」，但**分页器只显示 1 页** |
| easybi 数据集 | 288 款 × 15 日按批次分组 = 1181 行 > 单页 1000，取到 1000 就停 |
| ge `sku_breakdown` | `pageSize` 硬上限 2000；一张券可覆盖 >2000 个 SKU（实测 2235） |

三者的共同点：**返回结构完全正常、毫无迹象**。所以判据只能是
「**取到的行数 == 上限**」⇒ 一律当作被截断处理。

★宁可误报（正好整数行）也不能漏报：误报的代价是多翻一页，漏报的代价是结论错。
"""
from __future__ import annotations

from blacklight.core import BlacklightError


class TruncationError(BlacklightError):
    """取数被截断（或疑似被截断）。"""


def check_truncation(n_rows: int, page_size: int, what: str = "查询", hint: str = None) -> None:
    """**行数撞上限**就抛。`hint` 给领域补救建议（各模块的切片维度不一样）。"""
    if n_rows >= page_size:
        raise TruncationError(
            "%s 取到 %d 行 == 上限 %d，**按被截断处理**。%s"
            "（返回结构正常不代表没被截断——2026-08 实撞 3 处静默截断）"
            % (what, n_rows, page_size,
               hint or "调大 page_size、或按 SKU/批次切片、或缩短时间窗。"))


def check_total(n_rows: int, total, what: str = "查询", hint: str = None) -> None:
    """**取回行数 < 服务端 total** 就抛 —— 另一种截断形态，`check_truncation` 抓不到。

    ★2026-08-24 从 `easybi/dataset.py` 收编：那边实测 `dt × item_sku_id` 三天 total=386,299，
      page_size=20000 只取回 2 万，求和得 4,747 而真值 103,699（**只有 4.6%**），
      看着像"数据大跌"。服务端照常回 200，结构也正常 —— 只有对 total 才看得出来。
    """
    if total is None:
        return
    try:
        t = int(total or 0)
    except (TypeError, ValueError):
        return
    if n_rows < t:
        raise TruncationError(
            "%s 取回 %d 行但 total=%s —— **被分页截断，会静默少算**（求和结果不可用）。%s"
            % (what, n_rows, total,
               hint or "换低基数维度先定位再下钻；或加过滤缩小范围；或分页取后自行合并。"))


def fetch_paged(fetch, page_size: int, *, key=None, max_pages: int = 50,
                what: str = "查询") -> list:
    """按页取全。

    fetch(page, page_size) -> list[row]
    key(row) -> 去重键；给了就按它去重（服务端重复吐同一页时能发现）

    停止条件：某页不满 page_size，**或**去重后没有新行。
    后者是防死循环——easybi 实测有「翻页 58264 行里只有 38541 个不同 SKU」的重复吐页行为。
    """
    out, seen, page = [], set(), 1
    while True:
        rows = fetch(page, page_size) or []
        if key is None:
            fresh = rows
        else:
            fresh = []
            for r in rows:
                k = key(r)
                if k not in seen:
                    seen.add(k)
                    fresh.append(r)
        out += fresh
        if len(rows) < page_size:
            break
        if not fresh:
            # 服务端在重复吐同一页 —— 有多少算多少，但必须让调用方知道
            raise TruncationError(
                "%s 第 %d 页去重后无新行（服务端疑似重复吐页），已取 %d 行，"
                "结果可能不完整。改用切片取数。" % (what, page, len(out)))
        page += 1
        if page > max_pages:
            raise TruncationError(
                "%s 翻页超过 %d 页（已 %d 行），怀疑分页异常，中止以免拿到半截数据"
                % (what, max_pages, len(out)))
    return out
