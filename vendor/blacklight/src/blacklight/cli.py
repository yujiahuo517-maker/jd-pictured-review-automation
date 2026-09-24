"""blacklight 命令行入口 —— 注册 MCP / 体检 / 登录 / 导出配置。

★**为什么有这个模块**：安装最大的堵点是「MCP 注册时要填的绝对 python 路径要人自己找」，
找错了症状还很隐蔽（`/mcp` 里 `Failed to connect`、工具列表空白，看不出是路径问题）。

解法是 `sys.executable`：**跑这段代码的 python 就是那个路径**，不用猜、不用扫盘、不会错。
本模块被 `[project.scripts]` 注册成 `blacklight` 命令，控制台脚本会把正确的解释器写进 shebang，
∴ 装完之后再调 `blacklight ...` 永远不会重蹈路径覆辙。

用法：
    blacklight mcp-install          # 注册（或重装）3 个 server 到 Claude Code
    blacklight mcp-json             # 输出通用 mcpServers JSON（给别的 MCP 客户端用）
    blacklight doctor               # 三域契约巡检
    blacklight login / status       # 登录 / 看登录态
    blacklight smoke                # 离线冒烟
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess

from blacklight.core import paths as _paths
import sys

# ★★2026-08-24 审查修：这里长期只有 3 个，于是 `blacklight mcp-install` / `install.py` 跑完，
#   **easybi 与 pnl（含全部 ge_* 工具）永远不会被注册**——新环境装完静默少 31 个工具，
#   而 SKILL.md/README 里却写着它们的能力。属于"文档说有、装完没有"的功能 bug，不是文档滞后。
SERVERS = [
    ("blacklight-osw", "blacklight.servers.osw_server", "采销工作台"),
    ("blacklight-yx", "blacklight.servers.yx_server", "营销活动"),
    ("blacklight-jzt", "blacklight.servers.jzt_server", "京准通广告"),
    ("blacklight-easybi", "blacklight.servers.easybi_server", "easybi 数据平台"),
    ("blacklight-pnl", "blacklight.servers.pnl_server", "毛利监控 + 黄金眼"),
]

# 2.0 之前用过的注册名；重装时一并清掉，避免同一套工具挂两遍
LEGACY_NAMES = ["osw", "yx", "jzt", "osw-mcp", "yx-mcp", "jdcore"]


def python_path() -> str:
    """当前解释器的绝对路径 —— 这就是注册 MCP 要填的那个值。"""
    return sys.executable.replace("\\", "/")


def server_config() -> dict:
    """通用 `mcpServers` 配置块。

    MCP 是标准协议，**server 本身与客户端无关**；各家客户端的差别只在「配置写哪、外层键名叫啥」。
    要素只有三个：command（绝对 python）/ args / env。这份 JSON 可直接用于 Claude Desktop、
    Cursor 等使用 `mcpServers` 结构的客户端；Claude Code 走 `claude mcp add-json`。
    """
    py = python_path()
    return {
        "mcpServers": {
            name: {"command": py, "args": ["-m", module]}
            for name, module, _ in SERVERS
        }
    }


def _claude_bin() -> str | None:
    return shutil.which("claude")


def _run(cmd: list, quiet: bool = False) -> tuple:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if not quiet and p.returncode != 0:
            err = (p.stderr or p.stdout or "").strip().splitlines()
            if err:
                print("      " + err[-1][:160])
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:  # noqa: BLE001
        return 1, str(e)


def mcp_install(scope: str = "user", keep_legacy: bool = False) -> int:
    """把 3 个 server 注册到 Claude Code。已存在的先删再加（幂等，可反复跑）。"""
    claude = _claude_bin()
    if not claude:
        print("✗ 没找到 `claude` 命令（Claude Code CLI 不在 PATH 里）。")
        print("  如果你用的是别的 MCP 客户端，请改用：blacklight mcp-json")
        return 1

    print(f"python 路径：{python_path()}")
    if not keep_legacy:
        for old in LEGACY_NAMES:
            _run([claude, "mcp", "remove", old], quiet=True)

    ok = 0
    for name, module, desc in SERVERS:
        _run([claude, "mcp", "remove", name], quiet=True)      # 先删，保证幂等
        code, _ = _run([claude, "mcp", "add", "-s", scope, name, "--",
                        python_path(), "-m", module])
        if code == 0:
            print(f"  ✓ {name:<16} {desc}")
            ok += 1
        else:
            print(f"  ✗ {name:<16} 注册失败")
    print(f"\n{ok}/{len(SERVERS)} 个 server 已注册（scope={scope}）")
    if ok:
        print("→ 回到 Claude Code 里执行 /mcp 重连，工具才会生效")
    return 0 if ok == len(SERVERS) else 1


def mcp_json(pretty: bool = True) -> int:
    """输出通用配置，供 Claude Desktop / Cursor / 其它 MCP 客户端粘贴。"""
    print(json.dumps(server_config(), indent=2 if pretty else None, ensure_ascii=False))
    return 0


def check_env(verbose: bool = True) -> bool:
    """装包前的体检：这个 python 到底能不能用。

    ⚠️**这一步是必要的**：某些环境的 `python` 会解析到被裁剪过的运行时（缺 pip / 缺标准库），
    用它注册 MCP 的表现是 `/mcp` 里 `Failed to connect`、工具列表空白——
    **看不出是 python 选错了**，会浪费很久。所以宁可在装之前就报出来。
    """
    ok = True
    if verbose:
        print(f"python  : {sys.executable}")
        print(f"版本    : {sys.version.split()[0]}")
    if sys.version_info < (3, 9):
        print("✗ 需要 Python ≥ 3.9")
        ok = False
    code, out = _run([sys.executable, "-m", "pip", "--version"], quiet=True)
    if code != 0:
        print("✗ 这个 python 没有可用的 pip —— 多半是被裁剪过的运行时，请换一个完整安装的 Python")
        ok = False
    elif verbose:
        print(f"pip     : {out.strip().split(' from ')[0]}")
    return ok


def verify_imports() -> bool:
    missing = []
    for m in ("mcp", "httpx", "openpyxl", "xlrd"):
        try:
            __import__(m)
        except ImportError:
            missing.append(m)
    if missing:
        print(f"✗ 缺依赖：{', '.join(missing)}　→ 先跑 pip install -e .")
        return False
    return True


def doctor() -> int:
    """五域契约巡检。抓包封装的接口会随页面改版漂移，动手前先查。

    ★2026-08-24：原来只巡 osw/yx/jzt 三域，easybi/ge **有 doctor 却从没被跑过**
      （而它们正是最需要盯的——easybi 的自定义指标 code 就是中文名，谁都能改名）。
      两者的入口函数叫 `doctor()` 不叫 `run()`，所以这里两个名字都认。"""
    rc = 0
    for dom in ("osw", "yx", "jzt", "easybi", "ge"):
        try:
            mod = __import__(f"blacklight.{dom}.doctor", fromlist=["run", "doctor"])
            entry = getattr(mod, "run", None) or getattr(mod, "doctor", None)
            if entry is None:
                print(f"  ✗ {dom:<6} 没有 run()/doctor() 入口")
                rc = 1
                continue
            r = entry()
            bad = [c["check"] for c in r.get("checks", []) if not c.get("ok")]
            flag = "✓" if r.get("healthy") else "✗"
            print(f"  {flag} {dom:<6} healthy={r.get('healthy')}　检查项 {len(r.get('checks', []))}"
                  + (f"　失败：{bad}" if bad else ""))
            if not r.get("healthy"):
                rc = 1
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {dom:<6} 异常：{str(e)[:90]}")
            rc = 1
    if rc:
        print("\n有 drift → 相关接口可能漂移，写路径先转人工核对再放手。")
    return rc


def audit(quick: bool = False) -> int:
    """**一条命令跑完自检四件套** —— 静态契约 + 审计回归 + 护栏效用 + 两个测试文件。

    ★为什么要串起来：这四样以前散着，谁想起来谁跑；2026-08-24 审查时才发现
      `contract_lint` 早就在报 SKILL.md 计数漂移、`auditscan` 早就在报活体标记过期，
      **报了很久没人看**。一个没人跑的检查等于不存在。
    `quick=True` 只跑不联网的静态三件套（适合接进每日入口）。
    """
    import subprocess
    rc, lines = 0, []

    from blacklight.core import contract_lint, auditscan, rulestat
    l = contract_lint.lint()
    bad = {k: v for k, v in l.items() if isinstance(v, list) and v}
    lines.append(("contract_lint", l.get("ok"), "全绿" if l.get("ok") else str(bad)[:160]))
    rc |= 0 if l.get("ok") else 1

    a = auditscan.scan()
    n = a.get("问题数") or 0
    # ★auditscan 与 rulestat 是**提醒**不是闸：它们报的多是"待人工拍板"（活体标记、候删清单），
    #   不该让 audit 返回失败 —— 但也不能打 ✗ 说"失败"再在末尾说"全部通过"，那是自相矛盾的界面。
    #   所以单独一个 `note` 档：既看得见，又不谎报成失败。
    lines.append(("auditscan", None, "%d 条待人工看（活体标记/候删等，不作为失败）" % n))
    # auditscan 的问题多是"待人工拍板"（活体标记等），不作为失败：它是提醒不是闸
    r = rulestat.report()
    mm = len(r.get("矩阵对账") or [])
    lines.append(("rulestat", None, "写路径 %d 条 / 矩阵对账 %d 条待看 / 候删 %d 条"
                  % (len(r.get("写路径使用度") or []), mm, len(r.get("候删清单") or []))))

    if not quick:
        here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        for name in ("smoke_test.py", "unit_test.py"):
            t = os.path.join(here, "tests", name)
            if not os.path.exists(t):
                lines.append((name, False, "找不到（可编辑安装才有 tests/）"))
                rc |= 1
                continue
            env = dict(os.environ, PYTHONUTF8="1")
            p = subprocess.run([sys.executable, t], cwd=os.path.dirname(t), env=env,
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
            tail = [x for x in (p.stdout or "").strip().split("\n") if x.strip()][-1:]
            lines.append((name, p.returncode == 0, (tail[0] if tail else "")[:160]))
            rc |= 0 if p.returncode == 0 else 1

    print("\n== blacklight audit ==")
    for name, ok, detail in lines:
        mark = "…" if ok is None else ("✓" if ok else "✗")
        print("  %s %-16s %s" % (mark, name, detail))
    print("  " + ("闸全绿（… 那两行是待人工看的提醒，不是失败）" if rc == 0 else "★有失败项，见上"))
    return rc


def housekeep(days: int = 30, apply: bool = False) -> int:
    """runtime/ 保留策略：**默认只报告，加 --apply 才真删**。

    ★为什么默认不删：`runtime/` 里混着"可再生的产物"（exports/cache）和
      "删了就没了的证据"（audit.log、ledger 账本）。后者是 auditscan/rulestat/compare 的依据，
      本命令**从不碰**它们；前者也先给你看清单再动手。
    """
    import time as _t
    root = _paths.home()
    targets = [("exports", os.path.join(root, "exports")),
               ("cache", os.path.join(root, "cache"))]
    cutoff = _t.time() - days * 86400
    total, freed, victims = 0, 0, []
    for name, d in targets:
        if not os.path.isdir(d):
            continue
        for dp, _, fs in os.walk(d):
            for fn in fs:
                fp = os.path.join(dp, fn)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                total += st.st_size
                if st.st_mtime < cutoff:
                    victims.append((fp, st.st_size))
                    freed += st.st_size
    print("\n== runtime 保留策略（>%d 天的产物）==" % days)
    print("  exports+cache 合计 %.1f MB，其中可清理 %d 个文件 / %.1f MB"
          % (total / 1048576.0, len(victims), freed / 1048576.0))
    for fp, sz in sorted(victims, key=lambda x: -x[1])[:8]:
        print("    %8.1f KB  %s" % (sz / 1024.0, os.path.relpath(fp, root)))
    print("  ★audit.log / ledger **不在清理范围**：它们是 auditscan/rulestat/compare 的证据源")
    if not apply:
        print("  （只报告；真删加 --apply）")
        return 0
    n = 0
    for fp, _ in victims:
        try:
            os.remove(fp)
            n += 1
        except OSError:
            pass
    print("  已删除 %d 个文件" % n)
    return 0


def main(argv=None) -> int:
    # Windows 控制台默认 GBK，输出里的 ✓/★/⇒ 会 UnicodeEncodeError（同 contract_lint.main 的处理）
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="blacklight", description="blacklight 工具包命令行")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("mcp-install", help="注册 5 个 MCP server 到 Claude Code（幂等）")
    p.add_argument("--scope", default="user", choices=["user", "project", "local"])
    p.add_argument("--keep-legacy", action="store_true", help="不清理 2.0 之前的旧注册名")

    sub.add_parser("mcp-json", help="输出通用 mcpServers JSON（其它 MCP 客户端用）")
    sub.add_parser("doctor", help="五域契约巡检")
    sub.add_parser("check", help="检查当前 python 与依赖是否可用")
    sub.add_parser("login", help="弹浏览器登录并存 cookie")
    sub.add_parser("status", help="查看登录态")
    sub.add_parser("smoke", help="跑离线冒烟测试")
    ph = sub.add_parser("housekeep", help="runtime/ 产物保留策略（默认只报告）")
    ph.add_argument("--days", type=int, default=30)
    ph.add_argument("--apply", action="store_true", help="真删（默认只报告）")
    pa = sub.add_parser("audit", help="自检四件套：契约lint + 审计回归 + 护栏效用 + 两个测试文件")
    pa.add_argument("--quick", action="store_true", help="只跑不联网的静态三件套")
    sub.add_parser("path", help="打印当前 python 绝对路径（注册 MCP 用的就是它）")

    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 0

    if a.cmd == "mcp-install":
        return mcp_install(a.scope, a.keep_legacy)
    if a.cmd == "mcp-json":
        return mcp_json()
    if a.cmd == "path":
        print(python_path())
        return 0
    if a.cmd == "check":
        return 0 if (check_env() and verify_imports()) else 1
    if a.cmd == "doctor":
        return doctor()
    if a.cmd == "audit":
        return audit(quick=a.quick)
    if a.cmd == "housekeep":
        return housekeep(days=a.days, apply=a.apply)
    if a.cmd == "login":
        from blacklight.core import login as _login
        return _login.main() if hasattr(_login, "main") else 0
    if a.cmd == "status":
        from blacklight.core import auth as _auth
        print(json.dumps(_auth.status(), ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "smoke":
        here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        t = os.path.join(here, "tests", "smoke_test.py")
        if not os.path.exists(t):
            print(f"✗ 找不到 {t}（可编辑安装才有 tests/）")
            return 1
        return subprocess.call([sys.executable, t])
    return 0


if __name__ == "__main__":
    sys.exit(main())
