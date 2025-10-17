import discord
import ssl
import aiohttp
import certifi
from discord import app_commands, Permissions
from discord.ext import commands
import sqlite3
import time
from datetime import timedelta
from datetime import datetime
from discord.ui import View, Button


# --- In-Memory Data Structure ---
# Format: { guild_id: { currency_name: { user_id: amount, ... }, ... }, ... }
currencies_data = {}
# Format: { guild_id: { "homework": seconds, "officehours": seconds } }
guild_cooldowns = {}


DB_PATH = "currencies.db"

COOLDOWN_SECONDS = 6 * 60 * 60  # 6 hours


# --- Database Helpers ---

def load_db():
    """Load database into in-memory dictionary."""
    global currencies_data, hw_cooldowns, office_cooldowns
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Create tables if not exist
    c.execute("""
        CREATE TABLE IF NOT EXISTS currencies (
    guild_id INTEGER,
    name TEXT,
    emoji TEXT,
    PRIMARY KEY (guild_id, name)
)

    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS balances (
            guild_id INTEGER,
            user_id INTEGER,
            currency TEXT,
            amount INTEGER,
            PRIMARY KEY (guild_id, user_id, currency)
        )
    """)
    # New table for cooldowns
    c.execute("""
        CREATE TABLE IF NOT EXISTS cooldowns (
            guild_id INTEGER,
            user_id INTEGER,
            command TEXT,
            last_used REAL,
            PRIMARY KEY (guild_id, user_id, command)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id INTEGER PRIMARY KEY,
            homework_cooldown INTEGER DEFAULT 21600,
            officehours_cooldown INTEGER DEFAULT 21600
        )
    """)


    # Load currencies
    # Load currencies with emoji
    c.execute("SELECT guild_id, name, emoji FROM currencies")
    rows = c.fetchall()
    for guild_id, name, emoji in rows:
        currencies_data.setdefault(guild_id, {})[name] = {"emoji": emoji, "balances": {}}


    # --- Load balances ---
    c.execute("SELECT guild_id, user_id, currency, amount FROM balances")
    for guild_id, user_id, currency, amount in c.fetchall():
        if guild_id in currencies_data and currency in currencies_data[guild_id]:
            currencies_data[guild_id][currency]["balances"][user_id] = amount

    # Load cooldowns
    hw_cooldowns = {}
    office_cooldowns = {}
    c.execute("SELECT guild_id, user_id, command, last_used FROM cooldowns")
    rows = c.fetchall()
    for guild_id, user_id, command, last_used in rows:
        if command == "homework":
            hw_cooldowns.setdefault(guild_id, {})[user_id] = last_used
        elif command == "officehours":
            office_cooldowns.setdefault(guild_id, {})[user_id] = last_used

    # Load from DB
    c.execute("SELECT guild_id, homework_cooldown, officehours_cooldown FROM guild_settings")
    for guild_id, hw_cd, office_cd in c.fetchall():
        guild_cooldowns[guild_id] = {
            "homework": hw_cd,
            "officehours": office_cd
    }

    conn.commit()
    conn.close()

def save_guild_cooldowns():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    for guild_id, cds in guild_cooldowns.items():
        c.execute("""
        INSERT INTO guild_settings (guild_id, homework_cooldown, officehours_cooldown)
        VALUES (?, ?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET
            homework_cooldown=excluded.homework_cooldown,
            officehours_cooldown=excluded.officehours_cooldown
        """, (guild_id, cds["homework"], cds["officehours"]))
    conn.commit()
    conn.close()



def save_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("DELETE FROM currencies")
    c.execute("DELETE FROM balances")
    c.execute("DELETE FROM cooldowns")

    for guild_id, guild_currencies in currencies_data.items():
        for currency_name, data in guild_currencies.items():
            emoji = data.get("emoji", "")
            balances = data.get("balances", {})

            # Save emoji
            c.execute("INSERT INTO currencies VALUES (?, ?, ?)", (guild_id, currency_name, emoji))

            # Save balances
            for user_id, amount in balances.items():
                c.execute("INSERT INTO balances VALUES (?, ?, ?, ?)", (guild_id, user_id, currency_name, amount))

    # Save cooldowns
    for guild_id, users in hw_cooldowns.items():
        for user_id, last_used in users.items():
            c.execute("INSERT OR REPLACE INTO cooldowns VALUES (?, ?, ?, ?)",
                      (guild_id, user_id, "homework", last_used))

    for guild_id, users in office_cooldowns.items():
        for user_id, last_used in users.items():
            c.execute("INSERT OR REPLACE INTO cooldowns VALUES (?, ?, ?, ?)",
                      (guild_id, user_id, "officehours", last_used))

    conn.commit()
    conn.close()


# --- Bot Setup ---


# Patch default SSL context so aiohttp uses certifi bundle
ssl_context = ssl.create_default_context(cafile=certifi.where())
ssl._create_default_https_context = lambda: ssl_context

# --- Now import and run your bot ---
intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

load_db()


@bot.tree.command(name="set_cooldown", description="Set cooldown for homework or office hours for this server.")
@app_commands.describe(command="Which command to set", seconds="Cooldown in seconds")
@app_commands.choices(command=[
    app_commands.Choice(name="homework", value="homework"),
    app_commands.Choice(name="officehours", value="officehours")
])
@app_commands.checks.has_permissions(manage_guild=True)
async def set_cooldown(interaction: discord.Interaction, command: app_commands.Choice[str], seconds: int):
    guild_id = interaction.guild.id
    if seconds < 0:
        await interaction.response.send_message("❌ Cooldown must be 0 or greater.", ephemeral=True)
        return

    guild_cooldowns.setdefault(guild_id, {"homework": COOLDOWN_SECONDS, "officehours": COOLDOWN_SECONDS})
    guild_cooldowns[guild_id][command.value] = seconds
    save_guild_cooldowns()

    await interaction.response.send_message(f"✅ {command.value} cooldown set to {seconds} seconds for this server.")


import re

def parse_emoji(emoji_str):
    """
    Returns a valid emoji string if it's Unicode or a valid custom emoji.
    Returns None if invalid.
    """
    # Match custom emoji: <a:name:id> or <:name:id>
    custom_emoji_regex = r"<a?:\w+:\d+>"
    if re.fullmatch(custom_emoji_regex, emoji_str):
        return emoji_str  # valid custom emoji
    elif emoji_str:
        return emoji_str  # assume Unicode emoji
    return None


# --- Slash command: add_currency with emoji ---
@bot.tree.command(name="add_currency", description="Add a new currency for this guild with an emoji.")
@app_commands.describe(
    name="Name of the currency to add",
    emoji="Emoji to represent the currency"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def add_currency(interaction: discord.Interaction, name: str, emoji: str):
    guild_id = interaction.guild.id
    guild_currencies = currencies_data.setdefault(guild_id, {})

    if name in guild_currencies:
        embed = discord.Embed(
            title="⚠️ Currency Already Exists",
            description=f"❌ Currency `{name}` already exists!",
            color=discord.Color.red()
        )
        embed.set_footer(text=f"Requested by {interaction.user}", icon_url=interaction.user.avatar.url if interaction.user.avatar else None)
        embed.timestamp = datetime.utcnow()
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # Save currency with emoji
    emoji_parsed = parse_emoji(emoji)
    if not emoji_parsed:
        await interaction.response.send_message(
            "❌ Invalid emoji! Use a Unicode emoji or a server custom emoji.",
            ephemeral=True
        )
        return

    # Save currency with emoji
    guild_currencies[name] = {"emoji": emoji_parsed, "balances": {}}
    save_db()


    embed = discord.Embed(
        title=f"{emoji} Currency Added!",
        description=f"✅ {interaction.user.mention} added the currency **{name}** with emoji {emoji} to this server!",
        color=discord.Color.green()
    )
    embed.set_footer(text=f"Requested by {interaction.user}", icon_url=interaction.user.avatar.url if interaction.user.avatar else None)
    embed.timestamp = datetime.utcnow()
    await interaction.response.send_message(embed=embed)

# --- Autocomplete function ---
async def currency_autocomplete(interaction: discord.Interaction, current: str):
    guild_id = interaction.guild.id
    guild_currencies = currencies_data.get(guild_id, {})
    return [
        app_commands.Choice(name=name, value=name)
        for name in guild_currencies.keys()
        if current.lower() in name.lower()
    ][:25]  # Discord allows max 25 choices

@bot.tree.command(name="set_balance", description="Set a user's balance for a currency.")
@app_commands.describe(currency="Currency name", user="User to set balance for", amount="Balance amount")
@app_commands.autocomplete(currency=currency_autocomplete)  # attach autocomplete
@app_commands.checks.has_permissions(manage_guild=True)
async def set_balance(interaction: discord.Interaction, currency: str, user: discord.User, amount: int):
    guild_id = interaction.guild.id
    guild_currencies = currencies_data.get(guild_id, {})

    if currency not in guild_currencies:
        await interaction.response.send_message(f"❌ Currency `{currency}` not found!", ephemeral=True)
        return

    guild_currencies[currency]["balances"][user.id] = amount
    save_db()
    await interaction.response.send_message(f"✅ Set {user.mention}'s `{currency}` balance to `{amount}`.")

@set_balance.error
async def set_balance_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.errors.MissingPermissions):
        embed = discord.Embed(
            title="Permission Denied",
            description="❌ You do not have permission to use this command.",
            color=discord.Color.red()
        )
        embed.set_footer(text=f"Requested by {interaction.user}", icon_url=interaction.user.avatar.url if interaction.user.avatar else None)
        embed.timestamp = datetime.utcnow()
        await interaction.response.send_message(embed=embed, ephemeral=True)

async def get_currency_choices(interaction: discord.Interaction):
    """Return a list of available currencies for the guild as Choices."""
    guild_id = interaction.guild.id
    guild_currencies = currencies_data.get(guild_id, {})
    choices = [app_commands.Choice(name=name, value=name) for name in guild_currencies.keys()]
    return choices



@bot.tree.command(name="balance", description="Check a user's balance for all currencies in this guild.")
@app_commands.describe(user="User to check (optional)")
async def balance(interaction: discord.Interaction, user: discord.User = None):
    guild_id = interaction.guild.id
    user = user or interaction.user
    guild_currencies = currencies_data.get(guild_id, {})

    if not guild_currencies:
        embed = discord.Embed(
            title="No Currencies Found",
            description="❌ This guild has no currencies yet.",
            color=discord.Color.red()
        )
        embed.set_footer(text=f"Requested by {interaction.user}", icon_url=interaction.user.avatar.url if interaction.user.avatar else None)
        embed.timestamp = datetime.utcnow()
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # Build balance description
    description = ""
    for currency_name, data in guild_currencies.items():
        emoji = data.get("emoji", "")
        amount = data.get("balances", {}).get(user.id, 0)
        description += f"{emoji} **{currency_name}:** {amount}\n"


    embed = discord.Embed(
        title=f"{user.display_name}'s Balances",
        description=description,
        color=discord.Color.blue()
    )
    embed.set_footer(text=f"Requested by {interaction.user}", icon_url=interaction.user.avatar.url if interaction.user.avatar else None)
    embed.timestamp = datetime.utcnow()
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="list_currencies", description="List all currencies in this guild.")
async def list_currencies(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    guild_currencies = currencies_data.get(guild_id, {})

    if not guild_currencies:
        embed = discord.Embed(
            title="💱 No Currencies Found",
            description="❌ This guild has no currencies yet.",
            color=discord.Color.red()
        )
        embed.set_footer(
            text=f"Requested by {interaction.user}",
            icon_url=interaction.user.avatar.url if interaction.user.avatar else None
        )
        embed.timestamp = datetime.utcnow()
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # Build currency list with emojis
    description = ""
    for name, data in guild_currencies.items():
        emoji = data.get("emoji", "")
        description += f"{emoji} **{name}**\n"

    embed = discord.Embed(
        title="💱 Available Currencies",
        description=description,
        color=discord.Color.blue()
    )
    embed.set_footer(
        text=f"Requested by {interaction.user}",
        icon_url=interaction.user.avatar.url if interaction.user.avatar else None
    )
    embed.timestamp = datetime.utcnow()

    await interaction.response.send_message(embed=embed)


import random

# --- /homework command ---
@bot.tree.command(name="homework", description="Do homework to earn a random currency!")
async def homework(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    user_id = interaction.user.id
    hw_cd = guild_cooldowns.get(guild_id, {}).get("homework", COOLDOWN_SECONDS)
    guild_currencies = currencies_data.get(guild_id, {})

    if not guild_currencies:
        embed = discord.Embed(
            title="No Currencies Found",
            description="❌ No currencies available in this guild.",
            color=discord.Color.red()
        )
        embed.set_footer(text=f"Requested by {interaction.user}", icon_url=interaction.user.avatar.url if interaction.user.avatar else None)
        embed.timestamp = datetime.utcnow()
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # Check cooldown
    cd = guild_cooldowns.get(guild_id, {}).get("homework", COOLDOWN_SECONDS)
    last_time = hw_cooldowns.get(guild_id, {}).get(user_id, 0)
    now = time.time()
    if now - last_time < cd:
        remaining = cd - (now - last_time)
        embed = discord.Embed(
            title="Cooldown Active",
            description=f"⏱ You must wait {timedelta(seconds=int(remaining))} before doing homework again.",
            color=discord.Color.orange()
        )
        embed.set_footer(text=f"Requested by {interaction.user}", icon_url=interaction.user.avatar.url if interaction.user.avatar else None)
        embed.timestamp = datetime.utcnow()
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # Pick a random currency and amount
    currency = random.choice(list(guild_currencies.keys()))
    amount = random.randint(1, 10)

    # Update balance
    user_balances = guild_currencies[currency]["balances"]
    user_balances[user_id] = user_balances.get(user_id, 0) + amount

   

    # Update cooldown
    hw_cooldowns.setdefault(guild_id, {})[user_id] = now

    save_db()

    embed = discord.Embed(
        title="Homework Completed!",
        description=f"🎓 {interaction.user.mention} did homework and earned **{amount} {currency}**!",
        color=discord.Color.green()
    )
    embed.set_footer(text=f"Requested by {interaction.user}", icon_url=interaction.user.avatar.url if interaction.user.avatar else None)
    embed.timestamp = datetime.utcnow()
    await interaction.response.send_message(embed=embed)


# --- /officehours command ---
@bot.tree.command(name="officehours", description="Attend office hours to earn a random currency!")
async def officehours(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    user_id = interaction.user.id
    guild_currencies = currencies_data.get(guild_id, {})

    if not guild_currencies:
        await interaction.response.send_message("❌ No currencies available in this guild.", ephemeral=True)
        return

    # Check cooldown
    cd = guild_cooldowns.get(guild_id, {}).get("officehours", COOLDOWN_SECONDS)
    last_time = office_cooldowns.get(guild_id, {}).get(user_id, 0)
    now = time.time()
    if now - last_time < cd:
        remaining = cd - (now - last_time)
        embed = discord.Embed(
            title="Cooldown Active",
            description=f"⏱ You must wait {timedelta(seconds=int(remaining))} before attending office hours again.",
            color=discord.Color.orange()
        )
        embed.set_footer(text=f"Requested by {interaction.user}", icon_url=interaction.user.avatar.url if interaction.user.avatar else None)
        embed.timestamp = datetime.utcnow()
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # Pick a random currency and amount
    currency = random.choice(list(guild_currencies.keys()))
    amount = random.randint(1, 10)

    # Update balance
    user_balances = guild_currencies[currency]["balances"]
    user_balances[user_id] = user_balances.get(user_id, 0) + amount
    

    # Update cooldown
    office_cooldowns.setdefault(guild_id, {})[user_id] = now

    save_db()

    embed = discord.Embed(
        title="Office Hours Attended!",
        description=f"🎓 {interaction.user.mention} attended office hours and earned **{amount} {currency}**!",
        color=discord.Color.green()
    )
    embed.set_footer(text=f"Requested by {interaction.user}", icon_url=interaction.user.avatar.url if interaction.user.avatar else None)
    embed.timestamp = datetime.utcnow()
    await interaction.response.send_message(embed=embed)


from discord.ui import View, Button
from datetime import datetime
import discord
from discord import app_commands

# --- Slash command: remove_currency ---
@bot.tree.command(name="remove_currency", description="Remove a currency from this guild.")
@app_commands.describe(name="Name of the currency to remove")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.autocomplete(name=currency_autocomplete)  # attach autocomplete
async def remove_currency(interaction: discord.Interaction, name: str):
    guild_id = interaction.guild.id
    guild_currencies = currencies_data.get(guild_id, {})

    if name not in guild_currencies:
        embed = discord.Embed(
            title="❌ Currency Not Found",
            description=f"Currency `{name}` does not exist in this server.",
            color=discord.Color.red()
        )
        embed.set_footer(text=f"Requested by {interaction.user}", icon_url=interaction.user.avatar.url if interaction.user.avatar else None)
        embed.timestamp = datetime.utcnow()
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # --- Confirmation Buttons ---
    class ConfirmRemove(View):
        def __init__(self, command_interaction: discord.Interaction):
            super().__init__(timeout=30)
            self.command_interaction = command_interaction  # original /remove_currency call

        @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
        async def confirm(self, interaction: discord.Interaction, button: Button):

            if interaction.user.id != self.command_interaction.user.id:
                await interaction.response.send_message("❌ You cannot confirm this.", ephemeral=True)
                return

            # Remove currency
            guild_id = self.command_interaction.guild.id
            guild_currencies = currencies_data.get(guild_id, {})
            del guild_currencies[name]
            save_db()

            embed = discord.Embed(
                title=f"🗑️ Currency Removed",
                description=f"✅ {self.command_interaction.user.mention} removed the currency **{name}** from this server.",
                color=discord.Color.orange()
            )
            embed.set_footer(
                text=f"Requested by {self.command_interaction.user}",
                icon_url=self.command_interaction.user.avatar.url if self.command_interaction.user.avatar else None
            )
            embed.timestamp = datetime.utcnow()

            await interaction.response.edit_message(embed=embed, view=None)

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, button: Button):

            if interaction.user.id != self.command_interaction.user.id:
                await interaction.response.send_message("❌ You cannot cancel this.", ephemeral=True)
                return

            embed = discord.Embed(
                title="❌ Removal Cancelled",
                description=f"Currency `{name}` was not removed.",
                color=discord.Color.red()
            )
            embed.set_footer(
                text=f"Requested by {self.command_interaction.user}",
                icon_url=self.command_interaction.user.avatar.url if self.command_interaction.user.avatar else None
            )
            embed.timestamp = datetime.utcnow()

            await interaction.response.edit_message(embed=embed, view=None)

    # Send confirmation embed
    embed = discord.Embed(
        title="⚠️ Confirm Currency Removal",
        description=f"Are you sure you want to remove the currency **{name}**?\nThis action **cannot be undone**.",
        color=discord.Color.yellow()
    )
    embed.set_footer(text=f"Requested by {interaction.user}", icon_url=interaction.user.avatar.url if interaction.user.avatar else None)
    embed.timestamp = datetime.utcnow()

    await interaction.response.send_message(embed=embed, view=ConfirmRemove(interaction))

@bot.tree.command(name="give", description="Give a certain amount of a currency to another user.")
@app_commands.describe(
    currency="Currency to give",
    user="User to give to",
    amount="Amount to give"
)
@app_commands.autocomplete(currency=currency_autocomplete)
async def give(interaction: discord.Interaction, currency: str, user: discord.User, amount: int):
    if user.id == interaction.user.id:
        await interaction.response.send_message("❌ You cannot give currency to yourself.", ephemeral=True)
        return

    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be greater than zero.", ephemeral=True)
        return

    guild_id = interaction.guild.id
    guild_currencies = currencies_data.get(guild_id, {})

    if currency not in guild_currencies:
        await interaction.response.send_message(f"❌ Currency `{currency}` not found!", ephemeral=True)
        return

    sender_balances = guild_currencies[currency]["balances"]
    receiver_balances = guild_currencies[currency]["balances"]

    sender_amount = sender_balances.get(interaction.user.id, 0)
    if sender_amount < amount:
        await interaction.response.send_message(f"❌ You only have {sender_amount} {currency}.", ephemeral=True)
        return

    # Transfer
    sender_balances[interaction.user.id] = sender_amount - amount
    receiver_balances[user.id] = receiver_balances.get(user.id, 0) + amount
    save_db()

    emoji = guild_currencies[currency].get("emoji", "")

    embed = discord.Embed(
        title=f"{emoji} Currency Transferred",
        description=f"✅ {interaction.user.mention} gave **{amount} {currency}** to {user.mention}.",
        color=discord.Color.green()
    )
    embed.set_footer(text=f"Requested by {interaction.user}", icon_url=interaction.user.avatar.url if interaction.user.avatar else None)
    embed.timestamp = datetime.utcnow()
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="leaderboard", description="Show the top users for a specific currency.")
@app_commands.describe(currency="Currency to display leaderboard for")
@app_commands.autocomplete(currency=currency_autocomplete)
async def leaderboard(interaction: discord.Interaction, currency: str):
    guild_id = interaction.guild.id
    guild_currencies = currencies_data.get(guild_id, {})

    if currency not in guild_currencies:
        embed = discord.Embed(
            title="❌ Currency Not Found",
            description=f"Currency `{currency}` does not exist in this server.",
            color=discord.Color.red()
        )
        embed.set_footer(text=f"Requested by {interaction.user}", icon_url=interaction.user.avatar.url if interaction.user.avatar else None)
        embed.timestamp = datetime.utcnow()
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # Get balances and sort
    balances = guild_currencies[currency]["balances"]
    if not balances:
        await interaction.response.send_message(f"❌ No one has any `{currency}` yet.", ephemeral=True)
        return

    top_users = sorted(balances.items(), key=lambda x: x[1], reverse=True)[:10]  # top 10
    description = ""
    for i, (user_id, amount) in enumerate(top_users, start=1):
        user = interaction.guild.get_member(user_id)
        username = user.display_name if user else f"<Unknown User {user_id}>"
        description += f"**{i}. {username}** {amount}\n"

    embed = discord.Embed(
        title=f"🏆 {currency} Leaderboard",
        description=description,
        color=discord.Color.gold()
    )
    embed.set_footer(text=f"Requested by {interaction.user}", icon_url=interaction.user.avatar.url if interaction.user.avatar else None)
    embed.timestamp = datetime.utcnow()
    await interaction.response.send_message(embed=embed)

# --- /commands command ---
@bot.tree.command(name="commands", description="Show all available commands for this bot.")
async def commands_list(interaction: discord.Interaction):
    all_cmds = bot.tree.get_commands(guild=None)
  # get guild commands
    if not all_cmds:
        await interaction.response.send_message("❌ No commands found.", ephemeral=True)
        return

    embed = discord.Embed(
        title="🤖 Bot Commands",
        description="Here are the available commands for this server:",
        color=discord.Color.blue()
    )

    for cmd in all_cmds:
        name = cmd.name
        desc = cmd.description or "No description provided."
        embed.add_field(name=f"/{name}", value=desc, inline=False)

    embed.set_footer(
        text=f"Requested by {interaction.user}",
        icon_url=interaction.user.avatar.url if interaction.user.avatar else None
    )
    embed.timestamp = datetime.utcnow()

    await interaction.response.send_message(embed=embed)

from discord import Interaction, Embed

@bot.tree.command(name="rename", description="Rename a currency in this guild.")
@app_commands.describe(
    old_name="The current name of the currency",
    new_name="The new name you want to give it"
)
@app_commands.autocomplete(old_name=currency_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
async def rename(interaction: Interaction, old_name: str, new_name: str):
    guild_id = interaction.guild.id
    guild_currencies = currencies_data.get(guild_id, {})

    if old_name not in guild_currencies:
        await interaction.response.send_message(f"❌ Currency `{old_name}` not found.", ephemeral=True)
        return

    if new_name in guild_currencies:
        await interaction.response.send_message(f"❌ Currency `{new_name}` already exists.", ephemeral=True)
        return

    # Rename currency
    guild_currencies[new_name] = guild_currencies.pop(old_name)
    save_db()

    embed = Embed(
        title="✅ Currency Renamed",
        description=f"Currency `{old_name}` has been renamed to `{new_name}`.\nAll balances and emoji remain intact.",
        color=discord.Color.green()
    )
    embed.timestamp = datetime.utcnow()
    await interaction.response.send_message(embed=embed)


import random
from discord import app_commands, Interaction, Embed
from datetime import datetime

# --- /gamble command ---
@bot.tree.command(
    name="gamble",
    description="Gamble some of your currency for a chance to win more!"
)
@app_commands.describe(
    currency="Currency to gamble",
    amount="Amount to gamble"
)
@app_commands.autocomplete(currency=currency_autocomplete)
async def gamble(interaction: Interaction, currency: str, amount: int):
    guild_id = interaction.guild.id
    user_id = interaction.user.id
    guild_currencies = currencies_data.get(guild_id, {})

    # Validation
    if currency not in guild_currencies:
        await interaction.response.send_message(f"❌ Currency `{currency}` not found!", ephemeral=True)
        return

    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be greater than zero.", ephemeral=True)
        return

    user_balance = guild_currencies[currency]["balances"].get(user_id, 0)
    if user_balance < amount:
        await interaction.response.send_message(f"❌ You only have {user_balance} {currency}.", ephemeral=True)
        return

    # Gamble logic: 50% chance to double, 50% chance to lose
    win = random.random() < 0.5
    if win:
        winnings = amount
        guild_currencies[currency]["balances"][user_id] += winnings
        result_text = f"🎉 You won! You gained **{winnings} {currency}**!"
        color = 0x00FF00  # Green
    else:
        guild_currencies[currency]["balances"][user_id] -= amount
        result_text = f"💀 You lost **{amount} {currency}**."
        color = 0xFF0000  # Red

    # Save to database
    save_db()

    # Embed response
    emoji = guild_currencies[currency].get("emoji", "")
    embed = Embed(
        title=f"{emoji} Gamble Result",
        description=result_text,
        color=color
    )
    embed.set_footer(text=f"Called by {interaction.user}", icon_url=interaction.user.avatar.url if interaction.user.avatar else None)
    embed.timestamp = datetime.utcnow()

    await interaction.response.send_message(embed=embed)



@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    try:
        await bot.tree.sync()

    except Exception as e:
        print(f"❌ Sync failed: {e}")

# --- Run Bot ---
bot.run("MTQyODc1MzkxMDUyODczNzI5MQ.GaJZMc.HqXb-r_OFURZ9dGzfqQ0Md8If9UBJCqX9b__qM")
