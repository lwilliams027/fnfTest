"""
Core availability engine.

Given an injector's existing schedule, a procedure type, and a prospective
client's address, computes the earliest moment the injector can begin the
new procedure — accounting for travel time, setup, and cleanup buffers.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Protocol


# ─── Models ──────────────────────────────────────────────────────

@dataclass
class ProcedureType:
    name: str
    service_minutes: int            # active procedure time
    setup_buffer_minutes: int       # injector arrives this much early to prep
    cleanup_buffer_minutes: int     # time to pack up before leaving


@dataclass
class Injector:
    id: str
    name: str
    # Where this injector starts the day from (home, clinic, etc.)
    home_base_address: Optional[str] = None
    home_base_lat: Optional[float] = None
    home_base_lng: Optional[float] = None
    # Working hours per weekday (0=Mon, 6=Sun) as (start_hour, end_hour) in 24h
    working_hours: Optional[dict[int, tuple[int, int]]] = None
    # Google Calendar ID this injector's appointments live in (default "primary")
    google_calendar_id: Optional[str] = None
    # OAuth refresh token, populated after the injector authorizes
    google_refresh_token: Optional[str] = None


@dataclass
class Appointment:
    id: str
    injector_id: str
    procedure_type: str
    service_address: str
    service_lat: float
    service_lng: float
    start: datetime
    end: datetime


@dataclass
class AvailabilitySlot:
    injector_id: str
    injector_name: str
    earliest_start: datetime
    travel_minutes_to_client: int
    coming_from_address: str
    notes: str = ""


# ─── Routing protocol ───────────────────────────────────────────
# Anything that exposes this method works (Google, Mapbox, mock for tests).

class RoutingService(Protocol):
    def travel_minutes(
        self,
        origin: tuple[float, float],
        dest: tuple[float, float],
        departure_time: datetime,
    ) -> int: ...


# ─── Core algorithm ──────────────────────────────────────────────

def earliest_availability(
    injector: Injector,
    appointments: list[Appointment],
    procedure: ProcedureType,
    new_address: str,
    new_lat: float,
    new_lng: float,
    not_before: datetime,
    routing: RoutingService,
    lookahead_days: int = 14,
) -> Optional[AvailabilitySlot]:
    """
    Find the earliest time `injector` can START `procedure` at the new client
    location, on or after `not_before`.

    Tries every plausible slot:
      1. Before their first appointment (departing from home base)
      2. Between any two existing appointments (gap insertion)
      3. After their final appointment

    Returns the soonest valid option, or None if nothing fits in the window.
    """
    horizon = not_before + timedelta(days=lookahead_days)
    appts = sorted(
        [a for a in appointments if a.end >= not_before and a.start <= horizon],
        key=lambda a: a.start,
    )

    candidates: list[AvailabilitySlot] = []

    # Candidate 1: depart from home base before first appointment
    if injector.home_base_lat is not None and injector.home_base_lng is not None:
        departure = not_before
        travel = routing.travel_minutes(
            origin=(injector.home_base_lat, injector.home_base_lng),
            dest=(new_lat, new_lng),
            departure_time=departure,
        )
        start = departure + timedelta(minutes=travel)
        start = _push_to_working_hours(start, injector)

        if not appts or _fits_before(start, procedure, appts[0], new_lat, new_lng, routing):
            candidates.append(AvailabilitySlot(
                injector_id=injector.id,
                injector_name=injector.name,
                earliest_start=start,
                travel_minutes_to_client=travel,
                coming_from_address=injector.home_base_address or "home base",
                notes="Departing from home base",
            ))

    # Candidate 2 & 3: after each existing appointment
    for i, appt in enumerate(appts):
        depart = appt.end + timedelta(minutes=procedure.cleanup_buffer_minutes)
        travel = routing.travel_minutes(
            origin=(appt.service_lat, appt.service_lng),
            dest=(new_lat, new_lng),
            departure_time=depart,
        )
        start = max(depart + timedelta(minutes=travel), not_before)
        start = _push_to_working_hours(start, injector)

        # If there's a following appointment, make sure we can finish + travel
        next_appt = appts[i + 1] if i + 1 < len(appts) else None
        if next_appt and not _fits_before(start, procedure, next_appt, new_lat, new_lng, routing):
            continue

        candidates.append(AvailabilitySlot(
            injector_id=injector.id,
            injector_name=injector.name,
            earliest_start=start,
            travel_minutes_to_client=travel,
            coming_from_address=appt.service_address,
            notes=f"After {appt.procedure_type} ending {appt.end:%a %I:%M %p}",
        ))

    if not candidates:
        return None
    return min(candidates, key=lambda s: s.earliest_start)


# ─── Slot generation for booking UI ─────────────────────────────

def all_available_slots(
    injectors_with_appts: list[tuple[Injector, list[Appointment]]],
    procedure: ProcedureType,
    new_address: str,
    new_lat: float,
    new_lng: float,
    range_start: datetime,
    range_end: datetime,
    routing: RoutingService,
    slot_interval_minutes: int = 30,
    max_slots_per_injector_per_day: int = 6,
) -> list[AvailabilitySlot]:
    """
    Find all bookable slots for `procedure` at this address, across all
    injectors, within the date range. Walks forward through time using
    earliest_availability() repeatedly to find subsequent slots.

    Returns slots sorted by start time (soonest first).
    """
    from datetime import timedelta as _td  # local alias to keep top-level clean

    all_slots: list[AvailabilitySlot] = []

    for injector, appts in injectors_with_appts:
        not_before = range_start
        slots_today: dict[str, int] = {}  # date string → count, to cap per day

        # Hard cap on total iterations for safety
        for _ in range(50):
            if not_before >= range_end:
                break

            slot = earliest_availability(
                injector=injector,
                appointments=appts,
                procedure=procedure,
                new_address=new_address,
                new_lat=new_lat,
                new_lng=new_lng,
                not_before=not_before,
                routing=routing,
                lookahead_days=max(1, (range_end.date() - not_before.date()).days + 1),
            )
            if not slot or slot.earliest_start >= range_end:
                break

            # Round up to the next slot_interval boundary for clean times
            rounded = _round_up_to_interval(slot.earliest_start, slot_interval_minutes)
            if rounded != slot.earliest_start:
                slot = earliest_availability(
                    injector=injector,
                    appointments=appts,
                    procedure=procedure,
                    new_address=new_address,
                    new_lat=new_lat,
                    new_lng=new_lng,
                    not_before=rounded,
                    routing=routing,
                    lookahead_days=max(1, (range_end.date() - rounded.date()).days + 1),
                )
                if not slot or slot.earliest_start >= range_end:
                    break

            # Day cap
            day_key = slot.earliest_start.date().isoformat()
            if slots_today.get(day_key, 0) >= max_slots_per_injector_per_day:
                # Jump to next day to keep finding slots
                next_day = (slot.earliest_start + _td(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                not_before = next_day
                continue

            all_slots.append(slot)
            slots_today[day_key] = slots_today.get(day_key, 0) + 1

            # Advance not_before past this slot
            not_before = slot.earliest_start + _td(
                minutes=procedure.service_minutes
                + procedure.cleanup_buffer_minutes
                + slot_interval_minutes
            )

    return sorted(all_slots, key=lambda s: s.earliest_start)


def _round_up_to_interval(t: datetime, interval_minutes: int) -> datetime:
    """Round `t` up to the next interval_minutes boundary."""
    base = t.replace(second=0, microsecond=0)
    minutes_past = base.minute
    remainder = minutes_past % interval_minutes
    if remainder == 0 and t.second == 0 and t.microsecond == 0:
        return base
    add_minutes = interval_minutes - remainder
    return base + timedelta(minutes=add_minutes)


# ─── Helpers ────────────────────────────────────────────────────

def _fits_before(
    start: datetime,
    procedure: ProcedureType,
    next_appt: Appointment,
    new_lat: float,
    new_lng: float,
    routing: RoutingService,
) -> bool:
    """Does the new procedure finish + travel-to-next in time?"""
    finish = start + timedelta(
        minutes=procedure.service_minutes + procedure.cleanup_buffer_minutes
    )
    travel = routing.travel_minutes(
        origin=(new_lat, new_lng),
        dest=(next_appt.service_lat, next_appt.service_lng),
        departure_time=finish,
    )
    return finish + timedelta(minutes=travel) <= next_appt.start


def _push_to_working_hours(t: datetime, injector: Injector) -> datetime:
    """If `t` falls outside the injector's working hours, advance to next valid time."""
    if not injector.working_hours:
        return t
    # Try up to 14 days forward (handles weekends + days off)
    for _ in range(14):
        hours = injector.working_hours.get(t.weekday())
        if hours:
            start_h, end_h = hours
            day_start = t.replace(hour=start_h, minute=0, second=0, microsecond=0)
            day_end = t.replace(hour=end_h, minute=0, second=0, microsecond=0)
            if t < day_start:
                return day_start
            if t <= day_end:
                return t
        # Skip to next day at midnight
        t = (t + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return t
