#!/usr/bin/env python3

import re
from datetime import datetime
from typing import Optional, Tuple, Dict, List

import requests
import streamlit as st

# ----------------------------
# Location presets (type-aware)
# ----------------------------
PRESETS = {
    "Owyhee (near reservoir), NV": {
        "lat": 41.95,
        "lon": -116.92,
        "type": "lake",
    },
    "Henry's Lake (Island Park), ID": {
        "lat": 44.60,
        "lon": -111.36,
        "type": "lake",
    },
    "Whitebird / Hammer Creek (Salmon River), ID": {
        "lat": 45.75,
        "lon": -116.33,
        "type": "river",
        # USGS gage commonly used for this area:
        # Salmon River at White Bird, ID
        "usgs_site": "13317000",
        "usgs_name": "Salmon River at White Bird (USGS 13317000)",
    },
}

DEFAULT_PRESET_NAME = "Owyhee (near reservoir), NV"
DEFAULT_USER_AGENT = "FlyfishingWeatherPlanner (contact@example.com)"


# ----------------------------
# Threshold logic (uncertainty-aware)
# ----------------------------
def tighten_margin(days_out: int) -> int:
    """How many mph to subtract from thresholds as forecast gets farther out."""
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
    """Return (good_max, border_max) for a given days_out."""
    m = tighten_margin(days_out)
    good_max = max(min_good, good_base - m)
    border_max = max(min_border, border_base - m)
    border_max = max(border_max, good_max)
    return good_max, border_max


# ----------------------------
# Helpers
# ----------------------------
def wind_to_mph(wind_str: str) -> Optional[int]:
    """
    Convert NWS wind strings like:
      '5 to 10 mph', '10 mph', 'Around 15 mph'
    into a representative mph number (midpoint if range).
    """
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
    """Parse NWS ISO time (often includes timezone offset)."""
    return datetime.fromisoformat(start_time.replace("Z", "+00:00"))


def local_hour_from_isotime(start_time: str) -> int:
    return parse_nws_time(start_time).hour


def local_date_key(start_time: str) -> str:
    return parse_nws_time(start_time).date().isoformat()


def pretty_date(day_iso: str) -> str:
    """Convert YYYY-MM-DD into 'Tuesday, March 3' with cross-platform day formatting."""
    dt = datetime.fromisoformat(day_iso)
    try:
        return dt.strftime("%A, %B %-d")
    except ValueError:
        return dt.strftime("%A, %B %#d")


def days_out_from_date(day_iso: str) -> int:
    """Days between local 'today' and day_iso."""
    today = datetime.now().astimezone().date()
    day_date = datetime.fromisoformat(day_iso).date()
    return (day_date - today).days


def hour_label_12h(h: int) -> str:
    """0->12a, 12->12p, 13->1p, etc."""
    suffix = "a" if h < 12 else "p"
    hour12 = 12 if h % 12 == 0 else h % 12
    return f"{hour12}{suffix}"


def should_show_hour_label(h: int, start_h: int, end_h: int) -> bool:
    """Show only anchor labels to reduce clutter."""
    anchors = {start_h, end_h, 12, 15, 18}  # start, end, noon, 3p, 6p
    return h in anchors and start_h <= h <= end_h


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

    # Base on sustained/avg wind, then apply gust downgrade (good -> borderline)
    if wind_mph <= good_max:
        if gust_mph is not None and gust_mph >= gust_downgrade_at:
            return "borderline"
        return "good"
    if wind_mph <= border_max:
        return "borderline"
    return "bad"


# ----------------------------
# NWS Fetch
# ----------------------------
@st.cache_data(ttl=15 * 60)
def fetch_nws_hourly_periods(lat: float, lon: float, user_agent: str) -> List[dict]:
    headers = {"User-Agent": user_agent}

    point_url = f"https://api.weather.gov/points/{lat},{lon}"
    r = requests.get(point_url, headers=headers, timeout=30)
    r.raise_for_status()
    hourly_url = r.json()["properties"]["forecastHourly"]

    r = requests.get(hourly_url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()["properties"]["periods"]


# ----------------------------
# USGS Fetch (river flow)
# ----------------------------
@st.cache_data(ttl=10 * 60)
def usgs_latest_discharge_cfs(site_no: str) -> Tuple[Optional[float], Optional[str]]:
    """
    Returns (cfs, iso_datetime) for the most recent discharge reading, or (None, None).
    Uses USGS NWIS Instantaneous Values service with parameterCd=00060 (discharge in cfs).
    """
    url = "https://waterservices.usgs.gov/nwis/iv/"
    params = {
        "format": "json",
        "sites": site_no,
        "parameterCd": "00060",  # discharge (cfs)
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    j = r.json()

    series = j.get("value", {}).get("timeSeries", [])
    if not series:
        return None, None

    values = series[0].get("values", [])
    if not values or not values[0].get("value"):
        return None, None

    latest = values[0]["value"][-1]
    try:
        return float(latest["value"]), latest.get("dateTime")
    except Exception:
        return None, None


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
) -> Dict[str, Dict[int, str]]:
    """
    Returns:
      day_hours: day_iso -> hour -> tag (good/borderline/bad/unknown)
    """
    day_hours: Dict[str, Dict[int, str]] = {}

    for p in periods:
        start = p.get("startTime")
        if not start:
            continue

        hr = local_hour_from_isotime(start)
        if not (day_start_hour <= hr <= day_end_hour):
            continue

        day_iso = local_date_key(start)
        days_out = days_out_from_date(day_iso)

        wind = wind_to_mph(p.get("windSpeed", ""))
        gust = wind_to_mph(p.get("windGust", "")) if p.get("windGust") else None

        tag = hour_tag(
            wind_mph=wind,
            gust_mph=gust,
            days_out=days_out,
            good_base=good_base,
            border_base=border_base,
            min_good=min_good,
            min_border=min_border,
            gust_downgrade_at=gust_downgrade_at,
        )

        day_hours.setdefault(day_iso, {})[hr] = tag

    # Fill missing hours as unknown so every row has the same number of blocks
    for day_iso in list(day_hours.keys()):
        for hr in range(day_start_hour, day_end_hour + 1):
            day_hours[day_iso].setdefault(hr, "unknown")

    return day_hours


def render_color_key() -> None:
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


def render_timeline_strips(day_hours: Dict[str, Dict[int, str]], day_start_hour: int, day_end_hour: int) -> None:
    """Render each day as a row of touching rectangles (no gaps)."""
    colors = {
        "good": "#2ecc71",
        "borderline": "#f1c40f",
        "bad": "#e74c3c",
        "unknown": "#95a5a6",
    }

    st.markdown(
        """
        <style>
          .ow-row { display: flex; align-items: center; margin: 6px 0; }
          .ow-label { width: 240px; font-size: 14px; line-height: 1.15; }
          .ow-strip { display: flex; height: 20px; border-radius: 6px; overflow: hidden;
                      border: 1px solid rgba(0,0,0,0.08); }
          .ow-cell { width: 22px; height: 20px; margin: 0; padding: 0; }
          .ow-head { display:flex; align-items:center; margin: 10px 0 6px 0; }
          .ow-head-spacer { width: 240px; }
          .ow-hours { display:flex; }
          .ow-hour { width: 22px; font-size: 10px; text-align:center; color: #666; font-weight: 600; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # Centered label over the hours strip (not the whole page)
    st.markdown(
        """
        <div class="ow-head">
            <div class="ow-head-spacer"></div>
            <div class="ow-hours" style="justify-content:center; font-weight:600; font-size:14px; color:#444;">
                Fishing Hours (Local Time)
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Less-dense 12h header row
    hours_html_parts = []
    for h in range(day_start_hour, day_end_hour + 1):
        txt = hour_label_12h(h) if should_show_hour_label(h, day_start_hour, day_end_hour) else "&nbsp;"
        hours_html_parts.append(f"<div class='ow-hour'>{txt}</div>")

    hours_html = "".join(hours_html_parts)

    st.markdown(
        f"<div class='ow-head'><div class='ow-head-spacer'></div><div class='ow-hours'>{hours_html}</div></div>",
        unsafe_allow_html=True,
    )

    for day_iso in sorted(day_hours.keys()):
        label_html = pretty_date(day_iso)

        cells = []
        for hr in range(day_start_hour, day_end_hour + 1):
            tag = day_hours[day_iso].get(hr, "unknown")
            color = colors.get(tag, colors["unknown"])
            cells.append(
                f"<div class='ow-cell' title='{hour_label_12h(hr)} — {tag}' style='background:{color}'></div>"
            )

        st.markdown(
            f"<div class='ow-row'>"
            f"  <div class='ow-label'>{label_html}</div>"
            f"  <div class='ow-strip'>{''.join(cells)}</div>"
            f"</div>",
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

    location = PRESETS[preset_name]
    lat, lon = location["lat"], location["lon"]
    loc_type = location.get("type", "lake")
    user_agent = DEFAULT_USER_AGENT  # internal; not user-editable

    st.header("Fishing window")
    day_start = st.slider("Start time", 0, 23, 9)
    day_end = st.slider("End time", 0, 23, 19)

    st.header("Wind thresholds (sustained wind)")
    good_base = st.slider("Good max wind (mph)", 1, 25, 10)
    border_base = st.slider("Borderline max wind (mph)", 1, 30, 14)
    gust_downgrade_at = st.slider("Downgrade if gust ≥ (mph)", 10, 50, 20)

if day_end < day_start:
    st.error("End time must be >= start time.")
    st.stop()

# Fetch NWS wind forecast
try:
    periods = fetch_nws_hourly_periods(lat, lon, user_agent)
except Exception as e:
    st.error(f"Failed to fetch NWS data: {e}")
    st.stop()

st.subheader(f"Timeline — {preset_name}")
render_color_key()

day_hours = build_day_hour_tags(
    periods=periods,
    day_start_hour=day_start,
    day_end_hour=day_end,
    good_base=good_base,
    border_base=border_base,
    min_good=6,
    min_border=10,
    gust_downgrade_at=gust_downgrade_at,
)

if not day_hours:
    st.write("No hourly data fell within the selected fishing window.")
else:
    render_timeline_strips(day_hours, day_start, day_end)

# Optional: show USGS river flow only for river locations
if loc_type == "river" and location.get("usgs_site"):
    st.subheader("River flow (USGS)")

    site = location["usgs_site"]
    river_name = location.get("usgs_name", f"USGS site {site}")

    cfs, when = usgs_latest_discharge_cfs(site)

    col1, col2 = st.columns([2, 3])
    with col1:
        st.metric("Current discharge", "—" if cfs is None else f"{cfs:,.0f} CFS")
    with col2:
        st.write(river_name)
        if when:
            st.caption(f"Observed: {when}")
        else:
            st.caption("Observation time unavailable")
