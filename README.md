# Caravan Bot

Fair, automatic conductor and VIP rotation for a game alliance's daily
Alliance Caravan. Built for **Last Asylum: Plague**, but the mechanics
(one conductor + one VIP per day, picked by who's waited longest) are
generic enough to fit any game with a similar daily-turn system.

No more guessing whose turn it is, no more double-booking two people for
the same slot, and no manual list editing — the bot decides fairly and
announces it.

## How it works

- Members join the conductor rotation once with `/join-queue time:HH:MM`
  (their preferred **game server time**) and the VIP rotation once with
  `/join-vip-queue`. Both are permanent — no need to rejoin after a turn.
- Every day at **00:00 server time**, the bot picks whoever's waited
  longest in each rotation (never-had-a-turn members go first) and
  announces both picks in your leadership channel.
- **30 minutes before** the picked conductor's preferred time, the bot
  pings again as a reminder.
- Only one conductor and one VIP get picked per server day — matches
  games where the alliance caravan can only run once every 24 hours.
- Leadership can override either pick manually, or skip someone who
  already had a turn without the bot knowing.
- Removing someone from Discord automatically removes them from both
  rotations — no manual cleanup needed.

## Setup

See [SETUP_GUIDE.md](SETUP_GUIDE.md) for the full walkthrough, from
creating the Discord bot to deploying it. Quick summary of what you'll
configure in `.env`:

- `DISCORD_BOT_TOKEN` — your bot's token
- `LEADERSHIP_ROLE_NAME` — the Discord role allowed to use leadership commands (default: `Leadership`)
- `LEADERSHIP_CHANNEL_ID` — the channel where daily picks and reminders get posted
- `GAME_SERVER_UTC_OFFSET` — hours to add to UTC to get **your game's** server time (e.g. `-2` for UTC-2, `8` for UTC+8). This is specific to your game and possibly your particular server — check your game's in-app clock against real UTC time to figure out your offset, and adjust for Daylight Saving Time if your real-world timezone observes it but the game server doesn't (or vice versa).

```
pip install -r requirements.txt
python bot.py
```

On first run it creates `caravan.db` (SQLite) in the same folder — this
holds your rotation lists and history, and persists between restarts.

## Commands

**Anyone can use:**
- `/join-queue time:HH:MM` — join the conductor rotation with your preferred server time (required)
- `/leave-queue` — remove yourself from the conductor rotation
- `/queue` — view the conductor rotation, soonest-turn first
- `/join-vip-queue` / `/leave-vip-queue` / `/vip-queue` — same, for VIP
- `/history [member]` — view one member's history, or the full roster (sorted by who's waited longest) if left blank
- `/how-to-conduct` — posts the run instructions
- `/commands` — show this list

**Leadership role only:**
- `/assign-conductor @member` — manually assign the conductor, overriding today's pick
- `/assign-vip @member` — manually assign the VIP, overriding today's pick
- `/skip-conductor` — today's conductor already had their turn (e.g. handled entirely in-game); pick the next in rotation
- `/skip-vip` — same, for VIP
- `/remove-from-queue @member` / `/remove-from-vip-queue @member` — remove someone from one rotation
- `/remove-member @member` — remove someone from both rotations at once (e.g. they left the alliance)
- `/log-conductor @member date:YYYY-MM-DD` / `/log-vip @member date:YYYY-MM-DD` — backfill a past turn with no ping (for correcting history or importing existing records)

Leadership commands work on **anyone**, whether or not they're in a
rotation — the rotation is just what decides the daily pick automatically;
it never blocks a manual override.

## Deploying

### Option A: Railway (~$5/month, easiest)

1. Push this repo to GitHub, sign up at [railway.app](https://railway.app) with your GitHub account
2. **New Project** → **Deploy from GitHub repo** → select this repo
3. In **Variables**, add `DISCORD_BOT_TOKEN` and the other `.env` values above
4. In **Volumes**, attach a volume mounted to `/app` so `caravan.db` survives redeploys
5. Railway reads `railway.json` and runs the bot as a background worker automatically

### Option B: Any Linux VPS / free-tier cloud instance ($0–$6/month, more setup)

See [SETUP_GUIDE.md](SETUP_GUIDE.md) Phase 5, Option B for a full walkthrough
(SSH setup, running persistently, restarting after a reboot).

## Notes

- **Only one conductor + one VIP per server day**, no matter how many
  different preferred times are queued up — matches games where the
  in-game caravan itself can only run once every 24 hours. If your game
  allows more than one run per day, this bot's daily lock will need to
  be adjusted (it currently assumes exactly one).
- The rotation is fairness-based: whoever hasn't had a turn longest goes
  next. Never-had-a-turn members are always prioritized first.
- Might/power ranking was intentionally left out — can be added later as
  a field on `/join-queue` if you want to sort or filter by it.
- Hosting: this bot is lightweight — a free tier on Railway/Render, a
  Raspberry Pi, or a small VPS (~$0–6/month) are all more than enough.
