# Caravan Bot — Full Setup Guide

Everything needed to go from zero to a running bot in your alliance's server.
Do these roughly in order — each phase builds on the last.

---

## Phase 1: Create the bot on Discord

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) → **New Application** → name it (e.g. "Alliance Caravan Bot")
2. Go to the **Bot** tab
3. Click **Reset Token**, copy the token, and save it somewhere private for now (you'll paste it into Railway later, not into any file you share or commit)
4. Under **Privileged Gateway Intents**, turn ON **Server Members Intent** (you already did this ✅)
5. Turn OFF **Requires OAuth2 Code Grant** near the top of the page if it's on — not needed for this bot
6. Under **Bot Permissions** further down the page, check:
   - Send Messages
   - Embed Links
   - Use Slash Commands

## Phase 2: Generate the invite link

1. Go to the **OAuth2** tab (left sidebar)
2. Under **Scopes**, check `bot` and `applications.commands`
3. Under **Bot Permissions**, check the same three as above (Send Messages, Embed Links, Use Slash Commands) — you already did this ✅
4. Leave **Integration Type** as `Guild Install`
5. Copy the **Generated URL** at the bottom
6. Paste it into your browser, choose your alliance's server, and confirm — the bot will join (it'll show offline until the code is actually running)

## Phase 3: Set up your Discord server

1. Server Settings → Roles → create a role called **Leadership** (must match exactly, case-sensitive, unless you change `LEADERSHIP_ROLE_NAME`)
2. Assign that role to your leadership team members
3. That's it — the bot checks for this role before allowing `/assign-conductor`, `/assign-vip`, `/log-conductor`, and `/log-vip`

## Phase 4: Get the code ready on your PC

1. Install [Git for Windows](https://git-scm.com) (click through the installer with defaults)
2. Make a free [GitHub](https://github.com) account if you don't have one
3. On GitHub, create a **new private repository** (e.g. `caravan-bot`)
4. On your PC, open a terminal in the folder containing `bot.py`, `database.py`, `requirements.txt`, `railway.json`, and `.gitignore`:
   - In File Explorer, navigate into that folder
   - Right-click inside the folder (on empty space, not on a file) → **Git Bash Here**
   - This opens a terminal already pointed at the right folder
5. In that Git Bash window, run:
   ```
   git init
   git add .
   git commit -m "initial commit"
   git remote add origin https://github.com/YOUR-USERNAME/caravan-bot.git
   git branch -M main
   git push -u origin main
   ```
   (The `.gitignore` file makes sure your token and local database never get uploaded.)

## Phase 5: Deploy — choose ONE hosting path

### Option A: Railway (~$5/month, easiest)

1. Sign up at [railway.app](https://railway.app), using your GitHub account to sign in
2. **New Project** → **Deploy from GitHub repo** → select your `caravan-bot` repo
3. Railway auto-detects Python and installs `requirements.txt`
4. Go to the **Variables** tab and add:
   - `DISCORD_BOT_TOKEN` = your bot token
   - `LEADERSHIP_ROLE_NAME` = `Leadership`
5. Go to **Volumes**, attach a volume, and mount it to `/app` so `caravan.db` survives redeploys (without this, your history/queue data resets every time you push a code update)
6. Railway reads `railway.json` automatically and runs `python bot.py` as a background worker — no further config needed
7. Check the **Deployments** logs tab — you should see `Synced X commands. Logged in as [BotName].` Once you see that, your bot is live and should show online in Discord

### Option B: Oracle Cloud Always Free tier ($0/month forever, more setup)

More steps, but genuinely free with no time limit. Windows works fine — you'll connect to the remote server using Windows' built-in SSH (Terminal app) or [PuTTY](https://www.putty.org).

1. Sign up at [cloud.oracle.com](https://cloud.oracle.com) for an Always Free account (requires a credit card for identity verification, but the Always Free resources are not billed)
2. In the Oracle console, create a **Compute Instance**:
   - Choose an "Always Free eligible" shape (e.g. VM.Standard.E2.1.Micro or the Ampere ARM free shape)
   - Choose Ubuntu as the OS image
   - When prompted, download the SSH key pair Oracle generates. **Save it outside your `caravan-bot` folder** — e.g. in a dedicated `Documents\ssh-keys\` folder, not inside the git repo. If it ends up in the repo folder and you ever run `git add .` instead of adding specific files, this key could get pushed to GitHub — and since it's what grants SSH access to your server, that would let anyone with the repo link log into your machine. Once your key is saved, you'll reference its full path (not just the filename) in the `ssh -i` command below, so where it lives doesn't have to match your repo folder at all.
3. Configure networking: in the instance's **Virtual Cloud Network → Security Lists**, you generally don't need to open extra ports for a Discord bot (it only makes outbound connections), so default settings usually work — this step trips people up but for this specific bot there's little to configure
4. Connect from Windows:
   - Open **Terminal** (Windows 10/11 has SSH built in) or use PuTTY with your downloaded key
   - Run: `ssh -i path\to\your\key.key ubuntu@YOUR_INSTANCE_IP` (IP shown in the Oracle console)
5. Once connected, install what you need:
   ```
   sudo apt update
   sudo apt install python3 python3-pip git -y
   git clone https://github.com/YOUR-USERNAME/caravan-bot.git
   cd caravan-bot
   pip3 install -r requirements.txt
   ```
6. Create your `.env` file directly on the server (copy `.env.example`, fill in your real token):
   ```
   cp .env.example .env
   nano .env
   ```
   (edit the values, then Ctrl+X, Y, Enter to save)

   **Important — set `GAME_SERVER_UTC_OFFSET` for your own game/server.** This bot uses it to know
   what "server time" means for the daily 00:00 pick and the 30-minute reminder, and it's almost
   certainly different from the value in this repo's example — different games run on different
   server times, and even within one game, different servers can be on different offsets. To find
   yours: open your game and compare its in-game clock to the current real-world UTC time (search
   "UTC time now" for a reference). If your game's clock reads 2 hours behind UTC, set this to
   `-2`; if it's 8 hours ahead, set it to `8`. Also check whether your real-world region observes
   Daylight Saving Time while your game server doesn't (or vice versa) — if so, you may need to
   update this value twice a year.
7. Keep the bot running permanently even after you disconnect, using `screen`:
   ```
   sudo apt install screen -y
   screen -S caravanbot
   python3 bot.py
   ```
   Then press `Ctrl+A` then `D` to detach — the bot keeps running in the background. To check on it later: `screen -r caravanbot`

---

## Phase 6: Confirm it works

1. In Discord, type `/` in any channel — you should see your bot's commands appear
2. Try `/how-to-conduct` as a regular member — should post instantly
3. As a Leadership-role member, try `/assign-conductor @someone` — should post the assignment message
4. Try `/history` — should show the (currently empty) roster

## Phase 7 (later, whenever you're ready): backfill your existing records

Use `/log-conductor @member date:YYYY-MM-DD` and `/log-vip @member date:YYYY-MM-DD` to enter your existing manually-tracked history, so the fairness view (`/history`) is accurate from day one.
 