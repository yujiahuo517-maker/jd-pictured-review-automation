"""**写路径策略层**：限速 / 节流分类 / 退避重试。

## 为什么要有这一层（2026-08-19 从 2307 条审计定死）
`core` 此前只到「传输」为止（`make_client` / `json_post`），**策略是空的**：

- 全部失败 556 条里，**真·不稳定 256 条（占全部调用 11.1%）100% 是限流/并发**；
  契约漂移导致的失败 **0 条**。
- 而这 256 条的处置知识散在四个地方、写法各不相同：
  `ms.py` 自己实现了 `_post(retries=5, backoff)`；
  `bybt.py` 只把「改 concurrency=1 串行重试」写进 **docstring 让人记得**；
  `client.py` 把节流写进注释、靠换用 `table_apply` 绕开；
  `subsidy` / `markettool` **什么都没有**。
- 于是同一个修复在 2026-08-19 做了两遍（bybt 一遍、campaign 一遍），
  而 `subsidy.table_apply` / `swa.skublack_update` 至今各自为战。

⇒ 传输已经收口了，策略没有。这个模块就是把**已经写了三遍的东西**收口。

## ★核心结论：不变量是「间隔秒数」，不是「并发数」
按日统计 `bybt.apply` 的**中位请求间隔** vs 撞卡控率，零例外：

| 中位间隔 | 天数 | 调用 | 撞「正在报名中」 |
|---|---|---|---|
| **≥ 4.0s** | 9 | 573 | **0%** |
| ≤ 2.0s | 6 | 479 | **35~44%** |

`concurrency=1` **只是间隔在某台机器上的代理变量**——串行间隔 = 单次请求往返延迟，
机器越快 / 网络越近，间隔越小。所以「必须 concurrency=1」这条知识**换台机器就失效**
（实测：同事机器上失败率更高）。护栏必须写在**间隔**上才是机器无关的。

同理 `campaign.batchApply`：2026-08-17 那 188 次调用中位间隔 **0.0 秒**、
103 次挤在同一秒 ⇒ 66 次撞「操作中，请勿频繁操作」。

## ★★主动限速 vs 反应式退避：按「平台报不报数」选，别混用
| 平台行为 | 该用什么 | 例子 |
|---|---|---|
| **只说"操作太频繁"、不报数** | **主动限速 `pace`**（间隔由审计实测定） | `bybt.apply` 4.0s、`campaign.batchApply` 2.0s |
| **明确告诉你"还要等 N 秒"** | **反应式退避**（`throttle_wait` 读文案里的 N） | `upload.file` |

2026-08-19 上线当天就撞了这个坑：`upload.file` 按一次观察到的「再等 81 秒」
把 `min_interval` 设成 100s，结果平台那次只要 **2 秒**，我的闸硬压了 82.9s，
一次 2 款的上传拖了 ~13 分钟。**猜的常数打不过平台自己报的数。**
⇒ 会报数的路径把 `min_interval` 设小（够防背靠背即可），让退避去读平台的数。

## 三个原语
- `pace(key, min_interval)` —— 进程内按 key 限速，**闸放在被调用方**，
  不管调用方怎么循环都生效（不能指望调用方记得别循环）。
- `is_throttle(text, scene)` —— 节流文案分类。**撞节流 ≠ 部分失败**：
  平台是整批拒收，一条没落地，可以干净重试；当成部分失败去做增量补报会重复提交。
- `retry_throttled(fn, scene)` —— 只对节流文案退避重试，其它错误立即上抛。
  ⚠️**别对「写且非幂等」的调用无脑套这个**——先确认平台是"整批拒收"语义
  （见 `THROTTLE_RULES` 每条的 `clean_reject`）。不是的话走「回读证实没落地再重投」，
  例如 `bybt.enroll_bulk` 的 `retry_rounds`。
"""
from __future__ import annotations

import re
import threading
import time as _time
from typing import Callable, Optional

from .base import BlacklightError

# --------------------------------------------------------------------------- #
# 一、各写路径的节流策略（数值全部来自审计实测，改之前先看 audit 数据）
# --------------------------------------------------------------------------- #
#   min_interval : 两次调用之间的最小间隔（秒）。**机器无关的不变量**。
#   hints        : 平台节流文案（命中即判为"瞬时节流"，可重试）。
#   clean_reject : True=撞上时平台整批拒收、一条没落地 ⇒ 可直接原样重试；
#                  False=可能已部分落地 ⇒ **禁止盲目重试**，必须回读证实后再重投。
#   backoff      : 退避基数（秒），第 n 次等 backoff*n；文案里带秒数则优先用文案的。
THROTTLE_RULES = {
    # 百补报名：卡控按账号串行判定。实测 ≥4.0s 撞 0%(573次) / ≤2.0s 撞 35~44%(479次)。
    # ⚠️clean_reject=False：「正在报名中」返回失败但**可能异步已报上**（回执会偏低），
    #   所以 bybt 走的是「verify 证实没报上再重投」，不是盲目重试。
    "bybt.apply": dict(min_interval=4.0, backoff=5.0, clean_reject=False,
                       hints=("正在报名中", "无需重复点击")),
    # 官方直降 batchApply：2026-08-17 中位间隔 0.0s、103 次挤同一秒 ⇒ 66 次节流(35%)。
    "campaign.batchApply": dict(min_interval=2.0, backoff=3.0, clean_reject=True,
                                hints=("请勿频繁操作", "操作中，请勿")),
    # 国补表格上传：与 campaign 表格上传**共用同一个文件上传闸**（跨网关！mcpman vs mac）。
    # 2026-08-19 实测：直降上传成功后立刻传国补 → 「多个文件上传需要再等81秒」。
    # taskId=None + success=False ⇒ 文件没进队列，一条没落地，可干净重试。
    #
    # ★★min_interval 故意设得很小（5s），**不要按那个 81 秒去设主动限速**：
    #   这条路径的节流窗口是**动态**的，而且平台会在文案里**明确告诉你还要等几秒**。
    #   2026-08-19 首版按 100s 主动限速，当天就打脸——平台说「再等 **2** 秒」，
    #   我的闸却硬压了 82.9s，一次 2 款的上传拖了 ~13 分钟。
    #   ⇒ **平台沉默的路径才用主动限速（pace）；平台会报数的路径用反应式退避
    #     （throttle_wait 读文案里的秒数）**，它比我们猜的常数准。
    "upload.file": dict(min_interval=5.0, backoff=20.0, clean_reject=True,
                        hints=("多个文件上传需要再等", "请稍后再上传")),
    # 全站营销黑名单查询/更新：实测 spacing=0.6s 挂 38%，1.2s + 跨分钟重试可自愈。
    "swa.skublack": dict(min_interval=1.2, backoff=65.0, clean_reject=True,
                         hints=("操作次数已超过上限", "请一分钟后")),
    # 秒杀读路径的瞬时错（ms.py 原有 retries=5/线性退避，收口到这里）
    # ★"拼命加载中" 是 ms 域实测最常见的那条（原来只写在 yx/ms.py 的私有常量 _TRANSIENT_MS 里，
    #   等于同一份知识两处维护）。2026-08-24 收编，退避曲线与原 _post 完全一致（2/4/6/8s）。
    "ms.read": dict(min_interval=0.0, backoff=2.0, clean_reject=True,
                    hints=("系统繁忙", "请稍后重试", "网络异常", "拼命加载中")),
    # 删券促：逐张删之间留 0.4s（原来是 yx/markettool.py 里一句裸 sleep(0.4)）
    "markettool.delete": dict(min_interval=0.4, backoff=2.0, clean_reject=True,
                              hints=("请勿频繁操作", "系统繁忙", "请稍后重试")),
    # 全站营销黑名单**查询**侧：按单元逐个查，间隔由调用方给（默认 20s，实测串行也会撞）
    "swa.skublack_query": dict(min_interval=0.0, backoff=65.0, clean_reject=True,
                               hints=("操作次数已超过上限", "请一分钟后")),
}

#: 文案里自带「还需等 N 秒」时优先采信它（平台给的比我们猜的准）
_SECONDS_IN_TEXT = re.compile(r"(\d+)\s*秒")


def rule(scene: str) -> dict:
    """取某写路径的节流策略；未登记的返回空策略（不限速、不重试）。"""
    return dict(THROTTLE_RULES.get(scene) or {})


# --------------------------------------------------------------------------- #
# 二、限速：进程内按 key 排队
# --------------------------------------------------------------------------- #
_LOCKS: dict[str, threading.Lock] = {}
_LAST: dict[str, float] = {}
_REG_LOCK = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _REG_LOCK:
        if key not in _LOCKS:
            _LOCKS[key] = threading.Lock()
        return _LOCKS[key]


def pace(key: str, min_interval: Optional[float] = None) -> float:
    """**限速闸**：保证同一 `key` 的两次调用间隔 ≥ `min_interval` 秒，返回实际睡了多久。

    `min_interval` 留空则取 `THROTTLE_RULES[key]['min_interval']`；都没有则不限速。

    ★**这道闸必须放在被调用方**（域函数内部），不能放在调用方：
      2026-08-17 就是因为"指望调用方记得别逐款循环"，结果 188 次调用挤出 0.0s 中位间隔。
    ★补的是「上一次**开始** → 这一次**开始**」的间隔，所以请求本身耗时越长睡得越少；
      本机成本常常≈0，**在快机器上才真正补出等待**——这正是它机器无关的原因。
    ⚠️进程内有效。多进程/多机并发跑同一账号仍会互相打架（目前没有这种用法）。
    """
    gap = min_interval if min_interval is not None else (THROTTLE_RULES.get(key) or {}).get("min_interval")
    gap = float(gap or 0.0)
    if gap <= 0:
        return 0.0
    with _lock_for(key):
        prev = _LAST.get(key)
        slept = 0.0
        if prev is not None:
            wait = gap - (_time.monotonic() - prev)
            if wait > 0:
                _time.sleep(wait)
                slept = wait
        _LAST[key] = _time.monotonic()
        return slept


def reset_pace(key: str = None) -> None:
    """清掉限速状态（仅测试用；`key=None` 清全部）。"""
    with _REG_LOCK:
        if key is None:
            _LAST.clear()
        else:
            _LAST.pop(key, None)


# --------------------------------------------------------------------------- #
# 三、节流分类
# --------------------------------------------------------------------------- #
def is_throttle(text, scene: str = None) -> bool:
    """判断一段回执/异常文案是不是**瞬时节流**（可重试），而不是业务拒绝（重试也没用）。

    `scene` 给定则只比对该路径的 hints；留空则比对全部已登记文案。

    ⚠️别把它当"失败分类器"用：业务拒绝（不在可报范围/超上限/价格不符）**不在这里**，
      那些重试多少次都一样，属于正确失败。
    """
    s = str(text or "")
    if not s:
        return False
    rules = [THROTTLE_RULES[scene]] if scene in THROTTLE_RULES else list(THROTTLE_RULES.values())
    for r in rules:
        if any(h in s for h in (r.get("hints") or ())):
            return True
    return False


def throttle_wait(text, scene: str, attempt: int = 1) -> float:
    """撞节流后该等多久：**文案里自带秒数就用它**（+15s 余量），否则用 `backoff × attempt`。

    实证：国补上传被挡时文案是「多个文件上传需要再等 81 秒」——照它退避，第 1 次重试即成功。
    """
    m = _SECONDS_IN_TEXT.search(str(text or ""))
    if m:
        return float(m.group(1)) + 15.0
    return float((THROTTLE_RULES.get(scene) or {}).get("backoff") or 2.0) * max(1, int(attempt))


# --------------------------------------------------------------------------- #
# 四、退避重试
# --------------------------------------------------------------------------- #
def retry_throttled(fn: Callable, scene: str, *, rounds: int = 3,
                    is_bad: Callable = None, log: Callable = None, trace: list = None):
    """跑 `fn()`，**只对节流文案**退避重试；其它异常/失败立即返回或上抛。

    `is_bad(result) -> text|None`：从**正常返回值**里抠出失败文案（很多网关不抛异常，
      而是回 `{"success": false, "message": "..."}`）。返回 None 表示这次成功。
    `rounds`：最多尝试次数（含第一次）。

    ⚠️**只对 `clean_reject=True` 的路径无脑用**。`clean_reject=False`（如 bybt.apply）
      表示"被挡了也可能异步落地了"，盲目重试会**重复提交**——那种必须回读证实后再重投。
    """
    r = rule(scene)
    if r.get("clean_reject") is False:
        raise BlacklightError(
            f"policy: 写路径 {scene} 标了 clean_reject=False（被挡≠没落地），"
            f"禁止用 retry_throttled 盲目重试；请走「回读证实没落地再重投」。")
    last_text = None
    for attempt in range(1, max(1, int(rounds)) + 1):
        slept = pace(scene)
        if trace is not None and slept > 0.5:
            trace.append({"attempt": attempt, "限速等待s": round(slept, 1)})
        try:
            out = fn()
        except Exception as e:                       # 异常型失败
            if attempt >= rounds or not is_throttle(e, scene):
                raise
            last_text = str(e)
        else:                                        # 返回值型失败
            text = is_bad(out) if is_bad else None
            if not text or not is_throttle(text, scene):
                return out
            if attempt >= rounds:
                return out                           # 重试用尽，把最后一次原样交回调用方判断
            last_text = text
        wait = throttle_wait(last_text, scene, attempt)
        msg = f"[policy/{scene}] 第 {attempt} 次撞节流，等 {wait:.0f}s 后重试：{str(last_text)[:60]}"
        # ★**永远不要静默退避**：2026-08-19 首次上线时 upload_excel 没传 log，
        #   一次国补上传在退避里烧掉 ~13 分钟、审计和返回值里都看不出为什么
        #   （@audited 包在最外层，重试是在里面转）。一个会花掉十几分钟却不吭声的
        #   重试循环，比不重试更糟——所以这里**无条件 print 兜底**，`log`/`trace` 只是加强。
        (log or print)(msg)
        if trace is not None:
            trace.append({"attempt": attempt, "撞节流": str(last_text)[:80], "退避s": round(wait, 1)})
        _time.sleep(wait)
    return None
