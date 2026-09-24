"""ge 契约巡检 —— 抓包封装的接口随页面改版会漂移，取数前先跑。

⚠️与其它域 doctor 的区别：ge 有**两个只能靠探活发现**的失效模式，
契约结构完全正常时它们照样让结果错：
  1. `b-ext-device-info` **过期** → `no auth:专用菜单权限校验`（毛利监控整个不可用）
  2. `code=2000`「部分指标查询失败」→ **照样带数据回来**，只是悄悄少指标；
     少的若是毛利桥的减项，毛利会被算高 ⇒ 这里用**桥闭合**当哨兵。
"""
from __future__ import annotations

import datetime as _dt

from blacklight.core import BlacklightError
from . import couponbatch as cb
from . import margin as gm


def _chk(name, fn):
    try:
        return {"check": name, "ok": True, "detail": fn()}
    except BlacklightError as e:
        return {"check": name, "ok": False, "detail": str(e)[:160]}
    except Exception as e:
        return {"check": name, "ok": False, "detail": "%s: %s" % (type(e).__name__, str(e)[:140])}


def doctor() -> dict:
    now = _dt.datetime.now()
    S, E = now.strftime("%Y-%m-%d 00:00:00"), now.strftime("%Y-%m-%d %H:%M:%S")
    y = (now.date() - _dt.timedelta(days=1)).isoformat()
    checks = []

    def _device():
        # 任一毛利监控请求都会验设备指纹；缺/过期 → no auth
        b = gm.bridge(S, E)
        return "device_info 有效（桥返回 %d 项）" % len(b["拆解"])
    checks.append(_chk("b-ext-device-info 有效性（★会过期）", _device))

    def _bridge():
        b = gm.bridge(S, E)
        if not b["闭合"]:
            raise BlacklightError("毛利桥不闭合：%s" % (b.get("_警告") or ""))
        if b["缺失指标"]:
            raise BlacklightError("缺 %d 个指标（code=2000 静默少返）" % len(b["缺失指标"]))
        return "桥闭合，投后 %.2f，13 指标齐" % b["预估投后履约毛利"]
    checks.append(_chk("毛利桥闭合（★code=2000 哨兵）", _bridge))

    def _drill():
        d = gm.drill(S, E, degree="sku")
        kinds = {}
        for r in d["rows"]:
            kinds[r["行类型"]] = kinds.get(r["行类型"], 0) + 1
        if "预估毛利_投后" not in (d["rows"][0] if d["rows"] else {"预估毛利_投后": 1}):
            raise BlacklightError("drill 缺 预估毛利_投后")
        return "sku 下钻 %d 行，分类 %s" % (d["count"], kinds)
    checks.append(_chk("drill(sku) + 行分类", _drill))

    def _pre_post():
        """★投前/投后语义哨兵：ord_ 必须 > sku_（差额=广告），反了说明字段漂了。"""
        d = gm.drill(S, E, degree="sku")
        pre = sum(r["预估毛利_投前"] for r in d["rows"])
        post = sum(r["预估毛利_投后"] for r in d["rows"])
        ad = sum(r.get("广告消耗") or 0 for r in d["rows"])
        if abs((pre - post) - ad) > max(1.0, abs(ad) * 0.02):
            raise BlacklightError(
                "投前−投后(%.2f) ≠ 广告消耗(%.2f)，ord_/sku_ 语义可能已漂"
                % (pre - post, ad))
        return "投前 %.0f − 投后 %.0f = 广告 %.0f ✓" % (pre, post, ad)
    checks.append(_chk("投前/投后语义（★前缀与直觉相反）", _pre_post))

    def _filter_probe():
        """★阴性对照：确认 sku_id 筛选真的生效，而不是被静默忽略。"""
        base = gm.drill(S, E, degree="sku")
        if not base["rows"]:
            return "当日无成交，跳过"
        one = base["rows"][0]["sku_id"]
        got = gm.drill(S, E, degree="sku", sku_ids=[one])
        if got["count"] >= base["count"]:
            raise BlacklightError(
                "sku_id 筛选未生效（%d → %d）——filterList 对未知字段静默忽略，"
                "字段名可能已改" % (base["count"], got["count"]))
        return "sku_id 筛选生效（%d → %d）" % (base["count"], got["count"])
    checks.append(_chk("筛选生效性（★静默忽略探针）", _filter_probe))

    def _cb():
        # ★探针的目的只有一个：**两接口 join 上没有**（券名/发券人拼得上）。
        #   默认 page_size 会被昨日券批次量撞满（实测 500 == 上限）而触发截断闸，
        #   于是这条检查天天红 —— 那是"探针取太多"，不是契约漂了。
        #   给足 page_size 让它取全；真超了 5000 说明量级本身变了，那时候红才有意义。
        d = cb.batch_detail(y, y, page_size=5000)
        if not d["rows"]:
            return "昨日无券批次（可能正常）"
        r = d["rows"][0]
        if not r.get("jdr_sch_coupon_batch_name"):
            raise BlacklightError("券批次缺券名——getDimensionAttr 可能没拼上")
        return "券批次 %d 条，券名/发券人齐" % d["count"]
    checks.append(_chk("couponbatch 两接口 join", _cb))

    drift = [c for c in checks if not c["ok"]]
    return {"healthy": not drift, "drift": [c["check"] for c in drift],
            "checks": checks,
            "note": ("ge 契约正常。" if not drift else
                     "★有 drift，转人工。device_info 过期最常见——重抓一次该页请求即可。")}
