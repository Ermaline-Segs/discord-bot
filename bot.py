import discord
from discord.ext import commands
import os
import random
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # needed so the bot can see role membership properly

bot = commands.Bot(command_prefix="!", intents=intents)

# ---- DATA: the shape of our government ----
STATES = ["Asaba", "Warri", "Ughelli", "Ozoro", "Kwale", "Agbor"]

NATIONAL_ROLES = [
    "President", "Vice President",
    "Senate President", "Deputy Senate President",
    "Speaker", "Deputy Speaker"
]

STATE_ROLES = [
    "Governor", "Deputy Governor",
    "State Speaker", "State Deputy Speaker"
]

# ---- HELPER: figure out the real role name from what the user typed ----
def resolve_role_name(role_input: str):
    words = role_input.strip().split()
    last_word = words[-1]

    # Check if the last word is a state name
    matching_state = next((s for s in STATES if s.lower() == last_word.lower()), None)

    if matching_state:
        title = " ".join(words[:-1])
        matching_title = next((r for r in STATE_ROLES if r.lower() == title.lower()), None)
        if matching_title:
            return f"{matching_title} of {matching_state}"
        return None
    else:
        matching_title = next((r for r in NATIONAL_ROLES if r.lower() == role_input.strip().lower()), None)
        if matching_title:
            return matching_title
        return None

# ---- COMMAND: appoint someone ----
@bot.command()
@commands.has_permissions(administrator=True)
async def appoint(ctx, member: discord.Member, *, role_input: str):
    final_role_name = resolve_role_name(role_input)

    if final_role_name is None:
        await ctx.send(
            "I don't recognize that role. Use a national title (e.g. `President`) "
            "or a state title with the state name (e.g. `Governor Asaba`)."
        )
        return

    # Get the role, or create it if it doesn't exist yet
    role = discord.utils.get(ctx.guild.roles, name=final_role_name)
    if role is None:
        role = await ctx.guild.create_role(name=final_role_name)

    # If someone already holds this role, remove it from them first (unique office)
    for old_holder in role.members:
        await old_holder.remove_roles(role)

    await member.add_roles(role)
    await ctx.send(f"{member.mention} is now the {final_role_name}.")

# ---- COMMAND: manually remove someone from a role ----
@bot.command()
@commands.has_permissions(administrator=True)
async def dismiss(ctx, member: discord.Member, *, role_input: str):
    final_role_name = resolve_role_name(role_input)

    if final_role_name is None:
        await ctx.send("I don't recognize that role.")
        return

    role = discord.utils.get(ctx.guild.roles, name=final_role_name)
    if role is None or role not in member.roles:
        await ctx.send(f"{member.mention} doesn't hold that role.")
        return

    await member.remove_roles(role)
    await ctx.send(f"{member.mention} has been removed from {final_role_name}.")

# ---- COMMAND: show who holds what ----
@bot.command()
async def government(ctx):
    lines = ["**National Government:**"]
    for title in NATIONAL_ROLES:
        role = discord.utils.get(ctx.guild.roles, name=title)
        if role and role.members:
            names = ", ".join(m.display_name for m in role.members)
            lines.append(f"{title}: {names}")
        else:
            lines.append(f"{title}: *vacant*")

    for state in STATES:
        lines.append(f"\n**{state} State Government:**")
        for title in STATE_ROLES:
            full_name = f"{title} of {state}"
            role = discord.utils.get(ctx.guild.roles, name=full_name)
            if role and role.members:
                names = ", ".join(m.display_name for m in role.members)
                lines.append(f"{title}: {names}")
            else:
                lines.append(f"{title}: *vacant*")

    await ctx.send("\n".join(lines))

# ---- your earlier commands ----
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

@bot.command()
async def hello(ctx):
    await ctx.send("Hello! I'm alive.")

@bot.command()
async def dice(ctx):
    roll = random.randint(1, 6)
    await ctx.send(f"You rolled a {roll}!")

bot.run(TOKEN)