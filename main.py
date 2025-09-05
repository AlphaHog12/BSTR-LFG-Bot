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

def schedule_vc_inactivity(vc: discord.VoiceChannel, delay: int = 60):
    async def _wait_and_delete():
        try:
            await asyncio.sleep(delay)
            current = vc.guild.get_channel(vc.id)
            if not isinstance(current, discord.VoiceChannel):
                return
            if len(current.members) == 0:
                await delete_vc_safe(current)
        except asyncio.CancelledError:
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

# --- LFG Role Toggle ---
class LFGToggleView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Enlist/Unenlist", style=discord.ButtonStyle.primary, custom_id="lfg_toggle")
    async def toggle_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        role = discord.utils.get(interaction.guild.roles, name=LFG_ROLE)
        if not role:
            await interaction.response.send_message("⚠️ LFG role not found.", ephemeral=True)
            return
        if role in interaction.user.roles:
            await interaction.user.remove_roles(role)
            await interaction.response.send_message(f"❌ You have been unenlisted from {role.mention}.", ephemeral=True)
        else:
            await interaction.user.add_roles(role)
            await interaction.response.send_message(f"✅ You have been enlisted into {role.mention}!", ephemeral=True)

@bot.command()
async def post_lfg_signup(ctx):
    embed = discord.Embed(
        title="📢 LFG Role Signup",
        description="Click the button to **toggle enlistment**.\nIf enlisted, you’ll receive notifications for new groups.",
        color=discord.Color.blue()
    )
    view = LFGToggleView()
    await ctx.send(embed=embed, view=view)

# --- LFG Modal & View ---
class LFGModal(discord.ui.Modal):
    def __init__(self, user: discord.Member):
        super().__init__(title="Create LFG Post")
        self.user = user
        self.host = discord.ui.TextInput(label="Host", placeholder="Who is leading the group?", required=True)
        self.description = discord.ui.TextInput(label="Channel Description", placeholder="What is this squad doing?", required=True)
        self.max_players_input = discord.ui.TextInput(label="Max Party Size (0 = unlimited)", placeholder="Enter a number", required=True)
        self.add_item(self.host)
        self.add_item(self.description)
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
            if max_players < 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("⚠️ Max Party Size must be a non-negative number.", ephemeral=True)
            return

        user_limit = None if max_players == 0 else max_players

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(connect=True),
            guild.me: discord.PermissionOverwrite(connect=True, manage_channels=True)
        }
        if lfg_role:
            overwrites[lfg_role] = discord.PermissionOverwrite(connect=True)

        vc_name = self.description.value.strip()
        temp_vc = await guild.create_voice_channel(
            name=vc_name,
            overwrites=overwrites,
            category=lfg_category,
            user_limit=user_limit
        )
        managed_vcs.add(temp_vc.id)

        embed = discord.Embed(title=vc_name, color=discord.Color.blue())
        embed.add_field(name="Host", value=self.host.value, inline=False)
        embed.add_field(name="Voice Channel", value=temp_vc.mention, inline=False)

        initial_squad = [self.user]
        max_label = "∞" if max_players == 0 else str(max_players)
        embed.add_field(name="Current Squad", value=f"1/{max_label} {self.user.mention}", inline=False)
        embed.add_field(name="Max Party Size", value=max_label, inline=False)

        view = LFGView(msg_id=None, vc=temp_vc, max_players=max_players, host_id=self.user.id, host_name=self.host.value)
        msg = await alert_channel.send(content=f"{lfg_role.mention if lfg_role else ''} Looking for group!", embed=embed, view=view)
        squads[msg.id] = initial_squad
        view.msg = msg
        view.msg_id = msg.id
        await view.update_embed()
        schedule_vc_inactivity(temp_vc, 60)
        bot.loop.create_task(delete_post_after_duration(temp_vc, msg, 86400))
        user_active_lfg[self.user.id] = msg.id

        try:
            await self.user.move_to(temp_vc)
        except Exception:
            pass

        await interaction.response.send_message("✅ Your LFG has been posted!", ephemeral=True)

class LFGView(discord.ui.View):
    def __init__(self, msg_id: int | None, vc: discord.VoiceChannel, max_players: int, host_id: int, host_name: str):
        super().__init__(timeout=None)
        self.msg_id = msg_id
        self.vc = vc
        self.msg: discord.Message | None = None
        self.max_players = max_players
        self.host_id = host_id
        self.host_name = host_name

    async def update_embed(self):
        if not self.msg or not self.msg.embeds:
            return
        embed = self.msg.embeds[0].copy()
        squad = squads.get(self.msg_id, [])
        idx = next((i for i, f in enumerate(embed.fields) if f.name == "Current Squad"), None)

        max_label = "∞" if self.max_players == 0 else str(self.max_players)
        value = "\n".join([f"{i+1}/{max_label} {m.mention}" for i, m in enumerate(squad)]) or "Empty"

        if idx is not None:
            embed.set_field_at(idx, name="Current Squad", value=value, inline=False)
        else:
            embed.add_field(name="Current Squad", value=value, inline=False)

        self.clear_items()

        # Toggle Join/Leave
        if any(m.id == self.host_id for m in squad) and (interaction_user := self.msg.guild.get_member(self.host_id)):
            pass
        if any(m.id == self.host_id for m in squad):
            self.add_item(discord.ui.Button(label="Leave", style=discord.ButtonStyle.danger, custom_id="lfg_leave"))
        else:
            self.add_item(discord.ui.Button(label="Join", style=discord.ButtonStyle.success, custom_id="lfg_join"))

        # Delete button for host + officers
        delete_button = discord.ui.Button(label="Delete", style=discord.ButtonStyle.danger, custom_id="lfg_delete")
        self.add_item(delete_button)

        await self.msg.edit(embed=embed, view=self)

    async def interaction_check(self, interaction: discord.Interaction):
        custom_id = interaction.data["custom_id"]
        squad = squads.get(self.msg_id, [])

        if custom_id == "lfg_join":
            if self.max_players != 0 and len(squad) >= self.max_players:
                await interaction.response.send_message("⚠️ Party is full!", ephemeral=True)
                return False
            if interaction.user not in squad:
                squad.append(interaction.user)
            squads[self.msg_id] = squad
            await self.update_embed()
            await interaction.response.defer()
            return False

        elif custom_id == "lfg_leave":
            if interaction.user in squad:
                squad.remove(interaction.user)
            squads[self.msg_id] = squad
            await self.update_embed()
            await interaction.response.defer()
            return False

        elif custom_id == "lfg_delete":
            if OFFICER_ROLE not in [role.name for role in interaction.user.roles] and interaction.user.id != self.host_id:
                await interaction.response.send_message("Only Officers or the Host can delete this LFG post.", ephemeral=True)
                return False
            await delete_vc_safe(self.vc)
            try:
                await self.msg.delete()
            except Exception:
                pass
            user_active_lfg.pop(self.host_id, None)
            await interaction.response.send_message("✅ LFG post deleted.", ephemeral=True)
            return False

        return True

# --- Deploy Button ---
class DeployLFGView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Create LFG Post", style=discord.ButtonStyle.primary)
    async def create_lfg_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(LFGModal(interaction.user))

@bot.command()
async def post_lfg_button(ctx):
    view = DeployLFGView()
    await ctx.send("Click the button below to create an LFG post:", view=view)

# --- Voice State Updates ---
@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if before.channel and before.channel.id in managed_vcs:
        if len(before.channel.members) == 0:
            schedule_vc_inactivity(before.channel, 60)
    if after.channel and after.channel.id in managed_vcs:
        task = vc_inactivity_tasks.get(after.channel.id)
        if task and not task.done():
            task.cancel()

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
            schedule_vc_inactivity(new_vc, 10)
        except Exception as e:
            print(f"Error creating Join-to-Create VC: {e}")
            await member.send("⚠️ Failed to create your VC. Check bot permissions.")

# --- Bot Ready ---
@bot.event
async def on_ready():
    bot.add_view(LFGToggleView())
    print(f"✅ Logged in as {bot.user}")

webserver.keep_alive()
bot.run(TOKEN, log_handler=handler, log_level=logging.DEBUG)

