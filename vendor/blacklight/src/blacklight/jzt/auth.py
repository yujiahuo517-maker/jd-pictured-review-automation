"""
jzt 鉴权层：**京准通（cxjzt.jd.com）免密登录**。

京准通不是独立登录 —— osw 采销账号管理页的「免密登录」实现是**一个 POST 接口种 cookie**，
不是带票跳转（2026-08-04 实证）：

    POST https://admin-atoms-api.jd.com/financecore/user/skippwd/cookie/set
    headers: siteId: 99999 / loginmode: 4      ← 是 **header** 不是 body
    body:    {"pin": "<广告账号名>", "relationType": 2}
    resp:    {"code":1,"subCode":1,"data":{"url":"https://cxjzt.jd.com"}}

返回的 url **不含任何 token/ticket**，登录态全靠响应 `Set-Cookie` 落到 `.jd.com`
（关键 cookie `skpp_p`/`skpp_s`）。所以只要有 **yx/ERP 登录态**（与 osw/yx 同一份 cookie），
就能纯脚本换到京准通身份 —— 不需要开浏览器、不需要解析跳转链。

因此本层不持久化任何 jzt 凭证，只在**进程内**缓存一个带完整 cookie jar 的 httpx.Client：
  - skippwd 是**幂等**的，掉登录态（`code=-5004`）重 POST 一次即可续期 → `post()` 内置一次自动续期重试；
  - 进程重启重来一次的成本 ≈ 200ms，不值得多存一份 secret 上盘。

credentials.json 里只存**账号名**（`jzt.pin`，非密），用于多账号切换。
"""
from __future__ import annotations

import json as _json
import os
import threading
from typing import Optional

from blacklight.core import BlacklightError
from blacklight.core import auth as jd_auth
from blacklight.core.base import DEFAULT_UA, REDIRECT_CODES, gateway, scene_cfg

ATOMS = "https://admin-atoms-api.jd.com"
PATH_SKIPPWD_SET = "/financecore/user/skippwd/cookie/set"
PATH_SKIPPWD_LIST = "/financeadmin/saleRelation/saleuser/skippwd/list"

# 采销账号管理接口的固定 header（siteId/loginmode 是 header，不是 body —— 放 body 会 -205）
_ATOMS_HEADERS = {"Content-Type": "application/json", "siteId": "99999", "loginmode": "4",
                  "Accept": "application/json, text/plain, */*", "User-Agent": DEFAULT_UA,
                  "Origin": "https://admin-ads.jd.com", "Referer": "https://admin-ads.jd.com/"}

# atoms 信封：code==1 成功 / -100 需重登 / -205 无权限
_ATOMS_CODE = {1: "成功", -100: "需重新登录", -205: "无权限"}

_LOCK = threading.Lock()
_SESSION = None          # (pin, httpx.Client)


# --------------------------------------------------------------------------- #
# 账号（credentials.json 只存名字，非密）
# --------------------------------------------------------------------------- #
def _cred() -> dict:
    p = jd_auth.cred_path()
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                d = _json.load(f)
                return d if isinstance(d, dict) else {}
        except Exception:
            pass
    return {}


def current_account() -> str:
    """当前京准通广告账号名。环境 `JZT_PIN` > credentials.json `jzt.pin` > config.json `jzt.default_pin`。"""
    env = os.environ.get("JZT_PIN", "").strip()
    if env:
        return env
    saved = ((_cred().get("jzt") or {}).get("pin") or "").strip()
    return saved or (scene_cfg("jzt").get("default_pin") or "").strip()


def set_account(pin: str) -> str:
    """切换/持久化京准通广告账号名（**只存名字，不存凭证**）。切账号会丢弃进程内 session。"""
    pin = (pin or "").strip()
    if not pin:
        raise BlacklightError("pin 不能为空。用 jzt_accounts 看可用账号名。")
    d = _cred()
    d.setdefault("jzt", {})["pin"] = pin
    with open(jd_auth.cred_path(), "w", encoding="utf-8") as f:
        _json.dump(d, f, ensure_ascii=False, indent=2)
    global _SESSION
    with _LOCK:
        _SESSION = None
    return pin


# --------------------------------------------------------------------------- #
# session：yx cookie jar → skippwd 种 skpp_* → 同 jar 直接打 cxjzt-api
# --------------------------------------------------------------------------- #
def _new_client():
    """用 yx/ERP 登录态的 cookie 建 jar（**不走 Cookie header**，否则与 Set-Cookie 落进来的 skpp_* 会双份）。"""
    try:
        import httpx
    except ImportError as e:
        raise BlacklightError("缺少依赖 httpx，请先 pip install httpx") from e
    ck = jd_auth.session_cookie()          # 无登录态会抛「先 yx_login」
    jar = httpx.Cookies()
    for part in ck.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            jar.set(k.strip(), v.strip(), domain=".jd.com", path="/")
    return httpx.Client(cookies=jar, timeout=60.0, verify=False, follow_redirects=False,
                        headers={"User-Agent": DEFAULT_UA})


def _atoms(client, path: str, body: dict) -> dict:
    r = client.post(f"{ATOMS}{path}", json=body, headers=_ATOMS_HEADERS)
    if r.status_code in REDIRECT_CODES:
        raise BlacklightError(f"{path} 被重定向（{r.status_code}）——yx 登录态失效，请运行 yx_login/osw_login 重登")
    try:
        j = r.json()
    except Exception as e:
        raise BlacklightError(f"{path} 未返回 JSON（HTTP {r.status_code}）——yx 登录态可能失效，请 yx_login") from e
    code = j.get("code")
    if code != 1:
        hint = _ATOMS_CODE.get(code, "")
        raise BlacklightError(f"{path}: code={code}{'(' + hint + ')' if hint else ''} {j.get('msg') or ''}".strip())
    return j


def accounts() -> dict:
    """枚举**当前 ERP 可免密登录的广告账号**（自投 / 代投 / 已驳回都在里面）。

    `approvalStatus` 观测：1=已通过(可登录) / 2=已驳回。`operatorErpStr` 是被授权代投的 ERP 列表。"""
    with _new_client() as c:
        j = _atoms(c, PATH_SKIPPWD_LIST, {})
    rows = ((j.get("data") or {}).get("data")) or []
    cur = current_account()
    return {"count": len(rows), "current": cur,
            "rows": [{"pin": x.get("pin"), "归属ERP": x.get("ownerErp"),
                      "审批状态": {1: "已通过", 2: "已驳回"}.get(x.get("approvalStatus"), x.get("approvalStatus")),
                      "可登录": x.get("approvalStatus") == 1,
                      "代投ERP": x.get("operatorErpStr") or None,
                      "traceErp": x.get("traceErp"),
                      "当前": x.get("pin") == cur} for x in rows]}


def _skippwd(client, pin: str) -> dict:
    """真正种 cookie 的那一下。幂等，可重复调用（掉线续期就是再调一次）。"""
    j = _atoms(client, PATH_SKIPPWD_SET, {"pin": pin, "relationType": 2})
    return {"pin": pin, "url": (j.get("data") or {}).get("url"), "subCode": j.get("subCode")}


def session(pin: Optional[str] = None, force: bool = False):
    """取带京准通登录态的 httpx.Client（进程内缓存；换 pin 或 force 时重建并重新免密登录）。"""
    global _SESSION
    pin = (pin or current_account()).strip()
    if not pin:
        raise BlacklightError(
            "未设置京准通广告账号：先用 jzt_accounts 看可用账号，再 jzt_set_account('<账号名>')。")
    with _LOCK:
        if _SESSION and _SESSION[0] == pin and not force:
            return _SESSION[1]
        if _SESSION:
            try:
                _SESSION[1].close()
            except Exception:
                pass
        c = _new_client()
        _skippwd(c, pin)
        _SESSION = (pin, c)
        return c


def login(pin: Optional[str] = None) -> dict:
    """显式做一次免密登录（幂等）。掉登录态时不必手动调 —— `post()` 会自动续期重试一次。"""
    pin = (pin or current_account()).strip()
    c = session(pin, force=True)
    return {"logged_in": True, "pin": pin, "note": "skpp_p/skpp_s 已种到进程内 session（不落盘）"}


# --------------------------------------------------------------------------- #
# 业务请求：cxjzt-api 信封 {success, code, msg, data}
# --------------------------------------------------------------------------- #
def _jzt_headers() -> dict:
    return {"Content-Type": "application/json", "Accept": "application/json, text/plain, */*",
            "Origin": "https://cxjzt.jd.com", "Referer": "https://cxjzt.jd.com/swa/index.html"}


def _looks_logged_out(resp, j) -> bool:
    """京准通掉登录态的表现：302、非 JSON、或 body 里带 -5004（页面会跳 gw/index?code=-5004）。"""
    if resp.status_code in REDIRECT_CODES:
        return True
    if j is None:
        return True
    return str(j.get("code")) == "-5004" or "-5004" in str(j.get("msg") or "")


def post(path: str, body: dict, pin: Optional[str] = None, _retried: bool = False,
         envelope: str = "success") -> dict:
    """POST cxjzt-api → 校验成功标志 → 返回 `data`。

    ⚠️与 core.json_post 的区别：**掉登录态自动重做一次免密登录再重试**（京准通 session 短，
    重 POST 一次 skippwd 即续期，不需要重新浏览器登录）。

    **envelope**：cxjzt-api 下不同子系统的成功信封不一样，别混用——
    - `"success"`（默认，/swa/* 等）：靠布尔字段 `success`；
    - `"code1"`（/financecore/* 等）：**没有 `success` 字段**，靠 `code == 1`，`msg` 恒为「成功」。
      用默认信封打 financecore 会把 `msg` 当报错抛出「成功」这种荒谬错误（2026-08-05 实证）。"""
    c = session(pin)
    r = c.post(f"{gateway('jzt')}{path}", json=body, headers=_jzt_headers())
    try:
        j = r.json()
    except Exception:
        j = None
    if _looks_logged_out(r, j):
        if _retried:
            raise BlacklightError(
                f"{path}: 京准通登录态失效且免密续期无效——先确认 yx 登录态（yx_login_status），"
                f"再确认账号 {pin or current_account()} 的授权未被驳回（jzt_accounts）。")
        session(pin, force=True)                       # 续期一次
        return post(path, body, pin, _retried=True, envelope=envelope)
    r.raise_for_status()
    ok = (str(j.get("code")) == "1") if envelope == "code1" else bool(j.get("success"))
    if not ok:
        raise BlacklightError(f"{path}: {j.get('msg') or j.get('code')}")
    return j.get("data") or {}


def status() -> dict:
    """京准通登录态体检：yx 底座是否可用 + 当前账号 + 打一次真接口探活。"""
    yx = jd_auth.status_dict()
    pin = current_account()
    out = {"pin": pin, "yx_usable": yx.get("usable"), "yx_note": yx.get("note")}
    if not yx.get("usable"):
        out.update(usable=False, note="yx/ERP 登录态不可用 —— 京准通免密登录依赖它，请先 yx_login。")
        return out
    if not pin:
        out.update(usable=False, note="未设置广告账号 —— 先 jzt_accounts 看可用账号，再 jzt_set_account。")
        return out
    try:
        from blacklight.jzt import swa
        swa.ad_list(page=1, page_size=1)
        out.update(usable=True, note="登录态有效（已打通 cxjzt-api）")
    except BlacklightError as e:
        out.update(usable=False, note=f"探活失败：{e}")
    return out
