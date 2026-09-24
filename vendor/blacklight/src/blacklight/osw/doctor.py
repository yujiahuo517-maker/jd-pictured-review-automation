"""
osw-mcp 契约巡检（doctor）—— 抓包封装的接口（sff.jd.com 商品 / api.m 毛利 / selectioncms 选品）会随页面改版漂移字段/参数。
定期打 osw 关键接口、校验返回结构没变；**漂移了先告警/转人工**，别让无人值守 Agent 拿变形的契约去改价/取数/认领。
覆盖 7 接口：商品列表 / SKU明细 / 毛利监控 / 定价底料 / 公共商品池 / 标的详情 / 待铺货。
只读、无写操作。跑：  py doctor.py   或 MCP 工具 osw_doctor。
"""
from blacklight.core import auth as jd_auth
from blacklight.osw import product as osw_product
from blacklight.osw import margin as osw_margin
from blacklight.osw import selection as osw_selection


def _check(name, fn):
    try:
        ok, detail = fn()
        return {"check": name, "ok": bool(ok), "detail": detail}
    except Exception as e:
        return {"check": name, "ok": False, "detail": f"异常: {str(e)[:90]}"}


# 巡检间传递：从商品列表拿一个真实 productId/skuId 供 SKU明细/定价 检查复用；从公共商品池拿 vprojectId 供标的详情检查
_SAMPLE = {"productId": None, "skuId": None, "vprojectId": None}


def _product_list():
    r = osw_product.product_list(page=1, page_size=1, product_state="4")
    rows = r.get("rows") or []
    if not isinstance(r.get("total"), int) or not rows:
        return (False, f"列表空/total非int（可能字段漂移）：total={r.get('total')}")
    need = ["productId", "skuId", "jdPrice", "jxCgPriceMin", "categoryName", "created", "skuCount"]
    miss = [f for f in need if f not in rows[0]]
    _SAMPLE["productId"] = rows[0].get("productId")
    _SAMPLE["skuId"] = rows[0].get("skuId")
    return (not miss, f"total={r['total']}｜缺字段:{miss}" if miss else f"字段齐, total={r['total']}")


def _sku_detail():
    pid = _SAMPLE["productId"]
    if not pid:
        return (False, "无样例 productId（商品列表检查先失败）")
    d = osw_product.product_sku_detail(pid, with_cost=True)
    rows = d.get("rows") or []
    if not rows:
        return (False, f"SPU {pid} 无 SKU 明细（querySkuPrice 可能漂移）")
    need = ["skuId", "jdPrice", "purchasePrice", "actualTotalCost", "productType"]
    miss = [f for f in need if f not in rows[0]]
    return (not miss, f"SKU数={d.get('count')}｜缺字段:{miss}" if miss else f"字段齐, SKU数={d.get('count')}")


def _margin_monitor():
    with osw_margin._client() as c:
        d = osw_margin._call(c, "jxzy_markettool_queryPreDiscountHome",
                             {"env": "prod", "pageNo": 1, "pageSize": 1, "estimatedProfitChannel": "normal",
                              "timeType": 0, "skuStatus": 1, "strSkuIds": "", "buid": 325, "appCode": ""}, "POST") or {}
    items = d.get("skuPromotionInfoDetails") or []
    if not items:
        return (False, "监控列表返回空（可能字段名/参数漂移）")
    need = ["skuId", "jxEstimatedGrossProfitPrice", "jxNewUserAllowanceSum", "benchPrice",
            "promotionList", "purchasePrice", "jxCostSum"]
    miss = [f for f in need if f not in items[0]]
    if miss:
        return (False, f"totalCount={d.get('totalCount')}｜缺字段:{miss}")
    # ★值为 null 的哨兵：`_fen2yuan` 对 None 返回 0.0（求和场景需要如此），于是**关键金额字段一旦
    #   变成 null，会静默变成 0 元** —— 采购价 0 会让毛利虚高、基准价 0 会让毛利率算飞。
    #   字段"在"不代表"有值"，所以单独查一道（同 jzt/doctor 的余额哨兵，那类漂移最危险）。
    #   2026-08-05 实测 100 条样本这些字段无一为 null；这条是**防它哪天变**，不是当前有问题。
    nulls = [f for f in ("benchPrice", "purchasePrice", "jxCostSum") if items[0].get(f) is None]
    if nulls:
        return (False, f"关键金额字段值为 null {nulls} —— 会被 _fen2yuan 静默读成 0 元"
                       f"（采购价 0 → 毛利虚高），先核对接口是否改了字段语义")
    return (True, f"字段齐且关键金额非 null, totalCount={d.get('totalCount')}")


def _pricing():
    sku = _SAMPLE["skuId"]
    if not sku:
        return (False, "无样例 skuId")
    try:
        p = osw_margin.query_pricing(str(sku))
    except Exception as e:
        # 新品可能不在毛利监控 → 换列表里的亏损款兜底
        lm = osw_margin.list_low_margin(limit=1)
        if lm.get("rows"):
            p = osw_margin.query_pricing(lm["rows"][0]["skuId"])
        else:
            return (False, f"query_pricing 取不到（{str(e)[:50]}）")
    need = ["benchPrice", "fullCost", "jxFullProfit", "jxMargin", "isNewUserPhantom", "promotions"]
    miss = [f for f in need if f not in p]
    return (not miss, f"缺字段:{miss}" if miss else "定价底料字段齐(含新人价剔除标记)")


def _pool_list():
    r = osw_selection.pool_list(page=1, page_size=1)
    rows = r.get("rows") or []
    if not isinstance(r.get("total"), int) or not rows:
        return (False, f"公共商品池空/total非int（可能字段漂移）：total={r.get('total')}")
    need = ["vprojectId", "spuName", "canClaim", "categoryName", "status"]
    miss = [f for f in need if f not in rows[0]]
    _SAMPLE["vprojectId"] = rows[0].get("vprojectId")
    return (not miss, f"total={r['total']}｜缺字段:{miss}" if miss else f"字段齐, total={r['total']}")


def _selection_detail():
    vp = _SAMPLE["vprojectId"]
    if not vp:
        return (False, "无样例 vprojectId（公共商品池检查先失败）")
    d = osw_selection.project_detail(vp, raw=True)
    # 摘要层字段（认领/驳回/体检依赖）
    need = ["skus", "versionInfo", "buttonInfo", "roleInfo", "refuseTypes", "specWarnings"]
    miss = [f for f in need if f not in d]
    # 认领提交依赖：_raw.projectInfo.productInfo[].itemInfoList[].skuBom.supplyAll + skuRemark.venderSkuId
    raw = d.get("_raw") or {}
    pinfo = (raw.get("projectInfo") or {}).get("productInfo") or []
    sku_ok = False
    if pinfo and (pinfo[0].get("itemInfoList")):
        it0 = pinfo[0]["itemInfoList"][0]
        sku_ok = ("skuBom" in it0 and "supplyAll" in (it0.get("skuBom") or {})
                  and "venderSkuId" in (it0.get("skuRemark") or {}))
    if miss or not sku_ok:
        return (False, f"缺摘要字段:{miss}｜认领体依赖skuBom.supplyAll/skuRemark.venderSkuId={'OK' if sku_ok else '缺'}")
    return (True, f"字段齐(SKU数={d.get('skuCount')}, buttonInfo={d.get('buttonInfo')})")


def _bid_list():
    r = osw_selection.bid_item_list(page=1, page_size=1)
    rows = r.get("rows") or []
    if not isinstance(r.get("total"), int) or not rows:
        return (False, f"待铺货空/total非int（可能字段漂移）：total={r.get('total')}")
    need = ["projectId", "jdPriceMin", "jdPriceMax", "profitMarginMin", "authorityList", "stage"]
    miss = [f for f in need if f not in rows[0]]
    return (not miss, f"total={r['total']}｜缺字段:{miss}" if miss else f"字段齐, total={r['total']}")


def run() -> dict:
    """打 osw 关键接口校验结构。返回 {healthy, drift:[漂移的检查], checks:[...]}。"""
    results = [
        _check("登录探活", lambda: (jd_auth.yx_is_logged_in() is True, "已授权")),
        _check("product 商品列表(queryValidProductList)", _product_list),
        _check("product SKU明细(querySkuPrice+getPriceApprovalStatus)", _sku_detail),
        _check("margin 毛利监控(queryPreDiscountHome)", _margin_monitor),
        _check("pricing 定价底料(query_pricing)", _pricing),
        _check("selection 公共商品池(listVenderTask)", _pool_list),
        _check("selection 标的详情(getProjectById)", _selection_detail),
        _check("selection 待铺货(getBidItemList)", _bid_list),
    ]
    drift = [r["check"] for r in results if not r["ok"]]
    return {"healthy": not drift, "drift": drift,
            "note": "有 drift → 相关接口字段/参数可能漂移，改价/取数先转人工核对再放手。" if drift else "osw 关键接口契约正常。",
            "checks": results}


if __name__ == "__main__":
    import json
    r = run()
    print(json.dumps(r, ensure_ascii=False, indent=1))
    raise SystemExit(0 if r["healthy"] else 1)
