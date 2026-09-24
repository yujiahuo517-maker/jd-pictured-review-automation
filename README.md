# 安装说明

1. 完整解压 ZIP，不要在压缩包内直接运行。
2. 双击 `安装.cmd`。它会安装 Skill，并自动注册最小的 `blacklight-osw` MCP。
3. 重启 Codex。
4. 双击 `首次登录.cmd`，在 Chrome 中完成 ERP 登录。
5. 在 Codex 中说：`使用 $jd-pictured-review-automation 先试跑今天 10 条，不提交。`

本包不要求目标电脑预装其他 Skill、Blacklight、MCP 或系统 Python。它自带 Windows x64 便携 Python、离线依赖、Blacklight 核心代码和本地 CLI 兜底。

当前版本已加强真实评价图文校验：拦截真伪/品牌质疑、跨品牌评价、非京东晒单图、同条及同批重复图片，并将历史图片判重扫描扩大到平台可访问的最近 20000 条。

模型权限不是必需项：有 `llm-gw` 权限时优先 AI 生图；没有权限时自动使用包内真实评价图文采集器继续处理。

ZIP 内不含账号、Cookie、密钥或历史业务数据。登录凭证只会在安装后的 `runtime\state` 中生成。

系统要求：Windows 10/11 x64、Chrome、京东办公网络或 VPN，以及当前账号的 OSW 带图评价权限。

手工自检：

```powershell
.\scripts\run.ps1 doctor
.\scripts\run.ps1 userinfo
.\scripts\run.ps1 targets --json '{"limit":1}'
& .\runtime\python\python.exe .\scripts\mcp_server.py --self-test
```

## SPU 表批量处理

本版在原版基础上只增加一个 SPU 表入口。把 SPU 表放到桌面并命名为 `SPU.xlsx`，安装并登录后双击 `跑SPU表.cmd`。

规则：每个 SKU 按当前已上传数量计算剩余槽位，已传过 1-9 条的 SKU 也继续补，最多补到 10 条；单个 SPU 默认最多 100 条，其中最多 5 条带图评价、最多 95 条纯文案。

注意：纯文案只支持 `跑SPU表.cmd` / `scripts\run_spu_table.ps1` 这条入口；不要让 Codex 走原版 `screen`、`import-dryrun` 或 `import` 去校验纯文案行，否则会提示无图/不支持纯文案。

```powershell
.\scripts\run_spu_table.ps1 -Excel "D:\yourpath\SPU.xlsx" -DisplayLimit 5 -SpuLimit 100
```
