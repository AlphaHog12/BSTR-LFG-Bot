import discord
from discord.ext import commands
import logging, os, asyncio
from dotenv import load_dotenv
import webserver

# Load token
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

# Logging
handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')

# Intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Roles
LFG_ROLE = "LFG"
OFFICER_ROLE = "Officers"

# Channels & Category (replace with your IDs)
DEPLOY_CHANNEL_ID = 1413578264135467098
ALERT_CHANNEL_ID = 1413578336810172426
LFG_CATEGORY_ID = 1413571331202617374
JOIN_TO_CREATE_CHANNEL_ID = 1413590729942503474

# In-memory state
squads: dict[int, list[discord.Member]] = {}
managed_vcs: set[int] = set()
vc_inactivity_tasks: dict[int, asyncio.Task] = {}
user_active_lfg: dict[int, int] = {}
user_join_create: dict[int, int] = {}

# --- Helpers ---
async def delete_vc_safe(vc: discord.VoiceChannel):
    if not vc or not vc.guild:
        return
    try:
        await vc.delete()
    except Exception:
        pass
    finally:
        managed_vcs.discard(vc.id)
        task = vc_inactivity_tasks.pop(vc.id, None)
        if task and not task.done():
            task.cancel()
        for uid, vid in list(user_join_create.items()):
            if vid == vc.id:
                user_join_create.pop(uid, None)

def schedule_vc_inactivity(vc: discord.VoiceChannel, delay: int = 900):
    async def _wait_and_delete():
        try:
            await asyncio.sleep(delay)
            if not vc or not vc.guild:
                return
            current = vc.guild.get_channel(vc.id)
            if not isinstance(current, discord.VoiceChannel):
                return
            if len(current.members) == 0:
                await delete_vc_safe(current)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
    old = vc_inactivity_tasks.get(vc.id)
    if old and not old.done():
        old.cancel()
    vc_inactivity_tasks[vc.id] = bot.loop.create_task(_wait_and_delete())

async def delete_post_after_duration(vc: discord.VoiceChannel | None, msg: discord.Message, timeout: int = 86400):
    try:
        await asyncio.sleep(timeout)
        if vc and vc.guild:
            await delete_vc_safe(vc)
        try:
            await msg.delete()
        except Exception:
            pass
    except asyncio.CancelledError:
        pass

# --- LFG Signup ---
class LFGSignupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Enlist", style=discord.ButtonStyle.success, custom_id="lfg_enlist")
    async def enlist(self, interaction: discord.Interaction, button: discord.ui.Button):
        role = discord.utils.get(interaction.guild.roles, name=LFG_ROLE)
        if not role:
            await interaction.response.send_message("⚠️ LFG role not found.", ephemeral=True)
            return
        if role in interaction.user.roles:
            await interaction.response.send_message("You’re already enlisted!", ephemeral=True)
        else:
            await interaction.user.add_roles(role)
            await interaction.response.send_message(f"✅ You have been enlisted into {role.mention}!", ephemeral=True)

    @discord.ui.button(label="Unenlist", style=discord.ButtonStyle.danger, custom_id="lfg_unenlist")
    async def unenlist(self, interaction: discord.Interaction, button: discord.ui.Button):
        role = discord.utils.get(interaction.guild.roles, name=LFG_ROLE)
        if not role:
            await interaction.response.send_message("⚠️ LFG role not found.", ephemeral=True)
            return
        if role in interaction.user.roles:
            await interaction.user.remove_roles(role)
            await interaction.response.send_message(f"❌ You have been unenlisted from {role.mention}.", ephemeral=True)
        else:
            await interaction.response.send_message("You don’t have the LFG role.", ephemeral=True)

@bot.command()
@commands.has_role(OFFICER_ROLE)
async def post_lfg_signup(ctx):
    embed = discord.Embed(
        title="📢 LFG Role Signup",
        description="Click **Enlist** to get the LFG role and receive notifications for new groups.\nClick **Unenlist** if you no longer want to be notified.",
        color=discord.Color.blue()
    )
    view = LFGSignupView()
    await ctx.send(embed=embed, view=view)

# --- Activity Selection ---
class ActivitySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Contracts - Combat"),
            discord.SelectOption(label="Contracts - Cargo"),
            discord.SelectOption(label="PVP"),
            discord.SelectOption(label="Piracy"),
            discord.SelectOption(label="Industry - Salvage"),
            discord.SelectOption(label="Industry - Cargo"),
            discord.SelectOption(label="Other - Explain in Notes")
        ]
        super().__init__(placeholder="Choose an activity...", options=options, custom_id="lfg_activity")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(LFGModal(self.values[0], interaction.user))

class ActivitySelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ActivitySelect())

# --- LFG Modal & View ---
class LFGModal(discord.ui.Modal):
    def __init__(self, activity: str, user: discord.Member):
        super().__init__(title="Create LFG Post")
        self.activity = activity
        self.user = user
        self.ign = discord.ui.TextInput(label="Party Lead", placeholder="Player name")
        self.notes = discord.ui.TextInput(label="Notes", placeholder="Optional notes", required=False)
        self.max_players_input = discord.ui.TextInput(label="Max Party Size", placeholder="Enter a number", required=True)
        self.add_item(self.ign)
        self.add_item(self.notes)
        self.add_item(self.max_players_input)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        alert_channel = guild.get_channel(ALERT_CHANNEL_ID)
        lfg_role = discord.utils.get(guild.roles, name=LFG_ROLE)
        lfg_category = guild.get_channel(LFG_CATEGORY_ID)

        if not alert_channel or not lfg_category:
            await interaction.response.send_message("⚠️ Setup issue, contact an Officer.", ephemeral=True)
            return

        if self.user.id in user_active_lfg and OFFICER_ROLE not in [r.name for r in self.user.roles]:
            await interaction.response.send_message("⚠️ You already have an active LFG post.", ephemeral=True)
            return

        try:
            max_players = int(self.max_players_input.value)
        except ValueError:
            await interaction.response.send_message("⚠️ Max Party Size must be a number.", ephemeral=True)
            return

        # Use Notes for "Other" activity
        if self.activity.lower().startswith("other") and self.notes.value.strip():
            vc_name = self.notes.value.strip()
            embed_title = self.notes.value.strip()
        else:
            vc_name = f"{self.activity} | {self.ign.value}"
            embed_title = f"⚔️ {self.activity}"

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(connect=True),
            guild.me: discord.PermissionOverwrite(connect=True, manage_channels=True)
        }
        if lfg_role:
            overwrites[lfg_role] = discord.PermissionOverwrite(connect=True)

        temp_vc = await guild.create_voice_channel(
            name=vc_name,
            overwrites=overwrites,
            category=lfg_category,
            user_limit=max_players  # apply max party size limit
        )
        managed_vcs.add(temp_vc.id)

        embed = discord.Embed(title=embed_title, color=discord.Color.blue())
        embed.add_field(name="Host", value=self.user.mention, inline=False)
        embed.add_field(name="Voice Channel", value=temp_vc.mention, inline=False)
        initial_squad = [self.user]
        embed.add_field(name="Current Squad", value=f"1/{max_players} {self.user.mention}", inline=False)
        if self.notes.value.strip():
            embed.add_field(name="Notes", value=self.notes.value, inline=False)
        embed.add_field(name="Max Party Size", value=str(max_players), inline=False)

        view = LFGView(msg_id=None, vc=temp_vc, max_players=max_players)
        msg = await alert_channel.send(content=f"{lfg_role.mention if lfg_role else ''} Looking for group!", embed=embed, view=view)
        squads[msg.id] = initial_squad
        view.msg = msg
        view.msg_id = msg.id
        await view.update_embed()
        schedule_vc_inactivity(temp_vc, 60)  # LFG VC timer 60s
        bot.loop.create_task(delete_post_after_duration(temp_vc, msg, 86400))
        user_active_lfg[self.user.id] = msg.id

        await interaction.response.send_message("✅ Your LFG has been posted!", ephemeral=True)

class LFGView(discord.ui.View):
    def __init__(self, msg_id: int | None, vc: discord.VoiceChannel, max_players: int):
        super().__init__(timeout=None)
        self.msg_id = msg_id
        self.vc = vc
        self.msg: discord.Message | None = None
        self.max_players = max_players

    async def update_embed(self):
        if not self.msg or not self.msg.embeds:
            return
        embed = self.msg.embeds[0].copy()
        squad = squads.get(self.msg_id, [])
        idx = next((i for i, f in enumerate(embed.fields) if f.name == "Current Squad"), None)
        value = "\n".join([f"{i+1}/{self.max_players} {m.mention}" for i, m in enumerate(squad)]) or "Empty"
        if idx is not None:
            embed.set_field_at(idx, name="Current Squad", value=value, inline=False)
        else:
            embed.add_field(name="Current Squad", value=value, inline=False)
        await self.msg.edit(embed=embed, view=self)

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        squad = squads.get(self.msg_id, [])
        if len(squad) >= self.max_players:
            await interaction.response.send_message("⚠️ Party is full!", ephemeral=True)
            return
        if interaction.user not in squad:
            squad.append(interaction.user)
            squads[self.msg_id] = squad
        await self.update_embed()
        await interaction.response.defer()

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        squad = squads.get(self.msg_id, [])
        if interaction.user in squad:
            squad.remove(interaction.user)
        await self.update_embed()
        await interaction.response.defer()

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        if OFFICER_ROLE not in [role.name for role in interaction.user.roles] and interaction.user.id != self.msg.embeds[0].fields[0].value.id:
            await interaction.response.send_message("Only Officers or Host can delete LFG posts.", ephemeral=True)
            return
        await delete_vc_safe(self.vc)
        try:
            await self.msg.delete()
        except Exception:
            pass
        user_active_lfg.pop(interaction.user.id, None)
        await interaction.response.send_message("✅ LFG post deleted.", ephemeral=True)

# --- Deploy Button ---
class DeployLFGView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Create LFG Post", style=discord.ButtonStyle.primary)
    async def create_lfg_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if OFFICER_ROLE not in [role.name for role in interaction.user.roles]:
            await interaction.response.send_message("Only Officers can create LFG posts!", ephemeral=True)
            return
        await interaction.response.send_message("Select an activity:", view=ActivitySelectView(), ephemeral=True)

@bot.command()
@commands.has_role(OFFICER_ROLE)
async def post_lfg_button(ctx):
    view = DeployLFGView()
    await ctx.send("Click the button below to create an LFG post:", view=view)

# --- Voice State Updates ---
@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    # Handle LFG VC inactivity
    if before.channel and before.channel.id in managed_vcs:
        if len(before.channel.members) == 0:
            schedule_vc_inactivity(before.channel, 60)  # LFG VC timer 60s
    if after.channel and after.channel.id in managed_vcs:
        task = vc_inactivity_tasks.get(after.channel.id)
        if task and not task.done():
            task.cancel()

    # --- Join-to-Create VC ---
    if after.channel and after.channel.id == JOIN_TO_CREATE_CHANNEL_ID:
        if member.id in user_join_create:
            await member.send("⚠️ You already have an active Join-to-Create VC!")
            try:
                await member.move_to(before.channel)
            except Exception:
                pass
            return
        overwrites = {
            member.guild.default_role: discord.PermissionOverwrite(connect=True),
            member.guild.me: discord.PermissionOverwrite(connect=True, manage_channels=True)
        }
        vc_name = f"{member.display_name}'s VC"
        try:
            new_vc = await member.guild.create_voice_channel(
                name=vc_name,
                overwrites=overwrites,
                category=after.channel.category
            )
            managed_vcs.add(new_vc.id)
            user_join_create[member.id] = new_vc.id
            await member.move_to(new_vc)
            schedule_vc_inactivity(new_vc, 10)  # Join-to-Create VC timer 10s
        except Exception as e:
            print(f"Error creating Join-to-Create VC: {e}")
            await member.send("⚠️ Failed to create your VC. Check bot permissions.")

# --- Bot Ready ---
@bot.event
async def on_ready():
    bot.add_view(LFGSignupView())
    print(f"✅ Logged in as {bot.user}")


webserver.keep_alive()
bot.run(TOKEN, log_handler=handler, log_level=logging.DEBUG)
