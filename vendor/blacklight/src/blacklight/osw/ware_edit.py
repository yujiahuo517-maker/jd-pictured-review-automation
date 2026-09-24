"""
jdcore 公共层：osw_ware_edit（**在售/下架 编辑商品**，osw 采销工作台「编辑商品」页）。

osw 商品列表页在售/下架的**编辑商品**页（`osw.jd.com/gongxiao/ware/selling/edit`，跨域 iframe gongxiao.jd.com）。
比 osw_product_*（sff 商品列表）更细：逐 SKU 的 短标题/UPC/最低零售价/商家SKU/启用状态/采购价 等编辑字段。

两个网关（与商品列表 sff、选品 api.m/selectioncms 都不同）：
  - **加载**：`api.m.jd.com`（`appid=gx-pc`，functionId `api_ware_edit_detail`，GET，body={wareId}，**cookie-only 无 signStr**，带 ext）
  - **保存**：`gmall.jd.com/api/ware/save`（POST JSON，cookie-only，整品覆盖式 Read-Modify-Write）
逆向笔记见 osw-mcp/NOTES_ware_edit.md。

本模块当前：**只读加载**（ware_load）。写保存（整品覆盖）待建（见 NOTES 落地计划）。
只依赖 jd_auth / jd_core，不反向依赖任何业务 MCP 场域。
"""
from __future__ import annotations

import copy as _copy
import json as _json
import time as _time
from typing import Optional

from blacklight.core import auth as jd_auth
from blacklight.core import BlacklightError, make_client, confirm_token as _confirm_token, canon_for_token, audited

# --------------------------------------------------------------------------- #
# gx-pc 网关（加载）
# --------------------------------------------------------------------------- #
GXPC_API = "https://api.m.jd.com/api"
GX_ORIGIN = "https://gongxiao.jd.com"
GX_COMMON = {"scval": "ht", "loginType": "7", "appid": "gx-pc", "client": "pc"}


def _biz_id(override: Optional[str] = None) -> str:
    import os
    return (str(override).strip() if override else "") \
        or os.environ.get("YX_PRODUCT_BIZ_ID", "").strip() or "14691198"


def _cookie() -> str:
    ck = jd_auth.get_cookie()
    if not ck:
        raise BlacklightError("无登录态：先 osw_login / yx_login，或设 JD_COOKIE 环境变量")
    return ck


def _ext(biz_id: str) -> str:
    return _json.dumps({"requestSource": "color", "belongBizId": str(biz_id),
                        "curRole": "2", "requestClientType": "pc"},
                       separators=(",", ":"), ensure_ascii=False)


def _gxpc(function_id: str, body: dict, biz_id: str, method: str = "GET") -> dict:
    """gx-pc 网关请求（cookie-only，无 signStr）→ 校验 success/code==0 → 返回 data。"""
    ck = _cookie()
    c = make_client(ck, origin=GX_ORIGIN, referer=GX_ORIGIN + "/", content_type=None, timeout=25.0,
                    extra={"x-requested-with": "XMLHttpRequest", "accept": "application/json, text/plain, */*"})
    params = dict(GX_COMMON)
    params.update({"functionId": function_id, "t": str(int(_time.time() * 1000))})
    bodystr = _json.dumps(body or {}, separators=(",", ":"), ensure_ascii=False)
    with c:
        if method.upper() == "POST":
            r = c.post(GXPC_API, params=params, data={"body": bodystr, "ext": _ext(biz_id)},
                       headers={"Content-Type": "application/x-www-form-urlencoded"})
        else:
            p2 = dict(params); p2.update({"body": bodystr, "ext": _ext(biz_id)})
            r = c.get(GXPC_API, params=p2, headers={"Content-Type": "text/plain"})
    r.raise_for_status()
    try:
        j = r.json()
    except Exception as e:
        raise BlacklightError(f"{function_id} 未返回 JSON（HTTP {r.status_code}）——登录态可能失效，请 osw_login") from e
    if not (j.get("success") or j.get("code") in (0, "0")):
        raise BlacklightError(f"{function_id}: {j.get('msg') or j.get('message') or ('code=' + str(j.get('code')))}")
    return j.get("data") or {}


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _short_title(sku_raw: dict) -> Optional[str]:
    """短标题：加载在 featuresMap.shortTitle；也兼容 features:[{key:shortTitle,value}]。"""
    fm = sku_raw.get("featuresMap") or {}
    if fm.get("shortTitle") is not None:
        return fm.get("shortTitle")
    for f in (sku_raw.get("features") or []):
        if f.get("key") == "shortTitle":
            return f.get("value")
    return None


def _sku_view(s: dict) -> dict:
    """把加载的 skuList 一项压成编辑视图（UI 列对应字段）。"""
    fm = s.get("featuresMap") or {}
    return {
        "skuId": str(s.get("skuId")),
        "skuName": s.get("skuName"),
        "jdPrice": _num(s.get("jdPrice")),                 # 京东价
        "purchasePrice": _num(s.get("purchasePrice")) if s.get("purchasePrice") is not None else _num(fm.get("cgPrice")),  # 采购价
        "barCode": s.get("barCode") or "",                 # UPC编码
        "sellMin": s.get("sellMin") if s.get("sellMin") not in (None, "") else None,   # 最低零售价
        "outerId": s.get("outerId") or "",                 # 商家SKU
        "shortTitle": _short_title(s),                     # 短标题
        "stockNum": s.get("stockNum"),                     # 当前库存
        "enable": s.get("enable"), "status": s.get("status"),   # 启用状态
        "saleAttr": [{"attrId": a.get("attrId"), "value": (a.get("attrValueAlias") or [None])[0]}
                     for a in (s.get("saleAttrs") or [])],       # SKU属性(尺寸/颜色卖点)
    }


def ware_load(ware_id: str, biz_id: Optional[str] = None, raw: bool = False) -> dict:
    """[编辑商品·读] 加载在售/下架商品的可编辑整品数据（api_ware_edit_detail）。
    返回 SPU 摘要（title/brandName/jdPrice/itemNum/categoryId/stockNum）+ 每 SKU 编辑视图
    （skuId/京东价/采购价/UPC(barCode)/最低零售价(sellMin)/商家SKU(outerId)/**短标题**/库存/启用状态/SKU属性）。
    raw=True 附完整原始 data（含 ware/skuList/images/introduction，供未来整品保存回填）。"""
    bid = _biz_id(biz_id)
    data = _gxpc("api_ware_edit_detail", {"wareId": int(ware_id)}, bid) or {}
    ware = data.get("ware") or {}
    skus = ware.get("skuList") or ware.get("skus") or data.get("skuInfo") or []
    summary = {
        "wareId": str(ware.get("wareId") or ware_id),
        "title": ware.get("title"),                        # 长标题
        "brandId": ware.get("brandId"), "brandName": ware.get("brandName"),
        "categoryId": ware.get("categoryId"),
        "itemNum": ware.get("itemNum"),                    # 货号
        "jdPrice": _num(ware.get("jdPrice")),
        "wareStatus": ware.get("wareStatus"),              # 上下架态
        "stockNum": ware.get("stockNum"),                  # 总库存
        "skuCount": len(skus),
        "skus": [_sku_view(s) for s in skus],
        "saleAttrs": _sale_axes(skus),                 # 销售属性轴(尺寸/颜色)+值[{valueId,name,seq}]，供名称/顺序编辑
    }
    if raw:
        summary["_raw"] = data
    return summary


# --------------------------------------------------------------------------- #
# 写：编辑商品保存（gmall.jd.com/api/ware/save，整品覆盖）—— load→改目标字段→原样回填
# 当前放开字段（用户限定）：短标题/启用状态（每SKU）+ 24h最大限购/运费模板/时效模板（SPU）。
# 尺寸/颜色 名称·顺序 编辑涉及跨SKU共享值+alias多处同步，作为下一增分（本版忠实 echo 不改）。
# --------------------------------------------------------------------------- #
GMALL_SAVE = "https://gmall.jd.com/api/ware/save"

# load(api_ware_edit_detail) 无来源、抓包恒为 0/"" 的顶层字段 → 用观测默认回填（编辑对象不涉及这些）
_SAVE_FLAG_DEFAULTS = {"isIOUSPay": 0, "isCheckCode": 0, "isDangerGoods": "", "isWeChatStock": "",
                       "noShow": 0, "ztSale": "", "wareLocation": "", "isSopJdDy": 0}
# SPU 级可改字段（用户放开）
_SPU_EDITABLE = {"maxBuyTimes", "transportId", "promiseId"}
# SKU 级可改字段（用户放开）
_SKU_EDITABLE = {"shortTitle", "enable"}


def _value_alias_map(skus: list) -> dict:
    """value_id → attrValueAlias（JSON串），供 skuImgs 的 colorId 还原。"""
    vmap = {}
    for s in skus:
        for a in (s.get("saleAttrs") or []):
            for vid, alias in zip(a.get("attrValues") or [], a.get("attrValueAlias") or []):
                vmap[str(vid)] = alias
    return vmap


def _alias_name(alias: str) -> Optional[str]:
    """从 attrValueAlias(JSON串 [{modelName,value,...}]) 取显示名(value)。取不到返回原串。"""
    try:
        arr = _json.loads(alias)
        return (arr[0] or {}).get("value") if arr else alias
    except Exception:
        return alias


def _alias_axis(alias: str) -> Optional[str]:
    """从 alias 取轴名(modelName，如 卖点/颜色)。"""
    try:
        arr = _json.loads(alias)
        return (arr[0] or {}).get("modelName") if arr else None
    except Exception:
        return None


def _sale_axes(skus: list) -> list:
    """列出销售属性轴（尺寸=卖点/颜色…）及其值：[{attrId, axis, values:[{valueId,name,seq}]}]（跨SKU去重）。"""
    axes = {}   # attrId -> {axis, values:{valueId:{name,seq}}}
    for s in skus:
        for a in (s.get("saleAttrs") or []):
            aid = str(a.get("attrId"))
            seqs = a.get("attrValuesSeqNo") or []
            for i, (vid, alias) in enumerate(zip(a.get("attrValues") or [], a.get("attrValueAlias") or [])):
                ax = axes.setdefault(aid, {"attrId": aid, "axis": _alias_axis(alias), "values": {}})
                if str(vid) not in ax["values"]:
                    ax["values"][str(vid)] = {"valueId": str(vid), "name": _alias_name(alias),
                                              "seq": (seqs[i] if i < len(seqs) else None)}
    return [{"attrId": ax["attrId"], "axis": ax["axis"],
             "values": sorted(ax["values"].values(), key=lambda v: (v["seq"] is None, v["seq"]))}
            for ax in axes.values()]


def _sku_to_save(sku: dict, edits: dict, name_map: dict = None, seq_map: dict = None) -> dict:
    """load.ware.skuList 一项 → save.skuAttr 一项（忠实转换）+ 应用 edits(shortTitle/enable) + 销售属性名称/顺序编辑。"""
    o = _copy.deepcopy(sku)
    fm = dict(o.get("featuresMap") or {})
    short = edits.get("shortTitle") if "shortTitle" in edits else fm.get("shortTitle")
    fm.pop("shortTitle", None)                        # save 的 featuresMap 不含 shortTitle
    o["featuresMap"] = fm
    o["features"] = [{"key": "shortTitle", "value": short}] if short not in (None, "") else []
    if "enable" in edits:
        en = 1 if int(edits["enable"]) else 0
        o["enable"] = en
        o["status"] = en                              # 启用/停用两处同步
    # ---- 销售属性 名称/顺序 编辑（按 value_id）----
    if name_map or seq_map:
        for a in (o.get("saleAttrs") or []):
            vids = [str(v) for v in (a.get("attrValues") or [])]
            aliases = list(a.get("attrValueAlias") or [])
            seqs = list(a.get("attrValuesSeqNo") or [])
            for i, vid in enumerate(vids):
                if name_map and vid in name_map and i < len(aliases):
                    try:
                        arr = _json.loads(aliases[i]); old = (arr[0] or {}).get("value")
                        arr[0]["value"] = name_map[vid]
                        aliases[i] = _json.dumps(arr, ensure_ascii=False)
                        if old and o.get("skuName"):          # 同步 skuName 里的名称
                            o["skuName"] = o["skuName"].replace(old, name_map[vid])
                    except Exception:
                        pass
                if seq_map and vid in seq_map and i < len(seqs):
                    seqs[i] = int(seq_map[vid])
            a["attrValueAlias"] = aliases
            if seqs:
                a["attrValuesSeqNo"] = seqs
    # 派生字段（save 有、load 无）
    uid = (o.get("unionId") or "")
    o["indexStr"] = "_".join(uid.split(",")[1::2]) if uid else o.get("indexStr", "")
    o.setdefault("sellMin", o.get("sellMin") or "")
    o.setdefault("incrStock", 0)
    o["promiseId"] = ""                               # 每SKU promiseId 抓包恒空（SPU 级才是时效模板）
    return o


def _build_save_body(load_raw: dict, sku_edits: dict, spu_edits: dict, attr_edits: dict = None) -> tuple:
    """从 load 原始 data 重建 gmall ware/save 整品 body，应用 SPU/SKU/销售属性 编辑。返回 (body, changes)。"""
    ware = load_raw.get("ware") or {}
    skus = ware.get("skuList") or ware.get("skus") or []
    changes = []

    # ---- 销售属性 名称/顺序 编辑（attr_edits={value_id:{name?,seq?}}）----
    name_map, seq_map = {}, {}
    if attr_edits:
        axes = _sale_axes(skus)
        cur = {v["valueId"]: v for ax in axes for v in ax["values"]}
        for vid, e in attr_edits.items():
            vid = str(vid)
            if vid not in cur:
                raise BlacklightError(f"销售属性值 {vid} 不存在（可编辑值见 osw_ware_edit_get 的 saleAttrs）")
            bad = set(e) - {"name", "seq"}
            if bad:
                raise BlacklightError(f"销售属性 {vid} 不支持编辑字段 {bad}（仅放开 name/seq）")
            if "name" in e and (e["name"] or "") != (cur[vid]["name"] or ""):
                name_map[vid] = e["name"]
                changes.append({"sku": "-", "field": f"销售属性名称({cur[vid]['name']})", "old": cur[vid]["name"], "new": e["name"]})
            if "seq" in e and int(e["seq"]) != (cur[vid]["seq"] if cur[vid]["seq"] is not None else -1):
                seq_map[vid] = int(e["seq"])
                changes.append({"sku": "-", "field": f"销售属性顺序({cur[vid]['name']})", "old": cur[vid]["seq"], "new": int(e["seq"])})

    # ---- SKU 数组 ----
    sku_out = []
    for s in skus:
        sid = str(s.get("skuId"))
        e = (sku_edits or {}).get(sid) or (sku_edits or {}).get(int(sid)) or {}
        bad = set(e) - _SKU_EDITABLE
        if bad:
            raise BlacklightError(f"SKU {sid} 不支持编辑字段 {bad}（当前仅放开 {_SKU_EDITABLE}）")
        fm = s.get("featuresMap") or {}
        if "shortTitle" in e and (e["shortTitle"] or "") != (fm.get("shortTitle") or ""):
            changes.append({"sku": sid, "field": "短标题", "old": fm.get("shortTitle"), "new": e["shortTitle"]})
        if "enable" in e and int(bool(e["enable"])) != int(bool(s.get("enable"))):
            changes.append({"sku": sid, "field": "启用状态", "old": s.get("enable"), "new": int(bool(e["enable"]))})
        sku_out.append(_sku_to_save(s, e, name_map, seq_map))

    # ---- 图片拆分（colorId 0000000000=主图 / 其它=SKU图，colorId→alias）----
    #      alias 从**已应用名称编辑**的 sku_out 取（改名后 skuImgs colorId 同步）
    vmap = _value_alias_map(sku_out)
    spu_imgs, sku_imgs = [], []
    for im in (ware.get("images") or []):
        cid = str(im.get("colorId"))
        row = {"colorId": cid, "imgUrl": im.get("imgUrl"), "imgIndex": im.get("imgIndex")}
        if cid == "0000000000":
            spu_imgs.append(row)
        else:
            row["colorId"] = vmap.get(cid, cid)       # 还原为卖点 alias JSON
            sku_imgs.append(row)

    # ---- SPU 属性（echo 已填 props）----
    spu_attr = [{"attrId": p.get("attrId"), "attrValues": p.get("attrValues"),
                 "attrValueAlias": p.get("attrValueAlias")} for p in (ware.get("props") or [])]

    # ---- 顶层 SPU 字段 ----
    def g(k, dflt=None):
        return ware.get(k, dflt)
    body = {
        "brandId": str(g("brandId") or ""), "refund": str(g("refund") or ""),
        "title": g("title"), "jdPrice": str(g("jdPrice") or ""),
        "length": g("length") or 0, "width": g("width") or 0, "height": g("height") or 0,
        "itemNum": g("itemNum") or "", "barCode": g("barCode") or "",
        "delivery": g("delivery") if g("delivery") is not None else "",
        "transportId": str(g("transportId") or ""), "promiseId": g("promiseId"),
        "packListing": g("packListing") or "",
        "isPayFirst": g("isPayFirst") if g("isPayFirst") is not None else 1,
        "maxBuyTimes": g("maxBuyTimes") if g("maxBuyTimes") is not None else 100,
        "spuAttr": _json.dumps(spu_attr, ensure_ascii=False),
        "skuAttr": _json.dumps(sku_out, ensure_ascii=False),
        "stockNum": g("stockNum"),
        "spuImgs": _json.dumps(spu_imgs, ensure_ascii=False),
        "skuImgs": _json.dumps(sku_imgs, ensure_ascii=False),
        "deletedSkus": [],
        "introductionUseFlag": str(g("introductionUseFlag") if g("introductionUseFlag") is not None else "0"),
        "introduction": g("introduction") or "",
        "shopCategorys": ",".join(str(x) for x in (g("shopCategorys") or [])),
        "saveType": 0, "categoryId": str(g("categoryId") or ""),
        "wareId": str(g("wareId")), "zbUuid": "",
    }
    body.update(_SAVE_FLAG_DEFAULTS)
    # isSopJdDy 若 load 有则用真值
    if g("isSopJdDy") is not None:
        body["isSopJdDy"] = g("isSopJdDy")

    # ---- 应用 SPU 编辑 ----
    for k, v in (spu_edits or {}).items():
        if k not in _SPU_EDITABLE:
            raise BlacklightError(f"SPU 不支持编辑字段 '{k}'（当前仅放开 {_SPU_EDITABLE}）")
        old = body.get(k)
        body[k] = str(v) if k in ("transportId", "promiseId") else v
        if str(old) != str(body[k]):
            label = {"maxBuyTimes": "24h最大限购", "transportId": "运费模板", "promiseId": "时效模板"}[k]
            changes.append({"sku": "-", "field": label, "old": old, "new": body[k]})

    return body, changes


def _invariants(load_raw: dict) -> dict:
    """整品覆盖守门用的不变量（不应被本次编辑改动）：图片URL集合/SKU数/富文本长度/已填属性数。"""
    ware = load_raw.get("ware") or {}
    return {"imgUrls": sorted((im.get("imgUrl") or "") for im in (ware.get("images") or [])),
            "skuIds": sorted(str(s.get("skuId")) for s in (ware.get("skuList") or ware.get("skus") or [])),
            "introLen": len(ware.get("introduction") or ""),
            "propCount": len(ware.get("props") or [])}


def _body_invariants(body: dict) -> dict:
    """从重建的 save body 反算同口径不变量，用于与 load 比对。"""
    spu = _json.loads(body.get("spuImgs") or "[]")
    sku = _json.loads(body.get("skuImgs") or "[]")
    skuAttr = _json.loads(body.get("skuAttr") or "[]")
    spuAttr = _json.loads(body.get("spuAttr") or "[]")
    return {"imgUrls": sorted((x.get("imgUrl") or "") for x in (spu + sku)),
            "skuIds": sorted(str(s.get("skuId")) for s in skuAttr),
            "introLen": len(body.get("introduction") or ""),
            "propCount": len(spuAttr)}


def _guard(load_raw: dict, body: dict):
    """守门：重建 body 的不变量必须与 load 完全一致（图片/SKU/富文本/属性无丢失），否则拒绝。"""
    a, b = _invariants(load_raw), _body_invariants(body)
    lost = []
    if a["imgUrls"] != b["imgUrls"]:
        lost.append(f"图片({len(a['imgUrls'])}→{len(b['imgUrls'])})")
    if a["skuIds"] != b["skuIds"]:
        lost.append(f"SKU({len(a['skuIds'])}→{len(b['skuIds'])})")
    if a["introLen"] != b["introLen"]:
        lost.append(f"富文本长度({a['introLen']}→{b['introLen']})")
    if a["propCount"] != b["propCount"]:
        lost.append(f"属性数({a['propCount']}→{b['propCount']})")
    if lost:
        raise BlacklightError("整品覆盖守门失败——重建可能丢失：" + "、".join(lost) + "。已拒绝保存（防抹图/属性/详情）。")


def _edit_token(ware_id: str, sku_edits: dict, spu_edits: dict, attr_edits: dict = None) -> str:
    # ★数值必须递归归一：直接 json.dumps 会保留类型，{'limit':5} 与 {'limit':5.0} 得到两个令牌，
    #   二次确认门就会挡住操作者本人（2026-08-05 实测本函数正是如此）。
    _c = canon_for_token
    return _confirm_token({"path": "ware/save", "wareId": str(ware_id),
                           "sku": _json.dumps(_c(sku_edits or {}), sort_keys=True, ensure_ascii=False),
                           "spu": _json.dumps(_c(spu_edits or {}), sort_keys=True, ensure_ascii=False),
                           "attr": _json.dumps(_c(attr_edits or {}), sort_keys=True, ensure_ascii=False)})


def plan_ware_edit(ware_id: str, sku_edits: dict = None, spu_edits: dict = None,
                   attr_edits: dict = None, biz_id: Optional[str] = None) -> dict:
    """[编辑商品·只读] 加载整品→应用编辑→重建 save body→守门→出**改动 diff + confirm_token**。
    sku_edits={skuId:{shortTitle?,enable?}}；spu_edits={maxBuyTimes?,transportId?,promiseId?}；
    attr_edits={valueId:{name?,seq?}}（尺寸/颜色 名称/顺序，valueId 见 osw_ware_edit_get 的 saleAttrs）。
    仅放开这些字段；其余（图片/详情/京东价/采购价）原样回填。核对 changes 后传 ware_save。"""
    load_raw = ware_load(ware_id, biz_id=biz_id, raw=True).get("_raw") or {}
    body, changes = _build_save_body(load_raw, sku_edits or {}, spu_edits or {}, attr_edits or {})
    _guard(load_raw, body)
    if not changes:
        raise BlacklightError("没有任何改动（sku_edits/spu_edits/attr_edits 与当前值一致或为空）")
    return {"wareId": str(ware_id), "change_count": len(changes), "changes": changes,
            "guard": "passed（图片/SKU/富文本/属性无丢失）",
            "confirm_token": _edit_token(ware_id, sku_edits or {}, spu_edits or {}, attr_edits or {}),
            "note": "核对 changes 无误后，相同参数 + confirm=confirm_token 调 ware_save。整品覆盖，首次务必单字段+保存后读回。"}


def _gmall_save(body: dict, biz_id: str) -> dict:
    ck = _cookie()
    c = make_client(ck, origin=GX_ORIGIN, referer=GX_ORIGIN + "/", content_type="application/json",
                    timeout=30.0, extra={"x-requested-with": "XMLHttpRequest",
                                         "accept": "application/json, text/plain, */*"})
    params = {"curRole": "2", "belongBizId": str(biz_id), "requestClientType": "pc", "loginType": "7"}
    with c:
        r = c.post(GMALL_SAVE, params=params, content=_json.dumps(body, ensure_ascii=False))
    r.raise_for_status()
    try:
        return r.json()
    except Exception as e:
        raise BlacklightError(f"ware/save 未返回 JSON（HTTP {r.status_code}）") from e


@audited("ware", "save")
def ware_save(ware_id: str, sku_edits: dict = None, spu_edits: dict = None,
              attr_edits: dict = None, confirm: str = "", biz_id: Optional[str] = None) -> dict:
    """[编辑商品·真写] **真保存编辑商品**（gmall ware/save，整品覆盖）。
    需相同参数先 plan_ware_edit 拿 confirm_token 再带 confirm。守门通过才发。⚠️未活体：首次单字段+读回。"""
    token = _edit_token(ware_id, sku_edits or {}, spu_edits or {}, attr_edits or {})
    if confirm != token:
        return {"executed": False, "reason": "保存需二次确认：相同参数先跑 plan_ware_edit 拿 confirm_token 再带 confirm。"}
    bid = _biz_id(biz_id)
    load_raw = ware_load(ware_id, biz_id=bid, raw=True).get("_raw") or {}
    body, changes = _build_save_body(load_raw, sku_edits or {}, spu_edits or {}, attr_edits or {})
    _guard(load_raw, body)                             # 发送前再守门一次（防 load 期间漂移）
    if not changes:
        return {"executed": False, "reason": "没有任何改动"}
    resp = _gmall_save(body, bid)
    ok = bool(resp.get("success")) if isinstance(resp, dict) else None
    return {"executed": True, "confirm_token": token, "wareId": str(ware_id),
            "change_count": len(changes), "changes": changes, "success": ok, "response": resp}
