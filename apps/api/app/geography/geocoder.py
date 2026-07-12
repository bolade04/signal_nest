"""Geocoding abstraction. Fixture-based by default (offline, deterministic).

Real deployments swap in a geocoding provider behind the same ``geocode`` signature.
"""

from __future__ import annotations

from dataclasses import dataclass

_FIXTURE_CITIES: dict[str, tuple[float, float, str, str, str, str]] = {
    # key: (lat, lon, city, state/province, country, timezone)
    "dallas": (32.7767, -96.7970, "Dallas", "Texas", "United States", "America/Chicago"),
    "london": (51.5074, -0.1278, "London", "England", "United Kingdom", "Europe/London"),
    "lagos": (6.5244, 3.3792, "Lagos", "Lagos", "Nigeria", "Africa/Lagos"),
    "nairobi": (-1.2921, 36.8219, "Nairobi", "Nairobi", "Kenya", "Africa/Nairobi"),
    "austin": (30.2672, -97.7431, "Austin", "Texas", "United States", "America/Chicago"),
    "new york": (40.7128, -74.0060, "New York", "New York", "United States", "America/New_York"),
    "manchester": (53.4808, -2.2426, "Manchester", "England", "United Kingdom", "Europe/London"),
}


@dataclass
class GeocodeResult:
    latitude: float
    longitude: float
    city: str
    state_province: str
    country: str
    timezone: str
    confidence: float


def geocode(query: str) -> GeocodeResult | None:
    q = (query or "").strip().lower()
    for key, (lat, lon, city, state, country, tz) in _FIXTURE_CITIES.items():
        if key in q:
            return GeocodeResult(lat, lon, city, state, country, tz, confidence=0.95)
    return None
