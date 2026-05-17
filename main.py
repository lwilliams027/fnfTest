"""
FastAPI app: availability lookups + Podium webhook ingestion.

Endpoints:
  POST /availability                 → ranked next-available slots per injector
  POST /webhooks/podium/appointment  → ingest appointment events from Podium
  POST /sync                         → manually poll Podium (beta API only)
  GET  /injectors                    → list configured injectors
  GET  /appointments                 → list cached appointments
  POST /injectors                    → register an injector

Run locally:
  uvicorn main:app --reload
"""

import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Load env vars from a .env file in this folder (works on Windows, Mac, Linux)
load_dotenv()

from availability import (
    Appointment,
    AvailabilitySlot,
    Injector,
    ProcedureType,
    all_available_slots,
    earliest_availability,
)
from google_maps import GoogleMapsClient
from podium_client import PodiumClient

# Google Calendar via OAuth (per-injector). Optional — only loads if libs installed.
try:
    from google_calendar import (
        GoogleCalendarClient,
        make_oauth_flow,
        parse_event_address,
        parse_event_datetime,
        parse_procedure_from_title,
    )
    GOOGLE_CALENDAR_AVAILABLE = True
except ImportError:
    GOOGLE_CALENDAR_AVAILABLE = False


# ─── Procedure catalog ──────────────────────────────────────────
# Tune these for your business. service_minutes is active procedure time;
# setup/cleanup are the injector's prep + pack-up time at the client's home.

PROCEDURES: dict[str, ProcedureType] = {
    "botox":          ProcedureType("Botox",           30, 10, 10),
    "lip_filler":     ProcedureType("Lip Filler",      45, 10, 15),
    "cheek_filler":   ProcedureType("Cheek Filler",    60, 10, 15),
    "jawline_filler": ProcedureType("Jawline Filler",  60, 10, 15),
    "consultation":   ProcedureType("Consultation",    30,  5,  5),
}

# ─── In-memory stores ───────────────────────────────────────────
# Swap for Postgres in production.

INJECTORS: dict[str, Injector] = {}
APPOINTMENTS: dict[str, Appointment] = {}
BOOKINGS: dict[str, dict] = {}  # customer booking requests, pending dispatcher review

# ─── External clients ───────────────────────────────────────────

gmaps = GoogleMapsClient(api_key=os.environ.get("GOOGLE_MAPS_API_KEY", ""))

# Google Calendar OAuth config (per-injector)
GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
# Public URL where injectors will be redirected after authorizing — must match
# the redirect URI configured on the Google OAuth Client ID.
GOOGLE_OAUTH_REDIRECT_URI = (
    os.environ.get("GOOGLE_OAUTH_REDIRECT_URI")
    or (os.environ.get("PODIUM_REDIRECT_URI", "") + "/oauth/google/callback")
)

# Tracks OAuth state values mapped to injector IDs (CSRF protection + identifies caller)
_google_oauth_states: dict[str, str] = {}


def _podium() -> PodiumClient:
    return PodiumClient(
        client_id=os.environ["PODIUM_CLIENT_ID"],
        client_secret=os.environ["PODIUM_CLIENT_SECRET"],
        refresh_token=os.environ["PODIUM_REFRESH_TOKEN"],
    )


# ─── API schemas ────────────────────────────────────────────────

class AvailabilityRequest(BaseModel):
    client_address: str
    procedure_type: str
    not_before: datetime | None = None
    lookahead_days: int = 14


class AvailabilityResponse(BaseModel):
    injector_id: str
    injector_name: str
    earliest_start: datetime
    travel_minutes_from_previous: int
    coming_from: str
    notes: str


class RegisterInjectorRequest(BaseModel):
    id: str
    name: str
    home_base_address: str | None = None
    working_hours: dict[int, tuple[int, int]] | None = None  # weekday → (start_h, end_h)
    google_calendar_id: str | None = None  # for Google Calendar sync


# ─── App ────────────────────────────────────────────────────────

app = FastAPI(title="Injector Availability Service")


@app.post("/availability", response_model=list[AvailabilityResponse])
def get_availability(req: AvailabilityRequest):
    if req.procedure_type not in PROCEDURES:
        raise HTTPException(400, f"Unknown procedure: {req.procedure_type}")
    if not INJECTORS:
        raise HTTPException(400, "No injectors registered yet")

    procedure = PROCEDURES[req.procedure_type]
    not_before = req.not_before or datetime.now()
    lat, lng = gmaps.geocode(req.client_address)

    results: list[AvailabilityResponse] = []
    for injector in INJECTORS.values():
        injector_appts = [a for a in APPOINTMENTS.values() if a.injector_id == injector.id]
        slot = earliest_availability(
            injector=injector,
            appointments=injector_appts,
            procedure=procedure,
            new_address=req.client_address,
            new_lat=lat,
            new_lng=lng,
            not_before=not_before,
            routing=gmaps,
            lookahead_days=req.lookahead_days,
        )
        if slot:
            results.append(AvailabilityResponse(
                injector_id=slot.injector_id,
                injector_name=slot.injector_name,
                earliest_start=slot.earliest_start,
                travel_minutes_from_previous=slot.travel_minutes_to_client,
                coming_from=slot.coming_from_address,
                notes=slot.notes,
            ))

    results.sort(key=lambda r: r.earliest_start)
    return results


@app.post("/injectors")
def register_injector(req: RegisterInjectorRequest):
    lat, lng = (None, None)
    if req.home_base_address:
        lat, lng = gmaps.geocode(req.home_base_address)
    INJECTORS[req.id] = Injector(
        id=req.id,
        name=req.name,
        home_base_address=req.home_base_address,
        home_base_lat=lat,
        home_base_lng=lng,
        working_hours=req.working_hours,
        google_calendar_id=req.google_calendar_id,
    )
    return {"ok": True, "injector_id": req.id}


@app.get("/injectors")
def list_injectors():
    return list(INJECTORS.values())


@app.get("/appointments")
def list_appointments():
    return sorted(APPOINTMENTS.values(), key=lambda a: a.start)


# ─── Podium OAuth onboarding (one-time, to get a refresh token) ─

# Set this to your current ngrok URL, e.g. https://abc123.ngrok-free.app
# It must also be registered as the Redirect URI in your Podium app's OAuth settings.
OAUTH_REDIRECT_URI = os.environ.get("PODIUM_REDIRECT_URI", "")

# The scopes your app needs. Add or remove based on what you'll actually call.
OAUTH_SCOPES = "read_contacts read_locations read_users"

# Tracks state values we've issued, to prevent CSRF on the callback.
_oauth_states: set[str] = set()


@app.get("/oauth/start")
def oauth_start():
    """Kick off the Podium OAuth flow. Visit this in your browser."""
    client_id = os.environ.get("PODIUM_CLIENT_ID")
    if not client_id:
        raise HTTPException(500, "PODIUM_CLIENT_ID env var not set")
    if not OAUTH_REDIRECT_URI:
        raise HTTPException(500, "PODIUM_REDIRECT_URI env var not set")

    state = secrets.token_urlsafe(16)
    _oauth_states.add(state)

    params = {
        "client_id": client_id,
        "redirect_uri": f"{OAUTH_REDIRECT_URI}/oauth/callback",
        "scope": OAUTH_SCOPES,
        "state": state,
    }
    return RedirectResponse(
        f"https://api.podium.com/oauth/authorize?{urlencode(params)}"
    )


@app.get("/oauth/callback", response_class=HTMLResponse)
def oauth_callback(request: Request):
    """
    Podium redirects here after the user approves your app.
    We swap the `code` for an access_token + refresh_token, then show them.
    Copy the refresh_token into your PODIUM_REFRESH_TOKEN env var.
    """
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code:
        return HTMLResponse("<h1>Missing code</h1>", status_code=400)
    if state not in _oauth_states:
        return HTMLResponse("<h1>Invalid state (CSRF check failed)</h1>", status_code=400)
    _oauth_states.discard(state)

    resp = requests.post(
        "https://api.podium.com/oauth/token",
        json={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": f"{OAUTH_REDIRECT_URI}/oauth/callback",
            "client_id": os.environ["PODIUM_CLIENT_ID"],
            "client_secret": os.environ["PODIUM_CLIENT_SECRET"],
        },
        timeout=15,
    )
    if resp.status_code != 200:
        return HTMLResponse(
            f"<h1>Token exchange failed</h1><pre>{resp.text}</pre>",
            status_code=resp.status_code,
        )
    data = resp.json()
    refresh = data.get("refresh_token", "(not returned)")
    access = data.get("access_token", "(not returned)")

    return HTMLResponse(f"""
    <html><body style="font-family: system-ui; max-width: 700px; margin: 40px auto;">
      <h1>✅ Connected to Podium</h1>
      <p>Copy the refresh token below into your environment, then restart the server:</p>
      <pre style="background:#f4f4f4;padding:12px;border-radius:6px;overflow-x:auto;">export PODIUM_REFRESH_TOKEN="{refresh}"</pre>
      <p><strong>Refresh token</strong> (long-lived, store this securely):</p>
      <pre style="background:#f4f4f4;padding:12px;border-radius:6px;word-break:break-all;">{refresh}</pre>
      <p><strong>Access token</strong> (expires in ~10 hours, only useful for quick tests):</p>
      <pre style="background:#f4f4f4;padding:12px;border-radius:6px;word-break:break-all;font-size:11px;">{access}</pre>
    </body></html>
    """)


# ─── Existing endpoints below ──────────────────────────────────

# ─── Podium ingestion ──────────────────────────────────────────

@app.post("/webhooks/podium/appointment")
def podium_appointment_webhook(payload: dict):
    """
    Ingests appointment events from Podium.

    Expected payload shape (Data Feeds format):
      {
        "employee": {"id": "...", "name": "..."},
        "contact": {"firstName": "...", "lastName": "...", "address": "..."},
        "appointment": {
          "id": "...",
          "typeName": "Botox",
          "statusName": "Confirmed",
          "startLocalDateTime": "2026-05-20T09:00:00Z",
          "endLocalDateTime":   "2026-05-20T09:30:00Z"
        }
      }

    Adjust the field extractions below to match your account's actual payload —
    Podium normalizes field names per integration during onboarding.
    """
    appt_data = payload.get("appointment", {})
    contact = payload.get("contact", {})
    employee = payload.get("employee", {})

    if not appt_data.get("id"):
        raise HTTPException(400, "Missing appointment.id")

    # ── Resolve service address. This is the critical mapping decision:
    # for mobile concierge, you need the client's HOME address, not a clinic.
    # Common patterns:
    #   - Custom "service_address" field on the contact (recommended)
    #   - contact.address if that's where your business stores it
    #   - Fetched separately from Podium via get_contact()
    service_address = (
        contact.get("serviceAddress")
        or (contact.get("customFields") or {}).get("service_address")
        or contact.get("address")
    )
    if not service_address:
        raise HTTPException(400, "No service address found on contact")

    status = (appt_data.get("statusName") or "").lower()
    if status in ("canceled", "cancelled", "no_show", "no-show"):
        APPOINTMENTS.pop(appt_data["id"], None)
        return {"ok": True, "action": "removed"}

    lat, lng = gmaps.geocode(service_address)
    appt = Appointment(
        id=appt_data["id"],
        injector_id=employee.get("id", "unknown"),
        procedure_type=_normalize_procedure(appt_data.get("typeName", "")),
        service_address=service_address,
        service_lat=lat,
        service_lng=lng,
        start=_parse_dt(appt_data["startLocalDateTime"]),
        end=_parse_dt(appt_data["endLocalDateTime"]),
    )
    APPOINTMENTS[appt.id] = appt
    return {"ok": True, "action": "upserted"}


@app.post("/sync")
def sync_from_podium(lookahead_days: int = 14):
    """
    Pull appointments directly from Podium API. Requires Appointments beta
    access on your account. If unavailable, rely on webhook ingestion instead.
    """
    podium = _podium()
    start = datetime.now()
    end = start + timedelta(days=lookahead_days)
    synced = 0

    for location in podium.list_locations():
        loc_uid = location.get("uid") or location.get("id")
        try:
            raw_appts = podium.list_appointments(loc_uid, start, end)
        except Exception as e:
            return {"ok": False, "error": f"List failed for location {loc_uid}: {e}"}
        for raw in raw_appts:
            # Reuse the webhook mapping by reshaping into the same envelope.
            envelope = {
                "appointment": raw,
                "contact": raw.get("contact", {}),
                "employee": raw.get("employee", {}),
            }
            try:
                podium_appointment_webhook(envelope)
                synced += 1
            except HTTPException:
                continue
    return {"ok": True, "synced": synced}


# ─── Helpers ────────────────────────────────────────────────────

def _parse_dt(s: str) -> datetime:
    """Parse Podium's ISO timestamps (handles trailing Z)."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _normalize_procedure(name: str) -> str:
    """Map Podium's typeName to our internal procedure key."""
    key = name.lower().strip().replace(" ", "_").replace("-", "_")
    return key if key in PROCEDURES else "consultation"


# ─── Google Calendar OAuth (per-injector) ──────────────────────

@app.get("/oauth/google/start")
def google_oauth_start(injector_id: str):
    """
    Each injector visits this URL once to authorize calendar access.
    Pass ?injector_id=their_id to associate the resulting token correctly.
    """
    if not GOOGLE_CALENDAR_AVAILABLE:
        raise HTTPException(500, "Google Calendar libraries not installed")
    if not GOOGLE_OAUTH_CLIENT_ID:
        raise HTTPException(500, "GOOGLE_OAUTH_CLIENT_ID env var not set")
    if injector_id not in INJECTORS:
        raise HTTPException(404, f"Injector '{injector_id}' is not registered. Register first via POST /injectors.")

    flow = make_oauth_flow(
        GOOGLE_OAUTH_CLIENT_ID,
        GOOGLE_OAUTH_CLIENT_SECRET,
        GOOGLE_OAUTH_REDIRECT_URI,
    )
    auth_url, state = flow.authorization_url(
        access_type="offline",   # required for a refresh token
        prompt="consent",        # force refresh token even on re-auth
        include_granted_scopes="true",
    )
    _google_oauth_states[state] = injector_id
    return RedirectResponse(auth_url)


@app.get("/oauth/google/callback", response_class=HTMLResponse)
def google_oauth_callback(request: Request):
    """Google redirects here after the injector approves."""
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code:
        return HTMLResponse("<h1>Missing authorization code</h1>", status_code=400)
    injector_id = _google_oauth_states.pop(state or "", None)
    if not injector_id:
        return HTMLResponse("<h1>Invalid state (CSRF check failed)</h1>", status_code=400)
    injector = INJECTORS.get(injector_id)
    if not injector:
        return HTMLResponse(f"<h1>Injector '{injector_id}' no longer exists</h1>", status_code=400)

    flow = make_oauth_flow(
        GOOGLE_OAUTH_CLIENT_ID,
        GOOGLE_OAUTH_CLIENT_SECRET,
        GOOGLE_OAUTH_REDIRECT_URI,
    )
    try:
        flow.fetch_token(code=code)
    except Exception as e:
        return HTMLResponse(f"<h1>Token exchange failed</h1><pre>{e}</pre>", status_code=400)

    creds = flow.credentials
    if not creds.refresh_token:
        return HTMLResponse(
            "<h1>No refresh token received</h1>"
            "<p>You may have already authorized this app. To re-authorize, "
            "visit <a href='https://myaccount.google.com/permissions'>"
            "your Google account permissions</a>, remove this app's access, "
            "then retry the link.</p>",
            status_code=400,
        )

    injector.google_refresh_token = creds.refresh_token
    if not injector.google_calendar_id:
        injector.google_calendar_id = "primary"

    return HTMLResponse(f"""
    <html><body style="font-family: system-ui; max-width: 600px; margin: 40px auto;">
      <h1>✅ {injector.name} is connected</h1>
      <p>Google Calendar access authorized. You can close this window.</p>
      <p>The dispatcher can now see your appointments in the scheduling system.</p>
    </body></html>
    """)


@app.post("/sync/google")
def sync_from_google_calendar(lookahead_days: int = 14):
    """
    Pull appointments from each registered injector's Google Calendar.

    Each injector must have:
      1. Completed the /oauth/google/start flow (so they have a refresh token)
      2. (Optional) google_calendar_id set to a specific calendar, otherwise
         defaults to their primary calendar
      3. Events formatted with the procedure name in the title and the
         client address in the Location field
    """
    if not GOOGLE_CALENDAR_AVAILABLE:
        raise HTTPException(500, "Google Calendar libraries not installed")
    if not GOOGLE_OAUTH_CLIENT_ID:
        raise HTTPException(500, "GOOGLE_OAUTH_CLIENT_ID env var not set")

    range_start = datetime.now(timezone.utc)
    range_end = range_start + timedelta(days=lookahead_days)

    synced = 0
    skipped_no_address = 0
    errors: list[str] = []

    for injector in INJECTORS.values():
        if not injector.google_refresh_token:
            continue

        try:
            client = GoogleCalendarClient(
                GOOGLE_OAUTH_CLIENT_ID,
                GOOGLE_OAUTH_CLIENT_SECRET,
                injector.google_refresh_token,
            )
            calendar_id = injector.google_calendar_id or "primary"
            events = client.list_events(calendar_id, range_start, range_end)
        except Exception as e:
            errors.append(f"{injector.name}: calendar list failed — {e}")
            continue

        for event in events:
            event_id = event.get("id")
            if not event_id:
                continue
            if event.get("status") == "cancelled":
                APPOINTMENTS.pop(event_id, None)
                continue

            address = parse_event_address(event)
            if not address:
                skipped_no_address += 1
                continue

            try:
                lat, lng = gmaps.geocode(address)
            except Exception as e:
                errors.append(f"Geocode failed for '{address}': {e}")
                continue

            try:
                start = parse_event_datetime(event["start"])
                end = parse_event_datetime(event["end"])
            except Exception as e:
                errors.append(f"Event '{event.get('summary', event_id)}' datetime parse failed: {e}")
                continue

            APPOINTMENTS[event_id] = Appointment(
                id=event_id,
                injector_id=injector.id,
                procedure_type=parse_procedure_from_title(event.get("summary", "")),
                service_address=address,
                service_lat=lat,
                service_lng=lng,
                start=start.replace(tzinfo=None),
                end=end.replace(tzinfo=None),
            )
            synced += 1

    return {
        "ok": True,
        "synced": synced,
        "skipped_no_address": skipped_no_address,
        "errors": errors,
        "authorized_injectors": [
            inj.id for inj in INJECTORS.values() if inj.google_refresh_token
        ],
    }


# ─── Customer-facing booking flow ───────────────────────────────

class SlotsRequest(BaseModel):
    client_address: str
    procedure_type: str
    days_ahead: int = 7


class SlotResponse(BaseModel):
    start_time: datetime
    end_time: datetime
    injector_id: str
    injector_name: str
    travel_minutes: int


class BookingRequest(BaseModel):
    client_name: str
    client_email: str
    client_phone: str
    client_address: str
    procedure_type: str
    injector_id: str
    start_time: datetime


@app.post("/api/slots", response_model=list[SlotResponse])
def get_slots(req: SlotsRequest):
    """Return all bookable slots for this customer across all injectors."""
    if req.procedure_type not in PROCEDURES:
        raise HTTPException(400, f"Unknown procedure: {req.procedure_type}")
    if not INJECTORS:
        raise HTTPException(400, "No injectors available")

    procedure = PROCEDURES[req.procedure_type]
    range_start = datetime.now()
    range_end = range_start + timedelta(days=req.days_ahead)
    lat, lng = gmaps.geocode(req.client_address)

    # Pair injectors with their appointments
    injectors_with_appts = []
    for inj in INJECTORS.values():
        appts = [a for a in APPOINTMENTS.values() if a.injector_id == inj.id]
        injectors_with_appts.append((inj, appts))

    slots = all_available_slots(
        injectors_with_appts=injectors_with_appts,
        procedure=procedure,
        new_address=req.client_address,
        new_lat=lat,
        new_lng=lng,
        range_start=range_start,
        range_end=range_end,
        routing=gmaps,
    )

    return [
        SlotResponse(
            start_time=s.earliest_start,
            end_time=s.earliest_start + timedelta(minutes=procedure.service_minutes),
            injector_id=s.injector_id,
            injector_name=s.injector_name,
            travel_minutes=s.travel_minutes_to_client,
        )
        for s in slots
    ]


@app.post("/api/book")
def book_appointment(req: BookingRequest):
    """Capture a customer's booking request. Pending dispatcher confirmation."""
    booking_id = secrets.token_urlsafe(8)
    BOOKINGS[booking_id] = {
        "id": booking_id,
        "client_name": req.client_name,
        "client_email": req.client_email,
        "client_phone": req.client_phone,
        "client_address": req.client_address,
        "procedure_type": req.procedure_type,
        "injector_id": req.injector_id,
        "start_time": req.start_time.isoformat(),
        "status": "pending",
        "created_at": datetime.now().isoformat(),
    }
    return {"ok": True, "booking_id": booking_id}


@app.get("/api/bookings")
def list_bookings():
    """For the dispatcher: see all pending bookings."""
    return sorted(BOOKINGS.values(), key=lambda b: b["created_at"], reverse=True)


@app.get("/book", response_class=HTMLResponse)
def booking_page():
    """Serve the customer-facing booking page."""
    return FileResponse("static/book.html")


# Mount /static for any CSS/JS/image assets we add later
import os as _os
if _os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")
