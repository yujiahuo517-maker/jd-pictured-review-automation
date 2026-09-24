"""blacklight.pic —— 京喜**带图评价**（osw.jd.com/picstart，内部代号 aievalandsales）。

两层：
  - `client`：平台接口面（待补清单 / 任务回读 / AI 生成文案 / 批量导入提交）
  - `collect`：同品带图好评采集器编排（输入表 → 跑采集 → 读输出表）

全链路：`targets()` 待补清单 → `plan_input()` 生成采集输入表 → `run_collector()` 采图采文案
→ `import_rows_dryrun()/import_rows()` 提交 → `task_list()` 回读到 taskStatus=330 生效。
"""
from blacklight.pic import client, collect, imgzone, screen  # noqa: F401
