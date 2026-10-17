# One Thing

One inbox. One thing per lane per day. It lives where you already look: your calendar, and the moment you open TikTok.

```
capture (Siri / Action Button / web)  ─┐
Canvas / Outlook deadlines (ICS)      ─┼─►  inbox ─► morning pick (1 uni · 1 life · 1 side, optional)
                                       │                    │
                                       │     ┌──────────────┼───────────────────┐
                                       │     ▼              ▼                   ▼
                                       │  calendar feed   pause alert when     web app: tap done,
                                       │  (★ today's pick) TikTok/IG/YT opens  XP, streak, close day
```

**Daily loop (2 minutes total)**
1. **Morning:** open the app → the most urgent uni task is preselected → optionally add one life + one side task → *Start the day*.
2. **During the day:** your picks sit in your calendar as all-day events. Open TikTok/Instagram/YouTube/a game → a pause pops up with your pick → *Do it* (opens the app) or *5 minutes* (lets you through).
3. **Evening:** tap done, *Close the day*, one line on what moved. All picks done = +20 XP bonus.

Everything else stays hidden under *Everything (n)*. You never have to look at the full list.

**XP:** uni 30 · side 20 · life 10 · full day +20. Streak = consecutive days with at least one pick done. Missing a day doesn't punish you; the streak simply restarts.

---

## 1. Run locally (5 min)

```bash
cd onething
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
ONETHING_TOKEN=pick-a-long-random-string uvicorn app:app --reload
# open http://localhost:8000/?token=pick-a-long-random-string
python3 test_app.py   # all tests should pass
```

## 2. Deploy (15 min): your phone needs a public URL

Fly.io (keeps the SQLite file on a small volume; costs a few dollars a month at most, check fly.io/pricing):

```bash
brew install flyctl && fly auth signup        # or: fly auth login
# edit fly.toml → set a unique app name
fly launch --copy-config --no-deploy
fly volumes create onething_data --region nrt --size 1
fly secrets set ONETHING_TOKEN=$(openssl rand -hex 16)
fly secrets set ONETHING_ICS_URLS="<your Canvas feed URL>"   # optional, see §5
fly deploy
fly secrets list    # the token's value isn't shown here, so save it when you generate it
```

App URL: `https://<app-name>.fly.dev/?token=<TOKEN>`

> **Free alternative:** run it on your Mac and reach it from your phone over [Tailscale](https://tailscale.com). It only works while the Mac is awake, and Outlook can't subscribe to it (§4).

**Backup:** `fly ssh sftp get /data/onething.db ./backup.db`

## 3. iPhone setup (10 min)

**a) Home Screen app:** open the URL with `?token=…` in Safari → Share → *Add to Home Screen*. The token is remembered.

**b) 3-second capture (Shortcuts app → +)**
1. *Dictate Text*
2. *Get Contents of URL*: `https://<app>.fly.dev/api/capture?token=<TOKEN>`, Method **POST**, Request Body **JSON**, field `text` = *Dictated Text*
3. *Show Notification*: "Captured"

Name it **"Add task"** (so "Hey Siri, add task" works), then put it on the **Action Button**, Back Tap (Settings → Accessibility → Touch → Back Tap) or the Watch. Say "uni finish report due friday" and it lands in the uni lane with a due date.

**c) The pause (this replaces the "annoying" part)**

Shortcuts → Automation → + → **App** → choose TikTok, Instagram, YouTube and your games → **Is Opened** → **Run Immediately** (turn off *Notify When Run*). Actions:

1. *Get Contents of URL*: `https://<app>.fly.dev/today.txt?token=<TOKEN>` (GET)
2. *If* Contents **contains** `All done` → leave empty (you earned it, no pause)
3. *Otherwise* → *Choose from Menu*, prompt = *Contents of URL*, two options:
   - **Do it now** → *Open URLs* `https://<app>.fly.dev/` (takes you out of TikTok)
   - **5 minutes** → leave empty (lets you in)
4. *End If*

This works even with notifications muted, because it isn't a notification: it fires when you open the app. The goal is that every open becomes a conscious choice.

**d) Hard backstop (optional):** Screen Time → App Limits → Social + Games → e.g. 45 min/day. One sec or ScreenZen can add a breathing delay on top.

## 4. Picks in your calendar

In the app: Settings → copy the **Calendar subscription** URL (`/feed.ics?token=…`).

- **iPhone:** Settings → Calendar → Accounts → Add Account → Other → *Add Subscribed Calendar*.
- **Outlook (web/desktop):** Add calendar → *Subscribe from web* → paste. Outlook refreshes subscriptions slowly (sometimes hours), so use the iPhone calendar or the widget for same-day changes.
- **Mac Calendar:** File → New Calendar Subscription → set auto-refresh to every 5 min.

You'll see `★ UNI: …` on today (`✅` once done), the last 14 days of picks, and `⏰` for deadlines you captured with a due date.

## 5. Pull uni deadlines in automatically

Set `ONETHING_ICS_URLS` (comma-separated). Every 6 h (and on startup) new events are imported into the **uni** lane, deduplicated by event ID.

- **Canvas (Fontys):** Calendar → bottom of the right sidebar → **Calendar Feed** → copy the link. This gives you assignment deadlines and is the best source.
- **Outlook / Office 365:** Outlook on the web → Settings → Calendar → Shared calendars → **Publish a calendar** → *Can view all details* → copy the **ICS** link. (Your school's admins may have disabled publishing; if so, use Canvas.)
- Filter the noise with `ONETHING_ICS_KEYWORDS="deadline,exam,assignment,due"`. Only events whose title contains one of these words get imported. Lectures stay out.

Trigger an import manually: Settings → *Import calendar now*.

## 6. Make it motivate you: do this on day 1

Settings → fill the three **why** lines. They appear under every task of that lane, every day, in the app and in the calendar event.

- Uni: *"Finish the Bachelor strong → TU/e pre-master / the job I want."*
- Side: *"Ship things people use. Finished > perfect."*
- Life: *"Clear head, no background stress."*

Your own words beat anything generic. Then do the **brain dump**: everything you're carrying, one per line, in the first screen.

## Capture syntax

| You type / say | Lane | Due |
|---|---|---|
| `uni: finish report due fri` | uni | next Friday (today if it's Friday) |
| `side - deploy bot @tomorrow` | side | tomorrow |
| `study chapter 4 by 20-10` | uni | 20 Oct (day-month) |
| `uni: exam @2026-11-02` | uni | 2 Nov |
| `call the bank` | life (default) | none |

Lane words: `uni/u/school/study/class`, `life/l/home/admin`, `side/s/project/build`.

## Config (env vars)

| Var | Default | |
|---|---|---|
| `ONETHING_TOKEN` | `change-me` | **set this**; every API call needs it |
| `ONETHING_TZ` | `Asia/Seoul` | decides when "today" rolls over; `Europe/Amsterdam` when back |
| `ONETHING_DATA` | `./data` | where `onething.db` lives |
| `ONETHING_ICS_URLS` | (none) | calendar feeds to import |
| `ONETHING_ICS_KEYWORDS` | (none) | import filter |
| `ONETHING_IMPORT_HOURS` | `6` | import interval |

## API

All endpoints take `?token=` or an `X-Token` header.

| | |
|---|---|
| `GET /api/today` | everything the UI needs |
| `POST /api/capture` `{"text": "…"}` | one task per line |
| `POST /api/pick` `{"task_ids": [..]}` | set today's picks (max 3) |
| `POST /api/tasks/{id}/done[?undo=true]` | done / undo (XP adjusts) |
| `PATCH /api/tasks/{id}` `{"title","kind","due"}` | edit; `due: ""` clears |
| `DELETE /api/tasks/{id}` | |
| `POST /api/close` `{"note"}` | close the day, +20 if all picks done |
| `PUT /api/goals` `{"kind","why"}` | why-lines |
| `POST /api/import` | pull ICS feeds now |
| `GET /today.txt` | text for the pause shortcut |
| `GET /feed.ics` | calendar subscription |

## The builder's-trap rule

V1 is feature-complete. **Use it for 14 days before adding anything.** After 14 days, add a feature only if you hit the same friction at least 3 times. Candidates you might want then: a home-screen widget (Scriptable + `/today.txt`), a weekly review screen, a Telegram mirror. None of them are needed to start.
