# 天气数据工作台 / Weather Data Studio

用 Streamlit 构建的 Open-Meteo 数据下载网页，默认中文界面。无需大模型或 Agent。

你可以选择经纬度、天气特征、时间范围和数据源，获取并导出数据。默认使用免费非商业接口，无需 API Key。

## 功能

- 宁夏五市预设、自定义经纬度、多地点编辑、地点 CSV 上传（最多 20 点）。
- 天气预报（1–16 天）、历史天气再分析、历史预报拼接序列。
- 逐小时/每日，温湿度、风、降水、太阳辐射等特征；变量列表随接口变化。
- 北京时间/UTC，自动匹配模型或指定部分模型。
- 页面顶部自选起止日期，默认历史天气；支持最近一年、最近一个月、从最早日期到最新的快捷选择。
- 预计行数、HTTP 请求数与近似调用量；365 天分批、重试、15 分钟缓存。
- 地图位置预览、趋势图、表格、缺失统计。
- CSV、完整 ZIP、独立 Python 脚本；不超过 10 万行时支持 Excel。
- ZIP 含 weather.csv、units.csv、metadata.json、raw_responses.json、fetch_weather.py。
- 可选部署密码与商业 API Key，均从 Streamlit Secrets 读取。

## 文件结构

```text
weather-data-studio/
├── app.py                    # Streamlit 入口
├── weather_core.py           # API、校验、分批和导出逻辑
├── requirements.txt          # 云端自动安装的依赖
├── README.md
├── 部署教程.md                # 从解压到上线的中文步骤
├── locations_example.csv    # 多地点上传示例
├── secrets.example.toml      # 可选设置示例，不含真实密钥
├── .gitignore
├── .streamlit/
│   └── config.toml           # 主题与上传限制
└── tests/                    # 自动检查
```

## 本地运行（可选）

推荐 Python 3.12。在项目文件夹中运行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

Windows 激活命令为 `.venv\Scripts\activate`，创建环境可以使用 `py -3.12 -m venv .venv`。

测试不需要另外安装 pytest：

```bash
python -m unittest discover -s tests -v
```

## GitHub → Streamlit 发布

请看 [部署教程.md](部署教程.md)。最重要的是将本文件夹的**内容**上传到仓库根目录，让 `app.py` 和 `requirements.txt` 直接出现在根目录。

Streamlit Cloud 选择仓库、`main` 分支和 `app.py`，Python 选 3.12。普通非商业用法 Secrets 留空即可。

## 配额与数据含义

截至 2026-10-10，[官方价格页](https://open-meteo.com/en/pricing)列出的免费上限为每分钟 600、每小时 5,000、每天 10,000、每月 300,000 调用量。条款要求低于短周期上限，实际使用应留余量。变量超过 10 个、日期超过 14 天等会增加折算调用量。

本网站每地点单独请求，历史日期每 365 天一批。显示的调用量按每批 `max(1, 变量数/10) × max(1, 天数/14)` 求和估算，仅供计划，不是官方计费记录。免费请求按折算调用量节流到每下载任务约 400 调用量/分钟以下；重试也等待，同一任务的网络耗时会进一步降低速度。重试可能增加用量，缓存可能减少用量。没有真实账户余额显示或持久化全站配额计数；部署在同一服务器的访问者会共同消耗服务端额度，公开多人使用建议加访问密码或另外实现统一限流。

官方未在上述价格页给出统一的每日 MB/GB 下载限额。应用自行限制每任务 100 万行、500 万个气象数值、150 个请求，保护 Streamlit 内存；这不是官方限制。单地点默认 5 个逐小时变量可选择 1940 年至近期的完整区间，但多地点/更多变量可能超限。几十年的任务可能运行数分钟以上，并占用较多内存；请优先按需要选择日期。文件大小在生成后显示，更大任务请按日期或地点拆分。

- 历史天气预留 5 天发布延迟；历史预报预留 2 天。部分模型的覆盖可能更短。
- 预报日期以选择的时区当天为起点。导出的 Python 脚本再次运行时刷新预报日期；历史日期固定。
- 坐标采用 WGS84。地图只是点位预览，没有地图点击选点功能。示例城市中心为近似坐标。
- 返回的是网格模型数据，输出同时保留请求坐标、实际网格坐标、单位、时区和抓取时间。
- 自动匹配不提供每个数值实际使用的模型来源。`model` 记录的是请求设置。
- 抓取时间不是模型发布时间；缓存响应保留原抓取时间。缓存位于服务器内存，重启即失效。
- 气象空值会保留并统计，不自动插值。完整下载失败时不输出部分结果；近期成功批次可复用缓存。
- 历史预报是拼接序列，不还原某次发布的完整预测。日前电价回测应使用 [Single Runs](https://open-meteo.com/en/docs/single-runs-api) 并核对发布与可用时间；本版不含 Single Runs 界面。
- 本版不含 15 分钟、空气质量或电价预测，专注天气下载。

## 商业使用与密钥

免费 API 仅限非商业用途。企业内部业务或交易研究应核对[服务条款](https://open-meteo.com/en/terms)。付费历史天气/历史预报功能目前需 Professional 或以上方案。

在 Streamlit Cloud 的 Secrets 中可填：

```toml
OPEN_METEO_API_KEY = "你的商业密钥"
APP_PASSWORD = "你设置的访问密码"
```

应用会自动切换商业客户域名；密钥不会写入生成代码、ZIP 或查询元数据。导出脚本用环境变量 `OPEN_METEO_API_KEY` 接收商业密钥。商业域名切换逻辑经过模拟测试，真实付费请求尚未验证，因为交付不含商业密钥。

访问密码用于小范围分享，不是完整用户账户或企业认证系统。任何可访问网页的人都可用部署者的 API 配额查询。

数据遵循 CC BY 4.0；页面和导出元数据保留 Open-Meteo 来源及许可链接。下载后再发布数据时也请保留来源说明。

## 官方文档

- [天气预报](https://open-meteo.com/en/docs)
- [历史天气](https://open-meteo.com/en/docs/historical-weather-api)
- [历史预报](https://open-meteo.com/en/docs/historical-forecast-api)
- [价格与配额](https://open-meteo.com/en/pricing)
- [Streamlit 部署](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy)
- [Streamlit Secrets](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management)
