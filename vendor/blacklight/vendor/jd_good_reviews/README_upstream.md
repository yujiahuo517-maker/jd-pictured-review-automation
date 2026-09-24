# 京东同品带图好评采集工具

按输入 SKU 找京东同品/相似商品，再从评论接口导出带图好评到 Excel。输出严格使用模板列：

`SKUID, 商品名称, 评价文本, 实拍图1, 实拍图2, 实拍图3, 实拍图4, 实拍图5, 实拍图6, 实拍图7, 实拍图8, 实拍图9`

## 安装

```bash
cd /Users/liuyan482/Documents/cli/jd_good_reviews_tool
python3 -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn
python3 -m playwright install chromium
```

双击 `.command` / `.bat` 时，如果检测到依赖缺失，也会自动使用清华 PyPI 镜像安装。

## 运行

默认使用无登录极速接口模式：先读取目标 SKU 主图，再走京东图片识别同品接口和评论接口。通常直接双击：

```text
运行京东带图好评采集.command
```

选择输入文件即可，不需要先登录。

Windows 用户使用：

```text
运行京东带图好评采集_Windows.bat
```

bat 已保存为 UTF-8 BOM + CRLF，并在启动时执行 `chcp 65001`，尽量避免中文系统乱码。

命令行运行方式：

```bash
python3 main.py \
  --input input.xlsx \
  --output output.xlsx \
  --headless
```

也可以输入 CSV，字段支持 `SKUID`、`商品名称`。如果只有一列，会当作 `SKUID`。

## 断点续跑

脚本会创建：

- `logs/run.log`
- `data/progress.json`
- `data/used_images.json`

如果输出文件已存在，再次运行会跳过 `progress.json` 里已经完成的 SKU。找不到合适评价的 SKU 也会输出空行，并记录原因。

## 采集规则

- 同品识别复用 `jd-image-product-search` skill 的图片识别流程，默认走插件图片识别页触发 `pc_search_image_search` 网络接口。
- 评论优先走 `https://club.jd.com/comment/skuProductPageComments.action`。
- `score=4` 用于获取晒图/有图评价。
- 跳过京喜商品/店铺。
- 跳过差评、中评、负面词、默认好评、无图评价。
- 图片链接写普通文本，不嵌入 Excel。
- 图片全局去重：先按归一化 JD 图片 path 去重，可选再下载图片做 SHA256 内容去重。

## 插件与登录

工具已自带京东购物助手插件副本：

```text
vendor/jd-extension/1.0.9_1
```

当前默认不加载本地 Chrome 扩展，而是直接打开插件的图片识别页面触发同品接口，速度更快，也避免要求每台电脑都先登录京东。

如果京东后续改接口，需要手动演示插件 UI，可把 `config.yaml` 改成：

```yaml
load_extension: true
headless: false
```

登录脚本仍保留为备用：

```text
登录京东账号.command
登录京东账号_Windows.bat
```

## 常见问题

日志出现：

```text
Timeout ... while waiting for event "response"
```

表示同品识别接口 `pc_search_image_search` 没有在超时时间内返回，不代表商品没有评价。常见原因是京东登录态失效、风控、网络慢或接口偶发卡住。

处理方式：

1. 默认先保持无登录模式重试一次，或把 `same_product_response_timeout` 调大。
2. 如果仍失败，再运行 `演示无登录图片识别拦截.command` 看是否是京东页面弹窗/拦截。
3. 必要时才运行 `登录京东账号.command` 或 `登录京东账号_Windows.bat`，切换到登录态备用。

建议保持 `config.yaml` 中：

```yaml
no_login_mode: true
plugin_image_search_mode: true
keyword_search_fallback: false
```
