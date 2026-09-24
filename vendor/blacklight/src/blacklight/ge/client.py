# -*- coding: utf-8 -*-
"""ge（黄金眼）**共享 HTTP 客户端**工厂 —— 三个模块曾各写一份，几乎逐字相同。

收敛前：`ge/margin.shared_client()`、`ge/couponbatch._client()`、`ge/ssm._client()` 三份实现
差异只有 header 里的 `RequestUrl` / `menuId` / `resAppKey` 和一个无意义的 UA 版本号（150 vs 151）。
cookie 串解析、90s 超时、单例缓存、错误文案全部重复。

★**语义提醒（这个坑本包踩过）**：ge 的 client 是**共享单例**，
  **绝不要 `with ge_client(...)`** —— with 退出会把它关掉，后续所有 ge 调用报
  `Cannot send a request, as the client has been closed`（2026-08-21）。
  而 `osw/margin._client()` / `yx/bybt._client()` 是**每次新建**，那两个用 with 才是对的。
  同名不同义正是当初改名 `shared_client` 的原因。
"""
from __future__ import annotations

import httpx

from blacklight.core import BlacklightError
from blacklight.core import auth as jd_auth

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

_CACHE: dict = {}


def ge_client(page_url: str, menu_id: str, res_app_key: str = None,
              timeout: float = 90.0, ua: str = UA) -> httpx.Client:
    """按 (page_url, menu_id, resAppKey) 复用一个 ge 客户端。

    `ua` 可覆盖：收敛时各模块原本的 UA 版本号不同（150/151），保留参数以便逐字节保持原行为。
    """
    key = (page_url, menu_id, res_app_key, round(float(timeout), 3), ua)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    ck = jd_auth.session_cookie()
    if not ck:
        raise BlacklightError("没有登录态 cookie，先跑 python -m blacklight.core.login")
    jar = httpx.Cookies()
    n = 0
    for part in ck.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            jar.set(k.strip(), v.strip(), domain=".jd.com")
            n += 1
    if not n:
        raise BlacklightError("cookie 串解析出 0 条，格式不对")
    headers = {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "LoginErp": jd_auth.current_pin(),
        "Origin": "http://ge.jd.com",
        "Referer": "http://ge.jd.com/",
        "RequestUrl": page_url,
        "X-Requested-With": "XMLHttpRequest",
        "menuId": menu_id,
        "User-Agent": ua,
    }
    if res_app_key:
        headers["resAppKey"] = res_app_key
    cli = httpx.Client(cookies=jar, timeout=float(timeout), follow_redirects=True,
                       headers=headers)
    _CACHE[key] = cli
    return cli
