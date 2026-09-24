---
name: jd-pictured-review-automation
description: 在未安装其他 Skill 或 Blacklight MCP 的 Windows 电脑上，稳定执行京东京喜带图评价查询、生图上传、文案质检、受控提交、失败续跑与 330 生效回读。适用于每日处理或指定 SKU/SPU 补跑。
---

# 京喜带图评价便携自动化

本 Skill 自带便携 Python、离线依赖和所需 Blacklight 核心源码。优先使用随包注册的 `blacklight-osw` MCP；MCP 不可用时，使用 `scripts/run.ps1` 调用同一套本地核心。不要依赖其他 Skill、系统 Python、EasyBI 页面 DOM、WebCLI 扩展、DesignSpark 页面、Excel 队列或旧 `.pyz`。

## 首次使用

1. 阅读 `references/installation.md` 和 `references/stability-playbook.md`。
2. 执行 `& "<本 Skill 目录>\scripts\run.ps1" doctor`。
3. 若未登录，执行 `& "<本 Skill 目录>\scripts\login.ps1"`，在 Chrome 中完成 ERP 登录。
4. 再执行 `run.ps1 doctor`，确认运行时、核心模块和登录态可用。

## 工具选择

- `blacklight-osw` 可用时，使用对应的 `osw_pic_*` 和 `osw_llm_*` 工具。
- MCP 不可用时，使用 `scripts/run.ps1`。参数复杂时写入 UTF-8 JSON，再以 `--input <文件>` 传入。
- 用户提供 SPU 表时，使用 `scripts\run_spu_table.ps1 -Excel <SPU.xlsx路径> -DisplayLimit 5 -SpuLimit 100`；纯文案必须走这个脚本内置提交路径，不走原版 `screen/import-dryrun/import`。
- 本地命令包括：`doctor`、`userinfo`、`easybi-pending`、`targets`、`llm-status`、`llm-image`、`imgzone-upload`、`imgzone-find`、`ai-text`、`real-review`、`screen`、`import-dryrun`、`import`、`tasks`、`task-stats`。

## 不变量

- 只处理 `targets` 当前仍返回、且有可用主图、未达每 SKU 上限的 SKU；已上传过但未满 10 条的 SKU 仍按剩余槽位继续补。
- 用户提供 SPU 表时：每个 SKU 最多补到平台上限 10 条；单个 SPU 默认最多 100 条，其中最多 5 条带图评价、最多 95 条纯文案。
- 带图评价写入前必须先执行 `import-dryrun`。SPU 表批处理里的纯文案是例外：必须使用 `run_spu_table.py` 内置原始接口提交 `images: ""`，不能用原版 `import-dryrun` 校验纯文案。
- 提交回执只代表受理；最终成功只认 `tasks` / `task-stats` 中状态 `330`。
- 失败或状态不确定时先回读，禁止盲目重试和重复创建。
- 不删除图片空间中的图片，不输出 Cookie、密钥或凭证文件内容。

## 标准流程

### 1. 权限与范围

1. 执行 `userinfo`，要求 `can_import == true`，记录 ERP。
2. 用户提供 SPU 表时，以表内 SPU 作为硬范围，跳过 EasyBI；用户没有指定 SKU/SPU 且未提供 SPU 表时，先调用 `easybi_pic_pending`（CLI：`easybi-pending`）查询该 ERP 的未完成带图评价明细；默认查 T-1，无数据时最多回退 T-2。
3. 把 EasyBI 返回的 `spu_ids` 作为硬范围传给 `targets`，由 OSW 展开为可处理 SKU；EasyBI 返回空清单时报告无待办并结束。
4. EasyBI 权限、接口或字段校验失败时立即停止并报告，禁止静默退化为无范围 `targets`，避免扫描或处理全量待办。
5. 用户明确给了 SKU/SPU 或 SPU 表时，以用户清单作为硬范围，不再用 EasyBI 扩大范围。
6. 先回显 EasyBI 日期、是否使用回退日期、SPU 数量，以及 OSW 展开后的候选 SKU；范围与用户预期不一致时只报告，不提交。
7. SPU 表批处理时，展开该 SPU 下可处理 SKU 的剩余评价槽位；已上传 1-9 条的 SKU 继续补到平台上限 10 条。
8. SPU 表批处理默认每 SPU 最多 100 条：最多 5 条带图评价 + 95 条纯文案。

### 2. 图片

逐个 SKU 执行。SPU 表批处理时，仅对主推槽位生成图片，默认每 SPU 最多 5 条：

1. 用 `llm-status` 检查模型网关。
2. 模型网关可用时，用 `llm-image` 将商品 `sku_image` 作为源图，要求外观一致、自然使用场景、无文字水印、无品牌篡改、无夸大效果。
3. 文件名使用 `<sku_id>_piceval_<YYYYMMDD>_<序号>.png`。SPU 表批处理时，同一 SPU 的图片拍摄方式、背景、构图尽量不相似。
4. 用 `imgzone-upload` 上传，再用 `imgzone-find` 按文件名回读并验活。
5. 上传超时后先查同名文件；已存在则复用，不重复上传。

若 `llm-status` 明确返回无密钥、无权限或模型不可用，不得停在“请申请权限”：

1. 对已锁定的 `targets.items` 调用 `osw_pic_real_review`；CLI 兜底为 `real-review`。
2. 该工具使用包内同品真实晒单采集器，返回已执行严格质检的 `ok/review/dropped` 四桶。
3. 仅 `ok` 进入 `import-dryrun`；`review` 不自动提交，采集失败的 SKU 留待下次续跑。
4. 真实评价兜底不要求 `JD_LLM_GW_KEY`，也不要求目标电脑另装 Skill、Blacklight 或系统 Python。

### 3. 文案与质检

1. 用 `ai-text` 根据已上传图片生成文案；首次先验证单条返回。SPU 表批处理时，全部计划槽位都生成或保留候选文案，非主推槽位走纯文案提交。
2. 不凭空补写功能、参数、疗效、物流或售后体验。
3. 用 `screen` 复核 `{sku_id, eval_content, images}`。
4. 只有 `ok` 桶可提交；`review` 交人工，`needs_text` 留待补充，`dropped` 永不提交。

使用同品真实评价兜底时，额外执行以下硬闸门：

- 行数据标记 `source_type: real_review`，图片必须来自京东晒单路径 `/shaidan/`，禁止商品主图、外站图和同批重复图。
- 原文必须逐字保留，不拼接不同评价，不补写未出现的体验；“不像正品”“不是宣传品牌”等真伪、品牌或宣传质疑直接剔除。
- 文案出现明确品牌背书但目标商品标题不含该品牌时进入 `review`，不得自动提交。
- `import-dryrun` 的历史图片索引至少扫描平台可访问的最近 20000 条；覆盖不足必须在结果中明确提示，退回图不得原样重提。

### 4. 提交与验收

1. 提交前再次 `targets` 复核目标仍待补。
2. 用完全相同的 rows 执行 `import-dryrun`，检查配额、图片判重和拒绝原因。
3. 用户已明确授权提交时，才把 `confirm_token` 连同原 rows 传给 `import`。
4. 提交超时后先执行 `tasks`；查到任务即视为已受理，不重提。
5. 用 `tasks` 与 `task-stats` 回读。`330` 为生效；其他状态进入待回读或失败清单。

## 重试与输出

- 仅对超时、连接中断、HTTP 429 和 5xx 重试，最多 3 次，等待 5、15、45 秒。
- 权限、参数、业务校验、文本或图片闸门失败不自动重试。
- 单个 SKU 失败不阻塞其他 SKU；任何部分成功都按逐条结果处理。
- 汇报 ERP、范围、候选、质检通过、已受理、`330` 生效、处理中、失败、人工复核数量，以及续跑起点。
