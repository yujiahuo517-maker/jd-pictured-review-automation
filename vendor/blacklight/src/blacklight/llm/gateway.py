"""blacklight.llm.gateway —— 内网大模型网关客户端（chat / 文生图 / 图生图）。

2026-08-17 逐个端点实测，见 docs/llm/NOTES_gateway.md。
"""
from __future__ import annotations

import base64
import json as _json
import os
import time
import urllib.error
import urllib.request

from blacklight.core import BlacklightError
from blacklight.core import paths as _paths

BASE_URL = "http://llm-gw.jd.local/v1"

# 2026-08-17 /v1/models 实测在册
MODELS = ["Claude-Opus-4.8-joybuilder", "GPT-5.5-joybuilder", "GPT-5.6-Luna-joybuilder",
          "GPT-5.6-Sol-joybuilder", "GPT-5.6-Terra-joybuilder", "GPT-image-2-joybuilder"]
DEFAULT_CHAT_MODEL = "GPT-5.6-Terra-joybuilder"
IMAGE_MODEL = "GPT-image-2-joybuilder"

DAILY_TOKEN_QUOTA = 3_000_000     # 平台给每人每天 300w token
IMAGE_TIMEOUT = 300               # 生图实测 24~28s，留足
CHAT_TIMEOUT = 120
BACKOFF = (3, 8, 20)              # 429 退避（限流实测：快速连发第 3 次就撞）


class QuotaExhausted(BlacklightError):
    """当日额度用尽。**和限流不是一回事**——限流等一会儿能过，额度用尽今天怎么等都不会过。

    两者都回 HTTP 429，body 里 `code=2007`「API Key请求当日额度已用完」才是额度。
    不区分的后果实测过：批量在额度耗尽后又磨了 20 次，每次还退避 3/8/20 秒，纯浪费。
    """


# --------------------------------------------------------------------------- #
# 密钥（不落代码）
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 从 o2 模型网关自动取密钥 —— 不用再手工粘
# --------------------------------------------------------------------------- #
# 页面 `o2.jd.com/hub/model/gateway/api-key-bacisInfo?id=<KEY_ID>&type=edit`
# 是嵌在 o2 里的微前端子应用 `llm-gateway-web.jd.com`；它打的
#   GET https://llm-gateway-web.jd.com/api/apiKey/detail?id=<KEY_ID>
# 其实是个**由客户端头指定目标的代理**：
#   x-proxy-opts: {"target":"https://llm-gateway-console.jd.com","pathRewrite":{"^/api/(.*)":"/$1"}}
# ⚠️不带这个头裸打 `/api/...` 一律 404（代理不知道往哪转）——我按路径猜了十几种全挂在这上面。
# **直连后端 `llm-gateway-console.jd.com` 更干净，且不需要代理头**，我们自己的 SSO 主票即可鉴权。
O2_KEY_HOST = "https://llm-gateway-console.jd.com"
O2_KEY_ID_DEFAULT = "17681"        # 个人 key 的 id（countConfig 实测 personalMaxCount=1，一人一把）


def fetch_key_from_o2(key_id: str = None, save: bool = True) -> dict:
    """从 o2 模型网关取本人的 API Key。**返回体里只给掩码值**，完整值只写进 gitignore 的凭证文件。

    `key_id` 就是那个页面 URL 里的 `id=`；不传取 credentials 里存的、再不行取默认。
    """
    from blacklight.core import auth as jd_auth, make_client
    kid = str(key_id or _cred("llm_gw_key_id") or O2_KEY_ID_DEFAULT)
    ck = jd_auth.get_cookie()
    if not ck:
        raise BlacklightError("无登录态：先 osw_login / yx_login")
    c = make_client(ck, origin="https://o2.jd.com", referer="https://o2.jd.com/",
                    content_type=None, timeout=25.0,
                    extra={"x-requested-with": "XMLHttpRequest"})
    with c:
        r = c.get(f"{O2_KEY_HOST}/apiKey/detail", params={"id": kid})
    try:
        j = r.json()
    except Exception as e:                              # noqa: BLE001
        raise BlacklightError(f"取 key 未返回 JSON（HTTP {r.status_code}）——登录态可能失效") from e
    d = (j or {}).get("data") or {}
    key = str(d.get("key") or "").strip()
    if not key:
        raise BlacklightError(f"未取到 key：{_json.dumps(j, ensure_ascii=False)[:200]}")
    if save:
        _save_cred({"llm_gw_key": key, "llm_gw_key_id": kid})
    return {"key_id": kid, "key_masked": key[:6] + "…" + key[-4:],
            "owner": d.get("owner"), "desc": d.get("desc"),
            "department": d.get("department"), "state": d.get("state"),
            "create_time": d.get("createTime"), "saved": bool(save),
            "note": "完整 key 只写进 runtime/credentials.json（已 gitignore），不在返回体里"}


def _cred(name: str):
    try:
        with open(_paths.credentials_path(), encoding="utf-8") as f:
            return (_json.load(f) or {}).get(name)
    except Exception:                                   # noqa: BLE001
        return None


def _save_cred(patch: dict) -> None:
    p = _paths.credentials_path()
    try:
        with open(p, encoding="utf-8") as f:
            d = _json.load(f) or {}
    except Exception:                                   # noqa: BLE001
        d = {}
    d.update(patch)
    with open(p, "w", encoding="utf-8") as f:
        _json.dump(d, f, ensure_ascii=False, indent=1)


def _key(auto: bool = True) -> str:
    """env → 凭证文件 → **自动从 o2 取**。最后这档让 key 轮换后不用人工介入。"""
    k = os.environ.get("JD_LLM_GW_KEY", "").strip()
    if k:
        return k
    k = str(_cred("llm_gw_key") or "").strip()
    if k:
        return k
    if auto:
        try:
            fetch_key_from_o2()
            return str(_cred("llm_gw_key") or "").strip()
        except Exception as e:                          # noqa: BLE001
            raise BlacklightError(
                "缺 llm-gw 密钥，且自动取失败：%s。可设环境变量 JD_LLM_GW_KEY，"
                "或先 osw_login 恢复登录态后重试。" % str(e)[:160]) from e
    raise BlacklightError("缺 llm-gw 密钥")


def status() -> dict:
    """有没有 key、key 从哪来、网关通不通（不消耗额度）。"""
    src = "env" if os.environ.get("JD_LLM_GW_KEY", "").strip() else "credentials.json"
    try:
        ms = list_models()
        return {"usable": True, "key_source": src, "models": ms,
                "daily_token_quota": DAILY_TOKEN_QUOTA}
    except BlacklightError as e:
        return {"usable": False, "key_source": src, "error": str(e)}


# --------------------------------------------------------------------------- #
# 底层
# --------------------------------------------------------------------------- #
def _post(path: str, payload: dict, timeout: int, retry: int = 2) -> dict:
    body = _json.dumps(payload).encode("utf-8")
    last = ""
    for attempt in range(retry + 1):
        req = urllib.request.Request(
            BASE_URL + path, data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + _key()})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return _json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            txt = e.read()[:300].decode("utf-8", "replace")
            last = f"HTTP {e.code}: {txt}"
            if e.code == 429 and _is_quota(txt):
                # ★额度用尽：**立刻抛专用异常，不重试**，让上层能早停而不是一路磨到底
                raise QuotaExhausted("llm-gw 当日额度已用尽：" + txt[:160])
            if e.code != 429 or attempt >= retry:       # 限流才值得重试
                break
        except Exception as e:                          # noqa: BLE001
            last = f"{type(e).__name__}: {str(e)[:200]}"
            if attempt >= retry:
                break
        time.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
    raise BlacklightError(f"llm-gw {path} 失败 —— {last}")


def _is_quota(body: str) -> bool:
    b = str(body or "")
    return ("2007" in b) or ("额度" in b) or ("quota" in b.lower())


def list_models() -> list:
    req = urllib.request.Request(BASE_URL + "/models",
                                 headers={"Authorization": "Bearer " + _key()})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            j = _json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:                              # noqa: BLE001
        raise BlacklightError(f"llm-gw /models 失败：{type(e).__name__} {str(e)[:150]}") from e
    return [m.get("id") for m in (j.get("data") or [])]


# --------------------------------------------------------------------------- #
# 文本
# --------------------------------------------------------------------------- #
def chat(prompt: str = None, messages: list = None, model: str = None,
         timeout: int = CHAT_TIMEOUT, retry: int = 2, **extra) -> dict:
    """一次对话 → {text, usage, model}。给 prompt 或 messages 二选一。"""
    if not messages:
        if not prompt:
            raise BlacklightError("prompt 和 messages 至少给一个")
        messages = [{"role": "user", "content": prompt}]
    payload = {"model": model or DEFAULT_CHAT_MODEL, "messages": messages, **extra}
    j = _post("/chat/completions", payload, timeout, retry)
    try:
        text = j["choices"][0]["message"]["content"]
    except Exception as e:                              # noqa: BLE001
        raise BlacklightError(f"llm-gw 返回结构异常：{_json.dumps(j, ensure_ascii=False)[:200]}") from e
    return {"text": text, "usage": j.get("usage"), "model": payload["model"]}


def chat_json(prompt: str, model: str = None, timeout: int = CHAT_TIMEOUT, retry: int = 2):
    """要模型吐 JSON 并解析。模型爱在 JSON 外面裹解释文字，这里按最外层 {} / [] 抠出来。"""
    import re
    out = chat(prompt, model=model, timeout=timeout, retry=retry)
    t = out["text"]
    m = re.search(r"(\[.*\]|\{.*\})", t, re.S)
    if not m:
        raise BlacklightError(f"没在返回里找到 JSON：{t[:200]}")
    try:
        return _json.loads(m.group(1))
    except Exception as e:                              # noqa: BLE001
        raise BlacklightError(f"JSON 解析失败：{t[:200]}") from e


# --------------------------------------------------------------------------- #
# 图片
# --------------------------------------------------------------------------- #
def _as_data_url(src) -> str:
    """本地路径 / http(s) URL / 已经是 dataURL → dataURL。"""
    s = str(src)
    if s.startswith("data:"):
        return s
    if s.startswith("http://") or s.startswith("https://"):
        with urllib.request.urlopen(
                urllib.request.Request(s, headers={"User-Agent": "Mozilla/5.0"}), timeout=60) as r:
            raw = r.read()
        mime = "image/png" if s.lower().endswith(".png") else "image/jpeg"
    else:
        if not os.path.exists(s):
            raise BlacklightError(f"图片不存在：{s}")
        raw = open(s, "rb").read()
        mime = "image/png" if s.lower().endswith(".png") else "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


def _save(j: dict, out_dir: str, stem: str) -> dict:
    """把回执里的 b64 落盘 —— **别把 base64 往上层返**（一张图 1~2MB，会撑爆上下文）。"""
    out_dir = out_dir or _paths.exports_dir("llm_images")
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for i, item in enumerate(j.get("data") or []):
        b64 = item.get("b64_json")
        if not b64:
            continue
        p = os.path.join(out_dir, f"{stem}_{i + 1}.png" if i else f"{stem}.png")
        with open(p, "wb") as f:
            f.write(base64.b64decode(b64))
        paths.append(p)
    if not paths:
        raise BlacklightError("网关没回图片（data[].b64_json 为空）")
    return {"paths": paths, "count": len(paths), "usage": j.get("usage"), "cost": j.get("cost")}


def image_generate(prompt: str, out_dir: str = None, stem: str = "gen",
                   model: str = None, timeout: int = IMAGE_TIMEOUT, **extra) -> dict:
    """文生图 → 落盘，返回 {paths, usage, cost}。实测约 24s/张、1024×1024 PNG。"""
    payload = {"model": model or IMAGE_MODEL, "prompt": prompt, **extra}
    return _save(_post("/images/generations", payload, timeout), out_dir, stem)


def image_edit(prompt: str, images, out_dir: str = None, stem: str = "edit",
               model: str = None, timeout: int = IMAGE_TIMEOUT, **extra) -> dict:
    """图生图（拿商品主图生成场景/实拍风格图）→ 落盘。

    `images` 收 本地路径 / http(s) URL / dataURL，单个或列表。
    ⚠️`image` 字段必须是**数组**（传字符串网关报 `unmarshal string into []string`）。"""
    if isinstance(images, (str, bytes)):
        images = [images]
    urls = [_as_data_url(x) for x in (images or [])]
    if not urls:
        raise BlacklightError("图生图至少要给一张输入图")
    payload = {"model": model or IMAGE_MODEL, "prompt": prompt, "image": urls, **extra}
    return _save(_post("/images/edits", payload, timeout), out_dir, stem)
