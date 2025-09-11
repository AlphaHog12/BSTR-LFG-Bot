import discord
from discord.ext import commands
import asyncio, os, logging
from dotenv import load_dotenv
import webserver

# --- Load token ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# --- Logging ---
logging.basicConfig(level=logging.DEBUG, filename='discord.log', encoding='utf-8', filemode='w')
logger = logging.getLogger(__name__)

# --- Intents & Bot ---
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- Servers & Channels ---
SERVERS = {
    "main": {
        "server_id": 911035631193444412,
        "alert": 1414759057121873950,
        "posting": 1414759057121873950,
        "lfg_category": 1414750850701721703,
        "join_to_create": 1413590729942503474
    },
    "test": {
        "server_id": 1412815561477459991,
        "alert": 1413526136951935066,
        "posting": 1413526066198216775,
        "lfg_category": 1413532598378172548,
        "join_to_create": 1413556559883276380
    }
}

# --- Roles & Officers ---
BOT_OWNER_ID = 441386174670438401
OFFICER_ROLE_IDS = [
    1412827215002861608, 1173706633084403762, 1176539066569871531,
    911755541020311553, 1413165455421734985
]

# --- In-memory storage ---
squads = {}               # {guild_id: {msg_id: [discord.Member, ...]}}
managed_vcs = set()        # {vc.id}
vc_inactivity_tasks = {}   # {vc.id: asyncio.Task}
user_active_lfg = {}       # {user.id: msg_id}
user_join_create = {}      # {user.id: vc.id}

# --- Helper Functions ---
def is_officer(member: discord.Member) -> bool:
    return any(role.id in OFFICER_ROLE_IDS for role in member.roles)

async def dm_admin(msg: str):
    try:
        owner = await bot.fetch_user(BOT_OWNER_ID)
        await owner.send(f"[ADMIN DM] {msg}")
    except Exception as e:
        logger.error(f"Failed to DM admin: {e}")

async def delete_vc_safe(vc: discord.VoiceChannel):
    try:
        managed_vcs.discard(vc.id)
        for uid, vid in list(user_join_create.items()):
            if vid == vc.id:
                user_join_create.pop(uid, None)
        await vc.delete()
    except Exception as e:
        await dm_admin(f"Failed to delete VC {vc.name}: {e}")
    finally:
        task = vc_inactivity_tasks.pop(vc.id, None)
        if task and not task.done():
            task.cancel()

def schedule_vc_inactivity(vc: discord.VoiceChannel, delay: int = 60):
    async def _wait_and_delete():
        try:
            await asyncio.sleep(delay)
            current = vc.guild.get_channel(vc.id)
            if isinstance(current, discord.VoiceChannel) and len(current.members) == 0:
                await delete_vc_safe(current)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            await dm_admin(f"VC inactivity task failed for {vc.name}: {e}")

    old_task = vc_inactivity_tasks.get(vc.id)
    if old_task and not old_task.done():
        old_task.cancel()
    vc_inactivity_tasks[vc.id] = bot.loop.create_task(_wait_and_delete())

# --- LFG Modal ---
# --- LFG Modal ---
class LFGModal(discord.ui.Modal):
    def __init__(self, user: discord.Member, guild_key: str):
        super().__init__(title="Create LFG Post")
        self.user = user
        self.guild_key = guild_key
        self.host_input = discord.ui.TextInput(label="Host Name", placeholder="Who is leading?", required=True)
        self.desc_input = discord.ui.TextInput(label="Channel Description", placeholder="Description", required=True)
        self.max_input = discord.ui.TextInput(label="Max Players (0=unlimited)", placeholder="Enter a number", required=True)
        self.add_item(self.host_input)
        self.add_item(self.desc_input)
        self.add_item(self.max_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            guild = interaction.guild
            data = SERVERS[self.guild_key]
            alert_channel = guild.get_channel(data["alert"])
            lfg_category = guild.get_channel(data["lfg_category"])

            if self.user.id in user_active_lfg and not is_officer(self.user):
                await interaction.response.send_message("⚠️ You already have an active LFG post.", ephemeral=True)
                return

            max_players = int(self.max_input.value)
            user_limit = None if max_players == 0 else max_players

            # Create voice channel
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(connect=True),
                guild.me: discord.PermissionOverwrite(connect=True, manage_channels=True)
            }
            vc_name = self.desc_input.value.strip()
            temp_vc = await guild.create_voice_channel(vc_name, overwrites=overwrites, category=lfg_category)
            if user_limit:
                await temp_vc.edit(user_limit=user_limit)
            managed_vcs.add(temp_vc.id)

            # Build embed
            embed = discord.Embed(title=vc_name, color=discord.Color.blue())
            embed.add_field(name="Host", value=self.host_input.value, inline=False)
            embed.add_field(name="Voice Channel", value=temp_vc.mention, inline=False)
            max_label = "∞" if max_players == 0 else str(max_players)
            embed.add_field(name="Current Squad", value=f"1/{max_label} {self.user.mention}", inline=False)
            embed.add_field(name="Max Party Size", value=max_label, inline=False)

            # Create LFG view WITHOUT vc reference
            view = LFGView(msg_id=None, max_players=max_players, host_id=self.user.id)
            msg = await alert_channel.send(content=f"{self.user.mention} is looking for a group!", embed=embed, view=view)
            view.msg = msg
            view.msg_id = msg.id

            # Track squads
            if guild.id not in squads:
                squads[guild.id] = {}
            squads[guild.id][msg.id] = [self.user]

            schedule_vc_inactivity(temp_vc, 60)
            user_active_lfg[self.user.id] = msg.id

            # Move user to VC
            try:
                await self.user.move_to(temp_vc)
            except:
                pass

            await interaction.response.send_message("✅ LFG posted!", ephemeral=True)
        except Exception as e:
            await dm_admin(f"LFGModal submit error: {e}")
            await interaction.response.send_message("⚠️ Failed to create LFG post.", ephemeral=True)


# --- LFG View ---
class LFGView(discord.ui.View):
    def __init__(self, msg_id: int, host_id: int, max_players: int):
        super().__init__(timeout=None)
        self.msg_id = msg_id
        self.host_id = host_id
        self.max_players = max_players

    async def update_embed(self, msg: discord.Message):
        guild_id = msg.guild.id
        squad = squads.get(guild_id, {}).get(self.msg_id, [])
        embed = msg.embeds[0].copy()
        max_label = "∞" if self.max_players == 0 else str(self.max_players)
        value = "\n".join([f"{i+1}/{max_label} {m.mention}" for i, m in enumerate(squad)]) or "Empty"
        for i, f in enumerate(embed.fields):
            if f.name == "Current Squad":
                embed.set_field_at(i, name="Current Squad", value=value, inline=False)
        await msg.edit(embed=embed)

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, custom_id="lfg_join")
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = interaction.guild.id
        squad = squads.get(guild_id, {}).get(self.msg_id, [])
        if self.max_players != 0 and len(squad) >= self.max_players:
            await interaction.response.send_message("⚠️ Party full!", ephemeral=True)
            return
        if interaction.user not in squad:
            squad.append(interaction.user)
        squads[guild_id][self.msg_id] = squad
        await self.update_embed(interaction.message)
        await interaction.response.defer()

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.danger, custom_id="lfg_leave")
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = interaction.guild.id
        squad = squads.get(guild_id, {}).get(self.msg_id, [])
        if interaction.user in squad:
            squad.remove(interaction.user)
        squads[guild_id][self.msg_id] = squad
        await self.update_embed(interaction.message)
        await interaction.response.defer()

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, custom_id="lfg_delete")
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.host_id and not is_officer(interaction.user):
            await interaction.response.send_message("Only host or officers can delete.", ephemeral=True)
            return
        guild_id = interaction.guild.id
        squads.get(guild_id, {}).pop(self.msg_id, None)
        try:
            await interaction.message.delete()
        except:
            pass
        await interaction.response.send_message("✅ LFG post deleted.", ephemeral=True)

# --- Deploy Button ---
class DeployLFGButtonView(discord.ui.View):
    def __init__(self, guild_key: str):
        super().__init__(timeout=None)
        self.guild_key = guild_key

    @discord.ui.button(label="Create LFG Post", style=discord.ButtonStyle.primary, custom_id="deploy_lfg")
    async def deploy_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(LFGModal(interaction.user, self.guild_key))

# --- Backup/Cleanup Command ---
@bot.command()
@commands.is_owner()
async def refresh_lfg(ctx):
    """
    Removes old buttons/posts in posting channels and reposts a persistent "Create LFG Post" button.
    """
    try:
        for guild_key, data in SERVERS.items():
            guild = bot.get_guild(data["server_id"])
            if not guild:
                continue

            post_channel = guild.get_channel(data["posting"])
            if not post_channel:
                continue

            # Remove old buttons in posting channel
            messages = [msg async for msg in post_channel.history(limit=100)]
            for msg in messages:
                if msg.components:
                    try:
                        await msg.edit(view=None)
                    except:
                        pass

            # Post fresh "Create LFG Post" button
            view = DeployLFGButtonView(guild_key)
            await post_channel.send("Click the button below to create an LFG post!", view=view)

        await ctx.send("✅ LFG cleanup complete and new buttons posted in posting channels!")
    except Exception as e:
        await dm_admin(f"refresh_lfg command failed: {e}")
        await ctx.send(f"⚠️ Failed to refresh LFG: {e}")

# --- Voice State Updates ---
@bot.event
async def on_voice_state_update(member, before, after):
    try:
        if before.channel and before.channel.id in managed_vcs and len(before.channel.members) == 0:
            schedule_vc_inactivity(before.channel, 60)
        if after.channel and after.channel.id in managed_vcs:
            task = vc_inactivity_tasks.get(after.channel.id)
            if task and not task.done():
                task.cancel()

        for key, data in SERVERS.items():
            join_to_create = data["join_to_create"]
            if after.channel and after.channel.id == join_to_create:
                if member.id in user_join_create:
                    await member.send("⚠️ You already have an active VC!")
                    try:
                        await member.move_to(before.channel)
                    except:
                        pass
                    return
                overwrites = {
                    member.guild.default_role: discord.PermissionOverwrite(connect=True),
                    member.guild.me: discord.PermissionOverwrite(connect=True, manage_channels=True)
                }
                category = member.guild.get_channel(data["lfg_category"])
                new_vc = await member.guild.create_voice_channel(f"{member.display_name}'s VC", overwrites=overwrites, category=category)
                managed_vcs.add(new_vc.id)
                user_join_create[member.id] = new_vc.id
                await member.move_to(new_vc)
                schedule_vc_inactivity(new_vc, 60)
    except Exception as e:
        await dm_admin(f"Voice state update error: {e}")

# --- Bot Ready ---
@bot.event
async def on_ready():
    for guild_key in SERVERS:
        bot.add_view(DeployLFGButtonView(guild_key))
    print(f"✅ Logged in as {bot.user}")

# --- Keep bot alive & run ---
webserver.keep_alive()
bot.run(TOKEN, log_level=logging.DEBUG)
