# Injector Availability Service

Pulls appointment data from Podium and calculates the earliest each injector can arrive at a new client's house — factoring in the procedure they just finished, where they're coming from, and live traffic.

For mobile concierge cosmetic injection businesses where injectors travel between clients.

## Files in this project

- **`FULL_SETUP.md`** — End-to-end walkthrough from a fresh machine. Start here if you've set up nothing yet.
- `availability.py` — Core scheduling algorithm + data models. No external deps.
- `podium_client.py` — Podium v4 API wrapper with automatic token refresh.
- `google_maps.py` — Geocoding + traffic-aware Distance Matrix.
- `main.py` — FastAPI app: availability endpoint, webhook ingestion, OAuth helper routes.
- `.env.example` — Template for your config. Copy to `.env` and fill in.
- `requirements.txt`

## Quick start (already have your accounts and keys)

If you've already got a Podium developer account, OAuth app credentials, a Google Maps API key, and ngrok set up, this is the short version:

```bash
# 1. Install
python3 -m venv venv
source venv/bin/activate          # macOS/Linux
# venv\Scripts\activate            # Windows
pip install -r requirements.txt

# 2. Configure
cp .env.example .env              # then edit .env with your values

# 3. Run
uvicorn main:app --reload --port 8000

# 4. In another terminal, expose with ngrok
ngrok http 8000
```

Then visit `http://localhost:8000/docs` for the interactive API UI.

**First-time only**: visit `https://YOUR-NGROK-URL.ngrok-free.app/oauth/start` to run the Podium OAuth flow. The callback page shows your refresh token — copy it into `.env` and restart uvicorn.

If you haven't set up any of those things yet, follow **`FULL_SETUP.md`** instead.

## Configuration

All config goes in a `.env` file in this folder:

```ini
GOOGLE_MAPS_API_KEY=AIza...
PODIUM_CLIENT_ID=...
PODIUM_CLIENT_SECRET=...
PODIUM_REDIRECT_URI=https://your-ngrok-url.ngrok-free.app
PODIUM_REFRESH_TOKEN=...
```

Never commit `.env` — add it to your `.gitignore`. Use `.env.example` as the template you commit.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/oauth/start` | Kick off Podium OAuth — visit in browser to get a refresh token |
| GET | `/oauth/callback` | Handles the OAuth redirect (don't call directly) |
| POST | `/injectors` | Register an injector with name, home base, working hours |
| GET | `/injectors` | List registered injectors |
| POST | `/availability` | Find earliest slot per injector for a prospective client |
| GET | `/appointments` | List cached appointments |
| POST | `/webhooks/podium/appointment` | Ingest appointment events from Podium |
| POST | `/sync` | Pull appointments via Podium API (requires beta access) |

Visit `/docs` for the full interactive UI.

## How the math works

For each injector, the earliest they can start a new procedure at address X equals:

```
prev_appointment.end + cleanup_buffer + travel(prev_address → X, departing at that moment)
```

The engine also tries inserting into gaps between existing appointments (it'll only do so if the injector can finish + travel to the next one in time) and starting from the injector's home base if they have no prior commitment that day.

## Two phases of Podium integration

**Phase 1 — Webhook ingestion (works today):**
Configure a Podium Data Feed to POST appointment events to `/webhooks/podium/appointment`. No special API access needed. The Podium scopes you need are `read_contacts`, `read_locations`, and `read_users`.

**Phase 2 — Direct API sync (beta only):**
Podium's Appointments API is a beta product. If your account has beta access, hit `POST /sync` to pull appointments directly. Otherwise stick with webhooks.

## Key decisions to make before going live

1. **Where does the client's home address live in Podium?** A custom field called `service_address` on the contact is cleanest. Update the lookup in `podium_appointment_webhook()` to match your choice.
2. **Procedure durations.** Tune the `PROCEDURES` dict in `main.py` to your actual times — the defaults are placeholders.
3. **Cleanup vs setup buffer.** Cleanup is fixed per procedure (time to pack up); setup is how early the injector wants to arrive at the next client.
4. **Storage.** In-memory dicts are fine for prototyping. Move to Postgres before deploying.
5. **Time zones.** Datetimes are naive in this starter. If your injectors span time zones, switch to timezone-aware throughout.

## Production checklist

- Move `INJECTORS` and `APPOINTMENTS` from in-memory dicts to Postgres
- Verify Podium webhook signatures (Podium signs payloads — don't trust unsigned ones)
- Put the service behind HTTPS with a real domain (replace ngrok)
- Restrict the Google Maps API key to your production server's IP
- Add request logging on `/availability` so you can debug what dispatchers ask
- Cache Distance Matrix results — many routes repeat and they cost real money
- Add a dispatcher UI on top of `/availability`, or wire it into your CRM

## Troubleshooting

See the Troubleshooting section at the bottom of `FULL_SETUP.md`.
