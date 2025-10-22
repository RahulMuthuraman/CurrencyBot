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
    global currencies_data, hw_cooldowns, office_cooldowns, rob_cooldowns, jackpots, inventories, active_buffs
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
    c.execute("""
        CREATE TABLE IF NOT EXISTS jackpots (
            guild_id INTEGER,
            currency TEXT,
            amount INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, currency)
        )
    """)

    # --- Inventories ---
    c.execute("""
        CREATE TABLE IF NOT EXISTS inventories (
            guild_id INTEGER,
            user_id INTEGER,
            item TEXT,
            quantity INTEGER,
            PRIMARY KEY (guild_id, user_id, item)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS active_buffs (
            guild_id INTEGER,
            user_id INTEGER,
            buff_name TEXT,
            PRIMARY KEY (guild_id, user_id, buff_name)
        )
    """)

    # --- Load jackpots ---
    jackpots = {}
    c.execute("SELECT guild_id, currency, amount FROM jackpots")
    for guild_id, currency, amount in c.fetchall():
        jackpots.setdefault(guild_id, {})[currency] = amount

    # --- Load currencies ---
    currencies_data.clear()
    c.execute("SELECT guild_id, name, emoji FROM currencies")
    for guild_id, name, emoji in c.fetchall():
        currencies_data.setdefault(guild_id, {})[name] = {"emoji": emoji, "balances": {}}

    # --- Load balances ---
    c.execute("SELECT guild_id, user_id, currency, amount FROM balances")
    for guild_id, user_id, currency, amount in c.fetchall():
        if guild_id in currencies_data and currency in currencies_data[guild_id]:
            currencies_data[guild_id][currency]["balances"][user_id] = amount

    # --- Load cooldowns ---
    hw_cooldowns = {}
    office_cooldowns = {}
    rob_cooldowns = {}
    inventories = {}
    active_buffs = {}


    c.execute("SELECT guild_id, user_id, command, last_used FROM cooldowns")
    for guild_id, user_id, command, last_used in c.fetchall():
        if command == "homework":
            hw_cooldowns.setdefault(guild_id, {})[user_id] = last_used
        elif command == "officehours":
            office_cooldowns.setdefault(guild_id, {})[user_id] = last_used
        elif command == "rob":
            rob_cooldowns.setdefault(guild_id, {})[user_id] = last_used

    # --- Load guild settings ---
    c.execute("SELECT guild_id, homework_cooldown, officehours_cooldown, rob_cooldown FROM guild_settings")
    for guild_id, hw_cd, office_cd, rob_cd in c.fetchall():
        guild_cooldowns[guild_id] = {
            "homework": hw_cd,
            "officehours": office_cd,
            "rob": rob_cd if rob_cd is not None else 6 * 60 * 60  # default 6 hours
        }

    inventories.clear()
    c.execute("SELECT guild_id, user_id, item, quantity FROM inventories")
    for guild_id, user_id, item, quantity in c.fetchall():
        inventories.setdefault(guild_id, {}).setdefault(user_id, {})[item] = quantity

    # --- Load Active Buffs ---
    active_buffs.clear()
    c.execute("SELECT guild_id, user_id, buff_name FROM active_buffs")
    for guild_id, user_id, buff_name in c.fetchall():
        active_buffs.setdefault(guild_id, {}).setdefault(user_id, set()).add(buff_name)


    conn.commit()
    conn.close()


def save_guild_cooldowns():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Insert or update cooldowns
    for guild_id, cds in guild_cooldowns.items():
        # Ensure rob exists in dict
        rob_cd = cds.get("rob", 21600)
        c.execute("""
        INSERT INTO guild_settings (guild_id, homework_cooldown, officehours_cooldown, rob_cooldown)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET
            homework_cooldown=excluded.homework_cooldown,
            officehours_cooldown=excluded.officehours_cooldown,
            rob_cooldown=excluded.rob_cooldown
        """, (guild_id, cds["homework"], cds["officehours"], rob_cd))

    conn.commit()
    conn.close()


def save_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("DELETE FROM currencies")
    c.execute("DELETE FROM balances")
    c.execute("DELETE FROM cooldowns")
    c.execute("DELETE FROM jackpots")

    # --- Save currencies & balances ---
    for guild_id, guild_currencies in currencies_data.items():
        for currency_name, data in guild_currencies.items():
            emoji = data.get("emoji", "")
            balances = data.get("balances", {})

            c.execute("INSERT INTO currencies VALUES (?, ?, ?)", (guild_id, currency_name, emoji))

            for user_id, amount in balances.items():
                c.execute(
                    "INSERT INTO balances VALUES (?, ?, ?, ?)",
                    (guild_id, user_id, currency_name, amount)
                )

    # --- Save cooldowns ---
    for guild_id, users in hw_cooldowns.items():
        for user_id, last_used in users.items():
            c.execute(
                "INSERT OR REPLACE INTO cooldowns VALUES (?, ?, ?, ?)",
                (guild_id, user_id, "homework", last_used)
            )

    for guild_id, users in office_cooldowns.items():
        for user_id, last_used in users.items():
            c.execute(
                "INSERT OR REPLACE INTO cooldowns VALUES (?, ?, ?, ?)",
                (guild_id, user_id, "officehours", last_used)
            )

    # ✅ Save rob cooldowns
    for guild_id, users in rob_cooldowns.items():
        for user_id, last_used in users.items():
            c.execute(
                "INSERT OR REPLACE INTO cooldowns VALUES (?, ?, ?, ?)",
                (guild_id, user_id, "rob", last_used)
            )

    # --- Save jackpots ---
    for guild_id, guild_pots in jackpots.items():
        for currency, amount in guild_pots.items():
            c.execute(
                "REPLACE INTO jackpots (guild_id, currency, amount) VALUES (?, ?, ?)",
                (guild_id, currency, amount)
            )
    
    c.execute("DELETE FROM inventories")
    for guild_id, users in inventories.items():
        for user_id, items in users.items():
            for item_name, qty in items.items():
                c.execute(
                    "INSERT INTO inventories VALUES (?, ?, ?, ?)",
                    (guild_id, user_id, item_name, qty)
                )

    # --- Save Active Buffs ---
    c.execute("DELETE FROM active_buffs")
    for guild_id, users in active_buffs.items():
        for user_id, buffs in users.items():
            for buff_name in buffs:
                c.execute(
                    "INSERT INTO active_buffs VALUES (?, ?, ?)",
                    (guild_id, user_id, buff_name)
            )


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


@bot.tree.command(name="set_cooldown", description="Set cooldown for homework, office hours, or rob for this server.")
@app_commands.describe(command="Which command to set", seconds="Cooldown in seconds")
@app_commands.choices(command=[
    app_commands.Choice(name="homework", value="homework"),
    app_commands.Choice(name="officehours", value="officehours"),
    app_commands.Choice(name="rob", value="rob")
])
@app_commands.checks.has_permissions(manage_guild=True)
async def set_cooldown(interaction: discord.Interaction, command: app_commands.Choice[str], seconds: int):
    guild_id = interaction.guild.id
    if seconds < 0:
        await interaction.response.send_message("❌ Cooldown must be 0 or greater.", ephemeral=True)
        return

    # Initialize defaults if missing
    guild_cooldowns.setdefault(guild_id, {
        "homework": COOLDOWN_SECONDS,
        "officehours": COOLDOWN_SECONDS,
        "rob": COOLDOWN_SECONDS  # Default 6 hours
    })

    # Set new cooldown
    guild_cooldowns[guild_id][command.value] = seconds
    save_guild_cooldowns()

    await interaction.response.send_message(
        f"✅ `{command.value}` cooldown set to **{seconds} seconds** for this server."
    )


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
        embed.set_footer(
    text=f"Requested by {interaction.user}\nCurrencies in this bot have no value and are not sponsored by the ECE department.\n",
    icon_url=interaction.user.avatar.url if interaction.user.avatar else None
    )
        embed.timestamp = datetime.now()
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
    embed.set_footer(
    text=f"Requested by {interaction.user}\nCurrencies in this bot have no value and are not sponsored by the ECE department.\n",
    icon_url=interaction.user.avatar.url if interaction.user.avatar else None
    )
    embed.timestamp = datetime.now()
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
        embed.set_footer(
        text=f"Requested by {interaction.user}\nCurrencies in this bot have no value and are not sponsored by the ECE department.\n",
        icon_url=interaction.user.avatar.url if interaction.user.avatar else None
        )
        embed.timestamp = datetime.now()
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
        embed.set_footer(
        text=f"Requested by {interaction.user}\nCurrencies in this bot have no value and are not sponsored by the ECE department.\n",
        icon_url=interaction.user.avatar.url if interaction.user.avatar else None
        )
        embed.timestamp = datetime.now()
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
    embed.set_footer(
    text=f"Requested by {interaction.user}\nCurrencies in this bot have no value and are not sponsored by the ECE department.\n",
    icon_url=interaction.user.avatar.url if interaction.user.avatar else None
    )
    embed.timestamp = datetime.now()
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
    text=f"Requested by {interaction.user}\nCurrencies in this bot have no value and are not sponsored by the ECE department.\n",
    icon_url=interaction.user.avatar.url if interaction.user.avatar else None
    )
        embed.timestamp = datetime.now()
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
    text=f"Requested by {interaction.user}\nCurrencies in this bot have no value and are not sponsored by the ECE department.\n",
    icon_url=interaction.user.avatar.url if interaction.user.avatar else None
    )
    embed.timestamp = datetime.now()

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
        embed.set_footer(
    text=f"Requested by {interaction.user}\nCurrencies in this bot have no value and are not sponsored by the ECE department.\n",
    icon_url=interaction.user.avatar.url if interaction.user.avatar else None
    )
        embed.timestamp = datetime.now()
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
        embed.set_footer(
    text=f"Requested by {interaction.user}\nCurrencies in this bot have no value and are not sponsored by the ECE department.\n",
    icon_url=interaction.user.avatar.url if interaction.user.avatar else None
    )
        embed.timestamp = datetime.now()
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # Pick a random currency and amount
    currency_name = random.choice(list(guild_currencies.keys()))
    currency_data = guild_currencies[currency_name]
    amount = random.randint(1, 10)

    # Update balance
    user_balances = guild_currencies[currency_name]["balances"]
    
    user_balances[user_id] = user_balances.get(user_id, 0) + amount

   

    # Update cooldown
    hw_cooldowns.setdefault(guild_id, {})[user_id] = now

    save_db()

    emoji = currency_data.get("emoji", "💰")

    embed = discord.Embed(
        title="Homework Completed!",
        description=f"🎓 {interaction.user.mention} did homework and earned {emoji} **{amount} {currency_name}**!",
        color=discord.Color.green()
    )
    #embed.set_footer(text=f"Requested by {interaction.user}", icon_url=interaction.user.avatar.url if interaction.user.avatar else None)
    embed.set_footer(
    text=f"Requested by {interaction.user}\nCurrencies in this bot have no value and are not sponsored by the ECE department.\n",
    icon_url=interaction.user.avatar.url if interaction.user.avatar else None
    )

    embed.timestamp = datetime.now()
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
        embed.set_footer(
    text=f"Requested by {interaction.user}\nCurrencies in this bot have no value and are not sponsored by the ECE department.\n",
    icon_url=interaction.user.avatar.url if interaction.user.avatar else None
    )
        embed.timestamp = datetime.now()
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
    embed.set_footer(
    text=f"Requested by {interaction.user}\nCurrencies in this bot have no value and are not sponsored by the ECE department.\n",
    icon_url=interaction.user.avatar.url if interaction.user.avatar else None
    )
    embed.timestamp = datetime.now()
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
        embed.set_footer(
    text=f"Requested by {interaction.user}\nCurrencies in this bot have no value and are not sponsored by the ECE department.\n",
    icon_url=interaction.user.avatar.url if interaction.user.avatar else None
    )
        embed.timestamp = datetime.now()
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
    text=f"Requested by {interaction.user}\nCurrencies in this bot have no value and are not sponsored by the ECE department.\n",
    icon_url=interaction.user.avatar.url if interaction.user.avatar else None
    )
            embed.timestamp = datetime.now()

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
    text=f"Requested by {interaction.user}\nCurrencies in this bot have no value and are not sponsored by the ECE department.\n",
    icon_url=interaction.user.avatar.url if interaction.user.avatar else None
    )
            embed.timestamp = datetime.now()

            await interaction.response.edit_message(embed=embed, view=None)

    # Send confirmation embed
    embed = discord.Embed(
        title="⚠️ Confirm Currency Removal",
        description=f"Are you sure you want to remove the currency **{name}**?\nThis action **cannot be undone**.",
        color=discord.Color.yellow()
    )
    embed.set_footer(
    text=f"Requested by {interaction.user}\nCurrencies in this bot have no value and are not sponsored by the ECE department.\n",
    icon_url=interaction.user.avatar.url if interaction.user.avatar else None
    )
    embed.timestamp = datetime.now()

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
    embed.set_footer(
    text=f"Requested by {interaction.user}\nCurrencies in this bot have no value and are not sponsored by the ECE department.\n",
    icon_url=interaction.user.avatar.url if interaction.user.avatar else None
    )
    embed.timestamp = datetime.now()
    await interaction.response.send_message(embed=embed)

from discord import ui, Interaction, Embed, SelectOption
from datetime import datetime

# --- Currency leaderboard with dropdown ---
from discord import ui, Interaction, Embed, SelectOption
from datetime import datetime

# --- /leaderboard command with dropdown ---
@bot.tree.command(name="leaderboard", description="Show the top users for any currency.")
async def leaderboard(interaction: Interaction):
    guild_id = interaction.guild.id
    guild_currencies = currencies_data.get(guild_id, {})

    if not guild_currencies:
        await interaction.response.send_message("❌ No currencies found for this server.", ephemeral=True)
        return

    # Helper function to build leaderboard embed
    def make_leaderboard_embed(currency_name: str):
        balances = guild_currencies[currency_name]["balances"]
        # Filter out users with 0 balance
        nonzero_balances = {uid: amt for uid, amt in balances.items() if amt > 0}

        if not nonzero_balances:
            desc = f"❌ No one has any `{currency_name}` yet."
        else:
            top_users = sorted(nonzero_balances.items(), key=lambda x: x[1], reverse=True)[:10]
            desc = ""
            for i, (user_id, amount) in enumerate(top_users, start=1):
                user = interaction.guild.get_member(user_id)
                username = user.display_name if user else f"<Unknown User {user_id}>"
                desc += f"**{i}. {username}** — {amount}\n"

        emoji = guild_currencies[currency_name].get("emoji", "")
        embed = Embed(
            title=f"🏆 Leaderboard: {emoji} {currency_name}",
            description=desc,
            color=discord.Color.gold()
        )
        embed.set_footer(
    text=f"Requested by {interaction.user}\nCurrencies in this bot have no value and are not sponsored by the ECE department.\n",
    icon_url=interaction.user.avatar.url if interaction.user.avatar else None
    )
        embed.timestamp = datetime.now()
        return embed

    # Dropdown options for currencies
    options = [
        SelectOption(label=name, description=f"View {name} leaderboard",
                     emoji=guild_currencies[name].get("emoji", None))
        for name in guild_currencies.keys()
    ]

    class LeaderboardSelect(ui.Select):
        def __init__(self):
            super().__init__(
                placeholder="Select a currency...",
                options=options,
                min_values=1,
                max_values=1
            )

        async def callback(self, select_interaction: Interaction):
            currency_selected = self.values[0]
            embed = make_leaderboard_embed(currency_selected)
            await select_interaction.response.edit_message(embed=embed, view=self.view)

    class LeaderboardView(ui.View):
        def __init__(self):
            super().__init__(timeout=None)
            self.add_item(LeaderboardSelect())

    # Default to first currency
    first_currency = next(iter(guild_currencies.keys()))
    embed = make_leaderboard_embed(first_currency)
    view = LeaderboardView()
    await interaction.response.send_message(embed=embed, view=view)



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
    text=f"Requested by {interaction.user}\nCurrencies in this bot have no value and are not sponsored by the ECE department.\n",
    icon_url=interaction.user.avatar.url if interaction.user.avatar else None
    )
    embed.timestamp = datetime.now()

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
    embed.timestamp = datetime.now()
    await interaction.response.send_message(embed=embed)


import random
from discord import app_commands, Interaction, Embed
from datetime import datetime


import discord
from discord.ext import commands

class TradeView(discord.ui.View):
    def __init__(self, author, target, give_currency, give_amount, receive_currency, receive_amount, guild_id):
        super().__init__(timeout=60)
        self.author = author
        self.target = target
        self.give_currency = give_currency
        self.give_amount = give_amount
        self.receive_currency = receive_currency
        self.receive_amount = receive_amount
        self.guild_id = guild_id
        self.author_confirmed = False
        self.target_confirmed = False
        self.trade_completed = False

    async def complete_trade(self, interaction):
        """Perform the trade after both confirm."""
        guild_currencies = currencies_data[self.guild_id]

        # Deduct & add currency for both users
        guild_currencies[self.give_currency]["balances"][self.author.id] -= self.give_amount
        guild_currencies[self.give_currency]["balances"][self.target.id] += self.give_amount
        guild_currencies[self.receive_currency]["balances"][self.target.id] -= self.receive_amount
        guild_currencies[self.receive_currency]["balances"][self.author.id] += self.receive_amount


        save_db()
        self.trade_completed = True

        embed = discord.Embed(
            title="✅ Trade Completed!",
            description=f"{self.author.mention} and {self.target.mention} have successfully traded!",
            color=discord.Color.green()
        )
        embed.add_field(name=f"{self.author.display_name} gave:",
                        value=f"{self.give_amount} {self.give_currency}", inline=True)
        embed.add_field(name=f"{self.target.display_name} gave:",
                        value=f"{self.receive_amount} {self.receive_currency}", inline=True)
        embed.timestamp = datetime.now()

        await interaction.message.edit(embed=embed, view=None)

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user not in [self.author, self.target]:
            await interaction.response.send_message("❌ You’re not part of this trade!", ephemeral=True)
            return

        if interaction.user == self.author:
            self.author_confirmed = True
        elif interaction.user == self.target:
            self.target_confirmed = True

        if self.author_confirmed and self.target_confirmed:
            await self.complete_trade(interaction)
        else:
            await interaction.response.send_message("✅ You’ve confirmed your side of the trade. Waiting for the other user...", ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user not in [self.author, self.target]:
            await interaction.response.send_message("❌ You’re not part of this trade!", ephemeral=True)
            return

        embed = discord.Embed(
            title="❌ Trade Cancelled",
            description=f"The trade between {self.author.mention} and {self.target.mention} was cancelled.",
            color=discord.Color.red()
        )
        embed.timestamp = datetime.now()

        await interaction.message.edit(embed=embed, view=None)
        self.stop()


@bot.tree.command(name="trade", description="Propose a trade with another user.")
@app_commands.describe(
    user="User to trade with",
    give_currency="Currency you want to give",
    give_amount="Amount of your currency to trade",
    receive_currency="Currency you want to receive",
    receive_amount="Amount of currency you expect in return"
)
@app_commands.autocomplete(give_currency=currency_autocomplete)
@app_commands.autocomplete(receive_currency=currency_autocomplete)
async def trade(interaction: discord.Interaction, user: discord.User,
                give_currency: str, give_amount: int,
                receive_currency: str, receive_amount: int):

    guild_id = interaction.guild.id
    author_id = interaction.user.id
    target_id = user.id

    if author_id == target_id:
        await interaction.response.send_message("❌ You can’t trade with yourself!", ephemeral=True)
        return

    if guild_id not in currencies_data:
        await interaction.response.send_message("❌ No currencies found for this server.", ephemeral=True)
        return

    guild_currencies = currencies_data[guild_id]
    if give_currency not in guild_currencies or receive_currency not in guild_currencies:
        await interaction.response.send_message("❌ One or both specified currencies don’t exist.", ephemeral=True)
        return

    give_balances = guild_currencies[give_currency]["balances"]
    receive_balances = guild_currencies[receive_currency]["balances"]

    give_balances.setdefault(author_id, 0)
    give_balances.setdefault(target_id, 0)
    receive_balances.setdefault(author_id, 0)
    receive_balances.setdefault(target_id, 0)

    if give_balances[author_id] < give_amount:
        await interaction.response.send_message(f"❌ You don’t have enough {give_currency} to trade.", ephemeral=True)
        return

    if receive_balances[target_id] < receive_amount:
        await interaction.response.send_message(f"❌ {user.display_name} doesn’t have enough {receive_currency} to trade.", ephemeral=True)
        return

    embed = discord.Embed(
        title="🤝 Trade Proposal",
        description=f"{interaction.user.mention} wants to trade with {user.mention}.",
        color=discord.Color.blurple()
    )
    embed.add_field(name=f"{interaction.user.display_name} offers:",
                    value=f"{give_amount} {give_currency}", inline=True)
    embed.add_field(name=f"{user.display_name} will give:",
                    value=f"{receive_amount} {receive_currency}", inline=True)
    embed.set_footer(text="Both users must confirm within 60 seconds.")
    embed.timestamp = datetime.now()

    view = TradeView(interaction.user, user, give_currency, give_amount,
                     receive_currency, receive_amount, guild_id)

    await interaction.response.send_message(
    content=f"{user.mention}, you’ve received a trade request from {interaction.user.mention}!",
    embed=embed,
    view=view
)





# --- /gamble command ---
@bot.tree.command(name="gamble", description="Gamble some of your currency!")
@app_commands.describe(currency="Currency to gamble", amount="Amount to bet")
@app_commands.autocomplete(currency=currency_autocomplete)
async def gamble(interaction: discord.Interaction, currency: str, amount: int):
    guild_id = interaction.guild.id
    user_id = interaction.user.id

    # ✅ Ensure valid currency
    guild_currencies = currencies_data.get(guild_id, {})
    if currency not in guild_currencies:
        await interaction.response.send_message(f"❌ Currency `{currency}` not found.", ephemeral=True)
        return

    balances = guild_currencies[currency]["balances"]
    user_balance = balances.get(user_id, 0)

    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be greater than 0.", ephemeral=True)
        return

    if user_balance < amount:
        await interaction.response.send_message("❌ You don’t have enough to gamble.", ephemeral=True)
        return

    emoji = guild_currencies[currency].get("emoji", "💰")
    win = random.random() < 0.5  # 50% chance
    result_text = ""

    if win:
    # 🏆 Add winnings
        winnings = amount
        balances[user_id] = user_balance + winnings
        result_text = f"🎉 {interaction.user.mention} **won** {winnings} {currency} {emoji}!"


        # 🎯 0.1% * bet jackpot chance
        jackpot_chance = min(0.002 * amount, 0.5)  # e.g., 0.002 * 500 = 1.0 (100%)
        if random.random() < jackpot_chance:
            guild_pot = jackpots.get(guild_id, {})
            jackpot_text = ""

            for cur_name, pot_amount in guild_pot.items():
                if pot_amount > 0:
                    guild_currencies[cur_name]["balances"][user_id] = (
                        guild_currencies[cur_name]["balances"].get(user_id, 0) + pot_amount
                    )
                    jackpot_text += f"\n🎉 You also won {pot_amount} {guild_currencies[cur_name]['emoji']} **{cur_name}** from the jackpot!"

            # Reset the pot
            jackpots[guild_id] = {cur: 0 for cur in guild_pot}

            if jackpot_text:
                result_text += jackpot_text

    else:
        # 💸 Lose coins
        balances[user_id] = user_balance - amount
        result_text = f"😢 You **lost** {amount} {currency} {emoji} . \nAdded to the server’s pot. Use /jackpot to view this server's current pot."

        # Add lost coins to that currency’s jackpot pool
        jackpots.setdefault(guild_id, {}).setdefault(currency, 0)
        jackpots[guild_id][currency] += amount

    save_db()

    embed = discord.Embed(
        title=f"{emoji} Gamble Result",
        description=result_text,
        color=discord.Color.green() if win else discord.Color.red()
    )
    embed.set_footer(
    text=f"Requested by {interaction.user}\nCurrencies in this bot have no value and are not sponsored by the ECE department.\n",
    icon_url=interaction.user.avatar.url if interaction.user.avatar else None
    )
    embed.timestamp = datetime.now()
    await interaction.response.send_message(embed=embed)

# --- /jackpot command ---
@bot.tree.command(name="jackpot", description="Show the current jackpot pot for this server.")
async def jackpot(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    guild_currencies = currencies_data.get(guild_id, {})
    guild_pot = jackpots.get(guild_id, {})

    if not guild_pot or all(v == 0 for v in guild_pot.values()):
        embed = discord.Embed(
            title="💰 Jackpot Pot",
            description="The jackpot is currently **empty**! No coins have been lost yet.",
            color=discord.Color.orange()
        )
    else:
        description_lines = []
        for cur_name, amount in guild_pot.items():
            if amount > 0:
                emoji = guild_currencies.get(cur_name, {}).get("emoji", "💰")
                description_lines.append(f"{emoji} **{cur_name}**: {amount}")

        description = "\n".join(description_lines) if description_lines else "No jackpot funds currently available."

        embed = discord.Embed(
            title="💎 Server Jackpot Pot",
            description=description,
            color=discord.Color.gold()
        )

    embed.set_footer(
    text=f"Requested by {interaction.user}\nCurrencies in this bot have no value and are not sponsored by the ECE department.\n",
    icon_url=interaction.user.avatar.url if interaction.user.avatar else None
    )
    embed.timestamp = datetime.now()

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="rob", description="Try to rob another user for some currency!")
@app_commands.describe(user="The user you want to rob")
async def rob(interaction: discord.Interaction, user: discord.User):
    guild_id = interaction.guild.id
    author_id = interaction.user.id
    target_id = user.id

    if user.bot:
        await interaction.response.send_message("🤖 You can’t rob bots!", ephemeral=True)
        return
    if author_id == target_id:
        await interaction.response.send_message("🙄 You can’t rob yourself.", ephemeral=True)
        return

    guild_currencies = currencies_data.get(guild_id, {})
    if not guild_currencies:
        await interaction.response.send_message("❌ This server has no currencies set up.", ephemeral=True)
        return

    # --- Rob cooldown ---
    now = time.time()
    # Default cooldown: 6 hours if not set
    COOLDOWN = guild_cooldowns.get(guild_id, {}).get("rob", 6 * 60 * 60)
    last_time = rob_cooldowns.get(guild_id, {}).get(author_id, 0)

    if now - last_time < COOLDOWN:
        remaining = int(COOLDOWN - (now - last_time))
        # Format as H:M:S
        hours, remainder = divmod(remaining, 3600)
        minutes, seconds = divmod(remainder, 60)
        remaining_str = f"{hours}h {minutes}m {seconds}s" if hours else f"{minutes}m {seconds}s"
        await interaction.response.send_message(
            f"⏱ You need to wait **{remaining_str}** before robbing again!",
            ephemeral=True
        )
        return

    target_buffs = active_buffs.get(guild_id, {}).get(target_id, set())
    if "rf_shield" in target_buffs:
        # Remove RF Shield after it blocks a robbery
        target_buffs.remove("rf_shield")
        save_db()
        await interaction.response.send_message(
            f"🛡️ {user.mention}'s RF Shield protected them from your robbery attempt!"
        )
        return
    
    # --- Pick random currency victim actually has ---
    nonzero_currencies = [
        (name, data)
        for name, data in guild_currencies.items()
        if data["balances"].get(target_id, 0) > 0
    ]

    if not nonzero_currencies:
        await interaction.response.send_message(
            f"💤 {user.mention} has no currencies to steal!",
            ephemeral=True
        )
        return

    currency_name, currency_data = random.choice(nonzero_currencies)
    balances = currency_data["balances"]
    emoji = currency_data.get("emoji", "💰")

    robber_balance = balances.get(author_id, 0)
    victim_balance = balances.get(target_id, 0)

    # --- Attempt robbery ---
    amount = random.randint(1, min(50, victim_balance))  # cap at 50 or victim’s balance
    success = random.random() < 0.5  # 50% chance to succeed

    if success:
        balances[target_id] = victim_balance - amount
        balances[author_id] = robber_balance + amount
        result_text = f"😈 {interaction.user.mention} **successfully robbed** {user.mention} and stole **{emoji} {amount} {currency_name}!**"
        color = discord.Color.green()
    else:
        max_penalty = min(50, robber_balance)
        penalty = random.randint(1, max_penalty) if max_penalty > 0 else 0
        balances[author_id] = max(0, robber_balance - penalty)
        jackpots.setdefault(guild_id, {}).setdefault(currency_name, 0)
        jackpots[guild_id][currency_name] += penalty
        result_text = f"🚓 {interaction.user.mention} got **caught** trying to rob {user.mention} and paid **{emoji} {penalty} {currency_name}** into the jackpot!"
        color = discord.Color.red()

    # --- Save cooldown + DB ---
    rob_cooldowns.setdefault(guild_id, {})[author_id] = now
    save_db()

    # --- Embed response ---
    embed = discord.Embed(
        title="💸 Robbery Attempt",
        description=result_text,
        color=color
    )
    embed.set_footer(
    text=f"Requested by {interaction.user}\nCurrencies in this bot have no value and are not sponsored by the ECE department.\n",
    icon_url=interaction.user.avatar.url if interaction.user.avatar else None
    )
    embed.timestamp = datetime.now()

    await interaction.response.send_message(embed=embed)


shop_items = {
    "energy_drink": {
        "price": 25,
        "description": "Removes your robbery cooldown.",
        "type": "consumable",
        "emoji": "☕" 
    },
    "rf_shield": {
        "price": 25,
        "description": "Protects you from one robbery attempt.",
        "type": "consumable",
        "emoji": "🛡️"
    }
}


@bot.tree.command(name="shop", description="View items available for purchase")
async def shop(interaction: discord.Interaction):
    embed = discord.Embed(title="🛍️ ECE Shop", color=discord.Color.gold())
    for name, item in shop_items.items():
        embed.add_field(
            name=f"{item['emoji']} {name.replace('_', ' ').title()} — {item['price']} currency",
            value=item["description"],
            inline=False
        )
    await interaction.response.send_message(embed=embed)

async def item_autocomplete(interaction: discord.Interaction, current: str):
    """Autocomplete shop items based on what the user types."""
    guild_id = interaction.guild.id
    current_lower = current.lower()

    # Return all items that start with current input
    return [
        app_commands.Choice(name=item.replace("_", " ").title(), value=item)
        for item in shop_items.keys()
        if item.startswith(current_lower)
    ][:25] 

@bot.tree.command(name="buy", description="Buy an item from the shop")
@app_commands.describe(item="The item to buy", currency="Currency to spend")
@app_commands.autocomplete(item=item_autocomplete, currency=currency_autocomplete)
async def buy(interaction: discord.Interaction, item: str, currency: str):
    guild_id = interaction.guild.id
    user_id = interaction.user.id
    item = item.lower().replace(" ", "_")

    # Validate item
    if item not in shop_items:
        await interaction.response.send_message("❌ That item does not exist.", ephemeral=True)
        return

    # Validate currency
    guild_currencies = currencies_data.get(guild_id, {})
    if currency not in guild_currencies:
        await interaction.response.send_message(f"❌ Currency `{currency}` not found.", ephemeral=True)
        return

    # Check user balance
    balances = guild_currencies[currency]["balances"]
    user_balance = balances.get(user_id, 0)
    price = shop_items[item]["price"]

    if user_balance < price:
        await interaction.response.send_message("❌ You don't have enough to buy this item.", ephemeral=True)
        return

    # Deduct currency
    balances[user_id] = user_balance - price

    # Add item to inventory
    inventories.setdefault(guild_id, {}).setdefault(user_id, {})
    inventories[guild_id][user_id][item] = inventories[guild_id][user_id].get(item, 0) + 1

    save_db()
    emoji = shop_items[item].get("emoji", "")
    currency_emoji = guild_currencies[currency].get("emoji", "💰")
    await interaction.response.send_message(
        f"✅ You bought {emoji} **{item.replace('_', ' ').title()}** for {price} {currency} {currency_emoji}!"
    )

@bot.tree.command(name="use", description="Use an item from your inventory")
@app_commands.describe(item="The item to use")
@app_commands.autocomplete(item=item_autocomplete)
async def use(interaction: discord.Interaction, item: str):
    guild_id = interaction.guild.id
    user_id = interaction.user.id
    item = item.lower().replace(" ", "_")

    inv = inventories.get(guild_id, {}).get(user_id, {})
    if inv.get(item, 0) == 0:
        await interaction.response.send_message("❌ You don't have that item!", ephemeral=True)
        return

    # Check if buff already exists
    user_buffs = active_buffs.setdefault(guild_id, {}).setdefault(user_id, set())

    if item == "energy_drink":
        # Energy drink can always be used (removes rob cooldown)
        rob_cooldowns.get(guild_id, {}).pop(user_id, None)
        await interaction.response.send_message("⚡ You feel energized! Rob cooldown cleared.")

    elif item == "rf_shield":
        if "rf_shield" in user_buffs:
            await interaction.response.send_message("🛡️ You already have an active RF Shield!", ephemeral=True)
            return
        # Activate RF Shield
        user_buffs.add("rf_shield")
        await interaction.response.send_message("🛡️ RF Shield active until you are robbed!", ephemeral=True)

    else:
        await interaction.response.send_message("❌ You can't use that item!", ephemeral=True)
        return

    # Consume item
    inv[item] -= 1
    if inv[item] <= 0:
        del inv[item]

    save_db()


@bot.tree.command(name="buffs", description="View your active buffs")
async def buffs(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    user_id = interaction.user.id

    user_buffs = active_buffs.get(guild_id, {}).get(user_id, set())

    if not user_buffs:
        await interaction.response.send_message("💤 You have no active buffs.", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"{interaction.user.display_name}'s Active Buffs",
        color=discord.Color.green()
    )

    for buff in user_buffs:
        key = buff.lower().replace(" ", "_")
        emoji = shop_items.get(key, {}).get("emoji", "")
        # Convert underscore name to proper spaced name (RF Shield, Energy Drink)
        display_name = buff.replace("_", " ").title()
        # Special case for RF to keep caps
        if display_name.lower() == "rf shield":
            display_name = "RF Shield"
        description = shop_items.get(key, {}).get("description", "")
        embed.add_field(name=f"{emoji} {display_name}", value=description, inline=False)

    await interaction.response.send_message(embed=embed)



@bot.tree.command(name="inventory", description="View your owned items")
async def inventory(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    user_id = interaction.user.id

    user_inv = inventories.get(guild_id, {}).get(user_id, {})

    if not user_inv:
        await interaction.response.send_message("🎒 Your inventory is empty!", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"{interaction.user.display_name}'s Inventory",
        color=discord.Color.blurple()
    )

    for item_name, qty in user_inv.items():
        emoji = shop_items.get(item_name, {}).get("emoji", "")
        embed.add_field(
            name=f"{emoji} {item_name.replace('_', ' ').title()}",
            value=f"x{qty}",
            inline=True
        )

    # Show active buffs separately
    user_buffs = active_buffs.get(guild_id, {}).get(user_id, set())
    if user_buffs:
        embed.add_field(
            name="Active Buffs",
            value=", ".join(shop_items.get(b, {}).get("emoji", "") + " " + b.replace("_", " ").title() for b in user_buffs),
            inline=False
        )

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
