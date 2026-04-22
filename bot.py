"""
╔══════════════════════════════════════════════════════════════╗
║          FC26 ULTIMATE TRADING BOT — by Your Server         ║
║   Alertes instantanées · PC / Xbox / PlayStation · Futbin   ║
╚══════════════════════════════════════════════════════════════╝
"""

import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiohttp
from bs4 import BeautifulSoup
import asyncio
import json
import os
import re
import logging
from datetime import datetime
from typing import Optional

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
TOKEN           = os.getenv("DISCORD_TOKEN", "VOTRE_TOKEN_ICI")
MIN_PROFIT      = 500        # coins
EA_TAX          = 0.05       # 5%
SCAN_DELAY      = 30         # secondes entre chaque scan (scan permanent)
MAX_ALERTS      = 5          # max alertes envoyées par scan pour éviter le spam

PLATFORMS = {
    "pc":          {"label": "PC",          "emoji": "🖥️",  "color": 0x5865F2},
    "xbox":        {"label": "Xbox",        "emoji": "🟢",  "color": 0x107C10},
    "playstation": {"label": "PlayStation", "emoji": "🔵",  "color": 0x003791},
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Referer": "https://www.futbin.com/",
}

POSITIONS = [
    "ST","CF","CAM","CM","CDM","LW","RW",
    "LB","RB","CB","GK","LM","RM","LWB","RWB"
]

# ─── Storage ──────────────────────────────────────────────────────────────────
PREFS_FILE    = "user_prefs.json"    # préférences plateforme par user
CHANNELS_FILE = "channels.json"     # salons dédiés par guilde
SENT_FILE     = "sent_alerts.json"  # IDs déjà envoyés pour éviter doublons
STATS_FILE    = "stats.json"        # stats globales

def _load(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default

def _save(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

user_prefs:    dict = _load(PREFS_FILE, {})
channels:      dict = _load(CHANNELS_FILE, {})
sent_alerts:   dict = _load(SENT_FILE, {})
stats:         dict = _load(STATS_FILE, {"total_sent": 0, "total_profit_shown": 0, "scans": 0})

# ─── Bot ──────────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot  = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ─── Helpers ──────────────────────────────────────────────────────────────────
def fmt(n: int) -> str:
    if n >= 1_000_000: return f"{n/1_000_000:.2f}M"
    if n >= 1_000:     return f"{n/1_000:.1f}K"
    return str(n)

def parse_price(text) -> int:
    if not text: return 0
    t = str(text).strip().upper().replace(",", "").replace(" ", "").replace("\xa0","")
    try:
        if "M" in t:  return int(float(t.replace("M","")) * 1_000_000)
        if "K" in t:  return int(float(t.replace("K","")) * 1_000)
        return int(re.sub(r"[^\d]", "", t) or 0)
    except: return 0

def calc(snipe: int, market: int) -> dict:
    sell_price  = int(market * (1 - EA_TAX))
    profit      = sell_price - snipe
    roi         = round(profit / snipe * 100, 1) if snipe > 0 else 0
    discount    = round((market - snipe) / market * 100, 1) if market > 0 else 0
    return {"sell": sell_price, "profit": profit, "roi": roi, "discount": discount}

def roi_bar(roi: float) -> str:
    filled = min(int(roi / 4), 12)
    return "█" * filled + "░" * (12 - filled)

def get_user_platform(user_id: int) -> str:
    return user_prefs.get(str(user_id), {}).get("platform", "pc")

# ─── Scraping Engine ──────────────────────────────────────────────────────────
async def fetch_html(url: str, params: dict = None) -> Optional[str]:
    try:
        async with aiohttp.ClientSession(headers=HEADERS) as s:
            async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status == 200:
                    return await r.text()
                log.warning(f"HTTP {r.status} — {url}")
    except Exception as e:
        log.error(f"fetch error: {e}")
    return None

async def fetch_json(url: str, params: dict = None) -> Optional[dict | list]:
    try:
        h = {**HEADERS, "Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
        async with aiohttp.ClientSession(headers=h) as s:
            async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status == 200:
                    return await r.json(content_type=None)
    except Exception as e:
        log.error(f"fetch_json error: {e}")
    return None


PLATFORM_PRICE_KEY = {
    "pc":          ("LCPrice",  "LCPrice2"),
    "xbox":        ("XBPrice",  "XBPrice2"),
    "playstation": ("PSPrice",  "PSPrice2"),
}

async def get_players(filters: dict = None, platform: str = "pc") -> list[dict]:
    """Récupère les joueurs depuis Futbin avec calcul profit."""
    pk_market, pk_buy = PLATFORM_PRICE_KEY.get(platform, ("LCPrice","LCPrice2"))

    params = {
        "page":     1,
        "sort":     "pc_price",
        "order":    "asc",
        "per_page": 60,
        "version":  "all",
    }
    if filters:
        for k in ("position","nation","league","club","min_rating","max_rating","min_price","max_price"):
            if filters.get(k):
                params[k] = filters[k]

    players = []

    # — Tentative API JSON Futbin —
    data = await fetch_json("https://www.futbin.com/25/players", params)
    if isinstance(data, list) and data:
        for p in data:
            market = parse_price(p.get(pk_market) or p.get("LCPrice") or 0)
            snipe  = parse_price(p.get(pk_buy)    or p.get("LCPrice2") or market)
            if market < 300 or snipe <= 0: continue
            c = calc(snipe, market)
            if c["profit"] < MIN_PROFIT: continue
            pid = str(p.get("id",""))
            players.append({
                "id":       pid,
                "name":     p.get("Player_Name") or p.get("name","?"),
                "rating":   int(p.get("Rating") or p.get("rating") or 0),
                "position": (p.get("Position") or p.get("position","?")).upper(),
                "club":     p.get("Club")   or p.get("club","?"),
                "nation":   p.get("Nation") or p.get("nation","?"),
                "league":   p.get("League") or p.get("league","?"),
                "snipe":    snipe,
                "market":   market,
                "image":    p.get("Player_Image") or f"https://cdn.futbin.com/content/fifa25/img/players/{pid}.png",
                "url":      f"https://www.futbin.com/25/player/{pid}",
                **c,
            })
    else:
        # — Fallback scraping HTML —
        players = await _scrape_html(params, platform)

    players.sort(key=lambda x: x["profit"], reverse=True)
    return players


async def _scrape_html(params: dict, platform: str) -> list[dict]:
    html = await fetch_html("https://www.futbin.com/25/players", params)
    if not html: return []
    soup = BeautifulSoup(html, "html.parser")
    players = []
    for row in soup.select("table#repTb tbody tr, .player-table tbody tr")[:40]:
        try:
            cols = row.select("td")
            if len(cols) < 6: continue
            link_el  = row.select_one("a[href*='/player/']")
            name_el  = row.select_one(".player-name,.pname,a")
            img_el   = row.select_one("img[src*='players']")
            rat_el   = cols[1] if len(cols) > 1 else None
            pos_el   = cols[2] if len(cols) > 2 else None
            pr_col   = 5 if platform == "pc" else (6 if platform == "xbox" else 7)
            p1 = parse_price(cols[pr_col].get_text() if len(cols) > pr_col else "0")
            p2 = parse_price(cols[pr_col+1].get_text() if len(cols) > pr_col+1 else "0") or p1
            if p1 < 300: continue
            c = calc(p2, p1)
            if c["profit"] < MIN_PROFIT: continue
            href = link_el["href"] if link_el else ""
            pid  = re.search(r"/player/(\d+)", href)
            pid  = pid.group(1) if pid else ""
            players.append({
                "id":       pid,
                "name":     name_el.get_text(strip=True) if name_el else "?",
                "rating":   int(re.sub(r"\D","", rat_el.get_text()) or 0) if rat_el else 0,
                "position": pos_el.get_text(strip=True).upper() if pos_el else "?",
                "club":     "?", "nation": "?", "league": "?",
                "snipe":    p2,
                "market":   p1,
                "image":    img_el["src"] if img_el else f"https://cdn.futbin.com/content/fifa25/img/players/{pid}.png",
                "url":      "https://www.futbin.com" + href if href else "https://www.futbin.com/25/players",
                **c,
            })
        except Exception as e:
            log.debug(f"html row: {e}")
    return players

# ─── Embed Builder ────────────────────────────────────────────────────────────
def build_alert_embed(p: dict, platform: str) -> discord.Embed:
    plat       = PLATFORMS[platform]
    fire_level = "🚨🚨🚨" if p["discount"] >= 25 else ("🚨🚨" if p["discount"] >= 15 else "🚨")
    color      = 0xED4245 if p["discount"] >= 20 else (0xFF8C00 if p["discount"] >= 12 else 0x57F287)

    embed = discord.Embed(
        title=f"{plat['emoji']} @{plat['label']}  ·  {fire_level}",
        url=p["url"],
        color=color,
        timestamp=datetime.utcnow(),
    )

    # Ligne joueur
    embed.add_field(
        name=f"👤 {p['name']}  •  {p['rating']} {p['position']}",
        value=f"🌍 {p['nation']}  ·  🏆 {p['league']}  ·  ⚽ {p['club']}",
        inline=False,
    )

    # Prix
    embed.add_field(name="🎯 Snipe Price",   value=f"**{fmt(p['snipe'])}** 🪙",  inline=True)
    embed.add_field(name="💵 Sells Price",   value=f"**{fmt(p['sell'])}** 🪙",   inline=True)
    embed.add_field(name="💰 Profit",        value=f"**+{fmt(p['profit'])}** 🪙", inline=True)
    embed.add_field(name="🏷️ Réduction",     value=f"**-{p['discount']}%**",     inline=True)
    embed.add_field(name="📈 ROI",           value=f"**{p['roi']}%**",            inline=True)
    embed.add_field(name="💹 Rentabilité",   value=f"`{roi_bar(p['roi'])}`",      inline=True)

    # Lien
    embed.add_field(
        name="🔗 Lien vers la page du joueur",
        value=f"[Voir sur Futbin]({p['url']})",
        inline=False,
    )

    # Image joueur
    if p.get("image"):
        embed.set_thumbnail(url=p["image"])

    embed.set_footer(text=f"FC26 Trading Bot • Taxe EA 5% incluse • {plat['emoji']} {plat['label']}")
    return embed


def build_summary_embed(players: list[dict], platform: str, title: str, filters_str: str = "") -> discord.Embed:
    plat  = PLATFORMS[platform]
    color = plat["color"]
    embed = discord.Embed(
        title=f"{plat['emoji']} {title} — {len(players)} opportunité(s)",
        description=filters_str or "Meilleures affaires du moment",
        color=color,
        timestamp=datetime.utcnow(),
    )
    if players:
        lines = []
        for p in players[:10]:
            bar = "🟢" if p["roi"] > 20 else ("🟡" if p["roi"] > 10 else "🔴")
            lines.append(
                f"{bar} **{p['name']}** {p['rating']} {p['position']} "
                f"· 🎯 {fmt(p['snipe'])} → 💰 +{fmt(p['profit'])} ({p['roi']}%)"
            )
        embed.add_field(name="📋 Top opportunités", value="\n".join(lines), inline=False)
        best = players[0]
        embed.add_field(
            name="🏆 Meilleure affaire",
            value=f"**{best['name']}** — Snipe à {fmt(best['snipe'])} · Profit **+{fmt(best['profit'])}** coins ({best['roi']}% ROI)",
            inline=False,
        )
    else:
        embed.add_field(name="😔 Aucun résultat", value="Réessaie dans quelques instants.", inline=False)
    embed.set_footer(text=f"FC26 Trading Bot • Taxe EA 5% incluse • {plat['emoji']} {plat['label']}")
    return embed

# ─── Commandes Slash ──────────────────────────────────────────────────────────

# /plateforme — choisir sa plateforme
@tree.command(name="plateforme", description="🎮 Choisis ta plateforme (PC / Xbox / PlayStation)")
@app_commands.describe(plateforme="Ta plateforme de jeu")
@app_commands.choices(plateforme=[
    app_commands.Choice(name="🖥️ PC",           value="pc"),
    app_commands.Choice(name="🟢 Xbox",          value="xbox"),
    app_commands.Choice(name="🔵 PlayStation",   value="playstation"),
])
async def cmd_plateforme(interaction: discord.Interaction, plateforme: str):
    uid = str(interaction.user.id)
    if uid not in user_prefs:
        user_prefs[uid] = {}
    user_prefs[uid]["platform"] = plateforme
    _save(PREFS_FILE, user_prefs)
    plat = PLATFORMS[plateforme]
    embed = discord.Embed(
        title=f"✅ Plateforme définie : {plat['emoji']} {plat['label']}",
        description="Toutes tes commandes utiliseront désormais cette plateforme.\nUtilise `/scan`, `/snipe`, `/erreurs` etc. pour trouver des affaires !",
        color=plat["color"],
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# /setchannel — définir le salon d'alertes automatiques
@tree.command(name="setchannel", description="📢 Définit le salon pour les alertes automatiques")
@app_commands.describe(
    salon="Salon Discord dédié aux alertes trading",
    plateforme="Plateforme pour ce salon"
)
@app_commands.choices(plateforme=[
    app_commands.Choice(name="🖥️ PC",           value="pc"),
    app_commands.Choice(name="🟢 Xbox",          value="xbox"),
    app_commands.Choice(name="🔵 PlayStation",   value="playstation"),
    app_commands.Choice(name="🌐 Toutes",        value="all"),
])
async def cmd_setchannel(interaction: discord.Interaction, salon: discord.TextChannel, plateforme: str = "all"):
    gid = str(interaction.guild_id)
    if gid not in channels:
        channels[gid] = {}
    if plateforme == "all":
        for p in PLATFORMS:
            channels[gid][p] = salon.id
    else:
        channels[gid][plateforme] = salon.id
    _save(CHANNELS_FILE, channels)

    embed = discord.Embed(title="✅ Salon configuré !", color=0x57F287)
    if plateforme == "all":
        embed.description = f"Les alertes pour **toutes les plateformes** seront envoyées dans {salon.mention}"
    else:
        plat = PLATFORMS[plateforme]
        embed.description = f"Les alertes {plat['emoji']} **{plat['label']}** seront envoyées dans {salon.mention}"
    embed.set_footer(text="Le bot scannera en continu et alertera dès qu'une affaire est trouvée !")
    await interaction.response.send_message(embed=embed)


# /scan — scan manuel
@tree.command(name="scan", description="🔍 Scan immédiat — trouve les meilleures affaires")
@app_commands.describe(plateforme="Plateforme (laisse vide = ta plateforme par défaut)")
@app_commands.choices(plateforme=[
    app_commands.Choice(name="🖥️ PC",           value="pc"),
    app_commands.Choice(name="🟢 Xbox",          value="xbox"),
    app_commands.Choice(name="🔵 PlayStation",   value="playstation"),
])
async def cmd_scan(interaction: discord.Interaction, plateforme: str = None):
    await interaction.response.defer()
    plat = plateforme or get_user_platform(interaction.user.id)
    players = await get_players(platform=plat)
    await interaction.followup.send(embed=build_summary_embed(players, plat, "Scan Général"))
    for i, p in enumerate(players[:3]):
        await interaction.followup.send(embed=build_alert_embed(p, plat))


# /snipe — snipe avec budget
@tree.command(name="snipe", description="⚡ Snipe — meilleures affaires dans ton budget")
@app_commands.describe(budget="Budget max en coins", plateforme="Plateforme")
@app_commands.choices(plateforme=[
    app_commands.Choice(name="🖥️ PC",           value="pc"),
    app_commands.Choice(name="🟢 Xbox",          value="xbox"),
    app_commands.Choice(name="🔵 PlayStation",   value="playstation"),
])
async def cmd_snipe(interaction: discord.Interaction, budget: int = 50000, plateforme: str = None):
    await interaction.response.defer()
    plat    = plateforme or get_user_platform(interaction.user.id)
    filters = {"max_price": budget}
    players = await get_players(filters, plat)
    await interaction.followup.send(embed=build_summary_embed(players, plat, "⚡ Snipe", f"Budget: {fmt(budget)} coins"))
    for p in players[:3]:
        await interaction.followup.send(embed=build_alert_embed(p, plat))


# /erreurs — erreurs de prix
@tree.command(name="erreurs", description="🚨 Erreurs de prix — cartes massivement sous-évaluées")
@app_commands.describe(reduction="Réduction minimum en % (ex: 20)", plateforme="Plateforme")
@app_commands.choices(plateforme=[
    app_commands.Choice(name="🖥️ PC",           value="pc"),
    app_commands.Choice(name="🟢 Xbox",          value="xbox"),
    app_commands.Choice(name="🔵 PlayStation",   value="playstation"),
])
async def cmd_erreurs(interaction: discord.Interaction, reduction: int = 20, plateforme: str = None):
    await interaction.response.defer()
    plat    = plateforme or get_user_platform(interaction.user.id)
    players = await get_players(platform=plat)
    filtered = [p for p in players if p["discount"] >= reduction]
    await interaction.followup.send(embed=build_summary_embed(filtered, plat, "🚨 Erreurs de Prix", f"Réduction min: -{reduction}%"))
    for p in filtered[:5]:
        await interaction.followup.send(embed=build_alert_embed(p, plat))


# /position — par position
@tree.command(name="position", description="📍 Cherche par position (ST, CAM, CB...)")
@app_commands.describe(pos="Position du joueur", min_profit="Profit min en coins", plateforme="Plateforme")
@app_commands.choices(
    pos=[app_commands.Choice(name=p, value=p) for p in POSITIONS],
    plateforme=[
        app_commands.Choice(name="🖥️ PC",           value="pc"),
        app_commands.Choice(name="🟢 Xbox",          value="xbox"),
        app_commands.Choice(name="🔵 PlayStation",   value="playstation"),
    ]
)
async def cmd_position(interaction: discord.Interaction, pos: str, min_profit: int = 500, plateforme: str = None):
    await interaction.response.defer()
    plat    = plateforme or get_user_platform(interaction.user.id)
    players = await get_players({"position": pos}, plat)
    filtered = [p for p in players if p["profit"] >= min_profit]
    await interaction.followup.send(embed=build_summary_embed(filtered, plat, f"📍 {pos}", f"Position: {pos} · Profit min: {fmt(min_profit)}"))
    for p in filtered[:3]:
        await interaction.followup.send(embed=build_alert_embed(p, plat))


# /nation — par nation
@tree.command(name="nation", description="🌍 Cherche par nationalité")
@app_commands.describe(nation="Nom de la nation (ex: France, Brazil)", min_profit="Profit min", plateforme="Plateforme")
@app_commands.choices(plateforme=[
    app_commands.Choice(name="🖥️ PC",           value="pc"),
    app_commands.Choice(name="🟢 Xbox",          value="xbox"),
    app_commands.Choice(name="🔵 PlayStation",   value="playstation"),
])
async def cmd_nation(interaction: discord.Interaction, nation: str, min_profit: int = 500, plateforme: str = None):
    await interaction.response.defer()
    plat    = plateforme or get_user_platform(interaction.user.id)
    players = await get_players({"nation": nation}, plat)
    filtered = [p for p in players if p["profit"] >= min_profit]
    await interaction.followup.send(embed=build_summary_embed(filtered, plat, f"🌍 {nation}", f"Nation: {nation}"))
    for p in filtered[:3]:
        await interaction.followup.send(embed=build_alert_embed(p, plat))


# /ligue — par ligue
@tree.command(name="ligue", description="🏆 Cherche par ligue")
@app_commands.describe(ligue="Nom de la ligue (ex: Premier League)", min_profit="Profit min", plateforme="Plateforme")
@app_commands.choices(plateforme=[
    app_commands.Choice(name="🖥️ PC",           value="pc"),
    app_commands.Choice(name="🟢 Xbox",          value="xbox"),
    app_commands.Choice(name="🔵 PlayStation",   value="playstation"),
])
async def cmd_ligue(interaction: discord.Interaction, ligue: str, min_profit: int = 500, plateforme: str = None):
    await interaction.response.defer()
    plat    = plateforme or get_user_platform(interaction.user.id)
    players = await get_players({"league": ligue}, plat)
    filtered = [p for p in players if p["profit"] >= min_profit]
    await interaction.followup.send(embed=build_summary_embed(filtered, plat, f"🏆 {ligue}", f"Ligue: {ligue}"))
    for p in filtered[:3]:
        await interaction.followup.send(embed=build_alert_embed(p, plat))


# /meta — cartes méta
@tree.command(name="meta", description="🔥 Top cartes méta compétitives")
@app_commands.describe(min_note="Note minimum (ex: 87)", plateforme="Plateforme")
@app_commands.choices(plateforme=[
    app_commands.Choice(name="🖥️ PC",           value="pc"),
    app_commands.Choice(name="🟢 Xbox",          value="xbox"),
    app_commands.Choice(name="🔵 PlayStation",   value="playstation"),
])
async def cmd_meta(interaction: discord.Interaction, min_note: int = 87, plateforme: str = None):
    await interaction.response.defer()
    plat    = plateforme or get_user_platform(interaction.user.id)
    players = await get_players({"min_rating": min_note}, plat)
    await interaction.followup.send(embed=build_summary_embed(players, plat, f"🔥 Meta {min_note}+", f"Note min: {min_note}"))
    for p in players[:3]:
        await interaction.followup.send(embed=build_alert_embed(p, plat))


# /premium — cartes premium
@tree.command(name="premium", description="💎 Cartes premium — gros profits")
@app_commands.describe(min_prix="Prix minimum (ex: 50000)", min_profit="Profit min", plateforme="Plateforme")
@app_commands.choices(plateforme=[
    app_commands.Choice(name="🖥️ PC",           value="pc"),
    app_commands.Choice(name="🟢 Xbox",          value="xbox"),
    app_commands.Choice(name="🔵 PlayStation",   value="playstation"),
])
async def cmd_premium(interaction: discord.Interaction, min_prix: int = 50000, min_profit: int = 5000, plateforme: str = None):
    await interaction.response.defer()
    plat    = plateforme or get_user_platform(interaction.user.id)
    players = await get_players({"min_price": min_prix}, plat)
    filtered = [p for p in players if p["profit"] >= min_profit]
    await interaction.followup.send(embed=build_summary_embed(filtered, plat, "💎 Premium", f"Prix min: {fmt(min_prix)} · Profit min: {fmt(min_profit)}"))
    for p in filtered[:3]:
        await interaction.followup.send(embed=build_alert_embed(p, plat))


# /budget — petit budget
@tree.command(name="budget", description="💰 Budget trading — volume élevé, petit prix")
@app_commands.describe(max_prix="Prix maximum (ex: 10000)", plateforme="Plateforme")
@app_commands.choices(plateforme=[
    app_commands.Choice(name="🖥️ PC",           value="pc"),
    app_commands.Choice(name="🟢 Xbox",          value="xbox"),
    app_commands.Choice(name="🔵 PlayStation",   value="playstation"),
])
async def cmd_budget(interaction: discord.Interaction, max_prix: int = 10000, plateforme: str = None):
    await interaction.response.defer()
    plat    = plateforme or get_user_platform(interaction.user.id)
    players = await get_players({"max_price": max_prix}, plat)
    await interaction.followup.send(embed=build_summary_embed(players, plat, "💰 Budget", f"Max: {fmt(max_prix)} coins"))
    for p in players[:5]:
        await interaction.followup.send(embed=build_alert_embed(p, plat))


# /joueur — recherche un joueur précis
@tree.command(name="joueur", description="🔎 Recherche un joueur spécifique")
@app_commands.describe(nom="Nom du joueur (ex: Mbappe, Bellingham)", plateforme="Plateforme")
@app_commands.choices(plateforme=[
    app_commands.Choice(name="🖥️ PC",           value="pc"),
    app_commands.Choice(name="🟢 Xbox",          value="xbox"),
    app_commands.Choice(name="🔵 PlayStation",   value="playstation"),
])
async def cmd_joueur(interaction: discord.Interaction, nom: str, plateforme: str = None):
    await interaction.response.defer()
    plat = plateforme or get_user_platform(interaction.user.id)
    # Recherche sur Futbin
    data = await fetch_json(f"https://www.futbin.com/25/players", {"search": nom})
    players = []
    if isinstance(data, list):
        for p in data[:5]:
            pk_m, pk_b = PLATFORM_PRICE_KEY.get(plat, ("LCPrice","LCPrice2"))
            market = parse_price(p.get(pk_m) or 0)
            snipe  = parse_price(p.get(pk_b) or market)
            if market < 300: continue
            c = calc(snipe, market)
            pid = str(p.get("id",""))
            players.append({
                "id": pid, "name": p.get("Player_Name","?"),
                "rating": int(p.get("Rating") or 0),
                "position": str(p.get("Position","?")).upper(),
                "club": p.get("Club","?"), "nation": p.get("Nation","?"),
                "league": p.get("League","?"), "snipe": snipe, "market": market,
                "image": p.get("Player_Image") or f"https://cdn.futbin.com/content/fifa25/img/players/{pid}.png",
                "url": f"https://www.futbin.com/25/player/{pid}",
                **c,
            })
    if players:
        await interaction.followup.send(embed=build_summary_embed(players, plat, f"🔎 {nom}"))
        for p in players[:2]:
            await interaction.followup.send(embed=build_alert_embed(p, plat))
    else:
        await interaction.followup.send(f"❌ Joueur `{nom}` introuvable. Vérifie l'orthographe.", ephemeral=True)


# /stats — statistiques du bot
@tree.command(name="stats", description="📊 Statistiques du bot")
async def cmd_stats(interaction: discord.Interaction):
    embed = discord.Embed(title="📊 FC26 Trading Bot — Statistiques", color=0x5865F2, timestamp=datetime.utcnow())
    embed.add_field(name="🔔 Alertes envoyées",    value=str(stats.get("total_sent",0)),          inline=True)
    embed.add_field(name="🔍 Scans effectués",     value=str(stats.get("scans",0)),               inline=True)
    embed.add_field(name="💰 Profit total affiché",value=f"{fmt(stats.get('total_profit_shown',0))} coins", inline=True)
    embed.add_field(name="⚡ Délai de scan",        value=f"{SCAN_DELAY}s",                        inline=True)
    embed.add_field(name="💵 Profit min",           value=f"{fmt(MIN_PROFIT)} coins",              inline=True)
    embed.add_field(name="🏷️ Taxe EA",              value="5%",                                    inline=True)
    plat_str = "\n".join(f"{v['emoji']} {v['label']}" for v in PLATFORMS.values())
    embed.add_field(name="🎮 Plateformes", value=plat_str, inline=False)
    await interaction.response.send_message(embed=embed)


# /aide — guide complet
@tree.command(name="aide", description="❓ Guide complet du bot")
async def cmd_aide(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🤖 FC26 Trading Bot — Guide",
        description="Ton assistant pour dominer le marché FC 26 UT !",
        color=0x5865F2,
    )
    embed.add_field(name="⚙️ Configuration", value=(
        "`/plateforme` — Choisir ta plateforme\n"
        "`/setchannel` — Définir le salon d'alertes auto"
    ), inline=False)
    embed.add_field(name="🔍 Scans", value=(
        "`/scan` — Scan général\n"
        "`/snipe [budget]` — Affaires dans ton budget\n"
        "`/erreurs [%]` — Cartes sous-évaluées\n"
        "`/meta [note]` — Top cartes compétitives\n"
        "`/premium [min_prix]` — Grosses cartes\n"
        "`/budget [max_prix]` — Petit budget"
    ), inline=False)
    embed.add_field(name="🎯 Filtres avancés", value=(
        "`/position <POS>` — Par position (ST, CAM...)\n"
        "`/nation <pays>` — Par nationalité\n"
        "`/ligue <ligue>` — Par ligue\n"
        "`/joueur <nom>` — Recherche un joueur précis"
    ), inline=False)
    embed.add_field(name="📊 Autres", value=(
        "`/stats` — Statistiques du bot\n"
        "`/aide` — Ce guide"
    ), inline=False)
    embed.add_field(name="💡 Astuce", value=(
        "1️⃣ Commence par `/plateforme` pour choisir PC/Xbox/PS\n"
        "2️⃣ Utilise `/setchannel` pour recevoir les alertes auto\n"
        "3️⃣ Les alertes arrivent **instantanément** dès qu'une affaire est trouvée !"
    ), inline=False)
    embed.set_footer(text="Taxe EA 5% incluse dans tous les calculs")
    await interaction.response.send_message(embed=embed)


# ─── Scan Automatique Permanent ───────────────────────────────────────────────
@tasks.loop(seconds=SCAN_DELAY)
async def auto_scan():
    """Scan toutes les plateformes en continu. Envoie dès qu'une affaire est trouvée."""
    stats["scans"] = stats.get("scans", 0) + 1

    for guild in bot.guilds:
        gid = str(guild.id)
        guild_channels = channels.get(gid, {})
        if not guild_channels:
            continue

        for platform, channel_id in guild_channels.items():
            channel = bot.get_channel(channel_id)
            if not channel:
                continue

            try:
                players = await get_players(platform=platform)
                sent_count = 0

                for p in players:
                    if sent_count >= MAX_ALERTS:
                        break

                    # Clé unique pour éviter les doublons
                    alert_key = f"{gid}_{platform}_{p['id']}_{p['snipe']}"
                    if alert_key in sent_alerts:
                        continue

                    await channel.send(embed=build_alert_embed(p, platform))
                    sent_alerts[alert_key] = datetime.utcnow().isoformat()
                    stats["total_sent"]          = stats.get("total_sent", 0) + 1
                    stats["total_profit_shown"]  = stats.get("total_profit_shown", 0) + p["profit"]
                    sent_count += 1
                    await asyncio.sleep(1)

                # Nettoyage des vieilles alertes (garde les 500 dernières)
                if len(sent_alerts) > 500:
                    keys = list(sent_alerts.keys())
                    for k in keys[:-500]:
                        del sent_alerts[k]

                _save(SENT_FILE, sent_alerts)
                _save(STATS_FILE, stats)

            except Exception as e:
                log.error(f"auto_scan error [{platform}] guild {gid}: {e}")

        await asyncio.sleep(3)


# ─── Events ───────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await tree.sync()
    auto_scan.start()
    log.info(f"✅ {bot.user} connecté sur {len(bot.guilds)} serveur(s)")
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="le marché FC26 💹")
    )

@bot.event
async def on_guild_join(guild):
    log.info(f"Nouveau serveur : {guild.name}")


if __name__ == "__main__":
    bot.run(TOKEN)
