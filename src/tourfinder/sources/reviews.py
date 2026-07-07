"""Pluggable guest-review providers.

Reviews are not in the tour operator's data (Join Up exposes none), so they
come from an external platform matched by hotel name + location. Each
provider returns a normalized dict or a not_found/ambiguous marker; the
provider stays inert (returns None) when it has no credentials, so the code
is safe to ship without keys.
"""
import logging
import os
import time

import requests

log = logging.getLogger(__name__)


class ReviewProvider:
    platform = "base"

    def available(self) -> bool:
        return False

    def lookup(self, *, name: str, city: str | None, country: str | None,
               lat: float | None, lon: float | None) -> dict | None:
        """Return a review dict, a {'match_status': ...} marker, or None
        when the provider can't run. Dict shape:
            rating, rating_scale, reviews_count, summary, external_id, url,
            matched_name, match_status
        """
        raise NotImplementedError


class GooglePlacesReviews(ReviewProvider):
    """Google Places Text Search. Needs GOOGLE_PLACES_API_KEY. Free tier
    covers personal use. Matches by "<name> <city> <country>" and, when
    coordinates are given, prefers the nearest candidate."""
    platform = "google"
    TEXT_SEARCH = "https://maps.googleapis.com/maps/api/place/textsearch/json"

    def __init__(self, api_key: str | None = None, delay: float = 0.4):
        self.api_key = api_key or os.environ.get("GOOGLE_PLACES_API_KEY")
        self.delay = delay
        self.session = requests.Session()

    def available(self) -> bool:
        return bool(self.api_key)

    def lookup(self, *, name, city, country, lat, lon):
        if not self.available():
            return None
        query = " ".join(x for x in (name, city, country) if x)
        params = {"query": query, "type": "lodging", "key": self.api_key}
        if lat is not None and lon is not None:
            params["location"] = f"{lat},{lon}"
            params["radius"] = 5000
        time.sleep(self.delay)
        resp = self.session.get(self.TEXT_SEARCH, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status")
        if status == "ZERO_RESULTS":
            return {"match_status": "not_found"}
        if status != "OK":
            raise RuntimeError(f"google places: {status} {data.get('error_message', '')}")

        results = data.get("results", [])
        best = _nearest(results, lat, lon) if (lat is not None) else results[0]
        return {
            "rating": best.get("rating"),
            "rating_scale": 5,
            "reviews_count": best.get("user_ratings_total"),
            "summary": None,
            "external_id": best.get("place_id"),
            "url": (f"https://www.google.com/maps/place/?q=place_id:{best['place_id']}"
                    if best.get("place_id") else None),
            "matched_name": best.get("name"),
            "match_status": "ok" if len(results) == 1 or lat is not None else "ambiguous",
        }


def _haversine(lat1, lon1, lat2, lon2) -> float:
    from math import radians, sin, cos, asin, sqrt
    dlat = radians(lat2 - lat1); dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * 6371 * asin(sqrt(a))


def _nearest(results: list[dict], lat: float, lon: float) -> dict:
    def dist(r):
        loc = (r.get("geometry") or {}).get("location") or {}
        if "lat" not in loc:
            return float("inf")
        return _haversine(lat, lon, loc["lat"], loc["lng"])
    return min(results, key=dist)


def get_provider(name: str = "google") -> ReviewProvider:
    if name == "google":
        return GooglePlacesReviews()
    raise ValueError(f"unknown review provider: {name}")
