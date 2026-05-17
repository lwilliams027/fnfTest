"""
Podium API client.

Wraps Podium v4 REST API. Uses OAuth 2.0 — generate a refresh token once
via the OAuth flow, then this client exchanges it for short-lived access
tokens automatically.

IMPORTANT: Podium Appointments is a beta product and may not be enabled
on your account. If `list_appointments` returns 404/403, contact Podium
support to join the Appointments beta. Webhook-based ingestion (see
main.py) is the recommended fallback.

Docs: https://docs.podium.com
"""

import time
from datetime import datetime
from typing import Optional

import requests


BASE_URL = "https://api.podium.com/v4"
AUTHORIZE_URL = "https://api.podium.com/oauth/authorize"
TOKEN_URL = "https://api.podium.com/oauth/token"


class PodiumClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self._access_token: Optional[str] = None
        self._expires_at: float = 0
        self.session = requests.Session()

    # ─── Auth ────────────────────────────────────────────────────

    def _ensure_token(self):
        """Refresh the access token if expired or absent."""
        if self._access_token and time.time() < self._expires_at - 60:
            return
        resp = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["access_token"]
        self._expires_at = time.time() + data.get("expires_in", 3600)
        # Podium may rotate the refresh token; store it if returned
        if "refresh_token" in data:
            self.refresh_token = data["refresh_token"]

    def _headers(self) -> dict[str, str]:
        self._ensure_token()
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

    # ─── Endpoints ───────────────────────────────────────────────

    def list_locations(self) -> list[dict]:
        resp = self.session.get(
            f"{BASE_URL}/locations",
            headers=self._headers(),
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("data", [])

    def list_appointments(
        self,
        location_uid: str,
        start: datetime,
        end: datetime,
    ) -> list[dict]:
        """
        List appointments in a date range for a location.

        Beta endpoint — adjust parameter names if your account's API differs.
        """
        params = {
            "locationUid": location_uid,
            "startsAfter": start.isoformat(),
            "startsBefore": end.isoformat(),
        }
        resp = self.session.get(
            f"{BASE_URL}/appointments",
            headers=self._headers(),
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("data", [])

    def get_contact(self, contact_uid: str) -> dict:
        """Fetch a contact — useful for resolving the client's service address."""
        resp = self.session.get(
            f"{BASE_URL}/contacts/{contact_uid}",
            headers=self._headers(),
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
