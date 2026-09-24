"""ge = **黄金眼数据门户**（`ge.jd.com` 前台 / `ge.back.jd.com` 网关）。

⚠️**与 easybi 是两个平台，别混**（2026-08-11 用户纠正，此前本模块被误放在 `easybi/` 下）：

| | ge（本域） | easybi |
|---|---|---|
| 前台 | `ge.jd.com` | `easybi.jd.com` |
| 网关 | `ge.back.jd.com` | `easybi.jd.com` |
| 形态 | 低代码看板（`micro-app`），资源位 `resId`/`menuId` 寻址 | 数据集（`datasetId`）+ 维度/指标查询 |
| 登录 | 主票 cookie 直连 | 主票自动 OIDC 换域内会话 |

⚠️易混点：`blacklight/easybi/coupon.py` 里的券集 `1043053` / 促销集 `1045254`
在既有文档与记忆里被称作「**黄金眼**券促集」——那是 **easybi 上的数据集**，
不是本域。看到「黄金眼」三个字要先分清指的是**平台 ge** 还是 **easybi 上那两个集**。

两边**可以互校**（2026-08-11 实测，固定面额券 194/198 满足
`easybi(采销承担 + 平台承担) == ge 全额券成本`），这跟「成交预估 vs 出库计费不可比」
那条铁律不冲突——那条讲的是 osw/京准通 与 京算盘损益，不是这两个。
"""
from __future__ import annotations

from . import couponbatch, margin, ssm  # noqa: F401
from .doctor import doctor  # noqa: F401

__all__ = ["couponbatch", "margin", "ssm", "doctor"]
