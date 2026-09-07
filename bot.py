"""
Caravan/train rotation bot for a Last Asylum: Plague alliance.

Manages:
- A conductor sign-up queue (members list a preferred server time)
- Live conductor assignment (leadership-only, starts the in-game countdown)
- A separate VIP rotation, tracked the same way
- Manual backfill/history logging for turns that already happened
- A reusable "how to conduct" instructions command

Leadership-only commands are gated by a Discord role, set via LEADERSHIP_ROLE_NAME below.
"""

import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

import database as db

load_dotenv()

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
LEADERSHIP_ROLE_NAME = os.getenv("LEADERSHIP_ROLE_NAME", "Leadership")
LEADERSHIP_CHANNEL_ID = os.getenv("LEADERSHIP_CHANNEL_ID")
# Hours to add to UTC to get the game's server time. Adjust if your game's server time
# doesn't match UTC. E.g. if game server time is UTC+8, set GAME_SERVER_UTC_OFFSET=8.
GAME_SERVER_UTC_OFFSET = float(os.getenv("GAME_SERVER_UTC_OFFSET", "0"))

HOW_TO_CONDUCT_TEXT = (
    "**How to conduct the caravan:**\n\n"
    "**Step 1 — Sign up with the bot**\n"
    "Run `/join-queue time:HH:MM` with your preferred **server time** (use the in-game server "
    "clock, not your local time zone — the bot only understands server time). You don't need to "
    "message leadership yourself: the bot automatically pings Leadership 30 minutes before your "
    "requested time so they can assign you. That's the only step you need to take ahead of time.\n\n"
    "**Step 2 — Once you're assigned, turn the caravan gold**\n"
    "It costs **25 Alliance Caravan Tickets** to refresh the caravan 5 times, which is "
    "required to turn it gold. You collect tickets by clicking the **gift box** that appears "
    "in the lower-right corner of the Alliance Caravan page.\n\n"
    "**Step 3 — Let members join and gift tickets**\n"
    "Up to **24 members** can join your caravan. Each person who joins automatically gifts "
    "at least 1 ticket (up to 3). Since assignment starts the countdown immediately, expect "
    "to spend some of that time waiting for members to join and collecting their gifted "
    "tickets before you have all 25.\n\n"
    "**Step 4 — Set a VIP**\n"
    "You can set **one VIP member**, who gets extra rewards. VIP is a rotating spot — the bot "
    "will tell leadership who's up next when it pings about your run.\n\n"
    "**Step 5 — Set your defenses**\n"
    "Set all **3 squads** as your defenses.\n\n"
    "**Step 6 — You're done**\n"
    "Once the caravan is gold, your squads are set, and your VIP is assigned, there's nothing "
    "else to do — it automatically embarks **4 hours** after a caravan leader (conductor) is "
    "assigned."
)

COMMANDS_TEXT = (
    "**Caravan Bot — Commands**\n\n"
    "**Anyone can use:**\n"
    "`/join-queue [time]` — sign up for the conductor queue with your preferred server time\n"
    "`/leave-queue` — remove yourself from the conductor queue\n"
    "`/queue` — view the current conductor queue\n"
    "`/join-vip-queue` — sign up for the VIP queue\n"
    "`/leave-vip-queue` — remove yourself from the VIP queue\n"
    "`/vip-queue` — view the current VIP queue\n"
    "`/history [member]` — view your history, someone else's, or the full roster if left blank\n"
    "`/how-to-conduct` — full instructions for running the caravan\n"
    "`/commands` — show this list\n\n"
    "**Leadership only:**\n"
    "`/assign-conductor @member` — assign the conductor, starts the countdown immediately\n"
    "`/assign-vip @member` — assign the VIP for the current run\n"
    "`/remove-from-queue @member` — remove someone else from the conductor queue\n"
    "`/remove-from-vip-queue @member` — remove someone else from the VIP queue\n"
    "`/remove-member @member` — remove someone from both rotations at once (e.g. they left the alliance)\n"
    "`/log-conductor @member date:YYYY-MM-DD` — backfill a past conductor turn (no countdown)\n"
    "`/log-vip @member date:YYYY-MM-DD` — backfill a past VIP turn"
)

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)


def is_leadership():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False
        has_role = any(r.name == LEADERSHIP_ROLE_NAME for r in interaction.user.roles)
        if not has_role:
            await interaction.response.send_message(
                f"You need the **{LEADERSHIP_ROLE_NAME}** role to use this command.",
                ephemeral=True,
            )
        return has_role
    return app_commands.check(predicate)


def parse_date(date_str: str) -> Optional[str]:
    """Accepts YYYY-MM-DD, returns an ISO timestamp (midnight UTC) or None if invalid."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        return None


def game_server_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=GAME_SERVER_UTC_OFFSET)


def parse_hhmm(time_str: str):
    try:
        return datetime.strptime(time_str, "%H:%M")
    except ValueError:
        return None


@tasks.loop(minutes=1)
async def check_due_conductors():
    if not LEADERSHIP_CHANNEL_ID:
        return
    channel = bot.get_channel(int(LEADERSHIP_CHANNEL_ID))
    if channel is None:
        print(f"LEADERSHIP_CHANNEL_ID {LEADERSHIP_CHANNEL_ID} not found or not accessible.")
        return

    server_now = game_server_now()
    current_time_str = server_now.strftime("%H:%M")
    today_str = server_now.strftime("%Y-%m-%d")

    pending = db.get_pending_notification_entries()
    if not pending:
        return

    due = []
    for entry in pending:
        preferred = parse_hhmm(entry["preferred_time"])
        if preferred is None:
            continue
        # Trigger 30 minutes before the requested time (wraps correctly across midnight).
        trigger = (preferred - timedelta(minutes=30)).strftime("%H:%M")
        if trigger != current_time_str:
            continue
        # Skip if this entry already fired today (by game-server date) — this is
        # what lets the same signup repeat day after day without rejoining.
        if entry["notified_at"] and entry["notified_at"][:10] == today_str:
            continue
        due.append(entry)
    if not due:
        return

    leadership_role = None
    if isinstance(channel, discord.TextChannel) and channel.guild:
        leadership_role = discord.utils.get(channel.guild.roles, name=LEADERSHIP_ROLE_NAME)
    role_mention = leadership_role.mention if leadership_role else f"@{LEADERSHIP_ROLE_NAME}"

    # Only one person can actually conduct at a given time. If more than one queue
    # entry is due for the same trigger (e.g. two members both picked 00:00), pick
    # whoever has waited longest (never-conducted members first) as the real pick,
    # ping leadership for just that one, and flag the rest as a conflict instead of
    # silently sending a separate ping per person.
    def fairness_key(entry):
        last = entry["last_conducted_at"]
        return (last is not None, last or "")

    due.sort(key=fairness_key)
    chosen, conflicts = due[0], due[1:]

    # Pick the VIP the same way the conductor pick is decided: automatically, off
    # fairness, from the persistent VIP list — no /assign-vip needed, since
    # assignment actually happens in-game, not in Discord. Being picked does not
    # remove anyone from the list; it just updates when they last had a turn.
    # Exclude today's conductor so they can't end up as their own VIP.
    next_vip = db.get_next_vip(exclude_discord_id=chosen["discord_id"])
    if next_vip:
        vip_line = f"VIP for this run: **{next_vip['name']}**."
        db.assign_vip(next_vip["discord_id"], next_vip["name"], assigned_by="auto")
    else:
        vip_line = "No one is currently in the VIP queue."

    await channel.send(
        f"{role_mention} — **{chosen['name']}** is set to run the caravan at "
        f"**{chosen['preferred_time']}** server time (starting in 30 minutes). "
        f"Assign them with `/assign-conductor`.\n{vip_line}"
    )
    db.mark_conductor_notified(chosen["discord_id"], server_now.isoformat())

    if conflicts:
        names = ", ".join(f"**{c['name']}**" for c in conflicts)
        await channel.send(
            f"⚠️ {names} also requested **{chosen['preferred_time']}** server time, but only "
            f"one conductor can run at once. **{chosen['name']}** was picked because they've "
            f"waited longest since their last turn. Ask the others to run `/join-queue` again "
            f"with a different time."
        )


@bot.event
async def on_ready():
    db.init_db()
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} commands. Logged in as {bot.user}.")
    except Exception as e:
        print(f"Command sync failed: {e}")
    if not check_due_conductors.is_running():
        check_due_conductors.start()


# ---------------------------------------------------------------------------
# Conductor queue (self-service sign-up)
# ---------------------------------------------------------------------------

@bot.tree.command(name="join-queue", description="Sign up for the conductor rotation with your preferred server time.")
@app_commands.describe(time="Your preferred server time, e.g. 19:00")
async def join_queue(interaction: discord.Interaction, time: Optional[str] = None):
    db.join_conductor_queue(str(interaction.user.id), interaction.user.display_name, time)
    msg = f"You're on the conductor queue"
    msg += f", preferred time **{time}**." if time else "."
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="leave-queue", description="Remove yourself from the conductor queue.")
async def leave_queue(interaction: discord.Interaction):
    db.leave_conductor_queue(str(interaction.user.id))
    await interaction.response.send_message("You've been removed from the conductor queue.", ephemeral=True)


@bot.tree.command(name="queue", description="View the current conductor queue.")
async def view_queue(interaction: discord.Interaction):
    rows = db.get_conductor_queue()
    if not rows:
        await interaction.response.send_message("The conductor queue is empty.")
        return
    lines = []
    for r in rows:
        time_str = r["preferred_time"] or "no preference"
        lines.append(f"• **{r['name']}** — {time_str}")
    await interaction.response.send_message("**Conductor queue:**\n" + "\n".join(lines))


@bot.tree.command(name="remove-from-queue", description="[Leadership] Remove someone else from the conductor queue.")
@app_commands.describe(member="The member to remove from the conductor queue")
@is_leadership()
async def remove_from_queue(interaction: discord.Interaction, member: discord.Member):
    db.leave_conductor_queue(str(member.id))
    await interaction.response.send_message(f"**{member.display_name}** has been removed from the conductor queue.", ephemeral=True)


# ---------------------------------------------------------------------------
# VIP queue (self-service sign-up)
# ---------------------------------------------------------------------------

@bot.tree.command(name="join-vip-queue", description="Sign up for the VIP rotation.")
async def join_vip_queue(interaction: discord.Interaction):
    db.join_vip_queue(str(interaction.user.id), interaction.user.display_name)
    await interaction.response.send_message("You're on the VIP queue.", ephemeral=True)


@bot.tree.command(name="leave-vip-queue", description="Remove yourself from the VIP queue.")
async def leave_vip_queue(interaction: discord.Interaction):
    db.leave_vip_queue(str(interaction.user.id))
    await interaction.response.send_message("You've been removed from the VIP queue.", ephemeral=True)


@bot.tree.command(name="remove-from-vip-queue", description="[Leadership] Remove someone else from the VIP queue.")
@app_commands.describe(member="The member to remove from the VIP queue")
@is_leadership()
async def remove_from_vip_queue(interaction: discord.Interaction, member: discord.Member):
    db.leave_vip_queue(str(member.id))
    await interaction.response.send_message(f"**{member.display_name}** has been removed from the VIP queue.", ephemeral=True)


@bot.tree.command(name="remove-member", description="[Leadership] Remove someone from both the conductor and VIP rotations, e.g. if they've left the alliance.")
@app_commands.describe(member="The member to remove from both rotations")
@is_leadership()
async def remove_member(interaction: discord.Interaction, member: discord.Member):
    db.leave_conductor_queue(str(member.id))
    db.leave_vip_queue(str(member.id))
    await interaction.response.send_message(
        f"**{member.display_name}** has been removed from both the conductor and VIP rotations.",
        ephemeral=True,
    )


@bot.tree.command(name="vip-queue", description="View the current VIP queue.")
async def view_vip_queue(interaction: discord.Interaction):
    rows = db.get_vip_queue()
    if not rows:
        await interaction.response.send_message("The VIP queue is empty.")
        return
    lines = [f"• **{r['name']}**" for r in rows]
    await interaction.response.send_message("**VIP queue:**\n" + "\n".join(lines))


# ---------------------------------------------------------------------------
# Leadership: live assignment (starts the countdown in-game)
# ---------------------------------------------------------------------------

@bot.tree.command(name="assign-conductor", description="[Leadership] Assign a member as conductor. Starts the countdown now.")
@app_commands.describe(member="The member to assign as conductor")
@is_leadership()
async def assign_conductor(interaction: discord.Interaction, member: discord.Member):
    db.assign_conductor(str(member.id), member.display_name, str(interaction.user.id))
    await interaction.response.send_message(
        f"🚂 **{member.display_name}** has been assigned as conductor — the countdown starts now!\n"
        f"{member.mention}, here's what to do:\n{HOW_TO_CONDUCT_TEXT}"
    )


@bot.tree.command(name="assign-vip", description="[Leadership] Assign a member as VIP for the current train run.")
@app_commands.describe(member="The member to assign as VIP")
@is_leadership()
async def assign_vip(interaction: discord.Interaction, member: discord.Member):
    db.assign_vip(str(member.id), member.display_name, str(interaction.user.id))
    await interaction.response.send_message(f"⭐ **{member.display_name}** has been assigned as VIP for this run.")


# ---------------------------------------------------------------------------
# Leadership: manual backfill (past turns, no countdown, no ping)
# ---------------------------------------------------------------------------

@bot.tree.command(name="log-conductor", description="[Leadership] Log a past conductor turn without starting a countdown.")
@app_commands.describe(member="The member who conducted", date="Date they conducted, format YYYY-MM-DD")
@is_leadership()
async def log_conductor(interaction: discord.Interaction, member: discord.Member, date: str):
    date_iso = parse_date(date)
    if not date_iso:
        await interaction.response.send_message("Please use date format YYYY-MM-DD, e.g. 2026-09-01.", ephemeral=True)
        return
    db.log_conductor(str(member.id), member.display_name, date_iso, str(interaction.user.id))
    await interaction.response.send_message(
        f"Logged **{member.display_name}** as conductor on {date}. No countdown started.", ephemeral=True
    )


@bot.tree.command(name="log-vip", description="[Leadership] Log a past VIP turn without pinging anyone.")
@app_commands.describe(member="The member who was VIP", date="Date they were VIP, format YYYY-MM-DD")
@is_leadership()
async def log_vip(interaction: discord.Interaction, member: discord.Member, date: str):
    date_iso = parse_date(date)
    if not date_iso:
        await interaction.response.send_message("Please use date format YYYY-MM-DD, e.g. 2026-09-01.", ephemeral=True)
        return
    db.log_vip(str(member.id), member.display_name, date_iso, str(interaction.user.id))
    await interaction.response.send_message(
        f"Logged **{member.display_name}** as VIP on {date}.", ephemeral=True
    )


# ---------------------------------------------------------------------------
# History / fairness views
# ---------------------------------------------------------------------------

@bot.tree.command(name="history", description="View a member's conductor/VIP history, or the full roster if no one is specified.")
@app_commands.describe(member="Leave blank to see the full alliance roster sorted by fairness")
async def history(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    if member is None:
        rows = db.get_full_roster_by_fairness()
        if not rows:
            await interaction.response.send_message("No members tracked yet.")
            return
        lines = []
        for r in rows[:25]:
            last = r["last_conducted_at"][:10] if r["last_conducted_at"] else "never"
            lines.append(f"• **{r['name']}** — last conducted: {last}")
        await interaction.response.send_message("**Roster (longest-waiting first):**\n" + "\n".join(lines))
        return

    row, logs = db.get_history_for(str(member.id))
    if row is None:
        await interaction.response.send_message(f"No history yet for **{member.display_name}**.")
        return
    last_c = row["last_conducted_at"][:10] if row["last_conducted_at"] else "never"
    last_v = row["last_vip_at"][:10] if row["last_vip_at"] else "never"
    lines = [f"**{member.display_name}**", f"Last conducted: {last_c}", f"Last VIP: {last_v}", "", "Recent log:"]
    for log in logs:
        tag = " (backfilled)" if log["is_backfill"] else ""
        lines.append(f"• {log['role']} — {log['timestamp'][:10]}{tag}")
    await interaction.response.send_message("\n".join(lines))


# ---------------------------------------------------------------------------
# Instructions
# ---------------------------------------------------------------------------

@bot.tree.command(name="how-to-conduct", description="Show the full steps for conducting the caravan.")
async def how_to_conduct(interaction: discord.Interaction):
    await interaction.response.send_message(HOW_TO_CONDUCT_TEXT)


@bot.tree.command(name="commands", description="List all bot commands.")
async def list_commands(interaction: discord.Interaction):
    await interaction.response.send_message(COMMANDS_TEXT, ephemeral=True)


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("Set DISCORD_BOT_TOKEN in your .env file before running the bot.")
    bot.run(BOT_TOKEN)
