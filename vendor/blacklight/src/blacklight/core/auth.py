"""
yx-mcp 鉴权层：管理 yx.jd.com / mcpman.jd.com 的登录态。

⚠️★**2026-08-05 修正：下面这段旧结论已被证伪，别再据它维护两套登录。**

旧结论（现已作废）：「mcpman 走 ERP/"focus" realm（`pin/pinId/sdtoken/focus-*`），不是 jx-auth 的
`sso.jd.com/ssa.bdp` realm；dp cookie 调 mcpman 会 code=30000，所以 yx 必须自己登一次。」

**实测事实**：
1. 本模块**实际存下来的 cookie 里根本没有** `pin/pinId/sdtoken/focus-*`，全是 SSO realm 的
   `sso.jd.com / ssa.global.ticket / ssa.ticket` —— 且它工作正常。旧描述与自己的产物就对不上。
2. 拿 **jx-auth 的 dp cookie** 直接打 mcpman 探针：`success=True, code=00000`（不是 30000）。
3. 再用它跑遍 blacklight 全部 6 个网关：**sff 商品 / api.m 毛利 / oac 秒杀 / bid-activity 百补 /
   admin-atoms 免密登录 / cxjzt 全站营销 —— 全部通过**。

**真实模型**：一份 SSO 主票（`sso.jd.com` + `ssa.global.ticket`）+ 各子系统各自换发的票
（yx 是 `ssa.ticket`、BDP 是 `ssa.bdp`）。主票同源，所以两边可互换。

**因此 blacklight 与 jx-auth 维护两套浏览器登录是冗余的**（用户 2026-08-05 指出）。
在统一之前，注意 jx-auth 的 jar 里**多一个 `erp` cookie**，而本模块的没有 ——
这正是 `login.py` 自动识别 ERP 常失败的原因。

凭证来源优先级：环境变量 YX_COOKIE > yx-mcp/credentials.json 的 {"yx":{"cookie":...}}。

CLI:
  python jd_auth.py --status     查看登录态并在线探活
  python jd_auth.py --login      失效时用 DrissionPage 登录 yx.jd.com 并保存 cookie
"""
from __future__ import annotations

import json
import os
import sys
import time

def _fmt(sec):
    """人类可读剩余时长（原借 jx-auth.format_remaining，已内联解耦，不再依赖 sys.path hack）。"""
    if sec is None:
        return "未知"
    if sec <= 0:
        return "已过期"
    h = int(sec) // 3600
    m = (int(sec) % 3600) // 60
    return f"约 {h} 小时 {m} 分钟" if h else f"约 {m} 分钟"

MCPMAN = "https://mcpman.jd.com"
# 关键 cookie 是**会话型**（无显式过期），抓不到真到期→只能估算。实测真实登录态能撑数天，
# 故估算默认放到 72h（原 12h 太保守、显示"已过期"吓人）；可用 YX_ASSUMED_TTL_HOURS 调。
# 注意：估算只影响显示 + 是否触发探活；**真过期由操作时的接口报错兜底**（json_post 已识别登录失效），故放长很安全。
try:
    ASSUMED_TTL_SECONDS = int(float(os.environ.get("YX_ASSUMED_TTL_HOURS", "72")) * 3600)
except (TypeError, ValueError):
    ASSUMED_TTL_SECONDS = 72 * 3600
PROBE_CAMPAIGN_ID = os.environ.get("YX_PROBE_CAMPAIGN_ID", "2497067")
PROBE_URL = f"{MCPMAN}/campaign/getCampaignApplyDetailById"


def _here() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def cred_path() -> str:
    from blacklight.core import paths
    return paths.credentials_path()


def _load() -> dict:
    p = cred_path()
    if os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
                return d if isinstance(d, dict) else {}
        except Exception:
            pass
    return {}


def save_cookie(cookie: str, expires_at: float | None = None, expiry_source: str = "assumed") -> None:
    cookie = (cookie or "").strip()
    now = time.time()
    if not expires_at or expires_at <= now:
        expires_at, expiry_source = now + ASSUMED_TTL_SECONDS, "assumed"
    d = _load()
    prev = d.get("yx") or {}
    d["yx"] = {"cookie": cookie, "saved_at": now, "expires_at": expires_at, "expiry_source": expiry_source}
    if prev.get("pin"):                     # 保留已配置的操作人 PIN（别被登录覆盖掉）
        d["yx"]["pin"] = prev["pin"]
    with open(cred_path(), "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def set_pin(pin: str) -> str:
    """设置/持久化当前操作人 ERP/PIN 到 credentials.json（多用户：每人设一次）。返回设置后的 pin。"""
    pin = (pin or "").strip()
    d = _load()
    d.setdefault("yx", {})["pin"] = pin
    with open(cred_path(), "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    return pin


# --------------------------------------------------------------------------- #
# 兜底登录源：jx-auth（同一 SSO 主票）
# 2026-08-05 实证：jx-auth 的 dp cookie 可直接打通 blacklight 全部 6 个网关
# （sff / api.m / oac / bid-activity / admin-atoms / cxjzt）。见本模块顶部说明。
# 策略是**兜底不是替代**：自己的凭证好用就用自己的；失效了先借 jx-auth；都不行才弹浏览器。
# --------------------------------------------------------------------------- #
def _jx_auth_cookie() -> str:
    """读 jx-auth 的 dp cookie。文件不在/格式不对 → 返回 ''（兜底路径，绝不因它报错）。"""
    try:
        from blacklight.core import paths
        p = paths.jx_auth_credentials_path()
        if not os.path.isfile(p):
            return ""
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        return ((d.get("dp") or {}).get("cookie") or "").strip()
    except Exception:
        return ""


def _erp_from_cookie(cookie: str) -> str:
    """从 cookie 串里抠 `erp` 值。jx-auth 的 jar 有这个 cookie，blacklight 自己登的没有——
    这正是 login.py 自动识别 ERP 常失败的原因，借 jx-auth 时顺带把它捡回来。"""
    for part in (cookie or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == "erp" and v:
            return v.strip()
    return ""


def get_cookie() -> str:
    """yx cookie：环境变量 `YX_COOKIE` > 自己的 credentials.json > **jx-auth 兜底**。

    只在自己那份**为空**时才兜底（这里不做探活，避免每次取 cookie 都打网络）；
    自己那份**过期**的情况由 `ensure_session()` 处理——它会探活并在必要时改用 jx-auth 的。"""
    env = os.environ.get("YX_COOKIE", "").strip()
    if env:
        return env
    own = (_load().get("yx") or {}).get("cookie", "").strip()
    return own or _jx_auth_cookie()


def session_cookie() -> str:
    """登录态 cookie 字符串（YX_COOKIE 环境 > 缓存）；无则抛。bybt/ms 等 openness 场域共用。"""
    ck = get_cookie()
    if not ck:
        from blacklight.core import BlacklightError
        raise BlacklightError("无登录态：先 yx_login")
    return ck


def session_client(timeout: float = 20.0, **kw):
    """带登录态 cookie 的 make_client（默认 yx.jd.com origin，kw 透传 make_client）。
    放这里而非 jd_core：取 cookie 依赖 jd_auth，jd_core 不反向依赖 jd_auth（避免循环 import）。"""
    from blacklight.core import make_client
    return make_client(session_cookie(), timeout=timeout, **kw)


def current_pin() -> str:
    """当前操作人 ERP/PIN。来源：环境 `YX_PIN` > credentials.json `yx.pin` > ''。
    （yx cookie 走 ERP/SSO realm 无明文 pin；**登录时由 yx_login 自动读 yx.jd.com 的 localStorage['erp'] 存入** →
    正常无需手动设。取不到才手动 YX_PIN / yx_set_pin。）之后 eligible 查询默认用它、审计记录操作人。"""
    env = os.environ.get("YX_PIN", "").strip()
    if env:
        return env
    saved = (_load().get("yx") or {}).get("pin", "").strip()
    # 兜底：jx-auth 的 cookie jar 里带 `erp`，而自己登出来的不带 —— 取不到时从那边捡
    return saved or _erp_from_cookie(_jx_auth_cookie())


def status_meta() -> dict:
    yx = _load().get("yx") or {}
    cookie = get_cookie()
    present = bool(cookie)
    exp = yx.get("expires_at")
    if yx.get("cookie", "").strip() != cookie:  # 走了 env 覆盖，元数据不可信
        exp = None
    remaining = (exp - time.time()) if (present and exp) else None
    return {"present": present, "pin": current_pin(), "expires_at": exp,  # pin 独立于登录态(可先设后登)
            "remaining": remaining, "expired": bool(remaining is not None and remaining <= 0),
            "expiry_source": yx.get("expiry_source")}


def _headers(cookie: str) -> dict:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://yx.jd.com",
        "Referer": "https://yx.jd.com/",
        "Cookie": cookie,
    }


def yx_is_logged_in(timeout: float = 10.0, cookie: str = None):
    """
    实测 mcpman 是否已授权：
        True  = 已登录（探针接口 success==True）
        False = 未登录/失效（无 cookie / 302 / 非JSON / success!=True，如 code=30000）
        None  = 无法判定（无 httpx / 网络异常）

    `cookie` 显式传入则探这一份（不读缓存）——用来在**采纳前**先验证候选登录态是否可用。
    """
    cookie = cookie if cookie is not None else get_cookie()
    if not cookie:
        return False
    try:
        import httpx
    except ImportError:
        return None
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, verify=False) as c:
            r = c.post(PROBE_URL, json={"campaignId": PROBE_CAMPAIGN_ID}, headers=_headers(cookie))
        if r.status_code in (301, 302, 303, 307, 308):
            return False
        try:
            j = r.json()
        except Exception:
            return False
        return bool(isinstance(j, dict) and j.get("success") is True)
    except Exception:
        return None


def run_login() -> int:
    """用 DrissionPage 登录 yx.jd.com 并保存 cookie（见 login.py）。返回退出码。"""
    import subprocess
    script = os.path.join(_here(), "login.py")
    if not os.path.isfile(script):
        print("错误: 找不到 login.py", file=sys.stderr)
        return 1
    return subprocess.run([sys.executable, script], cwd=_here()).returncode


def _try_adopt_jx_auth() -> bool:
    """自己的登录态不行时，**借 jx-auth 的**（同一 SSO 主票）。**探活通过才采纳**并落盘。

    采纳=存进自己的 credentials.json，于是下游一切照旧、不必感知这件事。
    顺带把 jx-auth jar 里的 `erp` 捡成 pin（自己登出来的 jar 没有这个 cookie，
    这正是 login.py 自动识别 ERP 常失败的原因）。
    返回是否采纳成功。**任何异常都吞掉**——兜底路径不该把主流程带崩。"""
    try:
        ck = _jx_auth_cookie()
        if not ck:
            return False
        if yx_is_logged_in(cookie=ck) is not True:     # 先验证，别把坏的存进来
            return False
        save_cookie(ck)
        erp = _erp_from_cookie(ck)
        if erp and not current_pin():
            set_pin(erp)
            print(f"顺带从 jx-auth 取到 ERP：{erp}", file=sys.stderr)
        print("已改用 jx-auth 的登录态（同一 SSO 主票，探活通过）——省掉一次浏览器登录。",
              file=sys.stderr)
        return True
    except Exception:
        return False


def ensure_session(auto_relogin: bool = True) -> str:
    """返回可用 cookie。**弹浏览器是最后手段**，顺序：

        自己的凭证（探活通过）→ 借 jx-auth 的（探活通过就采纳落盘）→ 弹浏览器登录

    中间那步是 2026-08-05 加的：两个 skill 本来在给同一个 SSO realm 各维护一套登录，
    实证 jx-auth 的 cookie 能打通全部 6 个网关，所以它没过期时根本不必再登一次。"""
    st = status_meta()
    # ⚠️`expired=False` 只在**有可信到期元数据**时才算数。借来的/env 覆盖的 cookie 没有元数据，
    #   此时 expired 恒为 False——若据此短路，jx-auth 那份也死了就会返回一个死 cookie 且永不重登。
    trusted = st.get("expires_at") is not None
    if st["present"] and trusted and not st["expired"]:
        return get_cookie()
    if not auto_relogin:
        return get_cookie()
    if st["present"] and not trusted:                  # 元数据不可信 → 必须探活
        if yx_is_logged_in() is not False:
            return get_cookie()
        print("当前 cookie（无到期元数据）探活失败。", file=sys.stderr)

    if st["present"] and trusted:                      # 有但估算过期 → 先探活，别急着重登
        if yx_is_logged_in() is not False:
            print("yx 登录态估算到期但实测仍有效，继续。", file=sys.stderr)
            return get_cookie()
        print("yx 登录态已失效。", file=sys.stderr)
    else:
        print("未检测到 yx 登录态。", file=sys.stderr)

    if _try_adopt_jx_auth():                           # ← 先借，别急着弹浏览器
        return get_cookie()
    print("jx-auth 也不可用，正在启动浏览器登录 yx.jd.com…", file=sys.stderr)
    run_login()
    return get_cookie()


def status_dict() -> dict:
    st = status_meta()
    live = yx_is_logged_in()   # 真相：在线探活（None=探不了）
    # usable 以**探活为准**：探活过=可用（哪怕估算显示"已过期"）；探活失败=不可用；探不了则退回估算
    usable = bool(live) if live is not None else (st["present"] and not st["expired"])
    own = ((_load().get("yx") or {}).get("cookie") or "").strip()
    src = ("环境变量 YX_COOKIE" if os.environ.get("YX_COOKIE", "").strip()
           else "自己的 credentials.json" if own
           else "jx-auth 兜底" if _jx_auth_cookie() else "无")
    return {"usable": usable, "cookie_present": st["present"], "pin": st.get("pin", ""),
            "mcpman_live": live,   # 登录态真伪的权威信号
            "cookie_source": src,  # 当前这份 cookie 是哪来的
            "jx_auth_可用": bool(_jx_auth_cookie()),   # 失效时能否免浏览器兜底
            "note": ("登录态有效" if usable else
                     "登录态失效；jx-auth 有凭证，yx_login 会先尝试借用、借不到才弹浏览器"
                     if _jx_auth_cookie() else "登录态失效，请运行 yx_login 重登"),
            "remaining_est": _fmt(st["remaining"]), "expiry_source": st.get("expiry_source"),
            "expired_by_est": st["expired"]}   # 仅估算(会话cookie无真到期)，别据此重登——看 usable


def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="yx-mcp 鉴权（yx/ERP realm）")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.login:
        ensure_session(auto_relogin=True)
    snap = status_dict()
    if a.json:
        print(json.dumps(snap, ensure_ascii=False, indent=2))
    else:
        live = {True: "已授权", False: "未授权/失效", None: "无法判定"}[snap["mcpman_live"]]
        print(f"yx cookie: {'有' if snap['cookie_present'] else '无'} | "
              f"可用: {'是' if snap['usable'] else '否'}({snap['note']}) | "
              f"估算剩余: {snap['remaining_est']} | mcpman 探活: {live}")
    return 0 if snap["mcpman_live"] is True else 1


if __name__ == "__main__":
    raise SystemExit(_main())
