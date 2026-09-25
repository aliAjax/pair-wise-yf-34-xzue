"""续航放行计算规则。

纯函数模块：只负责把航线、机型参数和审核员填写的风阻系数折算成分钟并判定
是否满足满电时长，不依赖数据库或 HTTP 入口，与机型/计划数据、办理入口分开维护。
"""
from __future__ import annotations

import math
from typing import Any

EARTH_RADIUS_KM = 6371.0088
WIND_FACTOR_MIN = 1.0
WIND_FACTOR_MAX = 1.5
DEFAULT_WIND_FACTOR = 1.0


def leg_distance_km(a: list[float], b: list[float]) -> float:
    """相邻航点之间的球面直线距离，单位公里。"""
    lon1, lat1 = math.radians(a[0]), math.radians(a[1])
    lon2, lat2 = math.radians(b[0]), math.radians(b[1])
    dlon, dlat = lon2 - lon1, lat2 - lat1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def route_distance_km(route: list[list[float]]) -> float:
    """按相邻航点直线距离累加航线总里程。"""
    return sum(leg_distance_km(route[i], route[i + 1]) for i in range(len(route) - 1))


def validate_wind_factor(value: Any) -> float:
    """审核员填写的风阻系数必须在 1.0 到 1.5 之间。"""
    try:
        factor = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"风阻系数必须是数字: {value!r}") from None
    if not WIND_FACTOR_MIN <= factor <= WIND_FACTOR_MAX:
        raise ValueError(f"风阻系数必须在 {WIND_FACTOR_MIN} 到 {WIND_FACTOR_MAX} 之间")
    return factor


def evaluate_endurance(route: list[list[float]], cruise_speed_kmh: float, endurance_minutes: float,
                       return_home_minutes: float, wind_factor: float = DEFAULT_WIND_FACTOR) -> dict[str, Any]:
    """续航放行核算：里程 ÷ 巡航速度 × 风阻系数 ＋ 安全返航分钟，不得超过满电时长。"""
    distance_km = route_distance_km(route)
    base_minutes = distance_km / cruise_speed_kmh * 60
    required_minutes = base_minutes * wind_factor + return_home_minutes
    shortfall = max(0.0, required_minutes - endurance_minutes)
    return {
        "distance_km": round(distance_km, 3),
        "cruise_speed_kmh": cruise_speed_kmh,
        "base_minutes": round(base_minutes, 2),
        "wind_factor": wind_factor,
        "return_home_minutes": return_home_minutes,
        "required_minutes": round(required_minutes, 2),
        "endurance_minutes": endurance_minutes,
        "shortfall_minutes": round(shortfall, 2),
        "ok": required_minutes <= endurance_minutes + 1e-9,
    }
