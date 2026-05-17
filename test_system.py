"""
End-to-end test for the injector availability service.

Talks to a local server at http://localhost:8000. Registers two injectors,
adds two fake appointments, then runs several availability queries and
prints results in plain English.

Usage:
    1. Make sure uvicorn is running:  uvicorn main:app --reload
    2. In another terminal:           python test_system.py
"""

import sys
from datetime import datetime, timedelta

import requests

BASE_URL = "http://localhost:8000"


# ─── Pretty-print helpers ────────────────────────────────────

def banner(text: str) -> None:
    print()
    print("=" * 70)
    print(f"  {text}")
    print("=" * 70)


def step(text: str) -> None:
    print(f"\n→ {text}")


def show(resp: requests.Response):
    if resp.status_code >= 400:
        print(f"  ❌ {resp.status_code}: {resp.text}")
        return None
    try:
        return resp.json()
    except Exception:
        print(f"  Raw: {resp.text}")
        return None


def post(path: str, body: dict) -> requests.Response:
    return requests.post(f"{BASE_URL}{path}", json=body)


def get(path: str) -> requests.Response:
    return requests.get(f"{BASE_URL}{path}")


# ─── Test scenario ───────────────────────────────────────────

def main() -> None:
    banner("Health check")
    try:
        r = get("/docs")
        if r.status_code != 200:
            print(f"  Server returned {r.status_code} — is uvicorn running?")
            sys.exit(1)
        print("  ✓ Server is reachable")
    except requests.ConnectionError:
        print("  ❌ Can't connect to http://localhost:8000")
        print("     Start uvicorn first: uvicorn main:app --reload")
        sys.exit(1)

    # ── Inject two injectors ──────────────────────────────────
    banner("Registering injectors")

    injectors = [
        {
            "id": "inj_001",
            "name": "Dr. Smith",
            "home_base_address": "1100 Wilson Blvd, Arlington, VA 22209",
            "working_hours": {
                "0": [9, 19], "1": [9, 19], "2": [9, 19],
                "3": [9, 19], "4": [9, 17],
            },
        },
        {
            "id": "inj_002",
            "name": "Dr. Lee",
            "home_base_address": "200 Massachusetts Ave NW, Washington, DC 20001",
            "working_hours": {
                "0": [10, 18], "1": [10, 18], "2": [10, 18],
                "3": [10, 18], "4": [10, 18],
            },
        },
    ]

    for inj in injectors:
        step(f"Register {inj['name']}")
        result = show(post("/injectors", inj))
        if result and result.get("ok"):
            print(f"  ✓ {inj['name']} based at {inj['home_base_address']}")

    # ── Add fake appointments (always use tomorrow's date) ────
    banner("Adding fake appointments")

    tomorrow = (datetime.now() + timedelta(days=1)).date().isoformat()

    appointments = [
        {
            "appointment": {
                "id": "appt_fake_001",
                "typeName": "Botox",
                "statusName": "Confirmed",
                "startLocalDateTime": f"{tomorrow}T10:00:00",
                "endLocalDateTime": f"{tomorrow}T10:30:00",
            },
            "contact": {"serviceAddress": "8000 Towers Crescent Dr, Vienna, VA 22182"},
            "employee": {"id": "inj_001"},
        },
        {
            "appointment": {
                "id": "appt_fake_002",
                "typeName": "Lip Filler",
                "statusName": "Confirmed",
                "startLocalDateTime": f"{tomorrow}T12:00:00",
                "endLocalDateTime": f"{tomorrow}T12:45:00",
            },
            "contact": {"serviceAddress": "7101 Wisconsin Ave, Bethesda, MD 20814"},
            "employee": {"id": "inj_002"},
        },
    ]

    for appt in appointments:
        a = appt["appointment"]
        step(f"{a['typeName']} for {appt['employee']['id']} at {a['startLocalDateTime']}")
        result = show(post("/webhooks/podium/appointment", appt))
        if result:
            print(f"  ✓ {result.get('action', 'ok')} — {appt['contact']['serviceAddress']}")

    # ── Run availability queries ──────────────────────────────
    banner("Running availability queries")

    queries = [
        {
            "title": "Botox in Clarendon, can start any time after 11:00 AM",
            "body": {
                "client_address": "2900 Clarendon Blvd, Arlington, VA 22201",
                "procedure_type": "botox",
                "not_before": f"{tomorrow}T11:00:00",
            },
        },
        {
            "title": "Lip filler downtown DC, after 2:00 PM",
            "body": {
                "client_address": "1455 Pennsylvania Ave NW, Washington, DC 20004",
                "procedure_type": "lip_filler",
                "not_before": f"{tomorrow}T14:00:00",
            },
        },
        {
            "title": "Consultation way out in Reston, after 1:00 PM",
            "body": {
                "client_address": "11900 Sunrise Valley Dr, Reston, VA 20191",
                "procedure_type": "consultation",
                "not_before": f"{tomorrow}T13:00:00",
            },
        },
    ]

    for query in queries:
        step(query["title"])
        print(f"  Asking: {query['body']['procedure_type']} at {query['body']['client_address']}")
        results = show(post("/availability", query["body"]))
        if not results:
            continue
        if not isinstance(results, list) or len(results) == 0:
            print("  (no availability returned)")
            continue
        for i, slot in enumerate(results, 1):
            start = datetime.fromisoformat(slot["earliest_start"])
            print(f"  #{i}  {slot['injector_name']:12} → {start:%a %I:%M %p}")
            print(f"      coming from: {slot['coming_from']}")
            print(f"      travel time: {slot['travel_minutes_from_previous']} min")
            print(f"      notes:       {slot['notes']}")

    banner("Done")
    print("If the times and travel directions look right, your system works.")
    print("Send the output to debug anything that looks off.")
    print()


if __name__ == "__main__":
    main()
