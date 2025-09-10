import discord
from discord.ext import commands
import logging, os, asyncio
from dotenv import load_dotenv
import webserver

# --- Load token ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# --- Logging ---
handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')

# --- Intents ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

# --- Roles & Channels (replace with actual IDs) ---
LFG_ROLE_ID = 1413522249742286948  # LFG role
OFFICER_ROLE_IDS = [
    911755541020311553,  # Eternal
    1176539066569871531,   # High Council
    1413165455421734985    # Bot Developer
]
ALERT_CHANNEL_ID = 1414759057121873950
LFG_CATEGORY_ID = 1414750850701721703
JOIN_TO_CREATE_CHANNEL_ID = 1413590729942503474

# --- In-memory state ---
squads: dict[int, list[discord.Member]] = {}
managed_vcs: set[int] = set()
vc_inactivity_tasks: dict[int, asyncio.Task] = {}
user_active_lfg: dict[int, int] = {}
user_join_create: dict[int, int] = {}

# --- Helpers ---
def is_officer(member: discord.Member) -> bool:
    return any(role.id in OFFICER_ROLE_IDS for role in member.roles)

async def dm_admin(reason: str):
    try:
        owner = (await bot.application_info()).owner
        await owner.send(f"⚠️ LFG Bot Error: {reason}")
    except Exception:
        print(f"Failed to DM owner: {reason}")

async def delete_vc_safe(vc: discord.VoiceChannel):
    if not vc or not vc.guild:
        return
    try:
        await vc.delete()
    except Exception as e:
        await dm_admin(f"Failed to delete VC {vc.name}: {e}")
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
        except Exception as e:
            await dm_admin(f"VC inactivity task failed for {vc.name}: {e}")
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
        except Exception as e:
            await dm_admin(f"Failed to delete LFG post {msg.id}: {e}")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        await dm_admin(f"delete_post_after_duration failed: {e}")

# --- Persistent LFG Role Toggle ---
class LFGToggleView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Enlist/Unenlist", style=discord.ButtonStyle.primary, custom_id="lfg_toggle")
    async def toggle_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            role = interaction.guild.get_role(LFG_ROLE_ID)
            if not role:
                await interaction.response.send_message("⚠️ LFG role not found.", ephemeral=True)
                return
            if role in interaction.user.roles:
                await interaction.user.remove_roles(role)
                await interaction.response.send_message(f"❌ You have been unenlisted from {role.mention}.", ephemeral=True)
            else:
                await interaction.user.add_roles(role)
                await interaction.response.send_message(f"✅ You have been enlisted into {role.mention}!", ephemeral=True)
        except Exception as e:
            await dm_admin(f"LFGToggleView failed: {e}")
            await interaction.response.send_message("⚠️ Something went wrong.", ephemeral=True)

@bot.command()
async def post_lfg_signup(ctx):
    embed = discord.Embed(
        title="📢 LFG Role Signup",
        description="Click the button to **toggle enlistment**.",
        color=discord.Color.blue()
    )
    await ctx.send(embed=embed, view=LFGToggleView())

# --- Persistent Deploy Button ---
class DeployLFGView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Create LFG Post", style=discord.ButtonStyle.primary, custom_id="deploy_lfg")
    async def create_lfg_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.send_modal(LFGModal(interaction.user))
        except Exception as e:
            await dm_admin(f"DeployLFGView failed: {e}")
            await interaction.response.send_message("⚠️ Could not open modal.", ephemeral=True)

@bot.command()
async def post_lfg_button(ctx):
    await ctx.send("Click the button below to create an LFG post:", view=DeployLFGView())

# --- LFG Modal ---
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
        try:
            guild = interaction.guild
            alert_channel = guild.get_channel(ALERT_CHANNEL_ID)
            lfg_role = guild.get_role(LFG_ROLE_ID)
            lfg_category = guild.get_channel(LFG_CATEGORY_ID)
            if not alert_channel or not lfg_category:
                await interaction.response.send_message("⚠️ Setup issue, contact an Officer.", ephemeral=True)
                return

            if self.user.id in user_active_lfg and not is_officer(self.user):
                await interaction.response.send_message("⚠️ You already have an active LFG post.", ephemeral=True)
                return

            max_players = int(self.max_players_input.value)
            if max_players < 0:
                raise ValueError
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
            max_label = "∞" if max_players == 0 else str(max_players)
            embed.add_field(name="Current Squad", value=f"1/{max_label} {self.user.mention}", inline=False)
            embed.add_field(name="Max Party Size", value=max_label, inline=False)

            view = LFGView(msg_id=0, vc=temp_vc, max_players=max_players, host_id=self.user.id, host_name=self.host.value)
            squads_placeholder = [self.user]  # temporary
            squads[-1] = squads_placeholder  # temp key
            # Send the message without a view first
            msg = await alert_channel.send(content=f"{lfg_role.mention if lfg_role else ''} Looking for group!", embed=embed)
            # Assign correct msg_id and squad
            view.msg = msg
            view.msg_id = msg.id
            squads[msg.id] = [self.user]
            squads.pop(-1)
            await view.update_embed()
            # Send proper view for host so buttons appear
            await msg.edit(view=view.build_view_for(self.user))

            schedule_vc_inactivity(temp_vc, 60)
            asyncio.create_task(delete_post_after_duration(temp_vc, msg, 86400))
            user_active_lfg[self.user.id] = msg.id

            try:
                await self.user.move_to(temp_vc)
            except Exception:
                pass

            await interaction.response.send_message("✅ Your LFG has been posted!", ephemeral=True)

        except Exception as e:
            await dm_admin(f"LFGModal on_submit failed: {e}")
            await interaction.response.send_message("⚠️ Failed to create LFG post.", ephemeral=True)

# --- Dynamic LFG View ---
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
        try:
            embed = self.msg.embeds[0].copy()
            squad = squads.get(self.msg_id, [])
            idx = next((i for i, f in enumerate(embed.fields) if f.name == "Current Squad"), None)
            max_label = "∞" if self.max_players == 0 else str(self.max_players)
            value = "\n".join([f"{i+1}/{max_label} {m.mention}" for i, m in enumerate(squad)]) or "Empty"
            if idx is not None:
                embed.set_field_at(idx, name="Current Squad", value=value, inline=False)
            else:
                embed.add_field(name="Current Squad", value=value, inline=False)
            await self.msg.edit(embed=embed)
        except Exception as e:
            await dm_admin(f"LFGView update_embed failed: {e}")

    def build_view_for(self, member: discord.Member):
        view = LFGView(self.msg_id, self.vc, self.max_players, self.host_id, self.host_name)
        view.msg = self.msg
        squad = squads.get(self.msg_id, [])
        # Join/Leave buttons
        if member in squad:
            view.add_item(discord.ui.Button(label="Leave", style=discord.ButtonStyle.danger, custom_id="lfg_leave"))
        else:
            view.add_item(discord.ui.Button(label="Join", style=discord.ButtonStyle.success, custom_id="lfg_join"))
        # Officers and host see Delete
        if is_officer(member) or member.id == self.host_id:
            view.add_item(discord.ui.Button(label="Delete", style=discord.ButtonStyle.danger, custom_id="lfg_delete"))
        return view

    async def interaction_check(self, interaction: discord.Interaction):
        try:
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
                await interaction.response.edit_message(view=self.build_view_for(interaction.user))
                return False
            elif custom_id == "lfg_leave":
                if interaction.user in squad:
                    squad.remove(interaction.user)
                squads[self.msg_id] = squad
                await self.update_embed()
                await interaction.response.edit_message(view=self.build_view_for(interaction.user))
                return False
            elif custom_id == "lfg_delete":
                if not is_officer(interaction.user) and interaction.user.id != self.host_id:
                    await interaction.response.send_message("Only Officers or the Host can delete this LFG post.", ephemeral=True)
                    return False
                await delete_vc_safe(self.vc)
                try:
                    await self.msg.delete()
                except Exception as e:
                    await dm_admin(f"LFGView failed to delete message: {e}")
                user_active_lfg.pop(self.host_id, None)
                await interaction.response.send_message("✅ LFG post deleted.", ephemeral=True)
                return False
            return True
        except Exception as e:
            await dm_admin(f"LFGView interaction_check failed: {e}")
            await interaction.response.send_message("⚠️ Something went wrong.", ephemeral=True)
            return False

# --- Voice State Handling ---
@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    try:
        if before.channel and before.channel.id in managed_vcs and len(before.channel.members) == 0:
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
                except:
                    pass
                return
            overwrites = {
                member.guild.default_role: discord.PermissionOverwrite(connect=True),
                member.guild.me: discord.PermissionOverwrite(connect=True, manage_channels=True)
            }
            vc_name = f"{member.display_name}'s VC"
            new_vc = await member.guild.create_voice_channel(name=vc_name, overwrites=overwrites, category=after.channel.category)
            managed_vcs.add(new_vc.id)
            user_join_create[member.id] = new_vc.id
            await member.move_to(new_vc)
            schedule_vc_inactivity(new_vc, 10)
    except Exception as e:
        await dm_admin(f"on_voice_state_update failed for {member}: {e}")

# --- Bot Ready ---
@bot.event
async def on_ready():
    bot.add_view(LFGToggleView())
    bot.add_view(DeployLFGView())
    print(f"✅ Logged in as {bot.user}")
# --- Keep alive & run ---
webserver.keep_alive()
bot.run(TOKEN, log_handler=handler, log_level=logging.DEBUG)