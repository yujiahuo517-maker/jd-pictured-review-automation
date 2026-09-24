"""通用件：**落盘缓存 / 账号级文件锁 / 批量写节奏**。

2026-08-21 沉淀。三个都不是新想法，问题是**各模块各写各的、写一次错一次**：

1. `disk_cache` —— 本包的实际用法是「每次起一个新 Python 进程跑脚本」，
   **模块级 dict 缓存一退进程就没**，30 分钟 TTL 形同虚设。
   实测同一天为 `sku_black_scan` 付了两次 6 分钟冷扫、为秒杀门槛导出多次等 9 分钟。
   ⇒ 缓存一律落盘，并**把来源标出来**（`_cache_from`），别让缓存把实验骗了。

2. `account_lock` —— 平台限流是**账号级**的，开几个进程都没用，只会互相拖垮：
   实测并行扫黑名单 ⇒ 94 个单元只过 84 个、覆盖率 89% 被闸拦下。
   ⇒ 抢同一份额度的操作走同一把文件锁，第二个进程**等**而不是一起挂。

3. `paced` —— 限流看的是**窗口内总调用数，不是并发数**：串行处理 20+ 个单元、
   每单元 3 次调用照样打满 1 分钟窗口（9 个单元被拒）。
   ⇒ 批量写显式留间隔，别指望"串行就安全"。
"""
from __future__ import annotations

import functools
import hashlib
import io
import json
import os
import time
from typing import Callable, Optional

from blacklight.core import paths


def _cache_dir() -> str:
    d = os.path.join(paths.home(), "cache")
    os.makedirs(d, exist_ok=True)
    return d


def cache_key(*parts) -> str:
    """把任意入参压成稳定短键。"""
    raw = "|".join(json.dumps(p, ensure_ascii=False, sort_keys=True, default=str)
                   for p in parts)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def disk_get(name: str, key: str, ttl_s: float) -> Optional[dict]:
    """读落盘缓存；过期/不存在/损坏都返回 None（**不抛**，缓存不该影响主流程）。"""
    fp = os.path.join(_cache_dir(), "%s_%s.json" % (name, key))
    try:
        age = time.time() - os.path.getmtime(fp)
        if age >= ttl_s:
            return None
        with io.open(fp, encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict):
            d["_cache_from"] = "disk"
            d["_cached_age_s"] = round(age, 1)
        return d
    except Exception:
        return None


def disk_put(name: str, key: str, value) -> None:
    """写落盘缓存；失败静默（落盘失败不该让主流程挂掉）。"""
    fp = os.path.join(_cache_dir(), "%s_%s.json" % (name, key))
    try:
        with io.open(fp, "w", encoding="utf-8") as fh:
            json.dump(value, fh, ensure_ascii=False, default=list)
    except Exception:
        pass


def disk_cached(name: str, ttl_s: float, key_of: Callable = None):
    """装饰器：结果落盘缓存 ttl_s 秒，**跨进程复用**。

    · 被装饰函数需接受 `refresh: bool` 关键字（或忽略它）——传 `refresh=True` 强制重算。
    · 命中时结果里带 `_cache_from='disk'` + `_cached_age_s`。
    · **只缓存成功结果**：函数抛错时不写缓存（残缺结果被缓存比慢更危险）。
    ⚠️刚做过写操作要 `refresh=True`，否则会读到旧状态。
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrap(*a, **kw):
            refresh = bool(kw.get("refresh"))
            k = cache_key(key_of(*a, **kw) if key_of else (a, sorted(kw.items())))
            if not refresh and ttl_s > 0:
                hit = disk_get(name, k, ttl_s)
                if hit is not None:
                    return hit
            out = fn(*a, **kw)
            if ttl_s > 0:
                disk_put(name, k, out)
            return out
        return wrap
    return deco


# ---------------- 账号级文件锁 ----------------
class account_lock:
    """跨进程互斥：同一 `name` 同时只允许一个进程跑（用于抢同一份限流额度的操作）。

    用法：`with account_lock("swa.skublack"): ...`
    · `timeout_s` 内拿不到锁 → 抛（**别偷偷并发跑**，那正是要防的事）
    · 锁文件里写 pid + 时间戳；**陈旧锁**（超过 `stale_s`）自动接管，防进程崩了锁不释放
    """

    def __init__(self, name: str, timeout_s: float = 1800.0,
                 poll_s: float = 3.0, stale_s: float = 3600.0):
        self.fp = os.path.join(_cache_dir(), "lock_%s.json" % name.replace(".", "_"))
        self.name, self.timeout_s, self.poll_s, self.stale_s = name, timeout_s, poll_s, stale_s
        self._held = False

    def _stale(self) -> bool:
        try:
            return (time.time() - os.path.getmtime(self.fp)) > self.stale_s
        except Exception:
            return True

    def __enter__(self):
        from blacklight.core.base import BlacklightError
        deadline = time.time() + self.timeout_s
        while True:
            try:
                fd = os.open(self.fp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w") as fh:
                    fh.write(json.dumps({"pid": os.getpid(), "ts": time.time(),
                                         "name": self.name}))
                self._held = True
                return self
            except FileExistsError:
                if self._stale():
                    try:
                        os.remove(self.fp)      # 陈旧锁，接管
                        continue
                    except Exception:
                        pass
                if time.time() >= deadline:
                    raise BlacklightError(
                        "拿不到账号级锁 `%s`（另一个进程正在跑，等了 %.0fs）。"
                        "**别并发跑**——限流是账号级的，一起跑只会双双降级。"
                        % (self.name, self.timeout_s))
                time.sleep(self.poll_s)

    def __exit__(self, *exc):
        if self._held:
            try:
                os.remove(self.fp)
            except Exception:
                pass
        return False


# ---------------- 批量写节奏 ----------------
_PACED_SEQ = 0


def paced(items, fn, spacing: float = 20.0, on_error: str = "collect"):
    """批量写：**每项之间显式留 `spacing` 秒**，返回 {ok:[...], fail:[...]}。

    限流看窗口内总调用数——串行不等于安全（实测 20+ 单元连着跑，9 个被拒）。
    `on_error='collect'` 收集失败继续跑（默认）；`'raise'` 立刻抛。
    """
    from blacklight.core import policy as _policy
    ok, fail = [], []
    global _PACED_SEQ
    _PACED_SEQ += 1
    # ★key 必须**每次调用都全新**：用 id(fn) 不行 —— lambda 被回收后 id 会被复用，
    #   于是上一次 paced() 留下的时间戳会让这一次的**第一项也睡一觉**
    #   （实测 1 项 spacing=0.5 睡了 0.5s、4 项多睡一拍）。计数器最省事且无歧义。
    key = "reuse.paced:%d" % _PACED_SEQ
    for i, it in enumerate(items):
        # ★**每一项都调 pace（含第 0 项）**：pace 首次遇到某个 key 只登记时间、不睡，
        #   写成 `if i: pace(...)` 会**少睡一个间隔**（3 项 spacing=0.2 实测只等 0.2s 而非 0.4s）。
        #   —— 收敛公共件时最容易出的就是这种"看着等价其实差一拍"的回归，故留此注。
        _policy.pace(key, spacing)           # 计时统一走 policy.pace，别在这儿再实现一遍
        try:
            ok.append({"item": it, "result": fn(it)})
        except Exception as e:
            rec = {"item": it, "err": "%s: %s" % (type(e).__name__, str(e)[:160])}
            if on_error == "raise":
                raise
            fail.append(rec)
    return {"ok": ok, "fail": fail, "成功": len(ok), "失败": len(fail),
            "间隔秒": spacing}
