# -*- coding: UTF-8 -*-
"""从 Keep 运动详情数据生成 FIT/TCX 文件。

解码逻辑参照 running_page 项目 (https://github.com/yihong0618/running_page)
run_page/keep_sync.py 的实测实现。
"""
import base64
import json
import os
import zlib
from datetime import datetime, timedelta, timezone
from xml.dom import minidom
import xml.etree.ElementTree as ET

import eviltransform
from Crypto.Cipher import AES
from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.activity_message import ActivityMessage
from fit_tool.profile.messages.event_message import EventMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.profile_type import (
    Event,
    EventType,
    FileType,
    LapTrigger,
    Manufacturer,
    Sport,
    SubSport,
)

AES_KEY = base64.b64decode("NTZmZTU5OzgyZzpkODczYw==")
AES_IV = base64.b64decode("MjM0Njg5MjQzMjkyMDMwMA==")
# 时间单位均为分秒(1/10秒)
HR_FRAME_THRESHOLD_IN_DECISECOND = 100
TIMESTAMP_THRESHOLD_IN_DECISECOND = 3_600_000

# Keep dataType -> FIT (Sport, SubSport)
FIT_SPORT_MAP = {
    "outdoorRunning": (Sport.RUNNING, SubSport.STREET),
    "indoorRunning": (Sport.RUNNING, SubSport.INDOOR_RUNNING),
    "outdoorCycling": (Sport.CYCLING, SubSport.STREET),
    "indoorCycling": (Sport.CYCLING, SubSport.INDOOR_CYCLING),
    "outdoorWalking": (Sport.WALKING, SubSport.GENERIC),
    "mountaineering": (Sport.MOUNTAINEERING, SubSport.GENERIC),
    "running": (Sport.RUNNING, SubSport.STREET),
    "hiking": (Sport.HIKING, SubSport.GENERIC),
    "cycling": (Sport.CYCLING, SubSport.STREET),
}

# Keep dataType -> TCX Activity Sport 属性 (参照 running_page KEEP2TCX)
TCX_SPORT_MAP = {
    "outdoorRunning": "Running",
    "indoorRunning": "Running",
    "outdoorCycling": "Biking",
    "indoorCycling": "Biking",
    "outdoorWalking": "Walking",
    "mountaineering": "Hiking",
    "running": "Running",
    "hiking": "Hiking",
    "cycling": "Biking",
}


def decode_keep_payload(text, is_geo=False):
    """解码 Keep 加密时序数据: base64 -> (AES-CBC 解密 if geo) -> zlib -> JSON"""
    _bytes = base64.b64decode(text)
    if is_geo:
        cipher = AES.new(AES_KEY, AES.MODE_CBC, AES_IV)
        _bytes = cipher.decrypt(_bytes)
    return json.loads(zlib.decompress(_bytes, 16 + zlib.MAX_WBITS))


def find_nearest_hr(hr_data_list, target_time, start_time, threshold=HR_FRAME_THRESHOLD_IN_DECISECOND):
    """在 threshold(分秒)范围内找与 target_time(分秒)最近的心率点。

    target_time 为绝对时间戳(分秒)时先转成相对运动开始的偏移。
    """
    if target_time > TIMESTAMP_THRESHOLD_IN_DECISECOND:
        target_time = target_time - start_time // 100
    closest = None
    min_difference = float("inf")
    for item in hr_data_list:
        timestamp = item.get("timestamp")
        if not timestamp:
            continue
        difference = abs(timestamp - target_time)
        if difference <= threshold and difference < min_difference:
            closest = item
            min_difference = difference
    if closest:
        hr = closest.get("beatsPerMinute")
        if hr and hr > 0:
            return hr
    return None


def parse_workout_points(data):
    """解码 GPS 轨迹和心率时序。

    返回 (points, hr_series, start_time)：
    - points: [{latitude(WGS84), longitude(WGS84), altitude, timestamp(分秒), hr(可选)}]
    - hr_series: [{timestamp(分秒), beatsPerMinute}]
    - start_time: 运动开始毫秒时间戳
    """
    start_time = data["startTime"]
    hr_series = []
    heart_rate = data.get("heartRate") or {}
    heart_rates_text = heart_rate.get("heartRates")
    if heart_rates_text:
        try:
            hr_series = decode_keep_payload(heart_rates_text, is_geo=False)
        except Exception as e:
            print(f"心率数据解码失败: {e}")
            hr_series = []

    points = []
    geo_points_text = data.get("geoPoints")
    if geo_points_text:
        try:
            raw_points = decode_keep_payload(geo_points_text, is_geo=True)
        except Exception as e:
            print(f"轨迹数据解码失败: {e}")
            raw_points = []
        for p in raw_points:
            if "timestamp" not in p:
                p["timestamp"] = p.get("unixTimestamp", 0)
            # GCJ02 -> WGS84，避免导入 Garmin/Strava 后轨迹偏移
            lat, lng = eviltransform.gcj2wgs(p["latitude"], p["longitude"])
            p["latitude"] = lat
            p["longitude"] = lng
            if hr_series:
                hr = find_nearest_hr(hr_series, int(p["timestamp"]), start_time)
                if hr:
                    p["hr"] = hr
            points.append(p)
    return points, hr_series, start_time


def _point_epoch_ms(timestamp, start_time, absolute_ts):
    """轨迹点/心率点时间戳(分秒) -> 绝对毫秒时间戳

    Keep 新数据的时间戳是绝对分秒时间戳, 老数据是相对运动开始的偏移。
    """
    if absolute_ts:
        return timestamp * 100
    return start_time + timestamp * 100


def _is_absolute_timestamp(series):
    return bool(series) and int(series[0].get("timestamp", 0)) > TIMESTAMP_THRESHOLD_IN_DECISECOND


def _get_sport(data, log_type=None):
    data_type = data.get("dataType") or log_type or "running"
    return FIT_SPORT_MAP.get(data_type, (Sport.RUNNING, SubSport.STREET)), data_type


def generate_fit(data, file_path, log_type=None):
    (sport, sub_sport), _ = _get_sport(data, log_type)
    points, hr_series, start_time = parse_workout_points(data)
    start_ms = data["startTime"]
    end_ms = data["endTime"]
    duration = data.get("duration") or 0
    distance = data.get("distance") or 0
    calorie = data.get("calorie") or 0

    builder = FitFileBuilder(auto_define=True)

    fid = FileIdMessage()
    fid.time_created = start_ms
    fid.type = FileType.ACTIVITY
    fid.manufacturer = Manufacturer.DEVELOPMENT
    fid.product = 1
    fid.serial_number = 1
    builder.add(fid)

    event_start = EventMessage()
    event_start.timestamp = start_ms
    event_start.event = Event.TIMER
    event_start.event_type = EventType.START
    builder.add(event_start)

    activity = ActivityMessage()
    activity.timestamp = end_ms
    activity.num_sessions = 1
    builder.add(activity)

    if points:
        absolute_ts = _is_absolute_timestamp(points)
        for p in points:
            record = RecordMessage()
            record.timestamp = _point_epoch_ms(int(p["timestamp"]), start_time, absolute_ts)
            record.position_lat = p["latitude"]
            record.position_long = p["longitude"]
            altitude = p.get("altitude")
            if altitude is not None and -500 <= float(altitude) <= 12600:
                record.altitude = float(altitude)
            if p.get("hr"):
                record.heart_rate = int(p["hr"])
            builder.add(record)
    elif hr_series:
        absolute_ts = _is_absolute_timestamp(hr_series)
        for item in hr_series:
            timestamp = item.get("timestamp")
            bpm = item.get("beatsPerMinute")
            if not timestamp or not bpm or bpm <= 0:
                continue
            record = RecordMessage()
            record.timestamp = _point_epoch_ms(int(timestamp), start_time, absolute_ts)
            record.heart_rate = int(bpm)
            builder.add(record)

    heart_rate = data.get("heartRate") or {}
    avg_hr = heart_rate.get("averageHeartRate")
    max_hr = heart_rate.get("maxHeartRate")
    session = SessionMessage()
    session.timestamp = end_ms
    session.start_time = start_ms
    session.sport = sport
    session.sub_sport = sub_sport
    session.total_elapsed_time = duration
    session.total_timer_time = duration
    session.total_distance = distance
    session.total_calories = calorie
    session.message_index = 0
    if avg_hr and avg_hr > 0:
        session.avg_heart_rate = int(avg_hr)
    if max_hr and max_hr > 0:
        session.max_heart_rate = int(max_hr)
    builder.add(session)

    lap = LapMessage()
    lap.timestamp = end_ms
    lap.start_time = start_ms
    lap.total_elapsed_time = duration
    lap.total_timer_time = duration
    lap.total_distance = distance
    lap.total_calories = calorie
    lap.message_index = 0
    lap.lap_trigger = LapTrigger.MANUAL
    builder.add(lap)

    event_stop = EventMessage()
    event_stop.timestamp = end_ms
    event_stop.event = Event.TIMER
    event_stop.event_type = EventType.STOP_ALL
    builder.add(event_stop)

    builder.build().to_file(file_path)
    return file_path


def generate_tcx(data, file_path, log_type=None):
    _, data_type = _get_sport(data, log_type)
    points, hr_series, start_time = parse_workout_points(data)
    tcx_sport = TCX_SPORT_MAP.get(data_type, "Running")
    start_dt = datetime.fromtimestamp(data["startTime"] / 1000, tz=timezone.utc)
    start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    tcd = ET.Element(
        "TrainingCenterDatabase",
        {
            "xmlns": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2",
            "xmlns:ns5": "http://www.garmin.com/xmlschemas/ActivityGoals/v1",
            "xmlns:ns3": "http://www.garmin.com/xmlschemas/ActivityExtension/v2",
            "xmlns:ns2": "http://www.garmin.com/xmlschemas/UserProfile/v2",
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xmlns:ns4": "http://www.garmin.com/xmlschemas/ProfileExtension/v1",
            "xsi:schemaLocation": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2 http://www.garmin.com/xmlschemas/TrainingCenterDatabasev2.xsd",
        },
    )
    ET.ElementTree(tcd)
    activities = ET.Element("Activities")
    tcd.append(activities)
    activity = ET.Element("Activity", {"Sport": tcx_sport})
    activities.append(activity)
    activity_id = ET.Element("Id")
    activity_id.text = start_iso
    activity.append(activity_id)
    lap = ET.Element("Lap", {"StartTime": start_iso})
    activity.append(lap)

    def append_text(parent, tag, text):
        el = ET.Element(tag)
        el.text = str(text)
        parent.append(el)
        return el

    append_text(lap, "TotalTimeSeconds", data.get("duration") or 0)
    append_text(lap, "DistanceMeters", data.get("distance") or 0)
    append_text(lap, "Calories", data.get("calorie") or 0)

    track = ET.Element("Track")
    lap.append(track)
    if points:
        absolute_ts = _is_absolute_timestamp(points)
        for p in points:
            epoch_ms = _point_epoch_ms(int(p["timestamp"]), start_time, absolute_ts)
            time_iso = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            tp = ET.Element("Trackpoint")
            track.append(tp)
            append_text(tp, "Time", time_iso)
            position = ET.Element("Position")
            tp.append(position)
            append_text(position, "LatitudeDegrees", p["latitude"])
            append_text(position, "LongitudeDegrees", p["longitude"])
            if p.get("altitude") is not None:
                append_text(tp, "AltitudeMeters", p["altitude"])
            if p.get("hr"):
                bpm = ET.Element("HeartRateBpm")
                tp.append(bpm)
                append_text(bpm, "Value", int(p["hr"]))
    elif hr_series:
        absolute_ts = _is_absolute_timestamp(hr_series)
        for item in hr_series:
            timestamp = item.get("timestamp")
            bpm = item.get("beatsPerMinute")
            if not timestamp or not bpm or bpm <= 0:
                continue
            epoch_ms = _point_epoch_ms(int(timestamp), start_time, absolute_ts)
            time_iso = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            tp = ET.Element("Trackpoint")
            track.append(tp)
            append_text(tp, "Time", time_iso)
            bpm_el = ET.Element("HeartRateBpm")
            tp.append(bpm_el)
            append_text(bpm_el, "Value", int(bpm))

    xml_str = minidom.parseString(ET.tostring(tcd))
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(xml_str.toprettyxml())
    return file_path


SHANGHAI_TZ = timezone(timedelta(hours=8))


def generate_workout_files(data, out_dir, log_type=None):
    """生成 FIT 和 TCX 两个文件，返回 (fit_path, tcx_path)。"""
    start_time = data.get("startTime", 0) / 1000
    name = "keep_" + datetime.fromtimestamp(start_time, tz=SHANGHAI_TZ).strftime(
        "%Y%m%d_%H%M%S"
    )
    fit_path = os.path.join(out_dir, f"{name}.fit")
    tcx_path = os.path.join(out_dir, f"{name}.tcx")
    generate_fit(data, fit_path, log_type)
    generate_tcx(data, tcx_path, log_type)
    return fit_path, tcx_path
