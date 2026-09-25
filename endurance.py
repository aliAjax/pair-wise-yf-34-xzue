"""续航放行计算规则（纯函数）：球面距离累加、风阻折算、返航裕度。不依赖数据层。"""
from __future__ import annotations

import math
from typing import Any

EARTH_RADIUS_KM = 6371.0088
WIND_FACTOR_MIN = 1.0
WIND_FACTOR_MAX = 1.5


def haversine_km(a: list[float], b: list[float]) -> float:
    """两个 [经度,纬度] 航点之间的球面直线距离（公里）。"""
    lon1, lat1, lon2, lat2 = (math.radians(v) for v in (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def route_distance_km(route: list[list[float]]) -> float:
    """按相邻航点直线距离累加航线里程（公里）。"""
    return sum(haversine_km(a, b) for a, b in zip(route, route[1:]))


def wind_factor_valid(value: float) -> bool:
    """审核员填写的风阻系数必须在 1.0 到 1.5 之间。"""
    return WIND_FACTOR_MIN <= value <= WIND_FACTOR_MAX


def assess_endurance(route: list[list[float]], cruise_speed_kmh: float, endurance_minutes: float,
                     return_minutes: float, wind_factor: float) -> dict[str, Any]:
    """续航放行计算：折算耗时 = 航线里程 / 巡航速度 × 风阻系数，再加安全返航分钟，不得超过满电时长。"""
    distance_km = route_distance_km(route)
    cruise_minutes = distance_km / cruise_speed_kmh * 60
    adjusted_minutes = cruise_minutes * wind_factor
    required_minutes = adjusted_minutes + return_minutes
    margin_minutes = endurance_minutes - required_minutes
    return {
        "route_distance_km": round(distance_km, 3),
        "cruise_minutes": round(cruise_minutes, 1),
        "wind_factor": wind_factor,
        "adjusted_minutes": round(adjusted_minutes, 1),
        "return_minutes": return_minutes,
        "required_minutes": round(required_minutes, 1),
        "endurance_minutes": endurance_minutes,
        "margin_minutes": round(margin_minutes, 1),
        "shortfall_minutes": round(-margin_minutes, 1) if margin_minutes < 0 else 0.0,
        "energy_ok": margin_minutes >= 0,
    }
