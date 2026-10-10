"""Open-Meteo download engine; independent of Streamlit, also used by exported scripts."""
import copy
import json
import math
import os
import time
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED

import pandas as pd
import requests

SOURCES = {
    "forecast": ("天气预报", "https://api.open-meteo.com/v1/forecast"),
    "archive": ("历史天气 · 再分析", "https://archive-api.open-meteo.com/v1/archive"),
    "historical_forecast": ("历史预报 · 拼接序列", "https://historical-forecast-api.open-meteo.com/v1/forecast"),
}

# Endpoint-specific variables: historical reanalysis has 100 m wind, not 120 m.
HOURLY = {
    "temperature_2m": "气温 · 2 m", "relative_humidity_2m": "相对湿度 · 2 m",
    "apparent_temperature": "体感温度", "dew_point_2m": "露点温度",
    "precipitation": "降水量", "rain": "降雨量", "snowfall": "降雪量",
    "cloud_cover": "总云量", "pressure_msl": "海平面气压",
    "surface_pressure": "地面气压", "wind_speed_10m": "风速 · 10 m",
    "wind_direction_10m": "风向 · 10 m", "wind_gusts_10m": "阵风 · 10 m",
    "shortwave_radiation": "总短波辐射 GHI", "direct_radiation": "直接水平辐射",
    "diffuse_radiation": "散射辐射 DHI", "direct_normal_irradiance": "直接法向辐射 DNI",
    "et0_fao_evapotranspiration": "参考蒸散量 ET₀", "sunshine_duration": "日照时长",
}
DAILY = {
    "temperature_2m_max": "最高气温", "temperature_2m_min": "最低气温",
    "apparent_temperature_max": "最高体感温度", "apparent_temperature_min": "最低体感温度",
    "precipitation_sum": "累计降水", "rain_sum": "累计降雨", "snowfall_sum": "累计降雪",
    "wind_speed_10m_max": "最大风速 · 10 m", "wind_gusts_10m_max": "最大阵风 · 10 m",
    "shortwave_radiation_sum": "累计短波辐射", "et0_fao_evapotranspiration": "参考蒸散量 ET₀",
    "sunrise": "日出", "sunset": "日落", "sunshine_duration": "日照时长",
}
MAX_ROWS = 1_000_000
MAX_VALUES = 5_000_000
MAX_REQUESTS = 150
HISTORY_BATCH_DAYS = 365
MODEL_OPTIONS = {
    "forecast": {"best_match": "自动匹配", "ecmwf_ifs025": "ECMWF IFS · 0.25°", "gfs_seamless": "GFS Seamless"},
    "historical_forecast": {"best_match": "自动匹配", "ecmwf_ifs025": "ECMWF IFS · 0.25°", "gfs_seamless": "GFS Seamless"},
    "archive": {"best_match": "自动匹配", "era5": "ERA5 · 长期一致性"},
}


def variable_catalog(source, frequency):
    if frequency == "daily":
        return dict(DAILY)
    variables = dict(HOURLY)
    if source == "archive":
        variables.update({"wind_speed_100m": "风速 · 100 m", "wind_direction_100m": "风向 · 100 m",
                          "soil_temperature_0_to_7cm": "土温 · 0–7 cm", "soil_moisture_0_to_7cm": "土壤湿度 · 0–7 cm"})
    else:
        variables.update({"wind_speed_80m": "风速 · 80 m", "wind_speed_120m": "风速 · 120 m",
                          "wind_direction_120m": "风向 · 120 m", "precipitation_probability": "降水概率",
                          "soil_temperature_0cm": "土温 · 0 cm", "soil_moisture_0_to_1cm": "土壤湿度 · 0–1 cm"})
    return variables


def today_local(tz="Asia/Shanghai"):
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(tz)).date()


def validate_job(job):
    source, frequency = job["source"], job["frequency"]
    if source not in SOURCES or frequency not in ("hourly", "daily"):
        raise ValueError("未知数据源或时间粒度。")
    variables = job["variables"]
    if not variables or len(set(variables)) != len(variables):
        raise ValueError("请至少选择一个特征，且不要重复。")
    if not set(variables).issubset(variable_catalog(source, frequency)):
        raise ValueError("部分变量不适用于当前数据源和粒度，请重新选择。")
    if job["model"] not in MODEL_OPTIONS[source]:
        raise ValueError("模型不适用于当前数据源。")
    if job["timezone"] not in ("Asia/Shanghai", "UTC"):
        raise ValueError("请选择北京时间或 UTC。")
    locations = job["locations"]
    if not 1 <= len(locations) <= 20:
        raise ValueError("每个任务支持 1–20 个地点。")
    names = []
    for loc in locations:
        name = str(loc["name"]).strip()
        if not name or len(name) > 80:
            raise ValueError("地点名称不能为空，且不能超过 80 字。")
        lat, lon = float(loc["latitude"]), float(loc["longitude"])
        if not math.isfinite(lat) or not math.isfinite(lon) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("纬度必须在 -90 到 90，经度必须在 -180 到 180 之间。")
        names.append(name)
    if len(names) != len(set(names)):
        raise ValueError("请为每个地点设置不同的名称。")
    start, end = date.fromisoformat(job["start_date"]), date.fromisoformat(job["end_date"])
    today = today_local(job["timezone"])
    if end < start:
        raise ValueError("结束日期不能早于开始日期。")
    if source == "forecast":
        if start != today or end > today + timedelta(days=15):
            raise ValueError("天气预报必须从所选时区的今天开始，最多 16 天。请刷新页面。")
    else:
        earliest = date(1940, 1, 1) if source == "archive" else date(2022, 1, 1)
        latest = today - timedelta(days=5 if source == "archive" else 2)
        if start < earliest or end > latest:
            raise ValueError(f"当前数据源允许 {earliest} 至 {latest}；近期数据预留发布延迟。")
    days = (end - start).days + 1
    rows = days * (24 if frequency == "hourly" else 1) * len(locations)
    count = len(locations) * (1 if source == "forecast" else math.ceil(days / HISTORY_BATCH_DAYS))
    if rows > MAX_ROWS or rows * len(variables) > MAX_VALUES or count > MAX_REQUESTS:
        raise ValueError("任务过大：每次最多 100 万行、500 万个气象数值、150 个请求。请缩短日期或减少地点/特征。")
    return {"days": days, "rows": rows, "values": rows * len(variables), "requests": count,
            "estimated_calls": len(locations) * max(1, len(variables) / 10) * (
                max(1, days / 14) if source == "forecast" else
                sum(max(1, n / 14) for n in [HISTORY_BATCH_DAYS] * (days // HISTORY_BATCH_DAYS) + ([days % HISTORY_BATCH_DAYS] if days % HISTORY_BATCH_DAYS else [])))}


def request_plan(job):
    validate_job(job)
    endpoint = SOURCES[job["source"]][1]
    start, end = date.fromisoformat(job["start_date"]), date.fromisoformat(job["end_date"])
    plan = []
    for loc in job["locations"]:
        cursor = start
        while cursor <= end:
            stop = end if job["source"] == "forecast" else min(end, cursor + timedelta(days=HISTORY_BATCH_DAYS - 1))
            params = {"latitude": float(loc["latitude"]), "longitude": float(loc["longitude"]),
                      job["frequency"]: ",".join(job["variables"]), "timezone": job["timezone"],
                      "wind_speed_unit": "ms", "temperature_unit": "celsius", "precipitation_unit": "mm"}
            if job["model"] != "best_match":
                params["models"] = job["model"]
            if job["source"] == "forecast":
                params["forecast_days"] = (end - start).days + 1
            else:
                params.update(start_date=cursor.isoformat(), end_date=stop.isoformat())
            plan.append({"url": endpoint, "params": params, "location": dict(loc)})
            cursor = stop + timedelta(days=1)
    return plan


def customer_url(url):
    return url.replace("https://api.", "https://customer-api.").replace(
        "https://archive-api.", "https://customer-archive-api.").replace(
        "https://historical-forecast-api.", "https://customer-historical-forecast-api.")


def fetch_request(url, params, api_key=""):
    """At most three attempts; never include credentials in saved metadata or errors."""
    params = dict(params)
    # Pace by weighted call cost, not HTTP request count, for long historical batches.
    request_days = params.get("forecast_days")
    if request_days is None:
        request_days = ((date.fromisoformat(params["end_date"]) - date.fromisoformat(params["start_date"])).days + 1
                        if "start_date" in params and "end_date" in params else 1)
    variable_count = len(params.get("hourly", params.get("daily", "")).split(","))
    pace_seconds = max(0.25, max(1, variable_count / 10) * max(1, request_days / 14) * 60 / 400)
    if api_key:
        url = customer_url(url)
        params["apikey"] = api_key
    for attempt in range(3):
        if not api_key:
            time.sleep(pace_seconds)
        try:
            response = requests.get(url, params=params, timeout=(10, 90))
        except requests.RequestException:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError("无法连接 Open-Meteo，请检查网络后重试。") from None
        if response.status_code == 429 or response.status_code >= 500:
            if attempt < 2:
                try:
                    wait = min(30, max(2 ** attempt, float(response.headers.get("Retry-After", 0))))
                except ValueError:
                    wait = 2 ** attempt
                time.sleep(wait)
                continue
            raise RuntimeError(f"Open-Meteo 暂时不可用或额度受限（HTTP {response.status_code}），请稍后重试。")
        if not response.ok:
            raise RuntimeError(f"Open-Meteo 拒绝请求（HTTP {response.status_code}）。请核对变量、模型、日期范围及订阅权限。")
        try:
            body = response.json()
        except ValueError:
            raise RuntimeError("接口返回了非 JSON 数据，请稍后重试。") from None
        if not isinstance(body, dict) or body.get("error"):
            raise RuntimeError("接口返回错误数据，请检查参数。")
        return {"retrieved_at_utc": datetime.now(timezone.utc).isoformat(), "response": body}
    raise RuntimeError("请求未完成。")


def combine_results(job, plan, results):
    if len(plan) != len(results):
        raise ValueError("任务未完整下载，不导出部分结果。")
    frames, units = [], {}
    frequency = job["frequency"]
    for entry, result in zip(plan, results):
        response = result["response"]
        block = response.get(frequency, {})
        if not block.get("time") or any(v not in block for v in job["variables"]):
            raise ValueError("接口缺少时间或所选变量，无法导出完整结果。")
        expected = ((entry["params"].get("forecast_days", 0)) or
                    (date.fromisoformat(entry["params"]["end_date"]) - date.fromisoformat(entry["params"]["start_date"])).days + 1)
        expected *= 24 if frequency == "hourly" else 1
        if len(block["time"]) != expected:
            raise ValueError("接口返回的时间范围不完整，请缩小日期范围或更换模型。")
        frame = pd.DataFrame({"time": block["time"], **{v: block[v] for v in job["variables"]}})
        loc = entry["location"]
        frame.insert(0, "location", str(loc["name"]))
        frame["requested_latitude"] = float(loc["latitude"])
        frame["requested_longitude"] = float(loc["longitude"])
        frame["grid_latitude"] = response.get("latitude")
        frame["grid_longitude"] = response.get("longitude")
        frame["timezone"] = response.get("timezone", job["timezone"])
        frame["retrieved_at_utc"] = result["retrieved_at_utc"]
        for variable, unit in response.get(f"{frequency}_units", {}).items():
            if variable in units and units[variable] != unit:
                raise ValueError("分批结果的单位不一致，停止合并。")
            units[variable] = unit
        frames.append(frame)
    combined = pd.concat(frames, ignore_index=True)
    if combined.duplicated(["location", "time"]).any():
        raise ValueError("接口返回了重复时间，请检查查询。")
    combined = combined.sort_values(["location", "time"]).reset_index(drop=True)
    metadata = {"source": "Open-Meteo", "attribution": "Weather data by Open-Meteo and its data providers (CC BY 4.0).",
                "source_url": "https://open-meteo.com/", "license_url": "https://open-meteo.com/en/licence",
                "job": copy.deepcopy(job), "units": units,
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
                "requests": [{**entry, "retrieved_at_utc": result["retrieved_at_utc"],
                              "utc_offset_seconds": result["response"].get("utc_offset_seconds"),
                              "elevation": result["response"].get("elevation")}
                             for entry, result in zip(plan, results)],
                "missing_values": {v: int(combined[v].isna().sum()) for v in job["variables"]},
                "notes": ["Time column is in the requested timezone; retrieval time is UTC.",
                          "Best match can combine models; selected model is a request setting, not per-value model provenance.",
                          "Hourly radiation is averaged over the preceding hour.",
                          "Historical forecast is a stitched series, not a forecast issued at a fixed historical time."]}
    return combined, metadata


def csv_bytes(frame):
    """Prevent location labels from being interpreted as spreadsheet formulas."""
    safe = frame.copy()
    for column in safe.select_dtypes(include=["object", "string"]).columns:
        safe[column] = safe[column].map(lambda v: "'" + v if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@")) else v)
    return safe.to_csv(index=False).encode("utf-8-sig")


def generated_script(job):
    # Embed this same engine so the exported file is standalone and uses identical validation.
    from pathlib import Path
    engine = Path(__file__).read_text(encoding="utf-8")
    return engine + '\n\nJOB = ' + repr(job) + '''

if __name__ == "__main__":
    from pathlib import Path
    # pip install requests pandas
    # Commercial access: set OPEN_METEO_API_KEY in your environment, never in source control.
    key = os.environ.get("OPEN_METEO_API_KEY", "")
    out = Path("weather_output")
    out.mkdir(exist_ok=True)
    if JOB["source"] == "forecast":
        # Refresh relative forecast dates whenever this script is run.
        length = (date.fromisoformat(JOB["end_date"]) - date.fromisoformat(JOB["start_date"])).days + 1
        JOB["start_date"] = today_local(JOB["timezone"]).isoformat()
        JOB["end_date"] = (today_local(JOB["timezone"]) + timedelta(days=length - 1)).isoformat()
    plan = request_plan(JOB)
    responses = []
    for index, entry in enumerate(plan):
        print(f"Fetching {index + 1}/{len(plan)}")
        responses.append(fetch_request(entry["url"], entry["params"], key))
        time.sleep(0.25)
    frame, metadata = combine_results(JOB, plan, responses)
    (out / "weather.csv").write_bytes(csv_bytes(frame))
    (out / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "raw_responses.json").write_text(json.dumps(responses, ensure_ascii=False), encoding="utf-8")
    print(f"Saved {len(frame)} rows to {out.resolve()}")
'''


def zip_bundle(frame, metadata, responses, script):
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("weather.csv", csv_bytes(frame))
        archive.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))
        archive.writestr("raw_responses.json", json.dumps(responses, ensure_ascii=False))
        archive.writestr("fetch_weather.py", script)
        archive.writestr("units.csv", csv_bytes(pd.DataFrame([
            {"variable": name, "unit": unit} for name, unit in metadata["units"].items()])))
    return buffer.getvalue()
