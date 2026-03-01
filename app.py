#!/usr/bin/env python3

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple, Dict, List

import pandas as pd
import requests
import streamlit as st

# ----------------------------
# Location presets
# ----------------------------
PRESETS = {
    "Owyhee (near reservoir), NV": (41.95, -116.92),
    "Henry's Lake, ID": (44.60, -111.36),
    "Whitebird, ID": (45.75, -116.33),
}

DEFAULT_PRESET_NAME = "Owyhee (near reservoir), NV"
DEFAULT_USER_AGENT = "OwyheeWindPicker (contact@example.com)"

# ----------------------------
# Threshold logic (uncertainty-aware)
# ----------------------------

def tighten_margin(days_out: int) -> int:
    if days_out <= 2:
        return 0
    if days_out <= 4:
        return 1
    if days_out == 5:
        return 2
    return 3


def effective_thresholds(
    days_out: int,
    good_base: int,
    border_base: int,
    min_good: int,
    min_border: int,
) -> Tuple[int, int]:
    m = tighten_margin(days_out)
    good_max = max(min_good, good_base - m)
    border_max = max(min_border, border_base - m)
    border_max = max(border_max, good_max)
    return good_max, border_max


# ----------------------------
# Helpers
# ----------------------------

def pretty_date(day_iso: str) -> str:
    dt = datetime.fromisoformat(day_iso)
    try:
        return dt.strftime("%A, %B %-d")
    except ValueError:
        return dt.strftime("%A, %B %#d")


def wind_to_mph(wind_str: str) -> Optional[int]:
    if not wind_str:
        return None
    s = wind_str.lower()
    nums = [int(n) for n in re.findall(r"\d+", s)]
    if not nums:
        return None
    if "to" in s and len(nums) >= 2:
        return round((nums[0] + nums[1]) / 2)
    return nums[0]


def parse_nws_time(start_time: str) -> datetime:
    return datetime.fromisoformat(start_time.replace("Z", "+00:00"))


def local_hour_from_isotime(start_time: str) -> int:
    return parse_nws_time(start_time).hour


def local_date_key(start_time: str) -> str:
    return parse_nws_time(start_time).date().isoformat()


def days_out_from_date(day_iso: str) -> int:
    today = datetime.now().astimezone().date()
    day_date = datetime.fromisoformat(day_iso).date()
    return (day_date - today).days


# ----------------------------
# Classification
# ----------------------------

def hour_tag(
    wind_mph: Optional[int],
    gust_mph: Optional[int],
    days_out: int,
    good_base: int,
    border_base: int,
    min_good: int,
    min_border: int,
    gust_downgrade_at: int,
) -> str:
    if wind_mph is None:
        return "unknown"

    good_max, border_max = effective_thresholds(days_out, good_base, border_base, min_good, min_border)

    if wind_mph <= good_max:
        if gust_mph is not None and gust_mph >= gust_downgrade_at:
            return "borderline"
        return "good"

    if wind_mph <= border_max:
        return "borderline"

    return "bad"


# ----------------------------
# Data classes (kept for future expansion)
# ----------------------------

@dataclass
class DaySummary:
    day: str
    days_out: int
    good_max: int
    border_max: int
    good_hours: int
    border_hours: int
    bad_hours: int
    best_good_stretch: int
    qualifies: bool


# ----------------------------
# NWS Fetch
# ----------------------------

@st.cache_data(ttl=15 * 60)
def fetch_hourly_periods(lat: float, lon: float, user_agent: str) -> List[dict]:
    headers = {"User-Agent": user_agent}
    point_url = f"https://api.weather.gov/points/{lat},{lon}"
    r = requests.get(point_url, headers=headers, timeout=30)
    r.raise_for_status()
    hourly_url = r.json()["properties"]["forecastHourly"]

    r = requests.get(hourly_url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()["properties"]["periods"]


# ----------------------------
# Timeline helpers
# ----------------------------

def build_day_hour_tags(
    periods: List[dict],
    day_start_hour: int,
    day_end_hour: int,
    good_base: int,
    border_base: int,
    min_good: int,
    min_border: int,
    gust_downgrade_at: int,
):
    """
    Returns:
      day_hours: day_iso -> hour -> tag (good/borderline/bad/unknown)
      day_meta:  day_iso -> (days_out, good_max, border_max)
    """
    day_hours: Dict[str, Dict[int, str]] = {}
    day_meta: Dict[str, Tuple[int, int, int]] = {}

    for p in periods:
        start = p.get("startTime")
        if not start:
            continue

        hr = local_hour_from_isotime(start)
        if not (day_start_hour <= hr <= day_end_hour):
            continue

        day_iso = local_date_key(start)
        days_out = days_out_from_date(day_iso)

        good_max, border_max = effective_thresholds(days_out, good_base, border_base, min_good, min_border)

        wind = wind_to_mph(p.get("windSpeed", ""))
        gust = wind_to_mph(p.get("windGust", "")) if p.get("windGust") else None

        tag = hour_tag(
            wind,
            gust,
            days_out,
            good_base,
            border_base,
            min_good,
            min_border,
            gust_downgrade_at,
        )

        day_hours.setdefault(day_iso, {})[hr] = tag
        day_meta[day_iso] = (days_out, good_max, border_max)

    # Ensure each day has every hour in the window (fill missing as unknown)
    for day_iso in list(day_hours.keys()):
        for hr in range(day_start_hour, day_end_hour + 1):
            day_hours[day_iso].setdefault(hr, "unknown")

    return day_hours, day_meta


def render_color_key():
    st.markdown(
        """
        <style>
          .ow-key { display:flex; gap:18px; margin: 6px 0 6px 0; flex-wrap: wrap; }
          .ow-key-item { display:flex; align-items:center; gap:6px; font-size:13px; }
          .ow-swatch { width:14px; height:14px; border-radius:3px; border:1px solid rgba(0,0,0,0.2); }
          .ow-key-note { font-size:12px; color:#666; margin-top:4px; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <div class="ow-key">
          <div class="ow-key-item">
            <div class="ow-swatch" style="background:#2ecc71"></div>
            <strong>Good</strong> – Comfortable float-tube conditions.
          </div>

          <div class="ow-key-item">
            <div class="ow-swatch" style="background:#f1c40f"></div>
            <strong>Borderline</strong> – Fishable but windy or gusty.
          </div>

          <div class="ow-key-item">
            <div class="ow-swatch" style="background:#e74c3c"></div>
            <strong>Bad</strong> – Likely too windy.
          </div>

          <div class="ow-key-item">
            <div class="ow-swatch" style="background:#95a5a6"></div>
            <strong>Unknown</strong> – No data available.
          </div>
        </div>

        <div class="ow-key-note">
          Thresholds tighten slightly for days farther out to reflect forecast uncertainty.
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_timeline_strips(day_hours, day_meta, day_start_hour, day_end_hour):
    colors = {
        "good": "#2ecc71",
        "borderline": "#f1c40f",
        "bad": "#e74c3c",
        "unknown": "#95a5a6",
    }

    st.markdown(
        """
        <style>
          .ow-row { display:flex; align-items:center; margin:6px 0; }
          .ow-label { width:260px; font-size:14px; line-height:1.1; }
          .ow-strip { display:flex; height:20px; border-radius:6px; overflow:hidden;
                      border:1px solid rgba(0,0,0,0.08); }
          .ow-cell { width:22px; height:20px; }
          .ow-head { display:flex; align-items:center; margin:10px 0 6px 0; }
          .ow-head-spacer { width:260px; }
          .ow-hours { display:flex; }
          .ow-hour { width:22px; font-size:10px; text-align:center; color:#666; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    hours_html = "".join([f"<div class='ow-hour'>{h}</div>" for h in range(day_start_hour, day_end_hour + 1)])
    st.markdown(
        f"<div class='ow-head'><div class='ow-head-spacer'></div><div class='ow-hours'>{hours_html}</div></div>",
        unsafe_allow_html=True,
    )

    for day_iso in sorted(day_hours.keys()):
        days_out, good_max, border_max = day_meta.get(day_iso, (None, None, None))
        label = (
            f"{pretty_date(day_iso)}<br>"
  #          f"<span style='color:#666;font-size:12px'>D+{days_out} | good≤{good_max} border≤{border_max}</span>"
        )

        cells = []
        for hr in range(day_start_hour, day_end_hour + 1):
            tag = day_hours[day_iso].get(hr, "unknown")
            color = colors.get(tag, colors["unknown"])
            cells.append(f"<div class='ow-cell' title='{hr}:00 — {tag}' style='background:{color}'></div>")

        st.markdown(
            f"<div class='ow-row'><div class='ow-label'>{label}</div>"
            f"<div class='ow-strip'>{''.join(cells)}</div></div>",
            unsafe_allow_html=True,
        )


# ----------------------------
# Streamlit UI
# ----------------------------

st.set_page_config(page_title="Flyfishing Weather Planner", layout="wide")
st.title("Flyfishing Weather Planner")

with st.sidebar:
    st.header("Location")

    preset_name = st.selectbox(
        "Select fishing location",
        options=list(PRESETS.keys()),
        index=list(PRESETS.keys()).index(DEFAULT_PRESET_NAME),
    )

    lat, lon = PRESETS[preset_name]

    # Keep User-Agent internal (not user editable)
    user_agent = DEFAULT_USER_AGENT

    # If preset changes, these update automatically (because values depend on preset_*)
#    lat = st.number_input("Latitude", value=float(preset_lat), format="%.5f")
#    lon = st.number_input("Longitude", value=float(preset_lon), format="%.5f")

#    user_agent = st.text_input("User-Agent", value=DEFAULT_USER_AGENT)

    st.header("Fishing window (local)")
    day_start = st.slider("Start hour", 0, 23, 9)
    day_end = st.slider("End hour", 0, 23, 19)

    st.header("Wind thresholds (near-term)")
    good_base = st.slider("Good conditions if wind less than (mph)", 1, 25, 10)
    border_base = st.slider("Borderline wind conditions if more than (mph)", 1, 30, 14)
    gust_downgrade_at = st.slider("Red if gusts more than (mph)", 10, 50, 20)

if day_end < day_start:
    st.error("End hour must be >= start hour.")
    st.stop()

# Fetch
try:
    periods = fetch_hourly_periods(lat, lon, user_agent)
except Exception as e:
    st.error(f"Failed to fetch NWS data: {e}")
    st.stop()

# Timeline
st.subheader(f"Fishing window timeline — {preset_name}")
render_color_key()

day_hours, day_meta = build_day_hour_tags(
    periods,
    day_start,
    day_end,
    good_base,
    border_base,
    6,   # min_good (floor)
    10,  # min_border (floor)
    gust_downgrade_at,
)

if not day_hours:
    st.write("No hourly data fell within the selected fishing window.")
else:
    render_timeline_strips(day_hours, day_meta, day_start, day_end)
