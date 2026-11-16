# 产品详情生成脚本

## 窗口版副本

本目录是原脚本的独立桌面副本，原项目未改动。产品采集、提示词和 AI 生成逻辑沿用现有版本，新增窗口、后台进程、进度与停止保存。

开发运行：`venv\Scripts\python.exe desktop_app.py`。
构建：安装 `requirements-build.txt` 后运行 `build_desktop.ps1`。
发布程序位于 `dist/ProductDetailsDesktop/ProductDetailsDesktop.exe`，分发时发送整个文件夹或 `dist/ProductDetailsDesktop-win-x64.zip`。
用户说明见 `DESKTOP_USAGE.txt`；私有设置、账号登录态和日志保存在 `%LOCALAPPDATA%/ProductDetailsDesktop`。


从 Alibaba、淘宝和天猫商品页采集数据，使用 DeepSeek 或 Gemini 生成中英文标题与详情，并写入 Excel。
采集层使用 Scrapling 0.5.1 的异步浏览器会话；按站点登录态复用会话与标签页。
文案模板、AI 调用和 `run.py` 的异步流水线保持现有行为。

## 安装

需要 Python 3.10+。在本项目目录运行：

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
$env:PLAYWRIGHT_SKIP_BROWSER_GC = "1"
.\venv\Scripts\python.exe -m patchright install chromium
```

将 `.env.example` 复制为 `.env`，填写需要使用的 API Key。
单条采集验证不需要 API Key。

## 单条商品验证

```powershell
.\venv\Scripts\python.exe check_product.py "https://www.alibaba.com/product-detail/Baby-Product-OEM-ODM-Custom-Newborn_1601683518137.html" --headless
```

结果保存到 `debug/product_check.json`，其中 `variant_dimensions` 保存原始维度及全部选项，
`variants` 保存用于现有 Excel 的“颜色、尺寸、其他规格”字段。
此命令不调用 AI，也不修改 `products_output.xlsx`。
需要手动登录或验证时，去掉 `--headless`。

变体优先读取当前商品内嵌的 `detailData.globalData.product.sku`，DOM 支持当前购买面板及旧版 `sku-list`。
不会把推荐商品、定制服务或普通属性文本当作可选变体。
Alibaba 的折叠选项会尝试展开；缺失变体时保留本地页面证据用于排查。
淘宝/天猫仍保留原有价格、属性和 iframe 详情采集，尚未实现其变体解析。

## 登录与批量处理

现有 Playwright 登录态文件可继续加载；过期时重新登录：

```powershell
.\venv\Scripts\python.exe login.py alibaba
.\venv\Scripts\python.exe login.py taobao
.\venv\Scripts\python.exe login.py tmall
.\venv\Scripts\python.exe run.py
```

对应登录态为 `alibaba_state.json`、`taobao_state.json`、`tmall_auth.json`。
在 `run.py` 顶部配置平台并发、AI 并发、文案模板和输入输出文件。
`RESUME=True` 会继续复用已有中文标题的行；要重新采集旧结果中缺失的变体，请先备份输出表，再设 `RESUME=False` 重跑。

## 离线回归测试

```powershell
.\venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

测试使用从商品页面精简出的 SKU 样本，不调用真实站点或 AI。
