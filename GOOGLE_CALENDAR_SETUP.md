# Google Calendar Setup (OAuth Method)

This wires your server to read appointments from each injector's Google Calendar
using per-injector OAuth — no service account, no Google Cloud security policy
fights. ~30 min admin setup, then ~30 sec per injector.

How it works:
- You (admin) create one set of OAuth credentials in Google Cloud Console
- Each injector clicks a one-time authorization link to grant calendar access
- Your server stores their refresh token and reads their calendar on demand

---

## Part 1 — Set up OAuth in Google Cloud Console (15 min, one time)

### 1.1 Enable the Google Calendar API

- Go to `console.cloud.google.com`
- Make sure you're in your existing project (top dropdown)
- Left nav → **APIs & Services → Library**
- Search **Google Calendar API** → click it → **Enable**

### 1.2 Configure the OAuth consent screen

This is what your injectors will see when they click the authorization link.

- Left nav → **APIs & Services → OAuth consent screen**
- **User Type**: choose **External** (unless all your injectors are in a Google
  Workspace org you own — most won't be)
- Click **Create**
- Fill in:
  - **App name**: `Injector Availability` (or whatever)
  - **User support email**: your email
  - **Developer contact**: your email
- Click **Save and Continue**
- **Scopes**: click **Add or Remove Scopes** → find and check
  `https://www.googleapis.com/auth/calendar.readonly`
  → **Update**, then **Save and Continue**
- **Test users**: add the Gmail addresses of every injector who will authorize
  (while the app is in "Testing" mode, only listed test users can authorize).
  Click **Save and Continue**.
- Review → **Back to Dashboard**

The app stays in "Testing" mode forever for an internal tool. You can publish
it later if needed, but Testing works fine and isn't subject to verification.

### 1.3 Create the OAuth Client ID

- Left nav → **APIs & Services → Credentials**
- **+ Create Credentials → OAuth client ID**
- **Application type**: Web application
- **Name**: `Injector Availability Web Client`
- **Authorized redirect URIs**: click **+ Add URI** and paste:
  ```
  https://fnftest.onrender.com/oauth/google/callback
  ```
  (Substitute your Render URL.)
- Click **Create**

A popup shows your **Client ID** and **Client Secret**. Copy both — you'll
add them to Render in a sec.

---

## Part 2 — Add OAuth credentials to Render (3 min)

- Render dashboard → your service → **Environment** tab
- Add two env vars:
  - `GOOGLE_OAUTH_CLIENT_ID` = (paste the client ID)
  - `GOOGLE_OAUTH_CLIENT_SECRET` = (paste the client secret)
- Save — Render auto-redeploys (~2 min)

---

## Part 3 — Register your injectors (2 min per injector)

For each injector, POST to `/injectors` via `/docs`:

```json
{
  "id": "inj_001",
  "name": "Dr. Smith",
  "home_base_address": "1100 Wilson Blvd, Arlington, VA 22209",
  "working_hours": {"0":[9,19],"1":[9,19],"2":[9,19],"3":[9,19],"4":[9,17]}
}
```

No calendar ID needed yet — that's set automatically after they OAuth (defaults
to their primary calendar).

---

## Part 4 — Each injector authorizes their calendar (30 sec per injector)

For each injector, send them this link with their ID in the query string:

```
https://fnftest.onrender.com/oauth/google/start?injector_id=inj_001
```

What they see:
1. Click the link
2. Sign in to Google (with the account whose calendar has their appointments)
3. Google says "Injector Availability wants to see your calendar events"
4. They click **Allow**
5. They see a "✅ Dr. Smith is connected" confirmation page

Behind the scenes, their refresh token gets stored on their Injector record.

**Important**: each injector's link is different — the `injector_id` query
param has to match the ID you registered. Send the right link to the right
person.

While the app is in "Testing" mode (which is fine for internal use), Google
shows an "unverified app" warning. Tell injectors to click **Advanced → Go to
[App Name] (unsafe)**. It's only "unsafe" because Google hasn't reviewed it —
it's your app and you trust it.

---

## Part 5 — Tell injectors how to format calendar events

Send your team this:

> **When creating an appointment in Google Calendar:**
>
> - **Title**: include the procedure name. Examples:
>   - `Botox - Sarah Johnson`
>   - `Lip Filler with Maria`
>   - `Cheek Filler - new client`
> - **Location**: the client's full home address. e.g.:
>   - `8000 Towers Crescent Dr, Vienna, VA 22182`
> - **Start/End time**: actual appointment block
>
> Events without a Location won't be picked up. Use full street addresses
> so Google Maps can resolve them.

The system reads procedure type by looking for keywords in the title
(`botox`, `lip filler`, `cheek filler`, `jawline`, `consult`). If a title
doesn't match any, it defaults to "consultation."

---

## Part 6 — Sync and verify

Once injectors are authorized and have events on their calendars, trigger a
sync:

```
POST https://fnftest.onrender.com/sync/google
```

Easiest from `/docs` → find **POST /sync/google** → Try it out → Execute.

Response:
```json
{
  "ok": true,
  "synced": 12,
  "skipped_no_address": 1,
  "errors": [],
  "authorized_injectors": ["inj_001", "inj_002"]
}
```

Then verify the data landed:
```
GET https://fnftest.onrender.com/appointments
```

You should see the real appointments. Visit `/book` and the availability
math now runs against live calendar data.

---

## Keeping the sync fresh

The simplest pattern: scheduled re-sync. Use a free uptime monitor like
UptimeRobot to ping `/sync/google` every 15 minutes. Each call pulls the
latest calendar state.

For real-time, Google Calendar's push notifications (watch API) can POST to
your server when calendars change. More setup, save for later.

---

## Troubleshooting

**"GOOGLE_OAUTH_CLIENT_ID env var not set"**
→ The env vars in Render aren't set, or the redeploy hasn't finished. Wait.

**Authorization page says "This app isn't verified"**
→ Expected while the app is in Testing mode. Click **Advanced → Go to
   (App name) (unsafe)**. Not actually unsafe — it's your app.

**"Error 403: access_denied" during OAuth**
→ The injector's Gmail isn't in the OAuth consent screen's Test users list.
   Go back to OAuth consent screen → Test users → add them.

**Callback says "No refresh token received"**
→ The user has authorized this app before. Google only gives a refresh token
   on first authorization. To fix: have them visit
   `myaccount.google.com/permissions`, remove the app's access, then retry.

**`synced: 0` with no errors**
→ Either no events in date range, or no injectors have completed OAuth.
   Check `authorized_injectors` in the response.

**`skipped_no_address` is high**
→ Events in calendar don't have anything in the Location field. Train the
   team to fill it in.

**"redirect_uri_mismatch" during OAuth**
→ The Authorized redirect URI in Google Cloud Console doesn't exactly match
   the URI your server is using. Both must be:
   `https://fnftest.onrender.com/oauth/google/callback`
