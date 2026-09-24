"""
osw 采销域：**供应商管理（切商 / 报价 / 配额）**。

入口＝商品列表行内「供应商管理」按钮，前端页面
`jx-zy-selectioncms-pro.pf.jd.com/acceptance/onsale/supplier?projectId=&spuIdx=&spuIdSeller=<SPU>`，
后端与 selection 同一个微应用（api.m.jd.com + appid=selectioncms），**签名/cookie 直接复用 selection._call**。

★接口只有一个，靠「给不给 sku 参数」切换两种语义（2026-08-03 抓包 + 实证）：
  - 只给 spuIdSeller        → **该 SPU 下每个 SKU 的当前供应关系**（一行一个在供 SKU）
  - 再给 skuIdSeller/skuIdx → **该 SKU 收到的全部报价**（一行一个供应商报价，多供应商在此）

★**projectId 必传**，而它不在供应商页 URL 里 —— 只能从商品列表行的「标的管理」按钮 URL 抠
  （已封装成 product._clean 的 `projectId` 字段），本模块 `resolve_project_id()` 亦可代劳。

配额模型（页面「自动切商规则」原文）：
  生效配额 dailyCapacity = min(商家配额 vendorDailyCapacity, 采销配额 salesDailyCapacity)
  salesDailyCapacity=0 表示采销未配置；生效配额耗尽会自动切到还有配额的商家；
  已采纳商家价格/库存/配额都具备时，按采纳先后顺序供货。
  切商触发还有：低价切商、库存不可用切商、供应商 SKU 状态不可用切商。

金额单位：`supplyPrice` 服务端是**分**，本模块统一转元（`采购价`）。
"""
from __future__ import annotations

from typing import Optional

from blacklight.core import BlacklightError
from blacklight.osw import selection as _sel
from blacklight.osw import product as _prod

FN_ITEM_LIST = "jxzy_adopt_queryItemList"

BUID = 325
APP_CODE = "msc588d6d5"

# 物流模式（quotationPattern）—— 仅 2 已实证，其余按前端文案暂列，遇到未知值原样透出
QUOTATION_PATTERN = {2: "纯配全托"}

# 采纳状态（status/statusDesc 服务端自带，这里只列已见到的，statusDesc 优先）
_STATUS_SEEN = {30: "供货中"}


def _fen2yuan(v) -> Optional[float]:
    """分→元，**None→None**（单值语义：缺失就是缺失，不冒充 0）。
    ★与 `osw/margin.py::_fen2yuan` **故意不同**——那边要参与求和所以 None→0。同名不同义，别互相复制。"""
    if v is None:
        return None
    try:
        return round(int(v) / 100.0, 2)
    except (TypeError, ValueError):
        return None


def _row(it: dict) -> dict:
    """把一条 itemList 记录压成整洁行。保留主键，便于后续写操作（采纳/取消/配额）。"""
    vendor_cap = it.get("vendorDailyCapacity")
    sales_cap = it.get("salesDailyCapacity")
    pattern = it.get("quotationPattern")
    return {
        # ---- 主键（写操作要用）----
        "id": it.get("id"),                       # 采纳记录ID
        "projectId": it.get("projectId"),
        "spuIdx": it.get("spuIdx"), "skuIdx": it.get("skuIdx"),
        "inquiryId": it.get("inquiryId"),         # 询价单（与竞价链接同源）
        "lineId": it.get("lineId"), "quotationId": it.get("quotationId"),
        "version": it.get("version"),             # 乐观锁，改配额可能要带
        # ---- 供应商 ----
        "供应商ID": it.get("venderIdSupplier"),
        "供应商店铺": it.get("venderNameSupplier"),
        "商家SKU": it.get("skuIdSupplier"),
        "商家SPU": it.get("spuIdSupplier"),
        "商家SKU名": it.get("skuNameSupplier"),
        "商家SKU可用": it.get("vendorSkuStatus") == 1,
        # ---- 报价 ----
        "采购价": _fen2yuan(it.get("supplyPrice")),
        "物流模式": QUOTATION_PATTERN.get(pattern, pattern),
        "实时库存": it.get("stock"),
        # ---- 配额（生效 = min(商家, 采销)；采销 0 = 未配置）----
        "商家配额": vendor_cap,
        "采销配额": sales_cap,
        "采销配额未配置": (sales_cap in (0, None)),
        "生效配额": it.get("dailyCapacity"),
        "采销配额编辑人": it.get("salesDailyCapacityEditorId") or None,
        # ---- 状态 ----
        "状态": it.get("statusDesc") or _STATUS_SEEN.get(it.get("status"), it.get("status")),
        "statusCode": it.get("status"),
        "可取消采纳": it.get("allowedDenial"),
        "禁止采纳": it.get("disabledAdopt"),
        "可批量": it.get("canBatch"),
        # ---- 三猎确认 ----
        "猎物": it.get("preyId"), "猎人": it.get("hunterId"), "猎枪": it.get("shotgunId"),
        "采纳时间": it.get("preyConfirmTime"),
        "更新时间": it.get("updateTime"),
        # ---- 同步异常（切商失败排查）----
        "同步错误码": it.get("syncEffectiveErrorCode") or None,
        "同步错误": it.get("syncEffectiveErrorMsg") or None,
    }


def _query(project_id: str, spu_id_seller, spu_idx="1",
           sku_id_seller=None, sku_idx=None,
           page: int = 1, page_size: int = 50) -> dict:
    if not str(project_id or "").strip():
        raise BlacklightError(
            "projectId 必传（供应商接口硬要求）。用 osw_product_list 行里的 projectId，"
            "或 supplier.resolve_project_id(spu_id) 代查。")
    body = {"env": "prod", "pageIndex": int(page), "pageSize": int(page_size),
            "projectId": str(project_id), "spuIdx": str(spu_idx),
            "spuIdSeller": str(spu_id_seller),
            "buid": BUID, "appCode": APP_CODE}
    if sku_id_seller:
        body["skuIdSeller"] = str(sku_id_seller)
        body["skuIdx"] = str(sku_idx if sku_idx is not None else "1")
    d = _sel._call(FN_ITEM_LIST, body) or {}
    rows = [_row(x) for x in (d.get("itemList") or [])]
    return {"total": d.get("total"), "count": len(rows),
            "采销配额可编辑": d.get("showDailyCapacityEdit"),
            "可取消采纳按钮": d.get("showDenialButton"),
            "showMainSupply": d.get("showMainSupply"),
            "rows": rows}


def resolve_project_id(spu_id) -> Optional[str]:
    """按 SPU 反查标的ID（projectId）。走商品列表 productId 过滤，取「标的管理」按钮里的 VPROJ。"""
    res = _prod.product_list(page=1, page_size=10, product_state=None,
                             product_ids=[str(spu_id)])
    for r in res.get("rows") or []:
        if str(r.get("productId")) == str(spu_id) and r.get("projectId"):
            return r["projectId"]
    return None


def spu_supply(spu_id_seller, project_id: Optional[str] = None, spu_idx="1",
               page: int = 1, page_size: int = 50) -> dict:
    """**该 SPU 下每个 SKU 当前在供的供应商**（一行一个 SKU）。project_id 缺省自动反查。"""
    pid = project_id or resolve_project_id(spu_id_seller)
    return _query(pid, spu_id_seller, spu_idx, page=page, page_size=page_size)


def sku_quotes(spu_id_seller, sku_id_seller, project_id: Optional[str] = None,
               spu_idx="1", sku_idx="1", page: int = 1, page_size: int = 50) -> dict:
    """**该 SKU 收到的全部报价**（一行一个供应商；多供应商竞价/切商在这里看）。"""
    pid = project_id or resolve_project_id(spu_id_seller)
    return _query(pid, spu_id_seller, spu_idx, sku_id_seller, sku_idx,
                  page=page, page_size=page_size)
