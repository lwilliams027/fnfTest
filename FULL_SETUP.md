# Injector Availability — Full Setup Walkthrough

End-to-end, assuming a fresh machine. Estimated time: 60–90 min, mostly waiting on account approvals.

---

## What you'll create along the way

- A Google Cloud account with two Maps APIs enabled
- A Podium developer account + an OAuth app
- An ngrok account
- A local Python environment running the service

Have a credit card handy for Google billing (you almost certainly won't be charged) and a Podium login for the business account you want to read appointments from.

---

## Part 1 — Install the tools (15 min)

### 1.1 Install Python 3.11+

Check what you have:
```bash
python3 --version
```

If it's missing or older than 3.10, install:
- **macOS**: `brew install python@3.12` (install Homebrew first from brew.sh if you don't have it)
- **Windows**: download from python.org, check "Add Python to PATH" during install
- **Linux**: `sudo apt install python3.12 python3.12-venv`

### 1.2 Install ngrok

- Go to ngrok.com → sign up (free)
- Download the binary for your OS, or `brew install ngrok` on macOS
- From the ngrok dashboard, copy your authtoken
- Run once:
  ```bash
  ngrok config add-authtoken YOUR_TOKEN_HERE
  ```

### 1.3 Get the project code

Unzip `injector_availability.zip` somewhere you'll remember. Open a terminal in that folder:
```bash
cd injector_availability
```

### 1.4 Set up the Python environment

```bash
python3 -m venv venv
source venv/bin/activate          # macOS/Linux
# venv\Scripts\activate            # Windows
pip install -r requirements.txt
```

You should now have FastAPI, uvicorn, requests, and pydantic installed in the `venv` folder. From here on, whenever you open a new terminal, run `source venv/bin/activate` first.

---

## Part 2 — Google Maps API key (15 min)

### 2.1 Create a Google Cloud project

- Go to console.cloud.google.com and sign in (use a work account if you have one)
- Top of the page, project dropdown → **New Project**
- Name it `injector-availability`, click Create
- Wait ~10 seconds, then switch into it from the project dropdown

### 2.2 Enable billing

- Left nav → **Billing** → link a billing account
- A credit card is required even for the free tier — you won't be charged at your usage level, but APIs return 403 without it

### 2.3 Enable the two APIs

- Left nav → **APIs & Services → Library**
- Search for **Geocoding API**, click Enable
- Back to Library, search for **Distance Matrix API**, click Enable
- (Distance Matrix is in legacy status as of 2025 but works fine; if you ever want to upgrade, swap to Routes API → Compute Route Matrix)

### 2.4 Create the API key

- Left nav → **APIs & Services → Credentials**
- **Create Credentials → API Key**
- Copy the key immediately

### 2.5 Restrict the key (do not skip)

Click the key you just made and:
- **Application restrictions**: choose "IP addresses" and add your laptop's public IP (Google "what's my IP")
- **API restrictions**: choose "Restrict key" and select only Geocoding API and Distance Matrix API
- Save

### 2.6 Save it as an env var

```bash
export GOOGLE_MAPS_API_KEY="paste_the_key_here"
```

Quick sanity check:
```bash
curl "https://maps.googleapis.com/maps/api/geocode/json?address=1600+Pennsylvania+Ave&key=$GOOGLE_MAPS_API_KEY"
```
You should get JSON with `"status": "OK"` and a lat/lng.

---

## Part 3 — Podium developer access (15 min + approval wait)

### 3.1 Apply for a developer account

- Go to developer.podium.com
- Sign up — Podium reviews applications, can take a few days to a few weeks for approval. **Start this first**; everything else can be done while you wait.

Once approved, you'll get a welcome email. Log in.

### 3.2 Create an OAuth app

- In the Podium developer portal: **Create App**
- Name: `Injector Availability`
- Save

### 3.3 Configure the OAuth tab

You'll come back to this after Part 4 to add the actual redirect URI. For now, set:
- **Scopes**: check `read_contacts`, `read_locations`, `read_users`
- Leave Redirect URI blank for now (we need ngrok running first)

### 3.4 Grab the credentials

- Open the **Credentials** section of your app
- Copy the **Client ID** and **Client Secret** — the secret can only be viewed once, save it somewhere safe
- Save them as env vars:
  ```bash
  export PODIUM_CLIENT_ID="..."
  export PODIUM_CLIENT_SECRET="..."
  ```

---

## Part 4 — Start the servers (5 min)

### 4.1 Start FastAPI

In your project terminal (with venv activated and env vars set):
```bash
uvicorn main:app --reload --port 8000
```
You should see `Uvicorn running on http://127.0.0.1:8000`. Leave it running.

### 4.2 Start ngrok in a second terminal

```bash
ngrok http 8000
```
You'll see something like:
```
Forwarding   https://abc123-45-67-89.ngrok-free.app -> http://localhost:8000
```

**Copy that HTTPS URL.** This is your public URL — Podium will use it to send you webhooks and OAuth callbacks. Leave this terminal running too.

### 4.3 Verify the tunnel works

Open `https://abc123-45-67-89.ngrok-free.app/docs` in your browser. You should see the FastAPI interactive docs.

Open `http://127.0.0.1:4040` in another tab — that's ngrok's request inspector. **Keep this open**; it'll show you every webhook Podium sends so you can debug payloads.

---

## Part 5 — Get a Podium refresh token (10 min)

### 5.1 Tell your app about ngrok

In the terminal running uvicorn, stop it (Ctrl+C). Add one more env var:
```bash
export PODIUM_REDIRECT_URI="https://abc123-45-67-89.ngrok-free.app"
```
Restart uvicorn:
```bash
uvicorn main:app --reload --port 8000
```

### 5.2 Register the redirect URI in your Podium app

Back in the Podium developer portal → your app → OAuth tab:
- **Redirect URI**: `https://abc123-45-67-89.ngrok-free.app/oauth/callback`
- Save

The URL must match exactly — trailing slash, HTTPS, capitalization, everything.

### 5.3 Run the OAuth flow

In your browser:
```
https://abc123-45-67-89.ngrok-free.app/oauth/start
```

You'll be redirected to Podium → log in → consent screen showing the scopes → click Approve.

Podium redirects you back to `/oauth/callback`, which exchanges the auth code for tokens and shows them on a page.

### 5.4 Save the refresh token

Copy the long refresh token string from the page. In your uvicorn terminal, Ctrl+C, then:
```bash
export PODIUM_REFRESH_TOKEN="paste_long_string_here"
```
Restart uvicorn.

The refresh token is long-lived. The app uses it to mint short-lived access tokens automatically (Podium access tokens expire after 10 hours).

**Pro tip**: save all your env vars to a `.env` file or a shell script so you don't have to re-export them every time you open a new terminal.

---

## Part 6 — Wire up the Podium webhook (15 min)

This is where you tell Podium to push appointment events to your service.

### 6.1 Pick where the client's service address lives

For mobile concierge, this is the make-or-break decision. Options:
- **Recommended**: add a custom field on the Podium contact called `service_address` and train whoever books appointments to fill it in every time
- Use the contact's main `address` field (only works if it's always the home address)
- Store addresses in your own DB and join on contact ID

Pick one and stick with it.

### 6.2 Create the Data Feed in Podium

- In Podium, navigate to **Automations** (the exact path varies by Podium version — contact Podium support if you can't find it)
- Create a new Automation with a Data Feed trigger
- Trigger on: appointment created, updated, canceled
- Action: HTTP POST to:
  ```
  https://abc123-45-67-89.ngrok-free.app/webhooks/podium/appointment
  ```
- For payload, include at minimum: appointment id, type, status, start/end times, employee (the injector), contact (the client)

If you can't configure Data Feeds yourself, email Podium support and ask them to set one up — provide the URL above and a sample payload that includes the fields listed.

### 6.3 Test the webhook with a fake appointment

- In Podium, manually create a test appointment
- In your ngrok inspector tab (`http://127.0.0.1:4040`), you should see the POST come in
- Click the request to inspect the body — **this is the real payload shape**
- In your uvicorn terminal, you should see either a 200 (success) or an error

### 6.4 Tune the field mappings if needed

If the webhook errored or stored bad data, the field names in the real payload probably don't match what `podium_appointment_webhook()` in `main.py` expects. Open the inspector, look at the actual JSON, then edit the function. The critical fields to map:
- `appointment.id` — unique identifier
- `appointment.statusName` — to detect cancellations
- `appointment.typeName` — to pick a procedure
- `appointment.startLocalDateTime` and `endLocalDateTime`
- `employee.id` — injector ID
- `contact.serviceAddress` (or wherever you stored it in 6.1)

Restart uvicorn and re-test until you see appointments at `GET /appointments`.

---

## Part 7 — Register your injectors (5 min)

For each injector you want in the system, POST to `/injectors`. Easiest way: open `https://abc123-45-67-89.ngrok-free.app/docs`, find the **POST /injectors** endpoint, click "Try it out", and submit a body like:

```json
{
  "id": "inj_001",
  "name": "Dr. Smith",
  "home_base_address": "123 Main St, Arlington, VA 22201",
  "working_hours": {"0":[9,19],"1":[9,19],"2":[9,19],"3":[9,19],"4":[9,17]}
}
```

`working_hours` is a map of weekday number (0=Monday) to `[start_hour, end_hour]` in 24-hour time. Leave the days they don't work out of the map entirely.

The `id` should match the `employee.id` Podium sends in webhooks — that's how the system links injectors to their appointments. Check the ngrok inspector to confirm what Podium's `employee.id` looks like.

---

## Part 8 — End-to-end test (5 min)

In the FastAPI docs UI:

**1. Confirm appointments are landing**
- `GET /appointments` → should list whatever test appointments you've booked in Podium

**2. Ask for availability**
- `POST /availability` with:
  ```json
  {
    "client_address": "456 Oak Ave, McLean, VA 22102",
    "procedure_type": "lip_filler"
  }
  ```
- Response should be a ranked list of injectors, each with an earliest start time, where they're coming from, and a note like "After Botox ending Tue 2:00 PM"

If the math looks reasonable given your test appointments, you're done.

---

## Part 9 — Before you go live

In this order:

1. **Tune the `PROCEDURES` catalog** in `main.py` — replace placeholder durations with your actual service times, setup, and cleanup buffers per procedure
2. **Move from in-memory to Postgres** — the dicts in `main.py` lose all data on restart; spin up a real DB
3. **Switch ngrok to a static domain** — free tier rotates URLs every restart, which means re-pasting into Podium constantly; $10/mo for a static one
4. **Verify webhook signatures** — Podium signs payloads; verify them to prevent spoofing
5. **Lock down the Google Maps API key** — restrict to your production server's IP
6. **Add request logging** — at minimum log every `/availability` call so you can debug what dispatchers are asking

---

## Troubleshooting cheat sheet

**Uvicorn won't start: "Address already in use"**
Another process is on port 8000. Find it: `lsof -i :8000` (mac/linux) and kill it, or use `--port 8001`.

**ngrok URL changed and Podium webhooks are dead**
Free ngrok URLs rotate on restart. Update both the Podium app's Redirect URI and the Data Feed webhook URL.

**`/oauth/start` returns "PODIUM_CLIENT_ID env var not set"**
You set the env var in a different terminal. Re-export it in the terminal running uvicorn, then restart uvicorn.

**`/oauth/callback` returns "Token exchange failed"**
Usually a redirect_uri mismatch. The URI in your env var + `/oauth/callback` must match what's saved in the Podium app's OAuth settings, byte-for-byte.

**`/availability` returns "No injectors registered yet"**
You need to POST to `/injectors` at least once (Part 7).

**Webhook handler returns "No service address found on contact"**
The webhook payload doesn't have the service address where the code expects it. Open the ngrok inspector, look at the real payload, update `podium_appointment_webhook()` in `main.py`.

**Geocoding returns ZERO_RESULTS**
The address string Podium sent isn't precise enough. Either improve address quality in Podium or add a more forgiving geocoding fallback (Google has a `components` parameter that helps).
