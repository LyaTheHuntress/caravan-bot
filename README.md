# Caravan Bot

Manages the daily train/caravan conductor rotation and VIP rotation for a
Last Asylum: Plague alliance.

## Setup

1. **Create the bot on Discord**
   - Go to https://discord.com/developers/applications → New Application
   - Bot tab → Add Bot → copy the token
   - Under Privileged Gateway Intents, enable **Server Members Intent**
   - OAuth2 → URL Generator → check `bot` and `applications.commands` scopes,
     plus permissions like Send Messages and Use Slash Commands → use the
     generated URL to invite the bot to your server

2. **Create a "Leadership" role** in your Discord server (or use a different
   name — see below) and assign it to your leadership team. Only members
   with this role can use the assignment/logging commands.

3. **Install dependencies**
   ```
   pip install -r requirements.txt
   ```

4. **Configure**
   - Copy `.env.example` to `.env`
   - Paste your bot token into `DISCORD_BOT_TOKEN`
   - Set `LEADERSHIP_ROLE_NAME` to match your role's exact name (default: `Leadership`)

5. **Run it**
   ```
   python bot.py
   ```
   On first run it creates `caravan.db` (SQLite) in the same folder — this
   holds all your queue and history data, and persists between restarts.

## Commands

**Anyone can use:**
- `/join-queue [time]` — sign up for the conductor queue with an optional preferred server time
- `/leave-queue` — remove yourself from the conductor queue
- `/queue` — view the current conductor queue
- `/join-vip-queue` / `/leave-vip-queue` / `/vip-queue` — same, for VIP
- `/history [member]` — view one member's history, or the full roster (sorted by who's waited longest) if left blank
- `/how-to-conduct` — posts the run instructions (refresh 5x to gold, set defenses)

**Leadership role only:**
- `/assign-conductor @member` — assigns them as conductor **right now** (starts the real in-game countdown), removes them from the queue, DMs them the how-to
- `/assign-vip @member` — assigns them as VIP for the current run
- `/log-conductor @member date:YYYY-MM-DD` — backfills a past turn with no countdown and no ping (for correcting history or importing your existing records)
- `/log-vip @member date:YYYY-MM-DD` — same, for VIP

Leadership commands work on **anyone**, whether or not they're in the queue —
the queue is just a helpful view of who's waiting and when, it never blocks
a manual pick.

## Deploying on Railway

1. Push this folder to a **private** GitHub repo (the included `.gitignore`
   keeps your `.env` and `caravan.db` out of the repo automatically).
2. On railway.app: New Project → Deploy from GitHub repo → select this repo.
3. In the Railway project's **Variables** tab, add:
   - `DISCORD_BOT_TOKEN` = your bot token
   - `LEADERSHIP_ROLE_NAME` = `Leadership`
4. In the **Volumes** section, attach a volume and mount it to the project's
   working directory (e.g. `/app`) so `caravan.db` survives redeploys —
   without this, your queue/history data resets every time you push a code
   update.
5. The included `railway.json` tells Railway to run `python bot.py` as a
   persistent background worker (not a web server), so no further config
   should be needed — it should deploy and come online automatically.

## Notes

- Might/power ranking was intentionally left out for now — can be added
  later as a field on `/join-queue` if you want to sort or filter by it.
- The queue does not auto-expire; if you want it to reset daily, that's an
  easy addition (a scheduled task that clears `conductor_queue` at a set
  server time).
- Hosting: this bot is extremely lightweight — a free tier on Railway/Render,
  a Raspberry Pi, or a small VPS (~$4-6/month) are all more than enough.
