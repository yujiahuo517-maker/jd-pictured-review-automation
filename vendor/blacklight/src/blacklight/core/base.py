"""
yx-mcp 公共核心：错误类型、HTTP 客户端、二次确认令牌、配置加载。

所有场域模块（campaign/subsidy/markettool/bybt/ms）从这里取共享件，
不再互相 import（此前 BlacklightError/_confirm_token 挂在 yx_client 上，属分层错位）。
"""
from __future__ import annotations

import functools
import hashlib
import json as _json
import os
import threading as _threading
import time as _time
from functools import lru_cache
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
REDIRECT_CODES = (301, 302, 303, 307, 308)
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


class BlacklightError(RuntimeError):
    """所有场域统一的业务错误类型。"""


YxError = BlacklightError   # 兼容别名（历史名）——外部/遗留引用仍可用


# --------------------------------------------------------------------------- #
# HTTP 客户端
# --------------------------------------------------------------------------- #
def _httpx():
    try:
        import httpx
    except ImportError as e:  # pragma: no cover
        raise BlacklightError("缺少依赖 httpx，请先 pip install httpx") from e
    return httpx


def bare_client(timeout: float = 20.0):
    """裸 httpx.Client（不带默认头，headers 由各请求自带）。campaign(mcpman) / subsidy(mac) 用。"""
    return _httpx().Client(timeout=timeout, follow_redirects=False, verify=False)


def make_client(cookie: str, *, origin: str = "https://yx.jd.com", referer: str = "https://yx.jd.com/",
                content_type: Optional[str] = "application/json",
                accept: str = "application/json, text/plain, */*",
                timeout: float = 20.0, extra: dict = None):
    """带默认头（Cookie/UA/Origin/Referer）的 httpx.Client。markettool/bybt/ms 用。
    content_type=None 时不设（markettool 按请求自设）。"""
    headers = {"Cookie": cookie, "User-Agent": DEFAULT_UA, "Accept": accept,
               "Origin": origin, "Referer": referer}
    if content_type:
        headers["Content-Type"] = content_type
    if extra:
        headers.update(extra)
    return _httpx().Client(headers=headers, timeout=timeout, verify=False)


def json_post(client, base: str, path: str, body: Optional[dict] = None) -> dict:
    """POST JSON（`body` 可 None→空体）→ 校验 `success` → 返回 `data`（缺省 `{}`）。
    bybt/ms 等"yz cookie + JSON + 不签名"的网关共用（各模块 `_post` 收敛到此）。
    ⚠️`data or {}` 会把合法 falsy(0/False/[]) 抹成 `{}`——返回 falsy data 的接口别用本函数。"""
    r = client.post(f"{base}{path}",
                    content=_json.dumps(body, ensure_ascii=False) if body is not None else None)
    if r.status_code in REDIRECT_CODES:
        raise BlacklightError(f"{path.split('?')[0]} 被重定向（{r.status_code}）——登录态失效，请运行 yx_login 重登")
    r.raise_for_status()
    try:
        j = r.json()
    except Exception as e:
        raise BlacklightError(f"{path.split('?')[0]} 未返回 JSON（HTTP {r.status_code}）——登录态可能失效，请 yx_login") from e
    if not j.get("success"):
        raise BlacklightError(f"{path.split('?')[0]}: {j.get('message') or j.get('code')}")
    return j.get("data") or {}


def paged_scan(fetch, page_size: int, *, max_pages: int = 200, retries: int = 2,
               on_page=None) -> dict:
    """**分页拉全量的统一跑法**：空页重试 → 与 `totalCount` 轧账 → 明确回报是否拉全。

    起因（memory `subsidy-pool-pull-truncation` + 2026-08-05 复盘）：京东分页接口**串行也会偶发空页**，
    而各处都写成 `if not items: break`。后果分两档：
      - 拉列表的：静默少数据（实测 1300/2015），当成全量用就会漏；
      - **`find_applied` 这类的更糟**：空页 → 返回 `None` → 调用方读成"确认没报名/退不了"，
        那是个**看起来很确定的错误答案**。

    `fetch(page) -> (items, total)`；`on_page(items)` 可选，返回 True 表示"找到了、可以停"。
    返回 `{items, total, fetched, complete, stop_reason, truncated, stopped_early}`。
    **complete=False 时调用方不许把结果当全量**——尤其不许把"没找到"当成"不存在"。

    ★`stop_reason` 必须如实区分停因，别一律报成"接口空页"：
    调用方自己设了 `max_pages` 上限而停，跟接口抽风是两回事，混为一谈会让人去查根本没坏的接口。"""
    items, total, page, stopped, reason = [], None, 1, False, "到尾"
    while True:
        if page > max_pages:
            reason = "达到 max_pages 上限"
            break
        got, t = None, None
        for _ in range(retries + 1):
            got, t = fetch(page)
            if got:
                break                       # 拿到东西就不重试
        if t is not None:
            total = t
        if not got:
            reason = f"空页（重试 {retries} 次后仍空）"
            break
        items.extend(got)
        if on_page and on_page(got):
            stopped, reason = True, "命中即停"
            break
        if len(got) < page_size:
            reason = "到尾（末页不足页长）"
            break
        page += 1
    complete = stopped or (total is None) or (len(items) >= total)
    if complete:
        note = None
    elif reason.startswith("达到"):
        note = f"只拉到 {len(items)}/{total} 条——**是调用方设的上限所致**（{reason}），不是接口问题；要全量请调大上限"
    else:
        note = f"只拉到 {len(items)}/{total} 条（{reason}）——**别当全量用**"
    return {"items": items, "total": total, "fetched": len(items),
            "complete": complete, "stopped_early": stopped,
            "stop_reason": reason, "truncated": note}


def post_multipart(base: str, path: str, form: dict, files: dict, cookie: str, *,
                   extra_headers: Optional[dict] = None, timeout: float = 120) -> dict:
    """POST multipart/form-data（**不设 Content-Type**，httpx 按 files 自带 boundary）→ 解析后的 JSON。
    三处表格上传共用（subsidy fileProcess / ms apply/excel / campaign fileUpload）。
    form=普通字段 dict；files={"file":(name, bytes, mime)}；extra_headers 追加（如 campaign 的 X-Requested-With）。
    调用方自行取 cookie 传入（避免 jd_core←jd_auth 循环 import）与解析 data 字段。"""
    headers = {"Accept": "*/*", "User-Agent": DEFAULT_UA, "Origin": "https://yx.jd.com",
               "Referer": "https://yx.jd.com/", "Cookie": cookie}
    if extra_headers:
        headers.update(extra_headers)
    with bare_client(timeout=timeout) as c:
        r = c.post(f"{base}{path}", data=form, files=files, headers=headers)
    if r.status_code in (301, 302, 303, 307, 308):
        raise BlacklightError(f"{path} 被重定向（{r.status_code}）——登录态可能失效，请 yx_login")
    try:
        return r.json()
    except Exception as e:
        raise BlacklightError(f"{path} 未返回 JSON（HTTP {r.status_code}）") from e


# --------------------------------------------------------------------------- #
# 二次确认令牌
# --------------------------------------------------------------------------- #
def canon_num(v) -> str:
    """**数值进指纹前一律用它归一**，别直接 `str()`。

    `str()` 会让同一业务意图的不同写法产生不同令牌：`9` → "9"、`9.0` → "9.0"、`"9"` → "9"。
    dry-run 与真执行之间只要数值形态变一下（MCP JSON 往返、上游算了一次 float），令牌就对不上，
    **二次确认门就会挡住操作者本人**——2026-08-05 实证 `jzt_swa.budget_update` 因此连挡 10 次、
    跨 4 小时 0 成功；同日排查发现 bybt/ms/subsidy 另有 5 处同病（尚未咬人，属潜伏）。

    归一规则：能转 float 的 → 定点 4 位再去尾零（`32`/`32.0`/`"32"` → `"32"`，`32.70` → `"32.7"`）；
    转不了的原样 `str()`（None/空串/非数字 ID 都走这条，不改变原有行为）。
    ⚠️只用于**令牌**，不改发出去的 payload —— 线上字节保持原样。"""
    if v is None or isinstance(v, bool):
        return str(v)
    try:
        return f"{float(v):.4f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(v)


def canon_for_token(obj):
    """`canon_num` 的**递归版**：走 dict/list/tuple，把里面每个数值都归一，其余原样。

    为什么要有它：`canon_num` 只管标量。**令牌里塞整个 dict 时，`json.dumps` 会保留数值类型**，
    于是 `{'limit':5}` / `{'limit':5.0}` / `{'limit':'5'}` 仍然是三个令牌——
    2026-08-05 实测 `ware_edit._edit_token` 正是这样漏网的（上一轮只修了标量那 6 处）。

    用法：`confirm_token({... "sku": json.dumps(canon_for_token(sku_edits), sort_keys=True) ...})`
    ⚠️同样**只用于令牌**，不改发出去的 payload。"""
    if isinstance(obj, dict):
        return {k: canon_for_token(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [canon_for_token(v) for v in obj]
    if isinstance(obj, bool) or obj is None:
        return obj                     # 布尔/None 原样，别被 canon_num 变成字符串
    if isinstance(obj, (int, float)):
        return canon_num(obj)
    if isinstance(obj, str):           # 纯数字字符串也归一，"5" 与 5 才等价
        try:
            float(obj)
        except (TypeError, ValueError):
            return obj
        return canon_num(obj)
    return obj


def confirm_token(body: dict) -> str:
    """对将要发送的确切 body 取指纹，作为二次确认令牌（参数一变令牌就变）。
    ⚠️body 里的**数值**先过 `canon_num()`；**嵌套结构**用 `canon_for_token()`，
    否则 9 与 9.0（或 {'a':5} 与 {'a':5.0}）会得到两个令牌、把操作者本人挡在门外。"""
    raw = _json.dumps(body, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


_confirm_token = confirm_token  # 兼容旧名


class ConfirmGate:
    """写操作二次确认门：同一 path + 参数指纹作令牌。

        GATE = ConfirmGate("bybt/withdraw")
        token = GATE.token(id=apply_id, areaId=area_id)     # dry-run 回显
        GATE.check(confirm, id=apply_id, areaId=area_id)    # 真执行前校验，不符抛错

    ★**含数值参数的写路径请用 `body_token`/`check_body`，别用 `token`/`check`**：
    `token(**params)` 对**原始入参**取 `str()`，于是同一业务意图的不同写法会得到不同令牌
    （`999` vs `999.0` vs `"999"`）。2026-08-05 实证代价：`jzt_swa.budget_update`
    连续 10 次全被自己这道门拦下、跨 4 小时一次没通过——门没防住危险，只挡住了操作者本人。
    对照组 `status_update` 因为入参先 `int()` 归一过才进令牌，一次就通。
    `body_token` 对**最终要发送的 body** 取指纹（这也是 `confirm_token` 文档写的原意），
    body 组装过程天然会把 `float()/int()` 归一，同一意图必得同一令牌。
    """
    def __init__(self, path: str):
        self.path = path

    def token(self, **params) -> str:
        """按原始入参取指纹。**仅适用于入参已是规范化标量**（如已 int() 过的 id）。"""
        return confirm_token({"path": self.path, **{k: str(v) for k, v in sorted(params.items())}})

    def check(self, confirm: str, **params) -> None:
        if confirm != self.token(**params):
            raise BlacklightError(f"[{self.path}] 需二次确认：先用相同参数跑对应 *_dryrun 拿 confirm_token 再带 confirm。")

    def body_token(self, body) -> str:
        """按**将要发送的 body** 取指纹 —— 数值形态差异已被 body 组装归一，令牌稳定。"""
        return confirm_token({"path": self.path, "body": body})

    def check_body(self, confirm: str, body) -> None:
        if confirm != self.body_token(body):
            raise BlacklightError(f"[{self.path}] 需二次确认：先用相同参数跑对应 *_dryrun 拿 confirm_token 再带 confirm。")


# --------------------------------------------------------------------------- #
# 配置（config.json）—— 网关/收品池/各场域默认值，外置便于加频道不改代码
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def load_config() -> dict:
    path = os.path.join(_HERE, "config.json")
    with open(path, encoding="utf-8") as f:
        return _json.load(f)


def gateway(scene: str) -> str:
    """取某场域的业务网关 base URL。"""
    gws = load_config().get("gateways", {})
    if scene not in gws:
        raise BlacklightError(f"config.json gateways 缺 {scene}（有 {list(gws)}）")
    return gws[scene]


def scene_cfg(scene: str) -> dict:
    """取某场域的杂项配置块（默认 areaId、applySkuType 等）。"""
    return load_config().get(scene, {}) or {}


# --------------------------------------------------------------------------- #
# 写操作审计日志（append-only JSONL）—— 所有真写落一条，出事可追溯
# 路径：runtime/audit.log（env YX_AUDIT_LOG/BLACKLIGHT_AUDIT_LOG/BLACKLIGHT_HOME 可覆盖）。审计失败静默，绝不阻断业务。
# --------------------------------------------------------------------------- #
from blacklight.core import paths as _paths  # noqa: E402
AUDIT_PATH = _paths.audit_path()


def _trim(obj, maxlen: int = 1200) -> str:
    try:
        s = _json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        s = str(obj)
    return s if len(s) <= maxlen else s[:maxlen] + f"…<+{len(s) - maxlen}字符>"


def _operator() -> str:
    """当前操作人 PIN（懒 import jd_auth 避循环）。取不到返回 ''。"""
    try:
        from blacklight.core.auth import current_pin
        return current_pin() or ""
    except Exception:
        return ""


def pmap(fn, items, workers: int = 8):
    """**只读并发 map**（试算/校验加速用，保持输入顺序）。fn 须自己吞异常返回结果结构（否则 map 会抛）。
    各 HTTP 调用自建 client → 线程安全；写操作别用此（用带 confirm 的专用并发路径）。workers 上限 16。"""
    items = list(items)
    w = max(1, min(int(workers or 1), 16))
    if w <= 1 or len(items) <= 1:
        return [fn(x) for x in items]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=w) as ex:
        return list(ex.map(fn, items))


def _default_is_error(r) -> bool:
    """默认错误判定：None / 带 error|err 键 / ok 显式为 False。"""
    if r is None:
        return True
    if isinstance(r, dict):
        return bool(r.get("error") or r.get("err")) or r.get("ok") is False
    return False


def _default_key(x):
    if isinstance(x, dict):
        for k in ("skuId", "sku_id", "id"):
            if x.get(k) is not None:
                return str(x[k])
    return str(x)


def pmap_batch(fn, items, workers: int = 5, *, probe: int = 2, checkpoint: str = None,
               key=None, is_error=None, abort_window: int = 20, abort_rate: float = 1.0,
               chunk: int = 200, label: str = "batch", log=print) -> dict:
    """**长批量只读任务的标配跑法** —— 把「探针 / 熔断 / 断点续跑 / ETA」做成代码而不是纪律。

    2026-08-03 的教训：一个 1153 条的脚本因**自己一行 bug** 全部失败，却逐条吞异常跑到"完成"，
    42 分钟后才发现——而第一条结果在第 10 秒就已经是错的。同日另一个 1308 条的任务撞平台瞬时错误，
    **已完成大半却全损**（没有断点）。这两类损失都与"危险"无关，纯粹是**昂贵**，
    而工具箱原本只为"危险"（写操作）建了防护。见 [[probe-before-batch-any-script]]。

    四道机制：
      1) **探针**：先跑 `probe` 条，**打印第一条完整结果**（不是进度数字）。探针全错 → 立即抛，不进主循环。
      2) **ETA**：用探针实测速率 × 剩余条数，启动前就回显预计耗时（>2 分钟的任务应先报价再跑）。
      3) **熔断**：主循环前 `abort_window` 条错误率 ≥ `abort_rate` → 立即中止（默认"前 20 条全错即停"）。
      4) **断点**：给 `checkpoint`（.jsonl 路径）则每 chunk 追加落盘；重跑时自动跳过已完成的 key。

    fn 须自己吞异常并返回结果结构（与 pmap 一致）。返回
    {rows, done, skipped_resumed, errors, aborted, reason, seconds, eta_seconds}。"""
    import time as _t
    items = list(items)
    key = key or _default_key
    is_error = is_error or _default_is_error

    done_keys, rows = set(), []
    if checkpoint and os.path.exists(checkpoint):                 # 断点：捡回上次已完成的
        with open(checkpoint, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = _json.loads(line)
                except ValueError:
                    continue
                rows.append(r)
                done_keys.add(key(r))
        if done_keys:
            log(f"[{label}] 断点续跑：已完成 {len(done_keys)} 条，跳过")
    todo = [x for x in items if key(x) not in done_keys]
    if not todo:
        return {"rows": rows, "done": len(rows), "skipped_resumed": len(done_keys),
                "errors": 0, "aborted": False, "reason": "全部已完成(断点)", "seconds": 0}

    def _flush(part):
        if not checkpoint:
            return
        with open(checkpoint, "a", encoding="utf-8") as f:
            for r in part:
                f.write(_json.dumps(r, ensure_ascii=False, default=str) + "\n")

    # ---- ① 探针 ----
    t0 = _t.time()
    head = todo[:max(1, probe)]
    probe_rows = [fn(x) for x in head]
    probe_sec = _t.time() - t0
    bad = [r for r in probe_rows if is_error(r)]
    log(f"[{label}] 探针 {len(head)} 条 / {probe_sec:.1f}s，首条结果：")
    log("  " + _json.dumps(probe_rows[0], ensure_ascii=False, default=str)[:600])
    if len(bad) == len(probe_rows):
        raise BlacklightError(
            f"[{label}] 探针 {len(probe_rows)}/{len(probe_rows)} 全部失败，已中止（未进入主循环）。"
            f"首条：{_json.dumps(bad[0], ensure_ascii=False, default=str)[:300]}")
    rows += probe_rows
    _flush(probe_rows)

    # ---- ② ETA（探针实测速率）----
    rest = todo[len(head):]
    per = (probe_sec / max(len(head), 1)) / max(1, min(int(workers or 1), 16))
    eta = per * len(rest)
    log(f"[{label}] 剩余 {len(rest)} 条，按探针速率(并发{workers}) 预计 {eta/60:.1f} 分钟")

    # ---- ③④ 主循环：熔断 + 断点 ----
    err_n, aborted, reason = len(bad), False, ""
    for i in range(0, len(rest), chunk):
        part = pmap(fn, rest[i:i + chunk], workers)
        rows += part
        _flush(part)
        err_n += sum(1 for r in part if is_error(r))
        # 熔断：只看**固定的前 abort_window 条**（不是当前累计），够数了就判一次
        if len(rows) >= abort_window:
            head = rows[:abort_window]
            head_err = sum(1 for r in head if is_error(r))
            if head_err >= abort_window * abort_rate:
                aborted = True
                reason = (f"前 {abort_window} 条里错了 {head_err} 条"
                          f"（≥{abort_rate:.0%}）→ 熔断中止，已跑 {len(rows)}/{len(todo)}")
                break
        log(f"[{label}] {len(rows)}/{len(todo)+len(done_keys)}  错误 {err_n}")
    if aborted:
        log(f"[{label}] ⚠️ {reason}")
    return {"rows": rows, "done": len(rows), "skipped_resumed": len(done_keys),
            "errors": err_n, "aborted": aborted, "reason": reason,
            "seconds": round(_t.time() - t0, 1), "eta_seconds": round(eta, 1)}


_AUDIT_LOCK = _threading.Lock()


AUDIT_MAX_BYTES = 32 * 1024 * 1024        # 超过就轮转一次（2026-08-24：当时已 2.2MB 且**从无任何轮转**）


def _rotate_audit_if_big() -> None:
    """审计日志只保留 `audit.log` + `audit.log.1` 两代。

    ★**不能简单删**：`auditscan` / `rulestat` 拿这份日志当**回归源**（活体标记核对、
      失败率突变、写路径使用度全靠它），删了等于把回归依据丢掉。
      所以轮转出去的那一份仍会被 `auditscan.load()` 读回来（见其 docstring）。
    轮转失败静默 —— 审计从不阻断业务。
    """
    try:
        if os.path.getsize(AUDIT_PATH) < AUDIT_MAX_BYTES:
            return
        prev = AUDIT_PATH + ".1"
        if os.path.exists(prev):
            os.remove(prev)
        os.replace(AUDIT_PATH, prev)
    except Exception:
        pass


def audit_write(scene: str, action: str, request, response, ok: Optional[bool] = None,
                operator: Optional[str] = None) -> None:
    """把一次真写 append 到审计日志（JSONL）。含**操作人 PIN**（多用户可追责）。审计不阻断业务 → 全程吞异常。
    **加锁**：并发提交(如 bybt enroll_bulk 多线程)时，避免多线程 append 交错/丢行。"""
    try:
        rec = {"ts": _time.strftime("%Y-%m-%d %H:%M:%S"),
               "operator": operator if operator is not None else _operator(),
               "scene": scene, "action": action, "ok": ok,
               "verified": write_verified(scene, action).get("verified"),   # 该写路径是否活体验证过
               "request": _trim(request), "response": _trim(response)}
        with _AUDIT_LOCK:
            _rotate_audit_if_big()
            with open(AUDIT_PATH, "a", encoding="utf-8") as f:
                f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 写路径活体验证矩阵：verified=已活体真跑通(可自动)；False=契约齐但未活体(应小批/转人工)
# 无人值守放手前查此表；写工具/审计会带上此标记，Agent 可据此决定自动 or 转人工。
# --------------------------------------------------------------------------- #
WRITE_VERIFICATION = {
    "campaign.apply": {"verified": True, "note": "formSet驱动,券类1080420实证;官方直降2497067真报67(2026-07-16)+wait回执自验证(totalCount涨够+get_sku_status逐SKU,规避索引/生效延迟误判)"},
    "campaign.withdraw": {"verified": True, "note": "batchQuit实证;跨店满减(平台活动)退出实证2026-07-13"},
    "campaign.table_withdraw": {"verified": True, "note": "表格退出,1327跨店满减status3成功2026-07-13"},
    "campaign.table_apply": {"verified": True, "note": "★2026-08-19 活体：官方直降 2497067 报 94 款(探针2+批量92)，平台回执 total/success/fail 齐、failLink 空，get_sku_status 全量回读 94/94 有效报名。★未活体验证，小批量转人工确认后再铺开。2026-08-17 加：uploadType=2(退出是100)+templateType=2(商品SKU/优惠比例%)，两个坐标都是**只读反查**定的——downloadApplyTemplate 对 uploadType 完全不校验(0/1/2/3/10/20/50/100/101/200 全回同一份退出模板)，靠 getFileUploadRecord 逐 uploadType 查历史才定死(2 有 2026-06-29 的 478 行记录，100 是退出)。比例=降百分之几的整数1~90，模板备注原文『最终优惠=前台价×上传比例』；0.95 平台报 For input string，95 会降95%。dry-run/模板/历史/护栏均已通，缺真上传一次。"},
    "subsidy.apply": {"verified": False, "note": "★已停用(batch/create缺报名模式→会报错模式,报名无效)。国补真报名走 subsidy.table_apply。apply()直接raise引导。"},
    "subsidy.apply_batch": {"verified": False, "note": "★已停用(同 apply)。走 subsidy.table_apply。"},
    "subsidy.withdraw": {"verified": True},
    "subsidy.withdraw_batch": {"verified": True},
    "subsidy.table_apply": {"verified": True, "note": "Excel上传≤10万;2026-07-14 15%池真报8/8成功+wait回执自验证(fileProcess data.items[].processStatus/successCount/failUrl)"},
    "markettool.delete": {"verified": True, "note": "删券促"},
    "markettool.strip_except": {"verified": True,
                                "note": "批量退券(保留清单语义)。2026-08-10 探针：规划侧已活体"
                                        "(10186130760892，档位过滤+campaign去重把 229 张压到 7 次写，"
                                        "执行后 yx 回读 239→194 张、5.00 档整档消失、最高降到 4.00)。"
                                        "★2026-08-24 按**审计事实**转 True：`audit.log` 里本路径已成功 "
                                        "**40 次**（最近 2026-08-24 10:28:28），pmap_batch 执行路径早已活体，"
                                        "是矩阵没跟上（auditscan 连续报『应转True』）。"
                                        "⚠️它现在还会过 core/protected 禁碰清单（同日接入）。"},
    "promo.withdraw_all": {"verified": True, "note": "统一退出"},
    "bybt.apply": {"verified": True, "note": "真报名applyId141671277 byte-match;2026-07-14本人82款真报76成功"},
    "bybt.enroll_bulk": {"verified": True, "note": "逐条apply编排(取代已删的apply_batch),2026-07-14本人82款真报76成功;卡控透出+归属过滤+严格<建议价定价"},
    "bybt.withdraw": {"verified": True},
    "bybt.withdraw_batch": {"verified": True, "note": "★2026-08-07 活体：审计 1 次真写成功。未活体→小批"},
    "ms.table_apply": {"verified": True, "note": "秒杀真报真退2026-07-13;便宜包邮用户真报"},
    "ms.reduce_price": {"verified": True,
                        "note": "2026-08-11 失效预警处置真跑：探针10115564065860 22.90→22.88 回执'跟价提报成功';提交≠生效需过审"},
    "ms.withdraw": {"verified": True, "note": "GET quit实证"},
    "ms.withdraw_batch": {"verified": True},
    "ms.apply": {"verified": False, "note": "便宜包邮逐SKU saveApply契约齐,未活体→批量优先 table_apply"},
    "product.update_price": {"verified": True, "note": "改价 updatePrices;2026-07-20 首改实证 sku10131084025705 京东价10.9→32.7 success+读回确认+审计落库(operator=wangruihan9)。★2026-08-03 批量实证:49个SKU分4批(探针2+21+9+15)全部success,回读47/47价格生效、实际毛利与预测偏差-0.0%;并实证**改价后国补/直降按新京东价实时重算**(不冻结在报名basePrice),故提价无需重新报名。★★2026-08-14 实证**涨价对『到手价』不是 1:1 传导**(正因为上面那条实时重算):5款回读 100%/85%/76%/72%/**0%**;0% 那款是**便宜包邮锁死前台价**(origBench 5.00→6.56 而 reward 0.51→2.07,bench 恒 4.49,毛利分文未动)⇒ 定目标价必须先过 `product._reprice_transmit()`,别拿裸毛利算。"},
    "product.update_title": {"verified": False, "note": "改商品名/长标题 updateProducts(≤60字,SPU级);契约齐(用户抓包2026-07-21),未活体→首次务必单款+读回验证。非秒杀短标题。"},
    "product.update_status": {"verified": True, "note": "上/下架 updateProductStatus(operation up/down,SPU级,客户端立即生效);2026-07-21 本地cookie-only路径受控往返实证 productId 10035541059586 down→up 均 code200 success(无h5st),payload字节对齐用户抓包。批量仍走 dryrun 门。★2026-07-23:对『从未上架(state=5)』商品 up → 回执 success=False+『从未上架商品首次上架需审批通过,点击跳转审批中心查看』=触发首次上架审批(非失败,商品留state=5待审批通过转在售);审批中心 osw.jd.com/approvals 暂无接口。"},
    "selection.claim": {"verified": True, "note": "公共商品池认领 modifyProject(opt:1,选品CMS/api.m selectioncms);组装体与用户抓包提交逐字段对齐。★2026-07-24 MCP代码路径首次实盘:VPROJ178479645221679c0(束口收纳袋5SKU,猎人yuemeishuai1/猎枪xushichuang1,自动定价×1.8→.99)→回执{result:0,创建成功},detail status 0→40+三角色回填(prey/hunter/shotgun),进选品任务列表stage②寻源待猎人确认。定价用采购价上限×1.8→.99;price_map可选(缺则自动);首单核对回执。"},
    "selection.reject": {"verified": False, "note": "公共商品池驳回 refuseVenderTask(vprojId/refuseType 20/30/40/checkMsg);契约齐(用户抓包2026-07-22)未活体→首次单标的+核对回执。"},
    "selection.stock": {"verified": True, "note": "公共商品池铺货 copyWare(GET selectioncms,body={projectId,spuIdx,isCheck});isCheck=True平台预检(不真铺货,ready才出token)/False真铺货;前置需完成采纳(adopt)。★2026-07-23实盘真铺货2品(VPROJ17846378448878fe0→pid10035567326849/VPROJ17841027162760a40→pid10035567356761)成功,验证=商品列表已下架tab出现新品 state=5从未上架/下架时间=刚铺货时刻。之后上架(product.update_status 'up')触发首次上架审批。"},
    "jzt_swa.budget_update": {"verified": True, "note": "京准通全站营销改日预算 /swa/budget/update(campaignBudgetUpdateCommandList)。★2026-08-05 活体验证:取一条**已暂停且零花费**的推广(campaignId 8554187688)488→500→还原488,两次回执 successList 命中、ad_list 读回逐次确认。dayBudget=0 表示不限,区间 100~9999999。⚠️此前 10 次全被 confirm 门拦下 0 成功,根因是令牌取原始入参 str()(999≠999.0),同日改为对规范化命令列表取指纹(ConfirmGate.body_token)。"},
    "jzt_swa.bid_update": {"verified": True, "note": "★2026-08-13 活体：审计 2 次真写成功。京准通全站营销改出价(目标成交投产比) /swa/bid/update；payload 反解自 bundle(adGroupBiddingUpdateCommandList: id=**groupId**,biddingType 8192/controlType 2/target 22)，未活体→首次单条。⚠️出价修改次数有平台上限(行里 bidChangeLimit/priceChangeCount)，改废了当天就调不动了。"},
    "jzt_swa.skublack_update": {
        "verified": True,
        "note": "京准通 SKU 黑名单 /swa/skublack/update（2026-08-10 从用户抓包逆向）。"
                "★★**是全量覆盖不是追加**：整个 skublack 只有 query/update，"
                "add/delete/remove/save/cancel 全 404 —— 没有删除接口，取消拉黑只能提交不含它的集合，"
                "故 update 必为覆盖。**照抓包形状只传新增会清空该单元已有黑名单。**"
                "本模块所有写路径先 query 再求并集提交全集（两种语义下都对），并自动回读比对。"
                "读路径已活体（query 返回 blackSkuList/effectiveSkuList），**写路径未活体**，首次先单条探针。★**写路径 2026-08-18 活体通过**：30 个单元真写(新增拉黑 34 款/放出 32 款)，每单元执行后自动回读比对 30/30 落地一致；事后独立复扫 89 单元(覆盖率 1.0、失败 0)，黑名单 408→410，计划内未生效 0。⚠️限流实测：单元间隔 2.5s 仍会在第 ~10 个撞「操作次数已超过上限」，靠跨分钟重试自愈 ⇒ 批量必须带重试，别当失败。"},
    "jzt_swa.status_update": {"verified": True, "note": "京准通推广启停/删除 /swa/status/update {ids,status:1停2启3删,campaignType}；**stop(1) 已活体验证**(2026-08-05：探针1条+批量25条，回执 successList 全中，ad_all 读回 26/26 statusCode 2→1)，批量上限 MAX_WRITE_BATCH=50 内一次成功。start(2) 同接口反向、未单独验；**status=3 删除不可逆，仍禁止自动删**。"},
    "pic.imgzone_upload": {
        "evidence": "2026-08-17 活体：图片空间上传并回读到 imageId（审计日志起于 2026-07-13 且 @audited 是事后补的，故 audit.log 里没有它的成功记录；下次真跑一次即由 auditscan 自动确认）",
        "verified": True,
        "note": "图片空间上传 dsm.media.image.imageApiService.uploadImage（sff 网关 + cookie，"
                "JSON 里塞 base64）。2026-08-17 活体：1x1 探针 + AI 生图各一张，均 code=200、"
                "CDN GET 200 验活通过、随后删除，空间已复原。**上传不走 upload.shop.jd.com**——"
                "那个域认商家后台登录态、我们进不去，但页面按钮实际也没打它。",
    },
    "pic.imgzone_delete": {
        "evidence": "2026-08-17 活体：删除 3 张自建测试图成功（审计日志起于 2026-07-13 且 @audited 是事后补的，故 audit.log 里没有它的成功记录；下次真跑一次即由 auditscan 自动确认）",
        "verified": True,
        "note": "图片空间删图 batchDelete。2026-08-17 活体删除 3 张自建测试图成功。"
                "⚠️`imageIds` 要**逗号分隔字符串**，传数组一律 456「无效的字符串」（报错完全不提这点，"
                "照 JS 里的数组写法抄会一直失败）。**不可恢复**，且删掉被商品引用的图会导致商品展示异常。",
    },
    "osw.material_bind": {
        "evidence": "2026-08-18 活体：挂满 5 个位并逐位回读 status=3/materialId 已落库（审计日志起于 2026-07-13 且 @audited 是事后补的，故 audit.log 里没有它的成功记录；下次真跑一次即由 auditscan 自动确认）",
        "verified": True,
        "note": "商品素材挂位 dsm.media.material.imageRelations.batchBind（ware-material-jdm 子应用，"
                "sff 网关 appId=BD2QSA2XUESRKXAL1QKQ）。"
                "★2026-08-18 活体：SKU 10232766690176(SPU 10035824677493) 由 llm-gw 生图 → imgzone 上传 → "
                "挂满 5 个位（31白底/36透明/32场景×2/33卖点），逐位回读 status=3 审核中、materialId 已落库。"
                "⚠️**imgUrl 必须是 jfs 相对路径**，传完整 CDN URL 报「无效的图片」。"
                "⚠️**图必须 800×800**、31/32/33 用 JPG、36 用带 alpha 的 PNG；1254×1254 PNG 挂 31 报"
                "「图片格式不符合规则」且不提该改成什么 —— 先过 material.normalize_image()。"
                "⚠️服务端把逐条错误塞在 **HTTP 200** 的返回数组里（errorMsg），只看 code 会把失败当成功"
                "（首次探针就是这么被抓住的）。⚠️回读 order 是 1 起，发的 imgOrder 是 0 起。"
                "⚠️existNoReplace=False 会覆盖已有素材。",
    },
    "osw.material_autofill": {
        "verified": False,
        "note": "单 SKU 素材补齐（生成→质检→上传→挂位，material_gen.autofill）。"
                "2026-08-24 审查发现：它是**全仓唯一有 ConfirmGate 却没挂 @audited** 的真写路径 —— "
                "写得成，但事后查不到是谁在什么时候用什么参数写的。当日补上装饰器并登记本条。"
                "verified=False：本次只补审计与登记，**没有活体验证过这条路径**；"
                "等审计日志里出现第一条成功记录、由 auditscan 报『应转True』时再人工确认改真。",
    },
    "osw.material_sellpoints": {
        "evidence": "2026-08-18 活体：SKU 10232766690176 存 3 条，raw=success，回读 matched=True（审计日志起于 2026-07-13 且 @audited 是事后补的，故 audit.log 里没有它的成功记录；下次真跑一次即由 auditscan 自动确认）",
        "verified": True,
        "note": "通用卖点保存 dsm.media.text.shortTitleSeller.save "
                "{apiWareTextMaterialInfo:{sellPoint:[...],skuIds:[...]}}。"
                "★2026-08-18 活体：SKU 10232766690176 存 3 条，raw=success，回读 matched=True。"
                "⚠️**覆盖式**，不是追加。条数上限取服务端 `sellMaxNum`（实测 3）别硬编码；单条 ≤8 字。"
                "⚠️卖点是**对外承诺**：生成侧已禁绝对化用语/疗效/价格促销/品牌名/赠品承诺，但仍须人工过目。",
    },
    "osw.material_reuse": {
        "evidence": "2026-08-18 活体：兄弟 SKU 素材复用（解绑+同图 batchBind）（审计日志起于 2026-07-13 且 @audited 是事后补的，故 audit.log 里没有它的成功记录；下次真跑一次即由 auditscan 自动确认）",
        "verified": True,
        "note": "兄弟 SKU 素材复用 = batchUnBindMaterial（解绑目标位）+ batchBind（同一张 imgUrl）+"
                "可选 shortTitleSeller.save（卖点）。分组按**销售属性**（平台 handleBatchUse 口径："
                "saleAttrInfos[index==N] 取值相同），**不是按主图**——实撞反例：同 SPU 带木盖/无木盖两款"
                "主图完全一样，照主图复用会把无木盖的图挂到带木盖的 SKU 上。"
                "分组口径 2026-08-18 定为**外观签名**（销售属性去掉尺寸维度后比对；"
                "用户口径：颜色/款式差异明显、尺寸差异不明显，同外观取尺寸最大那款做源）。"
                "★**批处理路径已活体**：一次 `batchBind` 打到整个外观组、`existNoReplace=True` 只填空位，"
                "在 18 个 SPU 的批量补齐里持续跑通。"
                "★**standalone `reuse()` 2026-08-19 活体（两种模式都跑通）**：SPU 10035631435288、"
                "源 10231005118547 → 同外观组 3 个尺寸(118551/118543/118539)，"
                "`replace=False` 补空位 + `replace=True`(batchUnBindMaterial 再挂) 均 ok，独立回读 4/4 一致。"
                "同日修掉它三个静默失败（都会让你以为成功）："
                "① `material_types` 给素材位号(3201/3202)时永远匹配不上 `materials` 的线材键(32)，"
                "items 空转而 `all([])` 返回 **ok=True**——现在空 items 显式 skipped；"
                "② items 不带 order，两张场景图会挤进同一个位；"
                "③ ★**读回来的 order 是 1-based、写进去的 imgOrder 是 0-based**，"
                "照 0-based 还原会把场景图1 判成场景图2。",
    },
    "osw.material_ai_generate": {
        "verified": False,
        "note": "商品素材 AI 批量生成 dsm.media.service.materialTaskApiService.createTask "
                "{param:{taskType:5(AiBatchGenerateTask), businessIdList:[skuId], "
                "contentMap:{needMaterialType:'36,31,32'}}}，前端硬限 500 SKU/次。"
                "平台 AI 只兜 36透明图/31白底图/32场景图（getAiBatchMaterialType 活体返回 [36,31,32]）。"
                "未活体。⚠️taskStatus=70 是**待采纳**不是生效；发起前先看在途任务，"
                "在途锁会挡掉后发请求（同 bybt 并发报名）。",
    },
    "pic.ai_generate": {
        "verified": False,
        "note": "平台按图生评价文案 jxzy_aievalandsales_generateAIEval（契约来自 bundle，从未活体）。"
                "★2026-08-18 试跑**失败**：`code=-1`，换商品主图/不同 SKU 都一样。"
                "所以「同品采不到文案就用平台 AI 兜底」这条兜底路**目前是断的**，"
                "别在方案里当它可用；要么补文案来源，要么先把这个接口的真实入参抓下来核对。",
    },
    "pic.import_eval": {
        "verified": True,
        "note": "带图评价提交 jxzy_aievalandsales_importImageEval（契约反解自 picstart bundle："
                "{skuId, evalContent, images:'逗号URL', isCheck:false, skuTaskIndex}）。"
                "★**2026-08-18 单条探针活体**：SKU 10166147873996，用**同品采集来的真实买家晒单图+真实文案**"
                "（采集器 vendor/jd_good_reviews 首次实跑，1 条 success/28s），"
                "import_rows 回执 success_count=1，回读 task_id=jxzy69ae26081814507a58316、"
                "taskStatus=1 机审中、文案与图片与提交一致。`skuTaskIndex=0`（该 SKU 首条）可用，"
                "此前挂着的\"skuTaskIndex 语义待验\"一并解掉。"
                "⚠️提交前必过 plan_import 的**图片来源闸**（已用图/商品主图）——机审拒绝里 59.2% 是图片撞车。"
                "★受理≠生效：taskStatus 要走到 **330** 才算生效（中间 1机审→3/4、6人审、210/310评价中台处理）。"
                "★每 SKU 有条数上限（接口回 maxImageTaskNum，实测 10），skuTaskIndex 要从已建条数往后排，"
                "排重了会顶掉已有的那条。评价文本 ≤1000 字、图只收 URL 不收 base64。"},
    "ware.save": {"verified": False, "note": "编辑商品保存 gmall.jd.com/api/ware/save(整品覆盖);仅放开 短标题/启用(SKU)+24h限购/运费模板/时效模板(SPU);重建body与用户抓包save逐字段结构一致(2026-07-22无编辑重建35顶层键+skuAttr全一致+不变量守门通过)但未活体真写→首次单字段小改+ware_load读回。⚠️6个unsourced顶层flag(isIOUSPay/isCheckCode/isDangerGoods/isWeChatStock/noShow/ztSale)用观测默认0/空回填,危险品/首付等边缘品慎用。"},
}


def write_verified(scene: str, action: str) -> dict:
    """查某写路径的活体验证状态。verified: True可自动/False未验证应小批转人工/None未登记。"""
    return WRITE_VERIFICATION.get(f"{scene}.{action}", {"verified": None, "note": "未登记"})


def audited(scene: str, action: str):
    """装饰真写函数：成功/异常都记一条审计（含操作人 PIN + 写路径验证状态）。args/kwargs 与返回值均入日志。
        @audited("ms", "table_apply")
        def table_apply(...): ...
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrap(*args, **kwargs):
            op = _operator()
            try:
                res = fn(*args, **kwargs)
            except Exception as e:
                audit_write(scene, action, {"args": args, "kwargs": kwargs}, {"error": str(e)}, ok=False, operator=op)
                raise
            ok = None
            if isinstance(res, dict):
                ok = res.get("success", res.get("executed", res.get("applied", res.get("withdrawn"))))
            audit_write(scene, action, {"args": args, "kwargs": kwargs}, res,
                        ok=bool(ok) if ok is not None else None, operator=op)
            return res
        return wrap
    return deco
