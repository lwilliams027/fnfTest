"""
Google Maps wrapper: geocoding + traffic-aware travel time.

Requires GOOGLE_MAPS_API_KEY env var. Enable these APIs in Google Cloud
Console for your project:
  - Geocoding API
  - Distance Matrix API
Billing must be enabled. Free tier covers a generous quota.

Mapbox Matrix is a viable cheaper alternative — same shape, swap the URLs.
"""

from datetime import datetime
import requests


class GoogleMapsClient:
    """Implements the RoutingService protocol from availability.py."""

    GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
    DISTANCE_URL = "https://maps.googleapis.com/maps/api/distancematrix/json"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._geocode_cache: dict[str, tuple[float, float]] = {}

    def geocode(self, address: str) -> tuple[float, float]:
        """Convert a street address to (lat, lng). Cached in-process."""
        if address in self._geocode_cache:
            return self._geocode_cache[address]
        resp = requests.get(
            self.GEOCODE_URL,
            params={"address": address, "key": self.api_key},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data["status"] != "OK" or not data["results"]:
            raise ValueError(f"Geocoding failed for {address!r}: {data['status']}")
        loc = data["results"][0]["geometry"]["location"]
        result = (loc["lat"], loc["lng"])
        self._geocode_cache[address] = result
        return result

    def travel_minutes(
        self,
        origin: tuple[float, float],
        dest: tuple[float, float],
        departure_time: datetime,
    ) -> int:
        """
        Returns traffic-aware driving time in minutes.

        Google requires departure_time >= now for traffic estimates. If you
        pass a past time, we fall back to non-traffic duration.
        """
        # Google rejects past departure_time for traffic — clamp to now
        ts = max(int(departure_time.timestamp()), int(datetime.now().timestamp()))

        params = {
            "origins": f"{origin[0]},{origin[1]}",
            "destinations": f"{dest[0]},{dest[1]}",
            "departure_time": ts,
            "traffic_model": "best_guess",
            "mode": "driving",
            "key": self.api_key,
        }
        resp = requests.get(self.DISTANCE_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "OK":
            raise ValueError(f"Distance Matrix failed: {data.get('status')}")
        element = data["rows"][0]["elements"][0]
        if element.get("status") != "OK":
            raise ValueError(f"Route element failed: {element.get('status')}")
        seconds = element.get("duration_in_traffic", element["duration"])["value"]
        return max(1, round(seconds / 60))
