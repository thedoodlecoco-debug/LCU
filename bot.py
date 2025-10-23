# bot.py
import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
import json
import asyncio
import time
from datetime import datetime, timedelta
from dotenv import load_dotenv

# ---- CONFIG & ENV ----
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID")) if os.getenv("LOG_CHANNEL_ID") else None
# prefix kept for backwards compatibility but not used for slash commands
PREFIX = os.getenv("BOT_PREFIX", "/")

# ---- INTENTS ----
intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.message_content = True

# Using commands.Bot so we can reuse some helpers if needed
bot = commands.Bot(command_prefix=PREFIX, intents=intents)
tree = bot.tree

# ---- DATA STORE ----
DATA_FILE = "security_data.json"
if not os.path.exists(DATA_FILE):
    DATA = {
        "warnings": {},
        "jails": {},
        "backups": {},
        "tags": {},
        "config": {}
    }
    with open(DATA_FILE, "w") as f:
        json.dump(DATA, f, indent=2)
else:
    with open(DATA_FILE, "r") as f:
        DATA = json.load(f)

def save_data():
    with open(DATA_FILE, "w") as f:
        json.dump(DATA, f, indent=2)

# ---- LOGGING UTILITY ----
async def log(guild: discord.Guild, text: str):
    if not guild:
        return
    if LOG_CHANNEL_ID:
        ch = guild.get_channel(LOG_CHANNEL_ID) or bot.get_channel(LOG_CHANNEL_ID)
        if ch:
            try:
                await ch.send(f"[{datetime.utcnow().isoformat()} UTC] {text}")
            except Exception:
                # ignore send errors to log channel
                pass

# ---- ANTI-SPAM ----
SPAM_TRACK = {}  # {guild_id: {user_id: [timestamps]}}
SPAM_THRESHOLD = int(os.getenv("SPAM_THRESHOLD", "6"))
SPAM_WINDOW = int(os.getenv("SPAM_WINDOW", "8"))

@tasks.loop(seconds=15.0)
async def anti_spam_cleaner():
    now = time.time()
    for guild_id, user_map in list(SPAM_TRACK.items()):
        for uid, stamps in list(user_map.items()):
            SPAM_TRACK[guild_id][uid] = [s for s in stamps if now - s <= SPAM_WINDOW]
            if not SPAM_TRACK[guild_id][uid]:
                del SPAM_TRACK[guild_id][uid]

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")
    anti_spam_cleaner.start()
    try:
        synced = await tree.sync()
        print(f"Synced {len(synced)} commands.")
    except Exception as e:
        print("Slash command sync error:", e)

@bot.event
async def on_message(message: discord.Message):
    # keep the anti-spam working for text messages
    if message.author.bot or not message.guild:
        return
    gid = message.guild.id
    uid = message.author.id
    now = time.time()
    SPAM_TRACK.setdefault(gid, {}).setdefault(uid, []).append(now)
    # prune old
    SPAM_TRACK[gid][uid] = [t for t in SPAM_TRACK[gid][uid] if now - t <= SPAM_WINDOW]
    if len(SPAM_TRACK[gid][uid]) >= SPAM_THRESHOLD:
        role = discord.utils.get(message.guild.roles, name="Muted")
        if not role:
            try:
                role = await message.guild.create_role(name="Muted", permissions=discord.Permissions(send_messages=False))
                for c in message.guild.channels:
                    await c.set_permissions(role, send_messages=False)
            except Exception:
                role = None
        if role:
            try:
                await message.author.add_roles(role, reason="Auto anti-spam")
                await message.channel.send(f"{message.author.mention} muted for spamming.")
                await log(message.guild, f"Auto-muted {message.author} for spam.")
            except Exception:
                pass
        SPAM_TRACK[gid][uid] = []
    # important: don't call process_commands for slash-based flow
    # but keep it so text commands (if any) would still work
    await bot.process_commands(message)

# ---- MOD CHECK (for slash commands) ----
def is_mod():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False
        perms = interaction.user.guild_permissions
        return perms.manage_guild or perms.administrator
    return app_commands.check(predicate)

# ---- STORAGE FOR ANTIRAID/INVITE ETC. ----
INVITE_BLOCK = {}   # gid -> bool
ANTI_RAID = {}      # gid -> bool
SAFE_MODE = {}      # gid -> bool
WHITELIST = {}      # gid -> set of tuples (entity, id)
BLACKLIST = {}      # gid -> set of tuples (entity, id)

# -----------------------------
# ---- FULL SLASH COMMANDS ----
# -----------------------------

# HELP & UTIL
@tree.command(name="help", description="List all slash commands")
async def help_cmd(interaction: discord.Interaction):
    cmds = [c.name for c in tree.walk_commands()]
    await interaction.response.send_message(f"Commands ({len(cmds)}): " + ", ".join(sorted(cmds)), ephemeral=True)

@tree.command(name="ping", description="Check bot latency")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! {round(bot.latency * 1000)}ms")

# PREFIX (kept for compatibility, note slash commands don't need prefix)
@tree.command(name="setprefix", description="Set bot prefix (legacy)")
@is_mod()
@app_commands.describe(new="New prefix to use (legacy)")
async def setprefix(interaction: discord.Interaction, new: str):
    global PREFIX
    PREFIX = new
    await interaction.response.send_message(f"Prefix set to `{new}` (slash commands unaffected).", ephemeral=True)

# BAN / TEMPBAN / UNBAN
@tree.command(name="ban", description="Ban a member")
@is_mod()
@app_commands.describe(member="Member to ban", reason="Reason for ban")
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason"):
    await interaction.response.defer(ephemeral=True)
    try:
        await member.ban(reason=reason)
        await interaction.followup.send(f"Banned {member} — {reason}", ephemeral=True)
        await log(interaction.guild, f"{interaction.user} banned {member} — {reason}")
    except Exception as e:
        await interaction.followup.send(f"Failed to ban: {e}", ephemeral=True)

@tree.command(name="tempban", description="Temporarily ban a member (seconds)")
@is_mod()
@app_commands.describe(member="Member to ban", duration="Duration in seconds", reason="Reason")
async def tempban(interaction: discord.Interaction, member: discord.Member, duration: int, reason: str = "No reason"):
    await interaction.response.defer(ephemeral=True)
    try:
        await member.ban(reason=reason)
        await interaction.followup.send(f"Temporarily banned {member} for {duration}s", ephemeral=True)
        await log(interaction.guild, f"{interaction.user} tempbanned {member} for {duration}s — {reason}")
        await asyncio.sleep(duration)
        try:
            await interaction.guild.unban(discord.Object(id=member.id))
            # followup may fail if permissions changed; swallow errors
            try:
                await interaction.followup.send(f"Auto-unbanned {member}.", ephemeral=True)
            except:
                pass
        except Exception:
            pass
    except Exception as e:
        await interaction.followup.send(f"Failed to tempban: {e}", ephemeral=True)

@tree.command(name="unban", description="Unban a user by ID")
@is_mod()
@app_commands.describe(user_id="User ID to unban")
async def unban(interaction: discord.Interaction, user_id: int):
    await interaction.response.defer(ephemeral=True)
    try:
        user = discord.Object(id=user_id)
        await interaction.guild.unban(user)
        await interaction.followup.send(f"Unbanned {user_id}", ephemeral=True)
        await log(interaction.guild, f"{interaction.user} unbanned {user_id}")
    except Exception as e:
        await interaction.followup.send(f"Failed to unban: {e}", ephemeral=True)

# KICK / SOFTBAN
@tree.command(name="kick", description="Kick a member")
@is_mod()
@app_commands.describe(member="Member to kick", reason="Reason")
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason"):
    await interaction.response.defer(ephemeral=True)
    try:
        await member.kick(reason=reason)
        await interaction.followup.send(f"Kicked {member} — {reason}", ephemeral=True)
        await log(interaction.guild, f"{interaction.user} kicked {member} — {reason}")
    except Exception as e:
        await interaction.followup.send(f"Failed to kick: {e}", ephemeral=True)

@tree.command(name="softban", description="Softban (ban->unban) to purge messages")
@is_mod()
@app_commands.describe(member="Member to softban", reason="Reason")
async def softban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason"):
    await interaction.response.defer(ephemeral=True)
    try:
        await member.ban(reason=reason)
        await interaction.guild.unban(discord.Object(id=member.id))
        await interaction.followup.send(f"Softbanned {member}", ephemeral=True)
        await log(interaction.guild, f"{interaction.user} softbanned {member} — {reason}")
    except Exception as e:
        await interaction.followup.send(f"Failed to softban: {e}", ephemeral=True)

# MUTE / UNMUTE / TEMPMUTE
async def _ensure_muted_role(guild: discord.Guild) -> discord.Role:
    role = discord.utils.get(guild.roles, name="Muted")
    if role:
        return role
    perms = discord.Permissions(send_messages=False, speak=False)
    role = await guild.create_role(name="Muted", permissions=perms)
    for c in guild.channels:
        try:
            await c.set_permissions(role, send_messages=False, speak=False)
        except Exception:
            pass
    return role

@tree.command(name="mute", description="Mute (role-based) a member")
@is_mod()
@app_commands.describe(member="Member to mute", reason="Reason")
async def mute(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason"):
    await interaction.response.defer(ephemeral=True)
    try:
        role = await _ensure_muted_role(interaction.guild)
        await member.add_roles(role, reason=reason)
        await interaction.followup.send(f"Muted {member}", ephemeral=True)
        await log(interaction.guild, f"{interaction.user} muted {member} — {reason}")
    except Exception as e:
        await interaction.followup.send(f"Failed to mute: {e}", ephemeral=True)

@tree.command(name="unmute", description="Unmute a member")
@is_mod()
@app_commands.describe(member="Member to unmute")
async def unmute(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)
    try:
        role = discord.utils.get(interaction.guild.roles, name="Muted")
        if role:
            await member.remove_roles(role)
        await interaction.followup.send(f"Unmuted {member}", ephemeral=True)
        await log(interaction.guild, f"{interaction.user} unmuted {member}")
    except Exception as e:
        await interaction.followup.send(f"Failed to unmute: {e}", ephemeral=True)

@tree.command(name="tempmute", description="Temporarily mute a member")
@is_mod()
@app_commands.describe(member="Member", seconds="Seconds", reason="Reason")
async def tempmute(interaction: discord.Interaction, member: discord.Member, seconds: int, reason: str = "No reason"):
    await interaction.response.defer(ephemeral=True)
    try:
        role = await _ensure_muted_role(interaction.guild)
        await member.add_roles(role, reason=reason)
        await interaction.followup.send(f"Temporarily muted {member} for {seconds}s", ephemeral=True)
        await log(interaction.guild, f"{interaction.user} tempmuted {member} for {seconds}s — {reason}")
        await asyncio.sleep(seconds)
        try:
            await member.remove_roles(role)
        except Exception:
            pass
    except Exception as e:
        await interaction.followup.send(f"Failed to tempmute: {e}", ephemeral=True)

# WARN / WARNINGS / CLEARWARNS
@tree.command(name="warn", description="Warn a member")
@is_mod()
@app_commands.describe(member="Member", reason="Reason")
async def warn_cmd(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason"):
    await interaction.response.defer(ephemeral=True)
    try:
        gid = str(interaction.guild.id)
        DATA.setdefault("warnings", {})
        DATA["warnings"].setdefault(gid, {})
        DATA["warnings"][gid].setdefault(str(member.id), [])
        DATA["warnings"][gid][str(member.id)].append({"by": interaction.user.id, "reason": reason, "time": time.time()})
        save_data()
        await interaction.followup.send(f"Warned {member} — {reason}", ephemeral=True)
        await log(interaction.guild, f"{interaction.user} warned {member} — {reason}")
    except Exception as e:
        await interaction.followup.send(f"Failed to warn: {e}", ephemeral=True)

@tree.command(name="warnings", description="Show warnings for a member")
@is_mod()
@app_commands.describe(member="Member")
async def warnings_cmd(interaction: discord.Interaction, member: discord.Member):
    gid = str(interaction.guild.id)
    warns = DATA.get("warnings", {}).get(gid, {}).get(str(member.id), [])
    if not warns:
        await interaction.response.send_message(f"No warnings for {member}", ephemeral=True)
        return
    out = "\n".join([f"{i+1}. By {w['by']} — {w['reason']}" for i, w in enumerate(warns)])
    await interaction.response.send_message(f"Warnings for {member}:\n{out}", ephemeral=True)

@tree.command(name="clearwarns", description="Clear warnings for a member")
@is_mod()
@app_commands.describe(member="Member")
async def clearwarns_cmd(interaction: discord.Interaction, member: discord.Member):
    gid = str(interaction.guild.id)
    DATA.get("warnings", {}).get(gid, {}).pop(str(member.id), None)
    save_data()
    await interaction.response.send_message(f"Cleared warnings for {member}", ephemeral=True)

# PURGE (bulk delete)
@tree.command(name="purge", description="Delete a number of messages from the channel")
@is_mod()
@app_commands.describe(amount="Number of messages to delete")
async def purge_cmd(interaction: discord.Interaction, amount: int = 50):
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await interaction.channel.purge(limit=amount)
        await interaction.followup.send(f"Deleted {len(deleted)} messages.", ephemeral=True)
        await log(interaction.guild, f"{interaction.user} purged {len(deleted)} messages in {interaction.channel}")
    except Exception as e:
        await interaction.followup.send(f"Failed to purge: {e}", ephemeral=True)

# LOCK / UNLOCK
@tree.command(name="lock", description="Lock a channel (deny @everyone send_messages)")
@is_mod()
@app_commands.describe(channel="Channel to lock (optional)")
async def lock_cmd(interaction: discord.Interaction, channel: discord.TextChannel = None):
    ch = channel or interaction.channel
    await interaction.response.defer(ephemeral=True)
    try:
        await ch.set_permissions(interaction.guild.default_role, send_messages=False)
        await interaction.followup.send(f"Locked {ch.mention}", ephemeral=True)
        await log(interaction.guild, f"{interaction.user} locked {ch}")
    except Exception as e:
        await interaction.followup.send(f"Failed to lock: {e}", ephemeral=True)

@tree.command(name="unlock", description="Unlock a channel (allow @everyone send_messages)")
@is_mod()
@app_commands.describe(channel="Channel to unlock (optional)")
async def unlock_cmd(interaction: discord.Interaction, channel: discord.TextChannel = None):
    ch = channel or interaction.channel
    await interaction.response.defer(ephemeral=True)
    try:
        await ch.set_permissions(interaction.guild.default_role, send_messages=True)
        await interaction.followup.send(f"Unlocked {ch.mention}", ephemeral=True)
        await log(interaction.guild, f"{interaction.user} unlocked {ch}")
    except Exception as e:
        await interaction.followup.send(f"Failed to unlock: {e}", ephemeral=True)

# SLOWMODE
@tree.command(name="slowmode", description="Set slowmode for a channel")
@is_mod()
@app_commands.describe(seconds="Slowmode delay in seconds", channel="Channel (optional)")
async def slowmode_cmd(interaction: discord.Interaction, seconds: int, channel: discord.TextChannel = None):
    ch = channel or interaction.channel
    await interaction.response.defer(ephemeral=True)
    try:
        await ch.edit(slowmode_delay=seconds)
        await interaction.followup.send(f"Set slowmode to {seconds}s in {ch.mention}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Failed to set slowmode: {e}", ephemeral=True)

# NUKE (reset channel)
@tree.command(name="nuke", description="Reset a channel (clone -> delete -> recreate)")
@is_mod()
@app_commands.describe(channel="Channel to nuke (optional)")
async def nuke_cmd(interaction: discord.Interaction, channel: discord.TextChannel = None):
    ch = channel or interaction.channel
    await interaction.response.defer(ephemeral=True)
    try:
        new = await ch.clone(reason=f"Nuked by {interaction.user}")
        await ch.delete()
        await new.send("This channel was nuked (reset) by staff.")
        await interaction.followup.send(f"Nuked {ch.mention} -> {new.mention}", ephemeral=True)
        await log(interaction.guild, f"{interaction.user} nuked {ch} -> {new}")
    except Exception as e:
        await interaction.followup.send(f"Failed to nuke: {e}", ephemeral=True)

# MASSBAN (IDs passed as comma-separated string)
@tree.command(name="massban", description="Massban users by comma-separated IDs")
@is_mod()
@app_commands.describe(user_ids="Comma-separated user IDs to ban (e.g. 123,456,789)")
async def massban_cmd(interaction: discord.Interaction, user_ids: str):
    await interaction.response.defer(ephemeral=True)
    ids = [s.strip() for s in user_ids.replace(",", " ").split()]
    count = 0
    for idstr in ids:
        if not idstr.isdigit():
            continue
        uid = int(idstr)
        try:
            await interaction.guild.ban(discord.Object(id=uid), reason=f"Massban by {interaction.user}")
            count += 1
        except Exception:
            pass
    await interaction.followup.send(f"Massbanned {count} users.", ephemeral=True)
    await log(interaction.guild, f"{interaction.user} massbanned {count} users.")

# INVITEBLOCK toggle
@tree.command(name="inviteblock", description="Toggle invite link filtering on/off")
@is_mod()
@app_commands.describe(toggle="on or off")
async def inviteblock_cmd(interaction: discord.Interaction, toggle: str = "on"):
    gid = str(interaction.guild.id)
    INVITE_BLOCK[gid] = toggle.lower() in ("on", "1", "true", "yes")
    await interaction.response.send_message(f"Invite link filtering set to {INVITE_BLOCK[gid]}", ephemeral=True)
    await log(interaction.guild, f"{interaction.user} set inviteblock to {INVITE_BLOCK[gid]}")

# ON MESSAGE DELETE / EDIT logging (kept as events)
@bot.event
async def on_message_delete(message: discord.Message):
    if message.guild:
        await log(message.guild, f"Message deleted in {message.channel}: {message.content}")

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.guild:
        await log(before.guild, f"Message edited in {before.channel} by {before.author}: `{before.content}` -> `{after.content}`")

# ANTIRAID toggle
@tree.command(name="antiraid", description="Toggle anti-raid mode")
@is_mod()
@app_commands.describe(mode="on or off")
async def antiraid_cmd(interaction: discord.Interaction, mode: str = "on"):
    gid = str(interaction.guild.id)
    ANTI_RAID[gid] = mode.lower() in ("on", "1", "true", "yes")
    await interaction.response.send_message(f"Anti-raid: {ANTI_RAID[gid]}", ephemeral=True)

# ANTISPAM (set threshold/window)
@tree.command(name="antispam", description="Configure anti-spam (threshold, window)")
@is_mod()
@app_commands.describe(threshold="Messages count", window="Window in seconds")
async def antispam_cmd(interaction: discord.Interaction, threshold: int = 6, window: int = 8):
    global SPAM_THRESHOLD, SPAM_WINDOW
    SPAM_THRESHOLD = threshold
    SPAM_WINDOW = window
    await interaction.response.send_message(f"Anti-spam set: {threshold} msgs / {window}s", ephemeral=True)

# SETLOG
@tree.command(name="setlog", description="Set log channel for the bot")
@is_mod()
@app_commands.describe(channel="Text channel to receive logs")
async def setlog_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
    global LOG_CHANNEL_ID
    LOG_CHANNEL_ID = channel.id
    await interaction.response.send_message(f"Log channel set to {channel.mention}", ephemeral=True)

# FETCH user info
@tree.command(name="fetch", description="Fetch user info")
@is_mod()
@app_commands.describe(member="Member to fetch")
async def fetch_cmd(interaction: discord.Interaction, member: discord.Member):
    joined = member.joined_at.strftime("%Y-%m-%d %H:%M:%S") if member.joined_at else "Unknown"
    e = discord.Embed(title=str(member), description=f"ID: {member.id}")
    e.add_field(name="Joined", value=joined)
    e.add_field(name="Bot?", value=str(member.bot))
    await interaction.response.send_message(embed=e, ephemeral=True)

# WHOIS
@tree.command(name="whois", description="Show user roles and info")
@app_commands.describe(member="Member (optional)")
async def whois_cmd(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    roles = ", ".join([r.name for r in member.roles if r.name != "@everyone"])
    await interaction.response.send_message(f"{member} — Roles: {roles}", ephemeral=True)

# LOCKROLE (placeholder action)
@tree.command(name="lockrole", description="Placeholder to lock role changes (no-op)")
@is_mod()
@app_commands.describe(role="Role to lock")
async def lockrole_cmd(interaction: discord.Interaction, role: discord.Role):
    await interaction.response.send_message(f"Role management lock engaged for {role.name} (placeholder).", ephemeral=True)

# BACKUP / RESTORE
@tree.command(name="backup", description="Create a basic JSON backup of roles and channels")
@is_mod()
async def backup_cmd(interaction: discord.Interaction):
    gid = str(interaction.guild.id)
    DATA["backups"][gid] = {
        "name": interaction.guild.name,
        "roles": [{"name": r.name, "perms": r.permissions.value} for r in interaction.guild.roles],
        "channels": [{"name": c.name, "type": str(c.type)} for c in interaction.guild.channels]
    }
    save_data()
    await interaction.response.send_message("Backup saved.", ephemeral=True)

@tree.command(name="restore", description="Restore from backup (placeholder, destructive — manual confirm required)")
@is_mod()
async def restore_cmd(interaction: discord.Interaction):
    gid = str(interaction.guild.id)
    b = DATA.get("backups", {}).get(gid)
    if not b:
        await interaction.response.send_message("No backup for this guild.", ephemeral=True)
        return
    await interaction.response.send_message("Restore is a destructive op — do it manually. (placeholder)", ephemeral=True)

# ROLE / CHANNEL MANAGEMENT
@tree.command(name="createrole", description="Create a role")
@is_mod()
@app_commands.describe(name="Name of new role")
async def createrole_cmd(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)
    try:
        await interaction.guild.create_role(name=name)
        await interaction.followup.send(f"Created role `{name}`", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Failed to create role: {e}", ephemeral=True)

@tree.command(name="deleterole", description="Delete a role")
@is_mod()
@app_commands.describe(role="Role to delete")
async def deleterole_cmd(interaction: discord.Interaction, role: discord.Role):
    await interaction.response.defer(ephemeral=True)
    try:
        await role.delete()
        await interaction.followup.send(f"Deleted role {role.name}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Failed to delete role: {e}", ephemeral=True)

@tree.command(name="createchannel", description="Create a text channel")
@is_mod()
@app_commands.describe(name="Channel name")
async def createchannel_cmd(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)
    try:
        await interaction.guild.create_text_channel(name)
        await interaction.followup.send(f"Created channel {name}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Failed to create channel: {e}", ephemeral=True)

@tree.command(name="deletechannel", description="Delete a channel")
@is_mod()
@app_commands.describe(channel="Channel to delete")
async def deletechannel_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    try:
        await channel.delete()
        await interaction.followup.send(f"Deleted {channel.name}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Failed to delete channel: {e}", ephemeral=True)

# RAIDMODE: kick recent bots
@tree.command(name="raidmode", description="Kick newly joined bot accounts (recent)")
@is_mod()
async def raidmode_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    count = 0
    now = datetime.utcnow()
    for m in interaction.guild.members:
        try:
            join_age = (now - (m.joined_at.replace(tzinfo=None) if m.joined_at else now))
            if m.bot and join_age < timedelta(minutes=10):
                await m.kick(reason="Raid prevention")
                count += 1
        except Exception:
            pass
    await interaction.followup.send(f"Kicked {count} newly joined bots.", ephemeral=True)
    await log(interaction.guild, f"{interaction.user} raidmode kicked {count} bots")

# RENAME (set nickname)
@tree.command(name="rename", description="Set a member's nickname")
@is_mod()
@app_commands.describe(member="Member", nick="Nickname")
async def rename_cmd(interaction: discord.Interaction, member: discord.Member, nick: str):
    await interaction.response.defer(ephemeral=True)
    try:
        await member.edit(nick=nick)
        await interaction.followup.send(f"Set nickname for {member} -> {nick}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Failed to rename: {e}", ephemeral=True)

# ENFORCE_NICK placeholder
@tree.command(name="enforce_nick", description="Enforce nickname pattern (placeholder)")
@is_mod()
@app_commands.describe(pattern="Nickname pattern (regex)")
async def enforce_nick_cmd(interaction: discord.Interaction, pattern: str):
    await interaction.response.send_message(f"Enforce nickname pattern: {pattern} (placeholder)", ephemeral=True)

# SCAN attachments placeholder
@tree.command(name="scan", description="Scan recent messages for attachments/links (placeholder)")
@is_mod()
@app_commands.describe(limit="How many messages to scan")
async def scan_cmd(interaction: discord.Interaction, limit: int = 50):
    await interaction.response.send_message(f"Scanning last {limit} messages for attachments and suspicious links... (placeholder)", ephemeral=True)

# TAG / GETTAG
@tree.command(name="tag", description="Add a moderation note for a member")
@is_mod()
@app_commands.describe(member="Member", note="Note text")
async def tag_cmd(interaction: discord.Interaction, member: discord.Member, note: str):
    gid = str(interaction.guild.id)
    DATA.setdefault("tags", {})
    DATA["tags"].setdefault(gid, {})
    DATA["tags"][gid][str(member.id)] = note
    save_data()
    await interaction.response.send_message(f"Tagged {member} with note.", ephemeral=True)

@tree.command(name="gettag", description="Retrieve moderation note for a member")
@is_mod()
@app_commands.describe(member="Member")
async def gettag_cmd(interaction: discord.Interaction, member: discord.Member):
    gid = str(interaction.guild.id)
    note = DATA.get("tags", {}).get(gid, {}).get(str(member.id))
    await interaction.response.send_message(note or "No note.", ephemeral=True)

# JAIL / UNJAIL / TEMPJAIL
@tree.command(name="jail", description="Move a member to jailed role")
@is_mod()
@app_commands.describe(member="Member", reason="Reason")
async def jail_cmd(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason"):
    await interaction.response.defer(ephemeral=True)
    role = discord.utils.get(interaction.guild.roles, name="Jailed")
    if not role:
        perms = discord.Permissions(send_messages=False, view_channel=True)
        role = await interaction.guild.create_role(name="Jailed", permissions=perms)
        for c in interaction.guild.channels:
            try:
                await c.set_permissions(role, send_messages=False, speak=False)
            except Exception:
                pass
    DATA.setdefault("jails", {})
    DATA["jails"][str(member.id)] = {"guild": interaction.guild.id, "time": time.time()}
    save_data()
    try:
        await member.add_roles(role, reason=reason)
        await interaction.followup.send(f"Jailed {member}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Failed to jail: {e}", ephemeral=True)

@tree.command(name="unjail", description="Remove jailed role from a member")
@is_mod()
@app_commands.describe(member="Member")
async def unjail_cmd(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)
    role = discord.utils.get(interaction.guild.roles, name="Jailed")
    if role:
        try:
            await member.remove_roles(role)
        except Exception:
            pass
    DATA.get("jails", {}).pop(str(member.id), None)
    save_data()
    await interaction.followup.send(f"Unjailed {member}", ephemeral=True)

@tree.command(name="tempjail", description="Temporarily jail a member")
@is_mod()
@app_commands.describe(member="Member", seconds="Duration", reason="Reason")
async def tempjail_cmd(interaction: discord.Interaction, member: discord.Member, seconds: int, reason: str = "No reason"):
    await interaction.response.defer(ephemeral=True)
    await jail_cmd(interaction, member, reason=reason)
    await interaction.followup.send(f"Temporarily jailed {member} for {seconds}s", ephemeral=True)
    await asyncio.sleep(seconds)
    try:
        await unjail_cmd(interaction, member)
    except Exception:
        pass

# SETMODROLE
@tree.command(name="setmodrole", description="Set mod role (stored in data)")
@is_mod()
@app_commands.describe(role="Role to mark as mod")
async def setmodrole_cmd(interaction: discord.Interaction, role: discord.Role):
    gid = str(interaction.guild.id)
    DATA.setdefault("config", {})
    DATA["config"].setdefault(gid, {})
    DATA["config"][gid]["modrole"] = role.id
    save_data()
    await interaction.response.send_message(f"Set mod role to {role.name}", ephemeral=True)

# CHECKPERMS
@tree.command(name="checkperms", description="Show a member's guild permissions")
@app_commands.describe(member="Member (optional)")
async def checkperms_cmd(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    await interaction.response.send_message(f"{member} perms: {member.guild_permissions}", ephemeral=True)

# BANINFO
@tree.command(name="baninfo", description="Lookup ban info for a user ID")
@is_mod()
@app_commands.describe(user_id="User ID to lookup")
async def baninfo_cmd(interaction: discord.Interaction, user_id: int):
    try:
        bans = await interaction.guild.bans()
        for b in bans:
            if b.user.id == user_id:
                await interaction.response.send_message(f"Banned: {b.user} — reason: {b.reason}", ephemeral=True)
                return
        await interaction.response.send_message("No ban info.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Failed to fetch bans: {e}", ephemeral=True)

# INVITECOUNT
@tree.command(name="invitecount", description="Show how many times a member's invites were used")
@is_mod()
@app_commands.describe(member="Member (optional)")
async def invitecount_cmd(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    try:
        invs = await interaction.guild.invites()
        count = sum(i.uses for i in invs if i.inviter and i.inviter.id == member.id)
        await interaction.response.send_message(f"{member} created invites used {count} times.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Failed to fetch invites: {e}", ephemeral=True)

# SAFEMODE toggle
@tree.command(name="safemode", description="Toggle safe mode which enables strict checks")
@is_mod()
@app_commands.describe(toggle="on or off")
async def safemode_cmd(interaction: discord.Interaction, toggle: str = "on"):
    gid = str(interaction.guild.id)
    SAFE_MODE[gid] = toggle.lower() in ("on", "1", "true", "yes")
    await interaction.response.send_message(f"Safe mode: {SAFE_MODE[gid]}", ephemeral=True)

# WHOCHANGED (audit logs)
@tree.command(name="whochanged", description="Show recent audit log entries")
@is_mod()
@app_commands.describe(limit="Number of entries (max 25)")
async def whochanged_cmd(interaction: discord.Interaction, limit: int = 5):
    await interaction.response.defer(ephemeral=True)
    try:
        logs = await interaction.guild.audit_logs(limit=min(limit, 25)).flatten()
        out = []
        for l in logs:
            out.append(f"{l.user} {l.action} -> target: {l.target}")
        await interaction.followup.send("Recent audit entries:\n" + "\n".join(out[:25]), ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Failed to fetch audit logs: {e}", ephemeral=True)

# SETWELCOME
@tree.command(name="setwelcome", description="Set welcome channel in config")
@is_mod()
@app_commands.describe(channel="Welcome channel (optional)")
async def setwelcome_cmd(interaction: discord.Interaction, channel: discord.TextChannel = None):
    gid = str(interaction.guild.id)
    DATA.setdefault("config", {})
    DATA["config"].setdefault(gid, {})
    DATA["config"][gid]["welcome_channel"] = channel.id if channel else None
    save_data()
    await interaction.response.send_message(f"Welcome channel set to {channel.mention if channel else 'None'}", ephemeral=True)

# WHITELIST / BLACKLIST
@tree.command(name="whitelist", description="Whitelist an entity for actions")
@is_mod()
@app_commands.describe(entity="Type (role/member/invite)", id_str="ID")
async def whitelist_cmd(interaction: discord.Interaction, entity: str, id_str: str):
    gid = str(interaction.guild.id)
    WHITELIST.setdefault(gid, set()).add((entity, id_str))
    await interaction.response.send_message(f"Whitelisted {entity}:{id_str}", ephemeral=True)

@tree.command(name="blacklist", description="Blacklist an entity for actions")
@is_mod()
@app_commands.describe(entity="Type (role/member/invite)", id_str="ID")
async def blacklist_cmd(interaction: discord.Interaction, entity: str, id_str: str):
    gid = str(interaction.guild.id)
    BLACKLIST.setdefault(gid, set()).add((entity, id_str))
    await interaction.response.send_message(f"Blacklisted {entity}:{id_str}", ephemeral=True)

# AUDIT (basic)
@tree.command(name="audit", description="Show recent audit log events")
@is_mod()
@app_commands.describe(limit="Number of entries (max 25)")
async def audit_cmd(interaction: discord.Interaction, limit: int = 10):
    await interaction.response.defer(ephemeral=True)
    try:
        logs = await interaction.guild.audit_logs(limit=min(limit, 25)).flatten()
        lines = []
        for e in logs:
            lines.append(f"{e.user} {e.action} {getattr(e, 'target', '')} at {e.created_at}")
        await interaction.followup.send("\n".join(lines[:25]) or "No audit entries.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Failed to fetch audit: {e}", ephemeral=True)

# PINGDB (placeholder)
@tree.command(name="pingdb", description="Check DB connection (placeholder)")
async def pingdb_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("No DB connected. (placeholder)", ephemeral=True)

# -----------------------------
# End of fully implemented commands
# -----------------------------

if __name__ == "__main__":
    if not TOKEN:
        print("ERROR: DISCORD_TOKEN not set in environment or .env.")
        exit(1)
    bot.run(TOKEN)
