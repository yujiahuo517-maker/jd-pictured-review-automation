# 安装与恢复

## 推荐安装

完整解压 ZIP 后双击 `安装.cmd`。安装器会：

1. 把整个目录复制到 `%USERPROFILE%\.codex\skills\jd-pictured-review-automation`。
2. 用随包便携 Python 注册 `blacklight-osw` MCP。
3. 保留 `scripts\run.ps1` 作为 MCP 不可用时的同源兜底入口。

安装后重启 Codex，使新 Skill 和 MCP 生效。首次业务调用前双击 `首次登录.cmd`。

## 无法注册 MCP

若目标环境没有 `codex` 命令，安装器会跳过 MCP 注册。Skill 仍可通过：

```powershell
& "<Skill目录>\scripts\run.ps1" doctor
```

调用全部核心能力。不要因此退回旧 EasyBI DOM 或 WebCLI 自动化。

日常未指定 SKU/SPU 时，先用 `easybi_pic_pending`（CLI：`easybi-pending`）通过 EasyBI 接口读取销售员 ERP 名下未完成 SPU，再把返回的 `spu_ids` 传给 `osw_pic_targets`。EasyBI 查询失败时必须停止，不得改为无范围扫描。

## 升级

已有同名 Skill 时，默认拒绝覆盖。明确要升级时执行：

```powershell
.\scripts\install.ps1 -Force
```

安装器会先把旧目录改名为带时间戳的备份，再安装新版本。升级前自行备份旧目录下的 `runtime\state`，其中可能包含本机登录态。

## 数据边界

- ZIP 内不包含 `runtime\state`、Cookie、账号、密钥、日志或业务结果。
- 登录后凭证仅保存在安装目录的 `runtime\state`。
- 不要把登录后的 Skill 目录重新压缩或分享。
