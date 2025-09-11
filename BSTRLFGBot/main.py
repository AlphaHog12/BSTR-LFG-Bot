import discord
from discord.ext import commands, tasks
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
        "posting": 1414497675667308564,
        "lfg_category": 1413590729942503474,
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

# --- Storage ---
squads = {}               # {guild_id: {msg_id: {"members": [user_ids], "vc_id": int, "max_players": int}}}
managed_vcs = set()        # {vc.id}
vc_inactivity_tasks = {}   # {vc.id: asyncio.Task}
user_active_lfg = {}       # {user.id: msg_id}
user_join_create = {}      # {user.id: vc.id}

# --- Helper Functions ---
def is_officer(member: discord.Member) -> bool:
    return any(role.id in OFFICER_ROLE_IDS for role in member.roles)

async def dm_admin(msg: str):
    logger.info(f"[ADMIN DM] {msg}")

async def delete_vc_safe(vc: discord.VoiceChannel):
    try:
        # Remove references
        managed_vcs.discard(vc.id)
        for uid, vid in list(user_join_create.items()):
            if vid == vc.id:
                user_join_create.pop(uid, None)

        # Disable buttons in linked LFG post
        for guild_id, posts in squads.items():
            for msg_id, squad_data in posts.items():
                if squad_data.get("vc_id") == vc.id:
                    guild = vc.guild
                    alert_channel = None
                    for key, data in SERVERS.items():
                        if data["server_id"] == guild_id:
                            alert_channel = guild.get_channel(data["alert"])
                            break
                    if alert_channel:
                        try:
                            msg = await alert_channel.fetch_message(msg_id)
                            if msg:
                                view = discord.ui.View()
                                for row in msg.components:
                                    for child in row.children:
                                        if isinstance(child, discord.ui.Button):
                                            child.disabled = True
                                            view.add_item(child)
                                await msg.edit(view=view)
                        except Exception as e:
                            await dm_admin(f"Failed to update LFG message {msg_id}: {e}")
                    break

        await vc.delete()
    except Exception as e:
        await dm_admin(f"Failed to delete VC {vc.name}: {e}")

def schedule_vc_inactivity(vc: discord.VoiceChannel, delay: int = 60):
    async def _wait_and_delete():
        try:
            await asyncio.sleep(delay)
            current = vc.guild.get_channel(vc.id)
            if current and isinstance(current, discord.VoiceChannel) and len(current.members) == 0:
                await delete_vc_safe(current)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            await dm_admin(f"VC inactivity task failed for {vc.name}: {e}")

    # Cancel old task if exists
    old_task = vc_inactivity_tasks.get(vc.id)
    if old_task and not old_task.done():
        old_task.cancel()
    vc_inactivity_tasks[vc.id] = bot.loop.create_task(_wait_and_delete())

# --- LFG System ---
class LFGView(discord.ui.View):
    def __init__(self, msg_id, vc, max_players, host_id, guild_id):
        super().__init__(timeout=None)
        self.msg_id = msg_id
        self.vc = vc
        self.msg: discord.Message | None = None
        self.max_players = max_players
        self.host_id = host_id
        self.guild_id = guild_id

    async def update_embed(self):
        if not self.msg or not self.msg.embeds:
            return
        embed = self.msg.embeds[0].copy()
        squad_data = squads.get(self.guild_id, {}).get(self.msg_id, {})
        members = squad_data.get("members", [])
        max_label = "∞" if self.max_players == 0 else str(self.max_players)
        value = "\n".join([f"{i+1}/{max_label} <@{uid}>" for i, uid in enumerate(members)]) or "Empty"
        for i, f in enumerate(embed.fields):
            if f.name == "Current Squad":
                embed.set_field_at(i, name="Current Squad", value=value, inline=False)
                break
        await self.msg.edit(embed=embed)

    def build_view_for(self, member: discord.Member):
        view = discord.ui.View(timeout=None)
        squad_data = squads.get(self.guild_id, {}).get(self.msg_id, {})
        members = squad_data.get("members", [])

        if member.id in members:
            view.add_item(discord.ui.Button(label="Leave", style=discord.ButtonStyle.danger, custom_id="lfg_leave"))
        else:
            view.add_item(discord.ui.Button(label="Join", style=discord.ButtonStyle.success, custom_id="lfg_join"))

        if member.id == self.host_id or is_officer(member):
            view.add_item(discord.ui.Button(label="Delete", style=discord.ButtonStyle.danger, custom_id="lfg_delete"))

        return view

    async def interaction_check(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        squad_data = squads.get(self.guild_id, {}).get(self.msg_id, {})
        if not squad_data:
            await interaction.followup.send("⚠️ LFG post no longer exists.", ephemeral=True)
            return False

        members = squad_data.get("members", [])
        user_id = interaction.user.id
        custom_id = interaction.data.get("custom_id")

        if custom_id == "lfg_join":
            if self.max_players != 0 and len(members) >= self.max_players:
                await interaction.followup.send("⚠️ Party is full!", ephemeral=True)
                return False
            if user_id not in members:
                members.append(user_id)
                squad_data["members"] = members
                squads[self.guild_id][self.msg_id] = squad_data
            await self.update_embed()
            await self.msg.edit(view=self.build_view_for(interaction.user))
            return False

        elif custom_id == "lfg_leave":
            if user_id in members:
                members.remove(user_id)
                squad_data["members"] = members
                squads[self.guild_id][self.msg_id] = squad_data
            await self.update_embed()
            await self.msg.edit(view=self.build_view_for(interaction.user))
            return False

        elif custom_id == "lfg_delete":
            if user_id != self.host_id and not is_officer(interaction.user):
                await interaction.followup.send("Only the host or officers can delete this post.", ephemeral=True)
                return False

            if self.vc:
                await delete_vc_safe(self.vc)

            try:
                for child in self.children:
                    child.disabled = True
                await self.msg.edit(view=self)
                await self.msg.delete()
            except:
                pass

            user_active_lfg.pop(self.host_id, None)
            self.stop()
            return False

        return True

class LFGModal(discord.ui.Modal):
    def __init__(self, user: discord.Member, guild_key: str):
        super().__init__(title="Create LFG Post")
        self.user = user
        self.guild_key = guild_key
        self.host = discord.ui.TextInput(label="Host", placeholder="Who is leading the group?", required=True)
        self.description = discord.ui.TextInput(label="Channel Description", placeholder="What is this squad doing?", required=True)
        self.max_players_input = discord.ui.TextInput(label="Max Party Size (0=unlimited)", placeholder="Enter a number", required=True)
        self.add_item(self.host)
        self.add_item(self.description)
        self.add_item(self.max_players_input)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        alert_channel = guild.get_channel(SERVERS[self.guild_key]["alert"])
        lfg_category = guild.get_channel(SERVERS[self.guild_key]["lfg_category"])

        if self.user.id in user_active_lfg and not is_officer(self.user):
            await interaction.response.send_message("⚠️ You already have an active LFG post.", ephemeral=True)
            return

        max_players = int(self.max_players_input.value)
        user_limit = None if max_players == 0 else max_players

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(connect=True),
            guild.me: discord.PermissionOverwrite(connect=True, manage_channels=True)
        }

        vc_name = self.description.value.strip()
        temp_vc = await guild.create_voice_channel(name=vc_name, overwrites=overwrites, category=lfg_category)
        if user_limit:
            await temp_vc.edit(user_limit=user_limit)
        managed_vcs.add(temp_vc.id)

        embed = discord.Embed(title=vc_name, color=discord.Color.blue())
        embed.add_field(name="Host", value=self.host.value, inline=False)
        embed.add_field(name="Voice Channel", value=temp_vc.mention, inline=False)
        max_label = "∞" if max_players == 0 else str(max_players)
        embed.add_field(name="Current Squad", value=f"1/{max_label} {self.user.mention}", inline=False)
        embed.add_field(name="Max Party Size", value=max_label, inline=False)

        msg = await alert_channel.send(content=f"{self.user.mention} is looking for a group!", embed=embed)
        guild_id = guild.id
        if guild_id not in squads:
            squads[guild_id] = {}
        squads[guild_id][msg.id] = {"members": [self.user.id], "vc_id": temp_vc.id, "max_players": max_players}

        view = LFGView(msg.id, temp_vc, max_players, self.user.id, guild_id)
        view.msg = msg
        await msg.edit(view=view.build_view_for(self.user))

        schedule_vc_inactivity(temp_vc, 60)
        user_active_lfg[self.user.id] = msg.id

        try:
            await self.user.move_to(temp_vc)
        except:
            pass

        await interaction.response.send_message("✅ Your LFG has been posted!", ephemeral=True)

class DeployLFGButtonView(discord.ui.View):
    def __init__(self, guild_key):
        super().__init__(timeout=None)
        self.guild_key = guild_key

    @discord.ui.button(label="Create LFG Post", style=discord.ButtonStyle.primary, custom_id="deploy_lfg")
    async def deploy_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(LFGModal(interaction.user, self.guild_key))

# --- Voice State Updates ---
@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    try:
        # Schedule VC deletion for empty old channel
        if before.channel and before.channel.id in managed_vcs and len(before.channel.members) == 0:
            schedule_vc_inactivity(before.channel, 60)

        # Cancel deletion if someone joins
        if after.channel and after.channel.id in managed_vcs:
            task = vc_inactivity_tasks.get(after.channel.id)
            if task and not task.done():
                task.cancel()

        # Join-to-create VC
        for guild_key, data in SERVERS.items():
            join_to_create = data["join_to_create"]
            if after.channel and after.channel.id == join_to_create:
                if member.id in user_join_create:
                    await member.send("⚠️ You already have an active Join-to-Create VC!")
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
                vc_name = f"{member.display_name}'s VC"
                new_vc = await member.guild.create_voice_channel(name=vc_name, overwrites=overwrites, category=category)
                managed_vcs.add(new_vc.id)
                user_join_create[member.id] = new_vc.id
                await member.move_to(new_vc)
                schedule_vc_inactivity(new_vc, 60)
    except Exception as e:
        await dm_admin(f"Voice state update error: {e}")

# --- Bot Ready ---
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")
    # Register all persistent views for posts on startup
    for guild_key, data in SERVERS.items():
        alert_channel = bot.get_channel(data["alert"])
        if alert_channel:
            view = DeployLFGButtonView(guild_key)
            try:
                await alert_channel.send("Click to create an LFG post!", view=view)
            except:
                pass

# --- Keep Bot Alive & Run ---
webserver.keep_alive()
bot.run(TOKEN)
