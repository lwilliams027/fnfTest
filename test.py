"""
Core availability engine.

Given an injector's existing schedule, a procedure type, and a prospective
client's address, computes the earliest moment the injector can begin the
new procedure — accounting for travel time, setup, and cleanup buffers.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Protocol


# ─────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────
@dataclass
class ProcedureType:
    name: str
    service_minutes: int
    setup_buffer_minutes: int
    cleanup_buffer_minutes: int


@dataclass
class Injector:
    id: str
    name: str

    # Where this injector starts the day from
    home_base_address: Optional[str] = None
    home_base_lat: Optional[float] = None
    home_base_lng: Optional[float] = None

    # Working hours per weekday:
    # {0: (9, 17)} means Monday 9 AM → 5 PM
    working_hours: Optional[dict[int, tuple[int, int]]] = None


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


# ─────────────────────────────────────────────────────────────
# Routing protocol
# ─────────────────────────────────────────────────────────────
class RoutingService(Protocol):
    """
    Anything implementing this interface can provide routing data:
    Google Maps, Mapbox, OpenRouteService, or a test mock.
    """

    def travel_minutes(
        self,
        origin: tuple[float, float],
        dest: tuple[float, float],
        departure_time: datetime,
    ) -> int:
        ...


# ─────────────────────────────────────────────────────────────
# Core algorithm
# ─────────────────────────────────────────────────────────────
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
    Find the earliest time `injector` can START `procedure`
    at the new client location on or after `not_before`.

    Tries every plausible slot:
      1. Before first appointment (departing from home base)
      2. Between appointments
      3. After final appointment

    Returns:
        AvailabilitySlot | None
    """

    horizon = not_before + timedelta(days=lookahead_days)

    appts = sorted(
        [
            a
            for a in appointments
            if a.end >= not_before and a.start <= horizon
        ],
        key=lambda a: a.start,
    )

    candidates: list[AvailabilitySlot] = []

    # ─────────────────────────────────────────────────────────
    # Candidate 1:
    # Depart from home base before first appointment
    # ─────────────────────────────────────────────────────────
    if (
        injector.home_base_lat is not None
        and injector.home_base_lng is not None
    ):
        departure = not_before

        travel = routing.travel_minutes(
            origin=(injector.home_base_lat, injector.home_base_lng),
            dest=(new_lat, new_lng),
            departure_time=departure,
        )

        start = departure + timedelta(
            minutes=travel + procedure.setup_buffer_minutes
        )

        start = _push_to_working_hours(start, injector)

        if (
            not appts
            or _fits_before(
                start=start,
                procedure=procedure,
                next_appt=appts[0],
                new_lat=new_lat,
                new_lng=new_lng,
                routing=routing,
            )
        ):
            candidates.append(
                AvailabilitySlot(
                    injector_id=injector.id,
                    injector_name=injector.name,
                    earliest_start=start,
                    travel_minutes_to_client=travel,
                    coming_from_address=(
                        injector.home_base_address or "home base"
                    ),
                    notes="Departing from home base",
                )
            )

    # ─────────────────────────────────────────────────────────
    # Candidate 2 & 3:
    # After each existing appointment
    # ─────────────────────────────────────────────────────────
    for i, appt in enumerate(appts):
        depart = appt.end + timedelta(
            minutes=procedure.cleanup_buffer_minutes
        )

        travel = routing.travel_minutes(
            origin=(appt.service_lat, appt.service_lng),
            dest=(new_lat, new_lng),
            departure_time=depart,
        )

        start = max(
            depart + timedelta(
                minutes=travel + procedure.setup_buffer_minutes
            ),
            not_before,
        )

        start = _push_to_working_hours(start, injector)

        next_appt = appts[i + 1] if i + 1 < len(appts) else None

        # Ensure we can still make the next appointment
        if next_appt and not _fits_before(
            start=start,
            procedure=procedure,
            next_appt=next_appt,
            new_lat=new_lat,
            new_lng=new_lng,
            routing=routing,
        ):
            continue

        candidates.append(
            AvailabilitySlot(
                injector_id=injector.id,
                injector_name=injector.name,
                earliest_start=start,
                travel_minutes_to_client=travel,
                coming_from_address=appt.service_address,
                notes=(
                    f"After {appt.procedure_type} "
                    f"ending {appt.end:%a %I:%M %p}"
                ),
            )
        )

    if not candidates:
        return None

    return min(candidates, key=lambda s: s.earliest_start)


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
def _fits_before(
    start: datetime,
    procedure: ProcedureType,
    next_appt: Appointment,
    new_lat: float,
    new_lng: float,
    routing: RoutingService,
) -> bool:
    """
    Determine whether the new procedure can finish AND
    still travel to the next appointment in time.
    """

    finish = start + timedelta(
        minutes=(
            procedure.service_minutes
            + procedure.cleanup_buffer_minutes
        )
    )

    travel = routing.travel_minutes(
        origin=(new_lat, new_lng),
        dest=(next_appt.service_lat, next_appt.service_lng),
        departure_time=finish,
    )

    arrival = finish + timedelta(minutes=travel)

    return arrival <= next_appt.start


def _push_to_working_hours(
    t: datetime,
    injector: Injector,
) -> datetime:
    """
    If `t` falls outside the injector's working hours,
    advance to the next valid working time.
    """

    if not injector.working_hours:
        return t

    # Try up to 14 days ahead
    for _ in range(14):
        hours = injector.working_hours.get(t.weekday())

        if hours:
            start_h, end_h = hours

            day_start = t.replace(
                hour=start_h,
                minute=0,
                second=0,
                microsecond=0,
            )

            day_end = t.replace(
                hour=end_h,
                minute=0,
                second=0,
                microsecond=0,
            )

            if t < day_start:
                return day_start

            if t <= day_end:
                return t

        # Move to next day
        t = (t + timedelta(days=1)).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )

    return t