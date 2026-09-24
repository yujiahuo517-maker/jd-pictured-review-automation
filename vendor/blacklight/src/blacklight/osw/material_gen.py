"""blacklight.osw.material_gen —— **商品素材自动补齐**：选参考图 → 生图 → 归一化 → 挂位。

把 `llm.gateway`（生图）、`pic.imgzone`（图床）、`osw.material`（挂位）串成一条按素材位驱动的流水线，
调用方只给 SKU 和要补的位，prompt / 参考图 / 尺寸 / 格式全部自动决定。

## 三条实测出来的硬约束（2026-08-18，都不是猜的）

1. **比例必须 1:1，尺寸只要 ≥480×480 且文件 ≤3MB**（平台规格；实测 600/800/1024/1254 方图全过，
   1024×768 被拒 `图片规格不符合规则`）。注意平台两句报错**差一个字**：
   「图片**格式**不符合规则」= 格式错（PNG 挂 31）；「图片**规格**不符合规则」= 比例错。
2. **网关只回 PNG**，`output_format=jpeg` 传了也被忽略 —— 所以 31/32/33 位那一步 JPG 转换
   **省不掉**，只能本地做。
3. **`size` 只收模型支持的档位**：`1024x1024` ✅，`800x800` → HTTP 400「模型服务调用失败」。
   所以生成侧锁 1024 方图，**补方那步可以省掉**，但转 JPG 那步不行。

⇒ 结论：**生成阶段固定 `size=1024x1024`**（原生方图、最快、JPG 后约 90KB，是 3MB 上限的 3%），
落地只剩一次格式转换。1536 更慢更大且列表页看不出差别，480~800 无谓地丢细节。

## 透明图（36）不走大模型

生图模型**画棋盘格假装透明**，输出 RGB 无 alpha（见 [[llm-material-generation-limits]]）。
改用平台自带抠图 `getMattingImage`：**0.7s**、直接回 base64 的 RGBA PNG、边缘比本地洪填干净
（连投影一起去掉）。所以 36 位的正确路径是「先有白底图 → 抠图」，不是"生成"。

★★**反过来不行：白底图(31) 必须实生成后上传，不许拿透明图反向抠**（`generateTypes=[2]`
接口是存在的，但业务上不走这条——用户 2026-08-18 明确纠正过一次）。
所以依赖方向是**单向**的：31 生成 → 36 抠图。别再从"接口支持反向"推导出"白底图可以省"。
"""
from __future__ import annotations

import base64
import re
import json as _json
import os
import time
from typing import Optional

from blacklight.core import BlacklightError, ConfirmGate, audited
from blacklight.osw import material as M

# 生成阶段固定用它：模型原生方图档位，转 JPG 后约 90KB
GEN_SIZE = "1024x1024"
JPEG_QUALITY = 92
MAX_FILE_BYTES = 3 * 1024 * 1024        # 平台上限
MIN_EDGE = 480                          # 平台下限

MATTING_THROUGH, MATTING_WHITE = 1, 2   # getMattingImage 的 generateTypes（前端 Tt 枚举）
# ⚠️`MATTING_WHITE`（透明→白底）**不用于补 31 位**：白底图必须实生成后上传。
#   保留常量只为完整表达接口能力，`autofill` 里 31 位永远走生成。

# ★素材命名统一 `<skuId>_<素材类型>`，本地文件与图床名共用同一张表。
#   与图片空间里已有的「京麦智能作图」命名对齐（`<skuId>_scene_1.jpg` / `_whitebg.jpg`）。
SLOT_SLUG = {31: "whitebg", 36: "transparent", 32: "scene", 3201: "scene_1", 3202: "scene_2",
             33: "sellpoint", 34: "market", 51: "searchmain", 52: "distribute", 53: "recommend"}


def slot_filename(sku_id, material_type: int, ext: str = None) -> str:
    """`<skuId>_<素材类型>.<ext>` —— 生成物一律用这个名字，别再各处拼各自的。"""
    t = int(material_type)
    wire = M._WIRE_TYPE.get(t, (t, 0))[0]
    if ext is None:
        ext = "png" if M.SLOT_SPEC.get(wire) == "png" else "jpg"
    return "%s_%s.%s" % (sku_id, SLOT_SLUG.get(t, f"type{t}"), ext.lstrip("."))


# --------------------------------------------------------------------------- #
# 1) 参考图：自动挑，别再手工翻
# --------------------------------------------------------------------------- #
def main_images(sku_id) -> list:
    """本 SKU 的**全部主图**（`getSkuImages`，本例 5 张），不只是 logo 那一张。"""
    d = M._call("dsm.upload.material.ware.getSkuImages", {"query": {"skuIds": [str(sku_id)]}})
    infos = ((d or {}).get("skuInfoMap") or {}).get(str(sku_id), {}).get("wareImageInfos") or []
    return [M.full_url(x.get("path")) for x in infos if x.get("path")]


def pick_references(product_id, sku_id=None, max_n: int = 6) -> dict:
    """给一个 SKU 自动挑生图参考图，并**区分两种角色**。

    - **结构基准（clean）**：同 SPU 已审核通过(status=4)的白底图 31 / 透明图 36。
      干净、无杂物，用来锁商品本体的结构与配色。
    - **风格基准（style）**：本 SKU 的**全部主图**。它们是这件商品的真实拍摄图，
      多角度、结构真实，而且**运营已定调的视觉风格就在里面**（光影、色温、道具、构图）——
      生成图往这个风格靠，出来的东西才像同一个链接里的图。

    ⚠️主图里常有第三方品牌包装和大字贴片。**这是内容问题，不是参考图问题**：
    靠 prompt 明确"替换为无品牌素色包装、不要文字贴片"来解，不该因此丢掉整组风格基准。
    （2026-08-18 我一度据此把主图整体排除，判断下早了——那组图恰恰是结构最准的证据，
    我生成白底图时画错的"大开门"在主图里看得很清楚。）
    """
    rows = M.list_sku(product_id)["rows"]
    clean, why = [], []

    # ★★结构基准**必须来自同一个外观组**。同 SPU 里常有方形/圆形、带盖/无盖这类外观变体，
    #   随手取"任意一张已审核白底图"会让质检拿**错形状**去比，把画对的图判成画错的——
    #   2026-08-19 实撞：SPU 10035754577854 的「圆形L号」取到了「方形XL号」当基准，
    #   两个圆形组共 6 张全被误杀（图其实是对的）。误杀比画错更贵：会白烧一轮生成额度，
    #   还会让人以为模型不稳。
    group_ids = None
    if sku_id:
        try:
            me = next((r for r in rows if str(r["sku_id"]) == str(sku_id)), None)
            if me is not None:
                sig = appearance_sig(me)
                group_ids = {str(r["sku_id"]) for r in rows if appearance_sig(r) == sig}
        except Exception:                               # noqa: BLE001
            group_ids = None

    def _collect(slot, label, pool, tag):
        for r in pool:
            for m in (r.get("materials") or {}).get(slot, []):
                if m.get("status") == 4 and m.get("url") and m["url"] not in clean:
                    clean.append(m["url"])
                    why.append(f"结构基准·{label}(SKU {r['sku_id']}，已审核通过，{tag})")
                    return True
        return False

    clean_same_group = False
    if group_ids:
        pool = [r for r in rows if str(r["sku_id"]) in group_ids]
        a = _collect(31, "白底图", pool, "同外观组")
        b = _collect(36, "透明图", pool, "同外观组")
        clean_same_group = bool(a or b)
    if not clean:
        # 兜底：同组一张都没有。**跨组的图只能当风格参考，不能当结构基准**，
        # 所以这里 clean_same_group 保持 False，由调用方决定不拿它做质检基准。
        _collect(31, "白底图", rows, "⚠️跨外观组·仅供风格参考")
        _collect(36, "透明图", rows, "⚠️跨外观组·仅供风格参考")

    style = []
    if sku_id:
        try:
            style = [u for u in main_images(sku_id) if u not in clean]
        except Exception:                               # noqa: BLE001
            style = []
    if not style:
        logo = next((r.get("logo") for r in rows if r.get("logo")), None)
        style = [logo] if logo and logo not in clean else []

    n_style = max(0, max_n - len(clean))
    style = style[:n_style]
    why += [f"风格基准·主图{i + 1}" for i in range(len(style))]
    refs = clean + style
    if not refs:
        raise BlacklightError(f"SPU {product_id} 下找不到任何可用参考图")
    return {"refs": refs, "why": why, "clean_n": len(clean), "style_n": len(style),
            # ★调用方判"能不能拿 refs[0] 当质检的结构基准"看这个，别看 clean_n：
            #   clean_n>0 只说明有干净图，不保证它和本 SKU 是同一个外观。
            "clean_same_group": clean_same_group,
            "note": "结构看前 %d 张、风格看后 %d 张；主图里的第三方品牌由 prompt 负责替换%s"
                    % (len(clean), len(style),
                       "" if clean_same_group else
                       "。⚠️干净图来自**其它外观组**，不可作结构基准（质检会误杀）")}


# --------------------------------------------------------------------------- #
# 1.5) 商品上下文：从商品域接口捞类目/品牌/兄弟规格，喂给 prompt
# --------------------------------------------------------------------------- #
def enrich(product_id, sku_id) -> dict:
    """从素材页之外的接口补齐生图需要的商品语境。

    三个字段是这轮挖出来、**实测能改善生成质量**的：

    - **三级类目**（`getProductAllSkuMaterialInfo`）：场景图选哪个房间不该靠瞎猜，
      「收纳用品/收纳柜/其他收纳柜」直接决定场景。
    - **brandName**：本店多为「无品牌」，可以在 prompt 里断言"不得出现任何品牌标识"，
      比泛泛说"不要品牌"强。
    - ★**同 SPU 兄弟 SKU 的规格取值**（`getSkuSaleAttrList`）：
      这是**治结构错的关键**。上一轮模型把「大开门款」画成左右双开门，根因是
      "大开门"只有跟"小开口"对比才有意义，而 prompt 里没给对照。
      把兄弟取值一起塞进去（本款=大开门款-带木盖；同款还有 小开口款/无木盖），
      模型才知道该突出哪个差异。

    主图列表（`getSkuImages`，本例 5 张）由 `pick_references` 取，当**风格基准**用。
    """
    ctx = {"product_id": str(product_id), "sku_id": str(sku_id)}
    try:
        d = M._call("dsm.upload.material.ware.getProductAllSkuMaterialInfo",
                    {"apiProductQuery": {"productId": str(product_id), "sceneType": 1,
                                         "skuFieldSet": ["saleAttrs", "logo", "sellMaxNum"]}})
        me = next((c for c in (d.get("childList") or [])
                   if str(c.get("productId")) == str(sku_id)), {})
        cats = [d.get("category1Name"), me.get("category2Name"), me.get("category3Name")]
        ctx["category_path"] = " > ".join(x for x in cats if x)
        ctx["category_leaf"] = me.get("category3Name") or me.get("category2Name")
        ctx["brand"] = me.get("brandName")
        ctx["size"] = me.get("size") or None
    except Exception as e:                              # noqa: BLE001
        ctx["enrich_error"] = str(e)[:120]
    try:
        s = M._call("dsm.upload.material.sale.getSkuSaleAttrList",
                    {"saleAttributeQuery": {"spuId": str(product_id)}})
        axes = []
        for a in (s.get("saleAttributeInfoList") or []):
            vals = [p.get("skuProp") for p in (a.get("skuProps") or []) if p.get("skuProp")]
            if vals:
                axes.append({"attr": a.get("attrName"), "values": vals})
        ctx["variant_axes"] = axes
    except Exception as e:                              # noqa: BLE001
        ctx["variant_error"] = str(e)[:120]
    return ctx


def _variant_clause(ctx: dict, sku_name: str) -> str:
    """把「本款 vs 兄弟款」的差异写成一句话，专治模型改结构。"""
    import re
    axes = ctx.get("variant_axes") or []
    if not axes:
        return ""
    vals = axes[0].get("values") or []
    clean = [re.split(r"[【\[]", v)[0].strip() for v in vals]
    mine = next((c for c in clean if c and c in sku_name), None)
    others = [c for c in clean if c and c != mine]
    if not mine or not others:
        return ""
    return (f"⚠️本款规格是「{mine}」；同款还有「{'」「'.join(others[:3])}」等其它规格，"
            f"**务必按「{mine}」的结构来画，不要画成其它规格的样子**。")


# 类目 → 场景。命中靠子串，命中不了就退回通用描述（宁可泛，别瞎编一个房间）
# ★场景池要**够杂**：每个类目多给几种房间/光线/角度/色调，再按 SKU 打散，
#   否则同类目几十个 SKU 会出一整屏一模一样的暖色原木风（2026-08-18 用户指出这个问题）。
#   注意这里只描述**环境**，不规定商品颜色——商品长什么样以参考图为准。
_CATEGORY_SCENES = {
    "收纳": ["整洁的家居收纳角落，靠墙摆放，正午侧逆光",
             "客厅沙发旁作为边柜使用，傍晚暖光",
             "衣帽间/储物间的层架旁，冷白顶光，灰白极简调",
             "书房书桌下方或角落，清晨自然光，原木+白",
             "阳台洗晾区旁，明亮日光，蓝白清爽调"],
    "厨房": ["明亮整洁的厨房台面，冷白灯光，不锈钢与白瓷质感",
             "餐边柜区域，暖光，木质餐桌一角",
             "开放式厨房中岛旁，日光侧射",
             "橱柜内部/抽屉打开的俯视视角"],
    "母婴": ["温馨婴儿房，旁边有婴儿床，柔和日光",
             "卧室床边，夜灯氛围，低照度暖调",
             "儿童游戏区地毯旁，明快高饱和配色",
             "育儿角落，白墙+浅灰，干净克制"],
    "鞋":   ["入户玄关靠墙摆放，顶灯照明",
             "玄关换鞋区，旁边有换鞋凳，日光斜射",
             "走廊尽头的鞋柜区，冷调灰白",
             "更衣室内的鞋架陈列，聚光灯感"],
    "家具": ["与其风格相配的客厅中，午后侧光",
             "卧室角落，低饱和灰粉/灰蓝调",
             "略微俯视的侧前方视角，融入生活场景",
             "工作区/书房，冷白光，现代简约"],
}
_SCENE_FALLBACK = ["放在与其用途最匹配的家庭房间里靠墙摆放，中景平视，自然光",
                   "换一个房间和角度：略微俯视的侧前方视角，融入生活场景",
                   "干净的室内一角，冷调灰白背景，克制的现代感",
                   "使用中的生活场景，暖光，浅景深"]


# ★★**优先从商品标题取用途关键词**来定场景，类目只作兜底。
#   类目太粗（"其他收纳柜"），同类目几十个 SKU 会出一屏同质图；
#   而标题里通常明写着用途：「厨房锁鲜」「冰箱专用」「宿舍」「学生笔筒」「玄关」「衣柜」…
#   命中多个关键词时，场景图1/2 各取一个**不同的语境**，天然拉开差异。
_TITLE_SCENES = [
    (("冰箱", "保鲜", "冷冻", "锁鲜", "果蔬"),
     ["打开的冰箱内部层架上，冷白光，食材整齐",
      "厨房台面靠近冰箱处，清晨自然光"]),
    (("厨房", "橱柜", "灶", "餐边", "调味", "米面"),
     ["明亮整洁的厨房台面，冷白灯光，不锈钢与白瓷质感",
      "餐边柜区域，暖光，木质餐桌一角",
      "橱柜内部打开的俯视视角"]),
    (("玄关", "入户", "鞋"),
     ["入户玄关靠墙摆放，顶灯照明",
      "玄关换鞋区，旁边有换鞋凳，日光斜射"]),
    (("衣柜", "衣物", "衣服", "被子", "羽绒", "床品", "棉被"),
     ["衣帽间层架上，冷白顶光，灰白极简调",
      "卧室衣柜内部，柔和暖光，叠放整齐"]),
    (("书房", "办公", "书桌", "文具", "笔", "学生", "作业", "桌面"),
     ["书房书桌一角，清晨自然光，原木+白",
      "办公桌面，冷白光，现代简约",
      "学习区书架旁，午后侧光"]),
    (("宿舍",),
     ["学生宿舍书桌与床铺之间的收纳角落，明快配色",
      "宿舍上下铺旁的置物区，日光充足"]),
    (("卫生间", "浴室", "洗漱", "牙", "沐浴", "毛巾"),
     ["卫生间洗手台上，冷调白瓷与镜面",
      "浴室置物架旁，明亮通透"]),
    (("化妆品", "梳妆", "口红", "护肤"),
     ["梳妆台上，柔和补光，低饱和粉灰调",
      "卧室梳妆区，暖光，镜面反射"]),
    (("阳台", "晾", "洗衣", "洗护"),
     ["阳台洗晾区旁，明亮日光，蓝白清爽调",
      "洗衣机旁的置物区，冷调干净"]),
    (("婴儿", "母婴", "奶粉", "尿布", "宝宝", "儿童", "玩具"),
     ["温馨婴儿房，旁边有婴儿床，柔和日光",
      "儿童游戏区地毯旁，明快高饱和配色",
      "卧室床边，夜灯氛围，低照度暖调"]),
    (("车载", "汽车", "后备"),
     ["汽车后备箱内，日光斜射",
      "车内后排座位旁，冷调内饰"]),
    (("零食", "食品", "干货", "杂粮"),
     ["餐边柜上的零食收纳区，暖光",
      "厨房储物架，食材分类陈列"]),
]


def _title_scenes(title: str) -> list:
    """标题命中的用途语境（可能多个，按出现顺序）。"""
    t = str(title or "")
    hits = []
    for keys, scenes in _TITLE_SCENES:
        pos = [t.find(k) for k in keys if k in t]
        if pos:
            hits.append((min(pos), scenes))
    # ★按关键词在标题里**出现得多靠前**排序：京东标题习惯把主用途写在前面，
    #   这样场景图1 拿到的是主场景，次要用途留给场景图2。
    hits.sort(key=lambda x: x[0])
    return [x[1] for x in hits]


def scene_hint(ctx: dict, order: int = 0, seed=None, title: str = None) -> str:
    """按类目挑场景描述并**按 seed 打散**，避免同类目商品出一屏雷同的图。

    `title` 传 SKU 名（**优先级最高**：标题里的用途词比类目精确得多）；
    `seed` 传 SKU，用来在池子里打散。都命不中才退回通用说法（宁可泛，别瞎编一个房间）。
    """
    hits = _title_scenes(title)
    if hits:
        # 命中多个语境时，不同 order 取**不同语境**；只命中一个就在它内部换变体
        pool = hits[int(order) % len(hits)] if len(hits) > 1 else hits[0]
    else:
        leaf = (ctx.get("category_path") or "") + (ctx.get("category_leaf") or "")
        pool = _SCENE_FALLBACK
        for k, v in _CATEGORY_SCENES.items():
            if k in leaf:
                pool = v
                break
    # ⚠️别用 sum(ord(c)) 当散列：SKU 号只差末几位，字符和会聚集，
    #   实测相邻 SKU 全落到同一条场景上，等于没打散。用 crc32 才有雪崩效应。
    base = 0
    if seed is not None:
        import zlib
        base = zlib.crc32(str(seed).encode()) % len(pool)
    return pool[(base + int(order)) % len(pool)]


# --------------------------------------------------------------------------- #
# 2) 商品描述：从 SKU 数据自动组装，不用每次手写
# --------------------------------------------------------------------------- #
def describe(product_id, sku_id) -> dict:
    """把 SKU 名 + 销售属性拼成一段可直接塞进 prompt 的商品描述。

    SKU 名里常带一串促销词（"【环保PP材质/免安装/赠滑轮】"），这些恰好是天然的卖点来源，
    顺手抽出来给卖点图(33)用，省得每次手填。
    """
    import re
    rows = M.list_sku(product_id)["rows"]
    me = next((r for r in rows if str(r["sku_id"]) == str(sku_id)), None)
    if not me:
        raise BlacklightError(f"SKU {sku_id} 不在 SPU {product_id} 下")
    name = (me.get("sku_name") or "").strip()
    attrs = me.get("sale_attrs") or {}
    # 【】里的卖点串，按 / 、 空格切；只留 2~6 字的短词（长的塞进图里必然排版崩）
    pts = []
    for blk in re.findall(r"[【\[]([^】\]]+)[】\]]", name):
        for p in re.split(r"[/、,，\s]+", blk):
            p = p.strip()
            if 2 <= len(p) <= 6 and p not in pts:
                pts.append(p)
    head = re.split(r"[【\[]", name)[0].strip()
    # ⚠️销售属性里常有个叫「卖点」的伪属性，值就是标题尾巴本身；直接拼会把整串重复一遍。
    #   只保留**真规格**（层数/颜色/尺寸这类）且值还没出现在标题里的。
    useful = {k: v for k, v in attrs.items()
              if k not in ("卖点", "赠品") and re.split(r"[【\[]", str(v))[0].strip() not in head}
    text = head + ("（" + "、".join(f"{k}{v}" for k, v in useful.items()) + "）" if useful else "")
    return {"sku_id": me["sku_id"], "sku_name": name, "short_title": me.get("short_title"),
            "sale_attrs": attrs, "text": text, "sell_points": pts[:3],
            "note": "sell_points 抽自标题【】里的促销词，上图前**必须人工过一遍**（可能有夸大或不适合上图的词）"}


# --------------------------------------------------------------------------- #
# 3) 通用 prompt：三段式 = 硬约束 + 商品描述 + 该位的目标
# --------------------------------------------------------------------------- #
# 所有位共用的硬约束。**独立成段**，改一处全部生效。
_RULES = (
    "【硬性要求】"
    "①严格保持商品本身的结构、部件数量、比例、材质和颜色与参考图完全一致，不得增删或改变结构部件；"
    "**尤其不得改变门/抽屉/层板的数量与开合方式**（例如把一整扇门画成左右对开的两扇）；"
    "（按 N 只装售卖的商品，画面里出现多件同款是允许的）"
    "②画面中**绝对不允许出现任何真实品牌**（参考图里出现的奶粉罐/纸尿裤/家电等第三方品牌包装，"
    "一律替换为纯素色、无任何文字图案的同色系包装，或直接移除）；"
    "②b **书籍/杂志/画册/纸盒等印刷品是道具、不是商品本体**：书脊与封面一律画成纯色或纯色块，**不得出现任何可辨认的刊名、书名、英文词或仿文字笔画**"
    "（实撞：书脊被画成 KINFOLK、CEREAL 等真实杂志名，以及一行乱码字符）；"
    "③★**商品本体自带的文字与标识必须原样保留**——印在商品上的型号、刻度、面板字符、"
    "自有品牌标、材质标签等都是商品的一部分，**不得抹除、涂改或替换**；"
    "要去掉的只是**叠加在画面上的**营销文字、促销贴片、水印、二维码、边框，"
    "以及参考图里的人物和第三方品牌的包装；"
    "④**正方形 1:1 构图**，商品完整不被裁切。"
)
# ★★参考图按素材位分配，**不是所有位都该给风格图**（2026-08-18 A/B 实测）：
#   给 31 白底图喂实拍主图后，模型把主图台面上的奶瓶/水壶/纸巾盒**原样搬到了白底上**——
#   白底图要的是"裸商品"，风格参考在这里是纯污染。
#   而 32 场景图 / 33 卖点图 喂了主图后质量明显上台阶（光影色调道具都对上了运营那套视觉）。
SLOT_REF_POLICY = {31: "clean", 36: "clean", 32: "all", 33: "all", 34: "all"}


def refs_for(material_type: int, refs_meta: dict) -> tuple:
    """按素材位切参考图，返回 (图列表, 该位实际的 clean/style 张数)。"""
    t = M._WIRE_TYPE.get(int(material_type), (int(material_type), 0))[0]
    allrefs = list((refs_meta or {}).get("refs") or [])
    c = int((refs_meta or {}).get("clean_n", 0))
    if SLOT_REF_POLICY.get(t, "all") == "clean" and c:
        return allrefs[:c], {"clean_n": c, "style_n": 0}
    return allrefs, {"clean_n": c, "style_n": len(allrefs) - c}


# 参考图的角色说明 —— 结构看干净图、风格看实拍主图
_ROLES = ("【参考图说明】前 {clean} 张是本商品的**干净商品图**，用作**结构与配色基准**"
          "（商品本体长什么样以它为准）；后 {style} 张是本商品的**实拍主图**，用作**风格基准**："
          "请贴近**这组参考图本身**的光影、色温、材质与构图质感。"
          "★**不要套用固定色调**——别一律做成暖色原木风；参考图是冷调就冷调、是高饱和就高饱和，以图为准。"
          "**只学风格，不要照搬里面的第三方品牌商品和叠加的文字贴片**。")
_ROLES_STYLE_ONLY = ("【参考图说明】参考图是本商品的**实拍主图**，商品结构以它为准，"
                     "画面风格也请贴近这种真实电商摄影质感。"
                     "**但参考图里的第三方品牌包装、文字贴片、人物一律不要出现在生成结果中**。")

# 每个素材位的目标。{desc} 会被商品描述替换。
SLOT_PROMPTS = {
    31: "商品是：{desc}。"
        "生成**纯白背景的裸商品图**：背景纯白 RGB(255,255,255)，无灰度渐变，"
        "商品居中、正面微侧约45度，**画面里只有商品本体**——"
        "柜内/箱内完全清空、门与盖关闭、**台面上也不得摆放任何物品或道具**，"
        "仅保留极浅的接地投影，商品占画面约80%。",
    32: "商品是：{desc}。"
        "生成**真实家居使用场景图**：{scene}。自然采光、暖色调、画面干净有生活感，"
        "商品是画面主体且完整可见。",
    33: "商品是：{desc}。"
        "生成**电商卖点图**：纯净浅米色背景，商品居中偏左并保持结构比例不变，"
        "右侧用简洁的引导线+圆点分别指向商品的对应部位。"
        "**图中只允许出现这{n}组简体中文文字，字形必须准确、无错字无乱码**：{points}。"
        "除这{n}组词外不得出现任何其他文字、英文、数字、logo或水印。",
    34: "商品是：{desc}。"
        "生成**营销氛围图**：柔和渐变背景，商品居中略带俯视角，光影通透有质感。",
}

def build_prompt(material_type: int, desc: str, *, order: int = 0, sell_points=None,
                 scene: str = None, refs_meta: dict = None, ctx: dict = None,
                 sku_name: str = "") -> str:
    """拼 prompt。**改文案只改这里**，五个位共用同一套硬约束。

    `ctx` 是 `enrich()` 的产物：类目 → 场景选择、品牌 → 断言不出现品牌、
    兄弟规格 → 结构消歧（治"大开门被画成双开门"）。
    """
    t = M._WIRE_TYPE.get(int(material_type), (int(material_type), 0))[0]
    tpl = SLOT_PROMPTS.get(t)
    if not tpl:
        raise BlacklightError(f"素材位 {material_type} 没有 prompt 模板（有：{sorted(SLOT_PROMPTS)}）")
    ctx = ctx or {}
    kw = {"desc": desc}
    if t == 32:
        kw["scene"] = scene or scene_hint(ctx, order, seed=sku_name or ctx.get("sku_id"),
                                          title=sku_name or desc)
    if t == 33:
        # ★没有卖点词就**不该生成卖点图**：硬编码兜底词会画出与商品无关的承诺，
        #   而且质检对不上。让调用方先补通用卖点再来。
        if not sell_points:
            raise BlacklightError("卖点图(33)需要卖点词：先 sellpoints_generate/save 补通用卖点")
        pts = list(sell_points)
        kw["points"] = "、".join(f"「{p}」" for p in pts)
        kw["n"] = len(pts)
    lead = ""
    if ctx.get("category_path"):
        lead += f"该商品所属类目：{ctx['category_path']}。"
    body = tpl.format(**kw)
    tail = _variant_clause(ctx, sku_name or desc)
    if str(ctx.get("brand") or "") in ("无品牌", ""):
        tail += "本商品**无品牌**，画面上不得出现任何品牌标识或商标。"
    rm = refs_meta or {}
    c, st = int(rm.get("clean_n", 0)), int(rm.get("style_n", 0))
    roles = _ROLES.format(clean=c, style=st) if (c and st) else (_ROLES_STYLE_ONLY if st else "")
    return lead + body + tail + roles + _RULES


# --------------------------------------------------------------------------- #
# 4) 生成 + 归一化
# --------------------------------------------------------------------------- #
def _to_slot_file(src: str, material_type: int, out_path: str) -> str:
    """按素材位存成平台收的格式。生成侧已锁 1:1，这里正常只做一次格式转换。"""
    from PIL import Image
    t = M._WIRE_TYPE.get(int(material_type), (int(material_type), 0))[0]
    keep_alpha = M.SLOT_SPEC.get(t) == "png"
    im = Image.open(src)
    im = im.convert("RGBA") if keep_alpha else im.convert("RGB")
    w, h = im.size
    if w != h:                                          # 兜底：模型偶尔不听 size
        s = max(w, h)
        bg = im.convert("RGB").getpixel((2, 2))
        c = Image.new(im.mode, (s, s), bg + (255,) if keep_alpha else bg)
        c.paste(im, ((s - w) // 2, (s - h) // 2))
        im = c
    if min(im.size) < MIN_EDGE:
        im = im.resize((MIN_EDGE, MIN_EDGE), Image.LANCZOS)
    if keep_alpha:
        im.save(out_path, "PNG")
    else:
        im.save(out_path, "JPEG", quality=JPEG_QUALITY)
    if os.path.getsize(out_path) > MAX_FILE_BYTES:
        raise BlacklightError(f"{out_path} 超过平台 3MB 上限，降 quality 或缩边长")
    return out_path


def generate(material_type: int, desc: str, refs, out_dir: str, stem: str, *,
             order: int = 0, sell_points=None, scene: str = None,
             refs_meta: dict = None, ctx: dict = None, sku_name: str = "") -> dict:
    """生成一张该素材位的图并归一化到可上传状态。"""
    from blacklight.llm import gateway as G
    os.makedirs(out_dir, exist_ok=True)
    use_refs, meta = refs_for(material_type, refs_meta or {"refs": list(refs),
                                                           "clean_n": len(list(refs))})
    refs = use_refs or list(refs)
    prompt = build_prompt(material_type, desc, order=order, sell_points=sell_points,
                          scene=scene, refs_meta=meta, ctx=ctx, sku_name=sku_name)
    t0 = time.time()
    r = G.image_edit(prompt, list(refs), out_dir=out_dir, stem=stem + "_raw", size=GEN_SIZE)
    t = M._WIRE_TYPE.get(int(material_type), (int(material_type), 0))[0]
    ext = "png" if M.SLOT_SPEC.get(t) == "png" else "jpg"
    final = _to_slot_file(r["paths"][0], material_type, os.path.join(out_dir, f"{stem}.{ext}"))
    return {"material_type": int(material_type), "order": order,
            "path": final, "raw": r["paths"][0], "bytes": os.path.getsize(final),
            "seconds": round(time.time() - t0, 1), "usage": r.get("usage"), "prompt": prompt}


# --------------------------------------------------------------------------- #
# 5) 透明图：走平台抠图，不走大模型
# --------------------------------------------------------------------------- #
def matting(img_url: str, to: int = MATTING_THROUGH, out_path: str = None) -> dict:
    """平台算法抠图。实测 0.7s，回 **base64 的 RGBA PNG**（不是 URL），比本地洪填干净（投影也去掉）。

    `to=1`（默认）白底图 → 透明图，这是补 36 位的正路。
    ★`to=2`（透明 → 白底）**不用于补 31 位**：白底图必须实生成后上传。接口能力在此列全，
    但别据此推导出"白底图可以省"——依赖方向是单向的。
    """
    rel = str(img_url or "").replace(M.CDN, "").strip()
    if not rel:
        raise BlacklightError("matting 需要一张已在图片空间里的图")
    d = M._call("dsm.media.image.imageAlgoGenerateService.getMattingImage",
                {"imageParam": {"imgUrl": rel, "generateTypes": [int(to)],
                                "scene": "fine_matting"}}, timeout=180)
    b64 = ((d or {}).get("generateResults") or {}).get(str(int(to)))
    if not b64:
        raise BlacklightError(f"抠图未返回结果：{_json.dumps(d, ensure_ascii=False)[:200]}")
    raw = base64.b64decode(b64)
    if out_path:
        with open(out_path, "wb") as f:
            f.write(raw)
    return {"path": out_path, "bytes": len(raw), "b64": None if out_path else b64,
            "note": "回的是 base64，不是 URL；要挂位得先 imgzone.upload"}


# --------------------------------------------------------------------------- #
# 5.2) 质检：把"人眼比对结构"这步交给多模态模型
# --------------------------------------------------------------------------- #
# ★网关的 chat 接口**支持视觉输入**（content 里塞 image_url，图片会被 token 化计费）。
#   2026-08-18 用「已知好图 v3 / 已知坏图 v2」做对照实测：
#   GPT-5.5 / 5.6-Luna / 5.6-Sol / 5.6-Terra **8/8 全判对**（门数、台面有无道具），各约 4s。
#   Claude-Opus-4.8 与 GPT-image-2 在 /chat/completions 上 404 `model not support`。
#   ⇒ 之前反复说的「每张必须人眼比对结构」**可以自动化**，这里就是那一步。
QC_MODEL = "GPT-5.6-Terra-joybuilder"       # 四个都行，选最快的
QC_MODELS_OK = ["GPT-5.5-joybuilder", "GPT-5.6-Luna-joybuilder",
                "GPT-5.6-Sol-joybuilder", "GPT-5.6-Terra-joybuilder"]

_QC_BASE = (
    "图1是该商品的**基准图**（商品本体以它为准），图2是**待检图**。逐项检查图2，"
    "只输出 JSON，不要任何解释文字：\n"
    '{{"structure_same":true/false,"structure_diff":"不一致的地方，一致则空字符串",'
    '"third_party_brand":true/false,"brand_detail":"看到的品牌名，没有则空",'
    '"unexpected_text":true/false,"text_found":["图中出现的所有文字"],'
    '{extra}"product_text_garbled":true/false,"garbled_detail":"乱码处描述，正常则空",'
    '"verdict":"pass"/"fail","reasons":["判 fail 的原因"]}}\n'
    "判定口径：①**只看单件商品自身的构造**——门/抽屉/层板的数量与开合方式必须和图1一致"
    "（一整扇门被画成左右对开两扇＝不一致；部件被增删＝不一致）。"
    "★**画面里出现几件同款商品不算不一致**：很多商品本来就按「2只装/4只装」售卖，"
    "并排或叠放展示是正常的商品图表达，不要因此判 false；"
    "②画面内不得出现任何真实品牌商标或英文品牌名；"
    "③{text_rule}"
    "★注意：**商品本体自带的文字/标识本就该在**，把它们当成违规是误判；同理画面里出现几件同款商品也不算问题。"
    "⑨但要单独看一眼：**商品上那些文字是不是画成了乱码/伪汉字/无意义字符**"
    "（模型重绘小字时常糊成看不懂的形状）。是就把 product_text_garbled 置 true 并说明位置。"
)
_QC_EXTRA = {
    31: ('"bg_pure_white":true/false,"props_present":true/false,',
         "白底图内不得有清晰可辨的文字；④背景必须是纯白；⑤商品台面上、柜内均不得摆放任何物品或道具"),
    36: ('"bg_transparent":true/false,"props_present":true/false,',
         "透明图内不得有清晰可辨的文字；④背景必须是透明（不是白色、也不是棋盘格图案）；⑤不得摆放任何道具"),
    33: ('"texts_exact_match":true/false,',
         "卖点图**只允许**出现这些文字：{texts}；多一个字、少一个字、错字或乱码都算不通过"),
}
# ⚠️只统计"清晰可辨"的文字：场景图里道具包装上的模糊纹理会被模型当成文字，造成误杀
#   （2026-08-18 实撞：一张合格场景图被报「出现不该有的文字：['模糊不可辨文字']」）。
_LEGIBLE = ("（**只统计叠加在画面上的文字**：营销文案、贴片、水印、二维码。"
            "★**印在商品本体上的文字与标识不算**——型号、刻度、面板字符、自有品牌标都是商品的一部分，"
            "它们出现是正确的，不要报出来；道具或包装上模糊不可辨的纹理也不算。）")
_QC_DEFAULT_TEXT_RULE = "画面内不得出现清晰可辨的文字、水印或促销贴片" + _LEGIBLE


def qc(image, material_type: int, reference=None, expect_texts=None,
       model: str = None, timeout: int = 120) -> dict:
    """用多模态模型质检一张生成图。**这是上传前的闸门，不是事后复盘**。

    `image` / `reference` 收本地路径或 URL。`reference` 建议传该 SPU 已审核通过的白底图
    （`pick_references` 的 clean 那部分）——没有基准图就只能查品牌/文字，查不了结构。
    """
    from blacklight.llm import gateway as G
    t = M._WIRE_TYPE.get(int(material_type), (int(material_type), 0))[0]
    extra, rule = _QC_EXTRA.get(t, ("", _QC_DEFAULT_TEXT_RULE))
    if t == 33:
        rule = rule.format(texts="、".join(f"「{x}」" for x in (expect_texts or [])) or "（未指定）")
    prompt = _QC_BASE.format(extra=extra, text_rule=rule)
    content = [{"type": "text", "text": prompt}]
    if reference:
        content.append({"type": "image_url", "image_url": {"url": G._as_data_url(reference)}})
    else:
        content[0]["text"] = content[0]["text"].replace("图1是该商品的**基准图**（商品本体以它为准），图2是**待检图**",
                                                        "下图是待检图（无基准图，跳过结构比对）")
    content.append({"type": "image_url", "image_url": {"url": G._as_data_url(image)}})
    r = G.chat(messages=[{"role": "user", "content": content}],
               model=model or QC_MODEL, timeout=timeout)
    import re as _re
    m = _re.search(r"\{.*\}", r["text"], _re.S)
    if not m:
        raise BlacklightError(f"质检模型没回 JSON：{r['text'][:200]}")
    v = _json.loads(m.group(0))
    # 模型给的 verdict 只作参考，**最终判定由这里的规则算**（免得它嘴软放行）
    fail = []
    if reference and v.get("structure_same") is False:
        fail.append("结构与基准图不一致：" + str(v.get("structure_diff") or ""))
    if v.get("third_party_brand"):
        fail.append("出现第三方品牌：" + str(v.get("brand_detail") or ""))
    if t == 33:
        if v.get("texts_exact_match") is False:
            fail.append("卖点文案与预期不符：实际 %s" % (v.get("text_found") or []))
    elif v.get("unexpected_text"):
        fail.append("出现不该有的文字：%s" % (v.get("text_found") or []))
    if v.get("product_text_garbled"):
        # 保留商品自带文字是对的，但模型重绘小字常糊成伪汉字——那会直接暴露"这图是生成的"
        fail.append("商品上的文字被画成乱码：" + str(v.get("garbled_detail") or ""))
    if t == 31:
        if v.get("bg_pure_white") is False:
            fail.append("背景不是纯白")
        if v.get("props_present"):
            fail.append("商品上/内摆了道具（白底图要裸商品）")
    if t == 36 and v.get("bg_transparent") is False:
        fail.append("背景不透明（当心模型画棋盘格假装透明）")
    return {"ok": not fail, "failures": fail, "raw": v,
            "model": model or QC_MODEL, "usage": r.get("usage"),
            "note": "verdict 以本函数算出的 failures 为准，模型自报的 verdict 仅参考"}


# --------------------------------------------------------------------------- #
# 5.5) 兄弟 SKU 复用 —— 按**平台自己的分组口径**，不是按主图
# --------------------------------------------------------------------------- #
# ★★平台的分组**只看你选的那一个轴，其它轴是不是外观差异它不管**（2026-08-18 实撞）：
#   SPU 10035490905912（鞋架）按 attr_index=2 分组时 8 个 SKU 全在一组（都是"颜色=加厚"），
#   但组内有 奶油白/多巴胺 两种配色、2~5 层四种层数 —— 照它复用会把
#   「奶油白5层」的图挂到「多巴胺2层」上。
#   ⇒ 平台口径**只解决了"按哪个轴分组"，没解决"同组是否真的长得一样"**，必须再过一道外观词闸。
# ⚠️**别指望靠"外观词表"自动判**：属性名会骗人 —— 同一个 SPU 里名为「卖点」的轴装的其实是
#   配色（奶油白/多巴胺），名为「颜色」的轴装的却是"加厚"。词表拦得住「层数」，拦不住「卖点」。
#   ⇒ 本函数**列出组内所有取值不同的属性，一律视为冲突**（默认拒绝）；
#     哪些属性"不影响外观"是**领域判断**（服装的尺码不影响图，收纳柜的层数影响），
#     必须由调用方用 `ignore_attrs` 显式豁免，豁免项会进 confirm 指纹、可审计。
# ★★**尺寸差异不影响出图，颜色/款式差异影响** —— 2026-08-18 用户拍板的领域规则。
#   判据放在**取值形态**上，不放在属性名上：属性名会骗人（名为「卖点」的轴装的是配色，
#   名为「颜色」的轴装的是"加厚"）。做法是把尺寸词从取值里剥掉，比较**残值**：
#     「长80cm宽40cm」→ 残值空        ⇒ 只差尺寸，可复用
#     「白色【三层】高80cm」→ 残值「白色【三层】」 ⇒ 还差配色和层数，不可复用
#   ⚠️只剥**带长度单位**的数字，绝不剥「5层」「3件」这类——层数/件数是看得见的结构差异。
_SIZE_UNITS = r"(?:cm|CM|厘米|mm|MM|毫米|m|M|米|寸|英寸|inch|吋)"

# ★**整条属性就是尺寸分量**的轴：值常是**无单位裸数字**（长="79"、宽="58"），
#   靠值的形态认不出来，只能按属性名整条丢弃。`连接符`（值是 "*"）是尺寸三件套的连接符。
_SIZE_ATTR_NAMES = ("长", "宽", "高", "厚", "深", "直径", "口径", "边长",
                    "尺寸", "尺码", "规格", "连接符")

_SIZE_PATS = None


def _size_pats():
    global _SIZE_PATS
    if _SIZE_PATS is None:
        _SIZE_PATS = [
            # 带单位：长80cm / 高47cm / 25*16.5*10cm
            re.compile(r"(?:长|宽|高|厚|深|直径|口径|边长|尺寸|规格)?\s*\d+(?:\.\d+)?\s*"
                       r"(?:[*xX×]\s*\d+(?:\.\d+)?\s*)*" + _SIZE_UNITS),
            # 无单位的多维连乘：30.5*20*12（至少两个 * 分量才算尺寸，避免误吃 "滑轮*6"）
            re.compile(r"\d+(?:\.\d+)?(?:\s*[*xX×]\s*\d+(?:\.\d+)?){2,}"),
            # 号数分级：巨大号/特大号/超大号/加大号/大号/中号/小号 与 3号/4号
            re.compile(r"(?:巨|特|超|加)?[大中小]号|\d+号"),
            # 容量：18L / 8L / 310升 / 500ml —— 容量差本质是尺寸差（用户口径同尺寸）
            re.compile(r"\d+(?:\.\d+)?\s*(?:L|l|升|ml|mL|ML|毫升)"),
            # 字母号数：XL号 / M号 / XXL号（注意要放在容量之后，否则 "18L" 会被当成 L号）
            re.compile(r"(?:XS|S|M|L|XL|XXL|XXXL|[2-5]XL)号"),
        ]
    return _SIZE_PATS


def _size_strip(v: str) -> str:
    s = str(v or "")
    for p in _size_pats():
        s = p.sub("", s)
    return re.sub(r"[\s\-_/、,，]+", "", s)


def _size_key(row) -> tuple:
    """给一个 SKU 估个"尺寸大小"，用来在只差尺寸的组里**挑最大的那款**。

    ★用户口径（2026-08-18）：有尺寸差异的 SKU，素材统一放**尺寸最大**那款的。
    量纲取法：先看 长/宽/高 这类纯数字轴的乘积，取不到再退回值里所有带单位/连乘的数字之积，
    再退回号数分级（巨大>特大>超大>加大>大>中>小）。
    """
    a = row.get("sale_attrs") or {}
    dims = []
    for k, v in a.items():
        if any(k.startswith(n) for n in ("长", "宽", "高", "厚", "深", "直径", "边长")):
            m = re.search(r"\d+(?:\.\d+)?", str(v))
            if m:
                dims.append(float(m.group()))
    if dims:
        vol = 1.0
        for d in dims:
            vol *= d
        return (2, vol)
    blob = " ".join(str(x) for x in a.values())
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?\s*(?=[*xX×]|" + _SIZE_UNITS + ")", blob)]
    if nums:
        vol = 1.0
        for d in nums:
            vol *= d
        return (1, vol)
    grades = {"小号": 1, "中号": 2, "大号": 3, "加大号": 4, "超大号": 5, "特大号": 6, "巨大号": 7}
    best = 0
    for g, s in grades.items():
        if g in blob:
            best = max(best, s)
    return (0, best)


def pick_largest(members):
    """只差尺寸的一组里挑尺寸最大的 SKU 当素材源。"""
    return max(members, key=_size_key) if members else None


# 数量维度：【一个装】【2个装】【四只装】【6件套】【单个】… —— 归一化掉，见 appearance_sig
_QTY_PAT = re.compile(r"(?:\d+|[一两二三四五六七八九十])\s*(?:个|只|件|支|条|包|双|套|组)\s*(?:装|套)?"
                      r"|单[个只件]\s*装?")
_EMPTY_BRACKET = re.compile(r"[【\[（(]\s*[】\]）)]")


def _qty_strip(v: str) -> str:
    """剥掉「N个装」这类**数量**描述（外观相同、只是几件一起卖）。

    ★2026-08-19 用户拍板：**数量视为同外观、共用素材**。理由是这批要补的全是
    场景图/卖点图（白底图与透明图已基本齐全），共用不影响"裸商品图"的准确性；
    代价是四个装的场景图里可能只出现 1~2 件，用户接受。
    省了 36% 的生成量（41 个 SPU：426 → 272 次，207 组 → 152 组）。
    ⚠️注意只吃**带数量词**的形态：「双开门」「三层」「五宫格」不含数字+量词组合，不会被误吃。
    """
    return _EMPTY_BRACKET.sub("", _QTY_PAT.sub("", str(v or "")))


def appearance_sig(row) -> str:
    """外观签名：**尺寸与数量维度全部归一化掉**后的销售属性指纹。签名相同 ⇒ 认为长得一样。

    ★这是**分组的唯一真相**：`appearance_groups` / `reuse_plan` / 批处理脚本都走它，
    别在调用方再各写一份（写歪了就会出现"批量能复用、工具却拒绝"这种自相矛盾）。
    归一化两个维度：**尺寸**(`_size_strip`，含号数/容量) 与 **数量**(`_qty_strip`，N个装)。
    """
    a = row.get("sale_attrs") or {}
    return _json.dumps(
        {k: ("" if any(k.startswith(n) for n in _SIZE_ATTR_NAMES)
             else _qty_strip(_size_strip(v)))
         for k, v in sorted(a.items())}, ensure_ascii=False, sort_keys=True)


def appearance_groups(product_id) -> list:
    """把 SPU 下的 SKU 按外观分组，每组给出**尺寸最大**的那个当素材源。"""
    rows = M.list_sku(product_id)["rows"]
    g = {}
    for r in rows:
        g.setdefault(appearance_sig(r), []).append(r)
    return [{"signature": k, "members": v, "src": pick_largest(v),
             "sku_ids": [m["sku_id"] for m in v]} for k, v in g.items()]


def size_only_diff(members) -> dict:
    """组内差异是否**仅限尺寸**。是则可复用（用户口径：尺寸差异不明显，颜色/款式明显）。"""
    keys = set()
    for m in members:
        keys |= set((m.get("sale_attrs") or {}).keys())
    residual_sigs, size_attrs, other_attrs = set(), [], []
    for m in members:
        residual_sigs.add(appearance_sig(m))            # ★与分组同一套口径，别再写第二份
    for k in sorted(keys):
        raw = {str((m.get("sale_attrs") or {}).get(k)) for m in members}
        if len(raw) <= 1:
            continue
        if any(k.startswith(n) for n in _SIZE_ATTR_NAMES):
            size_attrs.append(k)                        # 整条就是尺寸分量
            continue
        res = {_size_strip((m.get("sale_attrs") or {}).get(k)) for m in members}
        (size_attrs if len(res) == 1 else other_attrs).append(k)
    ok = len(residual_sigs) == 1 and bool(keys)
    return {"size_only": ok, "size_attrs": size_attrs, "other_attrs": other_attrs,
            "note": "残值一致 ⇒ 只差尺寸，可复用" if ok else
                    ("残值仍有差异（%s）⇒ 含颜色/款式差异，不可复用" % other_attrs if other_attrs
                     else "属性解析为空，判不了")}


def _appearance_conflicts(members, ignore_attrs=None) -> list:
    """组内成员之间**取值不同**的属性。默认全算冲突；`ignore_attrs` 里的显式豁免。

    ★★两层判断，缺一不可（少了第二层会**假放行**）：
      ① 解析出的 `sale_attrs` 逐项比对；
      ② **原始签名 `sale_attr_sig` 兜底**——`attrValueAlias` 缺失时 `sale_attrs` 会是 `{}`，
        此时第①层看不出任何差异。实撞：SPU 10034654766253 的 **192 个 SKU 解析全空**，
        但签名各不相同、SKU 名里明写着「长80宽40」vs「长50宽30」——只靠第①层会判成"可复用"，
        然后把一个尺寸的图挂到全部 192 个 SKU 上。
    """
    ig = {str(x) for x in (ignore_attrs or [])}
    keys = set()
    for m in members:
        keys |= set((m.get("sale_attrs") or {}).keys())
    out = []
    for k in sorted(keys):
        vals = {str((m.get("sale_attrs") or {}).get(k)) for m in members}
        if len(vals) > 1 and k not in ig:
            out.append({"attr": k, "values": sorted(vals)})
    # ② 原始签名兜底：签名有差异但一条都没解析出来 ⇒ 判不了，按冲突处理（宁可不复用）
    sigs = {_json.dumps(m.get("sale_attr_sig") or {}, sort_keys=True) for m in members}
    if len(sigs) > 1 and not out:
        out.append({"attr": "(销售属性未能解析)", "values": ["签名不一致但 attrValueAlias 缺失"],
                    "why": "解析为空≠没有差异；判不了就不复用"})
    return out


def sibling_groups(product_id, attr_index: int = 1, ignore_attrs=None) -> dict:
    """按平台「批量应用相同颜色/相同尺码」的口径给 SPU 下的 SKU 分组。

    ★★**别用"主图相同"当复用判据**（2026-08-18 实撞反例）：
    SPU 10035824677493 里，SKU ...176（大开门-**带**木盖）和 ...177（大开门-**无**木盖）
    **五张主图完全一样**——商家把同一张图套给了两个款；而同 SPU 的 ...175/...174
    （同样是带盖/无盖之差）主图**却不同**。
    所以"主图相不相同"反映的是商家上传时偷没偷懒，**不是商品外观的真实分组**。
    照它复用，会把无木盖的白底图挂到带木盖的 SKU 上。

    平台的口径是**销售属性**：前端 `handleBatchUse` 取 `saleAttrInfos` 里 `index==N` 的那一项、
    去掉 `index` 后序列化比对，相同即同组（`values=1`→第1项"相同颜色"、`2`→第2项"相同尺码"、
    `null`→"全部"）。销售属性本来就是外观维度的定义，比主图哈希权威。
    """
    rows = M.list_sku(product_id)["rows"]
    groups = {}
    for r in rows:
        k = (r.get("sale_attr_sig") or {}).get(int(attr_index))
        groups.setdefault(k, []).append(r)
    out = []
    for k, members in groups.items():
        conf = _appearance_conflicts(members, ignore_attrs) if len(members) > 1 else []
        out.append({"signature": k,
                    "sku_ids": [m["sku_id"] for m in members],
                    "names": [m["sku_name"] for m in members],
                    # ★同组 ≠ 长得一样：还要组内其它属性没有外观差异才敢复用
                    "appearance_conflicts": conf,
                    "reusable": len(members) > 1 and not conf})
    return {"product_id": str(product_id), "attr_index": int(attr_index),
            "groups": out, "sku_count": len(rows),
            "note": "同组内素材可复用；组内只有 1 个 SKU 说明该款式独一份，必须单独生成"}


def reuse_plan(product_id, src_sku_id, material_types=None, attr_index: int = 1,
               scope: str = "appearance", ignore_attrs=None, waive_size: bool = True) -> dict:
    """把源 SKU 已有素材复用给同组兄弟 SKU 的方案（dry-run）。

    `scope`: **`appearance`（默认）= 外观签名相同**（尺寸已归一化，与批处理同口径）；
    `same_attr` = 平台「相同颜色/尺码」单轴口径（仅作对照，不建议）；`all` = 同 SPU 全部兄弟。
    复用的是**同一张图的 URL**，不重新生成也不重复占图床——这正是平台批量应用在做的事。
    """
    rows = M.list_sku(product_id)["rows"]
    src = next((r for r in rows if str(r["sku_id"]) == str(src_sku_id)), None)
    if not src:
        raise BlacklightError(f"源 SKU {src_sku_id} 不在 SPU {product_id} 下")
    if scope == "all":
        targets = [r for r in rows if str(r["sku_id"]) != str(src_sku_id)]
        basis = "批量应用全部（同 SPU 所有兄弟）"
    elif scope == "same_attr":
        # 平台「相同颜色/尺码」的原口径，留作对照 —— **不建议当默认**：它只看单个轴，
        # 同组里其它轴的颜色/款式差异它不管（实撞：8 个 SKU 全归一组但含两种配色四种层数）。
        sig = (src.get("sale_attr_sig") or {}).get(int(attr_index))
        targets = [r for r in rows if str(r["sku_id"]) != str(src_sku_id)
                   and (r.get("sale_attr_sig") or {}).get(int(attr_index)) == sig]
        basis = f"销售属性第 {attr_index} 项取值相同（平台「相同颜色/尺码」口径，仅作对照）"
    else:                                               # appearance（默认）
        sig = appearance_sig(src)
        targets = [r for r in rows if str(r["sku_id"]) != str(src_sku_id)
                   and appearance_sig(r) == sig]
        basis = "外观签名相同（尺寸维度已归一化；批处理与本函数同一口径）"
    # ★`waive_size=True`（默认，用户 2026-08-18 口径）：只差尺寸不算冲突，走外观签名比对；
    #   关掉则退回"任何属性有差异都拦下"。
    if targets and waive_size:
        conflicts = ([] if len({appearance_sig(x) for x in [src] + targets}) == 1
                     else _appearance_conflicts([src] + targets, ignore_attrs))
    else:
        conflicts = _appearance_conflicts([src] + targets, ignore_attrs) if targets else []
    if conflicts:
        targets = []            # 组内存在外观差异 —— 平台会放行，我们拦下
    have = {int(k): v for k, v in (src.get("materials") or {}).items()}
    # ★`have` 的键是**线材类型**(31/32/33/36)，调用方给的 material_types 往往是**素材位号**
    #   (3201/3202)——直接 `int(t) in have` 会永远落空，items 空转，而 reuse() 的
    #   `all([])` 又返回 True，表现成"ok=True 但一张没挂"（2026-08-19 实撞，standalone
    #   路径一直没验成功就是栽在这里）。这里两种写法都收。
    req = {int(t) for t in material_types} if material_types else None
    items = []
    for t in sorted(have):
        for m in have[t]:
            order = int(m.get("order") or 0)
            # 线材 32 底下是两个位（场景图1/2），按 order 还原成 3201/3202 再往下传，
            # 否则 bind 拿不到 order（_bind_body 只对 3201/3202 自动折算），两张都会挤进同一个位。
            # ⚠️★**读回来的 order 是 1-based，写进去的 imgOrder 是 0-based**（_WIRE_TYPE:
            #   3201→(32,0)、3202→(32,1)）。实测锚点：按 3201 挂的场景图1 读回来 order=1、
            #   按 3202 挂的读回来 order=2。照 0-based 判会把两张都算成场景图2
            #   （2026-08-19 实撞：目标 SKU 场景图1 空着、场景图2 里放的是场景图1 的图）。
            slot = (3202 if order >= 2 else 3201) if t == 32 else t
            if req is not None and slot not in req and t not in req:
                continue
            items.append({"type": slot, "wire": t, "name": M.MATERIAL_TYPES.get(slot, slot),
                          "url": m["url"], "order": order})
    return {"product_id": str(product_id), "src_sku_id": str(src_sku_id),
            "basis": basis, "target_sku_ids": [r["sku_id"] for r in targets],
            "target_names": [r["sku_name"] for r in targets],
            "items": items,
            "will_do": "先 batchUnBindMaterial 解绑目标位，再用同一张 imgUrl 挂上去（平台批量应用的原样动作）",
            "appearance_conflicts": conflicts, "ignored_attrs": list(ignore_attrs or []),
            "waive_size": bool(waive_size),
            "blocked": ("组内这些属性取值不同：%s —— 平台按单轴分组会放行，但复用可能挂错图，已拦下。"
                        "确认某项不影响外观(如服装尺码)就把它加进 ignore_attrs 再跑"
                        % [c["attr"] for c in conflicts]) if conflicts else
                       (None if targets else
                        "同组没有其它 SKU —— 该款式独一份，只能各自生成"),
            "warning": "⚠️复用前请确认两个 SKU **外观确实一致**：销售属性分组是平台口径，"
                       "但属性值本身由商家填写，仍可能把不同外观归进一组。"}


# --------------------------------------------------------------------------- #
# 5.7) 通用卖点（文案）—— 生成 / 保存 / 复用
# --------------------------------------------------------------------------- #
# 页面「通用卖点(0/8)」那一列。和素材图是两条链路：图走 batchBind，文案走 shortTitleSeller.save。
#   读：getProductAllSkuMaterialInfo → childList[].smartContentSellList（现值）+ sellMaxNum（条数上限）
#   平台 AI 建议：acquireCommonSellPoint（本 SKU 实测回 []，即平台没货，得自己生成）
#   写：dsm.media.text.shortTitleSeller.save
#       {apiWareTextMaterialInfo:{sellPoint:[...], skuIds:[...], shortTitle?}}
# ⚠️**条数上限用服务端给的 `sellMaxNum`（实测 3），别硬编码**；单条 ≤8 字（前端 maxlength）。
_SELL_MAX_CHARS = 8
_SELL_MIN_CHARS = 2
_SELL_GATE = ConfirmGate("osw/material_sellpoints")


def sellpoints_current(product_id, sku_id) -> dict:
    """读当前卖点 + 条数上限 + 平台 AI 建议。"""
    d = M._call("dsm.upload.material.ware.getProductAllSkuMaterialInfo",
                {"apiProductQuery": {"productId": str(product_id), "sceneType": 1,
                                     "skuFieldSet": ["saleAttrs", "logo", "sellMaxNum"]}})
    me = next((c for c in (d.get("childList") or [])
               if str(c.get("productId")) == str(sku_id)), {})
    try:
        ai = M._call("dsm.media.service.materialAlgorithmService.acquireCommonSellPoint",
                     {"skuId": str(sku_id)}) or []
    except Exception:                                   # noqa: BLE001
        ai = []
    cur = me.get("smartContentSellList") or []
    return {"sku_id": str(sku_id), "current": cur, "count": len(cur),
            "max_num": int(me.get("sellMaxNum") or 3),
            "max_chars": _SELL_MAX_CHARS,
            "platform_ai": [x for x in ai if isinstance(x, str) and x],
            "short_title": me.get("shortTitle")}


def _valid_points(pts, max_num, max_chars=_SELL_MAX_CHARS):
    out, bad = [], []
    for p in pts:
        p = str(p or "").strip()
        if not p:
            continue
        if not (_SELL_MIN_CHARS <= len(p) <= max_chars):
            bad.append(f"{p}({len(p)}字)")
            continue
        if p not in out:
            out.append(p)
    return out[:max_num], bad


def sellpoints_generate(product_id, sku_id, n: int = None, model: str = None) -> dict:
    """生成通用卖点：**平台 AI 优先，没有才用 llm-gw 生成**。

    平台 `acquireCommonSellPoint` 是免费且更贴平台口径的，本 SKU 实测回 `[]`（平台没货），
    这时才落到大模型。生成的词会按「2~8 字、去重、不超过 sellMaxNum 条」硬过滤。
    """
    from blacklight.llm import gateway as G
    cur = sellpoints_current(product_id, sku_id)
    n = int(n or cur["max_num"])
    if cur["platform_ai"]:
        pts, bad = _valid_points(cur["platform_ai"], n)
        if pts:
            return {**cur, "generated": pts, "source": "平台AI acquireCommonSellPoint",
                    "dropped": bad}
    d = describe(product_id, sku_id)
    ctx = enrich(product_id, sku_id)
    prompt = (f"商品：{d['text']}\n类目：{ctx.get('category_path') or '未知'}\n"
              f"请为这个商品写 {n} 条电商「通用卖点」短语，用于商品列表页展示。要求：\n"
              f"①每条 {_SELL_MIN_CHARS}~{_SELL_MAX_CHARS} 个汉字，越短越好；\n"
              "②只讲商品本身可验证的功能/材质/结构特性；\n"
              "③**不得出现**绝对化用语（最/第一/顶级/唯一）、疗效或安全承诺、价格与促销信息、"
              "品牌名、赠品承诺；\n"
              "④彼此不重复。\n"
              '只输出 JSON 数组，例如 ["免安装","可移动","大容量"]，不要任何解释。')
    raw = G.chat_json(prompt, model=model)
    pts, bad = _valid_points(raw if isinstance(raw, list) else [], n)
    return {**cur, "generated": pts, "source": "llm-gw", "dropped": bad,
            "warning": "⚠️卖点是**对外承诺**，上线前必须人工过一遍（夸大/绝对化/赠品承诺都是合规风险）"}


def sellpoints_save_dryrun(product_id, sku_ids, points) -> dict:
    """卖点保存 dry-run：过滤非法词、回显将写入的内容与被剔除的词，出 confirm_token。"""
    cur = sellpoints_current(product_id, list(sku_ids)[0])
    pts, bad = _valid_points(points, cur["max_num"])
    if not pts:
        raise BlacklightError(f"没有合法卖点（需 2~8 字）；被剔除：{bad}")
    body = {"apiWareTextMaterialInfo": {"sellPoint": pts,
                                        "skuIds": [str(x) for x in sku_ids]}}
    return {"action": "保存通用卖点（dsm.media.text.shortTitleSeller.save）",
            "sku_ids": [str(x) for x in sku_ids], "points": pts, "dropped": bad,
            "max_num": cur["max_num"], "current": cur["current"],
            "warning": "⚠️**会覆盖该 SKU 现有卖点**；卖点是对外承诺，先人工确认文案合规",
            "confirm_token": _SELL_GATE.body_token(body), "body": body}


@audited("osw", "material_sellpoints")
def sellpoints_save(product_id, sku_ids, points, *, confirm: str = "") -> dict:
    """真写卖点并**自动回读比对**。⚠️覆盖式，不是追加。"""
    plan_ = sellpoints_save_dryrun(product_id, sku_ids, points)
    body = plan_["body"]
    _SELL_GATE.check_body(confirm, body)
    d = M._call("dsm.media.text.shortTitleSeller.save", body)
    back = sellpoints_current(product_id, list(sku_ids)[0])
    return {"ok": True, "raw": d, "readback": back["current"],
            "matched": back["current"] == plan_["points"],
            "note": "以 readback 为准；不一致说明平台做了截断或改写"}


# --------------------------------------------------------------------------- #
# 5.8) 复用真执行（reuse_plan 的写侧）
# --------------------------------------------------------------------------- #
_REUSE_GATE = ConfirmGate("osw/material_reuse")


@audited("osw", "material_reuse")
def reuse(product_id, src_sku_id, material_types=None, attr_index: int = 1,
          scope: str = "appearance", ignore_attrs=None, waive_size: bool = True, *,
          confirm: str = "", with_sellpoints: bool = False, replace: bool = False) -> dict:
    """把源 SKU 的素材（可选：加卖点文案）复用给同组兄弟 SKU。

    默认**只填空位**（`existNoReplace=True`，与批处理已活体的那条路径一致），
    不会冲掉目标 SKU 已审核通过的素材。
    `replace=True` 才走平台「批量应用」的原动作：先 `batchUnBindMaterial` 解绑再挂 ——
    ⚠️那会把目标已有的图**覆盖掉**，且已通过的审核要重走。
    两种模式都**不重新生成、不重复占图床**（复用的是同一张 imgUrl）。
    """
    p = reuse_plan(product_id, src_sku_id, material_types, attr_index, scope,
                   ignore_attrs, waive_size)
    if not p["target_sku_ids"]:
        return {"ok": False, "skipped": True, "reason": p["blocked"], "plan": p}
    # ★源上没有可复用的素材时**必须显式失败**：早先这里会一路走到 `all([])`，
    #   返回 ok=True 却一张没挂（空集上的真空真值）。空结果先证伪，别当成功。
    if not p["items"]:
        return {"ok": False, "skipped": True,
                "reason": "源 SKU 在所选素材位上没有图可复用（检查 material_types 是否写成了"
                          "该 SKU 实际没有的位）", "plan": p}
    body = {"product_id": str(product_id), "src": str(src_sku_id),
            "targets": [str(x) for x in p["target_sku_ids"]],
            "items": [{"type": i["type"], "url": i["url"]} for i in p["items"]],
            "with_sellpoints": bool(with_sellpoints), "replace": bool(replace),
            "ignored_attrs": sorted(str(x) for x in (ignore_attrs or [])),
            "waive_size": bool(waive_size)}
    _REUSE_GATE.check_body(confirm, body)
    targets = [str(x) for x in p["target_sku_ids"]]
    types = sorted({i["type"] for i in p["items"]})
    steps = []
    # 只有 replace 模式才解绑（平台批量应用就是这么做的）；默认不解绑，只补空位
    if replace:
      try:
        M._call("dsm.media.material.WareMaterialService.batchUnBindMaterial",
                {"batchUnBindMaterialParam": {
                    "psIds": [{"productId": str(product_id), "skuIds": targets}],
                    "typeSet": [M._WIRE_TYPE.get(t, (t, 0))[0] for t in types]}})
        steps.append({"step": "batchUnBindMaterial", "ok": True, "types": types})
      except Exception as e:                            # noqa: BLE001
        steps.append({"step": "batchUnBindMaterial", "ok": False, "error": str(e)[:180]})
    for i in p["items"]:
        imgs = [{"url": i["url"], "type": i["type"], "order": i.get("order", 0)}]
        try:
            r = M.bind(product_id, targets, imgs, exist_no_replace=not replace,
                       confirm=M._BIND_GATE.body_token(
                           M._bind_body(product_id, targets, imgs, exist_no_replace=not replace)))
            steps.append({"step": "bind", "type": i["type"], "ok": r["ok"], "errors": r["errors"]})
        except Exception as e:                          # noqa: BLE001
            steps.append({"step": "bind", "type": i["type"], "ok": False, "error": str(e)[:180]})
        time.sleep(1)
    if with_sellpoints:
        cur = sellpoints_current(product_id, src_sku_id)
        if cur["current"]:
            try:
                b = {"apiWareTextMaterialInfo": {"sellPoint": cur["current"], "skuIds": targets}}
                M._call("dsm.media.text.shortTitleSeller.save", b)
                steps.append({"step": "sellpoints", "ok": True, "points": cur["current"]})
            except Exception as e:                      # noqa: BLE001
                steps.append({"step": "sellpoints", "ok": False, "error": str(e)[:180]})
    time.sleep(3)
    readback = {}
    for r in M.list_sku(product_id)["rows"]:
        if str(r["sku_id"]) in targets:
            readback[str(r["sku_id"])] = sorted((r.get("materials") or {}).keys())
    return {"ok": all(s.get("ok") for s in steps), "basis": p["basis"],
            "targets": targets, "steps": steps, "readback": readback}


# --------------------------------------------------------------------------- #
# 5.9) 组内传播：把**源已有**的素材补给同组缺的成员（不生成、不耗 token）
# --------------------------------------------------------------------------- #
_PROP_GATE = ConfirmGate("osw/material_propagate")


def propagate_plan(product_id, group) -> dict:
    """组内传播的**方案**（只读）：算出要把哪张图补到哪些 SKU 的哪个位，并给 confirm_token。

    ★★2026-08-24 审查补的：此前这条写路径的确认是 MCP 层一个**裸 bool**（`confirm=True` 即写），
      没有"预览-确认绑定" —— 真写时 token 是函数内部现算现填的，永远自证有效，
      等于同域其它 material 工具那套 `confirm_token 必须等于 dry-run 输出`的纪律被绕过。
      现在方案与真写共用同一个 body 指纹，你确认的就是你看过的那一份。"""
    r = propagate_group(product_id, group, dry_run=True)
    body = _prop_body(product_id, r["items"])
    return {**r, "confirm_token": _PROP_GATE.body_token(body)}


def _prop_body(product_id, items) -> dict:
    """传播动作的规范化 body —— token 只认它，改一个目标 SKU 或换一张图，token 就变。"""
    return {"product_id": str(product_id),
            "items": sorted([{"type": int(i["type"]), "url": str(i["url"]),
                              "to": sorted(str(x) for x in i["to"])} for i in (items or [])],
                            key=lambda x: (x["type"], x["url"]))}


def propagate_group(product_id, group, dry_run: bool = False, confirm: str = None) -> dict:
    """把外观组里**源 SKU 已有**的素材，补给同组还缺的成员。

    ★★**没有这一步会静默漏挂**（2026-08-18 实撞 27 个空位：透明图 17 + 白底图 10）：
    批量的待办是按**源 SKU** 算的，源在跑批前就已有的位会被整组跳过，
    于是同组成员的空位永远补不上——页面上看就是"有的 SKU 素材还是空的"。
    这一步零成本（复用同一张 imgUrl），**放在每组生成之前跑**。
    """
    src = group["src"]
    smat = {int(k): v for k, v in (src.get("materials") or {}).items()}
    todo, done, failed = [], 0, []
    if not dry_run:
        # 真写必须带上 propagate_plan 给的 token（对同一份 items 的指纹）
        _plan_items = []
        for _t, _m in sorted(smat.items()):
            if not _m:
                continue
            _need = [str(x["sku_id"]) for x in group["members"]
                     if len(({int(k): v for k, v in (x.get("materials") or {}).items()}).get(_t) or [])
                     < len(_m)]
            if _need:
                _plan_items.append({"type": _t, "url": _m[0]["url"], "to": _need})
        _PROP_GATE.check_body(confirm, _prop_body(product_id, _plan_items))
    for t, mats in sorted(smat.items()):
        if not mats:
            continue
        need = [str(m["sku_id"]) for m in group["members"]
                if len(({int(k): v for k, v in (m.get("materials") or {}).items()}).get(t) or [])
                < len(mats)]
        if not need:
            continue
        todo.append({"type": t, "name": M.MATERIAL_TYPES.get(t, t),
                     "url": mats[0]["url"], "to": need})
        if not dry_run:
            try:
                M.bind(product_id, need, [{"url": mats[0]["url"], "type": t}],
                       exist_no_replace=True,
                       confirm=M._BIND_GATE.body_token(
                           M._bind_body(product_id, need,
                                        [{"url": mats[0]["url"], "type": t}],
                                        exist_no_replace=True)))
                done += len(need)
            except Exception as e:                      # noqa: BLE001
                # ★不再 `pass` 掉（2026-08-24 审查）：原来每个素材位的失败都被静默吞掉，
                #   调用方只能从 filled 计数**反推**是不是全成了——失败多少、为什么，一个字都没有。
                failed.append({"type": t, "name": M.MATERIAL_TYPES.get(t, t),
                               "to": need, "err": "%s: %s" % (type(e).__name__, str(e)[:120])})
    out = {"src_sku_id": str(src["sku_id"]), "items": todo,
           "filled": done, "dry_run": bool(dry_run)}
    if failed:
        out["failed"] = failed
        out["_warn"] = "有 %d 个素材位没挂上（见 failed）；filled=%d 只是成功的那部分，别当全成了" % (
            len(failed), done)
    return out


def slot_filled(materials: dict, slot: int) -> bool:
    """某个素材位是否已被占。**判空位一律走这里，别自己数个数。**

    ⚠️★场景图1/2 共用线材类型 32、靠 `order` 区分，而 order **可能不连续**
    （某张被驳回删掉，或只挂上了场景图2）。用 `len(32列表) > 索引` 来判，
    在"只有场景图2"时会读成"场景图1 已挂、场景图2 空着"——**方向正好反**，
    于是既漏补真正的空位，又去重复生成已有的位
    （2026-08-19 实撞 SKU 10232161163364：只有 order=2，却被判成缺 3202）。
    读回来的 order 是 **1-based**，写进去的 imgOrder 是 0-based。
    """
    mats = {int(k): v for k, v in (materials or {}).items()}
    wire, idx = M._WIRE_TYPE.get(int(slot), (int(slot), None))
    ms = mats.get(wire) or []
    if idx is None:
        return bool(ms)
    return any(int(m.get("order") or 0) == idx + 1 for m in ms)


def spu_gaps(product_id, slots=None) -> dict:
    """整个 SPU 还缺什么：按外观组给出「可传播补齐」与「必须生成」两类。**只读**。"""
    slots = [int(x) for x in (slots or DEFAULT_SLOTS)]
    out, need_gen, can_prop = [], 0, 0
    for g in appearance_groups(product_id):
        src = g["src"]
        prop = propagate_group(product_id, g, dry_run=True)
        gen = [s for s in slots if not slot_filled(src.get("materials"), s)]
        need_gen += len([x for x in gen if x != 36])
        can_prop += sum(len(i["to"]) for i in prop["items"])
        out.append({"src_sku_id": str(src["sku_id"]), "members": len(g["members"]),
                    "to_generate": gen, "to_propagate": prop["items"]})
    return {"product_id": str(product_id), "groups": out,
            "generate_calls": need_gen, "propagate_slots": can_prop,
            "note": "propagate 零成本（复用已有图）；generate 每次约 5.7k token"}


# --------------------------------------------------------------------------- #
# 6) 编排：一个 SKU 一次补齐
# --------------------------------------------------------------------------- #
_GATE = ConfirmGate("osw/material_autofill")
DEFAULT_SLOTS = (31, 36, 3201, 3202, 33)


def _ordered(slots):
    """36 必须排在 31 之后 —— 它是从白底图抠出来的，不是生成的。
    3202 也必须在 3201 之后（平台前端有「没有场景图1不让传场景图2」的约束）。"""
    rank = {31: 0, 36: 1, 3201: 2, 3202: 3, 33: 4, 34: 5}
    return sorted({int(s) for s in slots}, key=lambda s: rank.get(s, 9))


def plan(product_id, sku_id, material_types=DEFAULT_SLOTS, sell_points=None) -> dict:
    """dry-run：把参考图、商品描述、每个位的 prompt 全摊开，**不生图不花钱**。"""
    slots = _ordered(material_types)
    d = describe(product_id, sku_id)
    refs = pick_references(product_id, sku_id)
    ctx = enrich(product_id, sku_id)
    # 卖点词优先级：调用方指定 > **平台上已存的通用卖点** > 标题【】里抽的 > 放弃 33 位
    # ★平台卖点排在标题前面：它是按"2~8字、可验证、无绝对化用语"生成并过审的，
    #   而标题【】抠出来的常是「2只装」「赠滑轮」这种数量/赠品碎片，画到图上既不像卖点也有合规风险。
    # ⚠️最后一档不能是硬编码兜底词：那会画出与商品无关的承诺，且 qc 的 expect_texts 对不上，
    #   必然浪费一次生图（2026-08-18 批量时实撞）。
    pts = list(sell_points) if sell_points else []
    if not pts:
        try:
            pts = list(sellpoints_current(product_id, sku_id)["current"] or [])
        except Exception:                               # noqa: BLE001
            pts = []
    if not pts:
        pts = list(d["sell_points"])
    have = {}
    for r in M.list_sku(product_id)["rows"]:
        if str(r["sku_id"]) == str(sku_id):
            have = {int(k): len(v) for k, v in (r.get("materials") or {}).items()}
    steps = []
    for s in slots:
        wire = M._WIRE_TYPE.get(s, (s, 0))[0]
        if s == 36:
            steps.append({"slot": 36, "name": "透明图", "how": "平台抠图 getMattingImage(白底图)",
                          "depends_on": "31 必须先挂好", "prompt": None})
            continue
        steps.append({"slot": s, "name": M.MATERIAL_TYPES.get(s, s), "how": "llm-gw 生图",
                      "wire_type": wire, "order": M._WIRE_TYPE.get(s, (s, 0))[1],
                      "prompt": build_prompt(s, d["text"], order=M._WIRE_TYPE.get(s, (s, 0))[1],
                                             sell_points=pts, refs_meta=refs_for(s, refs)[1],
                                             ctx=ctx, sku_name=d["sku_name"]),
                      "refs_used": len(refs_for(s, refs)[0])})
    body = {"product_id": str(product_id), "sku_id": str(sku_id),
            "slots": slots, "sell_points": pts}
    return {"product_id": str(product_id), "sku_id": str(sku_id),
            "slots": slots,
            "desc": d["text"], "sku_name": d["sku_name"], "context": ctx, "sell_points": pts,
            "sell_points_warning": d["note"],
            "references": refs, "already_filled": have, "steps": steps,
            "cost_hint": f"{len([s for s in slots if s != 36])} 次生图，约 30~60s/张、~3k token/张；"
                         f"抠图 0.7s 不计费",
            "warning": "⚠️生成图**必须人眼比对结构再上传**：模型会改结构（实撞：把一整扇大开门画成双开门）。"
                       "本函数只出方案，autofill 才会真写。",
            "confirm_token": _GATE.body_token(body)}


@audited("osw", "material_autofill")
def autofill(product_id, sku_id, material_types=DEFAULT_SLOTS, sell_points=None, *,
             out_dir: str = None, confirm: str = "", replace: bool = False,
             dry_images_only: bool = False, qc_on: bool = True,
             retry_on_qc_fail: bool = True) -> dict:
    """生成 → **质检** → 上传 → 挂位，一个 SKU 一次补齐。

    `dry_images_only=True` 只生成到本地并返回路径（先看图再决定挂不挂），不写平台。
    `qc_on`（默认开）：每张生成图上传前过 `qc()`；不合格重生一次，仍不合格则
    **不上传**并标 `blocked_by_qc`——宁可留空位，也别把画错结构的图挂到线上。
    抠图产物(36)不过质检：它是平台算法出的，不是模型画的。
    """
    from blacklight.core import paths as _paths
    from blacklight.pic import imgzone as Z
    p = plan(product_id, sku_id, material_types, sell_points)
    body = {"product_id": str(product_id), "sku_id": str(sku_id),
            "slots": p["slots"], "sell_points": p["sell_points"]}
    if not dry_images_only:
        _GATE.check_body(confirm, body)
    out_dir = out_dir or os.path.join(_paths.exports_dir("material_gen"), str(sku_id))
    os.makedirs(out_dir, exist_ok=True)
    refs = p["references"]["refs"]
    # 质检基准图用"结构基准"那张（同 SPU 已审核通过的白底图）；没有就只能查品牌/文字
    # ⚠️只有**同外观组**的干净图才能当结构基准；跨组的拿来比会把画对的判成画错的
    qc_ref = refs[0] if p["references"].get("clean_same_group") else None
    done, white_url = [], None

    for s in p["slots"]:
        rec = {"slot": s, "name": M.MATERIAL_TYPES.get(s, s)}
        try:
            if s == 36:
                if not white_url:
                    for r in M.list_sku(product_id)["rows"]:
                        if str(r["sku_id"]) == str(sku_id):
                            w = (r.get("materials") or {}).get(31) or []
                            white_url = w[0]["url"] if w else None
                if not white_url:
                    raise BlacklightError("没有白底图可抠 —— 36 依赖 31，先补 31")
                fp = os.path.join(out_dir, slot_filename(sku_id, 36))
                m = matting(white_url, MATTING_THROUGH, fp)
                rec.update({"how": "平台抠图", "path": fp, "bytes": m["bytes"]})
            else:
                g = generate(s, p["desc"], refs, out_dir,
                             os.path.splitext(slot_filename(sku_id, s))[0],
                             order=M._WIRE_TYPE.get(s, (s, 0))[1],
                             sell_points=p["sell_points"],
                             refs_meta=p["references"],
                             ctx=p.get("context"), sku_name=p.get("sku_name", ""))
                rec.update({"how": "llm-gw", "path": g["path"], "bytes": g["bytes"],
                            "seconds": g["seconds"]})
            # ★质检闸门：生成图**上传前**过一遍多模态质检，不合格重生一次
            if rec.get("how") == "llm-gw" and qc_on:
                v = qc(rec["path"], s, reference=qc_ref, expect_texts=p["sell_points"])
                # ⚠️首次结果**单独留档、不被重试结果覆盖**：覆盖了就永远看不到首次通过率，
                #   也就没法判断 prompt 是不是在退化（曾把这行写成覆盖，白底图重试过一次却看不出来）
                rec["qc_first"] = {"ok": v["ok"], "failures": v["failures"]}
                rec["qc"] = dict(rec["qc_first"])
                if not v["ok"] and retry_on_qc_fail:
                    g2 = generate(s, p["desc"], refs, out_dir,
                                  os.path.splitext(slot_filename(sku_id, s))[0] + "_retry",
                                  order=M._WIRE_TYPE.get(s, (s, 0))[1],
                                  sell_points=p["sell_points"], refs_meta=p["references"],
                                  ctx=p.get("context"), sku_name=p.get("sku_name", ""))
                    v2 = qc(g2["path"], s, reference=qc_ref, expect_texts=p["sell_points"])
                    rec["qc_retry"] = {"ok": v2["ok"], "failures": v2["failures"]}
                    rec["retried"] = True
                    if v2["ok"]:
                        rec.update({"path": g2["path"], "bytes": g2["bytes"]})
                        rec["qc"] = dict(rec["qc_retry"])       # 最终态；首次仍留在 qc_first
                    else:
                        # 两次都没过 —— **不上传**，留给人看，别把坏图挂到线上
                        rec["bound"] = False
                        rec["blocked_by_qc"] = True
                        done.append(rec)
                        time.sleep(2)
                        continue
                elif not v["ok"]:
                    rec["bound"] = False
                    rec["blocked_by_qc"] = True
                    done.append(rec)
                    time.sleep(2)
                    continue
            if dry_images_only:
                rec["bound"] = False
            else:
                up = Z.upload(rec["path"], os.path.basename(rec["path"]))
                rec["url"] = up["url"]
                if s == 31:
                    white_url = up["url"]
                r = M.bind(product_id, [sku_id],
                           [{"url": up["url"], "type": s}],
                           exist_no_replace=not replace,
                           confirm=M._BIND_GATE.body_token(
                               M._bind_body(product_id, [sku_id], [{"url": up["url"], "type": s}],
                                            exist_no_replace=not replace)))
                rec["bound"] = r["ok"]
                rec["errors"] = r["errors"]
            done.append(rec)
        except Exception as e:                          # noqa: BLE001
            # ★额度用尽要**立刻收工**：再试也不会过，只会把剩下的位一个个磨成失败记录
            #   （2026-08-18 实撞：批量在额度耗尽后又磨了 20 次）
            from blacklight.llm.gateway import QuotaExhausted as _QE
            rec["error"] = str(e)[:200]
            done.append(rec)
            if isinstance(e, _QE):
                rec["stopped_by_quota"] = True
                break
        time.sleep(2)

    readback = {}
    if not dry_images_only:
        time.sleep(4)
        for r in M.list_sku(product_id)["rows"]:
            if str(r["sku_id"]) == str(sku_id):
                readback = {int(k): [{"status": m["status_name"], "url": m["url"]} for m in v]
                            for k, v in (r.get("materials") or {}).items()}
    return {"sku_id": str(sku_id), "out_dir": out_dir, "steps": done,
            "readback": readback,
            "note": "status=3 审核中≠生效，走到 4 才算；驳回看 imageDiagnoseDataTextResults"}
