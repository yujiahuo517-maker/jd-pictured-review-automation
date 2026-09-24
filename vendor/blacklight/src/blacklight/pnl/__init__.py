"""pnl = **毛利监控 & 损益分析的跨域编排层**。

⚠️**这不是一个网关域**。`osw` / `yx` / `jzt` / `easybi` / `ge` 都按网关划分，
本包不对应任何网关，它把那几个域编排成一条链路。命名上注意区分：

    blacklight.pnl          ← 本包（编排层）
    blacklight.easybi.pnl   ← easybi 域内的京算盘损益模块
    blacklight.osw.margin   ← osw 域的毛利监控
    blacklight.ge.margin    ← ge 域的实时毛利桥

## 为什么要有这一层（2026-08-11 重构）
今天把 ge 接进来后暴露的问题，**大多不是 bug 而是方法缺陷**，而且同一个错误一天犯两次：
- **排序向后看**：15 日累计把已退潮的券排到第一（`1315454844` 近 7 日已塌 95%）
- **范围不对齐**：ge 默认整部门 / easybi 只有给定 SKU ⇒ 造出假差异，据此下过错结论
- **口径混用**：全额 vs 采销承担差 22.3%，会让「找谁谈」的名单完全换人
- **静默失败面广**：实撞 6 处，"没报错"证明不了任何事
- **慢**：逐 SKU 循环 vs ge 聚合下钻，差 1~2 个数量级

⇒ 本层的职责就是把这五条**从「靠人记得」变成「代码里挡得住」**：
`caliber`(口径) / `scope`(范围) / `paging`(截断) / `probe`(静默忽略) / `sentinel`(恒等式)。

## 两条时间线，绝不混用
| | 实时 RT (T-0) | 离线 OFF (T-1/T-2) |
|---|---|---|
| 源 | `ge.margin` + `osw.scan_portfolio`(前瞻) | `easybi.coupon` + `ge.couponbatch` + `easybi.pnl` |
| 用途 | **发现 + 止血** | **归因 + 定责 + 谈判** |

★实时只用于止血、**不用于定责**（当日广告/物流未结算），定责以离线复核为准。
"""
from __future__ import annotations

from .caliber import Caliber, Freshness, Tagged, tagged_sum
from .scope import Scope, align
from blacklight.core.paging import fetch_paged, check_truncation, TruncationError
from .probe import silent_ignore_probe
from .runrate import rank, from_ge_drill
from .offline import attribute, attribute_rt, feasibility
from .scan import scan_rt, scan_offline, margin_scan
from .actions import build as build_actions
from .sentinel import (Sentinel, run_sentinels, bridge_closes,
                       full_equals_self_plus_platform, ownership_overlap)

__all__ = [
    "Caliber", "Freshness", "Tagged", "tagged_sum",
    "Scope", "align",
    "fetch_paged", "check_truncation", "TruncationError",
    "silent_ignore_probe",
    "rank", "from_ge_drill",
    "attribute", "attribute_rt", "feasibility",
    "scan_rt", "scan_offline", "margin_scan", "build_actions",
    "Sentinel", "run_sentinels", "bridge_closes",
    "full_equals_self_plus_platform", "ownership_overlap",
]
