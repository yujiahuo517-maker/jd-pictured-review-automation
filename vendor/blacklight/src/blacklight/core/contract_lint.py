"""**离线契约巡检**（纯静态：AST + import，不打网络，秒级）。

抓的是三类**不报错、不 crash、只在你照着做的时候才炸**的问题——
2026-08-13 这三类各真实发生过一次，全靠人肉发现，所以固化下来。

    ① 包装层吞了「危险默认值」的参数
       实证：`yx_ms_plan_seckill_enroll` 没透传 `sales_erps`（域函数有、MCP 包装层没有）
       ⇒ 走 MCP 调用变成**全量规划**（4601 款 vs 本人 909），会操作到别人的商品。
       ★判据不是"吞了参数"（安全默认值吞了无所谓，属能力缺口），而是
         **「作用域/归属类参数」+「默认值是 None（= 不筛 = 全量）」** 这个组合。

    ② 文档里写的函数/工具名在代码里不存在
       实证：手册里写 `ge.drill(...)`（实为 `ge.margin.drill`）、
             `markettool.strip_except_plan`（域函数不存在，只有同名 MCP 工具）。
       照着敲直接 AttributeError，而肉眼看名字都"像对的"。

    ③ SKILL.md 的工具计数与实际漂移
       实证：SKILL.md 写 yx_* 79 个，实际 90 个（漂了 11 个才被发现）。

跑法：`python -m blacklight.core.contract_lint`，或在 tests/smoke_test.py 里断言 `lint()["ok"]`。
"""
from __future__ import annotations

import ast
import importlib
import inspect
import os
import re

# ★**归属类**参数：吞掉 ⇒ 操作到**别人的**东西（越权/串号），这是唯一真危险的一类。
#
# ⚠️别把「范围类」参数（`sku_ids`/`skus`/`limit` 等）放进来——它们缺失只是
#   「拿多了**自己的**东西」，结果里看得见、不会静默越权。2026-08-13 第一版放宽了，
#   于是把 `osw_product_list` 少一个 `sku_ids` 也报成危险，噪声会让这个闸被忽略。
SCOPE_PARAMS = {
    "sales_erps", "erp_assistant", "saler", "erp", "cate_op_erp", "operator",
}

# ★**显式豁免**（宁可写在这里被看见，也不要偷偷把规则改窄）。
#   格式 (tool, param): 理由。加一条就等于拍板一次，评审时能逐条质疑。
ALLOW_UNSAFE = {
    ("osw_product_list", "erp"):
        "只读列表工具，`erp=None`=不筛=页面默认口径（对总数用）。输出不直接喂写操作；"
        "要按归属收敛由调用方显式传。★若将来它被接进任何自动写链路，这条豁免必须撤销。",
    ("osw_product_all", "erp"):
        "同 osw_product_list：只读全量拉取，不筛是刻意能力。"
        "⚠️注意它一行是 SPU 不是 SKU（930 行=8274 SKU），别当 SKU 清单直接喂报名。",
    ("yx_ms_list_eligible", "sales_erps"):
        "★取数口径**必须**按采销助理ERP、不按销售员——2026-07-28 实证：让销售员ERP缺省=采销助理ERP "
        "等于多加一道 AND，可报范围从 21921 砍到 3672（−83%），而报名页显示的正是 21921。"
        "强制传 sales_erps 会重演那个 bug。**取数宽、报名窄**是刻意设计。",
    ("yx_ms_price_eligible", "sales_erps"): "同 yx_ms_list_eligible：取数口径按报名页(采销助理ERP)。",
    ("yx_ms_export_eligible", "sales_erps"): "同 yx_ms_list_eligible：导出走报名页口径。",
}
# 这些默认值等价于「不筛 / 全量」⇒ 与 SCOPE_PARAMS 组合即危险。
# ⚠️`""` 也要算：域侧普遍写 `(x or "").strip()`，空串与 None 同样是"不筛"。
UNSAFE_DEFAULTS = (None, "")

_TOOL_RE = re.compile(r"`((?:yx|osw|jzt|pnl|ge|easybi)_[a-z0-9_]+)`")
_FN_RE = re.compile(r"`([a-z_]+)\.([a-z_]+)\(")
_COUNT_RE = re.compile(r"`(yx|osw|jzt|pnl|ge|easybi)_\*`[，,]?\s*(\d+)\s*个")
# 模块路径 / 工具族通配，不是工具名，别误报
_DOC_ALLOW = {"yx_client", "yx_subsidy", "yx_bybt", "yx_ms", "ge_margin", "ge_ad",
              "osw_product", "osw_margin", "jzt_swa", "jzt_auth", "jzt_finance",
              "jzt_diagnose", "easybi_pnl",
              # 2026-08-24 补：审查文档里提到的模块名（不是工具名）
              "osw_ware_edit", "osw_selection", "yx_markettool", "yx_ssm", "ge_ssm",
              "easybi_coupon", "pnl_scan", "pnl_offline"}
# ★标准库/第三方模块：文档里写 `os.getcwd()` / `json.dumps()` 是在讲实现，不是本包契约。
#   不排除的话，任何一句"原来默认写 os.getcwd()"都会把这条检查点红 —— 而永远红的检查会被忽略。
_STDLIB_IN_DOCS = {"os", "sys", "io", "json", "time", "re", "ast", "glob", "shutil",
                   "subprocess", "datetime", "hashlib", "httpx", "openpyxl", "xlrd"}


def _pkg_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _skill_root() -> str:
    """skill 根（含 SKILL.md / docs/），即 src/blacklight 的祖父目录。"""
    return os.path.dirname(os.path.dirname(_pkg_dir()))


# ---------- ① 包装层吞危险参数 ----------
def _thin_delegate_target(node: ast.FunctionDef):
    """只认**瘦委托**：函数体最后一句是 `return <mod>.<fn>(...)`。

    这样能避开编排型包装（如 `pnl_runrate` 自己算窗口、`osw_inquiry_link` 固定筛选参数）
    ——它们内部调用域函数是**刻意固定参数**，不是"吞"。2026-08-13 第一版没这个约束，
    21 条命中里有 2 条是这种误报。
    """
    if not node.body:
        return None
    last = node.body[-1]
    if not (isinstance(last, ast.Return) and isinstance(last.value, ast.Call)):
        return None
    f = last.value.func
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        return f.value.id, f.attr
    return None


def _is_tool(node: ast.FunctionDef) -> bool:
    for d in node.decorator_list:
        if isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "tool":
            return True
        if getattr(d, "attr", "") == "tool":
            return True
    return False


def check_wrappers() -> list:
    srv = os.path.join(_pkg_dir(), "servers")
    out = []
    if not os.path.isdir(srv):
        return out
    for fn in sorted(os.listdir(srv)):
        if not fn.endswith("_server.py"):
            continue
        tree = ast.parse(open(os.path.join(srv, fn), encoding="utf-8").read())
        alias = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module:
                for a in n.names:
                    alias[a.asname or a.name] = "%s.%s" % (n.module, a.name)
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or not _is_tool(node):
                continue
            tgt = _thin_delegate_target(node)
            if not tgt:
                continue
            modalias, callee = tgt
            full = alias.get(modalias)
            if not full:
                continue
            try:
                m = importlib.import_module(full)
                target = getattr(m, callee, None)
                sig = inspect.signature(target)
            except Exception:
                continue
            # ★看**整个模块**而不是单个函数：`ge_ad.waste` 自己不解析 erp，
            #   它传给同模块的 `drill` 才 `erp = erp or current_pin()`。按函数看会误报。
            try:
                import sys as _sys
                src = inspect.getsource(_sys.modules[target.__module__])
            except Exception:
                try:
                    src = inspect.getsource(target)
                except Exception:
                    src = ""
            wrapper_defaults = _param_defaults(node)
            have = set(wrapper_defaults)
            # 本包装函数体内 raise 语句提到的名字 ⇒ 认为已加闸（fail-closed）
            guarded = _guarded_names(node)
            for p, v in sig.parameters.items():
                if p not in SCOPE_PARAMS:
                    continue
                if _defaults_to_me(src, p):
                    continue          # ★该参数留空 = 取当前登录 PIN(=我自己) ⇒ 安全默认值，不报
                if (node.name, p) in ALLOW_UNSAFE:
                    continue          # 已显式拍板豁免（理由见 ALLOW_UNSAFE）
                if p in have:
                    # ★参数在，但**默认值仍是"不筛"且没加闸** ⇒ 危险只是从"吞掉"变成"漏传"
                    #   （2026-08-13 代码评审指出：只查"在不在"会在修完后立刻变绿，
                    #     而不传 sales_erps 依然规划全量。必须连默认值一起查。）
                    d = wrapper_defaults[p]
                    if d in UNSAFE_DEFAULTS and p not in guarded:
                        out.append({
                            "server": fn, "tool": node.name,
                            "target": "%s.%s" % (modalias, callee), "参数": p,
                            "默认值": repr(d), "kind": "有参数但默认不筛且无闸",
                            "why": "调用方漏传即静默扩大到全量；请 fail-closed(raise) 或给安全默认值",
                        })
                    continue
                if v.default is inspect._empty or v.default not in UNSAFE_DEFAULTS:
                    continue          # 域侧必填(调用方一定会传) 或 安全默认值 ⇒ 只是能力缺口
                out.append({
                    "server": fn, "tool": node.name, "target": "%s.%s" % (modalias, callee),
                    "参数": p, "默认值": repr(v.default), "kind": "参数被吞",
                    "why": "作用域参数被吞且域侧默认=不筛 ⇒ MCP 调用会静默扩大到全量",
                })
    return out


_ME_RE = re.compile(r"\b([a-z_][a-z0-9_]*)\s+or\s+[^\n]{0,80}?current_pin")
_ME_CACHE = None


def _me_by_convention() -> set:
    """扫全包，收集「留空 = 取当前登录 PIN」的参数名（**包级约定**）。

    ★这是判危险与否的真正分水岭，**不是参数名本身**：
        `erp=None` / `saler=None` → 某处 `erp or jd_auth.current_pin()`  ⇒ 我自己，**安全**
        `sales_erps=None`         → `(sales_erps or "").strip()`          ⇒ **不筛=全量，危险**

    为什么要扫全包而不是单函数/单模块（前两版都栽了）：
      · `filter_own` 写的是 `me = (saler or current_pin())` —— 赋值目标不是形参本身；
      · `ge_ad.waste` 自己不解析，传给同模块 `drill` 才解析；
      · `pnl.scan.margin_scan` 更是**跨模块**一路传到 `ge.margin.drill` 才解析。
    这三种写法都真实存在，按名字的包级约定是唯一稳的判法。
    """
    global _ME_CACHE
    if _ME_CACHE is not None:
        return _ME_CACHE
    names = set()
    for dp, _, fs in os.walk(_pkg_dir()):
        for f in fs:
            if not f.endswith(".py"):
                continue
            try:
                txt = open(os.path.join(dp, f), encoding="utf-8").read()
            except Exception:
                continue
            names |= {m.group(1) for m in _ME_RE.finditer(txt)}
    _ME_CACHE = names
    return names


def _defaults_to_me(src: str, param: str) -> bool:
    return param in _me_by_convention()


def _param_defaults(node: ast.FunctionDef) -> dict:
    """包装函数的 {参数名: 默认值}（无默认值记 inspect._empty）。"""
    out = {}
    args = node.args
    pos = args.args
    defaults = list(args.defaults)
    pad = len(pos) - len(defaults)
    for i, a in enumerate(pos):
        out[a.arg] = inspect._empty if i < pad else _literal(defaults[i - pad])
    for a, d in zip(args.kwonlyargs, args.kw_defaults):
        out[a.arg] = inspect._empty if d is None else _literal(d)
    return out


def _literal(node):
    try:
        return ast.literal_eval(node)
    except Exception:
        return object()          # 非字面量默认值 ⇒ 视作"不是不筛默认值"


def _guarded_names(node: ast.FunctionDef) -> set:
    """哪些形参已被 **fail-closed 闸**（`if <涉及它> : raise ...`）保护。

    要解一层局部别名：真实写法常是 `se = (sales_erps or "").strip()` 然后 `if not se: raise`，
    闸的条件里出现的是 `se` 而不是形参本身。只认「条件里的名字」+「由形参赋值来的局部名」，
    不做全函数名字大扫除——否则任何带 raise 的函数都会被当成已加闸（第一版就是这个毛病）。
    """
    params = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
    alias = {}                                   # 局部名 -> 它引用到的形参集合
    for sub in ast.walk(node):
        if isinstance(sub, ast.Assign) and len(sub.targets) == 1 and isinstance(sub.targets[0], ast.Name):
            src = {n.id for n in ast.walk(sub.value) if isinstance(n, ast.Name)}
            hit = (src & params) | set().union(*(alias.get(s, set()) for s in src)) if src else set()
            if hit:
                alias[sub.targets[0].id] = hit
    guarded = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.If):
            continue
        if not any(isinstance(x, ast.Raise) for x in ast.walk(sub)):
            continue
        for n in ast.walk(sub.test):
            if not isinstance(n, ast.Name):
                continue
            if n.id in params:
                guarded.add(n.id)
            guarded |= alias.get(n.id, set())
    return guarded


# ---------- ⑤ 「取哪一份」的参数被吞（2026-08-24 加）----------
# ★与 ① 的分工：① 管**越权**（操作到别人的东西）；⑤ 管**取错那一份**（数据是别人的时段/别的池/
#   只有一半），两者都不报错、都只在你照着做的时候才炸，但成因和判据完全不同，**故不合并进
#   SCOPE_PARAMS**——①的注释里写着放宽过一次、噪声让闸被忽略，那个教训这里照抄不误。
#
# 判据：库函数有、瘦委托包装器**没暴露**，且这个参数属于下面四类"省略即换了一份数据"：
#   · 时间窗：begin_time/end_time —— 不传=全活动（实测 137716 条 > 平台 50000 上限 ⇒ 导出必失败）
#   · 取哪一份：after/exclude_ids —— 不传=**静默拿到上一次的旧导出**（2026-08-04 漏评估约 620 个 SKU）
#   · 池/口径开关：business_type/activity_duration —— 秒杀=122，便宜包邮/特价各不同，
#     写死就是拿秒杀的口径去查别的池（静默返回错的或空的）
#   · 覆盖范围硬顶：max_pages/page_size 且库侧到顶不报错 —— 悄悄少算尾部
# 全部来自 2026-08-24 当天实撞的四起，不是想象出来的类别。
SHAPE_PARAMS = {
    "begin_time": "时间窗：不传=全活动，可能直接超平台导出上限而必失败",
    "end_time": "时间窗：同 begin_time",
    "after": "认领刚触发的那一份：不传会静默返回上一次的旧导出",
    "exclude_ids": "认领刚触发的那一份：不传会静默返回上一次的旧导出",
    "business_type": "池/口径开关：写死=拿这个池的口径去查另一个池",
    "activity_duration": "池/口径开关：秒杀按小时、便宜包邮按天，写死会取错",
    "max_pages": "覆盖范围硬顶：到顶若不报错就是悄悄少算尾部",
}
# 显式豁免：写在这里=拍过板，评审时能逐条质疑（同 ALLOW_UNSAFE 的纪律）
ALLOW_SHAPE = {
    ("yx_ms_export_eligible", "activity_duration"):
        "导出触发按秒杀固定 28，其它频道有各自的触发工具；这里写死是刻意的。",
}


def check_shape_params() -> list:
    """库函数有、瘦委托包装器没暴露的「取哪一份」参数。"""
    srv = os.path.join(_pkg_dir(), "servers")
    out = []
    if not os.path.isdir(srv):
        return out
    for fn in sorted(os.listdir(srv)):
        if not fn.endswith("_server.py"):
            continue
        tree = ast.parse(open(os.path.join(srv, fn), encoding="utf-8").read())
        alias = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module:
                for a in n.names:
                    alias[a.asname or a.name] = "%s.%s" % (n.module, a.name)
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or not _is_tool(node):
                continue
            tgt = _thin_delegate_target(node)
            if not tgt:
                continue
            modalias, callee = tgt
            full = alias.get(modalias)
            if not full:
                continue
            try:
                m = importlib.import_module(full)
                sig = inspect.signature(getattr(m, callee))
            except Exception:
                continue
            have = set(_param_defaults(node))
            for p, v in sig.parameters.items():
                if p not in SHAPE_PARAMS or p in have:
                    continue
                # ★**库侧必填的不算被吞**（2026-08-24 首跑就误报了一条）：`easybi.coupon.cost_trend(before, after)`
                #   里 after 是必填区间，包装器一定在以别的形状（after_start/after_end）供给它，
                #   不可能"省略"。只有**可省略**的参数才谈得上"省略即换了一份数据"。
                if v.default is inspect._empty:
                    continue
                if (node.name, p) in ALLOW_SHAPE:
                    continue
                out.append({"server": fn, "tool": node.name,
                            "target": "%s.%s" % (modalias, callee), "参数": p,
                            "kind": "取数形状参数被吞", "why": SHAPE_PARAMS[p]})
    return out


# ---------- ② 文档标识符 ----------
def _real_names():
    tools, fns = set(), set()
    srv = os.path.join(_pkg_dir(), "servers")
    if os.path.isdir(srv):
        for fn in os.listdir(srv):
            if not fn.endswith("_server.py"):
                continue
            t = ast.parse(open(os.path.join(srv, fn), encoding="utf-8").read())
            tools |= {n.name for n in t.body if isinstance(n, ast.FunctionDef) and _is_tool(n)}
    for pkg in ("yx", "osw", "jzt", "ge", "pnl", "easybi", "core"):
        d = os.path.join(_pkg_dir(), pkg)
        if not os.path.isdir(d):
            continue
        # 包级再导出（文档常写 `core.pmap_batch()` 而不是 `base.pmap_batch()`）
        try:
            p = importlib.import_module("blacklight.%s" % pkg)
            for name, obj in vars(p).items():
                if callable(obj) and not name.startswith("_"):
                    fns.add("%s.%s" % (pkg, name))
        except Exception:
            pass
        for f in os.listdir(d):
            if not f.endswith(".py") or f.startswith("_"):
                continue
            try:
                m = importlib.import_module("blacklight.%s.%s" % (pkg, f[:-3]))
            except Exception:
                continue
            for name, obj in vars(m).items():
                if callable(obj) and not name.startswith("_"):
                    fns.add("%s.%s" % (f[:-3], name))
    return tools, fns


def check_docs() -> list:
    root = _skill_root()
    docs = []
    for dirpath, _, files in os.walk(os.path.join(root, "docs")):
        # ★**跳过 docs/archive/**（2026-08-24）：归档的是**明确标注已过时**的历史设计稿，
        #   里面的旧工具名是史料不是契约。不跳过的话它永远红，而一个永远红的检查会被忽略
        #   —— 那才是真损失。归档的准入条件是文件开头有"已过时"横幅（人工放进去时写的）。
        if os.sep + "archive" + os.sep in dirpath + os.sep:
            continue
        docs += [os.path.join(dirpath, f) for f in files if f.endswith(".md")]
    sk = os.path.join(root, "SKILL.md")
    if os.path.exists(sk):
        docs.append(sk)
    tools, fns = _real_names()
    out = []
    for d in docs:
        txt = open(d, encoding="utf-8").read()
        rel = os.path.relpath(d, root)
        for m in _TOOL_RE.finditer(txt):
            n = m.group(1)
            if n in tools or n in _DOC_ALLOW or n.endswith("_"):
                continue
            # 文档**说明某工具已退役**是合法提及，不该报（否则只能靠删掉历史来消警）
            line = txt[txt.rfind("\n", 0, m.start()) + 1: txt.find("\n", m.end())]
            if any(k in line for k in ("退役", "废弃", "已删除", "已改名", "旧工具", "旧名")):
                continue
            out.append({"doc": rel, "名字": n, "kind": "工具名不存在"})
        for m in _FN_RE.finditer(txt):
            mod, fname = m.group(1), m.group(2)
            if mod in _STDLIB_IN_DOCS:
                continue          # 标准库/第三方：不是本包契约
            if fname.startswith("_"):
                continue          # 私有函数不在导出面上，文档提到属实现说明，不算契约
            # 文档常用**导入别名**写（`yx_client.x()`=yx/client.py、`jd_auth.x()`=core/auth.py）
            # ⇒ 除原名外，再试「剥掉第一个 `xxx_` 前缀」的模块名
            cands = {"%s.%s" % (mod, fname)}
            if "_" in mod:
                cands.add("%s.%s" % (mod.split("_", 1)[1], fname))
            if cands & fns:
                continue
            out.append({"doc": rel, "名字": "%s.%s()" % (mod, fname), "kind": "域函数不存在"})
    return out


# ---------- ③ SKILL.md 工具计数 ----------
def check_counts() -> list:
    root = _skill_root()
    sk = os.path.join(root, "SKILL.md")
    if not os.path.exists(sk):
        return []
    tools, _ = _real_names()
    real = {}
    for t in tools:
        for p in ("yx", "osw", "jzt", "pnl", "ge", "easybi"):
            if t.startswith(p + "_"):
                real[p] = real.get(p, 0) + 1
                break
    out, matched = [], 0
    # ★2026-08-24：连 README.md 一起查 —— 审查时发现它写着"3 个 server、osw 45/yx 79/jzt 29"，
    #   而实际是 5 个 server、85/104/36，**全篇没提 easybi 与 pnl**。只守 SKILL.md 守不住这半边。
    docs = [("SKILL.md", sk)]
    rd = os.path.join(root, "README.md")
    if os.path.exists(rd):
        docs.append(("README.md", rd))
    for which, path in docs:
        for m in _COUNT_RE.finditer(open(path, encoding="utf-8").read()):
            matched += 1
            dom, n = m.group(1), int(m.group(2))
            if real.get(dom, 0) != n:
                out.append({"域": dom, "文件": which, "声明": n, "实际": real.get(dom, 0)})
    # ★阴性对照：正则一条都没匹配上时，"没发现问题"其实是"根本没检查"。
    #   SKILL.md 措辞一改（如 "`yx_*` 共 90 个"）就静默失效，而断言照样通过。
    #   这正是本文件要防的那类静默失败，所以自己也得带对照。
    if matched == 0:
        out.append({"域": "(全部)", "问题": "_COUNT_RE 一条都没匹配上 —— 大概率 SKILL.md 措辞变了，"
                                          "本项检查已静默失效，请改正则或改回措辞",
                    "SKILL.md 声明": None, "实际": None})
    return out


# ---------- ④ docstring 要求值 ≠ 签名默认值 ----------
def _iter_py():
    for dp, _, fs in os.walk(_pkg_dir()):
        if "__pycache__" in dp:
            continue
        for fn in fs:
            if fn.endswith(".py"):
                yield os.path.join(dp, fn)


def _read(p: str) -> str:
    with open(p, encoding="utf-8", errors="replace") as f:
        return f.read()



#: 「必须/务必/一律/建议 param=value」这类**行为要求**
_REQUIRE_PAT = re.compile(
    r"(?:必须|务必|一律|建议|请|应)[^。\n；;]{0,14}?[`\s]([a-z_][a-z0-9_]{2,24})\s*=\s*([A-Za-z0-9_.\'\"]+)")
#: 条件式 opt-in（「真要旧值请显式 allow_stale=True」）——安全默认是对的，别误报
_CONDITIONAL = re.compile(r"真要|确需|如需|若要|需要旧|想要|除非|要看全部|确实要")


def check_defaults() -> list:
    """★**docstring 说要用 A、签名默认却是 B** —— 只在你照着文档做时才不炸的那类漂移。

    2026-08-19 实证代价：`bybt.enroll_bulk` 的 docstring/SKILL.md/作业手册/3 个记忆
    共 **6 处**写着「必须 concurrency=1」，但域函数与 MCP 包装层默认都是 **8**，
    MCP 工具描述还写着「默认8，加速大批量」——同一仓库三种说法。
    结果：**179 次**「正在报名中」卡控，占全部真·不稳定失败的 70%。
    经验写于 07-27，却在 08-05/08-06 两次复发，直到 08-07 才稳定 —— **散文知识有复发率**。

    ⇒ 判据：这条知识漂了，行为会不会变？会变就必须是**默认值**，文档只能做解释。

    ## 命中后有三条正解，别一律改默认值
    1. **护栏该在代码里** ⇒ 把默认值改成文档要求的（`bybt.enroll_bulk` 走的这条）。
    2. **保护其实在别处** ⇒ 改措辞，别让文档暗示"默认是错的"。
       实例：`osw_material_autofill` 的 `images_only` 建议 True、默认 False，
       但真正的护栏是「无 confirm_token 只出方案」+「qc_on=True 不合格不上传」，
       所以正解是把「建议先 X=True」改成「如需人工过一眼再挂，传 X=True」。
    3. **条件式 opt-in** ⇒ 本检查已自动放过（`真要/确需/如需/除非…`），
       如 `ms.seckill_thresholds` 的「真要旧值请显式 allow_stale=True」——安全默认本就正确。
    """
    hits = []
    for p in _iter_py():
        try:
            tree = ast.parse(_read(p))
        except SyntaxError:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            doc = ast.get_docstring(fn) or ""
            if not doc:
                continue
            defaults = _param_defaults(fn)
            for m in _REQUIRE_PAT.finditer(doc):
                param, want = m.group(1), m.group(2).strip("'\"`")
                if param not in defaults:
                    continue
                seg = doc[max(0, m.start() - 24):m.end()]
                if _CONDITIONAL.search(seg):
                    continue                      # 条件式 opt-in，安全默认正确
                cur = defaults[param]
                if str(cur) == want or (cur is None and want.lower() in ("none", "null")):
                    continue
                hits.append({
                    "file": os.path.relpath(p, _skill_root()).replace(os.sep, "/"),
                    "func": fn.name, "param": param,
                    "文档要求": want, "签名默认": str(cur),
                    "原文": re.sub(r"\s+", " ", seg)[-70:],
                    "怎么办": "把默认值改成文档要求的（护栏要在代码里），或删掉文档那句要求",
                })
    return hits


def lint() -> dict:
    w, d, c = check_wrappers(), check_docs(), check_counts()
    f = check_defaults()
    sp = check_shape_params()
    return {
        "ok": not (w or d or c or f or sp),
        "包装层吞危险参数": w,
        "取数形状参数被吞": sp,
        "文档标识符不存在": d,
        "工具计数漂移": c,
        "文档要求≠默认值": f,
        "_note": "全静态、不打网络。四类都是『不报错只在照着做时才炸』的问题。"
                 "★安全默认值被吞不算问题（属能力缺口），只报作用域参数+不筛默认值的组合。",
    }


def main():
    import io as _io
    import json
    import sys as _sys
    # Windows 控制台默认 GBK，输出里的 ⇒/★ 会 UnicodeEncodeError ⇒ 显式转 UTF-8
    try:
        _sys.stdout = _io.TextIOWrapper(_sys.stdout.buffer, encoding="utf-8")
    except Exception:
        pass
    r = lint()
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
