"""blacklight.pic.screen —— 带图评价**提交前闸门**：空值剔除 + 贬损/风险文本筛查。

**为什么要独立一道**：采集器自带的负面词过滤只在**采集当下**生效（`main.py` 的 40 词
`NEGATIVE_WORDS`），一旦文案来源换成平台 AI 生成、或人工改写过，就没有任何人再管。
本模块放在**提交前**，不管文案哪来的都得过。

两类判定：
  - `drop`   硬拒，不许提交（空值 / 贬损 / 退货售后信号 / 联系方式广告 / 隐私 / 灌水）
  - `review` 可疑，**留给人看**（弱负面、极限词、疑似反讽），默认也不自动提交

★**误杀防护**：负面词是子串匹配，"没有色差""不掉色""没有异味""差不多"里都含负面词。
所以先把 `_PROTECTED` 里的**肯定型短语整体挖掉**再匹配 —— 这是采集器原逻辑里
只对"色差"做了、但对其余词都漏做的一件事。

可选的语义复核 `llm_review()` 走内网 `llm-gw`（规则拦不住反讽/委婉贬损）：
**默认不开**，要开得自己给 key（环境变量 `JD_LLM_GW_KEY`），本模块不落任何密钥。
"""
from __future__ import annotations

import json as _json
import os
import re
import time
import urllib.request
from typing import Optional

from blacklight.core import BlacklightError

MIN_CHARS = 12          # 与采集器一致：太短的没有信息量，机审也容易判灌水
MAX_CHARS = 1000        # 平台硬上限

# --- 先整体挖掉的肯定型短语（挖掉后再做负面词匹配，避免"没有色差"被判负面） ---
_PROTECTED = [
    "没有色差", "无色差", "没色差", "不存在色差", "色差不大", "几乎没有色差",
    "不掉色", "没掉色", "不起球", "没起球", "不变形", "没变形", "不缩水", "没缩水",
    "没有异味", "无异味", "没异味", "没有味道", "无味道", "不刺鼻",
    "没有瑕疵", "无瑕疵", "没瑕疵", "没有破损", "无破损", "没破损", "没有断",
    "不差", "差不多", "相差", "不会失望", "没有失望", "不用退货", "没退货",
    "不错", "不赖", "不占地", "不占空间", "不重", "不贵", "不亏",
    "后悔没早买", "后悔买晚了", "后悔没多买", "不后悔", "没后悔", "毫不后悔",
]

# ★否定/预防语境保护：这些前缀 + 负面词 = **正面**（"避免发霉""不会掉色""不用担心破损"）。
# 真实语料里误杀的三例（后悔/发霉/售后）全出在这一类，规则层不做这道就必然误伤。
_RE_PROTECT_CTX = re.compile(
    r"(不会|不再|从不|绝不|没有|没|不用|不必|不怕|避免|防止|以防|杜绝|防|拒绝|"
    r"不存在|无需担心|不用担心|不易|不容易|防止了|避免了)"
    r"[^。！？!.;；]{0,12}?"
    r"(掉色|褪色|异味|刺鼻|发霉|生锈|缩水|起球|变形|破损|损坏|坏|断|裂|开线|脱线|漏水|漏气|"
    r"色差|瑕疵|毛刺|异响|后悔|失望|退货|退款|磨损|变色|积灰|滑落|倒|塌)")

# --- 硬负面：出现即 drop ---
_NEG_HARD = [
    "垃圾", "太差", "很差", "质量差", "做工差", "客服差", "服务差", "差评",
    "烂", "劣质", "假货", "翻车", "踩雷", "后悔", "上当", "被骗", "坑人",
    "退货", "退款", "退了", "申请售后", "找售后", "走售后", "投诉", "维权", "赔偿", "三包",
    "破损", "损坏", "坏了", "断了", "裂了", "开线", "脱线", "漏水", "漏气",
    "掉色", "褪色", "异味", "刺鼻", "发霉", "生锈", "缩水", "起球", "变形",
    "不推荐", "别买", "不要买", "劝退", "浪费钱", "不值", "智商税",
    "货不对板", "与描述不符", "图片仅供参考", "夸大宣传", "虚假",
]

# --- 弱负面 / 可疑：标 review，交人工 ---
_NEG_SOFT = [
    "一般", "凑合", "将就", "就那样", "还行吧", "勉强", "有点小", "有点大", "偏小", "偏大",
    "太薄", "太厚", "太小", "太大", "不合适", "尺寸不对", "不够", "少了", "少一",
    "物流慢", "发货慢", "催了", "等了很久", "瑕疵", "色差", "误差", "售后",
    "一分钱一分货", "便宜没好货", "不如", "没想到",
]

# "态度"只在负面搭配下才算问题 —— 裸词会把"服务态度超级棒"也拖进复核桶
_RE_ATTITUDE = re.compile(r"(态度(差|不好|恶劣|冷淡|敷衍|傲慢))|((差|恶劣|冷淡)的?态度)")

# 标点/表情刷屏：不是灌水，清洗掉即可（"挺大的！！！！！！！" 是正常好评）
_RE_PUNCT_RUN = re.compile(r"([^\w一-鿿])\1{2,}")
_RE_CN_RUN4 = re.compile(r"([一-鿿])\1{3,}")      # 汉字连刷 4+ → 清洗时压到 3，不直接判灌水

# --- 否定 + 正面词 = 贬损（"不好用" "没那么结实"） ---
_NEGATORS = "不|没|没有|并不|不太|不算|不够|谈不上|说不上|称不上"
_POSITIVES = ("好用|好看|结实|扎实|耐用|满意|推荐|划算|值|舒服|方便|喜欢|理想|"
              "牢固|稳固|平整|服帖|柔软|顺滑|清晰|准确")
_RE_NEGATED = re.compile(f"({_NEGATORS})(那么|怎么|太|很|特别|如何)?({_POSITIVES})")

# --- 真评价里的真伪/品牌质疑：即使没有传统负面词，也不能当正向评价提交 ---
_RE_AUTHENTICITY_DOUBT = re.compile(
    r"((好像|似乎|感觉|看着|怀疑|疑似|不确定|不知道)[^。！？!?]{0,16}"
    r"(不是|不像|不对|不符|假货|假的|山寨|冒牌|仿品|正品|原装|品牌|宣传))|"
    r"((不是|不像|不对|不符)[^。！？!?]{0,10}(宣传|品牌|正品|原装|图片|描述))"
)

# 有明确品牌背书、但目标标题里没有该品牌时，至少转人工复核，防止同品采集串品牌。
_RE_BRAND_CLAIM = re.compile(
    r"(?:^|[。！？!?；;\s])([A-Za-z][A-Za-z0-9-]{1,15}|[一-鿿]{2,6})"
    r"[，,、\s]+(大品牌|老品牌|品牌值得信赖)"
)

# --- 联系方式 / 引流 / 竞品 ---
_RE_CONTACT = re.compile(
    r"(微信|加\s*[vV]\b|weixin|wx号|QQ群|扣扣|公众号|抖音|快手|小红书|直播间|"
    r"淘宝|天猫|拼多多|1688|闲鱼|私聊|私信|加我|联系我|客服电话)")
_RE_URL = re.compile(r"(https?://|www\.|\.com|\.cn/|二维码)", re.I)
_RE_PHONE = re.compile(r"(?<!\d)(1[3-9]\d{9}|\d{3,4}-?\d{7,8})(?!\d)")
_RE_ID = re.compile(r"(?<!\d)\d{15}(\d{2}[\dxX])?(?!\d)")

# --- 广告法极限词（评价里出现也可能被机审拦） ---
_EXTREME = ["最好", "最佳", "最优", "第一品牌", "全网最", "国家级", "世界级", "顶级",
            "独家", "绝无仅有", "史上最", "百分百", "100%好", "永久"]

# --- 默认好评 / 无信息量 ---
_DEFAULT_TEXTS = ["此用户未填写评价", "默认好评", "评价方未及时做出评价", "系统默认",
                  "用户未填写", "此用户没有填写"]

_RE_HAS_CN = re.compile(r"[一-鿿]")


def clean_text(text: str) -> str:
    """轻清洗：**标点/表情连刷压到 1 个**、去首尾空白、压多余空行。

    不改语义、不动汉字 —— 只是把"挺大的！！！！！！！"这种收拾干净。真实语料里
    这类占了灌水判定的绝大多数，直接丢掉是亏的（它们本身是合格好评）。"""
    t = (text or "").strip()
    t = _RE_PUNCT_RUN.sub(lambda m: m.group(1), t)
    t = _RE_CN_RUN4.sub(lambda m: m.group(1) * 3, t)      # "赞赞赞赞赞赞" → "赞赞赞"
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _is_spam(t: str) -> bool:
    """清洗后仍算灌水：整体高度重复（去重后剩不下几个字）。"""
    body = re.sub(r"[^\w一-鿿]", "", t)
    return bool(body) and (len(set(body)) < max(4, len(body) * 0.25))


def _mask_protected(text: str) -> str:
    """把肯定型短语和「否定/预防语境+负面词」整体挖掉，再做负面词匹配（防误杀）。"""
    out = text
    for p in _PROTECTED:
        out = out.replace(p, "")
    return _RE_PROTECT_CTX.sub("", out)


def screen_text(text: str, clean: bool = True) -> dict:
    """单条文案体检 → {level: ok|review|drop, reasons, cleaned}。纯规则、无网络。

    `clean=True` 先做轻清洗（标点/表情刷屏压掉），判定与 `cleaned` 都基于清洗后的文本
    —— 提交时用 `cleaned`，别用原文。"""
    t = clean_text(text) if clean else (text or "").strip()
    reasons, level = [], "ok"

    def bad(reason):
        reasons.append(reason)

    if not t:
        return {"level": "drop", "reasons": ["文案为空"], "chars": 0, "cleaned": ""}
    if len(t) > MAX_CHARS:
        bad(f"超过 {MAX_CHARS} 字")
    if len(t) < MIN_CHARS:
        bad(f"太短（{len(t)}字 < {MIN_CHARS}）")
    if not _RE_HAS_CN.search(t):
        bad("没有中文内容")
    if any(d in t for d in _DEFAULT_TEXTS):
        bad("默认/空评价模板")
    if _is_spam(t):
        bad("重复字符灌水")

    masked = _mask_protected(t)
    hard = [w for w in _NEG_HARD if w in masked]
    if hard:
        bad("贬损/负面词: " + "、".join(hard[:5]))
    if _RE_NEGATED.search(masked):
        bad("否定+正面（委婉贬损）: " + _RE_NEGATED.search(masked).group(0))
    if _RE_AUTHENTICITY_DOUBT.search(masked):
        bad("真伪/品牌/宣传质疑: " + _RE_AUTHENTICITY_DOUBT.search(masked).group(0))
    if _RE_CONTACT.search(t) or _RE_URL.search(t):
        bad("含联系方式/引流/竞品平台")
    if _RE_PHONE.search(t):
        bad("含手机号/电话")
    if _RE_ID.search(t):
        bad("疑似身份证号")

    if reasons:
        level = "drop"
    else:
        soft = [w for w in _NEG_SOFT if w in masked]
        if _RE_ATTITUDE.search(masked):
            soft.append("态度差")
        ext = [w for w in _EXTREME if w in t]
        if soft:
            bad("弱负面（需人工看）: " + "、".join(soft[:5]))
        if ext:
            bad("极限词: " + "、".join(ext[:3]))
        if reasons:
            level = "review"
    return {"level": level, "reasons": reasons, "chars": len(t), "cleaned": t}


# --------------------------------------------------------------------------- #
# 整批体检：空值闸门 + 文本闸门
# --------------------------------------------------------------------------- #
def screen_rows(rows: list, require_text: bool = True, allow_review: bool = False) -> dict:
    """[闸门] 整批分桶。**这是导出模板/提交前必过的一关**。

    分四桶：
      - `ok`         有图 + 文案干净 → 可提交
      - `needs_text` **有图但没文案** → 不是废行，交给平台 AI 补文案后再回来过闸
      - `review`     文案可疑（弱负面/极限词）→ 人工看，`allow_review=True` 才并进 ok
      - `dropped`    图空 / 图文全空 / 文案贬损违规 → **剔除**，每条带 reason

    `require_text=False` 时，"有图无文"直接算 ok（适合先提交图、文案后补的玩法）。"""
    ok, needs_text, review, dropped = [], [], [], []
    for i, r in enumerate(rows or []):
        sku = str(r.get("sku_id") or r.get("skuId") or r.get("SKUID") or "").strip()
        imgs = r.get("images")
        if isinstance(imgs, str):
            imgs = [x.strip() for x in imgs.split(",") if x.strip()]
        imgs = [str(u).strip() for u in (imgs or []) if str(u).strip()]
        text = str(r.get("eval_content") or r.get("evalContent") or r.get("评价文本") or "").strip()
        row = {**r, "sku_id": sku, "images": imgs, "eval_content": text}

        if not sku:
            dropped.append({**row, "reason": "SKU 为空", "row_index": i})
            continue
        if not imgs and not text:
            dropped.append({**row, "reason": "图文全为空（采集器占位行）", "row_index": i})
            continue
        if not imgs:
            dropped.append({**row, "reason": "无实拍图（带图评价必须有图）", "row_index": i})
            continue
        image_keys = [re.sub(r"^(?:https?:)?//[^/]+/", "", u.split("?", 1)[0], flags=re.I) for u in imgs]
        if len(image_keys) != len(set(image_keys)):
            dropped.append({**row, "reason": "同一评价内图片重复", "row_index": i})
            continue
        invalid_images = [u for u in imgs if not re.match(
            r"^(?:https?:)?//(?:[a-z0-9-]+\.)*360buyimg\.com/", u, re.I)]
        if invalid_images:
            dropped.append({**row, "reason": "图片不是京东 CDN 地址", "row_index": i})
            continue
        if str(r.get("source_type") or "").strip().lower() == "real_review":
            non_review_images = [u for u in imgs if "/shaidan/" not in u.split("?", 1)[0].lower()]
            if non_review_images:
                dropped.append({**row, "reason": "真实评价来源包含非晒单图片", "row_index": i})
                continue
        if not text:
            (needs_text if require_text else ok).append(
                {**row, "needs_text": True,
                 "note": "有图无文案：用 osw_pic_ai_text 补文案后再过一次闸"})
            continue

        v = screen_text(text)
        row["eval_content"] = v.get("cleaned") or text      # ★提交用清洗后的文本
        if v["level"] == "drop":
            dropped.append({**row, "reason": "；".join(v["reasons"]), "row_index": i})
        elif v["level"] == "review":
            (ok if allow_review else review).append({**row, "warnings": v["reasons"]})
        else:
            sku_name = str(r.get("sku_name") or r.get("skuName") or r.get("商品名称") or "")
            brand_claim = _RE_BRAND_CLAIM.search(text)
            claimed_brand = brand_claim.group(1) if brand_claim else ""
            if claimed_brand and claimed_brand not in sku_name:
                review.append({**row, "warnings": [f"疑似跨品牌评价: {claimed_brand}"]})
            else:
                ok.append(row)

    return {"ok": ok, "needs_text": needs_text, "review": review, "dropped": dropped,
            "summary": {"输入": len(rows or []), "可提交": len(ok), "待补文案": len(needs_text),
                        "待人工复核": len(review), "已剔除": len(dropped)},
            "drop_reasons": _tally(dropped), "review_reasons": _tally(review, "warnings")}


def _tally(rows: list, key: str = "reason") -> dict:
    out = {}
    for r in rows:
        v = r.get(key)
        for reason in (v if isinstance(v, list) else [v]):
            head = str(reason or "").split(":")[0].split("（")[0].strip()
            out[head] = out.get(head, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# --------------------------------------------------------------------------- #
# 可选：语义复核（内网 llm-gw）—— 规则拦不住反讽/委婉贬损
# --------------------------------------------------------------------------- #
LLM_URL = "http://llm-gw.jd.local/v1/chat/completions"
LLM_MODEL = "GPT-5.6-Terra-joybuilder"

_PROMPT = """你在为电商商品的「买家好评」做上线前审核。对每条评价，判断它是否**适合作为正面好评展示**。

判为不合适（pass=false）的情况：贬损/抱怨/退货售后经历、反讽或阴阳怪气、明褒暗贬、
提到竞品或引流、与商品无关的凑字数、无信息量的套话。
其余判 pass=true。

只输出 JSON 数组，每项 {"i": 序号, "pass": true/false, "why": "不超过15字"}，不要输出别的。

评价列表：
"""


def _llm_key() -> str:
    k = os.environ.get("JD_LLM_GW_KEY", "").strip()
    if not k:
        raise BlacklightError(
            "语义复核要 llm-gw 的 key：设环境变量 JD_LLM_GW_KEY（本模块不落密钥）。")
    return k


def llm_review(texts: list, batch: int = 20, timeout: int = 120, retry: int = 2) -> dict:
    """[可选·要 key] 用内网大模型对文案做**语义复核**，专抓规则拦不住的反讽/明褒暗贬。

    ⚠️网关有 API Key 限流（实测快速连发第 3 次就 429），故**串行 + 退避**，别并发。
    返回 {results:[{i, text, pass, why}], failed_batches}。判不准时以人工为准。"""
    key = _llm_key()
    items = [str(t or "").strip() for t in (texts or [])]
    results, failed = [], 0
    for start in range(0, len(items), batch):
        chunk = items[start:start + batch]
        listing = "\n".join(f"{i}. {t}" for i, t in enumerate(chunk))
        payload = {"model": LLM_MODEL,
                   "messages": [{"role": "user", "content": _PROMPT + listing}]}
        data = None
        for attempt in range(retry + 1):
            req = urllib.request.Request(
                LLM_URL, data=_json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = _json.loads(r.read().decode("utf-8", "replace"))
                break
            except Exception:                              # noqa: BLE001  (含 429 限流)
                if attempt >= retry:
                    break
                time.sleep(3 * (attempt + 1))
        if not data:
            failed += 1
            results.extend({"i": start + i, "text": t, "pass": None, "why": "复核失败"}
                           for i, t in enumerate(chunk))
            continue
        try:
            content = data["choices"][0]["message"]["content"]
            parsed = _json.loads(re.search(r"\[.*\]", content, re.S).group(0))
            by_i = {int(x.get("i", -1)): x for x in parsed}
        except Exception:                                  # noqa: BLE001
            by_i = {}
        for i, t in enumerate(chunk):
            x = by_i.get(i) or {}
            results.append({"i": start + i, "text": t,
                            "pass": x.get("pass"), "why": x.get("why") or ""})
        time.sleep(1.5)                                    # 退避，别撞限流
    return {"results": results, "failed_batches": failed,
            "rejected": [r for r in results if r["pass"] is False],
            "note": "语义判定仅作参考，最终以人工为准；pass=None 表示这批没复核成功。"}


def screen_rows_deep(rows: list, require_text: bool = True, allow_review: bool = False) -> dict:
    """规则闸门 + 语义复核两道：先 `screen_rows`，再把进了 ok/review 的文案送 `llm_review`，
    被语义判否的移进 dropped。要 `JD_LLM_GW_KEY`。"""
    base = screen_rows(rows, require_text=require_text, allow_review=allow_review)
    pool = base["ok"] + base["review"]
    if not pool:
        return {**base, "llm": {"results": [], "note": "没有需要复核的文案"}}
    llm = llm_review([r["eval_content"] for r in pool])
    bad_idx = {r["i"] for r in llm["results"] if r["pass"] is False}
    keep_ok, keep_review = [], []
    for i, r in enumerate(pool):
        if i in bad_idx:
            why = next((x["why"] for x in llm["results"] if x["i"] == i), "语义复核判否")
            base["dropped"].append({**r, "reason": f"语义复核: {why}"})
        elif r.get("warnings"):
            keep_review.append(r)
        else:
            keep_ok.append(r)
    base["ok"], base["review"] = keep_ok, keep_review
    base["summary"] = {"输入": len(rows or []), "可提交": len(keep_ok),
                       "待补文案": len(base["needs_text"]), "待人工复核": len(keep_review),
                       "已剔除": len(base["dropped"])}
    base["drop_reasons"] = _tally(base["dropped"])
    return {**base, "llm": {"failed_batches": llm["failed_batches"],
                            "rejected_count": len(bad_idx)}}
