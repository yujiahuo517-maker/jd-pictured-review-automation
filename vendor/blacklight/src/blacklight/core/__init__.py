"""core —— 基建再导出：域模块统一 `from blacklight.core import X`。"""
from blacklight.core.base import *          # noqa: F401,F403
from blacklight.core.base import BlacklightError, YxError  # noqa: F401  (YxError=别名兼容)
from blacklight.core.base import canon_num, canon_for_token  # noqa: F401  (进 confirm 指纹前的归一：标量用前者、嵌套结构用后者，别用裸 str()/json.dumps)
from blacklight.core.paging import fetch_paged, check_truncation, TruncationError  # noqa: F401  (统一截断闸：行数==上限即抛，别静默少算)
from blacklight.core.policy import (  # noqa: F401  (写路径策略：限速/节流分类/退避重试)
    pace, is_throttle, throttle_wait, retry_throttled, THROTTLE_RULES,
)
from blacklight.core.reuse import (  # noqa: F401  (通用件：落盘缓存/账号级文件锁/批量写节奏)
    disk_cached, disk_get, disk_put, cache_key, account_lock, paced,
)
