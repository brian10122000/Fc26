"""
╔══════════════════════════════════════════════════════════════╗
║       FC26 ULTIMATE TRADING BOT — Version Anti-Cloudflare   ║
║  Sources: futwiz.com + futgg + futbin avec contournement     ║
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
import random
from datetime import datetime
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

TOKEN        = os.getenv("DISCORD_TOKEN", "VOTRE_TOKEN_ICI")
MIN_PROFIT   = 500
EA_TAX       = 0.05
SCAN_DELAY   = 45    # secondes
MAX_ALERTS   = 6

# ─── User-Agents rotatifs (contourne Cloudflare) ──────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]

def get_headers(referer: str = "https://www.futbin.com/") -> dict:
    return {
        "User-Agent":      random.choice(USER_AGENTS),
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection":      "keep-alive",
        "Referer":         referer,
        "Sec-Fetch-Dest":  "document",
        "Sec-Fetch-Mode":  "navigate",
        "Sec-Fetch-Site":  "same-origin",
        "Cache-Control":   "max-age=0",
    }

def get_json_headers(referer: str = "https://www.futbin.com/") -> dict:
    return {
        "User-Agent":        random.choice(USER_AGENTS),
        "Accept":            "application/json, text/javascript, */*; q=0.01",
        "Accept-Language":   "fr-FR,fr;q=0.9,en-US;q=0.8",
        "Accept-Encoding":   "gzip, deflate, br",
        "X-Requested-With":  "XMLHttpRequest",
        "Referer":           referer,
        "Origin":            "https://www.futbin.com",
        "Connection":        "keep-alive",
    }

PLATFORMS = {
    "pc":          {"label": "PC",          "emoji": "🖥️",  "color": 0x5865F2, "price_key": "LCPrice",  "buy_key": "LCPrice2"},
    "xbox":        {"label": "Xbox",        "emoji": "🟢",  "color": 0x107C10, "price_key": "XBPrice",  "buy_key": "XBPrice2"},
    "playstation": {"label": "PlayStation", "emoji": "🔵",  "color": 0x003791, "price_key": "PSPrice",  "buy_key": "PSPrice2"},
}

POSITIONS = ["ST","CF","CAM","CM","CDM","LW","RW","LB","RB","CB","GK","LM","RM","LWB","RWB"]

# ─── Storage ──────────────────────────────────────────────────────────────────
def _load(path, default):
    if os.path.exists(path):
        with open(path) as f: return json.load(f)
    return default

def _save(path, data):
    with open(path, "w") as f: json.dump(data, f, indent=2, ensure_ascii=False)

user_prefs  = _load("user_prefs.json", {})
channels    = _load("channels.json", {})
sent_alerts = _load("sent_alerts.json", {})
stats       = _load("stats.json", {"total_sent":0,"total_profit":0,"scans":0,"errors":0,"last_source":""})

intents = discord.Intents.default()
intents.message_content = True
bot  = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ─── Helpers ──────────────────────────────────────────────────────────────────
def fmt(n: int) -> str:
    if n >= 1_000_000: return f"{n/1_000_000:.2f}M"
    if n >= 1_000:     return f"{n/1_000:.1f}K"
    return str(n)

def parse_price(v) -> int:
    if not v or v in ("N/A", "—", "-", 0): return 0
    t = str(v).upper().replace(",","").replace(" ","").replace("\xa0","").strip()
    try:
        if "M" in t: return int(float(t.replace("M","")) * 1_000_000)
        if "K" in t: return int(float(t.replace("K","")) * 1_000)
        return int(re.sub(r"[^\d]","",t) or 0)
    except: return 0

def calc(snipe: int, market: int) -> dict:
    sell     = int(market * (1 - EA_TAX))
    profit   = sell - snipe
    roi      = round(profit / snipe * 100, 1) if snipe > 0 else 0
    discount = round((market - snipe) / market * 100, 1) if market > 0 else 0
    return {"sell": sell, "profit": profit, "roi": roi, "discount": discount}

def roi_bar(roi: float) -> str:
    filled = min(int(roi / 4), 12)
    return "█" * filled + "░" * (12 - filled)

def get_user_platform(uid: int) -> str:
    return user_prefs.get(str(uid), {}).get("platform", "pc")

# ─── SESSION AVEC COOKIES (simule un vrai navigateur) ─────────────────────────
async def create_session_with_cookies() -> Optional[aiohttp.ClientSession]:
    """
    Visite d'abord la page principale de Futbin pour obtenir les cookies,
    puis utilise ces cookies pour les requêtes suivantes.
    """
    try:
        session = aiohttp.ClientSession()
        # Visite initiale pour récupérer les cookies Cloudflare
        async with session.get(
            "https://www.futbin.com/",
            headers=get_headers("https://google.com"),
            timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            log.info(f"Session init Futbin: HTTP {r.status}")
        await asyncio.sleep(random.uniform(1.5, 3.0))
        return session
    except Exception as e:
        log.error(f"create_session error: {e}")
        return None

# ─── SOURCE 1 : Futbin API JSON ───────────────────────────────────────────────
async def fetch_futbin_api(filters: dict, platform: str) -> list[dict]:
    plat = PLATFORMS[platform]
    params = {
        "page":     1,
        "sort":     plat["price_key"],
        "order":    "asc",
        "per_page": 60,
        "version":  "all",
    }
    if filters.get("position"):  params["position"]   = filters["position"]
    if filters.get("nation"):    params["nation"]      = filters["nation"]
    if filters.get("league"):    params["league"]      = filters["league"]
    if filters.get("club"):      params["club"]        = filters["club"]
    if filters.get("min_rating"):params["min_rating"]  = filters["min_rating"]
    if filters.get("max_rating"):params["max_rating"]  = filters["max_rating"]
    if filters.get("min_price"): params["min_price"]   = filters["min_price"]
    if filters.get("max_price"): params["max_price"]   = filters["max_price"]

    try:
        session = await create_session_with_cookies()
        if not session: return []
        async with session:
            async with session.get(
                "https://www.futbin.com/25/players",
                params=params,
                headers=get_json_headers("https://www.futbin.com/25/players"),
                timeout=aiohttp.ClientTimeout(total=20)
            ) as r:
                log.info(f"Futbin API: HTTP {r.status} | Content-Type: {r.headers.get('Content-Type','?')}")
                if r.status != 200:
                    return []
                text = await r.text()
                # Vérifie si c'est du JSON ou du HTML (Cloudflare block)
                if text.strip().startswith("{") or text.strip().startswith("["):
                    data = json.loads(text)
                    players = data if isinstance(data, list) else data.get("data", data.get("players", []))
                    result = []
                    for p in players:
                        market = parse_price(p.get(plat["price_key"]) or p.get("LCPrice") or 0)
                        snipe  = parse_price(p.get(plat["buy_key"])   or p.get("LCPrice2") or market)
                        if market < 500 or snipe <= 0: continue
                        c = calc(snipe, market)
                        if c["profit"] < MIN_PROFIT: continue
                        pid = str(p.get("id",""))
                        result.append({
                            "id":       pid,
                            "name":     p.get("Player_Name") or p.get("name","?"),
                            "rating":   int(p.get("Rating") or p.get("rating") or 0),
                            "position": str(p.get("Position") or p.get("position","?")).upper(),
                            "club":     p.get("Club")   or p.get("club","?"),
                            "nation":   p.get("Nation") or p.get("nation","?"),
                            "league":   p.get("League") or p.get("league","?"),
                            "snipe":    snipe,
                            "market":   market,
                            "image":    p.get("Player_Image") or f"https://cdn.futbin.com/content/fifa25/img/players/{pid}.png",
                            "url":      f"https://www.futbin.com/25/player/{pid}",
                            "source":   "futbin_api",
                            **c,
                        })
                    log.info(f"Futbin API: {len(result)} joueurs trouvés avec profit >= {MIN_PROFIT}")
                    stats["last_source"] = "futbin_api"
                    return result
                else:
                    log.warning("Futbin API: réponse HTML (Cloudflare block), passage au fallback")
                    return []
    except Exception as e:
        log.error(f"fetch_futbin_api error: {e}")
        return []

# ─── SOURCE 2 : Futbin HTML scraping avancé ───────────────────────────────────
async def fetch_futbin_html(filters: dict, platform: str) -> list[dict]:
    plat   = PLATFORMS[platform]
    params = {"page": 1, "sort": plat["price_key"], "order": "asc", "per_page": 30, "version": "all"}
    if filters.get("position"):  params["position"]   = filters["position"]
    if filters.get("min_rating"):params["min_rating"]  = filters["min_rating"]
    if filters.get("max_price"): params["max_price"]   = filters["max_price"]

    try:
        session = await create_session_with_cookies()
        if not session: return []
        async with session:
            # Simule la navigation: visite d'abord la page players
            await asyncio.sleep(random.uniform(1, 2))
            async with session.get(
                "https://www.futbin.com/25/players",
                params=params,
                headers=get_headers("https://www.futbin.com/"),
                timeout=aiohttp.ClientTimeout(total=25)
            ) as r:
                log.info(f"Futbin HTML: HTTP {r.status}")
                if r.status != 200: return []
                html = await r.text()

        soup    = BeautifulSoup(html, "html.parser")
        players = []

        # Cherche le tableau principal
        rows = soup.select("table#repTb tbody tr, .players-table tbody tr, table.table tbody tr")
        log.info(f"Futbin HTML: {len(rows)} lignes trouvées")

        for row in rows[:40]:
            try:
                cols = row.select("td")
                if len(cols) < 5: continue

                link_el  = row.select_one("a[href*='/player/']")
                name_el  = row.select_one(".player-name, .pname, .player_name, td:nth-child(1) a")
                img_el   = row.select_one("img[src*='players'], img[data-src*='players']")
                rat_el   = row.select_one(".rating, td:nth-child(2)")
                pos_el   = row.select_one(".position, td:nth-child(3)")

                # Récupère les prix selon la colonne plateforme
                price_cols = [parse_price(c.get_text()) for c in cols]
                price_cols = [p for p in price_cols if p > 100]
                if len(price_cols) < 2: continue

                market = price_cols[0]
                snipe  = price_cols[1] if price_cols[1] < market else market

                if market < 500: continue
                c = calc(snipe, market)
                if c["profit"] < MIN_PROFIT: continue

                href = link_el.get("href","") if link_el else ""
                pid  = re.search(r"/player/(\d+)", href)
                pid  = pid.group(1) if pid else str(random.randint(10000,99999))

                img_src = ""
                if img_el:
                    img_src = img_el.get("src") or img_el.get("data-src","")
                if not img_src:
                    img_src = f"https://cdn.futbin.com/content/fifa25/img/players/{pid}.png"

                players.append({
                    "id":       pid,
                    "name":     name_el.get_text(strip=True) if name_el else "Joueur",
                    "rating":   int(re.sub(r"\D","", rat_el.get_text()) or 0) if rat_el else 0,
                    "position": pos_el.get_text(strip=True).upper() if pos_el else "?",
                    "club":     "?", "nation": "?", "league": "?",
                    "snipe":    snipe,
                    "market":   market,
                    "image":    img_src,
                    "url":      "https://www.futbin.com" + href if href else "https://www.futbin.com/25/players",
                    "source":   "futbin_html",
                    **c,
                })
            except Exception as e:
                log.debug(f"row parse: {e}")

        log.info(f"Futbin HTML: {len(players)} joueurs avec profit")
        stats["last_source"] = "futbin_html"
        return players

    except Exception as e:
        log.error(f"fetch_futbin_html error: {e}")
        return []

# ─── SOURCE 3 : FUTWIZ (alternative fiable) ───────────────────────────────────
async def fetch_futwiz(filters: dict, platform: str) -> list[dict]:
    """
    Futwiz.com — site alternatif avec prix similaires à Futbin.
    Beaucoup moins protégé contre le scraping.
    """
    plat_map = {"pc": "pc", "xbox": "xbox", "playstation": "ps"}
    plat_str = plat_map.get(platform, "pc")

    params = {"page": 1, "order": "asc", "sort": f"{plat_str}_price"}
    if filters.get("position"):   params["position"]   = filters["position"].lower()
    if filters.get("min_rating"): params["min_rating"]  = filters["min_rating"]
    if filters.get("max_price"):  params["max_price"]   = filters["max_price"]

    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://www.futwiz.com/en/fc25/players",
                params=params,
                headers=get_headers("https://www.futwiz.com/"),
                timeout=aiohttp.ClientTimeout(total=20)
            ) as r:
                log.info(f"Futwiz: HTTP {r.status}")
                if r.status != 200: return []
                html = await r.text()

        soup    = BeautifulSoup(html, "html.parser")
        players = []

        for card in soup.select(".player-card, .player-item, .table-player")[:40]:
            try:
                name_el   = card.select_one(".player-name, .name, h3, h4")
                rating_el = card.select_one(".rating, .ovr, .overall")
                pos_el    = card.select_one(".position, .pos")
                price_els = card.select(".price, .market-price, [data-price]")
                img_el    = card.select_one("img")
                link_el   = card.select_one("a[href*='/player']")

                prices = [parse_price(p.get_text()) for p in price_els if parse_price(p.get_text()) > 100]
                if len(prices) < 1: continue

                market = prices[0]
                snipe  = prices[1] if len(prices) > 1 and prices[1] < market else int(market * 0.88)

                if market < 500: continue
                c = calc(snipe, market)
                if c["profit"] < MIN_PROFIT: continue

                href = link_el.get("href","") if link_el else ""
                pid  = re.search(r"/player/(\d+)|/(\d+)", href)
                pid  = (pid.group(1) or pid.group(2)) if pid else "0"

                players.append({
                    "id":       pid,
                    "name":     name_el.get_text(strip=True) if name_el else "Joueur",
                    "rating":   int(re.sub(r"\D","", rating_el.get_text()) or 0) if rating_el else 0,
                    "position": pos_el.get_text(strip=True).upper() if pos_el else "?",
                    "club":     "?", "nation": "?", "league": "?",
                    "snipe":    snipe,
                    "market":   market,
                    "image":    img_el.get("src","") if img_el else "",
                    "url":      ("https://www.futwiz.com" + href) if href else "https://www.futwiz.com/en/fc25/players",
                    "source":   "futwiz",
                    **c,
                })
            except Exception as e:
                log.debug(f"futwiz card: {e}")

        log.info(f"Futwiz: {len(players)} joueurs trouvés")
        stats["last_source"] = "futwiz"
        return players

    except Exception as e:
        log.error(f"fetch_futwiz error: {e}")
        return []

# ─── ORCHESTRATEUR : essaie les sources dans l'ordre ─────────────────────────
async def get_players(filters: dict = None, platform: str = "pc") -> list[dict]:
    if filters is None: filters = {}

    # Source 1: Futbin API (meilleure qualité)
    players = await fetch_futbin_api(filters, platform)

    # Source 2: Futbin HTML (si API bloquée)
    if not players:
        log.info("Fallback → Futbin HTML scraping")
        players = await fetch_futbin_html(filters, platform)

    # Source 3: Futwiz (si Futbin complètement bloqué)
    if not players:
        log.info("Fallback → Futwiz.com")
        players = await fetch_futwiz(filters, platform)

    if not players:
        log.warning("⚠️ Toutes les sources ont échoué")
        stats["errors"] = stats.get("errors", 0) + 1
        _save("stats.json", stats)
        return []

    # Filtres supplémentaires
    min_profit_filter = filters.get("min_profit", MIN_PROFIT)
    max_discount      = filters.get("max_discount", 100)
    min_discount      = filters.get("min_discount", 0)

    players = [
        p for p in players
        if p["profit"] >= min_profit_filter
        and p["discount"] >= min_discount
        and p["discount"] <= max_discount
    ]
    players.sort(key=lambda x: x["profit"], reverse=True)
    return players[:25]

# ─── Embeds ───────────────────────────────────────────────────────────────────
SOURCE_EMOJI = {"futbin_api": "✅ Futbin API", "futbin_html": "🔧 Futbin HTML", "futwiz": "🔄 Futwiz"}

def build_alert_embed(p: dict, platform: str) -> discord.Embed:
    plat  = PLATFORMS[platform]
    color = 0xED4245 if p["discount"] >= 20 else (0xFF8C00 if p["discount"] >= 12 else 0x57F287)
    fire  = "🚨🚨🚨" if p["discount"] >= 25 else ("🚨🚨" if p["discount"] >= 15 else "🚨")

    embed = discord.Embed(
        title=f"{plat['emoji']} @{plat['label']}  ·  {fire}",
        url=p["url"], color=color, timestamp=datetime.utcnow()
    )
    embed.add_field(
        name=f"👤 {p['name']}  •  {p['rating']} {p['position']}",
        value=f"🌍 {p['nation']}  ·  🏆 {p['league']}  ·  ⚽ {p['club']}",
        inline=False,
    )
    embed.add_field(name="🎯 Snipe Price",  value=f"**{fmt(p['snipe'])}** 🪙",   inline=True)
    embed.add_field(name="💵 Sells Price",  value=f"**{fmt(p['sell'])}** 🪙",    inline=True)
    embed.add_field(name="💰 Profit",       value=f"**+{fmt(p['profit'])}** 🪙", inline=True)
    embed.add_field(name="🏷️ Réduction",    value=f"**-{p['discount']}%**",      inline=True)
    embed.add_field(name="📈 ROI",          value=f"**{p['roi']}%**",             inline=True)
    embed.add_field(name="📊 Rentabilité",  value=f"`{roi_bar(p['roi'])}`",       inline=True)
    embed.add_field(name="🔗 Lien joueur",  value=f"[Voir sur Futbin]({p['url']})", inline=False)
    if p.get("image"):
        embed.set_thumbnail(url=p["image"])
    src = SOURCE_EMOJI.get(p.get("source",""), "?")
    embed.set_footer(text=f"FC26 Trading Bot · Taxe EA 5% · Source: {src}")
    return embed


def build_summary_embed(players: list, platform: str, title: str, desc: str = "") -> discord.Embed:
    plat  = PLATFORMS[platform]
    embed = discord.Embed(
        title=f"{plat['emoji']} {title} — {len(players)} opportunité(s)",
        description=desc or "Meilleures affaires du moment",
        color=plat["color"], timestamp=datetime.utcnow()
    )
    if players:
        lines = []
        for p in players[:10]:
            dot = "🟢" if p["roi"] > 20 else ("🟡" if p["roi"] > 10 else "🔴")
            lines.append(f"{dot} **{p['name']}** {p['rating']} {p['position']} · 🎯{fmt(p['snipe'])} → +{fmt(p['profit'])} ({p['roi']}%)")
        embed.add_field(name="📋 Top opportunités", value="\n".join(lines), inline=False)
        best = players[0]
        embed.add_field(
            name="🏆 Meilleure affaire",
            value=f"**{best['name']}** — Snipe {fmt(best['snipe'])} · Profit **+{fmt(best['profit'])}** ({best['roi']}% ROI)",
            inline=False
        )
    else:
        embed.add_field(name="😔 Aucun résultat", value=(
            "Futbin peut être temporairement indisponible.\n"
            "Le bot réessaie automatiquement dans 45 secondes."
        ), inline=False)
    src = stats.get("last_source","?")
    embed.set_footer(text=f"FC26 Bot · Source: {SOURCE_EMOJI.get(src,src)} · Taxe EA 5%")
    return embed

# ─── Commandes ────────────────────────────────────────────────────────────────
PLAT_CHOICES = [
    app_commands.Choice(name="🖥️ PC",         value="pc"),
    app_commands.Choice(name="🟢 Xbox",        value="xbox"),
    app_commands.Choice(name="🔵 PlayStation", value="playstation"),
]

@tree.command(name="plateforme", description="🎮 Choisis ta plateforme par défaut")
@app_commands.choices(plateforme=PLAT_CHOICES)
async def cmd_plateforme(interaction: discord.Interaction, plateforme: str):
    uid = str(interaction.user.id)
    user_prefs.setdefault(uid, {})["platform"] = plateforme
    _save("user_prefs.json", user_prefs)
    plat = PLATFORMS[plateforme]
    await interaction.response.send_message(
        embed=discord.Embed(title=f"✅ Plateforme : {plat['emoji']} {plat['label']}", color=plat["color"]),
        ephemeral=True
    )

@tree.command(name="setchannel", description="📢 Définit le salon pour les alertes automatiques")
@app_commands.describe(salon="Salon dédié", plateforme="Plateforme (ou toutes)")
@app_commands.choices(plateforme=[*PLAT_CHOICES, app_commands.Choice(name="🌐 Toutes", value="all")])
async def cmd_setchannel(interaction: discord.Interaction, salon: discord.TextChannel, plateforme: str = "all"):
    gid = str(interaction.guild_id)
    channels.setdefault(gid, {})
    targets = list(PLATFORMS.keys()) if plateforme == "all" else [plateforme]
    for p in targets:
        channels[gid][p] = salon.id
    _save("channels.json", channels)
    embed = discord.Embed(title="✅ Salon configuré !", color=0x57F287,
        description=f"Alertes envoyées dans {salon.mention}\n\n⚡ Le bot scanne en **continu** et alerte instantanément !")
    await interaction.response.send_message(embed=embed)

@tree.command(name="scan", description="🔍 Scan immédiat")
@app_commands.choices(plateforme=PLAT_CHOICES)
async def cmd_scan(interaction: discord.Interaction, plateforme: str = None):
    await interaction.response.defer()
    plat    = plateforme or get_user_platform(interaction.user.id)
    players = await get_players(platform=plat)
    await interaction.followup.send(embed=build_summary_embed(players, plat, "🔍 Scan Général"))
    for p in players[:3]:
        await interaction.followup.send(embed=build_alert_embed(p, plat))

@tree.command(name="snipe", description="⚡ Affaires dans ton budget")
@app_commands.describe(budget="Budget max en coins")
@app_commands.choices(plateforme=PLAT_CHOICES)
async def cmd_snipe(interaction: discord.Interaction, budget: int = 50000, plateforme: str = None):
    await interaction.response.defer()
    plat    = plateforme or get_user_platform(interaction.user.id)
    players = await get_players({"max_price": budget}, plat)
    await interaction.followup.send(embed=build_summary_embed(players, plat, "⚡ Snipe", f"Budget: {fmt(budget)} coins"))
    for p in players[:3]: await interaction.followup.send(embed=build_alert_embed(p, plat))

@tree.command(name="erreurs", description="🚨 Cartes massivement sous-évaluées")
@app_commands.describe(reduction="Réduction min en %")
@app_commands.choices(plateforme=PLAT_CHOICES)
async def cmd_erreurs(interaction: discord.Interaction, reduction: int = 20, plateforme: str = None):
    await interaction.response.defer()
    plat    = plateforme or get_user_platform(interaction.user.id)
    players = await get_players({"min_discount": reduction}, plat)
    await interaction.followup.send(embed=build_summary_embed(players, plat, "🚨 Erreurs de Prix", f"Réduction min: -{reduction}%"))
    for p in players[:5]: await interaction.followup.send(embed=build_alert_embed(p, plat))

@tree.command(name="position", description="📍 Par position")
@app_commands.choices(pos=[app_commands.Choice(name=p, value=p) for p in POSITIONS], plateforme=PLAT_CHOICES)
async def cmd_position(interaction: discord.Interaction, pos: str, min_profit: int = 500, plateforme: str = None):
    await interaction.response.defer()
    plat    = plateforme or get_user_platform(interaction.user.id)
    players = await get_players({"position": pos, "min_profit": min_profit}, plat)
    await interaction.followup.send(embed=build_summary_embed(players, plat, f"📍 {pos}", f"Position: {pos}"))
    for p in players[:3]: await interaction.followup.send(embed=build_alert_embed(p, plat))

@tree.command(name="nation", description="🌍 Par nationalité")
@app_commands.choices(plateforme=PLAT_CHOICES)
async def cmd_nation(interaction: discord.Interaction, nation: str, min_profit: int = 500, plateforme: str = None):
    await interaction.response.defer()
    plat    = plateforme or get_user_platform(interaction.user.id)
    players = await get_players({"nation": nation, "min_profit": min_profit}, plat)
    await interaction.followup.send(embed=build_summary_embed(players, plat, f"🌍 {nation}"))
    for p in players[:3]: await interaction.followup.send(embed=build_alert_embed(p, plat))

@tree.command(name="ligue", description="🏆 Par ligue")
@app_commands.choices(plateforme=PLAT_CHOICES)
async def cmd_ligue(interaction: discord.Interaction, ligue: str, min_profit: int = 500, plateforme: str = None):
    await interaction.response.defer()
    plat    = plateforme or get_user_platform(interaction.user.id)
    players = await get_players({"league": ligue, "min_profit": min_profit}, plat)
    await interaction.followup.send(embed=build_summary_embed(players, plat, f"🏆 {ligue}"))
    for p in players[:3]: await interaction.followup.send(embed=build_alert_embed(p, plat))

@tree.command(name="meta", description="🔥 Top cartes méta")
@app_commands.choices(plateforme=PLAT_CHOICES)
async def cmd_meta(interaction: discord.Interaction, min_note: int = 87, plateforme: str = None):
    await interaction.response.defer()
    plat    = plateforme or get_user_platform(interaction.user.id)
    players = await get_players({"min_rating": min_note}, plat)
    await interaction.followup.send(embed=build_summary_embed(players, plat, f"🔥 Meta {min_note}+"))
    for p in players[:3]: await interaction.followup.send(embed=build_alert_embed(p, plat))

@tree.command(name="premium", description="💎 Cartes premium")
@app_commands.choices(plateforme=PLAT_CHOICES)
async def cmd_premium(interaction: discord.Interaction, min_prix: int = 50000, min_profit: int = 5000, plateforme: str = None):
    await interaction.response.defer()
    plat    = plateforme or get_user_platform(interaction.user.id)
    players = await get_players({"min_price": min_prix, "min_profit": min_profit}, plat)
    await interaction.followup.send(embed=build_summary_embed(players, plat, "💎 Premium"))
    for p in players[:3]: await interaction.followup.send(embed=build_alert_embed(p, plat))

@tree.command(name="budget", description="💰 Petit budget")
@app_commands.choices(plateforme=PLAT_CHOICES)
async def cmd_budget(interaction: discord.Interaction, max_prix: int = 10000, plateforme: str = None):
    await interaction.response.defer()
    plat    = plateforme or get_user_platform(interaction.user.id)
    players = await get_players({"max_price": max_prix}, plat)
    await interaction.followup.send(embed=build_summary_embed(players, plat, "💰 Budget", f"Max: {fmt(max_prix)}"))
    for p in players[:5]: await interaction.followup.send(embed=build_alert_embed(p, plat))

@tree.command(name="joueur", description="🔎 Recherche un joueur")
@app_commands.choices(plateforme=PLAT_CHOICES)
async def cmd_joueur(interaction: discord.Interaction, nom: str, plateforme: str = None):
    await interaction.response.defer()
    plat = plateforme or get_user_platform(interaction.user.id)
    data = None
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://www.futbin.com/25/players",
                params={"search": nom, "per_page": 10},
                headers=get_json_headers(),
                timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                if r.status == 200:
                    text = await r.text()
                    if text.strip().startswith("[") or text.strip().startswith("{"):
                        data = json.loads(text)
    except: pass

    players = []
    if isinstance(data, list):
        pk_m = PLATFORMS[plat]["price_key"]
        pk_b = PLATFORMS[plat]["buy_key"]
        for p in data[:5]:
            market = parse_price(p.get(pk_m) or p.get("LCPrice") or 0)
            snipe  = parse_price(p.get(pk_b) or p.get("LCPrice2") or market)
            if market < 200: continue
            c   = calc(snipe, market)
            pid = str(p.get("id",""))
            players.append({
                "id": pid, "name": p.get("Player_Name","?"),
                "rating": int(p.get("Rating") or 0),
                "position": str(p.get("Position","?")).upper(),
                "club": p.get("Club","?"), "nation": p.get("Nation","?"),
                "league": p.get("League","?"), "snipe": snipe, "market": market,
                "image": f"https://cdn.futbin.com/content/fifa25/img/players/{pid}.png",
                "url": f"https://www.futbin.com/25/player/{pid}",
                "source": "futbin_api", **c,
            })

    if players:
        await interaction.followup.send(embed=build_summary_embed(players, plat, f"🔎 {nom}"))
        for p in players[:2]: await interaction.followup.send(embed=build_alert_embed(p, plat))
    else:
        await interaction.followup.send(f"❌ `{nom}` introuvable ou Futbin indisponible.", ephemeral=True)

@tree.command(name="status", description="📡 Vérifie si le bot scanne correctement")
async def cmd_status(interaction: discord.Interaction):
    await interaction.response.defer()
    # Test rapide en live
    embed = discord.Embed(title="📡 Statut du Bot", color=0x5865F2, timestamp=datetime.utcnow())
    embed.add_field(name="🔄 Scans effectués", value=str(stats.get("scans",0)), inline=True)
    embed.add_field(name="🔔 Alertes envoyées", value=str(stats.get("total_sent",0)), inline=True)
    embed.add_field(name="❌ Erreurs", value=str(stats.get("errors",0)), inline=True)
    embed.add_field(name="📊 Dernière source", value=SOURCE_EMOJI.get(stats.get("last_source","?"),"En attente..."), inline=True)
    embed.add_field(name="⏱️ Intervalle scan", value=f"{SCAN_DELAY}s", inline=True)
    embed.add_field(name="💹 Profit min", value=f"{fmt(MIN_PROFIT)} coins", inline=True)

    # Test connexion live
    embed.add_field(name="🔍 Test en cours...", value="Connexion à Futbin...", inline=False)
    await interaction.followup.send(embed=embed)

    test_players = await get_players(platform="pc")
    result_embed = discord.Embed(
        title="📡 Résultat du test",
        color=0x57F287 if test_players else 0xED4245,
        timestamp=datetime.utcnow()
    )
    if test_players:
        result_embed.add_field(name="✅ Scraping OK !", value=f"{len(test_players)} joueurs trouvés\nSource: {SOURCE_EMOJI.get(stats.get('last_source','?'),'?')}", inline=False)
        result_embed.add_field(name="🏆 Meilleure affaire trouvée", value=f"**{test_players[0]['name']}** — +{fmt(test_players[0]['profit'])} coins", inline=False)
    else:
        result_embed.add_field(name="❌ Aucun résultat", value="Toutes les sources sont bloquées.\nFutbin utilise Cloudflare — essaie dans quelques minutes.", inline=False)
    await interaction.followup.send(embed=result_embed)

@tree.command(name="stats", description="📊 Statistiques")
async def cmd_stats(interaction: discord.Interaction):
    embed = discord.Embed(title="📊 FC26 Trading Bot — Stats", color=0x5865F2, timestamp=datetime.utcnow())
    embed.add_field(name="🔔 Alertes", value=str(stats.get("total_sent",0)), inline=True)
    embed.add_field(name="🔍 Scans", value=str(stats.get("scans",0)), inline=True)
    embed.add_field(name="❌ Erreurs", value=str(stats.get("errors",0)), inline=True)
    embed.add_field(name="📊 Dernière source", value=SOURCE_EMOJI.get(stats.get("last_source",""),"—"), inline=True)
    embed.add_field(name="💰 Profit affiché", value=f"{fmt(stats.get('total_profit',0))} coins", inline=True)
    await interaction.response.send_message(embed=embed)

@tree.command(name="aide", description="❓ Guide complet")
async def cmd_aide(interaction: discord.Interaction):
    embed = discord.Embed(title="🤖 FC26 Trading Bot", description="Dominer le marché FC26 UT !", color=0x5865F2)
    embed.add_field(name="⚙️ Setup", value="`/plateforme` · `/setchannel`", inline=False)
    embed.add_field(name="🔍 Scans", value="`/scan` · `/snipe` · `/erreurs` · `/meta` · `/premium` · `/budget`", inline=False)
    embed.add_field(name="🎯 Filtres", value="`/position` · `/nation` · `/ligue` · `/joueur`", inline=False)
    embed.add_field(name="📡 Debug", value="`/status` — vérifie que le scraping fonctionne", inline=False)
    embed.add_field(name="💡 Astuce", value="1. `/plateforme PC`\n2. `/setchannel #alertes toutes`\n3. Les alertes arrivent automatiquement !", inline=False)
    await interaction.response.send_message(embed=embed)

# ─── Scan Automatique Permanent ───────────────────────────────────────────────
@tasks.loop(seconds=SCAN_DELAY)
async def auto_scan():
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
                sent = 0
                for p in players:
                    if sent >= MAX_ALERTS: break
                    key = f"{gid}_{platform}_{p['id']}_{p['snipe']}"
                    if key in sent_alerts: continue
                    await channel.send(embed=build_alert_embed(p, platform))
                    sent_alerts[key] = datetime.utcnow().isoformat()
                    stats["total_sent"]   = stats.get("total_sent",0) + 1
                    stats["total_profit"] = stats.get("total_profit",0) + p["profit"]
                    sent += 1
                    await asyncio.sleep(1.5)

                if len(sent_alerts) > 1000:
                    keys = list(sent_alerts.keys())
                    for k in keys[:-800]: del sent_alerts[k]

                _save("sent_alerts.json", sent_alerts)
                _save("stats.json", stats)

            except Exception as e:
                log.error(f"auto_scan [{platform}] {gid}: {e}")

        await asyncio.sleep(5)

@bot.event
async def on_ready():
    await tree.sync()
    auto_scan.start()
    log.info(f"✅ {bot.user} connecté — scan toutes les {SCAN_DELAY}s")
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="le marché FC26 💹"))

if __name__ == "__main__":
    bot.run(TOKEN)
