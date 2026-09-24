"""blacklight.pic.client —— 京喜带图评价平台接口面（`osw.jd.com/picstart`）。

**这是什么**：采销工作台「商品 / 带图评价」页。给自己名下**没有带图评价**的 SKU 补一条
「评价文本 + 实拍图」，走机审/人审后同步到前台评价区。页面顶部原文：该功能属保密项目，
严禁外传、禁止向商家宣贯 —— 所以本模块只在内部链路用，任何导出/发布都不要带出去。

**网关**：真实前端在跨域微应用 `jxcms-pro.local-pf.jd.com/picstart`，后端全部走
`api.m.jd.com` + `colorAppId=wqadmin` + `loginType=7` —— 与 osw_margin 是**同一套签名**
（HMAC-MD5 signStr），故本模块直接借 `osw_margin._client/_api`，不另起鉴权。

**接口全集**（2026-08-17 从前端 bundle `storage.360buyimg.com/pubfree-bucket/jxcms/*/umi.*.js`
逆向 + 只读活体验证，见 docs/osw/NOTES_piceval.md）：
  - `querySkuByPage`   待补清单（**imageFilter=1 无评价 / 0 全部**）★数据源
  - `queryTaskList`    已提交任务 + 审核态（**必带 taskType=2**，不带报 code=14）
  - `generateAIEval`   给图生文案（平台自带 AI，配额见 queryUserInfo.generateEvaluateNum）
  - `importImageEval`  ★写：提交一条带图评价
  - `queryUserInfo`    erp / 权限 / 组织（**GET**；POST 会 code=1）
  - `auditTask`        人审通过（**payload 未反解，本模块不实现**，避免瞎猜）

**判生效只认 taskStatus=330**（EFFECTIVE）。提交成功 ≠ 生效：中间还有机审(1→3/4)、人审(6)、
评价中台处理(210/310)。这与「报名≠生效」是同一类坑，回读一律用 `task_list`。

## ⚠️返回键速查（**三个函数三种键**，今天被绊了三次）
| 函数 | 列表键 |
|---|---|
| `targets()` / `targets_page()` | `items` |
| `task_list()` | `tasks` |
| `pic.imgzone.list_images()` | `images` |
取错键的表现是**「total 有值、行数为 0」**——看起来像"名下没有"，
实际是键名写错。判空前先 `sorted(d.keys())` 看一眼。
"""
from __future__ import annotations

import json as _json
import os
import time as _time

import re as _re
from typing import Optional

from blacklight.core import BlacklightError
from blacklight.core import paths as _paths, ConfirmGate, audited
from blacklight.osw import margin as _osw_margin   # 仅借：签名 + 带 cookie 的客户端（同 wqadmin secret）

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
FID_SKU_PAGE = "jxzy_aievalandsales_querySkuByPage"
FID_TASK_LIST = "jxzy_aievalandsales_queryTaskList"
FID_USER_INFO = "jxzy_aievalandsales_queryUserInfo"
FID_AI_EVAL = "jxzy_aievalandsales_generateAIEval"
FID_IMPORT = "jxzy_aievalandsales_importImageEval"

TASK_TYPE_PIC = 2                 # 带图评价（前端写死；不传后端报 code=14 参数错误）
IMAGE_FILTER_NO_EVAL = 1          # 无评价（页面默认）
IMAGE_FILTER_ALL = 0

MAX_EVAL_CHARS = 1000             # 前端硬校验：评价文本 >1000 字直接拒
MAX_IMAGES_PER_EVAL = 9           # 模板给到 实拍图1..9
DEFAULT_MAX_TASKS_PER_SKU = 10    # 实测 querySkuByPage 回 maxImageTaskNum=10（以接口返回为准）
MAX_IMPORT_BATCH = 200            # 本模块自设：单次提交上限（平台无明示，串行提交怕跑飞）

# taskStatus 枚举（前端 bundle 原样）
TASK_STATUS = {
    1: "机审中", 3: "机审通过", 4: "机审拒绝", 6: "人审通过",
    10: "创建中", 11: "已有评价", 13: "同步评价任务失败", 20: "创建失败",
    210: "处理中", 220: "创建失败", 310: "处理中(评价中台)", 320: "创建失败", 330: "生效",
}
STATUS_EFFECTIVE = 330
STATUS_PENDING = (1, 10, 210, 310)
STATUS_FAILED = (4, 13, 20, 220, 320)

_GATE = ConfirmGate("pic/import_eval")
_SKU_RE = _re.compile(r"^\d{6,20}$")


# --------------------------------------------------------------------------- #
# 底层请求
# --------------------------------------------------------------------------- #
def _call(function_id: str, body: dict = None, method: str = "POST"):
    """签名 + 发一次 picstart 请求 → 校验 code==0 → 返回 data。"""
    client = _osw_margin._client()
    with client:
        raw = _osw_margin._api(client, function_id, body or {}, method)
    if not isinstance(raw, dict):
        raise BlacklightError(f"{function_id} 返回异常")
    if raw.get("code") not in (0, "0"):
        msg = raw.get("msg") or raw.get("message") or f"code={raw.get('code')}"
        raise BlacklightError(f"{function_id}: {msg}")
    return raw.get("data")


def _sku(v) -> str:
    s = str(v or "").strip()
    if not _SKU_RE.match(s):
        raise BlacklightError(f"SKU 格式不对：{v!r}（应为 6~20 位数字）")
    return s


# --------------------------------------------------------------------------- #
# 只读：身份 / 权限
# --------------------------------------------------------------------------- #
def user_info() -> dict:
    """当前 ERP 在带图评价系统里的身份：erp / 组织 / 权限 / **AI 生成额度**。

    authorityList 关键位：`AIEVALANDSALES-IMAGE-IMPORT`(批量导入带图评价)、
    `AIEVALANDSALES-IMPORT`(导入SKU)、`AIEVALANDSALES-AUDIT`(审批)、`AIEVALANDSALES-READ`。
    没有 IMAGE-IMPORT 就别往下跑采集了 —— 采完也提交不了。"""
    d = _call(FID_USER_INFO, {}, "GET") or {}
    auth = d.get("authorityList") or []
    return {"erp": d.get("erp"), "is_jx": d.get("isJx"),
            "ai_quota": d.get("generateEvaluateNum"),
            "roles": d.get("roleList") or [], "authorities": auth,
            "can_import": "AIEVALANDSALES-IMAGE-IMPORT" in auth,
            "can_audit": "AIEVALANDSALES-AUDIT" in auth,
            "orgs": d.get("organizationList") or []}


# --------------------------------------------------------------------------- #
# 只读：待补清单（数据源）
# --------------------------------------------------------------------------- #
def _norm_target(it: dict) -> dict:
    used = int(it.get("imageTaskNum") or 0)
    return {"sku_id": str(it.get("skuId") or ""), "spu_id": str(it.get("productId") or ""),
            "sku_name": it.get("skuName") or "", "sku_image": it.get("skuImage") or "",
            "sku_status": it.get("skuStatus"), "used": used,
            "result_type": it.get("resultType"), "result_msg": it.get("resultMsg")}


def targets_page(page_no: int = 1, page_size: int = 100, sku_ids: list = None,
                 spu_ids: list = None, image_filter: int = IMAGE_FILTER_NO_EVAL) -> dict:
    """[只读] 待补带图评价的商品（单页）。`image_filter` 1=无评价(默认) / 0=全部。

    ⚠️**页内条数可能少于 page_size**（服务端在页内过滤，前端就是靠这个继续翻下一页），
    所以**别拿"这页短了"当作到底了** —— 终止条件只能是累计条数 ≥ totalItem 或翻满页数。"""
    body = {"pageNo": int(page_no), "pageSize": int(page_size), "imageFilter": int(image_filter)}
    if sku_ids:
        body["skuIds"] = ",".join(_sku(s) for s in sku_ids)
    if spu_ids:
        body["spuIds"] = ",".join(str(s).strip() for s in spu_ids if str(s).strip())
    d = _call(FID_SKU_PAGE, body) or {}
    return {"total": d.get("totalItem") or 0,
            "max_per_sku": d.get("maxImageTaskNum") or DEFAULT_MAX_TASKS_PER_SKU,
            "page_no": page_no, "page_size": page_size,
            "items": [_norm_target(x) for x in (d.get("data") or [])]}


def targets(limit: int = 200, page_size: int = 100, sku_ids: list = None,
            spu_ids: list = None, image_filter: int = IMAGE_FILTER_NO_EVAL,
            max_pages: int = 200) -> dict:
    """[只读] **待补清单全量翻页**（数据源入口）。返回 {total, max_per_sku, count, short_pages, items[]}。

    `limit` 取够就停（默认 200；传 0 表示不设上限、翻到 total 或 max_pages）。
    `short_pages` = 返回条数少于 page_size 的页数 —— **不是错误**，是服务端页内过滤；
    但如果 count 远小于 total 且 short_pages 很多，说明还有大量残页，要么加大 max_pages、
    要么按 spu/sku 分片拉，别直接把 count 当作"就这么多"。"""
    got, short, page = [], 0, 1
    total = 0
    max_per = DEFAULT_MAX_TASKS_PER_SKU
    while page <= max_pages:
        d = targets_page(page, page_size, sku_ids, spu_ids, image_filter)
        total, max_per = d["total"], d["max_per_sku"]
        items = d["items"]
        if len(items) < page_size:
            short += 1
        got.extend(items)
        if limit and len(got) >= limit:
            got = got[:limit]
            break
        if page * page_size >= total:      # ★终止只看 total，不看本页是否为空
            break
        page += 1
    return {"total": total, "max_per_sku": max_per, "count": len(got),
            "pages_read": page, "short_pages": short, "items": got,
            "note": "count<total 属正常（服务端页内过滤/limit 截断）；要全量把 limit=0 且调大 max_pages。"}


# --------------------------------------------------------------------------- #
# 只读：任务回读（判生效）
# --------------------------------------------------------------------------- #
def _norm_task(t: dict) -> dict:
    ext = t.get("taskExtInfo") or {}
    _il = [i for i in (ext.get("imageInfoList") or []) if isinstance(i, dict)]
    imgs = [i.get("imageUrl") for i in _il]
    # ★逐图明细一定要留：`imageCrc` 是平台白送的**判重指纹**，`systemCheckMsg` 是**逐图拒因**
    #   （列表页只显示"机审都不通过"那句没信息量的话）。
    #   曾经只留 imageUrl，结果做拒因统计/建判重库时才发现原料被规范化时丢了。
    details = [{"url": i.get("imageUrl"), "crc": i.get("imageCrc"),
                "check_msg": (i.get("systemCheckMsg") or "").strip(),
                "check_status": i.get("systemCheckStatus")} for i in _il]
    st = t.get("taskStatus")
    return {"task_id": t.get("taskId"), "sku_id": str(t.get("skuId") or ""),
            "spu_id": str(t.get("spuId") or ""), "sku_name": t.get("skuName") or "",
            "status": st, "status_cn": TASK_STATUS.get(st, f"未知({st})"),
            "effective": st == STATUS_EFFECTIVE,
            "remark": t.get("taskRemarks") or "",
            "eval_content": ext.get("aigcComment") or "", "images": imgs,
            "image_details": details,
            "parent_sku_id": str(t.get("parentSkuId") or ""),   # 同品来源（平台自己也记这个）
            "input_erp": t.get("inputErp"), "sku_erp": t.get("skuErp"),
            "create_time": t.get("createTime"), "update_time": t.get("updateTime"),
            "valid_time": t.get("validTime")}


def task_list(page: int = 1, page_size: int = 20, sku_ids: list = None, spu_ids: list = None,
              status: Optional[int] = None, input_erp: str = None,
              start_time: str = None, end_time: str = None) -> dict:
    """[只读] 已提交的带图评价任务 + 审核态。**判生效只认 status=330**。

    时间形如 'YYYY-MM-DD HH:MM:SS'。平台列表最多展示 20000 条，超了要加筛选。"""
    body = {"taskType": TASK_TYPE_PIC, "pageNum": int(page), "pageSize": int(page_size)}
    if sku_ids:
        body["skuIds"] = ",".join(_sku(s) for s in sku_ids)
    if spu_ids:
        body["spuIds"] = ",".join(str(s).strip() for s in spu_ids if str(s).strip())
    if status is not None and status != "":
        body["status"] = int(status)
    if input_erp:
        body["inputErp"] = input_erp
    if start_time:
        body["startTime"] = start_time
    if end_time:
        body["endTime"] = end_time
    d = _call(FID_TASK_LIST, body) or {}
    return {"total": d.get("totalCount") or 0, "page": d.get("page") or page,
            "page_size": d.get("pageSize") or page_size, "total_page": d.get("totalPage"),
            "tasks": [_norm_task(t) for t in (d.get("taskList") or [])]}


def task_stats(sku_ids: list = None, input_erp: str = None, start_time: str = None,
               end_time: str = None, scan_pages: int = 20, page_size: int = 100) -> dict:
    """[只读] 任务状态分布（补一条评价后**回读用这个**，看有多少真到 330 生效）。"""
    buckets, sample_fail, scanned = {}, [], 0
    total = 0
    for p in range(1, max(1, scan_pages) + 1):
        d = task_list(p, page_size, sku_ids=sku_ids, input_erp=input_erp,
                      start_time=start_time, end_time=end_time)
        total = d["total"]
        for t in d["tasks"]:
            scanned += 1
            key = f"{t['status']} {t['status_cn']}"
            buckets[key] = buckets.get(key, 0) + 1
            if t["status"] in STATUS_FAILED and len(sample_fail) < 10:
                sample_fail.append({"sku_id": t["sku_id"], "status_cn": t["status_cn"],
                                    "remark": t["remark"]})
        if p * page_size >= total or not d["tasks"]:
            break
    eff = sum(v for k, v in buckets.items() if k.startswith(f"{STATUS_EFFECTIVE} "))
    return {"total": total, "scanned": scanned, "by_status": buckets,
            "effective": eff, "effective_rate": round(eff / scanned, 4) if scanned else None,
            "failed_samples": sample_fail,
            "note": "生效只认 330；scanned<total 说明只扫了前 scan_pages 页。"}


# --------------------------------------------------------------------------- #
# 只读（消耗配额）：平台 AI 生成文案
# --------------------------------------------------------------------------- #
@audited("pic", "ai_generate")
def ai_generate(sku_id, images: list) -> dict:
    """[平台AI] 给**实拍图**生成评价文案（同品采集不到文案时的兜底）。

    消耗 `user_info().ai_quota`（实测账号 500）。不落库、不提交，只回文案。
    ⚠️契约来自 bundle，**未活体验证**；前端另有一条 craftx 代理直连内网的路子（见 NOTES）。"""
    imgs = [str(u).strip() for u in (images or []) if str(u).strip()]
    if not imgs:
        raise BlacklightError("没有实拍图就没法生成文案（平台是按图生文）")
    d = _call(FID_AI_EVAL, {"skuId": _sku(sku_id), "images": ",".join(imgs)}) or {}
    text = d.get("evalContent") or d.get("comment") or d.get("aigcComment") or (d if isinstance(d, str) else "")
    return {"sku_id": _sku(sku_id), "eval_content": text, "raw": d}


# --------------------------------------------------------------------------- #
# 写：批量提交带图评价
# --------------------------------------------------------------------------- #
def _norm_row(r: dict) -> dict:
    sku = _sku(r.get("sku_id") or r.get("skuId") or r.get("SKUID"))
    text = str(r.get("eval_content") or r.get("evalContent") or r.get("评价文本") or "").strip()
    imgs = r.get("images")
    if isinstance(imgs, str):
        imgs = [x for x in imgs.split(",") if x.strip()]
    imgs = [str(u).strip() for u in (imgs or []) if str(u).strip()]
    return {"sku_id": sku, "eval_content": text, "images": imgs,
            "sku_name": r.get("sku_name") or r.get("商品名称") or "",
            "source_type": r.get("source_type") or r.get("sourceType") or ""}


def _row_problem(row: dict, screen_text: bool = True) -> Optional[str]:
    """格式/空值/文本三道。`screen_text=False` 只做平台硬约束，不做贬损判断。"""
    if not row["images"] and not row["eval_content"]:
        return "图文全为空"
    if not row["images"]:
        return "无实拍图（带图评价必须有图）"
    if len(row["images"]) > MAX_IMAGES_PER_EVAL:
        return f"图片超过 {MAX_IMAGES_PER_EVAL} 张"
    for u in row["images"]:
        if u.startswith("data:image"):
            return "图片是 base64，平台只收 URL"
        if not _re.match(r"^(https?:)?//", u):
            return f"图片不是合法 URL：{u[:40]}"
    if not row["eval_content"]:
        return "无评价文本（有图无文：先用 ai_generate 补文案，或 require_text=False 另走）"
    if len(row["eval_content"]) > MAX_EVAL_CHARS:
        return f"评价文本 {len(row['eval_content'])} 字，超过 {MAX_EVAL_CHARS}"
    if row["eval_content"].startswith("data:image"):
        return "评价文本被识别为图片数据"
    if screen_text:
        from blacklight.pic import screen as _screen
        screened = _screen.screen_rows([row])
        if screened["dropped"]:
            return "图文体检不通过[drop]：" + str(screened["dropped"][0].get("reason") or "")
        if screened["review"]:
            warnings = screened["review"][0].get("warnings") or []
            return "图文体检不通过[review]：" + "；".join(warnings)
    return None


# --------------------------------------------------------------------------- #
# 已用图索引 —— 提交前拦掉"必然被拒"的图
# --------------------------------------------------------------------------- #
# ★为什么值得做：4832 条机审拒绝里抽 500 条 / 1721 张图，拒因分布是
#     「已经被使用过」50.9% + 「上传的图片有重复」8.3% = **59.2% 是图片撞车**。
#   这类拒审在提交前就能判掉——平台在任务回读里把 `imageCrc` 和图片 URL 都给了。
# ⚠️`imageCrc` 是**平台算的**，我们对一张新图算不出同样的值，
#   所以指纹只能用来查"这张图历史上用过没有"（URL/crc 命中即拒），
#   **不能**用来判断两张不同 URL 的图内容是否雷同。别把它当感知哈希用。
_USED_CACHE = {"urls": None, "crcs": None, "scanned": 0, "total": None}


def _img_key(u) -> str:
    """图片比对键：**只取 jfs 路径，丢掉 CDN 主机**。

    ★京东同一张图会在 img10/11/13/14/30 之间轮换主机（同一次会话里连续两次调用
    `targets` 拿到的主图 URL 主机名就不一样）。按完整 URL 比对必然漏判——
    2026-08-18 实撞：商品主图这条闸因此没拦住。
    """
    s = str(u or "").split("?")[0]
    i = s.find("/jfs/")
    if i >= 0:
        return s[i + 1:]
    return _re.sub(r"^https?://[^/]+/", "", s)


def _used_index_path() -> str:
    return os.path.join(_paths.exports_dir("pic_used_images"), "used_index.json")


def used_images(refresh: bool = False, max_pages: int = 200, page_size: int = 100) -> dict:
    """建立/加载「历史已用图」索引（URL + imageCrc）。

    默认读缓存；`refresh=True` 重新扫任务历史。任务量大（实测 3.7 万条），
    所以**默认只扫最近 max_pages 页**并如实回报覆盖率——覆盖不全时拦截会漏，别当成绝对保证。
    """
    path = _used_index_path()
    if not refresh and _USED_CACHE["urls"] is None and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                d = _json.load(f)
            _USED_CACHE.update({"urls": set(d.get("urls") or []), "crcs": set(d.get("crcs") or []),
                                "scanned": d.get("scanned") or 0, "total": d.get("total")})
        except Exception:                               # noqa: BLE001
            pass
    desired = min(int(_USED_CACHE.get("total") or 0), max_pages * page_size)
    if _USED_CACHE["urls"] is not None and int(_USED_CACHE.get("scanned") or 0) < desired:
        refresh = True
    if refresh or _USED_CACHE["urls"] is None:
        urls, crcs, scanned, total = set(), set(), 0, None
        stopped = None
        for pg in range(1, max_pages + 1):
            # ⚠️深翻时平台会偶发「系统异常」。**不能让一页失败毁掉整次扫描**——
            #   重试两次仍失败就带着已收集的部分收工，如实回报覆盖率。
            d, err = None, None
            for attempt in range(3):
                try:
                    d = task_list(page=pg, page_size=page_size)
                    break
                except Exception as e:                  # noqa: BLE001
                    err = str(e)[:120]
                    _time.sleep(1.5 * (attempt + 1))
            if d is None:
                stopped = f"第 {pg} 页连续失败：{err}"
                break
            total = d.get("total") if total is None else total
            ts = d.get("tasks") or []
            if not ts:
                stopped = f"第 {pg} 页空页"
                break
            scanned += len(ts)
            for t in ts:
                for im in (t.get("image_details") or []):
                    if im.get("url"):
                        urls.add(_img_key(im["url"]))
                    if im.get("crc"):
                        crcs.add(str(im["crc"]))
            if len(ts) < page_size:
                break
        _USED_CACHE.update({"urls": urls, "crcs": crcs, "scanned": scanned,
                            "total": total, "stopped": stopped})
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            _json.dump({"urls": sorted(urls), "crcs": sorted(crcs),
                        "scanned": scanned, "total": total}, f)
    cov = (_USED_CACHE["scanned"] / _USED_CACHE["total"]) if _USED_CACHE.get("total") else None
    return {"urls": len(_USED_CACHE["urls"] or ()), "crcs": len(_USED_CACHE["crcs"] or ()),
            "scanned": _USED_CACHE["scanned"], "total": _USED_CACHE["total"],
            "coverage": round(cov, 3) if cov else None,
            "stopped": _USED_CACHE.get("stopped"),
            "note": "只覆盖已扫到的任务；覆盖不全时拦截会漏，不是绝对保证" if (cov or 0) < 1 else None}


def _used_problem(row: dict, main_images: dict = None) -> Optional[str]:
    """图片来源那道闸：历史用过的图 / 商品自己的主图，都必然撞「已经被使用过」。"""
    used = _USED_CACHE.get("urls") or set()
    for u in row["images"]:
        if _img_key(u) in used:
            return f"该图历史上已用于带图评价（必撞「已经被使用过」）：...{u[-32:]}"
        if main_images and _img_key(u) in (main_images.get(row["sku_id"]) or set()):
            return f"这是该 SKU 自己的商品主图，不能当买家实拍图：...{u[-32:]}"
    return None


def _main_images_of(sku_ids) -> dict:
    """取这批 SKU 的商品主图（待补清单里就带 `sku_image`），用于拦"拿主图当买家图"。"""
    out = {}
    ids = [str(x) for x in (sku_ids or [])]
    if not ids:
        return out
    try:
        d = targets(limit=len(ids) * 2, sku_ids=ids)
        for it in (d.get("items") or []):
            u = it.get("sku_image")
            if u:
                out.setdefault(str(it.get("sku_id")), set()).add(_img_key(u))
    except Exception:                                   # noqa: BLE001
        pass                                            # 取不到就不拦这一项，别因此挡住整批
    return out


def _quota_map(sku_ids: list) -> tuple:
    """查这批 SKU 已建了几条 / 上限几条。返回 ({sku: used}, max_per_sku)。"""
    used, max_per = {}, DEFAULT_MAX_TASKS_PER_SKU
    ids = list(dict.fromkeys(sku_ids))
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        d = targets_page(1, max(len(chunk), 10), sku_ids=chunk, image_filter=IMAGE_FILTER_ALL)
        max_per = d["max_per_sku"] or max_per
        for it in d["items"]:
            used[it["sku_id"]] = it["used"]
    return used, max_per


def plan_import(rows: list, check_quota: bool = True, screen_text: bool = True,
                check_used: bool = True) -> dict:
    """[只读] 提交前体检：**空值剔除 + 贬损文本筛查** + 格式校验 + **每 SKU 剩余条数配额** + 序号排布。

    这是最后一道闸，不管行是采集来的、AI 生的还是人工填的都要过（`screen_text=False`
    可只留平台硬约束，但那样贬损评论就只能靠人工把关了）。
    返回 {ok[], rejected[], quota, max_per_sku}。ok 里每条带 `sku_task_index`
    （平台的第几条评价槽位，从该 SKU 已建条数往后排）。"""
    norm, rejected, batch_image_keys = [], [], set()
    for i, r in enumerate(rows or []):
        try:
            row = _norm_row(r)
        except BlacklightError as e:
            rejected.append({"index": i, "reason": str(e), "raw": r})
            continue
        p = _row_problem(row, screen_text=screen_text)
        if p:
            rejected.append({"index": i, "sku_id": row["sku_id"], "reason": p})
            continue
        row_image_keys = [_img_key(u) for u in row["images"]]
        if len(row_image_keys) != len(set(row_image_keys)):
            rejected.append({"index": i, "sku_id": row["sku_id"],
                             "reason": "同一评价内图片重复"})
            continue
        duplicate_keys = batch_image_keys.intersection(row_image_keys)
        if duplicate_keys:
            rejected.append({"index": i, "sku_id": row["sku_id"],
                             "reason": "图片与本批其他评价重复"})
            continue
        batch_image_keys.update(row_image_keys)
        norm.append(row)

    # ★图片来源闸：历史用过的图 / 商品自己的主图 —— 这两类占机审拒绝的 59%，提交前就能判掉
    used_stat = None
    if norm and check_used:
        used_stat = used_images()
        mains = _main_images_of({r["sku_id"] for r in norm})
        keep = []
        for row in norm:
            p2 = _used_problem(row, mains)
            if p2:
                rejected.append({"index": None, "sku_id": row["sku_id"], "reason": p2})
            else:
                keep.append(row)
        norm = keep

    used, max_per = ({}, DEFAULT_MAX_TASKS_PER_SKU)
    if norm and check_quota:
        used, max_per = _quota_map([r["sku_id"] for r in norm])

    ok, cursor = [], {}
    for row in norm:
        base = used.get(row["sku_id"], 0)
        idx = base + cursor.get(row["sku_id"], 0)
        if idx >= max_per:
            rejected.append({"sku_id": row["sku_id"],
                             "reason": f"已达每 SKU 上限 {max_per} 条（已建 {base}）"})
            continue
        cursor[row["sku_id"]] = cursor.get(row["sku_id"], 0) + 1
        ok.append({**row, "sku_task_index": idx})
    return {"ok": ok, "rejected": rejected, "max_per_sku": max_per,
            "quota_used": used, "count_ok": len(ok), "count_rejected": len(rejected),
            "used_index": used_stat}


def _payload(row: dict) -> dict:
    return {"skuId": row["sku_id"], "evalContent": row["eval_content"],
            "images": ",".join(row["images"]), "isCheck": False,
            "skuTaskIndex": int(row.get("sku_task_index") or 0)}


def import_rows_dryrun(rows: list, check_quota: bool = True, screen_text: bool = True,
                       check_used: bool = True) -> dict:
    """[dry-run] 组装但**不提交**：回显逐条 payload + confirm_token。"""
    plan = plan_import(rows, check_quota=check_quota, screen_text=screen_text)
    if not plan["ok"]:
        return {"would_import": False, "reason": "没有可提交的行", **plan}
    if len(plan["ok"]) > MAX_IMPORT_BATCH:
        return {"would_import": False,
                "reason": f"单次最多 {MAX_IMPORT_BATCH} 条，本次 {len(plan['ok'])} 条，请分批", **plan}
    payloads = [_payload(r) for r in plan["ok"]]
    return {"would_import": False, "count": len(payloads), **plan,
            "payload_sample": payloads[:3],
            "note": "DRY-RUN：未提交。真执行：相同 rows + confirm=confirm_token 调 import_rows。"
                    "★提交≠生效，回读用 task_list 看 taskStatus 是否到 330。",
            "confirm_token": _GATE.body_token(payloads)}


@audited("pic", "import_eval")
def import_rows(rows: list, confirm: str = "", check_quota: bool = True,
                screen_text: bool = True) -> dict:
    """[写] **真提交带图评价**（importImageEval，逐条串行）。

    需相同 rows 先 `import_rows_dryrun` 拿 confirm_token。**串行**不并发 —— 平台侧
    同一 SKU 的多条评价靠 skuTaskIndex 排位，并发提交容易互相顶掉（与百补并发报名同类坑）。
    回执逐条给 ok/error；成功只代表**受理**，生效要 `task_list` 回读到 330。"""
    plan = plan_import(rows, check_quota=check_quota, screen_text=screen_text)
    if not plan["ok"]:
        return {"executed": False, "reason": "没有可提交的行", **plan}
    payloads = [_payload(r) for r in plan["ok"]]
    if len(payloads) > MAX_IMPORT_BATCH:
        return {"executed": False, "reason": f"单次最多 {MAX_IMPORT_BATCH} 条", **plan}
    token = _GATE.body_token(payloads)
    if confirm != token:
        return {"executed": False,
                "reason": "提交需二次确认：相同 rows 先跑 import_rows_dryrun 拿 confirm_token 再带 confirm。"}

    results, ok_n = [], 0
    for row, body in zip(plan["ok"], payloads):
        rec = {"sku_id": row["sku_id"], "sku_task_index": body["skuTaskIndex"]}
        try:
            rec["response"] = _call(FID_IMPORT, body)
            rec["ok"] = True
            ok_n += 1
        except BlacklightError as e:
            rec["ok"] = False
            rec["error"] = str(e)
        results.append(rec)
    return {"executed": True, "confirm_token": token, "count": len(payloads),
            "success_count": ok_n, "failed_count": len(payloads) - ok_n,
            "results": results, "rejected": plan["rejected"],
            "note": "受理≠生效：隔几分钟用 task_list(sku_ids=...) 看 taskStatus 是否 330。"}
