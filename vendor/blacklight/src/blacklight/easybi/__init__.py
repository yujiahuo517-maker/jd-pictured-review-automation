"""easybi（京东数据平台 / Easy BI）域：自助 BI 数据集取数。

与 osw/yx/jzt 同一份登录态（`.jd.com` 主票），不需要单独登录。
"""
from __future__ import annotations

from blacklight.easybi.auth import client, login_info, reset
from blacklight.easybi.pl import (
    attribute_loss,
    caveats,
    overall_rate,
    pl_chain,
)
from blacklight.easybi.dataset import (
    DATASET_JX_PL,
    find_fields,
    list_fields,
    query,
)

__all__ = [
    "client", "login_info", "reset",
    "DATASET_JX_PL", "list_fields", "find_fields", "query",
    "pl_chain", "overall_rate", "attribute_loss", "caveats",
]
