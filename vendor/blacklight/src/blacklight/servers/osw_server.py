"""
osw-mcp MCP server（stdio）：**京东京喜 osw 采销工作台**的能力面。

两大数据原语，代码在公共层 `jdcore/`（单一真相，被 yx-mcp 的 bybt/ms 定价、未来 jzt-mcp 广告判盈亏共用）：
  - **商品列表 / 商品信息**（sff.jd.com，jdcore/osw_product）：全量 SKU 供货价(京喜采购价)/物流/库存/类目/上下架/采销归属筛
  - **实时毛利监控 / 定价底料**（osw.jd.com，jdcore/osw_margin）：名下亏损款(+根因) + 单/批 SKU 京东价/券促/到手价/**京喜全口径毛利率** + 盈亏体检

命名：工具带 `osw_` 前缀。鉴权与 yx 同一登录态（yx cookie 授权 sff/osw/api.m 等），共用 jdcore/credentials.json。
只读为主（本工作台面无写操作；券促删除/报名等写操作在 yx-mcp）。错误经 @safe 统一转 {"error":...}。

注册：
  claude mcp add osw -- python "C:/Users/wangruihan9/.claude/skills/osw-mcp/server.py"
"""
from __future__ import annotations

import functools
import os
import sys

from mcp.server.fastmcp import FastMCP  # noqa: E402

from blacklight.core import auth as jd_auth # noqa: E402  (公共层：登录态，与 yx 同 realm)
from blacklight.osw import product as osw_product # noqa: E402  (公共层：商品列表)
from blacklight.osw import margin as osw_margin # noqa: E402  (公共层：毛利监控/定价)
from blacklight.osw import selection as osw_selection # noqa: E402  (公共层：公共商品池/认领/驳回，选品CMS)
from blacklight.osw import ware_edit as osw_ware_edit # noqa: E402  (公共层：在售/下架 编辑商品，gx-pc 加载 / gmall 保存)
from blacklight.osw import supplier as osw_supplier # noqa: E402  (公共层：供应商管理/切商/报价/配额，选品CMS 同网关)
from blacklight.core import protected as osw_protected # noqa: E402  (公共层：禁止触碰清单，摘券/改价前置闸门)
from blacklight.pic import client as pic_client # noqa: E402  (带图评价 picstart：待补清单/提交/回读，同 wqadmin 网关)
from blacklight.core import BlacklightError  # noqa: E402

mcp = FastMCP("blacklight-osw")


def safe(fn):
    """只读工具错误包装：捕 BlacklightError → {"error": ...}（保留签名给 FastMCP 建 schema）。"""
    @functools.wraps(fn)
    def w(*a, **kw):
        try:
            return fn(*a, **kw)
        except BlacklightError as e:
            return {"error": str(e)}
    return w


# ------------------------------- 鉴权（与 yx 同一登录态） ------------------------------- #
@mcp.tool()
def osw_login_status() -> dict:
    """查看登录态：usable(能否用,以在线探活为准)+pin(当前采销)+mcpman_live。与 yx 同一 cookie(jdcore/credentials.json)，任一处登录即通。"""
    return jd_auth.status_dict()


@mcp.tool()
def osw_login() -> dict:
    """浏览器登录 yx.jd.com 并保存 cookie（弹 Chrome）。与 yx-mcp 共用登录态，登录一次两处都通。"""
    jd_auth.ensure_session(auto_relogin=True)
    return jd_auth.status_dict()


@mcp.tool()
def osw_set_pin(pin: str) -> dict:
    """[多用户] 设置当前采销 ERP/PIN（持久化到 jdcore/credentials.json）。之后归属筛/审计以此身份。也可用环境变量 YX_PIN。"""
    return {"pin": jd_auth.set_pin(pin), "note": "已保存。"}


@mcp.tool()
@safe
def osw_doctor() -> dict:
    """[无人值守] **契约巡检**：打 osw 关键接口(商品列表/SKU明细/毛利监控/定价)校验返回结构没漂移。返回 {healthy, drift[], checks[]}。
    抓包封装的接口会随页面改版漂移——改价/取数自动跑前先 doctor，`healthy=False`(有drift)转人工核对再放手。"""
    from blacklight.osw import doctor
    return doctor.run()


# ------------------------- 商品列表 / 商品信息（sff.jd.com） ------------------------- #
@mcp.tool()
@safe
def osw_product_list(page: int = 1, page_size: int = 50, product_state: str = "4",
                     name: str = None, category_ids: list = None,
                     min_price: float = None, max_price: float = None,
                     min_stock: int = None, max_stock: int = None,
                     created_from: str = None, created_to: str = None,
                     online_from: str = None, online_to: str = None,
                     offline_from: str = None, offline_to: str = None, erp: str = None,
                     sort: str = "onlineTime desc", biz_id: str = None) -> dict:
    """[商品] **分页列店铺商品**。粒度=**SPU(productId)**，行内 skuId 是代表主SKU，真实SKU数看 skuCount。
    每行：productId/skuId/skuCount/name/itemNum(货号)/state(4=在售·10=已下架)/京东价(jdPrice/min/max)/京喜采购价(jxCgPriceMin/Max=供货价)/库存/销量/类目/品牌/供应商/created(首次创建)/onlineTime(最近上架)/offlineTime(最近下架)/skuUrl。
    **拉新品用 created_from/created_to**(首次创建准；onlineTime 会被重新上架刷成当天)；online_from/online_to 按上架筛；offline_from/offline_to 按下架筛。时间收 'YYYY-MM-DD'。
    **erp=采销ERP归属筛**(传 ERP 只返其名下商品；⚠️行内不吐归属,只能反向筛,看自己名下传自己 ERP)。
    product_state **'4'=在售(默认)/'10'=已下架/''=全部**（拉下架款传 '10'，宜配 sort='offlineTime desc'）。sort='字段 asc|desc'(∈onlineTime/offlineTime/created/modified/jdPrice/salesVolume/stockNum)。page_size≤100。多店铺传 biz_id。"""
    return osw_product.product_list(page=page, page_size=page_size, product_state=product_state,
                                   name=name, category_ids=category_ids, min_price=min_price,
                                   max_price=max_price, min_stock=min_stock, max_stock=max_stock,
                                   created_from=created_from, created_to=created_to,
                                   online_from=online_from, online_to=online_to,
                                   offline_from=offline_from, offline_to=offline_to, erp=erp,
                                   sort=sort, biz_id=biz_id)


@mcp.tool()
@safe
def osw_product_get(sku_ids: list, biz_id: str = None) -> dict:
    """[商品] **按 SKU 批量取商品档**（skuIdList 过滤，单次≤100）。返回 {requested,found,missing,rows}。
    给一批 SKU 补齐名称/货号/京东价/京喜采购价(供货价)/库存/类目——毛利监控只给 SKU 维度，这里给完整商品档。"""
    return osw_product.product_get(sku_ids, biz_id=biz_id)


@mcp.tool()
@safe
def osw_product_search(name: str, page: int = 1, page_size: int = 50,
                       product_state: str = "4", biz_id: str = None) -> dict:
    """[商品] **按名称模糊搜商品**。product_state '4'=在售(默认)/''=全部。page_size≤100。"""
    return osw_product.product_search(name, page=page, page_size=page_size,
                                     product_state=product_state, biz_id=biz_id)


@mcp.tool()
@safe
def osw_product_all(product_state: str = "4", name: str = None, category_ids: list = None,
                    max_rows: int = 2000, created_from: str = None, created_to: str = None,
                    online_from: str = None, online_to: str = None,
                    offline_from: str = None, offline_to: str = None, erp: str = None,
                    sort: str = "onlineTime desc", biz_id: str = None,
                    with_rows: bool = False, sample: int = 5) -> dict:
    """[商品] **拉全量商品**（内部按100翻页到取完或 max_rows 上限）。返回 {total,fetched,truncated,rows}。
    product_state '4'=在售(默认)/'10'=已下架/''=全部。某月新品：created_from/created_to(首次创建,推荐)；online/offline_from/to 按上/下架筛；erp=采销ERP归属筛。时间收 'YYYY-MM-DD'。
    拉下架款：product_state='10'（宜配 sort='offlineTime desc'）。truncated=True 表示达上限未取全(调大 max_rows 或加过滤)。全量可与毛利监控 join 做全店盘点。"""
    from blacklight.core.mcpio import cap_rows
    r = osw_product.product_all(product_state=product_state, name=name, category_ids=category_ids,
                                max_rows=max_rows, created_from=created_from, created_to=created_to,
                                online_from=online_from, online_to=online_to,
                                offline_from=offline_from, offline_to=offline_to, erp=erp,
                                sort=sort, biz_id=biz_id)
    # 全店在售约 8000+ 款、每行十余字段 ⇒ 默认只回样例+条数（with_rows=True 取全量）
    return cap_rows(r, with_rows=with_rows, sample=sample,
                    where="要全量传 with_rows=True；做全店盘点建议在库层调 osw.product.product_all")


@mcp.tool()
@safe
def osw_product_sku_detail(product_id: str, with_cost: bool = True, biz_id: str = None) -> dict:
    """[商品] **SPU→SKU 明细**（商品列表是 SPU 粒度，这里展开到每个 SKU）。给一个 productId，列其全部 SKU：
    skuId/itemNum(货号)/variant(销售属性变体,如「粉色-中号」)/skuName/jdPrice(SKU京东价)/purchasePrice(采购价·供货价)
    + (with_cost) actualTotalCost(全成本·采购+物流landed) + grossProfit(裸毛利=京东价−全成本)/grossMargin。
    数据源 querySkuPrice + getPriceApprovalStatus（cookie-only）。product_list/all 拿到 productId 后用它下钻到 SKU。"""
    return osw_product.product_sku_detail(product_id, biz_id=biz_id, with_cost=with_cost)


# ---------------------- 改价（写：dry-run + confirm_token 门 + 审计） ---------------------- #
@mcp.tool()
@safe
def osw_price_plan(price_map: dict, biz_id: str = None) -> dict:
    """[改价·只读] 把 {skuId: 目标京东价} 解析成**可写行 + 预览**：每 sku 补 productId/全成本/productType/现价，算新裸毛利/毛利率。
    核对 rows(现价→新价)后，把 rows 传 osw_price_update_dryrun 拿 confirm_token 再改价。失败项在 errors。

    ★★**「新毛利率(裸·不含券促)」不是到手价毛利率**，别只看它就下单（2026-08-14 踩坑：
      本函数给 10130829981648 报「新毛利率 55.79%」，改完实测到手价毛利 **−1.41 分文未变**）。
      每行已附 `当前到手价 / 传导率估计 / 预估新到手价毛利 / 警告`：
      **便宜包邮锁价的款传导率=0，涨价纯属无效**，得先退促销。"""
    return osw_product.plan_reprice(price_map, biz_id=biz_id)


@mcp.tool()
@safe
def osw_price_update_dryrun(rows: list, biz_id: str = None) -> dict:
    """[改价·DRY-RUN] 组装 updatePrices 但**不发送**，回显 payload + confirm_token。
    rows=osw_price_plan 出的行(或手填 [{productId,skuId,jdPrice,actualTotalCost,productType}])。单次≤50。"""
    return osw_product.update_prices_dryrun(rows, biz_id=biz_id)


@mcp.tool()
def osw_price_update(rows: list, confirm: str = "", biz_id: str = None) -> dict:
    """[改价·真写] **真改京东价**（PriceWriteViewService.updatePrices，不可轻易撤回）。
    需相同 rows 先 osw_price_update_dryrun 拿 confirm_token 再带 confirm。单次≤50。
    ✅已活体验证（2026-07-20 首改 / 08-03 批量 49 款 / 08-14 五款回读），与 WRITE_VERIFICATION 一致。
    ⚠️**改完必回读 `osw_pricing_query`**：涨价对到手价**不是 1:1 传导**——国补按新价 15% 回补、
      直降/总价促销按比例多吃、**便宜包邮直接锁死前台价 ⇒ 0 传导**（实测 5 款 100/85/76/72/0%）。"""
    try:
        return osw_product.update_prices(rows, confirm=confirm, biz_id=biz_id)
    except osw_product.BlacklightError as e:
        return {"executed": False, "reason": str(e)}


@mcp.tool()
@safe
def osw_product_title_update_dryrun(updates: list, biz_id: str = None) -> dict:
    """[改标题·DRY-RUN] 改**商品名/长标题**(≤60字)组装但不发，回显 payload+confirm_token。
    updates=[{productId, productName}]。单次≤50。⚠️改的是商品名(长标题)，**非秒杀短标题**。"""
    return osw_product.update_titles_dryrun(updates, biz_id=biz_id)


@mcp.tool()
def osw_product_title_update(updates: list, confirm: str = "", biz_id: str = None) -> dict:
    """[改标题·真写] **真改商品名/长标题**（ProductInfoWriteViewService.updateProducts，≤60字，SPU级，不可轻易撤回）。
    需相同 updates 先 osw_product_title_update_dryrun 拿 confirm_token 再带 confirm。单次≤50。
    ⚠️未活体验证：首次务必单款+读回确认。⚠️非秒杀短标题(那是另一字段)。"""
    try:
        return osw_product.update_titles(updates, confirm=confirm, biz_id=biz_id)
    except osw_product.BlacklightError as e:
        return {"executed": False, "reason": str(e)}


@mcp.tool()
@safe
def osw_product_status_update_dryrun(product_ids: list, operation: str,
                                     down_reason: str = "", biz_id: str = None) -> dict:
    """[上下架·DRY-RUN] 上/下架组装但不发，校验+回显 payload+confirm_token。
    operation='down'(下架)/'up'(上架)；product_ids=[productId,...]（SPU级，去重，单次≤100）。
    ⚠️下架=客户端立即不可见。"""
    return osw_product.update_status_dryrun(product_ids, operation, down_reason=down_reason, biz_id=biz_id)


@mcp.tool()
def osw_product_status_update(product_ids: list, operation: str, confirm: str = "",
                              down_reason: str = "", biz_id: str = None) -> dict:
    """[上下架·真写] **真上/下架商品**（ProductStatusUpdateViewService.updateProductStatus，SPU级，**客户端立即生效**）。
    operation='down'(下架)/'up'(上架)。需相同参数先 osw_product_status_update_dryrun 拿 confirm_token 再带 confirm。单次≤100。
    回执逐品自验证 success_count/failed。⚠️本地cookie-only路径未活体：首次务必单款受控往返(down→up)确认。"""
    try:
        return osw_product.update_status(product_ids, operation, confirm=confirm,
                                        down_reason=down_reason, biz_id=biz_id)
    except osw_product.BlacklightError as e:
        return {"executed": False, "reason": str(e)}


# ---------------------- 实时毛利监控 / 定价底料（osw.jd.com） ---------------------- #
@mcp.tool()
@safe
def osw_margin_triage(protected_line: float = 1.0, top: int = 15) -> dict:
    """★★[毛利监控] **日常巡检入口**：双网发现 + 五桶分诊。扫全量在售（~25 秒 / 8,284 款）。

    **别再用 `osw_margin_list_low` 当巡检入口**——那只是双网里的「预估网」。
    2026-08-10 全量实测：它盯着 130 款纸面亏，却**一款也看不见** D 桶那 272 款
    真在流血的（15 日 −32,625，是 A 桶的 5.7 倍）。

    | 桶 | 判据 | 实测 | 处置 |
    |---|---|---|---|
    | A 真亏   | 预估<0 且 实际<0     | 32  | 进归因 |
    | B 纸面亏 | 预估<0 但 实际≥0     | 130 | **不动**；`昨日亏损单>0` 标"正在恶化" |
    | C 零单   | 近15日无成交         | 40  | 不动 |
    | D 漏检   | **预估≥0 但 实际<0** | 272 | **进归因（最大失血源）** |
    | E 禁令   | 命中禁止触碰清单     | 31  | 按 1 元线分档，超线进待处置 |

    输出的 `归因入口` = A+D+E2，直接喂给 `easybi_coupon_attribution` 做逐券归因。
    `protected_line`：禁令款可接受的单亏上限（元，用户拍板默认 1.0）。
    """
    return osw_margin.triage(protected_line=protected_line, top=top)


@mcp.tool()
@safe
def osw_protected_health(protected_line: float = 1.0) -> dict:
    """[禁止触碰清单] **禁令款 1 元线体检**：受保护 ≠ 可以无限亏。

    规则（用户 2026-08-10 拍板）：单亏 ≤1 元可接受，>1 元进待处置队列。
    判据用**实际单均毛利**不是预估（预估最坏情况 + 有盲区，会把线内款误报成超线）。
    实测：少数超线款吃掉几乎全部失血（20 款超线 −6,894，11 款线内 −501）。
    """
    from blacklight.core import protected as _prot
    rows = osw_margin.scan_portfolio()["rows"]
    cand = [r for r in rows if osw_margin.is_losing(r)]
    return _prot.health_check(cand, line=protected_line)


@mcp.tool()
@safe
def osw_margin_list_low(max_profit: float = 0.0, limit: int = 200) -> dict:
    """[毛利监控] 列名下**预估毛利** < max_profit 元的 SKU（默认 0），带根因+实际亏损单。

    ⚠️★**这只是双网里的「预估网」，别拿它当巡检入口**——它看不见「预估≥0 但实际在亏」
      的一整类（2026-08-10 实测 272 款 / −32,625）。日常巡检走 **`osw_margin_triage`**。
      本工具适合：只想看当前配置下最坏情况会亏的款（前瞻预警、报名前体检）。

    每条：预估毛利/毛利率/京东价/采购价/券促减免/到手价 + **根因**(定价/物流过高/券促打穿/薄毛利) + 昨日·近15日亏损单。"""
    return osw_margin.list_low_margin(max_profit=max_profit, limit=limit)


@mcp.tool()
@safe
def osw_pricing_query(sku_id: str) -> dict:
    """[定价] **单 SKU 定价底料**（Home 接口，频道无关）：采购价/物流/CPS/广告 + 全部券促减免 + 重构京东价/到手价/**京喜全口径毛利率**。
    到手价=基价−券−促；全成本=采购+物流+CPS+广告。报名定价与盈亏体检的数据源，纯读。"""
    return osw_margin.query_pricing(sku_id)


@mcp.tool()
@safe
def osw_pricing_batch(sku_ids: list, target_margin: float = None,
                      objective: str = "max_margin", caps: dict = None,
                      channel: str = None) -> dict:
    """[定价] **批量到手价/毛利/盈亏体检**（每 SKU 一次 Home 接口，纯本地算，无写）。
    给 target_margin 则附**建议报名价**(本地反解=平台试算到分)。objective: max_margin(撞上限最大毛利,默认)/min_price(压到保毛利线)。
    caps: {skuId: 到手价上限}；channel: 报名频道(解价时排除同大类已有促销)。"""
    return osw_margin.batch_pricing(sku_ids, target_margin=target_margin,
                                    objective=objective, caps=caps, channel=channel)


# ---------------- 公共商品池 / 认领 / 驳回（选品CMS，api.m.jd.com/selectioncms） ---------------- #
@mcp.tool()
@safe
def osw_pool_list(page: int = 1, page_size: int = 20, create_from: str = None, create_to: str = None,
                  status: int = None, name: str = None, vender_id: str = None, vender_spu: str = None) -> dict:
    """[公共商品池] **分页列供应商提报待认领的标的**（比"售卖中/已下架"更前置的选品环节）。
    每行：vprojectId(标的ID)/spuId/spuName/venderName(供应商工厂店)/sellerName(所属店铺)/categoryName/status/**canClaim(能否认领)**/createTime(提报时间)/三角色(prey/hunter/shotgun)。
    create_from/create_to='YYYY-MM-DD HH:MM:SS'(提报时间筛)；status=状态；name=品名模糊；page_size≤100。下钻用 osw_pool_detail(vprojectId)。"""
    return osw_selection.pool_list(page=page, page_size=page_size, create_from=create_from,
                                   create_to=create_to, status=status, name=name,
                                   vender_id=vender_id, vender_spu=vender_spu)


@mcp.tool()
@safe
def osw_pool_detail(project_id: str, raw: bool = False) -> dict:
    """[公共商品池] **单标的详情**（认领页数据模型）。给一个 vprojectId，返回：
    itemName/categoryName/venderId、roleInfo(prey/hunter/shotgun)、versionInfo(乐观锁)、**buttonInfo{submit,reject}**(能否认领/驳回)、refuseTypes(驳回原因枚举)、
    skus[{venderSkuId(认领price_map的key)/skuName/**supplyAll采购价上限/skuCost商品成本/expressCost快递/expressPack物流打包/materialsCost耗材**}]、
    **specWarnings(同规格成本一致性体检)**：同尺寸大小、成本字段有分歧就标出(常见驳回理由30；材质/包装不在数据源不覆盖)。raw=True 附完整原始 data。"""
    return osw_selection.project_detail(project_id, raw=raw)


@mcp.tool()
@safe
def osw_pool_tasks(page: int = 1, page_size: int = 50, status="", query_type: str = "0") -> dict:
    """[选品任务] **我名下已认领、在选品8步流程流转的任务列表**（getProjectList，区别于公共商品池的待认领标的）。
    选品流程：①发起(猎物)→②寻源(猎人)→③品拉审核(猎枪)→④三猎确认→⑤系统审核→⑥审批中→**⑦待上品(待铺货)**→⑧完成。
    每行：projectId(可传 osw_pool_detail 下钻)/itemName/status(阶段码)/**stage(按三猎确认推导的可靠进度:卡在②寻源/③品拉审核/④三猎确认完成)**/checkFailStatus/authorityList。
    status 过滤：''=全部（观测码 40/75/76/80…，确切码↔步待确认；判进度优先看 stage）。page_size≤100。"""
    return osw_selection.task_list(page=page, page_size=page_size, status=status, query_type=query_type)


@mcp.tool()
@safe
def osw_pool_pending(page: int = 1, page_size: int = 20, status="", query_type: str = "0") -> dict:
    """[待铺货] **认领成功、待上品/铺货的商品列表**（getBidItemList，osw 商品列表页「待铺货」tab）。
    每行：projectId(可下钻 osw_pool_detail)/itemName/createShopName(供应商)/createSupplierSpuId(商家SPU)/status/**stage(三猎确认推导的进度)**/
    **jdPriceMin~Max(京东价区间)/profitMarginMin~Max(毛利率区间)/addPriceRateMin~Max(加价率区间)/categoryLimitMaxPrice(类目限价)**/similarProductInfoList(同款)/authorityList(含 adopt采纳/copyWare铺货)。
    这是"认领→铺货"链路里认领后的商品池；铺货写接口待补。page_size≤100。"""
    return osw_selection.bid_item_list(page=page, page_size=page_size, status=status, query_type=query_type)


@mcp.tool()
@safe
def osw_pool_proxy_roles() -> dict:
    """[公共商品池] **可代理的采销角色列表**（认领时选 猎人/猎枪 的候选）。每项 {proxyErp, realName, roleList(PREY/HUNTER/OSW)}。"""
    return osw_selection.proxy_roles()


# --------------------- 场域：supplier（供应商管理 / 切商 / 报价 / 配额） --------------------- #
@mcp.tool()
@safe
def osw_inquiry_link(spu_ids: list) -> dict:
    """[竞价] **取 SPU 的「邀请商家报价」竞价链接**（发给商家，他们打开即可对该 SPU 报供货价）。
    链接形如 `https://jxinquiry.jd.com/detail/index?inquiryId=…`，**inquiryId 与 SPU 一一对应、长期稳定**。
    实证：链接本就随商品列表下发（operateButtonVOMap.productInviteLinkButtonInfo），点按钮只是开新窗，不产生新单。
    顺带返回 projectId(标的ID) —— 供应商管理接口必需，只能从这里拿。show!=1 的返回 None(不可用)。"""
    ids = [str(s).strip() for s in (spu_ids or []) if str(s).strip()]
    if not ids:
        raise BlacklightError("spu_ids 不能为空")
    res = osw_product.product_list(page=1, page_size=100, product_state=None, product_ids=ids)
    got = {str(r.get("productId")): r for r in (res.get("rows") or [])}
    rows = [{"spuId": s, "name": (got[s].get("name") if s in got else None),
             "竞价链接": got[s].get("inquiryLink") if s in got else None,
             "projectId": got[s].get("projectId") if s in got else None}
            for s in ids]
    return {"requested": len(ids), "found": len(got),
            "missing": [s for s in ids if s not in got], "rows": rows}


@mcp.tool()
@safe
def osw_supplier_list(spu_id: str, project_id: str = None, page: int = 1, page_size: int = 50) -> dict:
    """[供应商管理] **该 SPU 下每个 SKU 当前在供的供应商**（一行一个 SKU）。
    含 采购价(元)/物流模式/实时库存 + **配额三元组**：商家配额、采销配额(0=未配置)、生效配额=min(两者)。
    project_id 不传会自动按 SPU 反查（多一次商品列表调用），批量时建议显式传以省调用。"""
    return osw_supplier.spu_supply(spu_id, project_id=project_id, page=page, page_size=page_size)


@mcp.tool()
@safe
def osw_sku_quotes(spu_id: str, sku_id: str, project_id: str = None,
                   sku_idx: str = "1", page: int = 1, page_size: int = 50) -> dict:
    """[供应商管理] **该 SKU 收到的全部报价**（一行一个供应商，多供应商竞价/切商看这里）。
    sku_id=大店SKUID。★自动切商规则：低价切商 / 库存不可用 / 商家SKU状态不可用 / **配额切商**——
    生效配额耗尽自动切到还有配额的商家；都具备时按采纳先后顺序供货。
    返回含 id/inquiryId/lineId/quotationId/version 等主键，供后续采纳·取消采纳·改配额使用。"""
    return osw_supplier.sku_quotes(spu_id, sku_id, project_id=project_id,
                                   sku_idx=sku_idx, page=page, page_size=page_size)


@mcp.tool()
@safe
def osw_pool_claim_plan(project_id: str, hunter_erp: str, shotgun_erp: str,
                        price_map: dict = None, multiplier: float = 1.8, remark: str = "") -> dict:
    """[认领·只读] 拉标的详情→回填 猎人/猎枪 erp + 每SKU售价→算毛利→出**提交预览 + confirm_token + specWarnings**。
    price_map={venderSkuId: 售价}**可选**——未给的 SKU 自动按 **采购价上限×multiplier→末尾向上取.99**(默认 1.8) 定价。
    先看 specWarnings(同规格异价)，核对 preview(售价/来源→毛利)后传 osw_pool_claim_dryrun→claim。"""
    return osw_selection.plan_claim(project_id, hunter_erp, shotgun_erp,
                                    price_map=price_map, multiplier=multiplier, remark=remark)


@mcp.tool()
@safe
def osw_pool_claim_dryrun(project_id: str, hunter_erp: str, shotgun_erp: str,
                          price_map: dict = None, multiplier: float = 1.8, remark: str = "") -> dict:
    """[认领·DRY-RUN] 组装 modifyProject(opt:1) 但**不发送**，回显 payload_body + confirm_token。
    price_map 可选；未给的 SKU 按 采购价上限×multiplier→.99 自动定价（默认 1.8）。"""
    return osw_selection.claim_dryrun(project_id, hunter_erp, shotgun_erp,
                                      price_map=price_map, multiplier=multiplier, remark=remark)


@mcp.tool()
def osw_pool_claim(project_id: str, hunter_erp: str, shotgun_erp: str,
                   price_map: dict = None, multiplier: float = 1.8, confirm: str = "", remark: str = "") -> dict:
    """[认领·真写] **真提交认领**（modifyProject opt:1，推进选品流程①发起→②寻源）。
    price_map 可选(未给按 采购价上限×multiplier→.99)。需相同参数先 osw_pool_claim_dryrun 拿 confirm_token 再带 confirm。
    ⚠️未活体验证：首次务必单标的+核对回执（材质/包装等采销表单字段不在提交体，首单需人工核对完整性）。"""
    try:
        return osw_selection.claim(project_id, hunter_erp, shotgun_erp,
                                   price_map=price_map, multiplier=multiplier, confirm=confirm, remark=remark)
    except BlacklightError as e:
        return {"executed": False, "reason": str(e)}


@mcp.tool()
@safe
def osw_pool_reject_dryrun(vproj_id: str, refuse_type: int, check_msg: str) -> dict:
    """[驳回·DRY-RUN] 组装 refuseVenderTask 但**不发送**，回显 payload + confirm_token。
    refuse_type: **20**=采购价没竞争力 / **30**=商品信息填写错误 / **40**=大店已有同款。check_msg=驳回理由文本。"""
    return osw_selection.reject_dryrun(vproj_id, refuse_type, check_msg)


@mcp.tool()
def osw_pool_reject(vproj_id: str, refuse_type: int, check_msg: str, confirm: str = "") -> dict:
    """[驳回·真写] **真驳回标的**（refuseVenderTask，退回供应商修改）。refuse_type 20/30/40，check_msg=理由。
    需相同参数先 osw_pool_reject_dryrun 拿 confirm_token 再带 confirm。⚠️未活体验证：首次单标的+核对回执。"""
    try:
        return osw_selection.reject(vproj_id, refuse_type, check_msg, confirm=confirm)
    except BlacklightError as e:
        return {"executed": False, "reason": str(e)}


@mcp.tool()
@safe
def osw_pool_cancel_dryrun(project_id: str) -> dict:
    """[取消认领·DRY-RUN] 读当前 taskVersion/status，组装 controlProject(opt:20) 但**不发送**，回显 payload + confirm_token。
    取消后标的退回**发起态(status→0)**，可重新认领(osw_pool_claim)或驳回。用于「已认领想撤回重认」。"""
    return osw_selection.cancel_dryrun(project_id)


@mcp.tool()
def osw_pool_cancel(project_id: str, confirm: str = "") -> dict:
    """[取消认领·真写] **撤销已认领标的**（controlProject opt:20），退回发起态(status→0)可重新认领。taskVersion 自动取当前值。
    需相同 project_id 先 osw_pool_cancel_dryrun 拿 confirm_token 再带 confirm。@audited·已活体验证(2026-07-24 取消3品再重认)。"""
    try:
        return osw_selection.cancel(project_id, confirm=confirm)
    except BlacklightError as e:
        return {"executed": False, "reason": str(e)}


@mcp.tool()
@safe
def osw_pool_stock_dryrun(project_id: str, spu_idx: str = "1") -> dict:
    """[铺货·DRY-RUN] **平台预检**（copyWare isCheck=True，不真铺货）：校验该标的能否铺货上品。
    返回 {ready, precheck, confirm_token}——ready=True(预检通过)才出 token；ready=False 时 precheck 给拦截原因(如"需先完成采纳")。
    链路：公共商品池认领→选品流程→待铺货采纳(adopt)→**铺货(copyWare)**。"""
    return osw_selection.stock_dryrun(project_id, spu_idx=spu_idx)


@mcp.tool()
def osw_pool_stock(project_id: str, spu_idx: str = "1", confirm: str = "") -> dict:
    """[铺货·真写] **真铺货上品**（copyWare isCheck=False）。需相同参数先 osw_pool_stock_dryrun 拿 confirm_token(预检通过才有) 再带 confirm。
    @audited·⚠️未活体验证：首次务必单标的+核对回执+到商品列表确认已上品。"""
    try:
        return osw_selection.stock(project_id, spu_idx=spu_idx, confirm=confirm)
    except BlacklightError as e:
        return {"executed": False, "reason": str(e)}


# ---------------- 在售/下架 编辑商品（gx-pc 加载 / gmall 保存） ---------------- #
@mcp.tool()
@safe
def osw_ware_edit_get(ware_id: str, raw: bool = False) -> dict:
    """[编辑商品·读] **加载在售/下架商品的可编辑整品数据**（编辑商品页 api_ware_edit_detail）。
    补 osw_product_* 没有的逐 SKU 编辑字段：返回 SPU(title长标题/brandName/itemNum货号/jdPrice/stockNum) +
    skus[{skuId/jdPrice京东价/purchasePrice采购价/**barCode(UPC)/sellMin(最低零售价)/outerId(商家SKU)/shortTitle(短标题)**/stockNum库存/enable启用/saleAttr(尺寸-颜色)}]。
    raw=True 附完整原始 data（供整品保存回填）。写保存见 osw_ware_edit_plan/save。"""
    return osw_ware_edit.ware_load(ware_id, raw=raw)


@mcp.tool()
@safe
def osw_ware_edit_plan(ware_id: str, sku_edits: dict = None, spu_edits: dict = None,
                       attr_edits: dict = None) -> dict:
    """[编辑商品·只读] 加载整品→应用编辑→重建 gmall ware/save 整品 body→**不变量守门**→出 changes(改动diff) + confirm_token。
    **仅放开这些字段**：sku_edits={skuId:{**shortTitle短标题**?, **enable启用**(1/0)?}}；spu_edits={**maxBuyTimes24h限购**?, **transportId运费模板**?, **promiseId时效模板**?}；
    attr_edits={valueId:{**name名称**?, **seq顺序**?}}（**尺寸/颜色 名称·顺序**；valueId/当前值见 osw_ware_edit_get 的 saleAttrs）。
    其余（图片/富文本/京东价/采购价）原样回填；守门确保图片/SKU/富文本/属性无丢失。核对 changes 后传 osw_ware_edit_save。"""
    return osw_ware_edit.plan_ware_edit(ware_id, sku_edits=sku_edits, spu_edits=spu_edits, attr_edits=attr_edits)


@mcp.tool()
def osw_ware_edit_save(ware_id: str, sku_edits: dict = None, spu_edits: dict = None,
                       attr_edits: dict = None, confirm: str = "") -> dict:
    """[编辑商品·真写] **真保存编辑商品**（gmall ware/save，**整品覆盖**）。放开 短标题/启用(SKU) + 24h限购/运费模板/时效模板(SPU) + 尺寸/颜色 名称·顺序(attr_edits)。
    需相同参数先 osw_ware_edit_plan 拿 confirm_token 再带 confirm；发送前再不变量守门一次。@audited。
    ⚠️未活体验证+整品覆盖：首次务必**单字段小改** + 保存后 osw_ware_edit_get 读回比对确认无副作用。"""
    try:
        return osw_ware_edit.ware_save(ware_id, sku_edits=sku_edits, spu_edits=spu_edits,
                                       attr_edits=attr_edits, confirm=confirm)
    except BlacklightError as e:
        return {"executed": False, "reason": str(e)}


@mcp.tool()
def osw_margin_reconcile(sku_ids: list) -> dict:
    """[归因前置·必跑] **对账**：校验「逐项 jxReward 求和」是否等于权威汇总 jxCouponSum/jxPromoSum。
    ⚠️**任何券促归因/摘券方案之前先跑这个**。promotions 里会挂**不计入当前到手价**的促销（最典型是
    `便宜包邮`——频道内价格）；直接按列表求和会把它算成头号真凶（实证虚假归因 123 款/−1178 元，
    而它一分钱没影响毛利）。返回 mismatched[].嫌疑项 = 剔掉就能对平的单项，归因时排除。"""
    return osw_margin.reconcile(sku_ids)


@mcp.tool()
def osw_protected_list() -> dict:
    """[禁止触碰清单] 列出已拍板保留的结构性决策（有意的引流款/负毛利/不许退的活动）。
    亏损榜不知道哪些负毛利是**有意为之** —— 摘券/改价/退促方案生成前先过这个清单。
    规则按券名/活动ID匹配，能自动覆盖后续新进同一活动的 SKU（静态名单做不到）。"""
    return osw_protected.load(refresh=True)


@mcp.tool()
def osw_protected_check(sku_ids: list, action: str = "strip") -> dict:
    """[禁止触碰清单] 批量查这批 SKU 里哪些是**已决策保留**的，返回 {allowed, blocked}。
    **action = 你打算做的动作**：strip(摘券/退促，默认) / enroll(新增报名) / reprice(改价)。
    同一条规则可以只拦一部分动作——例如 5.9-5 钩子券「存量不许摘，但新场次允许提报」。
    blocked 的不要排进方案；要动先跟用户确认并更新清单。"""
    return osw_protected.filter_plan(sku_ids, osw_margin.query_pricing_batch([str(s) for s in sku_ids]),
                                     action=action)


@mcp.tool()
def osw_protected_add(reason: str, sku_id: str = "", rule_id: str = "",
                      rule_type: str = "", match: str = "", actions: list = None) -> dict:
    """[禁止触碰清单] 加保护。给 sku_id=保护单款；给 rule_id+rule_type+match=加规则
    （rule_type: coupon_name/promo_name/campaign_id/sku）。**规则优于名单**——按券名保护能自动覆盖
    后续新进同一张引流券的商品。reason 写清为什么（谁、哪天定的）。
    actions=要拦的动作，取值 strip/enroll/reprice，**缺省 ['strip','reprice']**（保留存量但允许继续报名）。"""
    if sku_id:
        return osw_protected.add_sku(sku_id, reason, actions=actions)
    if rule_id and rule_type and match:
        return osw_protected.add_rule(rule_id, rule_type, match, reason, actions=actions)
    return {"ok": False, "reason": "要么给 sku_id，要么给 rule_id+rule_type+match"}


@mcp.tool()
@safe
def osw_protected_audit(limit: int = 3000) -> dict:
    """[禁止触碰清单] **保护敞口自检**：拿当前全量亏损款的实时券促，看每条规则还能拦住几个。

    规则按**券名子串**匹配，而券名会改版——改了之后保护**静默失效**，不报错、只是从此拦不住任何东西。
    本清单出过一次（券名 `4.01-4元_东大` 失配，47 个引流款裸奔）。**动止亏方案前先跑一次**。

    结论分两档：🔴`规则逻辑有问题`=券名在样本里却没拦住（确凿 bug）；
    🟡`样本中无对应券`=可能改名也可能这批货没在亏损榜，**需人工看 `样本券名` 分辨**。"""
    from blacklight.core import protected as _p
    from blacklight.osw import margin as _m
    low = _m.list_low_margin(limit=limit, page_size=100)
    skus = [str(r["skuId"]) for r in (low.get("rows") or [])]
    if not skus:
        return {"样本数": 0, "note": "当前无亏损款，无从判断规则是否仍生效"}
    return _p.audit_rules(_m.query_pricing_batch(skus))


@mcp.tool()
def osw_protected_remove(rule_or_sku: str) -> dict:
    """[禁止触碰清单] 解除保护（按 rule id 或 skuId）。解除后该 SKU 会重新进入止亏方案。"""
    return osw_protected.remove(rule_or_sku)


# ---------------- 带图评价（osw.jd.com/picstart，内部代号 aievalandsales） ---------------- #
# 全链路：targets(待补清单) → collect_plan(输入表) → collect_run(采同品晒图好评)
#        → collect_read(读回) → import_dryrun → import(提交) → tasks(回读 330 生效)
# ⚠️该功能页面标注为保密项目，结论只在内部用，别外传/别对商家提。

@mcp.tool()
@safe
def osw_pic_userinfo() -> dict:
    """[带图评价] 我的身份/权限/AI额度。`can_import=False` 就别往下跑了——采完也提交不了。"""
    return pic_client.user_info()


@mcp.tool()
@safe
def osw_pic_targets(limit: int = 200, page_size: int = 100, sku_ids: list = None,
                    spu_ids: list = None, include_has_eval: bool = False,
                    max_pages: int = 200) -> dict:
    """[带图评价] ★**数据源：名下还没有带图评价的 SKU**（querySkuByPage，imageFilter=1）。

    每行 {sku_id, spu_id, sku_name, sku_image, used(已建几条), result_type/result_msg(平台预校验结论)}。
    `include_has_eval=True` 改成看全部（含已有评价的）。`limit=0` 表示不设上限翻到底。
    ⚠️**页内条数可能少于 page_size**（服务端页内过滤），别把"这页短了"当到底了；
    `short_pages` 多且 count<<total 时要么调大 limit 要么按 spu 分片拉。"""
    return pic_client.targets(limit=limit, page_size=page_size, sku_ids=sku_ids, spu_ids=spu_ids,
                              image_filter=0 if include_has_eval else 1, max_pages=max_pages)


@mcp.tool()
@safe
def osw_pic_collect_plan(limit: int = 100, sku_ids: list = None, out_path: str = None) -> dict:
    """[带图评价] 把待补清单写成**采集器输入表**（SKUID/商品名称）。返回 input_path，喂给 osw_pic_collect_cmd。"""
    from blacklight.pic import collect as _c
    items = ([{"sku_id": s} for s in sku_ids] if sku_ids
             else pic_client.targets(limit=limit)["items"])
    return _c.plan_input(items, out_path)


@mcp.tool()
@safe
def osw_pic_collect_cmd(input_path: str, output_path: str = None, headless: bool = True) -> dict:
    """[带图评价] 生成采集命令（**不执行**）。采一条 SKU 3~5 秒，几百条几十分钟——
    请把 cmd_str **丢后台跑**，再用 osw_pic_collect_status 看进度，别在工具里同步等。"""
    from blacklight.pic import collect as _c
    return _c.collector_command(input_path, output_path, headless=headless)


@mcp.tool()
@safe
def osw_pic_collect_status() -> dict:
    """[带图评价] 采集进度：progress.json 的 done/success/empty + run.log 尾巴。"""
    from blacklight.pic import collect as _c
    return _c.collector_status()


@mcp.tool()
@safe
def osw_pic_collect_read(output_path: str, require_text: bool = True,
                         allow_review: bool = False, screen: bool = True) -> dict:
    """[带图评价] 读采集输出表 → **过闸**后分四桶：`ok`(可提交) / `needs_text`(有图无文，交 osw_pic_ai_text) /
    `review`(弱负面·极限词，人工看) / `dropped`(图文全空·无图·贬损违规，**已剔除**，每条带 reason)。

    采集器对采不到的 SKU **会写空行占位**，这里默认剔掉。`drop_reasons` 给原因分布。
    `screen=False` 退回纯读表不做判断（不建议）。"""
    from blacklight.pic import collect as _c
    return _c.read_output(output_path, screen=screen, require_text=require_text,
                          allow_review=allow_review)


@mcp.tool()
@safe
def osw_pic_screen(rows: list, require_text: bool = True, allow_review: bool = False,
                   deep: bool = False) -> dict:
    """[带图评价] **文本/空值闸门**（任何来源的行都能过：采集的、AI 生的、人工填的）。

    规则层拦：图文全空/无图/无文、贬损负面词、否定+正面(委婉贬损)、退货售后信号、
    联系方式引流竞品、手机号身份证、灌水重复字、默认好评、极限词。
    已做误杀防护（"没有色差""不掉色""差不多"这类肯定型短语先挖掉再匹配）。
    `deep=True` 再加一道**内网大模型语义复核**抓反讽/明褒暗贬——需环境变量 JD_LLM_GW_KEY，
    且网关有限流（串行+退避，慢）。"""
    from blacklight.pic import screen as _s
    if deep:
        return _s.screen_rows_deep(rows, require_text=require_text, allow_review=allow_review)
    return _s.screen_rows(rows, require_text=require_text, allow_review=allow_review)


@mcp.tool()
@safe
def osw_pic_export_template(rows: list, path: str = None, dropped: list = None) -> dict:
    """[带图评价] 把行写成**平台批量导入模板**（SKUID/商品名称/评价文本/实拍图1..9）。
    只写传进来的行——请传体检后的 `ok` 桶。传 `dropped` 会另存一页「已剔除」含剔除原因，供人工回看。"""
    from blacklight.pic import collect as _c
    return _c.write_template(rows, path, dropped)


@mcp.tool()
@safe
def osw_pic_ai_text(sku_id: str, images: list) -> dict:
    """[带图评价] 平台 AI 按实拍图生成评价文案（同品采不到文案时的兜底）。消耗 AI 额度，不提交。
    ⚠️契约反解自前端 bundle、**未活体验证**，首次用先单条看返回。"""
    return pic_client.ai_generate(sku_id, images)


@mcp.tool()
@safe
def osw_pic_import_dryrun(rows: list, check_quota: bool = True) -> dict:
    """[带图评价·dry-run] 提交前体检：格式(≤1000字/图必须是URL/≤9张) + **每 SKU 剩余条数配额** + 槽位排布。
    rows=[{sku_id, eval_content, images:[url...]}]。回 confirm_token。"""
    return pic_client.import_rows_dryrun(rows, check_quota=check_quota)


@mcp.tool()
@safe
def osw_pic_import(rows: list, confirm: str = "", check_quota: bool = True) -> dict:
    """[带图评价·写] **真提交**带图评价（逐条串行）。需相同 rows 先 osw_pic_import_dryrun 拿 confirm_token。
    ★受理≠生效：提交完隔几分钟用 osw_pic_tasks 看 taskStatus 是否到 **330 生效**。"""
    return pic_client.import_rows(rows, confirm=confirm, check_quota=check_quota)


@mcp.tool()
@safe
def osw_llm_status() -> dict:
    """[内网大模型网关] 有没有 key / 在册模型 / 每日额度（300w token）。不消耗额度。
    密钥来源优先级：环境变量 JD_LLM_GW_KEY → runtime/credentials.json → **自动从 o2 模型网关取**。"""
    from blacklight.llm import gateway as _g
    return _g.status()


@mcp.tool()
@safe
def osw_llm_key_refresh(key_id: str = None) -> dict:
    """[内网大模型网关] 从 o2 模型网关**重新取 API Key** 并落盘（key 轮换/失效后用）。

    `key_id` 就是 o2 那个页面 URL 里的 `id=`（不传用上次存的，默认 17681）。
    返回体只给掩码值，完整 key 只写进 gitignore 的 runtime/credentials.json。"""
    from blacklight.llm import gateway as _g
    return _g.fetch_key_from_o2(key_id)


@mcp.tool()
@safe
def osw_llm_image(prompt: str, source_images: list = None, stem: str = "gen",
                  out_dir: str = None) -> dict:
    """[内网大模型网关] 生图 → **落盘**，返回文件路径（不返 base64，一张 1~2MB 会撑爆上下文）。

    给了 `source_images`（本地路径/http URL）就走**图生图**（拿商品主图生场景/实拍风格图），
    否则文生图。实测 24~28s/张、1024×1024 PNG。⚠️网关限流，别并发。
    ★生成的图**不能直接提交带图评价**（平台只收 URL 不收 base64）——要先进图片空间，
    看 osw_pic_imgzone_upload_howto。"""
    from blacklight.llm import gateway as _g
    if source_images:
        return _g.image_edit(prompt, source_images, out_dir=out_dir, stem=stem)
    return _g.image_generate(prompt, out_dir=out_dir, stem=stem)


@mcp.tool()
@safe
def osw_pic_imgzone_list(cate_id: str = "0", page: int = 1, page_size: int = 50,
                         only_image: bool = True) -> dict:
    """[图片空间] 列分类下的图片（含**完整 CDN URL**，可直接喂 osw_pic_import）+ 子分类。
    cate_id='0' 是根目录。"""
    from blacklight.pic import imgzone as _z
    return _z.list_images(cate_id, page, page_size, only_image)


@mcp.tool()
@safe
def osw_pic_imgzone_find(name: str, cate_id: str = "0", max_pages: int = 5) -> dict:
    """[图片空间] 按文件名找图 → 完整 URL。**上传后读回 URL 就用这个**。"""
    from blacklight.pic import imgzone as _z
    rows = _z.find_by_name(name, cate_id, max_pages)
    return {"count": len(rows), "images": rows}


@mcp.tool()
@safe
def osw_pic_imgzone_upload(source: str, file_name: str = None, cate_id: str = "0") -> dict:
    """[图片空间][写] 传图 → **直接返回可提交带图评价的 CDN URL**。

    `source` 收 本地路径 / base64 / dataURL —— osw_llm_image 生的图可以不落盘直接接过来。
    jpg/png/jpeg/webp ≤20M。传完想验活用 osw_pic_imgzone_find（HEAD 会 403，本工具链一律 GET）。"""
    from blacklight.pic import imgzone as _z
    return _z.upload(source, file_name, cate_id)


@mcp.tool()
@safe
def osw_pic_imgzone_delete(image_ids: list, parent_cate_id: str = "0",
                           confirm_token: str = None) -> dict:
    """[图片空间][写] 删图。**不可恢复**；平台明确警告删掉已被商品引用的图会导致商品展示异常。
    默认 dry-run，要拿 confirm_token 再执行。"""
    from blacklight.core.base import ConfirmGate
    from blacklight.pic import imgzone as _z
    gate = ConfirmGate("pic/imgzone_delete")
    ids = [str(x) for x in (image_ids or [])]
    if not ids:
        raise BlacklightError("image_ids 不能为空")
    body = {"image_ids": ids, "parent_cate_id": str(parent_cate_id)}
    tok = gate.body_token(body)
    if confirm_token != tok:
        return {"dry_run": True, "will_delete": ids, "irreversible": True,
                "warning": "删掉被商品引用的图会导致商品展示异常，先确认这些图没在用",
                "confirm_token": tok}
    return _z.delete(ids, parent_cate_id)


@mcp.tool()
@safe
def osw_pic_tasks(page: int = 1, page_size: int = 20, sku_ids: list = None, status: int = None,
                  input_erp: str = None, start_time: str = None, end_time: str = None) -> dict:
    """[带图评价] 已提交任务 + 审核态回读。status 枚举：1机审中/3机审通过/4机审拒绝/6人审通过/
    11已有评价/13同步失败/20|220|320创建失败/210|310处理中/**330生效**。"""
    return pic_client.task_list(page=page, page_size=page_size, sku_ids=sku_ids, status=status,
                                input_erp=input_erp, start_time=start_time, end_time=end_time)


@mcp.tool()
@safe
def osw_pic_task_stats(sku_ids: list = None, input_erp: str = None, start_time: str = None,
                       end_time: str = None, scan_pages: int = 20) -> dict:
    """[带图评价] 任务状态分布 + 生效率（补完一批后**用这个验收**，别只看提交回执）。"""
    return pic_client.task_stats(sku_ids=sku_ids, input_erp=input_erp, start_time=start_time,
                                 end_time=end_time, scan_pages=scan_pages)


# --------------------------------------------------------------------------- #
# 商品素材维护（materialCenter，ware-material-jdm 跨域子应用；网关同 sff 换 appId）
# --------------------------------------------------------------------------- #
@mcp.tool()
@safe
def osw_material_gap(types: list = None) -> dict:
    """[商品素材] 各素材位**未维护 SKU 数** —— 页面顶部那块统计。

    2026-08-17 活体：搜索分发/推荐分发 1991、搜索主图 1990、场景图 320、卖点图 172、
    透明图 112、白底图 85（在册 1991 SPU）。**这是 SKU 维度**，与 osw_material_missing
    的 SPU 维度不是一个口径，汇报必须写明。"""
    from blacklight.osw import material as _m
    return _m.gap(types=types)


@mcp.tool()
@safe
def osw_material_list(page: int = 1, page_size: int = 20, product_name: str = None,
                      product_ids: list = None, sku_ids: list = None,
                      category_id: int = None, material_status: dict = None) -> dict:
    """[商品素材] 按 SPU 列商品素材（搜索主图/白底图/透明图/场景图/卖点图/分发位）。

    每行给 `first_sku.materials`（各位的图 URL + 审核态）和 `missing_types`。
    ⚠️**列表接口每个 SPU 只回第一条 SKU 的素材明细**，其余 SKU 是空壳——
    要全部 SKU 用 osw_material_skus 展开。
    `material_status` 形如 {"31": [0]}（0待补充/3审核中/4通过/5驳回）。"""
    from blacklight.osw import material as _m
    return _m.list_spu(page=page, page_size=page_size, product_name=product_name,
                       product_ids=product_ids, sku_ids=sku_ids, category_id=category_id,
                       material_status=material_status)


@mcp.tool()
@safe
def osw_material_missing(material_type: int, page: int = 1, page_size: int = 20) -> dict:
    """[商品素材] 列出**某个素材位待补充**的 SPU。material_type：
    51搜索主图/31白底图/36透明图/32场景图/33卖点图/52搜索分发/53推荐分发。

    total 是 **SPU 维度**（白底图 68），osw_material_gap 是 **SKU 维度**（85）——别混。"""
    from blacklight.osw import material as _m
    return _m.missing(material_type, page=page, page_size=page_size)


@mcp.tool()
@safe
def osw_material_skus(product_id: str) -> dict:
    """[商品素材] 展开一个 SPU 下**全部 SKU** 的素材明细。
    ⚠️返回体里服务端的 count 恒为 0，别拿它判空——看 rows。"""
    from blacklight.osw import material as _m
    return _m.list_sku(product_id)


@mcp.tool()
@safe
def osw_material_tasks() -> dict:
    """[商品素材] 在途素材任务。**批量发起 AI 生成前必看**——在途锁会把后发的请求挡掉，
    表现像平台拒绝（同百补并发报名那个坑）。"""
    from blacklight.osw import material as _m
    return {"running": _m.running_tasks()}


@mcp.tool()
@safe
def osw_material_bind(product_id: str, sku_ids: list, images: list,
                      exist_no_replace: bool = True, confirm_token: str = None) -> dict:
    """[商品素材][写] 把图片空间的图**挂到 SKU 的素材位**。默认 dry-run。

    `images` = [{"url": "<imgzone 的 jfs 路径或完整 URL>", "type": 31, "order": 0}]。
    与 osw_pic_imgzone_upload 直接对接（llm 生图 → 传图床 → 挂位，全程不落盘）。
    ⚠️**此写路径未活体验证**：首次务必单 SKU 单图，之后 osw_material_skus 回读。
    `exist_no_replace=False` 会**覆盖已有素材**。"""
    from blacklight.osw import material as _m
    if confirm_token is None:
        return _m.bind_dryrun(product_id, sku_ids, images, exist_no_replace=exist_no_replace)
    return _m.bind(product_id, sku_ids, images, exist_no_replace=exist_no_replace,
                   confirm=confirm_token)


@mcp.tool()
@safe
def osw_material_ai_generate(sku_ids: list, material_types: list = None,
                             confirm_token: str = None) -> dict:
    """[商品素材][写] 发起**平台 AI 批量生成素材**（≤500 SKU/次）。默认 dry-run。

    平台 AI 只兜三个位：36透明图/31白底图/32场景图（getAiBatchMaterialType 活体口径）。
    ⚠️未活体验证。发起前先 osw_material_tasks 看在途；
    结果 taskStatus=70 是**待采纳**不是生效，要回读素材位确认。"""
    from blacklight.osw import material as _m
    if confirm_token is None:
        return _m.ai_generate_dryrun(sku_ids, material_types or _m.AI_MATERIAL_TYPES)
    return _m.ai_generate(sku_ids, material_types or _m.AI_MATERIAL_TYPES, confirm=confirm_token)


@mcp.tool()
@safe
def osw_material_plan_gen(product_id: str, sku_id: str, material_types: list = None,
                          sell_points: list = None) -> dict:
    """[商品素材] **补齐方案 dry-run**：自动选参考图 + 拼 prompt + 排依赖，**不生图不花钱**。

    默认补 31白底/36透明/32场景×2/33卖点。36 走平台抠图（依赖 31），不是大模型生成。
    参考图优先用**同 SPU 已审核通过的白底图**——拿商品主图当底图会把竞品包装带进生成结果。"""
    from blacklight.osw import material_gen as _g
    return _g.plan(product_id, sku_id, material_types or _g.DEFAULT_SLOTS, sell_points)


@mcp.tool()
@safe
def osw_material_autofill(product_id: str, sku_id: str, material_types: list = None,
                          sell_points: list = None, images_only: bool = False,
                          replace: bool = False, confirm_token: str = None) -> dict:
    """[商品素材][写] 生成 → 质检 → 上传图床 → 挂位，一个 SKU 一次补齐。

    真写需要 osw_material_plan_gen 给的 confirm_token（**不给就只出方案不写**）。
    replace=True 会覆盖已有素材。
    两道护栏已在代码里，不靠调用方记得：① 无 confirm_token 只返回方案
    ② 域函数 `qc_on=True`——每张生成图上传前过质检，不合格重生一次，仍不合格
    **不上传**并标 `blocked_by_qc`（宁可留空位，也别把画错结构的图挂上去）。
    如需人工过一眼再挂，传 `images_only=True` 只出图不挂位——模型确实会改商品结构
    （实撞：一整扇大开门被画成双开门）。"""
    from blacklight.osw import material_gen as _g
    if not images_only and not confirm_token:
        return _g.plan(product_id, sku_id, material_types or _g.DEFAULT_SLOTS, sell_points)
    return _g.autofill(product_id, sku_id, material_types or _g.DEFAULT_SLOTS, sell_points,
                       confirm=confirm_token or "", replace=replace, dry_images_only=images_only)


@mcp.tool()
@safe
def osw_material_matting(img_url: str, to_transparent: bool = True) -> dict:
    """[商品素材] 平台算法抠图：白底图 → 透明图（或反向）。实测 0.7s，回 base64 的 RGBA PNG。

    ⚠️透明图**不要用大模型生成**——模型会画棋盘格假装透明，输出没有 alpha 通道。"""
    from blacklight.osw import material_gen as _g
    from blacklight.core import paths as _p
    import os as _os
    d = _p.exports_dir("material_gen")
    _os.makedirs(d, exist_ok=True)
    out = _os.path.join(d, "matting_%s.png" % abs(hash(img_url)))
    return _g.matting(img_url, _g.MATTING_THROUGH if to_transparent else _g.MATTING_WHITE, out)


@mcp.tool()
@safe
def osw_material_qc(image: str, material_type: int, reference: str = None,
                    expect_texts: list = None) -> dict:
    """[商品素材] **上传前质检**：多模态模型比对生成图与基准图。

    查四类（都是实撞过的失败）：结构是否被改（一整扇门画成双开、一件画成两件）、
    有无第三方品牌、有无不该出现的文字、白底图背景是否纯白且无道具/透明图是否真透明。
    `reference` 建议传该 SPU 已审核通过的白底图；不传就只能查品牌和文字、查不了结构。"""
    from blacklight.osw import material_gen as _g
    return _g.qc(image, material_type, reference=reference, expect_texts=expect_texts)


@mcp.tool()
@safe
def osw_material_sibling_groups(product_id: str, attr_index: int = 1) -> dict:
    """[商品素材] 按**平台「批量应用相同颜色/尺码」的口径**给 SPU 下的 SKU 分组（判素材能否复用）。

    ⚠️**别用"主图相同"当复用判据**：实撞反例——同 SPU 里带木盖/无木盖两个款主图完全一样
    （商家套用了同一张图），照它复用会把无木盖的图挂到带木盖的 SKU 上。
    平台看的是销售属性第 N 项取值，不是主图。"""
    from blacklight.osw import material_gen as _g
    return _g.sibling_groups(product_id, attr_index)


@mcp.tool()
@safe
def osw_material_sellpoints(product_id: str, sku_id: str) -> dict:
    """[商品素材] 读 SKU 的**通用卖点**现值 + 条数上限 + 平台 AI 建议。

    条数上限用服务端给的 `sellMaxNum`（实测 3），单条 ≤8 字。"""
    from blacklight.osw import material_gen as _g
    return _g.sellpoints_current(product_id, sku_id)


@mcp.tool()
@safe
def osw_material_sellpoints_generate(product_id: str, sku_id: str, n: int = None) -> dict:
    """[商品素材] 生成通用卖点文案。**平台 AI 优先，没有才用 llm-gw**。

    自动按「2~8 字、去重、不超 sellMaxNum 条」过滤，并禁止绝对化用语/疗效承诺/价格促销/品牌名。
    ⚠️卖点是对外承诺，上线前必须人工过一遍。"""
    from blacklight.osw import material_gen as _g
    return _g.sellpoints_generate(product_id, sku_id, n)


@mcp.tool()
@safe
def osw_material_sellpoints_save(product_id: str, sku_ids: list, points: list,
                                 confirm_token: str = None) -> dict:
    """[商品素材][写] 保存通用卖点（dsm.media.text.shortTitleSeller.save）。默认 dry-run。
    ⚠️**会覆盖该 SKU 现有卖点**。保存后自动回读比对。"""
    from blacklight.osw import material_gen as _g
    if not confirm_token:
        return _g.sellpoints_save_dryrun(product_id, sku_ids, points)
    return _g.sellpoints_save(product_id, sku_ids, points, confirm=confirm_token)


@mcp.tool()
@safe
def osw_material_reuse(product_id: str, src_sku_id: str, material_types: list = None,
                       scope: str = "appearance", ignore_attrs: list = None,
                       waive_size: bool = True, with_sellpoints: bool = False,
                       replace: bool = False, confirm_token: str = None) -> dict:
    """[商品素材][写] 把源 SKU 的素材复用给**同外观**的兄弟 SKU。默认 dry-run。

    复用的是同一张 imgUrl，不重新生成也不重复占图床。
    分组按**外观签名**（销售属性去掉尺寸维度后比对）——不是按主图（主图相同不代表外观相同），
    也不是平台的单轴口径（它只看一个轴，其它轴的颜色/款式差异不管）。
    `waive_size=True`（默认）：只差尺寸视为同外观；`replace=False`（默认）只填空位，
    不会冲掉目标已审核通过的素材。"""
    from blacklight.osw import material_gen as _g
    if not confirm_token:
        return _g.reuse_plan(product_id, src_sku_id, material_types, scope=scope,
                             ignore_attrs=ignore_attrs, waive_size=waive_size)
    return _g.reuse(product_id, src_sku_id, material_types, scope=scope,
                    ignore_attrs=ignore_attrs, waive_size=waive_size,
                    confirm=confirm_token, with_sellpoints=with_sellpoints, replace=replace)


@mcp.tool()
@safe
def osw_material_spu_gaps(product_id: str, material_types: list = None) -> dict:
    """[商品素材] 整个 SPU 还缺什么：按外观组分「**可传播补齐**」与「**必须生成**」两类。只读。

    ⚠️传播那类是**零成本**的（同组源已有图，直接复用同一张 imgUrl）。
    补素材前先看这个——不看就会漏：批量的待办按源 SKU 算，源已有的位会被整组跳过，
    同组成员的空位补不上，页面上表现为"有的 SKU 还是空的"（2026-08-18 实撞 27 个）。"""
    from blacklight.osw import material_gen as _g
    return _g.spu_gaps(product_id, material_types)


@mcp.tool()
@safe
def osw_material_propagate(product_id: str, src_sku_id: str = None,
                           confirm: bool = False, confirm_token: str = None) -> dict:
    """[商品素材][写] 把**源 SKU 已有**的素材补给同外观组还缺的成员。零生成、零 token（指模型 token）。

    不传 src_sku_id 就对该 SPU 的**所有外观组**做。只填空位（existNoReplace=True），不覆盖已有素材。
    ★★**2026-08-24 起真写要 `confirm_token`**：先不带参数跑一次拿每组的 `confirm_token`（同时看 detail
      确认要补哪些位），再带 `confirm=True` + 该 token 真写。
      以前只要 `confirm=True` 就写，没有"预览-确认绑定"——真写时 token 在函数内部现算现填、永远自证有效，
      等于绕过了同域其它 material 工具的纪律。多组时逐组给 token，一次只确认一组。
    ★失败不再被吞：没挂上的位会出现在每组的 `failed` 里（以前只能从 filled 计数反推）。"""
    from blacklight.osw import material_gen as _g
    gs = _g.appearance_groups(product_id)
    if src_sku_id:
        gs = [g for g in gs if str(g["src"]["sku_id"]) == str(src_sku_id)]
        if not gs:
            raise BlacklightError(f"{src_sku_id} 不是任何外观组的源；先看 osw_material_spu_gaps")
    if not confirm:
        out = [_g.propagate_plan(product_id, g) for g in gs]
        return {"product_id": str(product_id), "groups": len(out),
                "filled": 0,
                "pending": sum(len(i["to"]) for x in out for i in x["items"]),
                "detail": [x for x in out if x["items"]],
                "dry_run": True,
                "note": "真写：带 confirm=True + 对应组的 confirm_token 再调一次（一次一组）。"}
    if not confirm_token:
        raise BlacklightError(
            "真写需要 confirm_token：先不带 confirm 跑一次拿到每组的 confirm_token（顺便看 detail 确认要补哪些位），"
            "再带 confirm=True + confirm_token。（2026-08-24 起收紧：原来裸 confirm=True 就写，"
            "没有预览-确认绑定。）")
    out = [_g.propagate_group(product_id, g, dry_run=False, confirm=confirm_token) for g in gs]
    return {"product_id": str(product_id), "groups": len(out),
            "filled": sum(x["filled"] for x in out),
            "failed": [f for x in out for f in (x.get("failed") or [])],
            "detail": [x for x in out if x["items"]],
            "dry_run": False}


if __name__ == "__main__":
    mcp.run()
