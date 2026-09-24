"""禁止触碰清单 —— 已拍板的结构性决策（有意的引流款/负毛利/不许退的活动）。

**为什么要有这个**：亏损榜是按数据排的，它不知道哪些负毛利是**有意为之**。
2026-07-28 实证踩坑：4.5→到手0.5 的引流款组（用户前一天明确说过保留不动）被按「昨日亏损单」
排到出血榜前三，又生成了一轮摘券建议——而那还是共补券（摘=白丢平台补贴且不可逆）。

**用法**：任何摘券/退促/改价方案生成时先过 `filter_plan()`，命中的标注「已决策保留」而不是排进方案。

**匹配优先按规则、其次按 SKU**：规则匹配（券名/活动ID）能自动覆盖**后续新进同一活动的 SKU**，
静态 SKU 名单做不到——今天新建的商品明天进同一张引流券，规则会自动保护，名单不会。

清单存 **`config/protected.json`**（决策配置，**入库、可追溯**）。
★2026-08-03 从 `runtime/` 迁出：runtime 是机器相关、可再生、不入库的东西（cookie/日志/浏览器 profile），
而本清单是人工拍板、丢了没法再生的结构性决策，混在一起会被 .gitignore 一起挡掉。
旧路径仍自动兼容：只在 runtime/ 里的会在首次写入时迁到 config/。
"""
from __future__ import annotations

import json
import os
import shutil
from typing import Optional

from blacklight.core.paths import config_dir, home, protected_path

_CACHE: Optional[dict] = None

# 保护动作维度 —— 「不许摘」和「不许新报」是两回事（2026-07-28 实证：5.9-5 钩子券既要保留存量、
# 又要按作业主动提报新场次；只有一种语义会把提报也一并拦掉）。
ACTIONS = ("strip", "enroll", "reprice")
ACTION_CN = {"strip": "摘券/退促", "enroll": "新增报名", "reprice": "改价"}
DEFAULT_ACTIONS = ("strip", "reprice")     # 不写 actions 时的默认：拦「摘」和「改价」，放行「新报」

_SEED = {
    "version": 2,
    "_note": ("规则命中即保护。type: coupon_name(券名子串)/promo_name/campaign_id/sku；"
              "actions: 拦哪些动作，取值 strip(摘券退促)/enroll(新增报名)/reprice(改价)，"
              "缺省=['strip','reprice']（保留存量但允许继续报名）"),
    "rules": [
        {
            "id": "cat401-loss-leader",
            "type": "coupon_name",
            "match": "4.01-4元_东大",
            "actions": ["strip", "reprice"],
            "reason": "品类新4.1-4券引流结构（采销担3/平台担1，共补券）。到手价 0.5~0.99 的负毛利是有意的。",
            "decided": "2026-07-27",
            "by": "user",
        },
        {
            "id": "hook59-baoyou",
            "type": "coupon_name",
            "match": "钩子品",
            "actions": ["strip"],
            "reason": ("便宜包邮 满5.9减5 钩子券（共补，我担2.45/客减5）。低价引流款，存量保留不摘；"
                       "但新场次提报是作业要求，故只拦 strip、放行 enroll。"),
            "decided": "2026-07-27",
            "by": "user",
        },
    ],
    "skus": {},
    "campaigns": {},
}


def path() -> str:
    """当前生效的清单路径（config/ 优先；仅存于旧 runtime/ 时先返回旧路径，见 migrate()）。"""
    return protected_path()


def migrate() -> Optional[str]:
    """把只存在于旧 runtime/ 的清单挪到 config/。返回迁移后的新路径，无需迁移则 None。
    用 move 而非 copy，避免两处并存后改了一处、另一处成为过期的「影子决策」。"""
    if os.environ.get("BLACKLIGHT_PROTECTED_FILE", "").strip():
        return None
    legacy = os.path.join(home(), "protected.json")
    new = os.path.join(config_dir(), "protected.json")
    if os.path.exists(legacy) and not os.path.exists(new):
        try:
            shutil.move(legacy, new)
            return new
        except Exception:
            return None
    return None


def load(refresh: bool = False) -> dict:
    """读清单；文件不存在则用种子初始化并落盘。"""
    global _CACHE
    if _CACHE is not None and not refresh:
        return _CACHE
    p = path()
    if not os.path.exists(p):
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump(_SEED, f, ensure_ascii=False, indent=1)
        except Exception:
            pass
        _CACHE = json.loads(json.dumps(_SEED))
        return _CACHE
    try:
        with open(p, encoding="utf-8") as f:
            _CACHE = json.load(f)
    except Exception:
        _CACHE = json.loads(json.dumps(_SEED))
    return _CACHE


def check(sku_id, coupons: Optional[list] = None, promos: Optional[list] = None,
          campaign_ids: Optional[list] = None, action: str = "strip") -> list:
    """返回命中的保护规则 [{id, reason, matched, actions}]；空列表=该动作可以做。

    action：你**打算做什么** —— strip(摘券/退促) / enroll(新增报名) / reprice(改价)。
    只有「规则声明要拦这个动作」才算命中。这样同一条规则可以「存量不许摘、但允许继续报名」
    （5.9-5 钩子券就是这种：存量是有意的引流结构，同时新场次提报是作业要求）。

    coupons/promos 传 `margin.query_pricing` 的 `coupons`/`promotions`（用 name 字段匹配）。
    只传 sku_id 也能查静态名单，但**券名规则要靠 coupons 才能生效**，方案生成务必带上。
    """
    if action not in ACTIONS:
        raise ValueError(f"action 必须是 {ACTIONS} 之一")
    cfg = load()
    sku = str(sku_id)
    hits = []
    if sku in (cfg.get("skus") or {}):
        ent = cfg["skus"][sku]
        ent = ent if isinstance(ent, dict) else {"reason": ent}
        if action in (ent.get("actions") or DEFAULT_ACTIONS):
            hits.append({"id": "sku:" + sku, "reason": ent.get("reason"), "matched": sku,
                         "actions": list(ent.get("actions") or DEFAULT_ACTIONS)})
    names_c = [str(c.get("name") or c.get("couponName") or "") for c in (coupons or [])]
    names_p = [str(c.get("name") or "") for c in (promos or [])]
    cids = [str(x) for x in (campaign_ids or [])]
    for r in (cfg.get("rules") or []):
        t, m = r.get("type"), str(r.get("match") or "")
        if not m:
            continue
        if action not in (r.get("actions") or DEFAULT_ACTIONS):
            continue                       # 该规则不拦这个动作 → 放行
        hit = None
        if t == "coupon_name":
            hit = next((n for n in names_c if m in n), None)
        elif t == "promo_name":
            hit = next((n for n in names_p if m in n), None)
        elif t == "campaign_id":
            hit = m if m in cids else None
        elif t == "sku":
            hit = m if m == sku else None
        if hit:
            hits.append({"id": r.get("id"), "reason": r.get("reason"), "matched": hit,
                         "actions": list(r.get("actions") or DEFAULT_ACTIONS)})
    return hits


def filter_plan(sku_ids: list, pricing: Optional[dict] = None, action: str = "strip") -> dict:
    """批量过滤：返回 {allowed:[skuId], blocked:[{skuId, hits}]}。
    action = 你打算做的动作（strip/enroll/reprice），只拦声明要拦该动作的规则。
    pricing = `margin.query_pricing_batch` 的返回（{skuId: 定价dict}），用于按券名/促销名匹配规则。"""
    allowed, blocked = [], []
    for s in sku_ids:
        s = str(s)
        p = (pricing or {}).get(s) or {}
        hits = check(s, p.get("coupons"), p.get("promotions"), action=action)
        (blocked if hits else allowed).append({"skuId": s, "hits": hits} if hits else s)
    return {"allowed": allowed, "blocked": blocked, "action": action,
            "note": f"blocked 是已拍板「不许{ACTION_CN.get(action, action)}」的结构性决策；"
                    "要动先跟用户确认并更新 protected.json"}


def health_check(rows: list, line: float = 1.0, pricing: Optional[dict] = None) -> dict:
    """★**禁令款体检**：受保护 ≠ 可以无限亏。按「单亏 line 元」分档。

    规则来源：用户 2026-08-10 拍板 ——「禁令款单亏在 1 元以内可接受，
    但如果实际亏损超过 1 元，那就得好好看看了」。这里做成**硬闸**：
    超线的自动进 `over`（待处置队列），不再靠人每次记。

    ⚠️判据用 **`实际单均毛利`**（perOrderAvgGrossProfit），不是预估——
      预估是最坏情况且有盲区，用它分档会把大量线内款误报成超线。

    rows: `triage()`/`list_low_margin()` 的行（要有 skuId / 实际单均毛利 / 近15日单量）。
    line: 可接受的单亏上限（元，正数）。默认 1.0；未来若要按品类分档改这个参数即可。

    2026-08-10 实测基线：28 款受保护中 **13 款超线（−5,295，占 A 桶失血 93%）**、
    15 款线内（−427）—— 少数超线款吃掉了几乎全部的失血。
    """
    from blacklight.osw import margin as _mt
    ids = [str(r.get("skuId") or r.get("sku")) for r in rows]
    byid = {str(r.get("skuId") or r.get("sku")): r for r in rows}
    pricing = pricing or _mt.query_pricing_batch(ids)
    res = filter_plan(ids, pricing, action="strip")
    blocked = {b["skuId"]: b for b in (res.get("blocked") or [])}

    over, ok = [], []
    for s, b in blocked.items():
        r = byid.get(s) or {}
        per = r.get("实际单均毛利")
        q = r.get("近15日单量") or 0
        item = {"sku": s, "name": r.get("name"),
                "预估毛利": r.get("预估毛利"), "实际单均毛利": per,
                "近15日单量": q, "近15日亏损单": r.get("近15日亏损单"),
                "15日实际失血": round(per * q, 0) if (per is not None and per < 0) else 0,
                "命中规则": [h.get("id") for h in (b.get("hits") or [])]}
        (over if (per is not None and per < -abs(line)) else ok).append(item)
    over.sort(key=lambda x: x["15日实际失血"])
    ok.sort(key=lambda x: (x["实际单均毛利"] if x["实际单均毛利"] is not None else 0))
    return {"line": line, "受保护": len(blocked),
            "超线": len(over), "线内": len(ok),
            "超线失血": round(sum(x["15日实际失血"] for x in over), 0),
            "线内失血": round(sum(x["15日实际失血"] for x in ok), 0),
            "over": over, "ok": ok,
            "_判据": "用实际单均毛利分档（非预估）；超线 = 单亏 > %.2f 元 → 进待处置队列" % abs(line)}


def audit_rules(pricing: dict, action: str = "strip") -> dict:
    """**保护敞口自检**：拿一批 SKU 的实时券促数据，看每条规则**现在还能匹配到几个**。

    为什么必须有：规则按**券名子串**匹配，而券名会改。改了之后保护**静默失效**——
    不报错、不告警，只是从此拦不住任何东西。本清单里 `cat401-loss-leader` 就是活例子：
    券名从 `4.01-4元_东大` 变了之后失配，**47 个引流款裸奔**，直到人肉发现才补规则。

    ⚠️★**本函数只能给线索，给不了判决** —— 样本里没匹配上，可能是券名失配，
    也可能只是**这批样本没覆盖到那批货**。2026-08-05 自测就踩了：拿 40 个亏损款跑，
    5 条规则全 0 匹配、看着像全面失守；实际那 40 个款压根不带这些券（样本只有 1 种券名）。

    所以结论分两档，别混：
      🔴 **规则逻辑有问题** = 样本里**存在**含该子串的券名、规则却没拦住 → 确凿 bug（多半 actions/type 配错）
      🟡 **样本中无对应券** = 券名根本没出现 → 可能改名、也可能这批货不在样本里，**必须人工分辨**
    （曾用"样本够不够大"当门槛，把 169 个=全部亏损款判成"不够格"，反而挡住了真信号，已废弃。）

    `pricing` = `margin.query_pricing_batch()` 的返回 `{skuId: {coupons, promotions, ...}}`。
    喂**全量亏损款**最有意义（那正是止亏方案的候选池，保护失效在这里最致命）。"""
    cfg = load()
    rules = cfg.get("rules") or []
    counts = {r.get("id"): 0 for r in rules}
    names = set()
    for sku, p in (pricing or {}).items():
        p = p or {}
        for c in (p.get("coupons") or []):
            n = c.get("name") or c.get("couponName")
            if n:
                names.add(str(n))
        for h in check(sku, p.get("coupons"), p.get("promotions"), action=action):
            if h["id"] in counts:
                counts[h["id"]] += 1
    rows = []
    for r in rules:
        rid, m = r.get("id"), str(r.get("match") or "")
        n = counts.get(rid, 0)
        is_coupon = r.get("type") == "coupon_name" and m
        # 券名类规则再给一条更细的线索：样本里有没有**任何**券名包含这个子串
        substr_seen = any(m in x for x in names) if is_coupon else None
        row = {"id": rid, "type": r.get("type"), "match": m, "matched_skus": n,
               "样本中有券名含此子串": substr_seen, "reason": (r.get("reason") or "")[:60]}
        rows.append(row)
    # 分两档，别用"样本够不够大"这种拍脑袋门槛（曾把 169 个=全部亏损款判成"不够格"）。
    # 真正有信息量的是：**券在样本里出现了，规则却没拦住** —— 那是规则本身的问题，铁定要修。
    broken = [x["id"] for x in rows if x["matched_skus"] == 0 and x["样本中有券名含此子串"] is True]
    absent = [x["id"] for x in rows if x["matched_skus"] == 0 and x["样本中有券名含此子串"] is not True]
    parts = []
    if broken:
        parts.append(f"🔴 **{len(broken)} 条规则匹配逻辑有问题**：{broken} —— "
                     f"样本里**存在**含该子串的券名，规则却一个都没拦住。"
                     f"多半是 `actions` 没含本次动作，或 type 写错。这条是硬问题，去修。")
    if absent:
        parts.append(f"🟡 {len(absent)} 条规则在本样本里没出现对应券名：{absent} —— "
                     f"**两种可能，得人工分辨**：①券名改了（保护已静默失效，本清单出过一次、"
                     f"47 个引流款裸奔）②这批货本来就不在样本范围内（比如当前不亏损）。"
                     f"对着 `样本券名` 看有没有形近的（如 4.1-4 vs 4.01-4）。")
    return {"样本数": len(pricing or {}), "券名种类": len(names),
            "action": action, "rules": rows,
            "规则逻辑有问题": broken, "样本中无对应券": absent,
            "敞口提示": ("；".join(parts) or None),
            # ★把样本里的券名**全列出来**，让人/agent 自己比对形近的（如 限品类4.1-4 vs 限品类7.1-7）。
            # 试过自动找"形近券名"，中文下 3 字公共子串信息量太低——既漏（"用增"只2字）
            # 又噪（"京喜冲单券"也被算成同族）。券名通常就几种，直接读比启发式准得多。
            "样本券名": sorted(names),
            "_边界": "0 匹配**不等于**规则失效——样本没覆盖到那批货也会 0。"
                     "只有『券名在样本里、规则却没拦住』才是确凿问题。"}


def add_sku(sku_id, reason: str, actions: Optional[list] = None) -> dict:
    """把某 SKU 加进保护名单。actions 缺省 ['strip','reprice']（保留存量但允许继续报名）。"""
    acts = list(actions or DEFAULT_ACTIONS)
    bad = [a for a in acts if a not in ACTIONS]
    if bad:
        raise ValueError(f"actions 含非法值 {bad}，合法值：{ACTIONS}")
    cfg = load(refresh=True)
    cfg.setdefault("skus", {})[str(sku_id)] = {"reason": reason, "actions": acts}
    _save(cfg)
    return {"ok": True, "skuId": str(sku_id), "reason": reason, "actions": acts}


def add_rule(rule_id: str, rule_type: str, match: str, reason: str,
             actions: Optional[list] = None, by: str = "user") -> dict:
    """加一条规则。rule_type: coupon_name / promo_name / campaign_id / sku。
    actions 缺省 ['strip','reprice']；要连新增报名一起拦就传 ['strip','enroll','reprice']。"""
    if rule_type not in ("coupon_name", "promo_name", "campaign_id", "sku"):
        raise ValueError("rule_type 必须是 coupon_name/promo_name/campaign_id/sku")
    acts = list(actions or DEFAULT_ACTIONS)
    bad = [a for a in acts if a not in ACTIONS]
    if bad:
        raise ValueError(f"actions 含非法值 {bad}，合法值：{ACTIONS}")
    cfg = load(refresh=True)
    cfg.setdefault("rules", [])
    cfg["rules"] = [r for r in cfg["rules"] if r.get("id") != rule_id]
    cfg["rules"].append({"id": rule_id, "type": rule_type, "match": match,
                         "actions": acts, "reason": reason, "by": by})
    _save(cfg)
    return {"ok": True, "rule": rule_id, "actions": acts, "total_rules": len(cfg["rules"])}


def remove(rule_or_sku: str) -> dict:
    """解除保护（按 rule id 或 skuId）。"""
    cfg = load(refresh=True)
    n0 = len(cfg.get("rules") or []) + len(cfg.get("skus") or {})
    cfg["rules"] = [r for r in (cfg.get("rules") or []) if r.get("id") != rule_or_sku]
    (cfg.get("skus") or {}).pop(str(rule_or_sku), None)
    _save(cfg)
    n1 = len(cfg.get("rules") or []) + len(cfg.get("skus") or {})
    return {"ok": n1 < n0, "removed": n0 - n1}


def _save(cfg: dict) -> None:
    global _CACHE
    migrate()                      # 落盘前先把旧 runtime/ 的挪走，保证只有一份权威清单
    with open(path(), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    _CACHE = cfg
