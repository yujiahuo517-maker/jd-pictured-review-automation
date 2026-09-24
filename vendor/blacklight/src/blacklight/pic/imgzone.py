"""blacklight.pic.imgzone —— 京东**图片空间**（`imgzone.shop.jd.com`，采销空间）。

带图评价只收图片 URL、不收 base64，而 llm-gw 生图只回 base64 —— 中间必须过图床。
**增删查全在 `sff.jd.com` 一个网关上，cookie-only 打通**（和 osw_product 同网关，h5st 非强制），
`appId=YYGSNPYN2EN5LVUEWU4Y` + 头 `dsm-platform: erp`。

★★**上传别去 `upload.shop.jd.com`**（2026-08-17 踩了两轮）：那个域确实存在、页面 JS 里也确实写着，
但它认 `passport.shop.jd.com`（商家后台 POP 登录态），ERP 主票过去一律 `code=1001 NotLogin` 且换不到。
**而页面上传按钮实际打的根本不是它**，是 `dsm.media.image.imageApiService.uploadImage`——
JSON + base64，走的就是我们已经通的这个网关。抓包时按 Network 里的**真实请求**判断，
别信 JS 里躺着的常量。

⚠️**imageIds 是逗号分隔字符串不是数组**（传数组报 456「无效的字符串」，报错文案完全不提这事）。

图片 URL 拼法：`https://img10.360buyimg.com/imgzone/` + 接口返回的 `imgUrl`（相对 jfs 路径）。
img10/11/13/14/30 都通。⚠️**HEAD 会 403，用 GET 验活**（别把 403 当成图片不存在）。
"""
from __future__ import annotations

import base64
import json as _json

from blacklight.core import BlacklightError, auth as jd_auth, make_client, audited

SFF_API = "https://sff.jd.com/api"
APP_ID = "YYGSNPYN2EN5LVUEWU4Y"
ORIGIN = "https://imgzone.shop.jd.com"
CDN = "https://img10.360buyimg.com/imgzone/"

# 排序值只收这四个字符串，传数字会报 456「无效的字符串」
ORDERS = ("createDate_desc", "createDate_asc", "imgName_cateName_asc", "imgName_cateName_desc")


def _call(api: str, body: dict, terminal: bool = False) -> dict:
    ck = jd_auth.get_cookie()
    if not ck:
        raise BlacklightError("无登录态：先 osw_login / yx_login")
    ctx = {"source": "web", "businessModel": "self"}
    if terminal:                                        # 上传接口的 accessContext 多带这个
        ctx["terminal"] = 0
    payload = {"accessContext": ctx, **(body or {})}
    c = make_client(ck, origin=ORIGIN, referer=ORIGIN + "/",
                    content_type="application/json;charset=UTF-8", timeout=30.0)
    with c:
        r = c.post(SFF_API, params={"v": "1.0", "appId": APP_ID, "api": api},
                   content=_json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   headers={"dsm-platform": "erp"})
    try:
        j = r.json()
    except Exception as e:                              # noqa: BLE001
        raise BlacklightError(f"{api} 未返回 JSON（HTTP {r.status_code}）——登录态可能失效") from e
    if j.get("code") not in (200, "200"):
        raise BlacklightError(f"{api} code={j.get('code')}: {j.get('msg')}")
    return j.get("data") or {}


def full_url(img_url: str) -> str:
    """相对 jfs 路径 → 完整 CDN URL（已是完整 URL 则原样返回）。"""
    s = str(img_url or "").strip()
    if not s:
        return ""
    if s.startswith("http://") or s.startswith("https://") or s.startswith("//"):
        return s
    return CDN + s.lstrip("/")


def _norm(x: dict) -> dict:
    return {"img_id": x.get("imgId") or x.get("id"), "name": x.get("imgName") or "",
            "url": full_url(x.get("imgUrl")), "cate_id": str(x.get("cateId") or ""),
            "width": x.get("imgWidth"), "height": x.get("imgHeight"),
            "size": x.get("imgSize"), "type": x.get("imgType"),
            "create_date": x.get("createDate"), "used": x.get("useFlag")}


def list_images(cate_id=0, page: int = 1, page_size: int = 50, only_image: bool = True,
                order: str = "createDate_desc") -> dict:
    """[只读] 列某分类下的图片 + 子分类。`cate_id=0` 是根。

    ⚠️`order` 只收 ORDERS 里那四个字符串，传数字报 456「无效的字符串」。"""
    if order not in ORDERS:
        raise BlacklightError(f"order 只能是 {ORDERS} 之一")
    d = _call("dsm.media.image.imageApiService.queryImageAndCate",
              {"imageQueryVo": {"cateId": cate_id, "page": int(page), "pageSize": int(page_size),
                                "onlyImage": bool(only_image), "orderByDate": order}})
    return {"page": d.get("currPage"), "page_total": d.get("pageTotal"),
            "total": d.get("imgDirTotal"),
            "dirs": [{"cate_id": str(x.get("cateId")), "name": x.get("cateName")}
                     for x in (d.get("dirList") or [])],
            "images": [_norm(x) for x in (d.get("imgList") or [])]}


def categories() -> list:
    """[只读] 根下的分类清单 [{cate_id, name}]。

    ⚠️分类和图片共用同一个分页，page_size 太小会把分类也截断（别用 page_size=1 去"只拿分类"）。"""
    return list_images(0, 1, 200, only_image=False)["dirs"]


def find_by_name(name: str, cate_id=0, max_pages: int = 5, page_size: int = 50) -> list:
    """[只读] 按文件名（子串）找图，返回带完整 URL 的行。**上传后读回 URL 就用这个**。"""
    key, out = str(name or "").strip(), []
    if not key:
        raise BlacklightError("name 不能为空")
    for p in range(1, max_pages + 1):
        d = list_images(cate_id, p, page_size)
        out.extend(x for x in d["images"] if key in x["name"])
        if d.get("page_total") and p >= d["page_total"]:
            break
    return out


def check_alive(url: str, timeout: float = 20.0) -> dict:
    """[只读] 验图片能不能取到。⚠️**必须 GET**：图床对 HEAD 回 403，用 HEAD 会误判成图挂了。"""
    import httpx
    with httpx.Client(timeout=timeout, verify=False, follow_redirects=True) as c:
        r = c.get(full_url(url), headers={"User-Agent": "Mozilla/5.0"})
    return {"url": full_url(url), "status": r.status_code,
            "content_type": r.headers.get("content-type"), "bytes": len(r.content),
            "alive": r.status_code == 200 and str(r.headers.get("content-type", "")).startswith("image")}


@audited("pic", "imgzone_upload")
def upload(source, file_name: str = None, cate_id="0") -> dict:
    """[写] 传图进图片空间 → 直接返回可用的完整 CDN URL。

    `source` 收 本地路径 / bytes / base64 字符串 / dataURL —— **llm-gw 的 b64 可以直接进来，不必落盘**。
    限制 jpg/png/jpeg/webp ≤20M（gif ≤3M）。⚠️上传后 CDN 有几秒缓存延迟，验活用 check_alive（GET）。"""
    import os
    import re

    if isinstance(source, bytes):
        raw_b64 = base64.b64encode(source).decode()
        name = file_name or "upload.png"
    else:
        s = str(source)
        if s.startswith("data:"):                        # dataURL → 去掉前缀
            raw_b64 = s.split(",", 1)[1]
            name = file_name or "upload.png"
        elif os.path.exists(s):
            with open(s, "rb") as f:
                raw_b64 = base64.b64encode(f.read()).decode()
            name = file_name or os.path.basename(s)
        elif re.fullmatch(r"[A-Za-z0-9+/=\s]{64,}", s):  # 裸 base64
            raw_b64 = re.sub(r"\s", "", s)
            name = file_name or "upload.png"
        else:
            raise BlacklightError(f"source 不是路径/bytes/base64：{s[:60]}")

    d = _call("dsm.media.image.imageApiService.uploadImage",
              {"cateId": str(cate_id), "fileData": raw_b64, "fileName": name},
              terminal=True)
    out = _norm(d)
    out["cate_id"] = str(cate_id)
    return out


@audited("pic", "imgzone_delete")
def delete(image_ids, parent_cate_id="0") -> dict:
    """[写] 删图。**不可恢复**，且平台警告「已被商品引用的图删了会导致商品展示异常」。

    ⚠️`imageIds` 服务端要的是**逗号分隔字符串**，传数组一律 456「无效的字符串」——
    报错文案完全没提这点，别照着 JS 里的数组写法抄。"""
    ids = image_ids if isinstance(image_ids, str) else ",".join(str(x) for x in image_ids)
    if not ids.strip():
        raise BlacklightError("image_ids 不能为空")
    _call("dsm.media.image.imageApiService.batchDelete",
          {"imageIds": ids, "categoryIds": "", "parentCateId": int(parent_cate_id), "operState": 1})
    return {"deleted": ids.split(","), "note": "缓存延迟约 10s，立刻回读可能还看得见"}
