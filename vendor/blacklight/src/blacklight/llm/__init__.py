"""blacklight.llm —— 京东内网大模型网关（`llm-gw.jd.local`）。

平台资源，**每人每天 300w token 额度**。OpenAI 兼容形状，但有三处不一样，踩过：
  - **只收 `application/json`**，multipart 直接 400（OpenAI 那套 `/images/edits` 上传法在这不能用）
  - `/v1/images/edits` 的 `image` 字段是**数组** `[dataURL]`，传字符串报
    `cannot unmarshal string into []string`
  - 图片接口**只回 base64**（`b64_json`），没有 URL —— 要 URL 得自己传图床（见 `blacklight.pic.imgzone`）

限流：实测快速连发第 3 次即 429 `请求次数已超过API Key限流阈值`，故本模块**串行 + 退避**，不并发。
密钥：环境变量 `JD_LLM_GW_KEY`，或 `runtime/credentials.json` 的 `llm_gw_key`（runtime 已 gitignore）。
**代码里不写死密钥。**
"""
from blacklight.llm.gateway import (  # noqa: F401
    BASE_URL, MODELS, DEFAULT_CHAT_MODEL, IMAGE_MODEL,
    chat, chat_json, image_generate, image_edit, list_models, status,
)
