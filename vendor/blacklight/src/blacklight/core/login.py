"""
yx.jd.com 登录：用 DrissionPage 打开 Chrome 登录 yx 内网（ERP/focus realm），
抓取整套 cookie jar（含 HttpOnly，如 pt_key/sdtoken）存入 yx-mcp/credentials.json。

与 jx-auth/login.py 同款机制，但目标是 yx.jd.com（mcpman 的鉴权realm），而非 dp/BDP。
登录成功判据：实测 mcpman 探针接口 success==True（而非某个具体 cookie 名）。

用法：
  python jd_login.py          # 打开 Chrome 登录 yx，成功后保存 cookie
"""
import sys
import time

from blacklight.core import auth as jd_auth

# 登录落地页：活动详情页天然会拉起 yx 内网登录并授权 mcpman。
LOGIN_URL = f"https://yx.jd.com/colorist/intranet/activityDetail?campaignId={jd_auth.PROBE_CAMPAIGN_ID}&tab=3"


def _extract(cookies):
    """DrissionPage cookies -> (cookie_str, 最早过期 epoch)。"""
    pairs, expiries = [], []
    if isinstance(cookies, dict):
        pairs = [f"{k}={v}" for k, v in cookies.items()]
    elif isinstance(cookies, (list, tuple)):
        for c in cookies:
            if not isinstance(c, dict):
                continue
            pairs.append(f"{c.get('name','')}={c.get('value','')}")
            exp = c.get("expiry") or c.get("expires")
            try:
                exp = float(exp)
                if exp > time.time():
                    expiries.append(exp)
            except (TypeError, ValueError):
                pass
    cookie_str = "; ".join(p for p in pairs if p and not p.endswith("="))
    return cookie_str, (min(expiries) if expiries else None)


def _capture_erp(page) -> None:
    """登录后自动识别操作人 ERP：读 yx.jd.com 页面的 localStorage['erp']（实证前端会写入），
    存进 credentials.json → current_pin() 直接可用，**无需再手动 yx_set_pin**。读不到静默跳过（仍可手动设）。"""
    # IIFE 形式跨 DrissionPage 版本都能取到返回值；先 localStorage['erp']，再从 watermark JSON 兜底
    js = ('(function(){try{var e=localStorage.getItem("erp");if(e)return e;'
          'var w=localStorage.getItem("watermark");if(w){var o=JSON.parse(w);if(o&&o.erp)return o.erp;}'
          '}catch(x){}return "";})()')
    for _ in range(3):
        try:
            erp = page.run_js(js)
            erp = str(erp).strip() if erp else ""
            if erp and erp.lower() != "null":
                jd_auth.set_pin(erp)
                print(f"已自动识别操作人 ERP：{erp}（无需手动设置）", file=sys.stderr)
                return
        except Exception:
            pass
        time.sleep(2)
    print("未能自动识别 ERP（可在 Claude 里用 yx_set_pin 手动设，或设环境变量 YX_PIN）。", file=sys.stderr)


def main() -> None:
    try:
        from DrissionPage import ChromiumPage, ChromiumOptions   # 懒加载：仅浏览器登录才需要
    except ImportError:
        print("浏览器登录需 DrissionPage（已从核心依赖拆出）。装法（本机需已装 Chrome）：\n"
              "  pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements-login.txt\n"
              "  或  pip install DrissionPage", file=sys.stderr)
        sys.exit(1)

    print("正在启动 Chrome，请在弹窗中完成 yx.jd.com 登录（公司环境下可能自动完成）。", file=sys.stderr)
    print(f"目标页面: {LOGIN_URL}", file=sys.stderr)

    import os
    options = ChromiumOptions()
    options.set_local_port(9444)
    from blacklight.core import paths
    user_data = paths.browser_data_dir()
    options.set_user_data_path(user_data)
    options.set_argument("--start-maximized")

    page = ChromiumPage(options)
    cookie_str, expires_at = "", None
    try:
        page.get(LOGIN_URL, timeout=60)
        waited, max_wait = 0, 600
        while waited < max_wait:
            time.sleep(3)
            waited += 3
            try:
                raw = page.cookies(as_dict=False)
            except TypeError:
                raw = page.cookies()
            cookie_str, expires_at = _extract(raw)
            # 成功判据：实测 mcpman 已授权
            if cookie_str.strip():
                jd_auth.save_cookie(cookie_str, expires_at,
                                    "browser" if expires_at else "assumed")
                if jd_auth.yx_is_logged_in() is True:
                    print("已检测到 yx 登录并通过 mcpman 探活。", file=sys.stderr)
                    _capture_erp(page)   # 自动识别操作人 ERP（读 yx.jd.com 的 localStorage['erp']）
                    break
            if waited % 30 == 0:
                print(f"等待登录中... ({waited}s)", file=sys.stderr)
        else:
            print("等待登录超时（10 分钟）。请确认已在浏览器中完成 yx 登录后重试。", file=sys.stderr)
            _quit(page)
            sys.exit(1)
    except Exception as e:
        print(f"运行出错: {e}", file=sys.stderr)
        _quit(page)
        sys.exit(1)

    st = jd_auth.status_dict()
    print(f"登录态已保存到: {jd_auth.cred_path()}", file=sys.stderr)
    print(f"可用: {'是' if st['usable'] else '否'} | 估算剩余: {st['remaining_est']} | mcpman 探活: {st['mcpman_live']}", file=sys.stderr)
    _quit(page)


def _quit(page) -> None:
    try:
        page.quit(timeout=5)
    except Exception:
        pass


if __name__ == "__main__":
    main()
