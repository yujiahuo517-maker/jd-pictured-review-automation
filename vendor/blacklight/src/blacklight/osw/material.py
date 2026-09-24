"""blacklight.osw.material —— **京喜商品素材维护**（osw.jd.com/materialCenter/materialScene）。

采销工作台里管「搜索主图 / 白底图 / 透明图 / 场景图 / 卖点图 / 搜索分发」的那一页。
和带图评价、图片空间同属一条内容链：**图片空间存图 → 本模块把图挂到 SKU 的素材位**。

## 嵌套（别在浏览器里死磕）
`osw.jd.com/materialCenter/...` 只是壳，真应用在**跨域 iframe** `ware-material-jdm.jd.com`
（micro-app → `osw-legacy-iframe`）。跨域 iframe 的 XHR 壳页 `read_network_requests` 抓不到、
`javascript_tool` 也进不去 —— 所以这套契约是直拉 bundle + 活体验证出来的，不是抓包。

## 网关（与图片空间同一个，只换 appId）
    POST https://sff.jd.com/api?v=1.0&appId=BD2QSA2XUESRKXAL1QKQ&api=<dsm.xxx>
    header  dsm-platform: erp        **cookie-only，h5st 非强制**
前端源码里写的是 `axios.post("/dsm/<api名>")`，dsm 插件再拼成上面这个 URL；
app.js 里一次性躺着 220 个端点（本模块只收编用得上的那几个）。

## ★accessContext 是这页唯一的坎（2026-08-17 试了 15 种组合）
| accessContext | 结果 |
|---|---|
| `businessModel: "jxpop"`（照抄 URL 参数） | **201 系统异常** |
| `businessModel: "self"` / `"jxSelf"` | **200 但 count=0** ← 最坑：空壳成功，会被当成"名下没数据" |
| `businessModel: "jxpop"` + `proxyVendorCode: "14691198"` | ✅ count=1991 |

- vendorId 由 `dsm.upload.ware.WareApiService.getAllJxPopVenderShop` 给（京喜自营官方店 14691198），
  与 `osw.product` 的 `belong-biz-id` 是同一个号。
- ⚠️`dsm.upload.ware.WareApiService.getBusinesssModel` 返回的三个码
  （`self` / `jxSupply` / `jxSelf`）**里面根本没有 `jxpop`**——照那个列表填必空。
  同 [[margin-rootcause-traps]] 一类：接口自己给的枚举不等于本页该用的值。

## 写路径
`batchBind` 收的 `imgUrl` 就是**图片空间的 jfs 相对路径**，于是
`llm.image_generate()(base64) → pic.imgzone.upload() → material.bind()` 是一条纯脚本闭环。
写接口一律 dry-run + ConfirmGate；未活体前在 `WRITE_VERIFICATION` 登记 verified=False。
"""
from __future__ import annotations

import json as _json
import time
from typing import Optional

from blacklight.core import BlacklightError, ConfirmGate, make_client, audited
from blacklight.core import auth as jd_auth
from blacklight.core.paging import fetch_paged

SFF_API = "https://sff.jd.com/api"
APP_ID = "BD2QSA2XUESRKXAL1QKQ"
ORIGIN = "https://ware-material-jdm.jd.com"
PAGE_URL = ORIGIN + "/materialCenter/materialScene?platform=erp&businessModel=jxpop"
CDN = "https://img10.360buyimg.com/imgzone/"
VENDER_ID = "14691198"                      # 京喜自营官方店；与 osw.product 的 belong-biz-id 同源

# ★分页上限是**服务端 10s 超时**逼出来的，不是参数校验：
#   ps=20→5.2s ✅  ps=30→5.1s ✅  ps=50→8.1s ✅（已贴边）  ps=80/100→10.1s **501 服务超时**（重试也必挂）
#   所以别把 501 当偶发去重试——它在 ps≥80 上是确定性的，只能调小页宽。
MAX_PAGE_SIZE = 50
DEFAULT_PAGE_SIZE = 30

# 素材位（前端 `$0` 枚举）。key 是接口里的 type，也是 materialImageList 的 map key
MATERIAL_TYPES = {
    51: "搜索主图",
    31: "白底图",
    36: "透明图",
    32: "场景图",
    3201: "场景图1",     # ⚠️仅 UI key，**不是线上值**，见 _WIRE_TYPE
    3202: "场景图2",     # ⚠️同上
    33: "卖点图",
    34: "营销图",
    52: "搜索分发",
    53: "推荐分发",
}
# 面板「一键生成全部素材」实际能 AI 兜的位（getAiBatchMaterialType 活体返回）
AI_MATERIAL_TYPES = (36, 31, 32)

# 单张素材的审核态（前端 `Qs` 枚举）
AUDIT_STATUS = {
    -1: "无效", 1: "转码中", 2: "转码成功", 3: "审核中",
    4: "审核通过", 5: "审核驳回", 6: "转码失败", 7: "全景处理中", -404: "未知",
}
# 素材任务态（前端 `I1` 枚举）——⚠️70=待采纳，不是终态
TASK_STATUS = {0: "等待中", 10: "处理中", 20: "失败", 30: "成功", 50: "已取消", 70: "待采纳"}

# 列表页筛选用的素材状态（getMaterialStatusFilterOptions 活体返回）
MATERIAL_STATUS = {0: "待补充", 3: "审核中", 4: "审核通过", 5: "审核驳回"}

# 素材场景（前端 `j` 枚举）——batchBind 的 sceneType
SCENE_TYPES = {1: "基础素材", 2: "搜索场景", 3: "推荐场景", 4: "促销场景", 5: "渠道场景", 6: "代运营场景"}
SCENE_MATERIAL = 1

# 单次挂位的 SKU 数护栏（平台未明示上限；本店有 192 SKU 的 SPU，一次全打风险太大）
MAX_BIND_SKUS = 50

# 素材任务类型（前端 `lu` 枚举）。★是**整数**不是字符串
TASK_TYPES = {
    1: "SkuBatchToolProcessingTask",           # SKU 批量工具
    2: "ExcelProcessingTask",                  # Excel 批量导入
    3: "OneClickGenerationTask",               # 一键生成
    4: "AllWarehouseOneClickGenerationTask",   # 全仓一键生成（面板那个大按钮）
    5: "AiBatchGenerateTask",                  # AI 批量生成（按 SKU）
    37: "WareListSkuBatchToolProcessingTask",
    38: "WareListOneClickGenerationTask",
    39: "WareListExcelProcessingTask",
}
_AI_BATCH_TASK = 5


# --------------------------------------------------------------------------- #
# 网关
# --------------------------------------------------------------------------- #
def _ctx(vender_id: Optional[str] = None) -> dict:
    """★这三个字段缺一不可，见模块 docstring 的表。"""
    return {"source": "web",
            "businessModel": "jxpop",
            "proxyVendorCode": str(vender_id or VENDER_ID)}


def _call(api: str, body: Optional[dict] = None, vender_id: Optional[str] = None,
          timeout: float = 40.0):
    ck = jd_auth.get_cookie()
    if not ck:
        raise BlacklightError("无登录态：先 osw_login / yx_login")
    payload = {"accessContext": _ctx(vender_id), **(body or {})}
    c = make_client(ck, origin=ORIGIN, referer=PAGE_URL,
                    content_type="application/json;charset=UTF-8", timeout=timeout)
    with c:
        r = c.post(SFF_API, params={"v": "1.0", "appId": APP_ID, "api": api},
                   content=_json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   headers={"dsm-platform": "erp"})
    try:
        j = r.json()
    except Exception as e:                              # noqa: BLE001
        raise BlacklightError(f"{api} 未返回 JSON（HTTP {r.status_code}）——登录态可能失效") from e
    if j.get("code") not in (200, "200"):
        raise BlacklightError(f"{api} code={j.get('code')}: {str(j.get('msg'))[:300]}")
    return j.get("data")


def full_url(content: str) -> str:
    """素材的 `content`（jfs 相对路径）→ 完整 CDN URL。与 pic.imgzone.full_url 同源。"""
    s = str(content or "").strip()
    if not s:
        return ""
    return s if s.startswith("http") else CDN + s.lstrip("/")


# --------------------------------------------------------------------------- #
# 读：店铺 / 缺口
# --------------------------------------------------------------------------- #
def vendors() -> list:
    """可选的京喜店铺（proxyVendorCode 从这里来）。"""
    d = _call("dsm.upload.ware.WareApiService.getAllJxPopVenderShop") or []
    return [{"vender_id": str(x.get("venderId") or ""), "vender_name": x.get("venderName")}
            for x in d]


def gap(types=None, vender_id: Optional[str] = None) -> dict:
    """各素材位的**未维护 SKU 数** —— 页面顶部「补充缺少SKU素材统计」那块的数据源。

    2026-08-17 活体：搜索主图 1990 / 搜索分发 1991 / 推荐分发 1991 /
    场景图 320 / 卖点图 172 / 透明图 112 / 白底图 85（在册 1991 个 SPU）。
    """
    ts = list(types) if types else [51, 31, 36, 32, 33, 52, 53]
    rows = []
    for t in ts:
        d = _call("dsm.media.material.WareMaterialService.getUnmaintainedInfo",
                  {"materialType": int(t)}, vender_id) or []
        hit = next((x for x in d if x and int(x.get("type", -1)) == int(t)), None)
        rows.append({"type": int(t),
                     "name": MATERIAL_TYPES.get(int(t), str(t)),
                     "unmaintained": int((hit or {}).get("unmaintainedCount") or 0)})
    rows.sort(key=lambda r: -r["unmaintained"])
    return {"vender_id": str(vender_id or VENDER_ID), "rows": rows}


# --------------------------------------------------------------------------- #
# 读：商品素材列表
# --------------------------------------------------------------------------- #
def _norm_material(m: dict) -> dict:
    st = m.get("status")
    return {"material_id": m.get("materialId"),
            "type": m.get("type"),
            "type_name": MATERIAL_TYPES.get(m.get("type"), str(m.get("type"))),
            "url": full_url(m.get("content")),
            "content": m.get("content"),
            "status": st,
            "status_name": AUDIT_STATUS.get(st, str(st)),
            "order": m.get("order")}


def _norm_sku(s: dict, product_id=None) -> dict:
    mats = {}
    for k, v in (s.get("materialImageList") or {}).items():
        mats[int(k)] = [_norm_material(m) for m in (v or [])]
    # 支持的素材位（materialBaseInfoMap.isSupport==1）——不支持的位不该算缺口
    supported = [int(k) for k, v in (s.get("materialBaseInfoMap") or {}).items()
                 if (v or {}).get("isSupport") == 1]
    # 销售属性藏两层：saleAttrInfos[].attrValueAlias[] 是**JSON 字符串**，里面才是
    # [{modelName:"层数", value:"5", unit:"层"}]。拍平成 {层数: "5层"} 供生图 prompt 用。
    # ⚠️`attrValueAlias` 的条目**不一定有 `modelName`**：实测 60 个多 SKU 的 SPU 里
    #   49 个的 alias 形如 `{"id":20319,"value":"白色【三层】高80cm"}`——只有 value 没有名字。
    #   早先要求 modelName 存在才收，导致这 82% 的 SKU 解析出空 attrs，
    #   下游"取值有没有差异"的判断跟着全部失灵（会误判成"没差异 ⇒ 可复用"）。
    #   没有名字就用 `属性<index>` 兜底，**值一定要收下**。
    attrs = {}
    for a in (s.get("saleAttrInfos") or []):
        idx0 = a.get("index")
        for alias in (a.get("attrValueAlias") or []):
            try:
                items = _json.loads(alias)
            except Exception:                           # noqa: BLE001
                continue
            for j, it in enumerate(items if isinstance(items, list) else [items]):
                v = it.get("value")
                if v is None:
                    continue
                k = it.get("modelName") or (f"属性{idx0}" if len(items) == 1
                                            else f"属性{idx0}-{j + 1}")
                attrs[str(k)] = f"{v}{it.get('unit') or ''}"
    # 平台「批量应用相同颜色/相同尺码」的分组签名：取 saleAttrInfos 里 index==N 的那一项、
    # 去掉 index 后按 key 排序序列化，签名相同即同组（前端 handleBatchUse 的原样口径）。
    # ★分组看的是**销售属性**，不是主图 —— 见 material_gen.sibling_groups 的注释。
    sig = {}
    for a in (s.get("saleAttrInfos") or []):
        idx = a.get("index")
        if idx is None:
            continue
        item = {k: v for k, v in a.items() if k != "index"}
        sig[int(idx)] = _json.dumps(item, ensure_ascii=False, sort_keys=True)
    return {
        "sku_id": s.get("skuId"),
        "product_id": s.get("productId") or product_id,
        "sku_name": s.get("skuName"),
        "sale_attrs": attrs,
        "sale_attr_sig": sig,
        "short_title": (s.get("skuFeatureMap") or {}).get("shortTitle"),
        "logo": full_url(s.get("logo")),
        "category_id": s.get("categoryId"),
        "materials": mats,
        "material_types": sorted(mats.keys()),
        "supported_types": sorted(supported),
        "missing_types": sorted(t for t in supported if not mats.get(t)),
        "buttons": [b.get("code") for b in (s.get("operateButtons") or []) if b],
    }


def _norm_spu(p: dict) -> dict:
    skus = [_norm_sku(s, p.get("productId")) for s in (p.get("skuInfoList") or []) if s]
    f = skus[0] if skus else {}
    # ⚠️本场景下 SPU 层的 productName/logo/jdPrice 一律为 null（页面「商品信息」列
    #   其实渲染的是第一条 SKU）——不兜底会得到一屏 null，被误判成"接口没数据"
    return {
        "product_id": p.get("productId"),
        "product_name": p.get("productName") or f.get("sku_name"),
        "item_num": p.get("itemNum"),
        "jd_price": p.get("jdPrice"),
        "product_status": p.get("productStatus"),
        "sku_count": p.get("childCount"),
        "distribute_sku_count": p.get("distributeSkuCount"),
        "category_id": p.get("categoryId") or f.get("category_id"),
        "logo": full_url(p.get("logo")) or f.get("logo"),
        "score": p.get("score"),
        # ⚠️列表接口每个 SPU 只回**第一条 SKU 有素材明细**，其余是空壳（只有 skuId）。
        #   要全部 SKU 的素材必须 list_sku(product_id) 展开。
        "skus": skus,
        "first_sku": skus[0] if skus else None,
    }


def _spu_query(page: int, page_size: int, *, product_name=None, brand_id=None,
               category_id=None, product_ids=None, sku_ids=None,
               product_status=None, material_status=None, supply_unit=None) -> dict:
    q = {"pageIndex": int(page), "pageSize": int(page_size), "scene": "search"}
    if product_name:
        q["productName"] = str(product_name).strip()
    if brand_id:
        q["brandId"] = brand_id
    if category_id:
        q["categoryIds"] = [category_id] if not isinstance(category_id, (list, tuple)) else list(category_id)
    if product_ids:
        q["productIds"] = [str(x) for x in product_ids]
    if sku_ids:
        q["skuIds"] = [str(x) for x in sku_ids]
    if product_status is not None:
        q["productStatusList"] = [product_status]
    if material_status:
        # 前端只对 51/52 两个位下发；这里允许 {type: [status...]} 直传
        if isinstance(material_status, dict):
            q["materialStatus"] = {str(k): list(v) for k, v in material_status.items()}
        else:
            st = list(material_status)
            q["materialStatus"] = {"51": st, "52": st}
    if supply_unit is not None:
        q["supplyUnit"] = supply_unit
    return {"productQuery": q}


def list_spu(page: int = 1, page_size: int = 20, vender_id: Optional[str] = None, **filters) -> dict:
    """按 SPU 分页列商品素材。filters 见 `_spu_query`。"""
    if int(page_size) > MAX_PAGE_SIZE:
        raise BlacklightError(f"page_size 上限 {MAX_PAGE_SIZE}")
    d = _call("dsm.upload.material.ware.queryMaterialSpuList",
              _spu_query(page, page_size, **filters), vender_id) or {}
    pd = d.get("pageData") or {}
    return {"total": pd.get("count"),
            "page": int(page),
            "page_size": int(page_size),
            "rows": [_norm_spu(x) for x in (pd.get("data") or []) if x]}


def all_spu(max_rows: int = 3000, page_size: int = DEFAULT_PAGE_SIZE,
            vender_id: Optional[str] = None, **filters) -> dict:
    """翻页拉全量 SPU（带截断闸 + 与服务端 total 交叉验证）。

    ⚠️同 [[subsidy-pool-pull-truncation]]：全量拉取会静默截断，所以这里除了
    `fetch_paged` 的去重/死循环闸，还要把 total 和实取行数对上报出来，由调用方判定。
    ⚠️**慢**：每页约 5s，1991 个 SPU 全量约 5～6 分钟。先用 `missing()` 缩范围再拉。
    """
    if int(page_size) > MAX_PAGE_SIZE:
        raise BlacklightError(f"page_size 上限 {MAX_PAGE_SIZE}")
    total = {"v": None}
    fetched = {"n": 0}

    def _fetch(pg, ps):
        if fetched["n"] >= max_rows:
            return []                       # 达到上限：回空页让 fetch_paged 正常收尾
        d = list_spu(pg, ps, vender_id, **filters)
        if pg == 1:
            total["v"] = d.get("total")
        fetched["n"] += len(d["rows"])
        return d["rows"]

    rows = fetch_paged(_fetch, int(page_size), key=lambda r: r["product_id"],
                       max_pages=max(1, -(-max_rows // int(page_size)) + 1),
                       what="商品素材列表")[:max_rows]
    total = total["v"]
    return {"total": total, "fetched": len(rows),
            "complete": total is not None and len(rows) >= int(total),
            "note": None if (total is not None and len(rows) >= int(total))
                    else f"实取 {len(rows)} < 服务端 total {total}，**按未取全处理**（切片重拉或调大 max_rows）",
            "rows": rows}


def list_sku(product_id, vender_id: Optional[str] = None) -> dict:
    """展开一个 SPU 下**全部 SKU** 的素材明细（列表接口只回第一条）。

    ⚠️返回体里 `pageData.count` 恒为 0（服务端没填），别拿它判空——看 `rows`。
    """
    d = _call("dsm.upload.material.ware.queryMaterialSkuList",
              {"productQuery": {"productId": str(product_id), "scene": "search"}}, vender_id) or {}
    pd = d.get("pageData") or {}
    rows = [_norm_sku(x, product_id) for x in (pd.get("data") or []) if x and x.get("skuId")]
    return {"product_id": str(product_id), "count": len(rows), "rows": rows}


def missing(material_type: int, page: int = 1, page_size: int = 20,
            vender_id: Optional[str] = None, **filters) -> dict:
    """列出**某个素材位待补充**的 SPU（`materialStatus={type:[0]}`）。

    ★两个「缺口」口径不一样，别混（同 [[erp-scope-assistant-vs-saler]] 那类坑）：
      - `gap()` 是 **SKU 维度**未维护数（白底图 85）
      - 本函数 total 是 **SPU 维度**（白底图 68）
      一个 SPU 下多个 SKU 缺同一个位，前者数 3 次后者数 1 次。汇报时必须写明是哪个维度。

    ⚠️前端只对 51/52 两个位下发 materialStatus，但服务端**对任意 type 都生效**
    （31/32/51/52 已逐个活体验证：1991 全量 → 68/303/1973/1672）。
    """
    t = int(material_type)
    if t not in MATERIAL_TYPES:
        raise BlacklightError(f"素材位 {t} 不在 {sorted(MATERIAL_TYPES)}")
    d = list_spu(page, page_size, vender_id, material_status={t: [0]}, **filters)
    d["material_type"] = t
    d["material_name"] = MATERIAL_TYPES[t]
    d["dimension"] = "SPU（注意 gap() 是 SKU 维度，数字会更大）"
    return d


def status_filter_options(vender_id: Optional[str] = None) -> list:
    """列表页「素材状态」级联筛选项（服务端权威，别硬编码）。"""
    return _call("dsm.media.material.WareMaterialService.getMaterialStatusFilterOptions",
                 {}, vender_id) or []


def running_tasks(vender_id: Optional[str] = None) -> list:
    """在途的素材任务。**批量发起 AI 生成前必看**——同 [[bybt-concurrent-enroll-lock]]，
    在途锁会把后发的请求挡掉，看起来像平台拒绝。"""
    return _call("dsm.media.service.materialTaskApiService.queryExistExecuteMaterialTask",
                 {}, vender_id) or []


# --------------------------------------------------------------------------- #
# 写：绑图 / AI 生成   —— 均未活体，WRITE_VERIFICATION 登记 verified=False
# --------------------------------------------------------------------------- #
_BIND_GATE = ConfirmGate("osw/material_bind")
_AI_GATE = ConfirmGate("osw/material_ai_generate")


# ★★素材图的硬规格（2026-08-18 活体撞出来的，接口不会提前告诉你）
#   - **imgUrl 必须是 jfs 相对路径**。传完整 CDN URL → `无效的图片！url:https://...`
#   - **尺寸 800×800**、白底图/场景图/卖点图用 **JPG**，透明图用 **PNG（带真 alpha）**。
#     1254×1254 PNG 挂 31 位 → `图片格式不符合规则！url:jfs/...`；
#     同一张图转成 800×800 JPG 后立刻通过。（格式与尺寸是一起改的，未单独拆变量，
#     但平台自有素材全是 800×800、31 位 JPG / 36 位 PNG，按这个口径走就对。）
#   - 回读的 `order` 是 **1 起**（发 imgOrder 0/1 → 读回 order 1/2），别拿来直接回填。
SLOT_SPEC = {31: "jpg", 32: "jpg", 33: "jpg", 34: "jpg", 51: "jpg", 52: "jpg", 53: "jpg",
             36: "png"}          # 36 透明图必须 PNG 且带 alpha
SLOT_SIZE = (800, 800)


def normalize_image(src, material_type: int, out_path: str) -> str:
    """把任意图归一化成该素材位能收的规格（补方 → 800×800 → JPG/PNG）。

    不做这步，`batchBind` 会以 `图片格式不符合规则` 拒掉，而错误文案不说该改成什么。
    透明图走 PNG 并保留 alpha；其余走 JPG（补方用四角采样色填充，不拉伸变形）。
    """
    try:
        from PIL import Image
    except ImportError as e:                            # noqa: BLE001
        raise BlacklightError("normalize_image 需要 Pillow：pip install pillow") from e
    t = _WIRE_TYPE.get(int(material_type), (int(material_type), 0))[0]
    keep_alpha = SLOT_SPEC.get(t) == "png"
    im = Image.open(src)
    im = im.convert("RGBA") if keep_alpha else im.convert("RGB")
    w, h = im.size
    if w != h:                                          # 补成正方形，不拉伸
        s = max(w, h)
        bg = im.convert("RGB").getpixel((2, 2))
        canvas = Image.new(im.mode, (s, s), bg + (255,) if keep_alpha else bg)
        canvas.paste(im, ((s - w) // 2, (s - h) // 2))
        im = canvas
    im = im.resize(SLOT_SIZE, Image.LANCZOS)
    if keep_alpha:
        im.save(out_path, "PNG")
    else:
        im.save(out_path, "JPEG", quality=92)
    return out_path


# ★场景图 1/2 在**线上都是 32**，靠 imgOrder 0/1 区分；`3201/3202` 只是前端的 UI key，
#   直接当 smartPicType 发出去是错的。权威映射来自 bundle 的 HI/qV 两个函数：
#     HI = {smartPicWhite:31, smartPicScene:32, smartPicScene2:32, smartPicSell:33,
#           smartPicMarket:34, smartThroughPic:36, mainVideo:1, searchImage:51}
#     qV(32, order) = order===1 ? "smartPicScene2" : "smartPicScene"
_WIRE_TYPE = {3201: (32, 0), 3202: (32, 1)}


def _bind_body(product_id, sku_ids, images, *, scene_type: int = SCENE_MATERIAL,
               exist_no_replace: bool = True) -> dict:
    """images: [{"url": jfs相对路径或完整URL, "type": 31, "order": 0}, ...]

    `type` 收 3201/3202 时自动折成 (32, order=0/1)。
    ⚠️平台前端有个顺序约束：**没有场景图1 时不让传场景图2**（"uploadFirstSceneImage"）。
    """
    if not sku_ids:
        raise BlacklightError("sku_ids 不能为空")
    # ⚠️一次挂位可以打到很多 SKU（复用同一张图时按外观组下发）。平台侧未见明确上限，
    #   但本店存在 192 个 SKU 的 SPU —— 一次全打出去，错了也是一次全错、且回滚要逐条。
    #   超过阈值直接拦下，让调用方切片，宁可多几次调用。
    if len(sku_ids) > MAX_BIND_SKUS:
        raise BlacklightError(
            f"一次挂位 {len(sku_ids)} 个 SKU 超过上限 {MAX_BIND_SKUS}——请切片下发"
            "（平台未明示上限，这是我们自己的护栏：错一次就是一片）")
    imgs = []
    for i, im in enumerate(images or []):
        u = str(im.get("url") or im.get("imgUrl") or "").strip()
        if not u:
            raise BlacklightError(f"images[{i}] 缺 url")
        t = im.get("type", im.get("smartPicType"))
        if int(t) not in MATERIAL_TYPES:
            raise BlacklightError(f"images[{i}] 素材位 {t} 不在 {sorted(MATERIAL_TYPES)}")
        wire, order = _WIRE_TYPE.get(int(t), (int(t), None))
        if order is None:
            order = int(im.get("order", 0))
        # 接口收的是 jfs 相对路径（图片空间口径），完整 URL 要剥掉 CDN 前缀
        if u.startswith(CDN):
            u = u[len(CDN):]
        imgs.append({"imgUrl": u, "smartPicType": wire, "imgOrder": order})
    if not imgs:
        raise BlacklightError("images 不能为空")
    return {"apiMaterialPicInfo": {
        "sceneType": int(scene_type),
        "apiRelativeSkus": {"productId": str(product_id), "skuIds": [str(s) for s in sku_ids]},
        "imgList": imgs,
        "existNoReplace": bool(exist_no_replace),
    }}


def bind_dryrun(product_id, sku_ids, images, *, exist_no_replace: bool = True) -> dict:
    """挂位 dry-run：回显将写入的位/图/影响的 SKU，出 confirm_token，**不发写请求**。"""
    body = _bind_body(product_id, sku_ids, images, exist_no_replace=exist_no_replace)
    info = body["apiMaterialPicInfo"]
    return {
        "action": "把图片挂到 SKU 的素材位（batchBind）",
        "product_id": str(product_id),
        "sku_count": len(info["apiRelativeSkus"]["skuIds"]),
        "sku_ids": info["apiRelativeSkus"]["skuIds"],
        "images": [{"位": MATERIAL_TYPES.get(i["smartPicType"], i["smartPicType"]),
                    "url": full_url(i["imgUrl"]), "order": i["imgOrder"]} for i in info["imgList"]],
        "exist_no_replace": info["existNoReplace"],
        "warning": "图必须是 **800×800**、31/32/33 用 JPG、36 用带 alpha 的 PNG，"
                   "否则报「图片格式不符合规则」——先过 normalize_image()。"
                   "existNoReplace=False 会**覆盖已有素材**。",
        "confirm_token": _BIND_GATE.body_token(body),
        "body": body,
    }


# 平台在**上一次挂位还没落库**时会这么回，文案固定是这句。组越大越容易撞：一次挂 20 个 SKU
# 比挂 4 个慢得多，紧接着的下一个素材位就会踩到（2026-08-19 实撞：外观分组把数量维度合并后
# 组变大，一批里两条 3202 因此报错）。
# ★★**它不是失败，是"已排进后台队列"**：当天那两条报错的位，事后回读**都已挂上且审核通过**。
#   ⇒ 收到这句**不要判失败**（我当天把它记成 bind_fail、还去补跑，白折腾）。
#   本函数对它退避重试是为了让 ok 反映最终状态；重试仍报同一句时，**以稍后 list_sku 回读为准**。
_BIND_BUSY = "素材正在后台批量绑定中"


@audited("osw", "material_bind")
def bind(product_id, sku_ids, images, *, exist_no_replace: bool = True,
         confirm: str = "", vender_id: Optional[str] = None,
         retries: int = 4, backoff: float = 4.0) -> dict:
    """真挂位。⚠️服务端把逐条错误塞在 **HTTP 200** 的返回数组里，本函数已解析成 `errors`——
    调用方**必须看 `ok` 而不是异常**，否则会把失败当成功。

    `retries`：仅对「素材正在后台批量绑定中」这类**瞬时节流**重试（退避 backoff 秒递增），
    其它错误立即返回，不重试——把真失败重试掉只会拖时间并掩盖问题。
    """
    body = _bind_body(product_id, sku_ids, images, exist_no_replace=exist_no_replace)
    _BIND_GATE.check_body(confirm, body)
    attempt = 0
    while True:
        d = _call("dsm.media.material.imageRelations.batchBind", body, vender_id)
        # 服务端把逐条错误塞在数组里返回，code 仍是 200 —— 不看这层会把失败当成功
        errs = ([x for x in (d or []) if isinstance(x, dict) and x.get("errorMsg")]
                if isinstance(d, list) else [])
        msgs = [x.get("errorMsg") for x in errs]
        if not errs or attempt >= int(retries) or not all(_BIND_BUSY in str(m) for m in msgs):
            return {"ok": not errs, "errors": msgs, "raw": d, "attempts": attempt + 1,
                    "note": "回读用 list_sku(product_id) 看素材位是否出现、status 是否走到 4（审核通过）"}
        attempt += 1
        time.sleep(float(backoff) * attempt)


def ai_generate_dryrun(sku_ids, material_types=AI_MATERIAL_TYPES) -> dict:
    """「一键生成全部素材」的 SKU 维度版（createTask / AiBatchGenerateTask）。"""
    ts = [int(t) for t in material_types]
    bad = [t for t in ts if t not in AI_MATERIAL_TYPES]
    if bad:
        raise BlacklightError(f"素材位 {bad} 不在平台 AI 可生成范围 {list(AI_MATERIAL_TYPES)}"
                              "（getAiBatchMaterialType 活体口径）")
    ids = [str(s) for s in (sku_ids or [])]
    if not ids:
        raise BlacklightError("sku_ids 不能为空")
    if len(ids) > 500:
        raise BlacklightError(f"前端硬限 500 个 SKU/次，当前 {len(ids)}")
    body = {"param": {"taskType": _AI_BATCH_TASK, "businessIdList": ids,
                      "contentMap": {"needMaterialType": ",".join(str(t) for t in ts)}}}
    return {"action": "发起平台 AI 批量生成素材（createTask）",
            "sku_count": len(ids),
            "material_types": [MATERIAL_TYPES.get(t, t) for t in ts],
            "warning": "⚠️未活体（verified=False）。发起前先 running_tasks() 看在途，"
                       "同 [[bybt-concurrent-enroll-lock]]：在途锁会把后发的挡掉。"
                       "生成结果 taskStatus=70 是**待采纳**不是生效。",
            "confirm_token": _AI_GATE.body_token(body),
            "body": body}


@audited("osw", "material_ai_generate")
def ai_generate(sku_ids, material_types=AI_MATERIAL_TYPES, *, confirm: str = "",
                vender_id: Optional[str] = None) -> dict:
    """发起平台 AI 批量生成素材。**未活体**（verified=False），首次请小批量试。"""
    plan = ai_generate_dryrun(sku_ids, material_types)
    body = plan["body"]
    _AI_GATE.check_body(confirm, body)
    d = _call("dsm.media.service.materialTaskApiService.createTask", body, vender_id)
    return {"ok": True, "raw": d,
            "note": "回读 running_tasks()；结果落到素材位后再用 list_sku 确认 status"}
