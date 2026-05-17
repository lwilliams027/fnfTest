"""
Google Calendar integration via OAuth (per-injector).

Each injector authorizes the app once via the OAuth flow. Their refresh
token is stored on the Injector record. The server uses that token to
read their calendar on demand.

Compared to service accounts:
  - More secure: each user grants explicit consent
  - No org policies to fight
  - Revocable per-user
  - Works on any Google account (personal or Workspace)
"""

from datetime import datetime, timezone
from typing import Optional

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build


SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"


def make_oauth_flow(client_id: str, client_secret: str, redirect_uri: str) -> Flow:
    """Build a Google OAuth flow object for the authorization journey."""
    return Flow.from_client_config(
        {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": AUTH_URI,
                "token_uri": TOKEN_URI,
                "redirect_uris": [redirect_uri],
            }
        },
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )


class GoogleCalendarClient:
    """Per-user OAuth-authenticated Calendar client."""

    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri=TOKEN_URI,
            client_id=client_id,
            client_secret=client_secret,
            scopes=SCOPES,
        )
        self.service = build(
            "calendar", "v3", credentials=creds, cache_discovery=False,
        )

    def list_events(
        self,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
        max_results: int = 250,
    ) -> list[dict]:
        """Returns a flat list of events between time_min and time_max."""
        if time_min.tzinfo is None:
            time_min = time_min.replace(tzinfo=timezone.utc)
        if time_max.tzinfo is None:
            time_max = time_max.replace(tzinfo=timezone.utc)

        result = self.service.events().list(
            calendarId=calendar_id,
            timeMin=time_min.isoformat(),
            timeMax=time_max.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=max_results,
        ).execute()
        return result.get("items", [])

    def get_user_info(self) -> dict:
        """Get the authenticated user's basic info (email)."""
        # We didn't request the userinfo scope, so just return primary calendar
        result = self.service.calendars().get(calendarId="primary").execute()
        return {"primary_calendar_id": result.get("id")}


# ─── Event parsing helpers ──────────────────────────────────────

# Each procedure has a list of keywords we look for in the event title.
# Order matters — more specific phrases first.
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
    return "consultation"


def parse_event_address(event: dict) -> Optional[str]:
    """Extract the client's service address from an event."""
    location = (event.get("location") or "").strip()
    if location:
        return location
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
        return datetime.fromisoformat(dt_dict["date"] + "T00:00:00+00:00")
    raise ValueError(f"Cannot parse datetime from {dt_dict}")
