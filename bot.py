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
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

import database as db

load_dotenv()

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
LEADERSHIP_ROLE_NAME = os.getenv("LEADERSHIP_ROLE_NAME", "Leadership")

HOW_TO_CONDUCT_TEXT = (
    "**How to conduct the train:**\n"
    "1. Refresh the train **5 times** to upgrade it to gold tier.\n"
    "2. Set your **defenses** before the countdown ends.\n"
    "3. You're now the conductor — good luck!\n\n"
    "As conductor you can also assign a **VIP**, who gets extra rewards. "
    "Leadership will let you know if there's a VIP queued up."
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


def parse_date(date_str: str) -> str | None:
    """Accepts YYYY-MM-DD, returns an ISO timestamp (midnight UTC) or None if invalid."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        return None


@bot.event
async def on_ready():
    db.init_db()
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} commands. Logged in as {bot.user}.")
    except Exception as e:
        print(f"Command sync failed: {e}")


# ---------------------------------------------------------------------------
# Conductor queue (self-service sign-up)
# ---------------------------------------------------------------------------

@bot.tree.command(name="join-queue", description="Sign up for the conductor rotation with your preferred server time.")
@app_commands.describe(time="Your preferred server time, e.g. 19:00")
async def join_queue(interaction: discord.Interaction, time: str | None = None):
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
async def history(interaction: discord.Interaction, member: discord.Member | None = None):
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

@bot.tree.command(name="how-to-conduct", description="Show the steps for conducting the train.")
async def how_to_conduct(interaction: discord.Interaction):
    await interaction.response.send_message(HOW_TO_CONDUCT_TEXT)


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("Set DISCORD_BOT_TOKEN in your .env file before running the bot.")
    bot.run(BOT_TOKEN)
