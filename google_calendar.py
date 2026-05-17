"""
Google Calendar integration.

Uses a service account to read events from each injector's calendar. This
means appointments live in Google Calendar — Podium isn't required.

Setup:
  1. In your Google Cloud project, enable the Google Calendar API.
  2. Create a service account, generate a JSON key, download it.
  3. Set GOOGLE_SERVICE_ACCOUNT_JSON env var to the JSON contents.
  4. Each injector shares their Google Calendar with the service account's
     email (with "See all event details" permission).
  5. Register each injector with their google_calendar_id.

Event format expectations (what your injectors should put in their calendar):
  - Title: include the procedure name. e.g. "Botox - Sarah Johnson"
  - Location: full client home address. e.g. "8000 Towers Crescent Dr, Vienna VA"
  - Start/End: actual appointment times
"""

import json
from datetime import datetime, timezone
from typing import Optional, Union

from google.oauth2 import service_account
from googleapiclient.discovery import build


SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


class GoogleCalendarClient:
    """Reads events from any calendar shared with the service account."""

    def __init__(self, service_account_json: Union[str, dict]):
        info = (
            json.loads(service_account_json)
            if isinstance(service_account_json, str)
            else service_account_json
        )
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=SCOPES,
        )
        self.service = build(
            "calendar", "v3", credentials=creds, cache_discovery=False,
        )
        # Surface the service account's email so we can show it to users
        self.service_account_email = info.get("client_email", "unknown")

    def list_events(
        self,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
        max_results: int = 250,
    ) -> list[dict]:
        """Returns a flat list of events between time_min and time_max."""
        # Google requires RFC3339 timestamps with timezone
        if time_min.tzinfo is None:
            time_min = time_min.replace(tzinfo=timezone.utc)
        if time_max.tzinfo is None:
            time_max = time_max.replace(tzinfo=timezone.utc)

        result = self.service.events().list(
            calendarId=calendar_id,
            timeMin=time_min.isoformat(),
            timeMax=time_max.isoformat(),
            singleEvents=True,           # expand recurring events
            orderBy="startTime",
            maxResults=max_results,
        ).execute()
        return result.get("items", [])


# ─── Event parsing helpers ──────────────────────────────────────

# Each procedure has a list of keywords we look for in the event title.
# Order matters — more specific phrases should appear first.
PROCEDURE_KEYWORDS: list[tuple[str, list[str]]] = [
    ("lip_filler",     ["lip filler", "lip-filler", "lip enhancement", "lips"]),
    ("cheek_filler",   ["cheek filler", "cheek-filler", "midface", "cheek"]),
    ("jawline_filler", ["jawline filler", "jaw filler", "jaw-filler", "jawline", "jaw"]),
    ("botox",          ["botox", "bo tox", "tox"]),
    ("consultation",   ["consult", "consultation", "intake"]),
]


def parse_procedure_from_title(title: str) -> str:
    """Best-effort detection of procedure type from an event title."""
    lower = (title or "").lower()
    for proc_key, keywords in PROCEDURE_KEYWORDS:
        for kw in keywords:
            if kw in lower:
                return proc_key
    return "consultation"  # default fallback


def parse_event_address(event: dict) -> Optional[str]:
    """Extract the client's service address from an event."""
    # Primary: the event's Location field
    location = (event.get("location") or "").strip()
    if location:
        return location

    # Fallback: pull from description if labeled
    description = event.get("description") or ""
    for line in description.split("\n"):
        for marker in ("address:", "location:", "client address:", "client:"):
            if line.lower().strip().startswith(marker):
                return line.split(":", 1)[1].strip()
    return None


def parse_event_datetime(dt_dict: dict) -> datetime:
    """Parse Google's start/end datetime dict. Returns timezone-aware datetime."""
    if "dateTime" in dt_dict:
        return datetime.fromisoformat(dt_dict["dateTime"].replace("Z", "+00:00"))
    if "date" in dt_dict:
        # All-day event — start at midnight UTC
        return datetime.fromisoformat(dt_dict["date"] + "T00:00:00+00:00")
    raise ValueError(f"Cannot parse datetime from {dt_dict}")
