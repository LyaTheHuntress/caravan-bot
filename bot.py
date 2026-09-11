"""
Caravan/train rotation bot for a Last Asylum: Plague alliance.

Manages:
- A persistent conductor rotation and a persistent VIP rotation (members join
  once and stay in rotation forever; being picked doesn't remove them)
- A once-per-server-day pick: at 00:00 server time, the bot decides whose
  turn it is (pure fairness — whoever's waited longest, never-gone members
  first) for both conductor and VIP, and announces it
- A 30-minutes-before reminder ping for whoever was picked, using their
  preferred time
- Leadership-only skip commands, for when someone's already had their turn
  without the bot knowing (e.g. an out-of-band in-game assignment)
- Live conductor/VIP assignment (leadership-only manual override)
- Manual backfill/history logging for turns that already happened
- Automatic removal from both rotations when a member leaves the Discord server
- A reusable "how to conduct" instructions command

Leadership-only commands are gated by a Discord role, set via LEADERSHIP_ROLE_NAME below.
"""

import os
import random
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
    "clock, not your local time zone — the bot only understands server time). Every server day "
    "at 00:00, the bot picks whoever's turn it is and announces it, then pings Leadership again "
    "30 minutes before that person's preferred time so they can assign it in-game. That's the "
    "only step you need to take ahead of time.\n\n"
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
    "You can set **one VIP member**, who gets extra rewards. VIP is picked the same way as "
    "the conductor — the bot announces it at server midnight. A few things about how it works "
    "in-game: sending the invite starts a countdown (defaults to 60 minutes if they're offline, "
    "shorter if they're online) — if they don't accept in time, the invite opens back up so you "
    "can pick someone else. VIP and Guard are mutually exclusive; you can only assign one or the "
    "other, not both. Once the VIP accepts, it's the **conductor** who picks which 2 of the 4 "
    "wagon slots their rewards come from — not the VIP. If the picked VIP doesn't accept and "
    "you don't want to wait, leadership can run `/skip-vip` to move on to the next person in "
    "rotation.\n\n"
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
    "`/join-queue time:HH:MM` — join the conductor rotation with your preferred server time (required)\n"
    "`/leave-queue` — remove yourself from the conductor rotation\n"
    "`/queue` — view the conductor rotation, soonest-turn first\n"
    "`/join-vip-queue` — join the VIP rotation\n"
    "`/leave-vip-queue` — remove yourself from the VIP rotation\n"
    "`/vip-queue` — view the VIP rotation, soonest-turn first\n"
    "`/history [member]` — view your history, someone else's, or the full roster if left blank\n"
    "`/how-to-conduct` — full instructions for running the caravan\n"
    "`/commands` — show this list\n\n"
    "**Leadership only:**\n"
    "`/assign-conductor @member` — manually assign the conductor, overriding today's pick\n"
    "`/assign-vip @member` — manually assign the VIP, overriding today's pick\n"
    "`/skip-conductor` — today's conductor already had their turn; pick the next in rotation\n"
    "`/skip-vip` — today's VIP already had their turn; pick the next in rotation\n"
    "`/remove-from-queue @member` — remove someone else from the conductor rotation\n"
    "`/remove-from-vip-queue @member` — remove someone else from the VIP rotation\n"
    "`/remove-member @member` — remove someone from both rotations at once (e.g. they left the alliance)\n"
    "`/log-conductor @member date:YYYY-MM-DD` — backfill a past conductor turn (no countdown)\n"
    "`/log-vip @member date:YYYY-MM-DD` — backfill a past VIP turn"
)

SKIP_CONDUCTOR_LINES = [
    "🚨 Leadership says **{old}** already had their turn — skipping them.",
    "🕵️ Turns out **{old}** already ran the caravan. Moving right along.",
    "⏭️ **{old}** already had a go, apparently. Next up:",
]

SKIP_VIP_LINES = [
    "🚨 Leadership says **{old}** already got the VIP treatment — skipping them.",
    "🕵️ Turns out **{old}** already had a VIP turn. Moving right along.",
    "⏭️ **{old}** already had their VIP moment, apparently. Next up:",
]

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


async def get_leadership_channel_and_mention():
    """Returns (channel, role_mention) or (None, None) if not configured/found."""
    if not LEADERSHIP_CHANNEL_ID:
        return None, None
    channel = bot.get_channel(int(LEADERSHIP_CHANNEL_ID))
    if channel is None:
        print(f"LEADERSHIP_CHANNEL_ID {LEADERSHIP_CHANNEL_ID} not found or not accessible.")
        return None, None
    leadership_role = None
    if isinstance(channel, discord.TextChannel) and channel.guild:
        leadership_role = discord.utils.get(channel.guild.roles, name=LEADERSHIP_ROLE_NAME)
    role_mention = leadership_role.mention if leadership_role else f"@{LEADERSHIP_ROLE_NAME}"
    return channel, role_mention


@tasks.loop(minutes=1)
async def daily_caravan_check():
    channel, role_mention = await get_leadership_channel_and_mention()
    if channel is None:
        return

    server_now = game_server_now()
    current_time_str = server_now.strftime("%H:%M")
    today_str = server_now.strftime("%Y-%m-%d")

    # --- Phase 1: at server midnight, decide whose turn it is for the day ---
    # Selection is pure fairness across the whole rotation — not tied to
    # anyone's individual preferred time. The preferred time only controls
    # when the later reminder ping goes out.
    if current_time_str == "00:00" and db.get_todays_run(today_str) is None:
        conductor, vip = db.start_daily_run(today_str)
        if conductor is None:
            await channel.send(
                f"{role_mention} — no one is currently in the conductor rotation. "
                f"Have someone run `/join-queue` to get today's caravan moving."
            )
        else:
            conductor_mention = f"<@{conductor['discord_id']}>"
            vip_line = f"VIP for today: <@{vip['discord_id']}>." if vip else "No one is currently in the VIP rotation."
            await channel.send(
                f"{role_mention} — today's conductor is {conductor_mention}, scheduled for "
                f"**{conductor['preferred_time']}** server time. {vip_line}"
            )

    # --- Phase 2: 30-minutes-before reminder for whoever was already picked ---
    run = db.get_todays_run(today_str)
    if run is None or run["conductor_discord_id"] is None or run["reminder_sent_at"]:
        return

    conductor_entry = db.get_conductor_entry(run["conductor_discord_id"])
    if conductor_entry is None or conductor_entry["preferred_time"] is None:
        return
    preferred = parse_hhmm(conductor_entry["preferred_time"])
    if preferred is None:
        return
    trigger = (preferred - timedelta(minutes=30)).strftime("%H:%M")
    if trigger != current_time_str:
        return

    vip_line = f"VIP for this run: <@{run['vip_discord_id']}>." if run["vip_discord_id"] else "No one is currently in the VIP rotation."

    await channel.send(
        f"{role_mention} — <@{run['conductor_discord_id']}> is set to run the caravan at "
        f"**{conductor_entry['preferred_time']}** server time (starting in 30 minutes).\n{vip_line}"
    )
    db.mark_reminder_sent(today_str)


@bot.event
async def on_ready():
    db.init_db()
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} commands. Logged in as {bot.user}.")
    except Exception as e:
        print(f"Command sync failed: {e}")
    if not daily_caravan_check.is_running():
        daily_caravan_check.start()


@bot.event
async def on_member_remove(member: discord.Member):
    # Auto-clean both rotations when someone leaves the Discord server, so
    # leadership never has to manually edit the lists for that.
    db.leave_conductor_queue(str(member.id))
    db.leave_vip_queue(str(member.id))


# ---------------------------------------------------------------------------
# Conductor rotation (self-service sign-up, persistent)
# ---------------------------------------------------------------------------

@bot.tree.command(name="join-queue", description="Join the conductor rotation with your preferred server time.")
@app_commands.describe(time="Your preferred server time, e.g. 19:00 (required)")
async def join_queue(interaction: discord.Interaction, time: str):
    if parse_hhmm(time) is None:
        await interaction.response.send_message(
            "You need to include a preferred server time in `HH:MM` format (e.g. `19:00`) so the "
            "bot knows when to remind leadership about your run. Try again, e.g. `/join-queue time:19:00`.",
            ephemeral=True,
        )
        return
    db.join_conductor_queue(str(interaction.user.id), interaction.user.display_name, time)
    await interaction.response.send_message(
        f"You're in the conductor rotation, preferred time **{time}**.", ephemeral=True
    )


@bot.tree.command(name="leave-queue", description="Remove yourself from the conductor rotation.")
async def leave_queue(interaction: discord.Interaction):
    db.leave_conductor_queue(str(interaction.user.id))
    await interaction.response.send_message("You've been removed from the conductor rotation.", ephemeral=True)


@bot.tree.command(name="queue", description="View the conductor rotation.")
async def view_queue(interaction: discord.Interaction):
    rows = db.get_conductor_queue()
    if not rows:
        await interaction.response.send_message("The conductor rotation is empty.")
        return
    lines = [f"• **{r['name']}** — {r['preferred_time']}" for r in rows]
    await interaction.response.send_message("**Conductor rotation (soonest turn first):**\n" + "\n".join(lines))


@bot.tree.command(name="remove-from-queue", description="[Leadership] Remove someone else from the conductor rotation.")
@app_commands.describe(member="The member to remove from the conductor rotation")
@is_leadership()
async def remove_from_queue(interaction: discord.Interaction, member: discord.Member):
    db.leave_conductor_queue(str(member.id))
    await interaction.response.send_message(f"**{member.display_name}** has been removed from the conductor rotation.", ephemeral=True)


# ---------------------------------------------------------------------------
# VIP rotation (self-service sign-up, persistent)
# ---------------------------------------------------------------------------

@bot.tree.command(name="join-vip-queue", description="Join the VIP rotation.")
async def join_vip_queue(interaction: discord.Interaction):
    db.join_vip_queue(str(interaction.user.id), interaction.user.display_name)
    await interaction.response.send_message("You're in the VIP rotation.", ephemeral=True)


@bot.tree.command(name="leave-vip-queue", description="Remove yourself from the VIP rotation.")
async def leave_vip_queue(interaction: discord.Interaction):
    db.leave_vip_queue(str(interaction.user.id))
    await interaction.response.send_message("You've been removed from the VIP rotation.", ephemeral=True)


@bot.tree.command(name="remove-from-vip-queue", description="[Leadership] Remove someone else from the VIP rotation.")
@app_commands.describe(member="The member to remove from the VIP rotation")
@is_leadership()
async def remove_from_vip_queue(interaction: discord.Interaction, member: discord.Member):
    db.leave_vip_queue(str(member.id))
    await interaction.response.send_message(f"**{member.display_name}** has been removed from the VIP rotation.", ephemeral=True)


@bot.tree.command(name="remove-member", description="[Leadership] Remove someone from both rotations at once (e.g. they left the alliance).")
@app_commands.describe(member="The member to remove from both rotations")
@is_leadership()
async def remove_member(interaction: discord.Interaction, member: discord.Member):
    db.leave_conductor_queue(str(member.id))
    db.leave_vip_queue(str(member.id))
    await interaction.response.send_message(
        f"**{member.display_name}** has been removed from both the conductor and VIP rotations.",
        ephemeral=True,
    )


@bot.tree.command(name="vip-queue", description="View the VIP rotation.")
async def view_vip_queue(interaction: discord.Interaction):
    rows = db.get_vip_queue()
    if not rows:
        await interaction.response.send_message("The VIP rotation is empty.")
        return
    lines = [f"• **{r['name']}**" for r in rows]
    await interaction.response.send_message("**VIP rotation (soonest turn first):**\n" + "\n".join(lines))


# ---------------------------------------------------------------------------
# Leadership: live assignment (manual override) and skip
# ---------------------------------------------------------------------------

@bot.tree.command(name="assign-conductor", description="[Leadership] Manually assign the conductor, overriding today's pick.")
@app_commands.describe(member="The member to assign as conductor")
@is_leadership()
async def assign_conductor(interaction: discord.Interaction, member: discord.Member):
    db.assign_conductor(str(member.id), member.display_name, str(interaction.user.id))
    run_day = game_server_now().strftime("%Y-%m-%d")
    db.record_manual_conductor(run_day, str(member.id))
    await interaction.response.send_message(
        f"🚂 **{member.display_name}** has been assigned as conductor — the countdown starts now!\n"
        f"{member.mention}, here's what to do:\n{HOW_TO_CONDUCT_TEXT}"
    )


@bot.tree.command(name="assign-vip", description="[Leadership] Manually assign the VIP, overriding today's pick.")
@app_commands.describe(member="The member to assign as VIP")
@is_leadership()
async def assign_vip(interaction: discord.Interaction, member: discord.Member):
    db.assign_vip(str(member.id), member.display_name, str(interaction.user.id))
    run_day = game_server_now().strftime("%Y-%m-%d")
    db.record_manual_vip(run_day, str(member.id))
    await interaction.response.send_message(f"⭐ **{member.display_name}** has been assigned as VIP for this run.")


@bot.tree.command(name="skip-conductor", description="[Leadership] Today's conductor already had their turn — pick the next in rotation.")
@is_leadership()
async def skip_conductor_cmd(interaction: discord.Interaction):
    today_str = game_server_now().strftime("%Y-%m-%d")
    old_name, new_conductor = db.skip_conductor(today_str)
    if old_name is None:
        await interaction.response.send_message("No conductor has been picked for today yet — nothing to skip.", ephemeral=True)
        return
    if new_conductor is None:
        await interaction.response.send_message(
            f"Tried to skip **{old_name}**, but there's no one else left in the conductor rotation to pick instead.",
            ephemeral=True,
        )
        return
    line = random.choice(SKIP_CONDUCTOR_LINES).format(old=old_name)
    await interaction.response.send_message(
        f"{line}\n🚂 New conductor for today: **{new_conductor['name']}**, scheduled for "
        f"**{new_conductor['preferred_time']}** server time. VIP stays the same."
    )


@bot.tree.command(name="skip-vip", description="[Leadership] Today's VIP already had their turn — pick the next in rotation.")
@is_leadership()
async def skip_vip_cmd(interaction: discord.Interaction):
    today_str = game_server_now().strftime("%Y-%m-%d")
    old_name, new_vip = db.skip_vip(today_str)
    if old_name is None:
        await interaction.response.send_message("No VIP has been picked for today yet — nothing to skip.", ephemeral=True)
        return
    if new_vip is None:
        await interaction.response.send_message(
            f"Tried to skip **{old_name}**, but there's no one else left in the VIP rotation to pick instead.",
            ephemeral=True,
        )
        return
    line = random.choice(SKIP_VIP_LINES).format(old=old_name)
    await interaction.response.send_message(
        f"{line}\n⭐ New VIP for today: **{new_vip['name']}**. Conductor stays the same."
    )


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
