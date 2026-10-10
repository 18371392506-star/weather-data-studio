"""Streamlit entry point: streamlit run app.py"""
import hashlib
import io
import json
import time
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from weather_core import (
    SOURCES, MODEL_OPTIONS, variable_catalog, today_local, validate_job,
    request_plan, fetch_request, combine_results, generated_script, csv_bytes, zip_bundle,
)

st.set_page_config(page_title="天气数据工作台", page_icon="🌤️", layout="wide")
st.markdown("""<style>
html, body, [class*="css"] {font-family: 'Noto Sans SC', sans-serif;}
.block-container {max-width: 1280px; padding-top: 2.5rem;}
.hero {padding: 28px 32px; border: 1px solid #d5e5e5; border-radius: 20px;
background: linear-gradient(120deg, #edf7f5, #f5f8ff); margin-bottom: 24px; color:#193c43;}
.eyebrow {font-size:12px; letter-spacing:3px; color:#16776c; font-weight:700;}
.hero h1 {font-size:34px; margin:12px 0 8px; color:#193c43;}
.hero p {color:#536970; margin:0; line-height:1.8;}
[data-testid="stMetric"] {background:#f0f6f6; border:1px solid #dfebea; border-radius:12px; padding:14px;}
div.stButton > button[kind="primary"] {border-radius:10px;}
</style>""", unsafe_allow_html=True)
st.markdown("""<div class="hero"><div class="eyebrow">WEATHER DATA STUDIO / OPEN-METEO</div>
<h1>把天气，变成可用的数据。</h1><p>选择坐标、时间和天气特征，预览趋势并导出数据。<br>
从宁夏出发，支持全球经纬度查询。</p></div>""", unsafe_allow_html=True)

PRESETS = {
    "银川": (38.47, 106.28), "石嘴山": (38.98, 106.38), "吴忠": (37.99, 106.20),
    "中卫": (37.50, 105.20), "固原": (36.01, 106.24),
}


@st.cache_data(ttl=900, max_entries=32, show_spinner=False)
def cached_request(url, params_json, credential_fingerprint, _api_key):
    # Fingerprint separates paid credentials; the actual secret is excluded from cache hashing.
    return fetch_request(url, json.loads(params_json), _api_key)


def read_settings():
    try:
        return dict(st.secrets)
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        return {}


settings = read_settings()
api_key = str(settings.get("OPEN_METEO_API_KEY", ""))
access_password = str(settings.get("APP_PASSWORD", ""))
if access_password:
    import hmac
    password = st.text_input("访问密码", type="password")
    if not hmac.compare_digest(password.encode("utf-8"), access_password.encode("utf-8")):
        st.info("请输入部署者设置的访问密码。")
        st.stop()

st.subheader("选择数据源与时间区间")
source = st.selectbox("数据源", ["archive", "forecast", "historical_forecast"], format_func=lambda v: SOURCES[v][0])
with st.sidebar:
    st.markdown("### 其他查询设置")
    frequency = st.radio("时间粒度", ["hourly", "daily"], format_func=lambda v: "逐小时" if v == "hourly" else "每日", horizontal=True)
    tz = st.selectbox("时区", ["Asia/Shanghai", "UTC"])
    model = st.selectbox("气象模型", list(MODEL_OPTIONS[source]), format_func=lambda v: MODEL_OPTIONS[source][v], key=f"model_{source}")
    st.divider()
    st.caption("服务模式")
    st.write("商业客户接口" if api_key else "免费 · 非商业接口")
    st.caption("免费额度：600/分钟 · 5,000/小时 · 10,000/天 · 300,000/月。大请求折算多个调用量。")
    st.link_button("查看官方额度与许可", "https://open-meteo.com/en/pricing")

today = today_local(tz)
if source == "forecast":
    days = st.slider("预报天数", 1, 16, 7)
    start, end = today, today + timedelta(days=days - 1)
    st.caption(f"预报范围：{start} 至 {end}。七天是默认值，可选 1–16 天；查询过去请选历史天气。")
else:
    earliest = date(1940, 1, 1) if source == "archive" else date(2022, 1, 1)
    latest = today - timedelta(days=5 if source == "archive" else 2)
    st.info(f"可选日期：{earliest} 至 {latest}（包含起止两天）。你可以输入日期，自由选择任意区间。")
    presets = st.columns(3)
    ranges = [("最近一年", max(earliest, latest - timedelta(days=364))),
              ("从最早日期到最新", earliest), ("最近一个月", max(earliest, latest - timedelta(days=29)))]
    for column, (label, beginning) in zip(presets, ranges):
        if column.button(label, key=f"range_{source}_{label}", width="stretch"):
            st.session_state[f"start_{source}"] = beginning
            st.session_state[f"end_{source}"] = latest
    date_cols = st.columns(2)
    start = date_cols[0].date_input("开始日期", max(earliest, latest - timedelta(days=364)), min_value=earliest, max_value=latest, key=f"start_{source}", format="YYYY-MM-DD")
    end = date_cols[1].date_input("结束日期", latest, min_value=earliest, max_value=latest, key=f"end_{source}", format="YYYY-MM-DD")
    st.caption("最新日期为本网站预留发布延迟后的保守边界，并非实时探测的最后一条数据。部分模型/变量历史更短或可能存在空值。跨几十年建议选 ERA5；任务过大时请减少地点/特征，或分区间下载。")

if source == "archive":
    st.info("历史天气为再分析/模型数据，不是气象站逐条实测。ERA5 适合长期一致性分析。")
elif source == "historical_forecast":
    st.warning("历史预报是多次模型运行最初时段的拼接序列，不能还原某一时刻已知的完整日前预报。严格电价回测需另用 Single Runs，并核对发布时间。")
else:
    st.caption("预报会持续更新。下载包保留本次原始响应和 UTC 抓取时间；它不等同于模型发布时间。")

left, right = st.columns([1.1, 1], gap="large")
with left:
    st.subheader("01 / 选择位置")
    mode = st.radio("位置方式", ["宁夏城市", "输入经纬度", "多个地点"], horizontal=True)
    locations = []
    location_error = None
    if mode == "宁夏城市":
        selected = st.multiselect("选择城市", list(PRESETS), default=["银川"])
        locations = [{"name": name, "latitude": PRESETS[name][0], "longitude": PRESETS[name][1]} for name in selected]
        st.caption("城市中心为近似坐标；场站分析请使用场站实际坐标。")
    elif mode == "输入经纬度":
        name = st.text_input("地点名称", "自定义地点")
        a, b = st.columns(2)
        lat = a.number_input("纬度 Latitude", -90.0, 90.0, 38.47, step=0.01, format="%.5f")
        lon = b.number_input("经度 Longitude", -180.0, 180.0, 106.28, step=0.01, format="%.5f")
        locations = [{"name": name.strip(), "latitude": lat, "longitude": lon}]
    else:
        st.caption("每行一个地点，最多 20 个。支持 CSV 上传，列名为 name,latitude,longitude。")
        uploaded = st.file_uploader("上传地点 CSV（UTF-8）", type=["csv"])
        initial = pd.DataFrame([{"name": n, "latitude": xy[0], "longitude": xy[1]} for n, xy in PRESETS.items()])
        if uploaded:
            try:
                if uploaded.size > 100_000:
                    raise ValueError("地点文件过大，请上传小于 100 KB 的 CSV。")
                incoming = pd.read_csv(uploaded, encoding="utf-8-sig", nrows=21)
                if not {"name", "latitude", "longitude"}.issubset(incoming.columns):
                    raise ValueError("CSV 缺少 name、latitude 或 longitude 列。")
                initial = incoming[["name", "latitude", "longitude"]]
            except (ValueError, UnicodeError, pd.errors.ParserError) as exc:
                location_error = str(exc)
        edited = st.data_editor(initial, num_rows="dynamic", hide_index=True, key="locations_editor", width="stretch")
        try:
            for row in edited.to_dict("records"):
                if pd.isna(row["name"]) or pd.isna(row["latitude"]) or pd.isna(row["longitude"]):
                    raise ValueError("地点表格中存在空白字段，请填写或删除该行。")
                locations.append({"name": str(row["name"]).strip(), "latitude": float(row["latitude"]), "longitude": float(row["longitude"])})
        except (ValueError, TypeError) as exc:
            location_error = str(exc)
    if locations and not location_error:
        points = pd.DataFrame(locations)
        valid = points[points.latitude.between(-90, 90) & points.longitude.between(-180, 180)]
        if not valid.empty:
            st.map(valid.rename(columns={"latitude": "lat", "longitude": "lon"}), size=120, zoom=5, height=230)
        st.caption("坐标使用 WGS84；高德/百度地图坐标需先转换。地图用于预览位置。")

with right:
    st.subheader("02 / 选择天气特征")
    catalog = variable_catalog(source, frequency)
    default = (["temperature_2m", "relative_humidity_2m", "wind_speed_10m", "shortwave_radiation", "cloud_cover"]
               if frequency == "hourly" else ["temperature_2m_max", "temperature_2m_min", "precipitation_sum", "shortwave_radiation_sum"])
    variables = st.multiselect("天气特征", list(catalog), default=default, format_func=lambda v: f"{catalog[v]}  ·  {v}", key=f"vars_{source}_{frequency}")
    st.caption("风速统一为 m/s；温度 °C；降水 mm。其他单位以返回值和下载包的 units.csv 为准。")
    if frequency == "hourly":
        st.caption("短波/直接/散射辐射通常为前一小时平均值。历史再分析采用 100 m 风，预报可选 80/120 m 风。")
    with st.expander("变量与数据说明"):
        st.dataframe(pd.DataFrame([{"API 字段": k, "说明": v} for k, v in catalog.items()]), hide_index=True, width="stretch")
        st.markdown("[天气预报文档](https://open-meteo.com/en/docs) · [历史天气文档](https://open-meteo.com/en/docs/historical-weather-api)")
        st.write("个别模型可能返回空值或不支持某些变量；下载后会显示缺失统计。自动匹配不是宁夏精度保证。")

job = {"source": source, "frequency": frequency, "timezone": tz, "model": model,
       "start_date": start.isoformat(), "end_date": end.isoformat(), "variables": variables, "locations": locations}
error = location_error
estimate = None
try:
    if error:
        raise ValueError(error)
    estimate = validate_job(job)
except (ValueError, TypeError, KeyError) as exc:
    error = str(exc)

st.subheader("03 / 检查并获取")
if error:
    st.error(error)
else:
    cols = st.columns(4)
    cols[0].metric("预计数据行", f"{estimate['rows']:,}")
    cols[1].metric("天气特征", str(len(variables)))
    cols[2].metric("HTTP 请求", str(estimate["requests"]))
    cols[3].metric("估算调用量", f"{estimate['estimated_calls']:,.1f}")
    st.caption("调用量为按地点、变量数和分批长度计算的近似值，不是账户余额；重试也可能消耗额度。历史数据每 365 天一批，免费请求按折算调用量放慢速度；几十年下载可能需要数分钟以上。单次最多 100 万行、500 万个气象数值、150 个请求。")
    with st.expander("查看请求与生成的 Python 代码"):
        st.json({"configuration": job, "first_request": request_plan(job)[0]})
        script = generated_script(job)
        st.download_button("下载独立 Python 脚本", script, "fetch_weather.py", "text/x-python", key="script_before")
        st.caption("脚本可独立执行：pip install requests pandas。预报日期会在执行时更新为当天；历史日期保持固定。")

if st.button("获取天气数据", type="primary", disabled=bool(error), width="stretch"):
    # Clear stale results before attempting a new task. Never present partial downloads as complete.
    st.session_state.pop("result", None)
    plan = request_plan(job)
    responses = []
    progress = st.progress(0, text="准备获取数据…")
    try:
        fingerprint = hashlib.sha256(api_key.encode()).hexdigest() if api_key else "free"
        for index, entry in enumerate(plan):
            progress.progress(index / len(plan), text=f"{entry['location']['name']} · 请求 {index + 1}/{len(plan)}")
            responses.append(cached_request(entry["url"], json.dumps(entry["params"], sort_keys=True), fingerprint, api_key))
            if index < len(plan) - 1:
                time.sleep(0.25)
        frame, metadata = combine_results(job, plan, responses)
        script = generated_script(job)
        st.session_state.result = {"frame": frame, "metadata": metadata,
                                   "csv": csv_bytes(frame), "zip": zip_bundle(frame, metadata, responses, script), "script": script}
        progress.progress(1.0, text="获取完成")
    except Exception as exc:
        # Unexpected exceptions are described without echoing response URLs or credentials.
        if isinstance(exc, (ValueError, RuntimeError)):
            st.error(str(exc))
        else:
            st.error("数据处理失败，请缩小查询范围后重试。部署者可运行项目测试排查。")
        st.info("此次任务未导出部分结果。已经成功的分批请求可在 15 分钟缓存有效期内复用。")

result = st.session_state.get("result")
if result:
    frame, metadata = result["frame"], result["metadata"]
    completed = metadata["job"]
    st.divider()
    st.subheader("下载结果")
    st.caption(f"下方为最近一次成功任务：{SOURCES[completed['source']][0]} · {completed['start_date']} 至 {completed['end_date']} · {len(completed['locations'])} 个地点。修改上方参数后，需要重新点击获取。")
    st.success(f"已获取 {len(frame):,} 行 · CSV {len(result['csv']) / 1024 / 1024:.2f} MB · 时区 {completed['timezone']}")
    missing = {k: v for k, v in metadata["missing_values"].items() if v}
    if missing:
        st.warning("部分气象数值为空，请在建模前检查。空值不会自动填补。")
        st.json(missing)
    a, b, c = st.columns(3)
    a.download_button("↓ 完整数据包 ZIP", result["zip"], "weather_data.zip", "application/zip", width="stretch")
    b.download_button("↓ 数据 CSV", result["csv"], "weather.csv", "text/csv", width="stretch")
    c.download_button("↓ Python 代码", result["script"], "fetch_weather.py", "text/x-python", width="stretch", key="script_after")
    st.caption("ZIP 包含数据、单位、原始响应、查询参数和抓取时间。建议保留 ZIP 用于复现分析。")
    tab_chart, tab_data, tab_meta = st.tabs(["趋势预览", "数据表格", "单位与元数据"])
    with tab_chart:
        numeric = [v for v in completed["variables"] if pd.api.types.is_numeric_dtype(frame[v])]
        if numeric:
            feature = st.selectbox("预览特征", numeric, format_func=lambda v: f"{v} ({metadata['units'].get(v, '')})")
            chart = frame.pivot(index="time", columns="location", values=feature)
            chart.index = pd.to_datetime(chart.index)
            if len(chart) > 5000:
                st.caption("预览取等间隔样本，完整数据请下载。")
                chart = chart.iloc[::max(1, len(chart) // 5000)]
            st.line_chart(chart, height=320)
        else:
            st.info("所选字段不含数值型特征，请查看表格。")
    with tab_data:
        st.dataframe(frame.head(1000), hide_index=True, width="stretch")
        st.caption("最多预览前 1,000 行；下载文件包含完整结果。")
        if len(frame) <= 100_000:
            if st.button("生成 Excel 文件"):
                buffer = io.BytesIO()
                with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                    frame.to_excel(writer, sheet_name="weather", index=False)
                    pd.DataFrame(list(metadata["units"].items()), columns=["variable", "unit"]).to_excel(writer, sheet_name="units", index=False)
                    # Explicit string cells stop user-supplied labels becoming Excel formulas.
                    for sheet in writer.book.worksheets:
                        for row in sheet:
                            for cell in row:
                                if cell.data_type == "f":
                                    cell.data_type = "s"
                st.download_button("↓ 下载 Excel", buffer.getvalue(), "weather.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        else:
            st.caption("超过 10 万行时使用 CSV/ZIP，避免 Excel 转换占用过多内存。")
    with tab_meta:
        st.json(metadata)

st.divider()
st.caption("天气数据由 Open-Meteo 及其上游数据提供方提供，遵循 CC BY 4.0。免费 API 限非商业用途；企业内部业务用途应核对商业订阅。数据准确性应按地点、变量和预测提前量验证。")
st.markdown("[Open-Meteo](https://open-meteo.com/) · [数据许可](https://open-meteo.com/en/licence) · [服务条款](https://open-meteo.com/en/terms)")
