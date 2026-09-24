# -*- coding: utf-8 -*-
"""**代码新鲜度探针** —— 回答「现在跑着的这个进程，用的是不是磁盘上最新的代码」。

2026-08-24 的直接起因：给 MCP 工具补了参数后从 MCP 调过去**没报未知参数、还照常打到了平台**，
我差点据此宣布"验证通过"。真相是 MCP server 进程 16:48 起的、模块 17:12 才改，
**FastMCP 把不认识的 kwargs 静默丢掉了** ⇒ 等于没传。
「调用没报错」证明不了跑的是新代码；能证明的只有**进程启动时间 vs 文件 mtime**。
"""
from __future__ import annotations

import os
import sys
import time


def _proc_start_ts() -> float | None:
    """当前进程启动时间（epoch 秒）。psutil 缺失时退回 None（不猜）。"""
    try:
        import psutil
        return psutil.Process(os.getpid()).create_time()
    except Exception:
        pass
    try:                       # Windows 无 psutil 时的兜底：进程启动后写的第一个 pyc 不可靠，
        import ctypes          # 直接问内核。
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        creation = wintypes.FILETIME()
        rest = (wintypes.FILETIME * 3)()
        if not k32.GetProcessTimes(k32.GetCurrentProcess(), ctypes.byref(creation), *[ctypes.byref(x) for x in rest]):
            return None
        ft = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return ft / 1e7 - 11644473600.0        # FILETIME(1601) → epoch(1970)
    except Exception:
        return None


def code_freshness(packages=("blacklight",)) -> dict:
    """比对**进程启动时间**与已加载模块的**文件 mtime**，列出「进程起来之后才改过」的模块。

    返回 {ok, 进程启动, 最新改动, 陈旧模块:[{module, mtime}], note}。
    `ok=False` ⇒ **跑的是旧代码**，MCP 要 `/mcp` 重连、CLI 要重启，否则你验证的不是你改的东西。
    拿不到进程启动时间时返回 `ok=None`（不猜，别把"没测出来"报成"没问题"）。
    """
    start = _proc_start_ts()
    stale, newest = [], None
    for name, mod in list(sys.modules.items()):
        if not any(name == p or name.startswith(p + ".") for p in packages):
            continue
        f = getattr(mod, "__file__", None)
        if not f or not f.endswith(".py") or not os.path.isfile(f):
            continue
        try:
            mt = os.path.getmtime(f)
        except OSError:
            continue
        if newest is None or mt > newest[1]:
            newest = (name, mt)
        if start is not None and mt > start:
            stale.append({"module": name, "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mt))})
    fmt = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else None
    return {"ok": (None if start is None else not stale),
            "进程启动": fmt(start),
            "最新改动": ({"module": newest[0], "mtime": fmt(newest[1])} if newest else None),
            "陈旧模块": stale[:20], "陈旧数": len(stale),
            "note": ("拿不到进程启动时间，无法判定（不猜）" if start is None else
                     ("跑的是磁盘最新代码" if not stale else
                      "**进程里是旧代码**：%d 个模块在进程启动后被改过 ⇒ MCP 请 `/mcp` 重连、"
                      "CLI 请重启，否则新参数会被静默丢弃、新逻辑根本没在跑" % len(stale)))}
