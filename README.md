# 带图评价维护 Skill（jd-pictured-review-automation）

一句话：给一批 SPU，自动找**京东同款真实晒单**素材；没有可用同款就用 **DesignSpark** 生成商品展示图；提交带图评价后回读平台状态。

平台只认状态 `330` 为生效，其余（`310` 处理中、`4` 机审拒绝等）都不算完成。

- 当前版本：`2026.11.26`
- 适用环境：Windows 10/11 x64 + Chrome + 京东办公网络/VPN + 本人账号的 OSW 带图评价权限

## 处理流程

1. **拉取待维护范围**：EasyBI 未完成带图评价 SPU 清单（或直接给 SPU 列表）。
2. **找京东同款素材**：以目标商品主图做同款/相似商品检索，逐项核对品牌、外形、颜色、尺寸、套装数量；素材对得上才用它的真实晒单图片和原文提交，对不上自动跳过换下一个。
3. **历史图判重**：扫描平台可访问的最近 20000 条历史图片，避免“图片已经被使用过”被机审拒绝。
4. **没有可用同款走 DesignSpark**：用目标商品自有主图生成商品展示图，按 OSW 带图评价流程提交，并在汇总里标注来源为 DesignSpark 生成。
5. **提交**：`screen` → `import-dryrun` → `confirm` → `import`，每步留回执。
6. **回读汇总**：按 SPU 输出“生效330 / 处理中 / 机审拒绝 / 无素材”四类，生成 xlsx + md。

## 目录结构

| 路径 | 说明 |
| --- | --- |
| `SKILL.md` | Skill 主说明与执行约束 |
| `agents/openai.yaml` | Skill 元信息 |
| `assets/` | DesignSpark 默认提示词与页面选择器配置 |
| `references/` | 安装说明、稳定性手册、DesignSpark 素材说明 |
| `scripts/` | Skill 执行入口（含 DesignSpark 客户端、素材维护、SPU 表批量） |
| `vendor/blacklight/` | 内嵌 blacklight CLI 与京东晒单采集器 |
| `runtime/` | 便携运行环境与离线依赖（`runtime/state/` 不入库） |
| `安装.cmd` / `首次登录.cmd` / `跑SPU表.cmd` | 双击入口 |

## 安装

1. 完整解压，不要在压缩包内直接运行。
2. 双击 `安装.cmd`：安装 Skill 并注册最小的 `blacklight-osw` MCP。
3. 重启 Codex。
4. 双击 `首次登录.cmd`，在 Chrome 中完成 ERP 登录。
5. 在 Codex 中说：`使用 $jd-pictured-review-automation 先试跑今天 10 条，不提交。`

自检：

```powershell
.\scripts\run.ps1 doctor
.\scripts\run.ps1 userinfo
& .\runtime\python\python.exe .\scripts\mcp_server.py --self-test
```

## SPU 表批量

```powershell
.\scripts\run_spu_table.ps1 -Excel "D:\yourpath\SPU.xlsx" -DisplayLimit 5 -SpuLimit 100
```

规则：每个 SKU 按已上传数量计算剩余槽位，已传 1-9 条的继续补，最多补到 10 条；单个 SPU 默认最多 100 条，其中最多 5 条带图评价、最多 95 条纯文案。

## 更新记录

### 2026.11.26

- 新增 `assets/`（DesignSpark 默认提示词、页面选择器）与 `references/designspark-source.md`。
- 新增 `scripts/designspark_client.py`、`maintain_assets.py`、`run-assets.ps1`、`test_assets.py`。
- 新增 `使用教程-新人培训版.md`。
- 真实评价图文校验加强：拦截真伪/品牌质疑、跨品牌评价、非京东晒单图、同款及同批重复图；历史图判重扫描扩到平台可访问的最近 20000 条。
- 采集器与 `pic/client.py`、`pic/screen.py` 更新，支持 DesignSpark 缺省提示词与多 lane 并行采集。
- 新增 SPU 表批量入口 `跑SPU表.cmd` / `scripts/run_spu_table.ps1`。

### 2026.09.24

- 首个版本：EasyBI 待维护 SPU 筛选 + 京东同款素材检索 + 提交 + 回读汇总。

## 说明

- 仓库不含账号、Cookie、密钥与历史业务数据；登录态只在安装后的 `runtime\state` 生成。
- 同款评价按原文使用，不改写成目标商品买家的使用体验；DesignSpark 生成图按来源标注。
