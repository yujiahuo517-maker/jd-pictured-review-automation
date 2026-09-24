"""
yx-mcp MCP server（stdio）：把 yx.jd.com / mcpman.jd.com 的能力暴露为 MCP tools。

命名约定：**每个业务场域带场域限定词** `yx_<scene>_*`（campaign/subsidy/markettool/bybt/ms）。
鉴权(yx_login*)与统一退出(yx_promo_*)跨场域，不带场域词。
只读/查询工具的错误经 `@safe` 统一转 {"error":...}；写工具保留显式 {executed/withdrawn:False, reason}（表明未执行）。

写操作安全：真写(apply/withdraw)走二次确认令牌——先 *_dryrun 拿 confirm_token，再用相同参数 + confirm 调真执行。

注册：
  claude mcp add yx -- python "C:/Users/wangruihan9/.claude/skills/yx-mcp/server.py"
"""
from __future__ import annotations

import functools
import os
import sys

from mcp.server.fastmcp import FastMCP  # noqa: E402

from blacklight.core import auth as jd_auth # noqa: E402
from blacklight.yx import client as yx_client # noqa: E402
from blacklight.yx import subsidy as yx_subsidy # noqa: E402
from blacklight.yx import markettool as yx_markettool # noqa: E402
from blacklight.yx import bybt as yx_bybt # noqa: E402
from blacklight.yx import ms as yx_ms # noqa: E402
from blacklight.yx import stoploss as yx_stoploss # noqa: E402  (止亏方案生成器：对账→归因→过禁令→过可执行性)
from blacklight.yx import ssm as yx_ssm # noqa: E402  (顺手买/黄流结算页专享价：第三套报名体系，走 mac/common/fileProcess)
from blacklight.yx import stacking as yx_stacking # noqa: E402  (券促叠加/生效规则：官方51×51矩阵查询)
from blacklight.core import BlacklightError  # noqa: E402

mcp = FastMCP("blacklight-yx")


def safe(fn):
    """只读/查询工具的错误包装：捕 BlacklightError → {"error": ...}（保留函数签名给 FastMCP 建 schema）。
    写工具不用它——它们保留显式 {executed/withdrawn:False, reason} 表示未执行。"""
    @functools.wraps(fn)
    def w(*a, **kw):
        try:
            return fn(*a, **kw)
        except BlacklightError as e:
            return {"error": str(e)}
    return w


# ------------------------------- 鉴权（跨场域） ------------------------------- #
@mcp.tool()
def yx_login_status() -> dict:
    """查看 yx 登录态：**usable(能否用,以在线探活为准)** + note + pin(当前操作人) + mcpman_live。看 usable 别看 remaining_est(那是估算,会话cookie无真到期,常显示"已过期"但其实还能用)。pin 空先 yx_set_pin。"""
    return jd_auth.status_dict()


@mcp.tool()
def yx_set_pin(pin: str) -> dict:
    """[多用户] 设置当前操作人 ERP/PIN（持久化到 credentials.json）。设一次即可：之后 eligible 类查询默认用它、审计日志记录操作人。也可用环境变量 YX_PIN。"""
    return {"pin": jd_auth.set_pin(pin), "note": "已保存。eligible 查询与审计将以此身份进行。"}


@mcp.tool()
@safe
def yx_doctor() -> dict:
    """[无人值守] **契约巡检**：打关键接口(formset/毛利监控/getBatchId/活动详情/登录)校验返回结构没漂移。返回 {healthy, drift[], checks[]}。抓包封装的接口会随页面改版漂移——Agent 自动跑前先 doctor，有 drift 转人工。"""
    from blacklight.yx import doctor
    return doctor.run()


@mcp.tool()
@safe
def yx_write_status(with_stats: bool = True) -> dict:
    """[无人值守] 查所有真写路径的**活体验证矩阵**：verified=True(已真跑通,可自动)/False(契约齐但未活体,应小批或转人工)/None(未登记)。Agent 放手前据此决定自动执行 or 转人工。

    `with_stats=True` additionally 读审计日志给出**事实面**：每条路径的真实使用次数/成功/被闸拦/闲置天数、
    闸门拦截统计、以及**矩阵(声明) vs 审计(事实)的对账**——矩阵是手工维护的，人会忘，对账后不靠纪律。
    候删清单里只有 `可直接动手=True` 的才有数据支撑，其余都附了「先确认」什么（见返回里的工具限制说明）。"""
    from blacklight.core import base as jd_core
    m = jd_core.WRITE_VERIFICATION
    out = {"total": len(m), "verified": [k for k, v in m.items() if v.get("verified") is True],
           "unverified_小批或转人工": {k: v.get("note") for k, v in m.items() if v.get("verified") is False},
           "matrix": m}
    if with_stats:
        from blacklight.core import rulestat
        r = rulestat.report()
        out["审计事实"] = {k: r[k] for k in
                           ("审计范围", "记录数", "写路径使用度", "闸门拦截",
                            "矩阵对账", "候删清单", "⚠️本工具的限制") if k in r}
    return out


@mcp.tool()
def yx_login() -> dict:
    """失效时用浏览器登录 yx.jd.com（ERP/focus realm）并保存 cookie。会弹出 Chrome 窗口。返回登录后状态。"""
    jd_auth.ensure_session(auto_relogin=True)
    return jd_auth.status_dict()


# --------------------- 场域：campaign（招商/会场活动报名） --------------------- #
@mcp.tool()
def yx_campaign_get_detail(campaign_id: str) -> dict:
    """[campaign] 读活动详情（POST /campaign/getCampaignApplyDetailById）：活动名/状态/报名起止/退出规则(applySkuQuitType)。"""
    return yx_client.get_activity_detail(campaign_id)


@mcp.tool()
def yx_campaign_get_applied(campaign_id: str, page: int = 1, page_size: int = 10) -> dict:
    """[campaign] 读已报名列表（已报名管理，POST /apply/applied/page）。行主键 id（报名ID）。"""
    return yx_client.get_applied_page(campaign_id, page=page, page_size=page_size)


@mcp.tool()
def yx_campaign_get_sku_status(campaign_id: str, sku_ids: list[str]) -> dict:
    """
    [campaign] 查一批 SKU 在活动中的状态（只读）：是否已报名、报名中/报名完成、报名ID、能否退出(+被禁原因)、商品名/类目。
    未命中已报名列表的标记为"未报名"。返回 {campaignId: {skuId: {...}}}。
    """
    return yx_client.get_sku_status(campaign_id, sku_ids)


@mcp.tool()
def yx_campaign_get_form_set(campaign_id: str) -> list:
    """[campaign] 读报名表单配置（/apply/form/formSet）：各字段 code/名称/选项。确认合法的优惠类型与力度取值。"""
    return yx_client.get_form_set(campaign_id)


@mcp.tool()
def yx_campaign_apply_dryrun(campaign_id: str, sku_ids: list[str],
                             discount_type: str = "ratio", discount: float | None = None,
                             amount: float | None = None) -> dict:
    """
    [campaign] 报名 DRY-RUN：组装 batchApply 但**绝不发送**，返回将要 POST 的 body + confirm_token 供核对。
    **formSet 驱动，自适配任意活动玩法**：券类等只有 skuId 的活动，discount_type/discount 会被忽略，只报 SKU；
    官方直降等直降类才用下列参数：
    - discount_type: "ratio"(每件折/比例，促销力度%) | "amount"(每件减/金额)
    - discount: ratio 力度，如 10=直降10%；不填=取活动默认
    - amount:   amount 直降金额（discount_type=amount 时必填）
    真执行：用相同参数 + confirm=confirm_token 调 yx_campaign_apply。
    """
    return yx_client.apply_dryrun(campaign_id, sku_ids, discount_type=discount_type,
                                  discount=discount, amount=amount)


@mcp.tool()
def yx_campaign_apply(campaign_id: str, sku_ids: list[str], discount_type: str = "ratio",
                      discount: float | None = None, amount: float | None = None,
                      confirm: str = "", wait: bool = True, wait_timeout: int = 120) -> dict:
    """
    [campaign] **真执行**报名（对活动真实提交，承诺促销价，不可轻易撤回）。
    必须先用相同参数跑 yx_campaign_apply_dryrun 拿 confirm_token，再带 confirm=该token 调用；否则被拒绝。
    单次 SKU 数上限 50。**官方直降 5% = discount_type='ratio', discount=5**（与活动内现有报名口径一致）。
    ★★**该用本工具还是 yx_campaign_table_apply**：1~2 款探针 / ≤50 款且过了预检 → **本工具（满批一次传，别逐款）**；
      >50 款 或 预期有不合格行 → **table_apply**（一次传完 + 平台逐行判定，失败行进 failLink）；
      多个小批量连着发 → 本工具（table_apply 受文件上传节流约束 ~90~120s 一个文件、且跨频道共享）。
    ⚠️**绝对不要逐款循环调本工具**：2026-08-17 就是这么干的，188 次里 187 次只带 1 款、中位间隔 0.0s
      ⇒ 66 次撞「操作中，请勿频繁操作」、4.2 分钟只落地 48 款。同批走 table_apply 是 92 行/69 秒/0 失败。
      已加模块级兜底闸（两次调用间隔 ≥2s）+ 撞节流时返回 `throttled=True` 并点名正解。
    **wait=True 回执自验证**：batchApply 只回批级 success，逐个入库另验→返回 result{verified_per_sku/pending_index}。
    **判成败以 result 为准**：刚报完 markettool/get_sku_status 显示"未报名"多是索引/生效延迟、非失败（本工具已内置轮询规避）。
    """
    try:
        return yx_client.apply(campaign_id, sku_ids, discount_type=discount_type,
                               discount=discount, amount=amount, confirm=confirm,
                               wait=wait, wait_timeout=wait_timeout)
    except yx_client.BlacklightError as e:
        return {"executed": False, "reason": str(e)}


@mcp.tool()
def yx_campaign_withdraw_dryrun(campaign_id: str, sku_ids: list[str] | None = None,
                                apply_ids: list[str] | None = None,
                                all_quit: bool = False) -> dict:
    """
    [campaign] 退出（撤回报名）DRY-RUN：组装但**绝不发送**，返回将要 POST 的 body + confirm_token 供核对。
    - sku_ids:   先 sku→报名ID 并校验 operateList 可退，只纳入可退行，跳过项在 eligibility.skipped 带原因（推荐）
    - apply_ids: 直接给报名ID（/apply/batchQuit）
    - all_quit:  整场退出（/apply/all/quit，仅 dry-run）
    真执行：用相同参数 + confirm=confirm_token 调 yx_campaign_withdraw。
    """
    return yx_client.withdraw_dryrun(campaign_id, apply_ids=apply_ids,
                                     sku_ids=sku_ids, all_quit=all_quit)


@mcp.tool()
def yx_campaign_withdraw(campaign_id: str, sku_ids: list[str] | None = None,
                         apply_ids: list[str] | None = None, all_quit: bool = False,
                         confirm: str = "") -> dict:
    """
    [campaign] **真执行**退出（对活动真实撤回报名，不可轻易撤回）。
    必须先用相同参数跑 yx_campaign_withdraw_dryrun 拿 confirm_token，再带 confirm=该token 调用；否则被拒绝。
    单次 SKU 数上限 50；all_quit 不支持真执行。
    """
    try:
        return yx_client.withdraw(campaign_id, sku_ids=sku_ids, apply_ids=apply_ids,
                                  all_quit=all_quit, confirm=confirm)
    except yx_client.BlacklightError as e:
        return {"executed": False, "reason": str(e)}


@mcp.tool()
def yx_campaign_table_withdraw_dryrun(campaign_id: str, sku_ids: list[str]) -> dict:
    """[campaign] **上传表格退出 DRY-RUN**：按 SKU 表格批量退出（绕开"已报名浏览上限1万"，直接给 skuId 列表即可）。回显 SKU 数 + confirm_token，不生成不上传。"""
    try:
        return yx_client.table_withdraw_dryrun(campaign_id, sku_ids)
    except yx_client.BlacklightError as e:
        return {"would_withdraw": False, "reason": str(e)}


@mcp.tool()
def yx_campaign_table_withdraw(campaign_id: str, sku_ids: list[str], confirm: str = "",
                               wait: bool = True, wait_timeout: int = 120) -> dict:
    """[campaign] **上传表格退出真执行**（绕开已报名浏览上限1万，大批量正道）：生成1列xlsx→/fileUpload→/uploadApplySave 批量退出。需相同 sku_ids 先 yx_campaign_table_withdraw_dryrun 拿 confirm_token 再带 confirm。**wait=True 自动轮询到终态、回执自验证**(返回 result: 成功/失败数+清单直链，不看有延迟的看板)。"""
    try:
        return yx_client.table_withdraw(campaign_id, sku_ids, confirm=confirm,
                                        wait=wait, wait_timeout=wait_timeout)
    except yx_client.BlacklightError as e:
        return {"executed": False, "reason": str(e)}


@mcp.tool()
def yx_campaign_table_apply_dryrun(campaign_id: str, sku_ids: list, ratio: int = None) -> dict:
    """[campaign] **上传表格报名 DRY-RUN**：按 SKU 表格批量报名（官方直降等），回显行数/比例分布 + confirm_token，不生成不上传。
    sku_ids 可为 [skuId...]（配统一 ratio）或 [{skuId, ratio}...]（逐款不同比例）。
    ★ratio = **降百分之几**的整数 1~90（填 5 ⇒ 降 5%）；留空取活动默认比例。别填 0.95(平台解析失败) 或 95(会降 95%)。"""
    try:
        return yx_client.table_apply_dryrun(campaign_id, sku_ids, ratio=ratio)
    except yx_client.BlacklightError as e:
        return {"would_apply": False, "reason": str(e)}


@mcp.tool()
def yx_campaign_table_apply(campaign_id: str, sku_ids: list, ratio: int = None,
                            confirm: str = "", wait: bool = True, wait_timeout: int = 180) -> dict:
    """[campaign] **上传表格报名真执行**（大批量正道，优于逐款 batchApply）：生成2列xlsx→/fileUpload(uploadType=2)→/uploadApplySave。需相同参数先 yx_campaign_table_apply_dryrun 拿 confirm_token 再带 confirm。**wait=True 轮询到终态并回执自验证**（result 含成功/失败数 + failLink 失败明细直链）。
    ★为什么别用 yx_campaign_apply 做大批量：batchApply **一款不合格整批失败**，且并发>1 撞「操作中，请勿频繁操作」（实测 113 款里 66 款中招）。
    ⚠️「商品不在活动可报范围内」是选品池未收录（资格问题），换通道同样失败。"""
    try:
        return yx_client.table_apply(campaign_id, sku_ids, ratio=ratio, confirm=confirm,
                                     wait=wait, wait_timeout=wait_timeout)
    except yx_client.BlacklightError as e:
        return {"executed": False, "reason": str(e)}


@mcp.tool()
def yx_campaign_apply_upload_record(campaign_id: str, page: int = 1, page_size: int = 10) -> dict:
    """[campaign] 查**表格报名**的上传处理进度（uploadType=2）：status(3=完成)/总数/成功/失败 + 成功·失败清单直链。只读。"""
    try:
        return yx_client.apply_upload_record(campaign_id, page=page, page_size=page_size)
    except yx_client.BlacklightError as e:
        return {"error": str(e)}


@mcp.tool()
@safe
def yx_campaign_upload_record(campaign_id: str, page: int = 1, page_size: int = 10) -> dict:
    """[campaign] 查上传表格退出的处理进度（getFileUploadRecord），倒序。每条：status(3=完成)/totalCount/successCount/failCount/**successLink+failLink(成功/失败清单直链)**/uploadTime。"""
    return yx_client.upload_record(campaign_id, page=page, page_size=page_size)


# ---------------------- 场域：subsidy（国家补贴 / 政府补贴） ---------------------- #
@mcp.tool()
def yx_subsidy_get_applied(block_id: str, page: int = 1, page_size: int = 10,
                           sku_id: str = "") -> dict:
    """[subsidy] 读某收品池已报名素材列表（mac.jd.com/apply/page）。block_id=收品池编号(areaId)。行主键 报名编号(applyId)。
    ★**查单 SKU 报没报务必传 sku_id**（2026-08-24 审查补透传）：服务端支持精确过滤，比翻页快得多；
      默认 page_size=10，靠翻页找会在翻不到时得到**假阴性的「未报名」**。"""
    return yx_subsidy.get_applied(block_id, page=page, page_size=page_size,
                                  sku_id=(sku_id or None))


@mcp.tool()
def yx_subsidy_check_sku(block_id: str, sku_id: str) -> dict:
    """[subsidy] 校验 SKU 对某收品池的可报性并取商品信息（只读）：{eligible, code, message, jdPrice(基准价), categoryName}。"""
    return yx_subsidy.check_sku(block_id, sku_id)


@mcp.tool()
def yx_subsidy_apply_dryrun(block_id: str, sku_id: str, promo_time: str,
                            base_price: str = "", discount: str = "", merchant: str = "京喜自营",
                            energy_level: str = "0") -> dict:
    """
    [subsidy] 国补报名 DRY-RUN：组装 apply/batch/create 但**不发送**，返回将 POST 的 form + confirm_token。
    - block_id: 收品池编号；discount 留空默认取该池力度档位（15%池→"15"）
    - base_price 留空则自动取前台京东价(check_sku)并拦截不可报SKU；promo_time: "开始~结束"
    真执行：相同参数 + confirm=confirm_token 调 yx_subsidy_apply。仅已捕获模板的池可用(见 NOTES)。
    """
    return yx_subsidy.apply_dryrun(block_id, sku_id, base_price=(base_price or None),
                                   discount=(discount or None), merchant=merchant,
                                   energy_level=energy_level, promo_time=promo_time)


@mcp.tool()
def yx_subsidy_apply(block_id: str, sku_id: str, promo_time: str, base_price: str = "",
                     discount: str = "", merchant: str = "京喜自营", energy_level: str = "0",
                     confirm: str = "") -> dict:
    """[subsidy] **真执行**国补报名（真实提交）。discount/base_price 留空自动取池力度/前台京东价。需相同参数先 dry-run 拿 confirm_token 再带 confirm。"""
    try:
        return yx_subsidy.apply(block_id, sku_id, base_price=(base_price or None),
                                discount=(discount or None), merchant=merchant,
                                energy_level=energy_level, promo_time=promo_time, confirm=confirm)
    except yx_subsidy.BlacklightError as e:
        return {"executed": False, "reason": str(e)}


@mcp.tool()
def yx_subsidy_withdraw_dryrun(apply_id: str) -> dict:
    """[subsidy] 国补退出 DRY-RUN：组装 apply/quit 但**不发送**，返回 form + confirm_token。apply_id=报名编号。"""
    return yx_subsidy.withdraw_dryrun(apply_id)


@mcp.tool()
def yx_subsidy_withdraw(apply_id: str, confirm: str = "") -> dict:
    """[subsidy] **真执行**国补退出（按报名编号 applyId）。需相同 applyId 先 dry-run 拿 confirm_token 再带 confirm。"""
    try:
        return yx_subsidy.withdraw(apply_id, confirm=confirm)
    except yx_subsidy.BlacklightError as e:
        return {"executed": False, "reason": str(e)}


# --------- subsidy 批量（batch）---------
@mcp.tool()
def yx_subsidy_check_skus(block_id: str, sku_ids: list[str]) -> dict:
    """[subsidy] **批量**取多 SKU 基准价/类目 + 基本校验（逐SKU查）。返回 {skuId: {...}}。"""
    return yx_subsidy.check_skus(block_id, sku_ids)


@mcp.tool()
def yx_subsidy_apply_batch_dryrun(block_id: str, sku_ids: list[str], promo_time: str,
                                  discount: str = "", merchant: str = "京喜自营",
                                  energy_level: str = "0") -> dict:
    """[subsidy] **批量报名** DRY-RUN：多 SKU 一次 batch/create，每 SKU 自动取前台京东价，返回 confirm_token。
    ⚠️ 多SKU applyList 结构为推断，请核对 decoded 后再真执行。"""
    return yx_subsidy.apply_batch_dryrun(block_id, sku_ids, discount=(discount or None),
                                         merchant=merchant, energy_level=energy_level, promo_time=promo_time)


@mcp.tool()
def yx_subsidy_apply_batch(block_id: str, sku_ids: list[str], promo_time: str,
                           discount: str = "", merchant: str = "京喜自营",
                           energy_level: str = "0", confirm: str = "") -> dict:
    """[subsidy] **批量真执行**报名。需相同参数先 apply_batch_dryrun 拿 confirm_token 再带 confirm。单次≤50。"""
    try:
        return yx_subsidy.apply_batch(block_id, sku_ids, discount=(discount or None),
                                      merchant=merchant, energy_level=energy_level,
                                      promo_time=promo_time, confirm=confirm)
    except yx_subsidy.BlacklightError as e:
        return {"executed": False, "reason": str(e)}


@mcp.tool()
def yx_subsidy_resolve_withdrawable(sku_ids: list[str], workers: int = 4) -> dict:
    """[subsidy] ★**批量退国补前必跑**：把 SKU 解析成真正可退的 applyId 并分桶。

    毛利监控/前瞻网的**根因「国补打穿到手价」≠ 这款能退**——它看得见减免、看不见**报名归属**
    （国补可能是别人报的）。实测按根因取 31 款直接退，**只有 11 款有 applyId、20 款打空**。
    返回 {可退, 无报名, 失败, apply_ids} —— `apply_ids` 可直接喂 withdraw_batch_dryrun。
    ⚠️退出是异步：回执成功后**立即回读仍会看到记录**，等 ~120 秒才消失，别据此判失败。"""
    return yx_subsidy.resolve_withdrawable(sku_ids, workers=workers)


@mcp.tool()
def yx_subsidy_withdraw_batch_dryrun(apply_ids: list[str]) -> dict:
    """[subsidy] **批量退出** DRY-RUN：逐个 apply/quit（无原生批量接口），返回 confirm_token。"""
    return yx_subsidy.withdraw_batch_dryrun(apply_ids)


@mcp.tool()
def yx_subsidy_withdraw_batch(apply_ids: list[str], confirm: str = "") -> dict:
    """[subsidy] **批量真执行**退出：逐个 apply/quit。需相同 applyId 列表先 dry-run 拿 confirm_token 再带 confirm。单次≤50。"""
    try:
        return yx_subsidy.withdraw_batch(apply_ids, confirm=confirm)
    except yx_subsidy.BlacklightError as e:
        return {"executed": False, "reason": str(e)}


# --------- subsidy 表格报名（Excel 上传，单次≤100,000 行，大批量正道）---------
@mcp.tool()
def yx_subsidy_table_apply_dryrun(block_id: str, sku_ids: list[str], promo_time: str,
                                  merchant: str = "京喜自营", energy_level: str = "0",
                                  report_mode: int = 10) -> dict:
    """[subsidy] **表格报名** DRY-RUN：不上传，回显行数/参数 + confirm_token。单次≤10万SKU。report_mode:10非超链单店/20超链1.0/30超链2.0。"""
    return yx_subsidy.table_apply_dryrun(block_id, sku_ids, promo_time, merchant=merchant,
                                         energy_level=energy_level, report_mode=report_mode)


@mcp.tool()
def yx_subsidy_table_apply(block_id: str, sku_ids: list[str], promo_time: str,
                           merchant: str = "京喜自营", energy_level: str = "0",
                           report_mode: int = 10, confirm: str = "", wait: bool = True) -> dict:
    """
    [subsidy] **表格报名真执行**：生成 xlsx→上传 /common/fileProcess（异步导入队列）。**大批量正道，单次≤100,000 SKU**。
    每 SKU 自动取前台京东价、力度由池定。需相同参数先 dry-run 拿 confirm_token 再带 confirm。
    **wait=True 自动轮询到终态并回执自验证**（返回 result: 成功/失败数 + 失败明细直链 failUrl），别看延迟监控。
    """
    try:
        return yx_subsidy.table_apply(block_id, sku_ids, promo_time, merchant=merchant,
                                      energy_level=energy_level, report_mode=report_mode,
                                      confirm=confirm, wait=wait)
    except yx_subsidy.BlacklightError as e:
        return {"executed": False, "reason": str(e)}


@mcp.tool()
def yx_subsidy_import_progress(block_id: str, page: int = 1, page_size: int = 10) -> dict:
    """[subsidy] 轮询表格报名导入进度（/common/fileProcess/page）：各任务 fileName/successCount/failCount/failUrl(失败明细)。"""
    return yx_subsidy.file_process_page(block_id, page=page, page_size=page_size)


# --------------------- 场域：product = 商品列表 / 商品信息（sff.jd.com，只 cookie 鉴权） --------------------- #
# --------------------- 场域：markettool = 京喜营销工具（查/删券促 + 统一退出；毛利监控/定价/商品列表已移至 osw-mcp） --------------------- #
@mcp.tool()
@safe
def yx_markettool_query_sku(sku_id: str) -> dict:
    """
    [markettool] **查单 SKU 全部券促**（api.m.jd.com 京喜营销工具，补 campaign/subsidy 缺口）。
    返回券(couponList)+促(promoList)并分类：可删(本工具deleteDiscount/pm-erp) vs 活动类/不可退(需走 yx_campaign_withdraw 或人工)。
    """
    return yx_markettool.query_sku(sku_id)


@mcp.tool()
@safe
def yx_markettool_ladder(sku_id: str) -> dict:
    """★**摘券前必看**：券档位阶梯 + 补位后的真实回血上限。

    同类券**互斥、每单只生效面额最大的 1 张** ⇒ 摘掉最高档，**次高档立刻顶上**。
    2026-08-10 实证 `10163019223327`：挂着 **232 张券 / 9 档**，摘最高的 5.00 档（45 张）
    后 4.00 档顶上，**真实回血只有 1.00 元/单不是 5.00**；要到底得摘 231 张
    —— 这种深度的券池，**摘券根本不是可行手段**，该走涨价或找录券人整批退出圈选。

    每档给 `张数/共补/受保护/可摘` 与 `真实回血/单`。
    ⚠️**平台担=0 不等于可摘**：B补券 100% 自担却不能摘（平台另给团长 0.5 元/单推广补贴，
      走的是另一本账，券成本表里看不到）。禁令按 **yx 券名逐张**匹配——
      用 osw 的券列表过会放行 B补券（osw 只显示择优后生效的那张）。
    """
    return yx_markettool.ladder(sku_id)


@mcp.tool()
@safe
def yx_markettool_strip_except_plan(sku_ids: list, keep_names: list = None,
                                    only_above_kept: bool = True) -> dict:
    """★★**批量退券（保留清单语义）**：保住计划内的券，踢掉计划外的。只读。

    典型场景：一个 SKU 同时挂着**品类新**（受禁令保护、会控单亏、**计划内**）和一大堆
    冲单券/会场券/复购券（**计划外**）。同类券互斥、每单只生效面额最大的一张 ⇒
    只要有更高档的计划外券在，**品类新永远轮不上**，单亏失控。
    本工具把计划外的整批清掉，让品类新成为生效那张。

    保留 = 共补券 + 命中禁令清单的 + `keep_names` 指定的。
    ★**只摘「我担 ≥ 保留券」的部分**——比保留券低的永远赢不了，留着无害。
      实测 6 款：待摘 618 张 → **只需 172 张**，省 72% 写操作。

    三个已内置的闸（都会显式列出来，不静默）：
      · `无收益(已剔除)`：保留券已是最高档 ⇒ 摘了零回血（实测拦下 1 款白写 82 次）
      · `无保留券`：全摘会把到手价抬回裸价 ⇒ 另一个决策，默认不执行
      · `取数失败`：`query_sku` 会**静默返回空券列表**，0 张券按取数失败处理

    执行走 `yx_markettool_strip_except_dryrun` → `yx_markettool_strip_except`。
    """
    return yx_markettool.plan_strip_except(sku_ids, keep_names=keep_names,
                                           only_above_kept=only_above_kept)


@mcp.tool()
@safe
def yx_markettool_strip_except_dryrun(sku_ids: list, keep_names: list = None,
                                      only_above_kept: bool = True) -> dict:
    """批量退券 DRY-RUN：出完整计划 + confirm_token，不执行。"""
    return yx_markettool.strip_except_dryrun(sku_ids, keep_names=keep_names,
                                             only_above_kept=only_above_kept)


@mcp.tool()
@safe
def yx_markettool_strip_except(sku_ids: list, confirm: str, keep_names: list = None,
                               only_above_kept: bool = True, checkpoint: str = "") -> dict:
    """**真执行**批量退券（不可逆）。需相同参数先跑 dryrun 拿 confirm_token。

    走 `pmap_batch`（探针/ETA/熔断/断点）——单款就可能上百次写，裸跑全挂了也会"跑完"。
    `checkpoint` 给 .jsonl 路径可断点续跑。

    ⚠️执行完**必须回读** `osw_pricing_batch`，比对「我担减免」是否落到各 SKU 的
      `预计我担减免`。对不上就是补位（铁律 6），按实测重算别按线性外推。
    ⚠️摘券会**抬高到手价** ⇒ 可能触发生效中百补被平台自动删，动手前查 bybt 重叠。
    ⚠️先探针：`sku_ids` 只传 1 款跑通并回读，再批量。
    """
    return yx_markettool.strip_except(sku_ids, keep_names=keep_names,
                                      only_above_kept=only_above_kept, confirm=confirm,
                                      checkpoint=checkpoint or None)


@mcp.tool()
@safe
def yx_markettool_withdraw_plan(sku_id: str) -> dict:
    """[markettool] 单 SKU 全券促的**退出分流建议**（只读）：可删券促/活动报名促(走campaign退)/不可退(人工)三类。"""
    return yx_markettool.withdraw_plan(sku_id)


@mcp.tool()
def yx_markettool_delete_dryrun(coupons: list = None, promos: list = None, site: str = "301") -> dict:
    """[markettool] 删券/删促 DRY-RUN：不删，回显清单+confirm_token。coupons:[{skuId,campaignId}] promos:[{promoId}]。"""
    return yx_markettool.delete_dryrun(coupons or [], promos or [], site=site)


@mcp.tool()
def yx_markettool_delete(coupons: list = None, promos: list = None, site: str = "301", confirm: str = "") -> dict:
    """[markettool] **真删**券/促（营销工具，不可逆）。需相同参数先 delete_dryrun 拿 confirm_token 再带 confirm。"""
    try:
        return yx_markettool.delete(coupons or [], promos or [], site=site, confirm=confirm)
    except yx_markettool.BlacklightError as e:
        return {"executed": False, "reason": str(e)}


@mcp.tool()
@safe
def yx_promo_withdraw_dryrun(sku_id: str) -> dict:
    """
    [统一退出] 单 SKU 全券促的**一键退出计划**（DRY-RUN，不执行）：自动分流并**把活动名对到 campaignId**——
    营销工具删可删券促、退出活动报名(含 canOp=0 券)。返回 plan + confirm_token；unresolved=没对到活动ID的需你补。
    """
    return yx_markettool.withdraw_all_dryrun(sku_id)


@mcp.tool()
def yx_promo_withdraw(sku_id: str, confirm: str = "") -> dict:
    """
    [统一退出] **真执行**：营销工具删可删券促 + 逐活动退出报名（operateList 逐条可退，不可退跳过带原因）。
    需相同 sku 先 promo_withdraw_dryrun 拿 confirm_token 再带 confirm。unresolved 活动需补 campaignId 单独退。
    """
    try:
        return yx_markettool.withdraw_all(sku_id, confirm=confirm)
    except yx_markettool.BlacklightError as e:
        return {"executed": False, "reason": str(e)}


# --------------------- 场域：bybt（超级补贴/原百亿补贴，竞价，只查询） --------------------- #
@mcp.tool()
@safe
def yx_bybt_list_eligible(area_id: int = 319901, page: int = 1, page_size: int = 200, sku_name: str = "") -> dict:
    """[超级补贴/原百亿补贴] **可报商品列表**（竞价活动，网关 bid-activity.jd.com）。返回 skuId/建议价/京东价/类目/可报状态。sku_name 按标题模糊搜。area_id 来自活动页URL(默认319901)。"""
    return yx_bybt.list_eligible(area_id, page=page, page_size=page_size, sku_name=sku_name)


@mcp.tool()
@safe
def yx_bybt_get_applied(activity_id: str, area_id: int = 319901, page: int = 1, page_size: int = 100) -> dict:
    """[超级补贴] **已报名列表**（promoCreateStatus 1审核中/2生效中）。需 activity_id（来自活动页URL的 activityId）。"""
    return yx_bybt.get_applied(activity_id, area_id, page=page, page_size=page_size)


@mcp.tool()
@safe
def yx_bybt_price_info(sku_id: str, area_id: int = 319901, bid_price: float = None, bidding_id: int = -2) -> dict:
    """[超级补贴] **报名价试算/券信息**。bidding_id=-2 取券/满减；给活动 biddingId + bid_price 试算到手价（竞价定价用）。"""
    return yx_bybt.price_info(sku_id, area_id, bid_price=bid_price, bidding_id=bidding_id)


@mcp.tool()
@safe
def yx_bybt_ware_detail(sku_ids: list, area_id: int = 319901, bidding_type: int = 1) -> dict:
    """[超级补贴] 取可报SKU的**报名元数据**（/apply/bid/new/ware/detail/list）——biddingId/bidBatchId + skuExtendInfo 来源（报名 saveApply 必需）。"""
    dl = yx_bybt.ware_detail(sku_ids, area_id, bidding_type)
    return {"count": len(dl), "items": [{"skuId": d.get("skuId"), "biddingId": d.get("biddingId"),
            "bidBatchId": d.get("bidBatchId"), "bindStatus": d.get("bindStatus"),
            "name": d.get("name")} for d in dl]}


@mcp.tool()
@safe
def yx_bybt_find_applied(activity_id: str, sku_id: str, area_id: int = 319901) -> dict:
    """[超级补贴] 在已报名里按 skuId 找报名记录，取退出所需 applyId/applyWareId/biddingType/bidPrice。"""
    r = yx_bybt.find_applied(activity_id, sku_id, area_id)
    return r or {"found": False, "note": "该 SKU 不在已报名列表"}


@mcp.tool()
@safe
def yx_bybt_verify_enrolled(activity_id: str, sku_ids: list, area_id: int = 319901,
                            with_reasons: bool = True) -> dict:
    """[超级补贴] ★**T+1 回执**：昨天报的这批，今天到底活着几个 + 掉的为什么掉。

    `enroll_bulk` 的 `verified.newly_reported` 只验到**「占坑」**，答不了「跑起来没有」——
    百补 **报名 ≠ 生效**（实测占坑 400 / 真生效 107），且 **24h 未出单会被自动下线**。

    三分桶：`生效中`（真在跑）/ `占坑未生效`（审核中·竞价中，再等）/ `已掉出`（驳回·退出·过期）。
    `with_reasons=True` 给「已掉出」补**驳回原因**（唯一来源是 flow_log，`get_applied` 里那两个
    reason 字段对审核驳回都是空的）。

    ⚠️`已掉出` ≠ 永久失败：未中标/未建促销的会**释放回可报池**，隔几天重报常能成。
    ⚠️自带状态漂移哨兵：看 `哨兵_状态漂移` 里的 **`未知activityStatus占比%`** /
      **`未知promoCreateStatus占比%`**（不是 `unknown_ratio`——那个键不存在，
      按不存在的键取值会读到 None 而被当成"无漂移"，正好读反）。偏高时别急着据此重报。

    实证 2026-08-13：当天报 71 款、提交回执 71/71 全成功，**1 小时后生效仅 19（26.8%）、
    被驳回 32**——原因 22 条「商详主图规格标注」+ 8 条「不支持随机款」+ 2 条价格，
    **94% 跟定价无关**，提交时的回执完全看不到。
    """
    return yx_bybt.verify_enrolled(activity_id, sku_ids, area_id=area_id,
                                   with_reasons=with_reasons)


@mcp.tool()
def yx_bybt_withdraw_dryrun(apply_id: int, apply_ware_id: int, area_id: int = 319901, bidding_type: int = 1) -> dict:
    """[超级补贴] **退出(申请退出) DRY-RUN**：组装 appeal/save(type:4) 但不发送，返回 confirm_token。apply_id/apply_ware_id 来自 find_applied/get_applied。"""
    return yx_bybt.withdraw_dryrun(apply_id, apply_ware_id, area_id, bidding_type)


@mcp.tool()
def yx_bybt_withdraw(apply_id: int, apply_ware_id: int, area_id: int = 319901, bidding_type: int = 1, confirm: str = "") -> dict:
    """[超级补贴] **退出真执行(申请退出)**：需相同参数先 withdraw_dryrun 拿 confirm_token 再带 confirm。"""
    try:
        return yx_bybt.withdraw(apply_id, apply_ware_id, area_id, bidding_type, confirm=confirm)
    except yx_bybt.BlacklightError as e:
        return {"withdrawn": False, "reason": str(e)}


@mcp.tool()
@safe
def yx_bybt_apply_dryrun(sku_id: int, bid_price: float, activity_id: str, resource_id: str = None,
                         form_order: list = None, roles: dict = None, area_id: int = 319901,
                         jd_price: float = None, bid_num: int = 50000) -> dict:
    """[超级补贴] **竞价报名 DRY-RUN**（saveApply 组装，**不发送**）。组装 body → 齐全则跑 applyRemind 活体预检硬校验，过才发 confirm_token + 回显完整 saveApply body。
    form_order/roles/resource_id 缺则按 activity_id 从 config bybt.forms 自动取；bid_price 来自 yx_bybt_price_info 定价；biddingId 需>0（SKU 已绑竞价房间）。missing 非空/预检不过 → 不发 token。"""
    return yx_bybt.apply_dryrun(sku_id, bid_price, activity_id, resource_id, form_order, roles,
                                area_id=area_id, jd_price=jd_price, bid_num=bid_num)


@mcp.tool()
def yx_bybt_apply(sku_id: int, bid_price: float, activity_id: str, resource_id: str = None,
                  form_order: list = None, roles: dict = None, area_id: int = 319901,
                  jd_price: float = None, bid_num: int = 50000, confirm: str = "") -> dict:
    """[超级补贴] **竞价报名真执行**（saveApply，真金白银）。需相同参数先 yx_bybt_apply_dryrun 拿 confirm_token 再带 confirm。missing 非空拒发。"""
    try:
        return yx_bybt.apply(sku_id, bid_price, activity_id, resource_id, form_order, roles,
                             area_id=area_id, jd_price=jd_price, bid_num=bid_num, confirm=confirm)
    except yx_bybt.BlacklightError as e:
        return {"applied": False, "reason": str(e)}


@mcp.tool()
def yx_bybt_withdraw_batch_dryrun(records: list, area_id: int = 319901) -> dict:
    """[超级补贴] **批量退出 DRY-RUN**（逐个 appeal/save type:4，不发送）。records=[{applyId,applyWareId,biddingType?}]（来自 yx_bybt_get_applied/find_applied）。返回 confirm_token。"""
    try:
        return yx_bybt.withdraw_batch_dryrun(records, area_id)
    except yx_bybt.BlacklightError as e:
        return {"would_withdraw": False, "reason": str(e)}


@mcp.tool()
def yx_bybt_withdraw_batch(records: list, area_id: int = 319901, confirm: str = "") -> dict:
    """[超级补贴] **批量退出真执行**（逐个申请退出）。需相同 records 先 yx_bybt_withdraw_batch_dryrun 拿 confirm_token 再带 confirm。逐条回报成败。"""
    try:
        return yx_bybt.withdraw_batch(records, area_id, confirm=confirm)
    except yx_bybt.BlacklightError as e:
        return {"executed": False, "reason": str(e)}


@mcp.tool()
@safe
def yx_bybt_own_eligible(area_id: int = 319901, saler: str = None) -> dict:
    """[超级补贴] 可报清单里**只属于本人**(ware_detail.saler==当前登录erp)的 SKU——可报清单混整店多采销的货，报名前必先按归属过滤别误碰别人。返回 {items, by_saler(各采销占比), owned_count, total_eligible}。"""
    return yx_bybt.own_eligible(area_id, saler)


@mcp.tool()
@safe
def yx_bybt_plan_bulk_enroll(area_id: int = 319901, saler: str = None, target_margin: float = 0.05,
                             limit: int = None, skus: list = None, activity_id: str = None,
                             concurrency: int = 8) -> dict:
    """[超级补贴] ★**批量报名一键规划**（只读、零写）：筛本人(saler)→剔已报/已超补(传activity_id用已报名列表权威去重,补监控延迟)→**并发**真试算定价(到手价**严格<建议价**·保 target_margin 毛利)→分桶。返回 {A_biddable(可报名+报名价), review_platform(平台券待京喜口径复核), C_infeasible(建议价太低), skipped_enrolled(已报/已超补), errors, summary}。skus 指定则只算这些(仍校归属)。concurrency=并发试算线程数(默认8·上限16)。全量不传 skus 会扫全店 ware_detail(重,建议后台跑或传 limit)。"""
    return yx_bybt.plan_bulk_enroll(area_id, saler, target_margin, limit, skus, activity_id, concurrency=concurrency)


@mcp.tool()
def yx_bybt_enroll_bulk_dryrun(rows: list, activity_id: str, resource_id: str = None, area_id: int = 319901) -> dict:
    """[超级补贴] 批量报名 DRY-RUN。rows=[{skuId,bidPrice,jdPrice?}]（一般直接用 yx_bybt_plan_bulk_enroll 的 A_biddable）。回显待报清单 + confirm_token。"""
    return yx_bybt.enroll_bulk_dryrun(rows, activity_id, resource_id, area_id)


@mcp.tool()
def yx_bybt_enroll_bulk(rows: list, activity_id: str, resource_id: str = None, area_id: int = 319901, confirm: str = "", concurrency: int = 1, min_interval: float = 4.0, retry_rounds: int = 2) -> dict:
    """[超级补贴] ★**批量报名真执行·串行限速提交**——逐条 apply 真报(各带独立回执/卡控明细·一条失败不拖累其他)。rows=[{skuId,bidPrice,jdPrice?}]。需相同 rows 先 yx_bybt_enroll_bulk_dryrun 拿 confirm。
★★**别调 concurrency 想提速**：卡控「正在报名中，无需重复点击」按账号串行判定，真正的不变量是**两次提交的间隔**(`min_interval` 秒)。2026-08-19 从 2307 条审计定死：中位间隔 ≥4.0s 撞 0%(9天573次)、≤2.0s 撞 35~44%(6天479次)，零例外。`concurrency=1` 只是间隔在某台机器上的代理——机器越快间隔越小，换台机器就失效，所以护栏写在 `min_interval` 上。
`retry_rounds` 每轮只重投 **verify 证实没报上** 的(前后已报名集合差集，权威)，零重复报名风险。返回 {total, success_receipt, min_interval, rounds, results, verified}。**判成败看 verified.newly_reported 别看 success_receipt**。"""
    try:
        return yx_bybt.enroll_bulk(rows, activity_id, resource_id, area_id, confirm=confirm, concurrency=concurrency,
                                   min_interval=min_interval, retry_rounds=retry_rounds)
    except yx_bybt.BlacklightError as e:
        return {"executed": False, "reason": str(e)}


# --------------------- 场域：ms（秒杀/便宜包邮/特价，oac.jd.com：查/报名(逐SKU+表格)/退出/导出/定价） --------------------- #
@mcp.tool()
@safe
def yx_ms_get_applied(activity_id: str, area_id: int, apply_sku_type: str = "3",
                      current_status: str = "", page: int = 1, page_size: int = 100,
                      sku_id: str = "", begin_time: str = None, end_time: str = None) -> dict:
    """[秒杀] **已报名秒杀商品**（oac.jd.com）。apply_sku_type: 3京喜自营(默认)/2京东自营；current_status ''全部/'6'失效预警。
    **sku_id**：传了就走服务端过滤(已报名可达数万条，查单SKU务必传，别全量翻页)。返回 applyId/skuId/beginTime场次时间/当前价/采购价/到手价上限。activity_id/area_id 来自报名页URL。"""
    return yx_ms.get_applied(activity_id, area_id, apply_sku_type, current_status, page, page_size,
                             sku_id=sku_id, begin_time=begin_time, end_time=end_time)


@mcp.tool()
@safe
def yx_ms_invalidation_warnings(activity_id: str, area_id: int, sales_erps: str = None,
                                apply_sku_type: str = "3", with_margin: bool = True,
                                min_margin: float = 0.02,
                                page_size: int = 100, max_pages: int = 50) -> dict:
    """[秒杀] ★**活动失效预警**（已报名管理→「活动失效预警」= currentStatus 6）。报上名≠会生效：
    平台在**场次开始前**查价，不达标就到点失效（报了等于没报还占坑）。规则=秒杀到手价须 ≤ 活动后到手价−0.01。
    ⚠️**别用 priceWarning 字段**（实测 39/39 都不是目标价）；权威是 reducePriceWarningCMSPrice，本工具的「需降至」。
    ⚠️**本接口不按人筛**（实测混 3 个 ERP）⇒ **`sales_erps` 必传，留空直接报错**。
      本工具的输出直接喂 `yx_ms_reduce_price`（真改价），漏传就会**改到别人的商品**。
      光在文档写"务必传"挡不住漏传，所以 fail-closed。确实要看全部人传 `sales_erps="*"`。"""
    se = (sales_erps or "").strip()
    if not se:
        raise BlacklightError(
            "sales_erps 必传：本接口**不按人筛**（实测混 3 个 ERP），"
            "而输出直接喂 reduce_price 改价 —— 漏传就是改别人的商品。"
            "确实要看全部人请显式传 sales_erps='*'。")
    return yx_ms.invalidation_warnings(activity_id=activity_id, area_id=area_id,
                                       sales_erps=("" if se == "*" else se),
                                       apply_sku_type=apply_sku_type,
                                       with_margin=with_margin, min_margin=min_margin,
                                       page_size=page_size, max_pages=max_pages)


@mcp.tool()
@safe
def yx_ms_solve_warned_price(sku_id: str, area_id: int, batch_id: int, cap: float,
                             cur_promo: float) -> dict:
    """[秒杀] 解「到手价 ≤ cap 的**最高**促销价」，couponInfo 平台真值二分。cap 取失效预警的「需降至」。
    ★**别用 −0.01 直觉**：斜率不是 1（券促多为折扣型），实测 22.90→22.89 到手价纹丝不动，22.88 才降 0.01；32 款里多数需降 0.02。
    ★也别用 yx_ms_solve_price：那是京喜/客户口径，与失效预警的「单件普惠到手价」是两回事。"""
    return yx_ms.solve_warned_price(sku_id, area_id, batch_id, cap, cur_promo)


@mcp.tool()
@safe
def yx_ms_reduce_price_dryrun(rows: list, area_id: int) -> dict:
    """[秒杀] 发起降价 DRY-RUN（不发送，返回 confirm_token）。rows 每项需 applyId/promoPrice/purchasePrice。"""
    return yx_ms.reduce_price_dryrun(rows, area_id)


@mcp.tool()
@safe
def yx_ms_reduce_price(rows: list, area_id: int, confirm: str, concurrency: int = 4) -> dict:
    """[秒杀] **发起降价**（失效预警的处置动作）。需 dryrun 的 confirm_token。
    ⚠️平台语义：创建同数量新促销、成功后删除原促销，且**审核通过才生效**——提交成功≠生效。
    ★★**回读走 `yx_ms_verify_reduced`，不是预警桶**（2026-08-24 实测 24 款）：降价=新建促销、
      原促销随后才被删，刚发完时预警桶里挂的还是**旧行**（applyId/促销价/预警时间都没变）。
      实测发完仍 22 款在桶里、10 分钟后剩 3、半小时还是那 3，而那 3 款查已报名是**每款两行**：
      新行 applyStatus=2 已通过、旧行 18/19 待清 ⇒ **24/24 其实全落地**。
      拿预警桶判会报"3 款失败"再去重复降价（又建一条促销）。"""
    return yx_ms.reduce_price(rows=rows, area_id=area_id, confirm=confirm, concurrency=concurrency)


@mcp.tool()
@safe
def yx_ms_find_applied(activity_id: str, sku_id: str, area_id: int, apply_sku_type: str = "3") -> dict:
    """[秒杀] 按 skuId 在已报名里找**首条**记录，取退出所需 applyId + 采购价/到手价上限。走服务端过滤(不全量翻页)。要跨场次全部记录/按日期筛用 yx_ms_applied_by_skus。"""
    r = yx_ms.find_applied(activity_id, area_id, sku_id, apply_sku_type)
    return r or {"found": False, "note": "该 SKU 不在已报名列表"}


@mcp.tool()
@safe
def yx_ms_applied_by_skus(activity_id: str, area_id: int, sku_ids: list,
                          dates: list = None, apply_sku_type: str = "3",
                          via: str = "auto", export_threshold: int = 5) -> dict:
    """[秒杀/便宜包邮/特价] **查一批SKU在(可选:指定日期)场次有没有报名**——「这几个SKU报没报、要退拿哪些applyId」一步到位。

    **两条路自动选**（`via='auto'`）：`page`=服务端skuId过滤+并发翻页(少量最快)；
    `export`=触发已报名导出一次拿全(固定~10~30s，**与SKU数无关**)。auto: >=`export_threshold`(默认5) 走 export。
    ★**别一次传一大把走 page**：2026-08-20 实测同样 8 个 SKU 整批超时两次、拆 4+4 两次秒回——
      并发翻页会把平台打出「拼命加载中」再叠退避，顶穿 120s。auto 已经帮你绕开。

    ⚠️**两条路状态字段口径不同**：page 给数字 `applyStatus`(2通过/3驳回/9退出)；
      export 给中文 `auditStatusText`/`promoStatusText` 且 **`applyStatus` 恒 None**（不做没实证过的数字映射）。
      看返回里的 `via` 确认走了哪条，export 路另带 `_warn`。**要数字态就显式 `via='page'`**。
      export 路的补偿是多给翻页接口没有的字段：秒杀=**审核进度/驳回原因/价格预警/秒杀到手价/短标题**；
      便宜包邮·特价=**报名人 ERP/促销生效状态/审核状态**。

    dates 形如 ['2026-07-17','2026-07-18']（None=全部场次）。
    返回 by_sku{sku:[记录]}, matched[命中记录], apply_ids[可直接喂 yx_ms_withdraw_batch_dryrun/withdraw_batch], no_apply[无报名的sku], via。
    典型链路：本工具查→apply_ids→yx_ms_withdraw_batch_dryrun 拿token→yx_ms_withdraw_batch 退。"""
    return yx_ms.applied_by_skus(activity_id, area_id, sku_ids, dates, apply_sku_type,
                                 via=via, export_threshold=export_threshold)


@mcp.tool()
@safe
def yx_ms_fetch_applied_export(area_id: int, activity_id: str = "", trigger: bool = False,
                               wait_timeout: int = 120, apply_sku_type: str = "3",
                               allow_stale: bool = False,
                               begin_time: str = None, end_time: str = None) -> dict:
    """[秒杀/便宜包邮/特价] **取「已报名」全量导出**（zip→xls 解析成行）。`trigger=True` 先触发一份新的（需 activity_id）。

    ★★**`trigger=True` 必须带 `begin_time`/`end_time`**（按**场次开始时间**筛，形如 '2026-08-27 00:00:00'）：
      不带就是全活动，该活动 137716 条 > 平台 **50000 行上限** ⇒ 任务必 status=2 无文件、且不会自愈。
      实测：全活动 137716 ❌ / 未来30天 6492 ✅ / **单场次 713 ✅ 一次就成**。先用 applied_export_feasible 探量。
    ★**比 get_applied 翻页强**：一次拿全；get_applied 深翻页会**静默丢页**（实证 8099 只取回 4837）。
      ⚠️**列名按频道不同**：秒杀=skuId/促销开始时间/审核进度/促销状态（**无「报名人」**，另有驳回原因/价格预警/短标题）；
      便宜包邮/特价=报名SKU/促销生效时间/审核状态/促销生效状态（**有「报名人」**）。工具已双取，别自己按一套读。
    ⚠️`trigger=True` 只认**触发之后产出的新任务**：新任务失败(status=2)或超时都**直接抛错，绝不退回旧导出**
      （旧文件可能是几个月前的，据此判重会漏报/重复报名）。确需旧文件显式 `allow_stale=True`。
    列（2026-08-24 实测）：秒杀=报名编号/商品信息/skuId/spuId/短标题/促销价格/秒杀到手价/价格预警/
    活动时长/审核进度/驳回原因/促销开始时间/促销状态/…；便宜包邮·特价=报名编号/报名人/报名SKU/商品名称/
    类目信息/SPU ID/促销价格/促销数量/促销库存/促销生效时间/促销生效状态/报名时间/审核状态。
    返回 {rows, columns, count, fileName, taskId, path}。"""
    return yx_ms.fetch_applied_export(area_id, activity_id, trigger=trigger,
                                      wait_timeout=wait_timeout, allow_stale=allow_stale,
                                      apply_sku_type=apply_sku_type,
                                      begin_time=begin_time, end_time=end_time)


@mcp.tool()
def yx_ms_withdraw_dryrun(apply_id: str, area_id: int) -> dict:
    """[秒杀] **退出 DRY-RUN**：组装 quit(id,areaId) 但不发送，返回 confirm_token。apply_id 来自 find_applied/get_applied。"""
    return yx_ms.withdraw_dryrun(apply_id, area_id)


@mcp.tool()
def yx_ms_withdraw(apply_id: str, area_id: int, confirm: str = "") -> dict:
    """[秒杀] **退出真执行**：需相同参数先 withdraw_dryrun 拿 confirm_token 再带 confirm。"""
    try:
        return yx_ms.withdraw(apply_id, area_id, confirm=confirm)
    except yx_ms.BlacklightError as e:
        return {"withdrawn": False, "reason": str(e)}


@mcp.tool()
def yx_ms_withdraw_batch_dryrun(apply_ids: list, area_id: int) -> dict:
    """[便宜包邮/秒杀/特价] **批量退出 DRY-RUN**（逐个 openness/quit，不发送）。apply_ids 来自 get_applied/find_applied。返回 confirm_token。"""
    try:
        return yx_ms.withdraw_batch_dryrun(apply_ids, area_id)
    except yx_ms.BlacklightError as e:
        return {"would_withdraw": False, "reason": str(e)}


@mcp.tool()
def yx_ms_withdraw_batch(apply_ids: list, area_id: int, confirm: str = "") -> dict:
    """[便宜包邮/秒杀/特价] **批量退出真执行**（逐个退出）。需相同 apply_ids 先 yx_ms_withdraw_batch_dryrun 拿 confirm_token 再带 confirm。逐条回报成败。"""
    try:
        return yx_ms.withdraw_batch(apply_ids, area_id, confirm=confirm)
    except yx_ms.BlacklightError as e:
        return {"executed": False, "reason": str(e)}


@mcp.tool()
@safe
def yx_ms_solve_price(sku_id: str, target_margin: float, cap: float = None,
                      channel: str = "ms", objective: str = "max_margin") -> dict:
    """
    [秒杀/便宜包邮/特价] **单 SKU 解建议报名价(=促销价)**（本地引擎反解，京喜承担口径算毛利）。
    这三频道报名价=直接设的促销价，同套引擎。cap=客户到手价上限(秒杀=maxActualPrice；便宜包邮/特价=池门槛价，缺则 max_margin 取报名价=京东价)。
    objective: max_margin(撞上限最大毛利) / min_price(压到保毛利线最低价，"便宜/特价"场更合适)。
    """
    return yx_ms.solve_price(sku_id, target_margin, cap=cap, channel=channel, objective=objective)


@mcp.tool()
@safe
def yx_ms_price_applied(activity_id: str, area_id: int, target_margin: float,
                        apply_sku_type: str = "3", objective: str = "max_margin",
                        limit: int = 50) -> dict:
    """
    [秒杀/便宜包邮/特价] **对某池已报名 SKU 批量解建议报名价**（cap 取各自 maxActualPrice 到手价上限）。
    返回每 SKU：当前促销价/京喜盈亏 + 建议报名价/预测京喜到手价/毛利。activity_id/area_id 来自池(见 config.baoyou.pools)。
    """
    return yx_ms.price_applied(activity_id, area_id, target_margin,
                               apply_sku_type=apply_sku_type, objective=objective, limit=limit)


@mcp.tool()
@safe
def yx_ms_apply_dryrun(sku_id: str, area_id: int, batch_id: int, promo_price: float,
                       duration: str = "30", stock: int = 5000, jd_price: float = None) -> dict:
    """
    [秒杀/便宜包邮/特价] **报名 DRY-RUN**（saveApply 组装，不发送）。全自动抓 skuExtendInfo+白底图+短标题+numeric cid。
    promo_price=定价器解出的报名价(促销价)。返回 body + missing + confirm_token。missing 非空(缺字段)时别真报。
    """
    return yx_ms.apply_dryrun(sku_id, area_id, batch_id, promo_price, duration=duration,
                              stock=stock, jd_price=jd_price)


@mcp.tool()
@safe
def yx_ms_apply(sku_id: str, area_id: int, batch_id: int, promo_price: float, confirm: str,
                duration: str = "30", stock: int = 5000, jd_price: float = None) -> dict:
    """
    [秒杀/便宜包邮/特价] **报名真执行**（saveApply，真金白银）。需相同参数先 apply_dryrun 拿 confirm_token 再带 confirm。
    白底图/字段自动补全；missing 非空拒发。成功返回报名ID。
    ⚠️**此逐SKU saveApply 路径代码齐但未活体真报验证**——批量优先用已验证的 `yx_ms_table_apply`；必用此路径先小批(1-2个)试报确认。
    """
    return yx_ms.apply(sku_id, area_id, batch_id, promo_price, white_img=None, duration=duration,
                       stock=stock, jd_price=jd_price, confirm=confirm)


@mcp.tool()
@safe
def yx_ms_get_batch_id(area_id: int, begin_time: str, register_mode=None,
                       end_time: str = None) -> dict:
    """[秒杀] 取场次 batchId（getBatchId）。begin_time='YYYY-MM-DD HH:00:00'(日期+场次整点)。秒杀报名场次窗口制。
    `end_time`/`register_mode`：便宜包邮等按区间取批次的池要用（2026-08-24 补透传，之前只能取秒杀那种整点场次）。"""
    return yx_ms.get_batch_id(area_id, begin_time, register_mode=register_mode, end_time=end_time)


@mcp.tool()
@safe
def yx_ms_get_rule_id(area_id: int, activity_id: str) -> dict:
    """[秒杀/openness] 取活动 **ruleId**（area/detail）——list_eligible/price/export 必需，但报名页 URL 不含它，免浏览器抓包。返回 {ruleId}。"""
    return {"ruleId": yx_ms.get_rule_id(area_id, activity_id)}


@mcp.tool()
@safe
def yx_ms_plan_seckill_enroll(area_id: int, activity_id: str, begin_time: str, rule_id: str = None,
                              target_margin: float = 0.05, duration: int = 28, limit: int = None,
                              skus: list = None, concurrency: int = 4,
                              sales_erps: str = None, erp_assistant: str = None,
                              checkpoint: str = None) -> dict:
    """[秒杀] ★**批量报名一键规划**（只读、零写，复用百补真值方法）：本人可报(list_eligible服务端按ERP过滤·可报=未报内建去重)→导出取门槛→**并发** couponInfo 平台真值定价(保 target_margin·到手价≤门槛)→分桶。begin_time='YYYY-MM-DD HH:00:00'(场次,自动换batchId·须≥T+3)；rule_id 缺自动取；concurrency=并发试算线程(**默认4**·上限16,导出固定开销不并行)。
    ★**逐SKU二分是唯一的N倍扇出**(门槛已由导出一次拿全、成本已批量取)，走 pmap_batch：**探针/ETA/熔断/断点**。
      并发从8降到4——8 并发会把平台打出「拼命加载中」再叠退避，**整批超时=0产出**。
      给 `checkpoint`(.jsonl路径) 可断点续跑：超时/熔断后原样重调即跳过已完成的。
      返回里的 `跑批` 带 已完成/断点跳过/错误/熔断/耗时/ETA/进度。
    返回 {A_biddable(可报名+报名价), review_platform, C_infeasible, batchId, 跑批, summary}。报名走 yx_ms_table_apply(channel='seckill') 把 A_biddable 转 rows。

    ★**`sales_erps` 必传，留空直接报错**（2026-08-13 补 + 代码评审加固）：
      不传=按活动全量规划，实测 **4601 款 vs 本人 909 款** ⇒ **会报成别人的商品**。
      本函数**输出直接喂写操作**（table_apply），所以这里 fail-closed：
      光"文档写必传"挡不住漏传——那正是 [[fix-the-caller-not-just-the-function]]
      说的「新增一个『记得传』的参数 ≠ 把危险默认值改安全」。
      确实要全量时显式传 `sales_erps="*"`。
      `erp_assistant`(采销助理) 范围宽、用来对报名页总数；`sales_erps`(销售员) 才是「我自己的」。
    """
    se = (sales_erps or "").strip()
    if not se:
        raise BlacklightError(
            "sales_erps 必传（销售员ERP=「我自己的」）。留空会按活动**全量**规划——"
            "实测 4601 款 vs 本人 909 款，报下去就是别人的商品。"
            "确实要全量请显式传 sales_erps='*'。")
    return yx_ms.plan_seckill_enroll(area_id, activity_id, begin_time, rule_id=rule_id,
                                     target_margin=target_margin, duration=duration, limit=limit,
                                     skus=skus, concurrency=concurrency,
                                     sales_erps=("" if se == "*" else se),
                                     erp_assistant=erp_assistant, checkpoint=checkpoint)


@mcp.tool()
@safe
def yx_ms_plan_baoyou_enroll(area_id: int, activity_id: str, begin_time: str, rule_id: str = None,
                             target_margin: float = 0.05, duration: int = 30, channel: str = "baoyou",
                             limit: int = None, skus: list = None, concurrency: int = 4,
                             checkpoint: str = None) -> dict:
    """[便宜包邮/特价] ★**批量报名一键规划**（只读、零写，与秒杀同套 couponInfo 二分真值定价）：本人可报(ERP过滤·可报=未报)→**并发** couponInfo 平台真值定价(保 target_margin)→分桶。**报名价约束=`threshold`(报名价上限,直接给·无需导出;≠秒杀的到手价门槛)**；platform 没给上限的行会带 `警告` 字段(该行未施加上限约束)，summary 有计数。channel: baoyou/tejia；duration 按天(默认30)；rule_id 缺自动取；concurrency **默认4**(8并发会被平台限流叠退避,整批超时=0产出)。★逐SKU二分走 pmap_batch(探针/ETA/熔断/断点)，给 `checkpoint`(.jsonl) 可续跑；返回里 `跑批` 带进度与ETA。返回 {A_biddable(可报名+报名价), review_platform, C_infeasible, batchId, summary}。报名走 yx_ms_table_apply(channel='baoyou'/'tejia')。"""
    return yx_ms.plan_baoyou_enroll(area_id, activity_id, begin_time, rule_id=rule_id,
                                    target_margin=target_margin, duration=duration, channel=channel,
                                    limit=limit, skus=skus, concurrency=concurrency,
                                    checkpoint=checkpoint)


@mcp.tool()
@safe
def yx_ms_list_eligible(batch_id: int, area_id: int, rule_id: str, erp_assistant: str = None,
                        sales_erps: str = None, page: int = 1, page_size: int = 50,
                        business_type: int = 122, activity_duration: str = "30") -> dict:
    """
    [秒杀/便宜包邮/特价] **可报商品列表**（pagingQuerySkuList4Http）。返回 skuId/pPrice/**threshold(报名价上限,规范键)**/thresholdField/已报状态。
    ⚠️门槛字段名**按池不同**（秒杀池=suggestPrice，便宜包邮池=opennessMinPrice，2026-08-06 实测）——一律读 `threshold`，别读原始字段名。
    batch_id/rule_id 每池特有；**erp_assistant/sales_erps 留空默认当前登录用户**（多用户友好，每人查自己范围）。
    ⚠️★**business_type 默认 122 = 秒杀**（2026-08-24 审查补透传）：库里明写「秒杀=122，便宜包邮/特价各不同」，
      而本工具自称三池通用 —— 此前这个参数被写死，对包邮/特价池会**静默返回错的或空的可报清单**
      （与「少发一条 AND 条件把 21921 打成 3672」是同一形状）。查非秒杀池按该池实际值传。
    ⚠️activity_duration：秒杀实为 28（小时），便宜包邮/特价是 30（天）—— 同样别拿默认值蒙。
    """
    return yx_ms.list_eligible(batch_id, area_id, rule_id, erp_assistant, sales_erps,
                               page=page, page_size=page_size,
                               business_type=business_type, activity_duration=activity_duration)


@mcp.tool()
@safe
def yx_ms_price_eligible(batch_id: int, area_id: int, rule_id: str, target_margin: float = 0.10,
                         erp_assistant: str = None, sales_erps: str = None, objective: str = "max_margin",
                         channel: str = "baoyou", page: int = 1, page_size: int = 30,
                         limit: int = 30, concurrency: int = 8) -> dict:
    """
    [便宜包邮/特价] **对可报(未报名)SKU **并发**批量解建议报名价**（报名前决策）。opennessMinPrice 当**报名价上限(门槛)**。
    ⚠️秒杀请用 yx_ms_plan_seckill_enroll(couponInfo 平台真值)，此函数(本地模型)留给便宜包邮/特价。concurrency=并发试算线程(默认8·上限16)。
    """
    return yx_ms.price_eligible(batch_id, area_id, rule_id, erp_assistant, sales_erps,
                                target_margin, objective=objective, channel=channel,
                                page=page, page_size=page_size, limit=limit, concurrency=concurrency)


@mcp.tool()
@safe
def yx_ms_export_eligible(area_id: int, activity_id: str, batch_id: int, rule_id: str,
                          erp_assistant: str = None, sales_erps: str = None, activity_duration: int = 28,
                          business_type: int = 122) -> dict:
    """[秒杀/便宜包邮/特价] **触发导出「可提报清单」**（整份可报 SKU + 报名价格门槛，异步，**约9分钟**非1~2分钟）。
    取结果用 yx_ms_export_fetch，**必须带 exclude_ids/after**否则会静默拿到旧导出。取秒杀门槛直接用 seckill_thresholds（已内建认领+续等+缓存）。
    business_type 秒杀=122。**erp 留空默认当前登录用户**。"""
    return yx_ms.export_eligible(area_id, activity_id, batch_id, rule_id, erp_assistant,
                                 sales_erps, activity_duration=activity_duration,
                                 business_type=business_type)


@mcp.tool()
@safe
def yx_ms_export_fetch(area_id: int, download: bool = True, out_path: str = None,
                       with_rows: bool = False, sample: int = 5,
                       after: str = None, exclude_ids: list = None,
                       min_progress: int = 100) -> dict:
    """[秒杀/便宜包邮/特价] 取**最新已完成**的导出任务，下载并解析可报清单。秒杀=.xlsx(含**报名价格门槛**列)；便宜包邮/特价=.zip(内含老式.xls,列 sku ID/京东价/**建议价**/近30天最低价)——**建议价即报名价格上限门槛**(用户确认)，故返回的 `门槛` 便宜包邮/特价=建议价。促销价须≤门槛。自动解压+读 .xls/.xlsx/.csv。没完成会提示稍后再试。

    ⚠️★★**刚触发了新导出就必须带 after 或 exclude_ids**（2026-08-24 审查补透传）：本工具返回的是
      「最新**已完成**」的任务，你刚触发的那个通常还在跑 ⇒ 它会**静默返回上一次的旧导出**，
      文件名/行数都正常、看不出异常。实证（2026-08-04）：为 08-07 场触发后立刻取，拿到 08-06 那份，
      据此报了 890 条、119 条被拒，还**漏评估了约 620 个当天真正可报的 SKU**。
      用法：先 yx_ms_export_list 记下现有 id → 触发 → 轮询本工具并传 exclude_ids=[那些 id]
      （或 after="YYYY-MM-DD HH:MM:SS"）。取秒杀门槛直接用 seckill_thresholds（已内建认领）。"""
    from blacklight.core.mcpio import cap_rows
    r = yx_ms.export_fetch(area_id, download=download, out_path=out_path,
                           after=after, exclude_ids=exclude_ids, min_progress=min_progress)
    # 体积保护收编进 core.mcpio（本条就是那 435KB 卡死 20 分钟的当事工具）
    return cap_rows(r, with_rows=with_rows, sample=sample,
                    where="全量在返回的 parsed_from/file 落盘文件里，或传 with_rows=True")


@mcp.tool()
@safe
def yx_ms_export_list(area_id: int, limit: int = 10) -> dict:
    """[秒杀/便宜包邮/特价] 查导出任务列表（进度+下载直链），倒序。status 0进行中/1完成。"""
    return yx_ms.export_list(area_id, limit=limit)


@mcp.tool()
@safe
def yx_ms_table_apply_dryrun(area_id: int, batch_id: int, rows: list, channel: str = "seckill",
                             activity_duration=None, apply_sku_type: int = 3,
                             expected_time: str = "20:00:00", form_override: dict = None) -> dict:
    """
    [秒杀/便宜包邮/特价] **表格批量报名 DRY-RUN**：规整行+生成填好模板(不上传)，回显 form/行数+confirm_token。
    **channel**：`seckill`(秒杀:6列/场次+时长按小时/有expectedTime) · `baoyou`/`tejia`(便宜包邮/特价:3列/仅时长按天/无场次/applyExtendInfo带packageType)。
    rows：秒杀=[{skuId,promoPrice,promoQty,limitQty?,limitMode?,isMain?}]；便宜包邮/特价=[{skuId,promoPrice,promoStock}]。
    activity_duration 留空→channel 默认(秒杀28/便宜包邮30天)；batch_id=yx_ms_get_batch_id。formItemId 走 formset 按 code 自动动态取(formItemId_source 显示来源)，异常可 form_override={code:formItemId} 覆盖。
    """
    return yx_ms.table_apply_dryrun(area_id, batch_id, rows=rows, channel=channel,
                                    activity_duration=activity_duration, apply_sku_type=apply_sku_type,
                                    expected_time=expected_time, form_override=form_override)


@mcp.tool()
@safe
def yx_ms_table_apply(area_id: int, batch_id: int, rows: list, confirm: str, channel: str = "seckill",
                      activity_duration=None, apply_sku_type: int = 3,
                      expected_time: str = "20:00:00", form_override: dict = None,
                      wait: bool = True, wait_timeout: int = 120) -> dict:
    """
    [秒杀/便宜包邮/特价] **表格批量报名真执行**（上传填好模板到 /apply/openness/apply/excel，真金白银）。
    channel: `seckill` / `baoyou` / `tejia`。需相同参数先 yx_ms_table_apply_dryrun 拿 confirm_token 再带 confirm。**wait=True 自动轮询到终态、回执自验证**(返回 result: 成功/失败数+失败明细，不看有延迟的监控数据)；大批量可 wait=False 后自行 yx_ms_submit_list 查。formItemId 走 formset 动态取。
    """
    return yx_ms.table_apply(area_id, batch_id, rows=rows, confirm=confirm, channel=channel,
                             activity_duration=activity_duration, apply_sku_type=apply_sku_type,
                             expected_time=expected_time, form_override=form_override,
                             wait=wait, wait_timeout=wait_timeout)


@mcp.tool()
@safe
def yx_ms_form_set(area_id: int) -> dict:
    """[秒杀/便宜包邮/特价] 取该 area 报名 formSet 的 **{code: formItemId}** 映射（POST /openness/formset/area/detail）。formItemId 每活动可能变、code 稳定；formset 只按 areaId 取、与报名模式无关。用于核对/调试表格报名的活动级 formItemId。"""
    return {"areaId": int(area_id), "map": yx_ms.fetch_form_set(area_id)}


@mcp.tool()
@safe
def yx_ms_submit_list(area_id: int, apply_sku_type: int = 3, limit: int = 10) -> dict:
    """[秒杀/便宜包邮/特价] 查**表格报名提交进度**（querySubmitList），倒序。status 0进行中/1完成/2失败；含 total/success/fail + **failFileAddress(失败明细直链)**。"""
    return yx_ms.submit_list(area_id, apply_sku_type=apply_sku_type, limit=limit)


@mcp.tool()
@safe
def yx_stoploss_plan(max_profit: float = 0.0, limit: int = 300, only_with_orders: bool = False,
                     resolve_feasibility: bool = True) -> dict:
    """[止亏·一键方案] **亏损清单 → 对账 → jx实担归因 → 过禁令 → 过可执行性 → 按手段分组**。只读，不发写请求。
    四道闸门顺序不可换（2026-07-28 每类各踩一次坑）：①对账剔掉不计入到手价的促销(便宜包邮)；
    ②按 jxReward 我担口径归因(不是客户侧)；③过 osw_protected 禁令(有意的引流款别排进来)；
    ④过可执行性(国补他人报名的没 applyId 退不了、券解不到 campaignId 的摘不了)。
    only_with_orders=True 只留真出过单的（实证 214 款亏损里 117 款是纸面亏、零单）。
    返回含「真凶排行」「分组方案」「动不了」「时效」「★执行纪律」。**收益是上限不是预测**——减免会补位。"""
    return yx_stoploss.build_plan(max_profit=max_profit, limit=limit,
                                  only_with_orders=only_with_orders,
                                  resolve_feasibility=resolve_feasibility)


@mcp.tool()
@safe
def yx_stacking_check(a: str, b: str = "") -> dict:
    """[券促规则·必查] **查两类券促能否叠加**（官方 51×51 规则表真值，只读）。
    只给 a → 返回它的全部互斥(×)/可叠加(√)清单；给 a+b → 返回该组合的 √/×/可选。
    ⚠️**动券促方案前先查这个，别凭印象推**——2026-07-28 凭记忆推错过三次
    （漏了「券×平台神券/广告智能券=√」跨类可叠、漏了「券×总价促销=×」）。
    名字支持简写：便宜包邮/超级补贴/官方直降/国补/全品类券/跨店满减…（会解析成规范名）。"""
    return yx_stacking.can_stack(a, b) if b else yx_stacking.conflicts(a)


@mcp.tool()
@safe
def yx_stacking_predict(current: list, new_name: str, new_jx: float,
                        new_face: float = 0, kind: str = "coupon") -> dict:
    """[券促规则] ★**预测「给某SKU新增一项券/促」对京喜毛利的影响**（只读）。
    current = 该 SKU 当前的 coupons 或 promotions（osw_pricing_query 的返回，需含 name/reward/jxReward）；
    new_jx = 新增项的**我担**金额；new_face = 新增项面额（判谁生效用）；kind: coupon/promo。
    返回 Δ毛利 + 判定（顶替了谁 / 面额不够不生效 Δ=0 / 无互斥项纯新增）。
    ⚠️**别用「现毛利 − 新增项我担」估算**——那假设一切可叠加，会严重高估损失
    （5.9-5 券实测 538 款里 179 款其实 Δ=0、15 款反而变好）。"""
    return yx_stacking.predict_delta(current or [], {"name": new_name, "reward": new_face},
                                     new_jx, kind=kind)


@mcp.tool()
@safe
def yx_stacking_rules() -> dict:
    """[券促规则] 生效/展示**数量与优先级**表（官方 sheet2）：单品促销只生效1个及其优先级
    (百亿补贴>减钱大的>免邮促销>双价格权重大的>闪购>后创建的)、总价促销普惠1+非普惠1用户可选、
    优惠券弹层最多下发30张(展示≠生效) 等。另附满减门槛口径（按单品促销价，无单促时取京东价）。"""
    return {"门槛口径": yx_stacking._load()["_门槛口径"],
            "取值含义": yx_stacking._load()["_取值"],
            "生效展示规则": yx_stacking.effect_rules(),
            "全部类型名": yx_stacking.names(),
            "详版": "docs/yx/NOTES_stacking.md"}


if __name__ == "__main__":
    mcp.run()


@mcp.tool()
@safe
def yx_ms_recover_thresholds(area_id: int, since: str = "", limit: int = 10,
                             apply_sku_type: int = 3) -> dict:
    """★[秒杀/包邮] **从被拒明细里回收平台真实门槛**——导出清单"无门槛"≠平台没门槛。

    2026-08-10 实证：479 款首轮落地 432(90%)，47 款被拒中 42 款来自无门槛桶；
    回收真实门槛重解价格补报后 **68/68 落地，总落地率 90%→98%**。

    ⚠️**必须传 `since`**（'YYYY-MM-DD HH:MM:SS'，本次提交时刻）：
      `submit_list` 是**账号维度的历史任务**，不传会把几天前的失败文件也捞进来，
      导致把早已落地的 SKU 又报一遍（我 2026-08-10 就这么干了）。

    返回 `thresholds`（可补报）+ `others`（短标题非法字符/已报过名等**非价格原因**，改价没用）。
    """
    return yx_ms.recover_thresholds(area_id, since=since or None, limit=limit,
                                    apply_sku_type=apply_sku_type)


@mcp.tool()
@safe
def yx_ms_replan_rejected(rows: list, thresholds: dict, min_margin: float = 0.02) -> dict:
    """[秒杀/包邮] 按回收的真实门槛**重解价格**，产出可直接喂 table_apply 的补报行。

    把到手价压到「门槛 − 0.01」，压完毛利 < `min_margin` 的进 `giveup`
    ——那是**平台门槛低于我的成本**，报了就是亏（实测有一款门槛 2.21 而成本 3.99）。
    """
    return yx_ms.replan_rejected(rows, thresholds, min_margin=min_margin)


# --------------------------------------------------------------------------- #
# 顺手买（黄流结算页专享价）—— 第三套报名体系，别和 campaign / openness 混
# --------------------------------------------------------------------------- #
@mcp.tool()
@safe
def yx_ssm_pools(activity_id: str = "101757021") -> dict:
    """[顺手买] 列出全部收品池 + **容量与已报数**（`已报/容量`）+ 各池 resourceId。

    ★这是**第三套报名体系**：`yx_campaign_get_detail` 会报「活动id不存在」、
      `ms.find_applied` 会报「收品池信息不存在」——**不是登录态问题，是走错体系**。
    ★`已报/容量` 是判落地最省事的口径（实证 251/2263 与上传回执完全一致）。
    ★`resourceId` 为 None 的池**不能上传**：它与 areaId 是两个号，需从该池一次上传动作的
      抓包里取 form 的 resourceId，补进 `yx/ssm.py` 的 `POOL_RESOURCE`。"""
    return yx_ssm.list_pools(activity_id)


@mcp.tool()
@safe
def yx_ssm_rules(activity_id: str = "101757021") -> dict:
    """[顺手买] 取**真实价格门槛**（actcenter room 详情）。

    ★实证门槛只有「**低于京东前台价 9 折**」。池配置里那段「到手价≤活动上线前15天历史最低成交价」
      是**大促模板套话**，266 款实盘一条都没被价格拒 ⇒ 报更深是业务选择，不是平台要求。
    ★真正卡人的是**商品资质**：好评率 ≥88%、价格星级——**改价救不了，只能换品**。"""
    return yx_ssm.room_rules(activity_id)


@mcp.tool()
@safe
def yx_ssm_plan_prices(sku_ids: list, floor: float = -0.5, disc: float = 0.7,
                       listed_prices: dict = None, coupon_self: dict = None) -> dict:
    """[顺手买] 按**成本底料**算建议专享价（候选取最低，`成本+floor` 兜底）。

    `floor` 是**每单最多亏多少**（-0.5 = 最多亏 5 毛），**是底线不是目标**——让到够用就停。
    `coupon_self` {sku: 单均自担券} 传了就会把价定到「券后价下方 0.1」，把订单从券通道拉过来
    （专享价不与券促叠加，同 SKU 实证走专享价比走非专享价单均高 **+¥0.734**）。

    ⚠️**底料必须用 osw 成本，别拿看板 `per_ord_loss` 倒算**——后者含已让利，倒算会重复扣减，
      把跑量款判成「该涨价」（实测把 21 单/日、现价 ¥2.90 的款判成该涨到 ¥4.87）。
    ⚠️成本已**剔除 advCost**（24h 快照会炸，实测把 ¥7.50 的品算成全成本 ¥42.54）。
    状态非「正常」的行别提交。"""
    return yx_ssm.plan_prices(sku_ids, floor=floor, disc=disc,
                              listed_prices=listed_prices, coupon_self=coupon_self)


@mcp.tool()
@safe
def yx_ssm_apply_dryrun(area_id: int, rows: list, resource_id: int = None) -> dict:
    """[顺手买] 报名 DRY-RUN：校验行 + 生成 6 列 xlsx（不上传）+ confirm_token。
    rows=[{skuId, price, stock?=100000, limitQty?=20, limitOrd?=20}]。"""
    return yx_ssm.apply_dryrun(area_id, rows, resource_id=resource_id)


@mcp.tool()
@safe
def yx_ssm_apply(area_id: int, rows: list, confirm: str = "", resource_id: int = None,
                 wait: bool = True, wait_timeout: int = 180) -> dict:
    """[顺手买] **报名真执行**：xlsx → 上传 `mac/common/fileProcess` → 轮询回执 → 自动解析失败明细。
    需相同参数先 `yx_ssm_apply_dryrun` 拿 confirm_token。

    ⚠️**上传闸跨频道共享**（与国补/直降共用，约 90~120 秒一个文件），已接退避；
      `taskId is None` + `success=False` ⇒ **一条都没落地**，可原样干净重试，别当部分失败去补报。
    ⚠️**别预筛资质**：平台好评率口径与选品表有出入（表 0.86→平台 87%、表 0.50→平台 85%），
      按表格筛会误杀（实测 24 款疑似里只有 14 款真被拒）。**全量提交让平台判**。
    ⚠️重复提交同一批会逐行报「在该场次下已报名」——**那是判重不是失败**，可当上一批的落地回读。"""
    return yx_ssm.apply(area_id, rows, confirm=confirm, resource_id=resource_id,
                        wait=wait, wait_timeout=wait_timeout)


@mcp.tool()
@safe
def yx_ssm_apply_result(area_id: int = None, resource_id: int = None, limit: int = 10) -> dict:
    """[顺手买] 查最近几次上传回执（成功/失败数），并把最新一次的 failUrl **逐行归类**。

    实证四类：`好评率要求N%以上`(资质,改价没用) / `价格星级`(资质) /
    `在该场次下已报名`(判重,反证已落地) / 其它。
    ★判生效别看提报数——回读要看 `ge_ssm_overview` 的 `fixed_price_deal_ord_num` 起量。"""
    return yx_ssm.apply_result(area_id, resource_id=resource_id, limit=limit)


@mcp.tool()
@safe
def yx_ms_plan_reduce_warned(activity_id: str, area_id: int, erp: str = "",
                             sessions: list = None, include_lossy: bool = False,
                             limit: int = None) -> dict:
    """[秒杀] ★**失效预警降价「规划」**（只读）：扫预警 → 按人筛 → 只取「照降」→ 逐款 couponInfo 真值解新促销价
    → 出可直接喂 `yx_ms_reduce_price_dryrun` 的 rows。

    把每次现搭的三步固化：**必须按人筛**（本接口混多 ERP，实测 27 条里本人 24、另 3 人各 1）、
    **只动「照降」**（`降了会亏` 要人拍板，`include_lossy=True` 才带）、
    **新价必须解**（斜率不是 1，照 −0.01 常常白降）。
    ★还会**跳过「已经降过」的**：预警桶里挂的是没清掉的旧行，不查就会对同一款反复降、每次多建一条促销。
    返回 {rows, 汇总, 别人的, 降了会亏, 已降过, 解析失败}。erp 留空=当前登录用户。"""
    return yx_ms.plan_reduce_warned(activity_id, area_id, erp=(erp or None),
                                    sessions=sessions, include_lossy=include_lossy, limit=limit)


@mcp.tool()
@safe
def yx_ms_verify_reduced(activity_id: str, area_id: int, rows: list,
                         apply_sku_type: str = "3") -> dict:
    """[秒杀] ★★**降价成没成的权威回读**：判据 = 同 SKU 同 batch 出现「**新 applyId + 新促销价 + applyStatus=2**」。
    **别看失效预警桶**——它是滞后指标（旧行 18/19 等平台清，清空比落地晚半小时以上）。
    rows = yx_ms_plan_reduce_warned 的 rows。返回 {已落地, 未落地, 明细, 未落地明细}。"""
    return yx_ms.verify_reduced(activity_id, area_id, rows, apply_sku_type=apply_sku_type)


@mcp.tool()
@safe
def yx_daily_run(part: str = "", with_bybt_plan: bool = True) -> dict:
    """[yx] ★**每日必跑入口**（只读、零写，对标 jzt_daily）：代码新鲜度 → 秒杀失效预警 → 秒杀当日报名进度
    → 百补报名缺口 → 便宜包邮/特价池概览。返回 {成功, 失败, **需要人看**, 结果}。

    **一天跑两次**（早/晚）：平台在场次开始前才挂失效预警——2026-08-24 上午扫完 15→0，17:00 又冒 27 条。
    起因：**百补从 8-21 断到 8-24 没人发现**（当天 A 桶有 60 款可报），促销这条线以前全靠人记得。
    `part`='morning'/'evening'（留空按钟点猜）；`with_bybt_plan=False` 跳过最重的百补规划(~180s)。"""
    from blacklight.yx import daily as yx_daily
    return yx_daily.run(part=(part or None), with_bybt_plan=with_bybt_plan)
