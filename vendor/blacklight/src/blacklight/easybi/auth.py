"""easybi 鉴权层：**主票 → OIDC 自动换域内会话**（2026-08-07 实证）。

easybi 的 API 未登录时 302 到 `ssa.jd.com/oidc/authorize`。但**不需要手写换码流程**——
`.jd.com` 上的主票（`sso.jd.com` / `ssa.ticket` / `ssa.global.ticket`）本来就在我们的 cookie 里，
只要用**带真 cookie jar 的 client** 打一次任意 API 并允许跟重定向，整条链会自动走完：

    GET  easybi.jd.com/api/common/auth/loginInfo
      302 → ssa.jd.com/oidc/authorize?...&response_type=code&redirect_uri=...
      302 → jdp-common.jd.com/api/common/auth/loginInfo
      200   {"code":200,"data":{"Pin":"wangruihan9","Nick":"汪瑞翰","roles":[...]}}

握手后 jar 里多出域内会话：`easybi.jd.com/ssa.jdp_web.state` + `jdp-common.jd.com/ssa.jdp_web`。
★**握手是一次性的，不是每次调用**：实证第 2、3 次调用直接 200、0 次重定向。

⚠️**必须用 cookie jar，不能用 cookie 字符串**：`core.auth.session_client()` 是把 cookie 拼成
  header 字符串注入的，接不住握手响应里的 `Set-Cookie` → 会每次都重新 302，且拿不到域内会话。
  这是接入时唯一需要改的地方。

⚠️ 「code 30 秒内必须消费、不能被别的调用抢先」这条平台约束客观存在，但它整个发生在
  **一次自动重定向链内部**（毫秒级），正常用不会碰到。若并发很高出现偶发 302，
  串行化握手即可（本模块用锁保证同进程只握手一次）。

不持久化任何 easybi 凭证：握手成本 ≈ 一次 HTTP 往返，进程重启重来即可。
"""
from __future__ import annotations

import threading

import httpx

from blacklight.core import BlacklightError
from blacklight.core import auth as jd_auth
from blacklight.core.base import DEFAULT_UA

BASE = "http://easybi.jd.com"
PATH_LOGIN_INFO = "/api/common/auth/loginInfo"

_LOCK = threading.Lock()
_CLIENT: httpx.Client | None = None
_WHO: dict | None = None


def _build() -> httpx.Client:
    """种入主票 → 建带 jar 的 client。"""
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
    if n == 0:
        raise BlacklightError("cookie 串解析出 0 条，格式不对")
    return httpx.Client(
        cookies=jar, timeout=60.0, follow_redirects=True,
        headers={"User-Agent": DEFAULT_UA,
                 "Accept": "application/json, text/plain, */*",
                 "Content-Type": "application/json",
                 "Origin": BASE, "Referer": BASE + "/"},
    )


def _handshake(c: httpx.Client) -> dict:
    """打一次 loginInfo 触发 OIDC 链；返回身份。失败抛错，不静默降级。"""
    r = c.get(BASE + PATH_LOGIN_INFO)
    if r.status_code // 100 == 3:
        raise BlacklightError("握手未完成：仍是 %s → %s（主票可能已过期，重登）"
                              % (r.status_code, r.headers.get("location", "")[:120]))
    try:
        j = r.json()
    except Exception:
        raise BlacklightError("握手返回非 JSON（HTTP %s，长度 %d）——多半是被登录页拦了"
                              % (r.status_code, len(r.text)))
    data = j.get("data") or {}
    if not data.get("Pin"):
        raise BlacklightError("握手返回里没有 Pin：%s" % str(j)[:200])
    return data


def client(force: bool = False) -> httpx.Client:
    """拿已握手的 client（进程内缓存）。"""
    global _CLIENT, _WHO
    with _LOCK:
        if _CLIENT is None or force:
            c = _build()
            _WHO = _handshake(c)
            _CLIENT = c
        return _CLIENT


def login_info(refresh: bool = False) -> dict:
    """当前 easybi 身份 {Pin, Nick, roles...}。"""
    global _WHO
    c = client(force=refresh)
    if _WHO is None or refresh:
        _WHO = _handshake(c)
    return _WHO


def reset() -> None:
    """丢弃缓存 client（换登录态/调试用）。"""
    global _CLIENT, _WHO
    with _LOCK:
        if _CLIENT is not None:
            try:
                _CLIENT.close()
            except Exception:
                pass
        _CLIENT, _WHO = None, None


def request(method: str, path: str, *, json_body=None, params=None, retry: bool = True):
    """带一次自动重握手的请求。返回 httpx.Response。

    掉登录态的表现是**又开始 302**（而不是 401），所以这里按重定向次数判断而不是状态码。
    """
    c = client()
    url = path if path.startswith("http") else BASE + path
    r = c.request(method, url, json=json_body, params=params)
    if retry and r.history and any(h.status_code // 100 == 3 for h in r.history):
        # 走过重定向说明域内会话没了，重握一次再打
        reset()
        c = client()
        r = c.request(method, url, json=json_body, params=params)
    return r
