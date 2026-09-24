"""
yx-mcp campaign 场域客户端（mcpman.jd.com）：官方直降/招商/券类/满减的查/报名/退出。

契约见 NOTES_endpoints.md（block-and-capture 实证）。请求走 POST + JSON，鉴权用 jd_auth 的 cookie。
写操作(apply/withdraw/table_withdraw)均已开放，走 confirm_token 二次确认门；all_quit 仍只 dry-run。
报名 formSet 驱动自适配任意玩法；批量退出另有「上传表格退出」(table_withdraw，绕开已报名浏览上限1万)。
"""
from __future__ import annotations

import json as _json
from typing import Any, Optional

from blacklight.core import auth as jd_auth
# 公共核心（错误类型/裸客户端/令牌）来自 jd_core；此处 re-export 兼容依赖 yx_client 的旧 import。
from blacklight.core import (BlacklightError, bare_client as _client, confirm_token as _confirm_token,  # noqa: F401
                     post_multipart, audited, gateway, scene_cfg,
                     pace, retry_throttled, THROTTLE_RULES as _POLICY_RULES)

MCPMAN = gateway("campaign")
RESOURCE_TYPE = scene_cfg("campaign").get("resource_type", 1000002)  # resourceParam.resourceType


def _post(path: str, payload: dict, cookie: Optional[str] = None) -> dict:
    """POST 一个只读/无副作用接口，返回解析后的信封。302/非JSON 视为登录失效。"""
    cookie = cookie or jd_auth.ensure_session()
    url = f"{MCPMAN}{path}"
    with _client() as c:
        r = c.post(url, json=payload, headers=jd_auth._headers(cookie))
    if r.status_code in (301, 302, 303, 307, 308):
        raise BlacklightError(f"{path} 被重定向（{r.status_code}）——登录态可能失效，请 jd_auth --login")
    try:
        j = r.json()
    except Exception as e:
        raise BlacklightError(f"{path} 未返回 JSON（HTTP {r.status_code}）——可能是登录页") from e
    if isinstance(j, dict) and j.get("success") is False:
        raise BlacklightError(f"{path} 业务失败: code={j.get('code')} msg={j.get('message') or j.get('msg')}")
    return j


def _post_send(path: str, payload: dict, cookie: Optional[str] = None) -> dict:
    """真正发送一个写请求，返回解析后的信封（**不**在 success=False 时抛错，让调用方看到业务码/消息）。"""
    cookie = cookie or jd_auth.ensure_session()
    with _client() as c:
        r = c.post(f"{MCPMAN}{path}", json=payload, headers=jd_auth._headers(cookie))
    if r.status_code in (301, 302, 303, 307, 308):
        raise BlacklightError(f"{path} 被重定向（{r.status_code}）——登录态可能失效，请 jd_auth --login")
    try:
        return r.json()
    except Exception as e:
        raise BlacklightError(f"{path} 未返回 JSON（HTTP {r.status_code}）") from e


def _resource_param(campaign_id: str) -> dict:
    return {
        "resourceId": str(campaign_id),
        "resourceType": RESOURCE_TYPE,
        "campaignId": str(campaign_id),
    }


# --------------------------------------------------------------------------- #
# 只读
# --------------------------------------------------------------------------- #
def get_activity_detail(campaign_id: str | int) -> dict:
    """活动详情：POST /campaign/getCampaignApplyDetailById {campaignId}。返回 data 对象。"""
    j = _post("/campaign/getCampaignApplyDetailById", {"campaignId": str(campaign_id)})
    return j.get("data", j)


def get_applied_page(campaign_id: str | int, page: int = 1, page_size: int = 10,
                     condition: Optional[dict] = None, material_type: int = 1) -> dict:
    """
    已报名列表（tab=已报名管理）：POST /apply/applied/page。
    condition 额外筛选并入 {campaignId,...}（如 skuId/报名状态等）。返回 data（含分页与行）。
    """
    cond = {"campaignId": str(campaign_id)}
    if condition:
        cond.update(condition)
    payload = {
        "page": page,
        "pageSize": page_size,
        "condition": cond,
        "materialType": material_type,
        "resourceParam": _resource_param(campaign_id),
    }
    j = _post("/apply/applied/page", payload)
    return j.get("data", j)


# --------------------------------------------------------------------------- #
# 功能一：SKU 状态查询（只读）
# --------------------------------------------------------------------------- #
QUIT_OP = 1  # operateList 中 value==1 = 退出操作（实证）
_PROCESS_STATUS = {0: "报名中", 6: "报名完成"}


def _quit_eligibility(operate_list) -> tuple[bool, Optional[str]]:
    """从 operateList 判断能否退出：存在 value==QUIT_OP 且无 reason -> 可退。"""
    for op in (operate_list or []):
        if isinstance(op, dict) and op.get("value") == QUIT_OP:
            reason = op.get("reason")
            return (reason is None, reason)
    return (False, "无退出操作项")


def _classify_row(r: dict) -> dict:
    sku = (r.get("skuDTO") or {})
    can_quit, quit_reason = _quit_eligibility(r.get("operateList"))
    ps = r.get("applyProcessStatus")
    return {
        "status": _PROCESS_STATUS.get(ps, f"process:{ps}"),
        "applyId": r.get("id"),                     # 报名ID（退出用 quitList[].id）
        "materialId": str(r.get("materialId") or sku.get("skuId") or ""),  # 退出用 quitList[].materialId
        "materialType": r.get("materialType"),      # 退出用 quitList[].materialType
        "applyStatus": r.get("applyStatus"),
        "applyProcessStatus": ps,
        "canQuit": can_quit,
        "quitBlockReason": quit_reason,
        "productName": sku.get("productName"),
        "spuId": sku.get("spuId"),
        "categoryNames": sku.get("categoryNames"),
    }


def get_sku_status(campaign_id: str | int, sku_ids: list[str | int]) -> dict:
    """
    查询一批 SKU 在该活动中的状态（只读）。
    命中已报名列表 -> 归一化状态 + applyId + 可否退出(+原因)；未命中 -> 标记"未报名/不在已报名列表"。
    逐 SKU 查询：`condition.skuId` 的逗号批量里**只要有一个未参与的 SKU 就会把整批清零**（实证），
    而状态查询天然会带未报名 SKU，故按单 SKU 查以保证正确。返回 {campaignId: {skuId: {...}}}。
    """
    sku_ids = [str(s).strip() for s in sku_ids if str(s).strip()]
    found: dict[str, list] = {}
    for sid in dict.fromkeys(sku_ids):  # 去重保序
        d = get_applied_page(campaign_id, page=1, page_size=100,
                             condition={"skuId": sid})
        for r in (d.get("items") or []):
            rsid = str((r.get("skuDTO") or {}).get("skuId") or "")
            if rsid == sid:
                found.setdefault(sid, []).append(_classify_row(r))
    out = {}
    for sid in sku_ids:
        rows = found.get(sid)
        if rows:
            # 一个 SKU 可能有多条报名记录（多物料），全部返回；顶层给一个汇总能否退出
            out[sid] = {"applied": True, "records": rows,
                        "anyQuitable": any(x["canQuit"] for x in rows)}
        else:
            out[sid] = {"applied": False, "status": "未报名/不在已报名列表",
                        "note": "可报/不可报细分需 canApply 探针（后续）"}
    return {str(campaign_id): out}


# --------------------------------------------------------------------------- #
# dry-run 公共信封（组装但不发送）
# --------------------------------------------------------------------------- #
def _dryrun_envelope(path: str, body: dict, note: str, extra: Optional[dict] = None) -> dict:
    env = {
        "would_send": False,
        "note": note,
        "request": {
            "method": "POST",
            "url": f"{MCPMAN}{path}",
            "headers": {"Content-Type": "application/json",
                        "Accept": "application/json, text/plain, */*",
                        "Cookie": "<yx cookie（pin/pt_key/sdtoken…，运行时注入）>"},
            "body": body,
        },
    }
    if extra:
        env.update(extra)
    return env


# --------------------------------------------------------------------------- #
# 功能三：withdraw（撤回报名/退出活动）——只 dry-run
# --------------------------------------------------------------------------- #
def _resolve_quittable(campaign_id, sku_ids) -> tuple[list, list]:
    """把 sku_ids 解析成可退的 quitList 项(含 id/materialId/materialType) + 被跳过明细（含原因）。"""
    status = get_sku_status(campaign_id, sku_ids)[str(campaign_id)]
    eligible, skipped = [], []
    for sid, info in status.items():
        if not info.get("applied"):
            skipped.append({"skuId": sid, "reason": "未报名/不在已报名列表"})
            continue
        for rec in info.get("records", []):
            if rec.get("canQuit"):
                eligible.append({"id": rec["applyId"],
                                 "materialId": rec.get("materialId") or sid,
                                 "materialType": rec.get("materialType") if rec.get("materialType") is not None else 1,
                                 "skuId": sid})
            else:
                skipped.append({"skuId": sid, "applyId": rec.get("applyId"),
                                "reason": rec.get("quitBlockReason") or "operateList 判定不可退"})
    return eligible, skipped


MAX_QUIT_BATCH = 50  # 单次真执行退出 SKU 数上限（护栏）


def _build_quit_body(campaign_id, apply_ids=None, sku_ids=None) -> tuple[Optional[dict], dict]:
    """
    组装 batchQuit body（dry-run 与真发共用，保证 confirm_token 一致）。
    返回 (body | None, extra)。sku_ids 无可退行时 body=None。
    """
    extra: dict = {}
    if sku_ids:
        eligible, skipped = _resolve_quittable(campaign_id, sku_ids)
        extra = {"eligibility": {"quittable": eligible, "skipped": skipped}}
        quit_list = [{"id": e["id"], "materialId": str(e["materialId"]),
                      "materialType": e["materialType"]} for e in eligible]
        if not quit_list:
            return None, extra
    elif apply_ids:
        quit_list = []
        for aid in apply_ids:  # 缺 materialId：反查 applied 列表按报名ID补全
            d = get_applied_page(campaign_id, page=1, page_size=10, condition={"applyId": str(aid)})
            row = next((r for r in (d.get("items") or []) if str(r.get("id")) == str(aid)), None)
            if not row:
                raise BlacklightError(f"报名ID {aid} 未在 applied 列表找到，无法补全 materialId；建议改用 sku_ids。")
            quit_list.append({"id": int(aid),
                              "materialId": str(row.get("materialId") or (row.get("skuDTO") or {}).get("skuId")),
                              "materialType": row.get("materialType") if row.get("materialType") is not None else 1})
    else:
        raise BlacklightError("batchQuit 需要 sku_ids 或 apply_ids；整场退出请用 all_quit=True")
    body = {"resourceParam": _resource_param(campaign_id),
            "quitList": quit_list, "operateRequestSource": 2}
    return body, extra


def withdraw_dryrun(campaign_id: str | int, apply_ids: Optional[list] = None,
                    sku_ids: Optional[list] = None, all_quit: bool = False) -> dict:
    """
    组装 withdraw(batchQuit) 请求但**绝不发送**。batchQuit 结构为 block-and-capture 实证真值。
      - sku_ids:   先 sku→报名行 并**校验 operateList 可退**，只把可退行组进 quitList，跳过项带原因（推荐）
      - apply_ids: 直接给报名ID；缺 materialId 会自动反查补全
      - all_quit:  整场退出（/apply/all/quit，payload 未实证，仅 dry-run，不支持真执行）
    返回体含 confirm_token：用**相同参数** + confirm=该token 调 yx_withdraw 才会真执行。
    """
    note = ("DRY-RUN：请求未发送。batchQuit 为实证真值。真执行：相同参数 + confirm=confirm_token 调 yx_withdraw。")
    if all_quit:
        body = {"campaignId": str(campaign_id), "all": True,
                "resourceParam": _resource_param(campaign_id)}
        return _dryrun_envelope("/apply/all/quit", body,
                                "DRY-RUN：all_quit payload 未实证，为推断值，且不支持真执行（见 NOTES）。")

    body, extra = _build_quit_body(campaign_id, apply_ids, sku_ids)
    if body is None:
        return {"would_send": False, "note": note, "request": None, **extra,
                "warning": "没有可退的报名行（见 skipped 原因）；未组装 batchQuit 请求。"}
    token = _confirm_token(body)
    extra = {**extra, "confirm_token": token, "quit_count": len(body["quitList"])}
    return _dryrun_envelope("/apply/batchQuit", body, note, extra)


@audited("campaign", "withdraw")
def withdraw(campaign_id: str | int, apply_ids: Optional[list] = None,
             sku_ids: Optional[list] = None, all_quit: bool = False, confirm: str = "") -> dict:
    """
    **真执行**退出(batchQuit)。护栏：
      1) 必须先 withdraw_dryrun 拿 confirm_token，用相同参数 + confirm=该token 才放行。
      2) 单次 SKU 数 ≤ MAX_QUIT_BATCH。
      3) **all_quit 不支持真执行**（payload 未实证 + 整场退出风险极大）。
    这会对活动**真实退出报名**，是不可轻易撤回的写操作。

    ⚠️**生效是异步审核，不是即时**（2026-07-28 实证）：`code=00000 / success=true / data:[]` 只代表
    **申请已受理**——促销仍在跑、毛利零变化。用 `get_sku_status(campaignId,[skuId])` 查真实状态：
    `applyProcessStatus=10` + `canQuit=false` + "该商品退出审核中或已退出" ＝ 已提交待审，**须隔天复查**。
    别因为回读毛利没动就判失败去重复提交（会撞「无需重复申请」）。
    对比：摘券(`markettool.delete`)、退国补(`subsidy.withdraw_batch`) 都是**即时**生效。
    """
    if all_quit:
        raise BlacklightError("all_quit 真执行未开放（payload 未实证 + 整场退出风险极大）。请按行 sku_ids/apply_ids 退出。")
    body, extra = _build_quit_body(campaign_id, apply_ids, sku_ids)
    if body is None:
        raise BlacklightError("没有可退的报名行（operateList 判定不可退或未报名）。")
    token = _confirm_token(body)
    if confirm != token:
        raise BlacklightError(
            "退出真执行需二次确认：先用相同参数跑 withdraw_dryrun 拿 confirm_token，"
            "再带 confirm=该token 调用（当前传入 confirm 与之不符）。")
    n = len(body["quitList"])
    if n > MAX_QUIT_BATCH:
        raise BlacklightError(f"单次退出 SKU 数 {n} 超过上限 {MAX_QUIT_BATCH}，请分批。")
    resp = _post_send("/apply/batchQuit", body)
    ok = bool(isinstance(resp, dict) and resp.get("success") is True)
    return {"executed": True, "confirm_token": token, "request_body": body, "response": resp,
            "success": ok,
            "生效方式": "★异步审核：success=true 只代表申请已受理，促销仍在跑、毛利不会立刻变。",
            "复查方式": f"get_sku_status('{campaign_id}', [skuId]) → applyProcessStatus=10 即已提交待审；隔天再查毛利。",
            "勿": "别因当场毛利没变就重复提交（会撞「该商品退出审核中或已退出，无需重复申请」）。",
            **extra}


# --------------------------------------------------------------------------- #
# 功能一·补：**上传表格退出**（按 SKU 表格批量退出，绕开"已报名浏览上限1万"）
#   ① POST /apply/form/downloadApplyTemplate → 模板URL（uploadType=100退出/templateType=1）
#   ② POST /fileUpload (multipart: campaignId, uploadType, file) → fileKey(data)
#   ③ POST /uploadApplySave (JSON: fileInfos+resourceType+bizId+operateRequestSource:2) → 提交
#   ④ POST /getFileUploadRecord (JSON: campaignId, uploadType, page) → 进度+成功/失败清单直链
# 模板：单 sheet「商品信息」、1 列「商品SKU」。uploadType 100=退出。
# --------------------------------------------------------------------------- #
UPLOAD_TYPE_QUIT = 100
XLSX_MIME_C = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_WITHDRAW_SHEET = "商品信息"
_WITHDRAW_HEADER = "商品SKU"


def download_withdraw_template(campaign_id: str | int) -> str:
    """取上传表格退出的模板下载 URL（POST /apply/form/downloadApplyTemplate, uploadType=100）。只读。"""
    j = _post("/apply/form/downloadApplyTemplate",
              {"resourceParam": {"campaignId": str(campaign_id), "resourceId": str(campaign_id)},
               "uploadType": UPLOAD_TYPE_QUIT, "templateType": 1})
    return j.get("data")


def _build_withdraw_xlsx(sku_ids, out_path: str) -> dict:
    """生成表格退出 xlsx（单 sheet「商品信息」、1 列「商品SKU」）。返回 {path, rows}。"""
    import openpyxl
    ids = [str(s).strip() for s in sku_ids if str(s).strip()]
    if not ids:
        raise BlacklightError("表格退出需要至少一个 skuId")
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = _WITHDRAW_SHEET
    ws.append([_WITHDRAW_HEADER])
    for s in ids:
        ws.append([s])
    wb.save(out_path)
    return {"path": out_path, "rows": len(ids)}


def _upload_file(campaign_id: str | int, xlsx_path: str, cookie: Optional[str] = None) -> dict:
    """上传 xlsx（POST /fileUpload, multipart）→ 返回 {fileKey, fileName, response}。

    ⚠️**文件上传闸是跨频道共享的**：本函数走 mcpman，国补表格上传走 mac，两套不同的活动体系，
      但共用同一个上传节流窗口（约 90~120s 一个文件）。已接 `core.policy` 的 `upload.file` 闸。
    """
    import os
    pace("upload.file")
    cookie = cookie or jd_auth.ensure_session()
    if not os.path.isfile(xlsx_path):
        raise BlacklightError(f"文件不存在: {xlsx_path}")
    fname = os.path.basename(xlsx_path)
    data = open(xlsx_path, "rb").read()
    files = {"file": (fname, data, XLSX_MIME_C)}
    form = {"campaignId": str(campaign_id), "uploadType": str(UPLOAD_TYPE_QUIT)}
    j = post_multipart(MCPMAN, "/fileUpload", form, files, cookie,          # 公共 multipart（jd_core）
                       extra_headers={"X-Requested-With": "XMLHttpRequest"})
    if not j.get("success"):
        raise BlacklightError(f"/fileUpload 失败: {j.get('message') or j.get('code')}")
    return {"fileKey": j.get("data"), "fileName": fname, "response": j}


def _upload_apply_save(campaign_id: str | int, file_key: str, file_name: str) -> dict:
    """提交上传退出（POST /uploadApplySave）→ 提交入队。真写。"""
    body = {"campaignId": str(campaign_id),
            "fileInfos": [{"fileKey": file_key, "fileName": file_name}],
            "uploadType": UPLOAD_TYPE_QUIT, "resourceType": RESOURCE_TYPE,
            "resourceId": str(campaign_id), "bizId": str(campaign_id), "operateRequestSource": 2}
    return _post_send("/uploadApplySave", body)


def upload_record(campaign_id: str | int, page: int = 1, page_size: int = 10) -> dict:
    """查上传表格退出的处理进度（POST /getFileUploadRecord, uploadType=100），倒序。
    返回 items：status(3=完成)/totalCount/successCount/failCount/**successLink(成功清单)+failLink(失败清单)**/uploadTime。只读。"""
    j = _post("/getFileUploadRecord",
              {"campaignId": str(campaign_id), "uploadType": UPLOAD_TYPE_QUIT,
               "page": page, "pageSize": page_size})
    d = j.get("data") or {}
    items = [{"fileName": it.get("fileName"), "status": it.get("status"),
              "totalCount": it.get("totalCount"), "successCount": it.get("successCount"),
              "failCount": it.get("failCount"), "successLink": it.get("successLink") or "",
              "failLink": it.get("failLink") or "", "uploadTime": it.get("uploadTime"),
              "fileKey": it.get("fileKey")}
             for it in (d.get("items") or [])]
    return {"totalNum": d.get("totalNum"), "items": items}


def _table_withdraw_token(campaign_id, sku_ids) -> str:
    return _confirm_token({"path": "/uploadApplySave", "campaignId": str(campaign_id),
                           "uploadType": UPLOAD_TYPE_QUIT, "skus": sorted(str(s) for s in sku_ids)})


def table_withdraw_dryrun(campaign_id: str | int, sku_ids: list) -> dict:
    """**上传表格退出 DRY-RUN**：只回显将退出的 SKU 数 + confirm_token，不生成不上传。
    真执行：相同参数 + confirm 调 table_withdraw。绕开"已报名浏览上限1万"，直接按 SKU 表格退出。"""
    ids = [str(s).strip() for s in sku_ids if str(s).strip()]
    if not ids:
        raise BlacklightError("表格退出需要至少一个 skuId")
    return {"would_withdraw": False, "sku_count": len(ids), "campaignId": str(campaign_id),
            "note": "DRY-RUN：未生成/未上传。真执行：相同 sku_ids + confirm=confirm_token 调 table_withdraw。",
            "confirm_token": _table_withdraw_token(campaign_id, ids)}


def _await_upload(campaign_id, pre_id, timeout: int = 120, interval: int = 6) -> dict:
    """轮询 getFileUploadRecord 顶部**新**记录(id≠pre_id)直到终态(status==3完成)或超时。
    回执自验证：直接拿 success/fail + 成功/失败清单直链，不碰有延迟的看板数据。"""
    import time
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        items = upload_record(campaign_id, page_size=1).get("items") or []
        if items and str(items[0].get("fileKey")) != str(pre_id):
            last = items[0]
            if last.get("status") == 3:          # 3=完成
                return {"done": True, "status": 3, "total": last.get("totalCount"),
                        "success": last.get("successCount"), "fail": last.get("failCount"),
                        "successLink": last.get("successLink"), "failLink": last.get("failLink")}
        time.sleep(interval)
    return {"done": False, "note": f"轮询{timeout}s未完成，稍后 campaign_upload_record 查", "last": last}


@audited("campaign", "table_withdraw")
def table_withdraw(campaign_id: str | int, sku_ids: list, confirm: str = "",
                   xlsx_path: Optional[str] = None, wait: bool = True, wait_timeout: int = 120) -> dict:
    """**上传表格退出真执行**：生成1列xlsx→/fileUpload→/uploadApplySave（真实批量退出）。
    需相同 sku_ids 先 table_withdraw_dryrun 拿 confirm_token 再带 confirm。**wait=True 自动轮询到终态并回执自验证**(返回 result: 成功/失败数+清单直链)。"""
    import os
    ids = [str(s).strip() for s in sku_ids if str(s).strip()]
    if not ids:
        raise BlacklightError("表格退出需要至少一个 skuId")
    if confirm != _table_withdraw_token(campaign_id, ids):
        raise BlacklightError("表格退出真执行需二次确认：先用相同 sku_ids 跑 table_withdraw_dryrun 拿 confirm_token 再带 confirm。")
    pre = upload_record(campaign_id, page_size=1).get("items") or []
    pre_id = pre[0].get("fileKey") if pre else None
    xlsx_path = xlsx_path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          f"_tablequit_{campaign_id}.xlsx")
    built = _build_withdraw_xlsx(ids, xlsx_path)
    up = _upload_file(campaign_id, xlsx_path)
    save = _upload_apply_save(campaign_id, up["fileKey"], up["fileName"])
    out = {"executed": True, "campaignId": str(campaign_id), "rows": built["rows"],
           "fileKey": up["fileKey"], "save_response": save,
           "success": bool(isinstance(save, dict) and save.get("success") is True),
           "confirm_token": _table_withdraw_token(campaign_id, ids)}
    if wait:
        out["result"] = _await_upload(campaign_id, pre_id, timeout=wait_timeout)   # ★回执自验证
    else:
        out["note"] = "已提交，用 campaign_upload_record 轮询处理进度+成功/失败清单直链。"
    return out


# --------------------------------------------------------------------------- #
# 功能一·补二：**上传表格报名**（按 SKU 表格批量报名，比 batchApply 快一个数量级）
#   与「表格退出」同一条链路（downloadApplyTemplate→fileUpload→uploadApplySave→getFileUploadRecord），
#   只有两个坐标不同：**uploadType=2**（退出是 100）、模板 **templateType=2**（退出是 1）。
#
# ★★两个坐标都不能靠试，2026-08-17 实证：
#   ① `downloadApplyTemplate` **对 uploadType 完全不校验** —— 0/1/2/3/10/20/50/100/101/200
#      全部回 200 且回同一份退出模板（单列「商品SKU」）。"没报错"在这里证明不了任何事（铁律 9）。
#      真正分辨模板的是 **templateType**：
#        0=商品id/优惠类型/优惠折扣/直降金额  1=商品SKU(退出)  **2=商品SKU/优惠比例(%)**
#        3=商品SKU/优惠金额立减(元)  4=促销起止时间  5=商品SPU
#   ② 报名侧 uploadType 用**只读反查**定死：`getFileUploadRecord` 逐个 uploadType 查历史，
#      只有 2 和 100 有记录（2 → 2026-06-29 上传 478 行/成功 474；100 → 退出）。
#      别用 uploadApplySave 去试 —— 那是写操作。
#
# ★优惠比例填**整数 1~90**，语义是「降百分之几」：模板备注原文
#   「最终促销优惠为商品前台价（单品促销后价格）× 上传优惠比例」，例：填 20 ⇒ 降 20%。
#   `batchApply` 侧同字段同语义（存量 50/50 条报名记录的 discount 值都是 "5"）。
#   ⚠️别填 0.95 或 95：填 0.95 平台报 `For input string: "0.95"`（Java 整数解析失败），
#     填 95 会变成**直降 95%**。
# ⚠️不填比例 = 取活动默认比例，不是"不打折"。
# --------------------------------------------------------------------------- #
UPLOAD_TYPE_APPLY = 2                 # 表格报名（退出是 100）
TEMPLATE_TYPE_APPLY_RATIO = 2         # 商品SKU + 优惠比例(%)
_APPLY_SHEET = "商品信息"
_APPLY_HEADER_SKU = "商品SKU"


def download_apply_template(campaign_id: str | int,
                            template_type: int = TEMPLATE_TYPE_APPLY_RATIO) -> str:
    """取上传表格**报名**的模板下载 URL。只读。

    `template_type` 见本节注释（2=比例、3=立减金额、0=全字段）。
    ⚠️该接口不校验 uploadType，模板由 template_type 决定。"""
    j = _post("/apply/form/downloadApplyTemplate",
              {"resourceParam": {"campaignId": str(campaign_id), "resourceId": str(campaign_id)},
               "uploadType": UPLOAD_TYPE_APPLY, "templateType": int(template_type)})
    return j.get("data")


def _build_apply_xlsx(rows, out_path: str) -> dict:
    """生成表格报名 xlsx（单 sheet「商品信息」、2 列：商品SKU / 优惠比例）。

    `rows` = [{skuId, ratio}]，ratio 为 1~90 的整数或 None（None ⇒ 留空取活动默认比例）。"""
    import openpyxl
    if not rows:
        raise BlacklightError("表格报名需要至少一行")
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = _APPLY_SHEET
    ws.append([_APPLY_HEADER_SKU, "优惠比例（%）"])
    for r in rows:
        ws.append([str(r["skuId"]), "" if r.get("ratio") is None else int(r["ratio"])])
    wb.save(out_path)
    return {"path": out_path, "rows": len(rows)}


def _norm_apply_rows(sku_ids, ratio) -> list:
    """把 sku_ids(+统一 ratio) 或 [{skuId,ratio}] 规整成统一行结构，并校验比例范围。"""
    rows = []
    for s in sku_ids:
        if isinstance(s, dict):
            sid, rt = s.get("skuId") or s.get("sku"), s.get("ratio", ratio)
        else:
            sid, rt = s, ratio
        sid = str(sid or "").strip()
        if not sid:
            continue
        if rt is not None:
            raw = rt                                   # ★先按原值校验再取整，否则 0.95→0，报错信息会误导
            if isinstance(raw, float) and not float(raw).is_integer():
                raise BlacklightError(
                    f"优惠比例须为 1~90 的**整数**（收到 {raw}）。它是「降百分之几」："
                    f"填 5 = 降 5%。别填 0.95 —— 平台会报 For input string: \"0.95\"。")
            rt = int(raw)
            if not 1 <= rt <= 90:
                raise BlacklightError(
                    f"优惠比例须为 1~90 的整数（收到 {raw}）。它是「降百分之几」："
                    f"填 5 = 降 5%；填 95 会变成**直降 95%**。")
        rows.append({"skuId": sid, "ratio": rt})
    if not rows:
        raise BlacklightError("表格报名需要至少一个 skuId")
    return rows


def apply_upload_record(campaign_id: str | int, page: int = 1, page_size: int = 10) -> dict:
    """查上传表格**报名**的处理进度（uploadType=2），字段同 `upload_record`。只读。"""
    j = _post("/getFileUploadRecord",
              {"campaignId": str(campaign_id), "uploadType": UPLOAD_TYPE_APPLY,
               "page": page, "pageSize": page_size})
    d = j.get("data") or {}
    items = [{"fileName": it.get("fileName"), "status": it.get("status"),
              "totalCount": it.get("totalCount"), "successCount": it.get("successCount"),
              "failCount": it.get("failCount"), "successLink": it.get("successLink") or "",
              "failLink": it.get("failLink") or "", "uploadTime": it.get("uploadTime"),
              "fileKey": it.get("fileKey")}
             for it in (d.get("items") or [])]
    return {"totalNum": d.get("totalNum"), "items": items}


def _await_apply_upload(campaign_id, pre_id, timeout: int = 180, interval: int = 6) -> dict:
    """轮询报名侧上传记录到终态（status==3）。回执自验证，拿成功/失败清单直链。"""
    import time
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        items = apply_upload_record(campaign_id, page_size=1).get("items") or []
        if items and str(items[0].get("fileKey")) != str(pre_id):
            last = items[0]
            if last.get("status") == 3:
                return {"done": True, "status": 3, "total": last.get("totalCount"),
                        "success": last.get("successCount"), "fail": last.get("failCount"),
                        "successLink": last.get("successLink"), "failLink": last.get("failLink")}
        time.sleep(interval)
    return {"done": False, "note": f"轮询{timeout}s未完成，稍后 campaign_apply_upload_record 查",
            "last": last}


def _table_apply_token(campaign_id, rows) -> str:
    return _confirm_token({"path": "/uploadApplySave", "campaignId": str(campaign_id),
                           "uploadType": UPLOAD_TYPE_APPLY,
                           "rows": sorted(f"{r['skuId']}:{r['ratio']}" for r in rows)})


def table_apply_dryrun(campaign_id: str | int, sku_ids: list, ratio=None) -> dict:
    """**上传表格报名 DRY-RUN**：回显行数/比例分布 + confirm_token，不生成不上传。

    `sku_ids` 可以是 [skuId...]（配统一 `ratio`）或 [{skuId, ratio}...]（逐款不同比例）。
    `ratio`=1~90 整数，**降百分之几**；None ⇒ 留空取活动默认比例。"""
    rows = _norm_apply_rows(sku_ids, ratio)
    from collections import Counter
    return {"would_apply": False, "campaignId": str(campaign_id), "rows": len(rows),
            "比例分布": dict(Counter("默认" if r["ratio"] is None else f"降{r['ratio']}%"
                                     for r in rows)),
            "note": "DRY-RUN：未生成/未上传。真执行：相同参数 + confirm=confirm_token 调 table_apply。"
                    " 比例语义=降百分之几（5 ⇒ 降 5%）。",
            "confirm_token": _table_apply_token(campaign_id, rows)}


@audited("campaign", "table_apply")
def table_apply(campaign_id: str | int, sku_ids: list, ratio=None, confirm: str = "",
                xlsx_path: Optional[str] = None, wait: bool = True,
                wait_timeout: int = 180) -> dict:
    """**上传表格报名真执行**：生成2列xlsx→/fileUpload(uploadType=2)→/uploadApplySave。

    需相同参数先 `table_apply_dryrun` 拿 confirm_token。**wait=True 自动轮询到终态并回执自验证**
    （返回 result: 成功/失败数 + failLink 失败明细直链）。

    ★相对 `apply`(batchApply) 的优势：batchApply **一款不合格整批失败**，且并发>1 会撞
      「操作中，请勿频繁操作」；表格报名一次提交整批、平台逐行判定，失败的单独进 failLink。
      2026-08-17 实测 batchApply 逐款提交 113 款：66 款撞节流需重试、耗时数分钟。
    ⚠️「商品不在活动可报范围内」是**选品池未收录**，换成表格报名同样会失败——
      那是资格问题不是通道问题（实测该活动可报池按 SPU 全有全无、滞后创建约 3~5 天）。"""
    import os
    rows = _norm_apply_rows(sku_ids, ratio)
    if confirm != _table_apply_token(campaign_id, rows):
        raise BlacklightError("表格报名真执行需二次确认：先用相同参数跑 table_apply_dryrun 拿 confirm_token 再带 confirm。")
    pre = apply_upload_record(campaign_id, page_size=1).get("items") or []
    pre_id = pre[0].get("fileKey") if pre else None
    xlsx_path = xlsx_path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          f"_tableapply_campaign_{campaign_id}.xlsx")
    built = _build_apply_xlsx(rows, xlsx_path)

    cookie = jd_auth.ensure_session()
    fname = os.path.basename(xlsx_path)
    files = {"file": (fname, open(xlsx_path, "rb").read(), XLSX_MIME_C)}
    form = {"campaignId": str(campaign_id), "uploadType": str(UPLOAD_TYPE_APPLY)}
    # ★跨频道共享的文件上传闸 + 退避重试（clean_reject=True：被挡时文件没进队列，一条没落地）
    up = retry_throttled(
        lambda: post_multipart(MCPMAN, "/fileUpload", form, files, cookie,
                               extra_headers={"X-Requested-With": "XMLHttpRequest"}),
        "upload.file",
        is_bad=lambda r: (r or {}).get("message") if not (r or {}).get("success") else None) or {}
    if not up.get("success"):
        raise BlacklightError(f"/fileUpload 失败: {up.get('message') or up.get('code')}")
    file_key = up.get("data")

    save = _post_send("/uploadApplySave",
                      {"campaignId": str(campaign_id),
                       "fileInfos": [{"fileKey": file_key, "fileName": fname}],
                       "uploadType": UPLOAD_TYPE_APPLY, "resourceType": RESOURCE_TYPE,
                       "resourceId": str(campaign_id), "bizId": str(campaign_id),
                       "operateRequestSource": 2})
    out = {"executed": True, "campaignId": str(campaign_id), "rows": built["rows"],
           "xlsx": xlsx_path, "fileKey": file_key, "save_response": save,
           "success": bool(isinstance(save, dict) and save.get("success") is True),
           "confirm_token": _table_apply_token(campaign_id, rows)}
    if wait:
        out["result"] = _await_apply_upload(campaign_id, pre_id, timeout=wait_timeout)
    else:
        out["note"] = "已提交，用 campaign_apply_upload_record 轮询进度+成功/失败清单直链。"
    return out


# --------------------------------------------------------------------------- #
# 功能二：报名（sign up）——只 dry-run
# batchApply 真实结构（block-and-capture 实证，2026-07-03，SKU 10196579387336）：
#   body = {resourceParam, applyList:[{applyItemList:[{formItemId, value}, ...]}], resourceList:[resourceParam]}
#   applyItemList 每项 = {formItemId: formSet里该字段的 id, value: 值}
#   字段(用 formSet 的 code 定位其 id)：
#     skuId(value=数字SKU) | discountType(value="2"每件折/比例 或 "1"每件减/金额)
#     discount(value=比例数字,如5=直降5%) | amount(value=金额或"")
# --------------------------------------------------------------------------- #
DISCOUNT_TYPE = {"ratio": "2", "amount": "1"}  # ratio=每件折(比例), amount=每件减(金额)；value 为字符串


def get_form_set(campaign_id: str | int) -> list:
    """读报名表单配置（POST /apply/form/formSet {campaignId}）。返回表单项列表(含各字段 code/id/名称/选项)。"""
    j = _post("/apply/form/formSet", {"campaignId": str(campaign_id)})
    data = j.get("data", j)
    if isinstance(data, dict):  # 服务端把 list 包成 {"0":..,"1":..}
        return [data[k] for k in sorted(data, key=lambda x: int(x)) if str(x).isdigit()]
    return data or []


_CAMPAIGN_LIST_CACHE = None


def list_campaigns(refresh: bool = False) -> list:
    """商家可报活动列表（POST /apply/trust/campaign，分页，缓存）。返回 [{campaignId,campaignName,campaignMethod}]。
    用作「活动名→campaignId」映射源（markettool 统一退出把促销名对到活动ID）。"""
    global _CAMPAIGN_LIST_CACHE
    if _CAMPAIGN_LIST_CACHE is not None and not refresh:
        return _CAMPAIGN_LIST_CACHE
    out, page = [], 1
    while True:
        d = _post("/apply/trust/campaign", {"page": page, "pageSize": 100}).get("data") or {}
        for it in (d.get("items") or []):
            out.append({"campaignId": str(it.get("campaignId")), "campaignName": it.get("campaignName") or "",
                        "campaignMethod": it.get("campaignMethod")})
        if page >= (d.get("totalPage") or 1):
            break
        page += 1
    _CAMPAIGN_LIST_CACHE = out
    return out


def resolve_campaign_id(name: str, refresh: bool = False) -> Optional[str]:
    """按活动名对 campaignId：先精确匹配 campaignName，再包含匹配。找不到返回 None。"""
    name = (name or "").strip()
    if not name:
        return None
    cl = list_campaigns(refresh=refresh)
    for it in cl:                       # 精确
        if it["campaignName"] == name:
            return it["campaignId"]
    for it in cl:                       # 包含（促销名可能有前后缀差异）
        cn = it["campaignName"]
        if cn and (name in cn or cn in name):
            return it["campaignId"]
    return None


_FORMSET_CACHE: dict = {}


def _campaign_fields(campaign_id) -> list:
    """formSet 字段列表（有序，含 code/id），带缓存。formItemId 每活动不同，必须动态取。"""
    key = str(campaign_id)
    if key not in _FORMSET_CACHE:
        fields = [f for f in get_form_set(campaign_id) if f.get("code") and f.get("id") is not None]
        if not any(f["code"] == "skuId" for f in fields):
            raise BlacklightError(f"formSet 无 skuId 字段（现有 {[f.get('code') for f in fields]}）")
        _FORMSET_CACHE[key] = fields
    return _FORMSET_CACHE[key]


MAX_APPLY_BATCH = 50  # 单次真执行报名 SKU 数上限（护栏）

# ★两次 batchApply 之间的最小间隔（秒）——**单一事实源在 `core.policy`**，这里只做别名。
#   2026-08-17 实证：188 次 apply 里 **187 次只带 1 款**（被拆成了逐款循环），
#   中位间隔 **0.0 秒**、103 次挤在同一秒 ⇒ 66 次撞「操作中，请勿频繁操作」（35%）。
#   ⚠️这道闸**不管调用方怎么循环都生效**——护栏必须在被调用方，不能指望调用方记得。
#   它不是给正常用法准备的：满批 50 款正常调根本碰不到 2 秒。
MIN_BATCH_APPLY_INTERVAL = _POLICY_RULES["campaign.batchApply"]["min_interval"]

# 平台节流文案白名单（撞上说明**整批被拒、一条没落地**，可干净重试；别当部分失败去做增量补报）
BATCH_APPLY_THROTTLE_HINTS = _POLICY_RULES["campaign.batchApply"]["hints"]


def _num(v):  # 整数值不带 .0，与页面实际发送一致
    return int(v) if isinstance(v, float) and v.is_integer() else v


def _build_apply_body(campaign_id, sku_ids, discount_type=None, discount=None, amount=None,
                      extra=None) -> tuple[dict, dict, str | None]:
    """
    **formSet 驱动**组装 batchApply body（自适配任意 campaign 玩法）：
      - 券类活动 formSet 只有 skuId → applyItemList 只填 skuId；
      - 官方直降 formSet 有 skuId+discountType+discount+amount → 按 discount_type/discount/amount 填。
    applyItemList 覆盖 formSet **全部字段**，已知角色填值、其余留空（与页面实证一致）。
    extra: {code: value} 覆盖/补充其它未知字段。返回 (body, code→id映射, dt|None)。
    """
    sku_ids = [str(s).strip() for s in sku_ids if str(s).strip()]
    if not sku_ids:
        raise BlacklightError("需要至少一个 skuId")
    fields = _campaign_fields(campaign_id)
    codes = {f["code"] for f in fields}
    extra = extra or {}
    dt = None
    if "discountType" in codes:                 # 直降类活动才解析力度
        if discount_type is None:
            discount_type = "ratio"
        dt = DISCOUNT_TYPE.get(discount_type)
        if dt is None:
            raise BlacklightError(f"discount_type 只能是 {list(DISCOUNT_TYPE)}")
        if discount_type == "amount" and amount is None:
            raise BlacklightError("discount_type=amount(每件减) 时必须给 amount(直降金额)")

    def val(code, sku):
        if code == "skuId":
            return int(sku)
        if code == "discountType":
            return dt if dt is not None else ""
        if code == "discount":
            return _num(discount) if (discount_type == "ratio" and discount is not None) else ""
        if code == "amount":
            return _num(amount) if discount_type == "amount" else ""
        return extra.get(code, "")

    rp = _resource_param(campaign_id)
    body = {
        "resourceParam": rp,
        "applyList": [{"applyItemList": [{"formItemId": f["id"], "value": val(f["code"], s)}
                                         for f in fields]} for s in sku_ids],
        "resourceList": [rp],
    }
    fid = {f["code"]: f["id"] for f in fields}
    return body, fid, dt


def apply_dryrun(campaign_id: str | int, sku_ids: list[str | int],
                 discount_type: str = "ratio", discount=None, amount=None) -> dict:
    """
    组装报名(batchApply)请求但**绝不发送**。结构为 block-and-capture 实证真值。
      - discount_type: "ratio"(每件折/比例, 促销力度%) | "amount"(每件减/金额)
      - discount: ratio 模式的力度（如 5 表示直降5%）；不填=空串(取活动默认)
      - amount:   amount 模式的直降金额（必填）
    返回体含 confirm_token：用**相同参数** + confirm=该token 调 yx_apply 才会真执行。
    """
    body, fid, dt = _build_apply_body(campaign_id, sku_ids, discount_type, discount, amount)
    token = _confirm_token(body)
    note = ("DRY-RUN：报名请求未发送。结构与 formItemId 均为 block-and-capture 实证真值"
            "（SKU 10196579387336, 2026-07-03）。真执行：用相同参数 + confirm=confirm_token 调 yx_apply。")
    return _dryrun_envelope("/apply/batchApply", body, note,
                            {"discountTypeResolved": {"input": discount_type, "value": dt},
                             "formItemIds": fid, "confirm_token": token,
                             "sku_count": len(body["applyList"])})


def _await_applied(campaign_id, sku_ids: list, before_count: int,
                   timeout: int = 120, interval: int = 10) -> dict:
    """报名回执自验证：batchApply 只回**批级** success，逐个是否入库需另验。
    先轮询活动已报名 totalCount 涨够 n（1 次/轮，立等），再做一次 get_sku_status 拿逐SKU明细
    （**按 skuId 服务端过滤·索引约 30-60s 跟上**）。**pending 多为索引未跟上而非失败**。"""
    import time as _t
    n = len(sku_ids)
    cur = before_count
    waited = 0
    while waited < timeout:
        _t.sleep(interval); waited += interval
        cur = get_applied_page(campaign_id, page=1, page_size=1).get("totalCount") or cur
        if cur - before_count >= n:
            break
    delta = cur - before_count
    st = get_sku_status(campaign_id, sku_ids).get(str(campaign_id), {})
    verified = [s for s in sku_ids if st.get(str(s), {}).get("applied")]
    pending = [s for s in sku_ids if not st.get(str(s), {}).get("applied")]
    return {"done": delta >= n, "submitted": n, "count_delta": delta,
            "verified_per_sku": len(verified), "pending_index": pending,
            "note": ("count_delta≥submitted 即全部入库；per-SKU 索引 ~30-60s 跟上，"
                     "pending 多为索引延迟非失败，可稍后 get_sku_status 复查；"
                     "直降促销生效/审核另有刷新延迟，别看 markettool 秒级复查。"
                     "⚠️活动为多人共用时 count_delta 可能被他人报名干扰，以 verified_per_sku 为准。")}


@audited("campaign", "apply")
def apply(campaign_id: str | int, sku_ids: list = None, discount_type: str = "ratio",
          discount=None, amount=None, confirm: str = "", wait: bool = True,
          wait_timeout: int = 120) -> dict:
    """
    **真执行**报名(batchApply)。护栏：
      1) 必须先 apply_dryrun 拿 confirm_token，用相同参数 + confirm=该token 才放行（参数一变 token 就变）。
      2) 单次 SKU 数 ≤ MAX_APPLY_BATCH。
      3) 两次调用间隔 ≥ MIN_BATCH_APPLY_INTERVAL 秒（模块级兜底，见下）。
    这会对活动**真实提交报名**（承诺促销价），是不可轻易撤回的写操作。

    ## ★该用 batchApply 还是表格上传（2026-08-19 定，之前两边都没写清楚，才退化成逐款循环）
    | 场景 | 用哪个 | 为什么 |
    |---|---|---|
    | **1~2 款探针** | **本函数** | 同步、立即拿回执；走表格要等异步轮询，纯浪费 |
    | **≤50 款且都过了预检** | **本函数满批** | 1 次同步 POST，比表格的「上传→save→轮询」三段更快，且不占文件闸 |
    | **>50 款，或预期有不合格行** | **`table_apply`** | 一次传完 + **平台逐行判定**，失败行进 `failLink` 明细 |
    | **多个小批量连着发** | **本函数** | 表格受**文件上传节流**约束(~90~120s 一个文件、且跨频道共享)，小批量反而更慢 |

    ⚠️**绝对不要逐款循环调本函数**。2026-08-17 就是这么干的：188 次调用里 187 次只带 1 款、
      中位间隔 0.0 秒 ⇒ 66 次撞「操作中，请勿频繁操作」(35%)、4.2 分钟只落地 48 款。
      同样这批走 `table_apply`：92 行 / 约 69 秒 / 92 成功 0 失败。
    ⚠️旧 docstring 曾称「batchApply 一款不合格整批失败」——**该说法未被审计支持**：
      15 次多 SKU 调用 13 次成功（含四次 50 款满批），仅有的两次失败一次是
      比例填成 0.95 的**参数 bug**(`For input string: "0.95"`)、一次原因未能确认。
      在拿到真凭据前别拿它当拆成逐款的理由。**待验**：故意混一个不在可报池的 SKU 满批提交一次即可定死。

    **wait=True 回执自验证**：batchApply 回执 success 仅表示"批被接收"，逐个是否入库另验
    → 提交后自动轮询活动 totalCount + get_sku_status，返回 result{verified_per_sku/pending_index}。
    **判成败以 result 为准，别被刚报完 markettool/get_sku_status 的"未报名"误导（索引/生效延迟）**。
    """
    skus = [str(s).strip() for s in (sku_ids or []) if str(s).strip()]
    body, _, _ = _build_apply_body(campaign_id, sku_ids, discount_type, discount, amount)
    token = _confirm_token(body)
    if confirm != token:
        raise BlacklightError(
            "报名真执行需二次确认：先用相同参数跑 apply_dryrun 拿 confirm_token，"
            f"再带 confirm=该token 调用（当前传入 confirm 与之不符）。本次应有 token=<见 dry-run 输出>。")
    n = len(body["applyList"])
    if n > MAX_APPLY_BATCH:
        raise BlacklightError(f"单次报名 SKU 数 {n} 超过上限 {MAX_APPLY_BATCH}，请分批。")
    before = 0
    if wait:
        before = get_applied_page(campaign_id, page=1, page_size=1).get("totalCount") or 0
    pace("campaign.batchApply")   # ★兜底闸：不管调用方怎么循环都生效（护栏在被调用方）
    resp = _post_send("/apply/batchApply", body)
    ok = bool(isinstance(resp, dict) and resp.get("success") is True)
    out = {"executed": True, "confirm_token": token, "request_body": body,
           "response": resp, "success": ok}
    msg = str((resp or {}).get("message") or "") if isinstance(resp, dict) else ""
    if not ok and any(h in msg for h in BATCH_APPLY_THROTTLE_HINTS):
        # 撞节流 ⇒ **整批被拒、一条没落地**。这里点名正解，别让调用方在循环里白烧几十次。
        out["throttled"] = True
        out["hint"] = (f"撞平台节流（{msg}）：整批被拒、一条没落地，可干净重试。"
                       f"若你在逐款循环调本函数——**那就是病根**，改用满批(≤{MAX_APPLY_BATCH})或 "
                       f"table_apply（>{MAX_APPLY_BATCH} 款/预期有不合格行时）。见本函数 docstring 的路由表。")
        return out
    if wait and ok:
        out["result"] = _await_applied(campaign_id, skus, before, timeout=wait_timeout)
    return out


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description="yx mcpman 只读/干跑客户端")
    ap.add_argument("campaign_id")
    ap.add_argument("--detail", action="store_true")
    ap.add_argument("--applied", action="store_true")
    ap.add_argument("--sku-status", help="逗号分隔的 skuId，查这些 SKU 在活动中的状态")
    ap.add_argument("--page", type=int, default=1)
    ap.add_argument("--page-size", type=int, default=10)
    ap.add_argument("--dryrun-quit", help="逗号分隔的报名ID，走 batchQuit dry-run")
    ap.add_argument("--dryrun-quit-sku", help="逗号分隔的 skuId，先解析+校验可退再 batchQuit dry-run")
    ap.add_argument("--dryrun-all-quit", action="store_true")
    ap.add_argument("--form-set", action="store_true", help="读报名表单配置")
    ap.add_argument("--dryrun-apply", help="逗号分隔的 skuId，报名 batchApply dry-run")
    ap.add_argument("--discount-type", default="ratio", choices=["ratio", "amount"])
    ap.add_argument("--discount", type=float, default=None, help="ratio 力度(百分比)，如 10")
    ap.add_argument("--amount", type=float, default=None, help="amount 直降金额")
    a = ap.parse_args()

    out: Any
    if a.detail:
        out = get_activity_detail(a.campaign_id)
    elif a.sku_status:
        out = get_sku_status(a.campaign_id, a.sku_status.split(","))
    elif a.applied:
        out = get_applied_page(a.campaign_id, a.page, a.page_size)
    elif a.form_set:
        out = get_form_set(a.campaign_id)
    elif a.dryrun_apply:
        out = apply_dryrun(a.campaign_id, a.dryrun_apply.split(","),
                           discount_type=a.discount_type, discount=a.discount, amount=a.amount)
    elif a.dryrun_all_quit:
        out = withdraw_dryrun(a.campaign_id, all_quit=True)
    elif a.dryrun_quit_sku:
        out = withdraw_dryrun(a.campaign_id, sku_ids=a.dryrun_quit_sku.split(","))
    elif a.dryrun_quit:
        out = withdraw_dryrun(a.campaign_id, apply_ids=a.dryrun_quit.split(","))
    else:
        out = get_activity_detail(a.campaign_id)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
