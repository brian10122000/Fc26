"""
FC26 ULTIMATE TRADING BOT
curl-cffi = fingerprint TLS identique à Chrome → Cloudflare contourné sans navigateur
"""

import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import json
import os
import re
import logging
import random
from datetime import datetime
from typing import Optional
from bs4 import BeautifulSoup

# curl_cffi imite le TLS de Chrome → contourne Cloudflare
try:
    from curl_cffi.requests import AsyncSession
    CURL_OK = True
    log_curl = "✅ curl-cffi disponible (mode Chrome TLS)"
except ImportError:
    import aiohttp
    CURL_OK = False
    log_curl = "⚠️ curl-cffi absent, fallback aiohttp"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
log = logging.getLogger(__name__)
log.info(log_curl)

TOKEN      = os.getenv("DISCORD_TOKEN", "VOTRE_TOKEN_ICI")
MIN_PROFIT = 500
EA_TAX     = 0.05
SCAN_DELAY = 60
MAX_ALERTS = 8

PLATFORMS = {
    "pc":          {"label": "PC",          "emoji": "🖥️",  "color": 0x5865F2, "col": 0},
    "xbox":        {"label": "Xbox",        "emoji": "🟢",  "color": 0x107C10, "col": 1},
    "playstation": {"label": "PlayStation", "emoji": "🔵",  "color": 0x003791, "col": 2},
}
POSITIONS = ["ST","CF","CAM","CM","CDM","LW","RW","LB","RB","CB","GK","LM","RM","LWB","RWB"]

def _load(p, d):
    if os.path.exists(p):
        with open(p) as f: return json.load(f)
    return d

def _save(p, d):
    with open(p, "w") as f: json.dump(d, f, indent=2, ensure_ascii=False)

user_prefs  = _load("user_prefs.json", {})
channels    = _load("channels.json", {})
sent_alerts = _load("sent_alerts.json", {})
stats       = _load("stats.json", {"sent":0,"profit":0,"scans":0,"errors":0,"last":"—","source":"—"})

def fmt(n):
    if n >= 1_000_000: return f"{n/1_000_000:.2f}M"
    if n >= 1_000:     return f"{n/1_000:.1f}K"
    return str(n)

def parse_price(v):
    if not v: return 0
    t = str(v).upper().replace(",","").replace(" ","").replace("\xa0","").strip()
    try:
        if "M" in t: return int(float(t.replace("M",""))*1_000_000)
        if "K" in t: return int(float(t.replace("K",""))*1_000)
        return int(re.sub(r"[^\d]","",t) or 0)
    except: return 0

def calc(snipe, market):
    sell = int(market*(1-EA_TAX))
    profit = sell-snipe
    roi = round(profit/snipe*100,1) if snipe>0 else 0
    disc = round((market-snipe)/market*100,1) if market>0 else 0
    return {"sell":sell,"profit":profit,"roi":roi,"discount":disc}

def roi_bar(roi):
    f = min(int(roi/4),12)
    return "█"*f+"░"*(12-f)

def get_plat(uid):
    return user_prefs.get(str(uid),{}).get("platform","pc")

# ── REQUÊTE CHROME TLS ────────────────────────────────────────────────────────
IMPERSONATE_LIST = ["chrome110","chrome107","chrome104","chrome101","chrome100","chrome99"]

async def fetch_url(url: str, params: dict = None) -> Optional[str]:
    """Requête avec fingerprint TLS Chrome via curl-cffi."""
    full_url = url
    if params:
        qs = "&".join(f"{k}={v}" for k,v in params.items())
        full_url = f"{url}?{qs}"

    if CURL_OK:
        try:
            impersonate = random.choice(IMPERSONATE_LIST)
            async with AsyncSession() as s:
                r = await s.get(
                    full_url,
                    impersonate=impersonate,
                    headers={
                        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8",
                        "Referer": "https://www.futbin.com/",
                    },
                    timeout=20,
                )
                log.info(f"curl-cffi [{impersonate}] HTTP {r.status_code} — {full_url[:80]}")
                if r.status_code == 200:
                    return r.text
                return None
        except Exception as e:
            log.error(f"curl-cffi error: {e}")

    # Fallback aiohttp classique
    try:
        import aiohttp
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "fr-FR,fr;q=0.9",
            "Referer": "https://www.futbin.com/",
        }
        async with aiohttp.ClientSession() as s:
            async with s.get(full_url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as r:
                log.info(f"aiohttp HTTP {r.status} — {full_url[:80]}")
                if r.status == 200:
                    return await r.text()
    except Exception as e:
        log.error(f"aiohttp error: {e}")

    return None

# ── SCRAPING FUTBIN ────────────────────────────────────────────────────────────
async def scrape_futbin(filters: dict = None, platform: str = "pc") -> list[dict]:
    if filters is None: filters = {}
    plat_col = PLATFORMS[platform]["col"]

    sort_map = {"pc": "pc_price", "xbox": "xbox_price", "playstation": "ps_price"}
    params = {
        "sort":     sort_map[platform],
        "order":    "asc",
        "per_page": "60",
        "page":     "1",
    }
    if filters.get("position"):   params["position"]   = filters["position"]
    if filters.get("nation"):     params["nation"]      = filters["nation"]
    if filters.get("league"):     params["league"]      = filters["league"]
    if filters.get("min_rating"): params["min_rating"]  = str(filters["min_rating"])
    if filters.get("max_rating"): params["max_rating"]  = str(filters["max_rating"])
    if filters.get("min_price"):  params["min_price"]   = str(filters["min_price"])
    if filters.get("max_price"):  params["max_price"]   = str(filters["max_price"])

    html = await fetch_url("https://www.futbin.com/25/players", params)
    if not html:
        log.warning("Futbin: aucune réponse")
        stats["errors"] = stats.get("errors",0)+1
        _save("stats.json", stats)
        return []

    # Vérifie si Cloudflare a bloqué
    if "Just a moment" in html or "cf-browser-verification" in html or "Enable JavaScript" in html:
        log.warning("⚠️ Cloudflare détecté malgré curl-cffi")
        stats["errors"] = stats.get("errors",0)+1
        _save("stats.json", stats)
        return []

    soup    = BeautifulSoup(html, "html.parser")
    players = []

    # Tentative 1 : tableau #repTb
    rows = soup.select("table#repTb tbody tr, #repTb tbody tr")
    log.info(f"Futbin HTML: {len(rows)} lignes dans #repTb")

    # Tentative 2 : n'importe quel tableau
    if not rows:
        rows = soup.select("table tbody tr")
        log.info(f"Futbin HTML fallback: {len(rows)} lignes dans tables")

    for row in rows[:60]:
        try:
            cols = row.select("td")
            if len(cols) < 5: continue

            link_el  = row.select_one("a[href*='/player/']")
            name_el  = row.select_one(".player-name,.pname,td:nth-child(1) a")
            img_el   = row.select_one("img[src*='players'],img[data-src*='players']")
            rat_el   = cols[1] if len(cols)>1 else None
            pos_el   = cols[2] if len(cols)>2 else None

            # Extrait tous les prix de la ligne
            all_prices = []
            for col in cols:
                txt = col.get_text(strip=True)
                p   = parse_price(txt)
                if p > 300:
                    all_prices.append(p)

            if len(all_prices) < 2: continue

            # Le prix marché = plus grand, snipe = plus petit
            all_prices.sort()
            snipe  = all_prices[0]
            market = all_prices[-1]

            if market < 500 or snipe <= 0: continue
            if snipe > market: snipe = market

            c = calc(snipe, market)
            if c["profit"] < MIN_PROFIT: continue

            href = link_el.get("href","") if link_el else ""
            pid  = re.search(r"/player/(\d+)", href)
            pid  = pid.group(1) if pid else str(random.randint(10000,99999))

            img = ""
            if img_el:
                img = img_el.get("src") or img_el.get("data-src","")
            if not img or not img.startswith("http"):
                img = f"https://cdn.futbin.com/content/fifa25/img/players/{pid}.png"

            players.append({
                "id":       pid,
                "name":     name_el.get_text(strip=True) if name_el else "Joueur",
                "rating":   int(re.sub(r"\D","",rat_el.get_text()) or "0") if rat_el else 0,
                "position": pos_el.get_text(strip=True).upper() if pos_el else "?",
                "club": "—","nation": "—","league": "—",
                "snipe":  snipe,
                "market": market,
                "image":  img,
                "url":    ("https://www.futbin.com"+href) if href else "https://www.futbin.com/25/players",
                "source": "curl_cffi" if CURL_OK else "aiohttp",
                **c,
            })
        except Exception as e:
            log.debug(f"row: {e}")

    log.info(f"✅ {len(players)} joueurs avec profit ≥ {MIN_PROFIT} coins (source: {'curl-cffi' if CURL_OK else 'aiohttp'})")
    stats["source"] = "curl-cffi ✅" if CURL_OK else "aiohttp"
    stats["last"]   = datetime.utcnow().strftime("%H:%M:%S")

    # Filtres finaux
    mp   = filters.get("min_profit", MIN_PROFIT)
    mind = filters.get("min_discount", 0)
    maxd = filters.get("max_discount", 100)
    players = [p for p in players if p["profit"]>=mp and mind<=p["discount"]<=maxd]
    players.sort(key=lambda x: x["profit"], reverse=True)
    return players[:25]

# ── Bot ───────────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot  = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ── Embeds ────────────────────────────────────────────────────────────────────
def alert_embed(p, platform):
    pl    = PLATFORMS[platform]
    color = 0xED4245 if p["discount"]>=20 else (0xFF8C00 if p["discount"]>=12 else 0x57F287)
    fire  = "🚨🚨🚨" if p["discount"]>=25 else ("🚨🚨" if p["discount"]>=15 else "🚨")
    e = discord.Embed(title=f"{pl['emoji']} @{pl['label']}  ·  {fire}", url=p["url"], color=color, timestamp=datetime.utcnow())
    e.add_field(name=f"👤 {p['name']}  •  {p['rating']} {p['position']}", value=f"🌍 {p['nation']}  ·  🏆 {p['league']}  ·  ⚽ {p['club']}", inline=False)
    e.add_field(name="🎯 Snipe Price",  value=f"**{fmt(p['snipe'])}** 🪙",   inline=True)
    e.add_field(name="💵 Sells Price",  value=f"**{fmt(p['sell'])}** 🪙",    inline=True)
    e.add_field(name="💰 Profit",       value=f"**+{fmt(p['profit'])}** 🪙", inline=True)
    e.add_field(name="🏷️ Réduction",    value=f"**-{p['discount']}%**",      inline=True)
    e.add_field(name="📈 ROI",          value=f"**{p['roi']}%**",             inline=True)
    e.add_field(name="📊",              value=f"`{roi_bar(p['roi'])}`",       inline=True)
    e.add_field(name="🔗", value=f"[Voir sur Futbin]({p['url']})", inline=False)
    if p.get("image","").startswith("http"):
        e.set_thumbnail(url=p["image"])
    e.set_footer(text=f"FC26 Bot · Taxe EA 5% · {datetime.utcnow().strftime('%H:%M:%S')}")
    return e

def summary_embed(players, platform, title, desc=""):
    pl = PLATFORMS[platform]
    e  = discord.Embed(title=f"{pl['emoji']} {title} — {len(players)} opportunité(s)", description=desc or "Meilleures affaires", color=pl["color"], timestamp=datetime.utcnow())
    if players:
        lines = []
        for p in players[:10]:
            dot = "🟢" if p["roi"]>20 else ("🟡" if p["roi"]>10 else "🔴")
            lines.append(f"{dot} **{p['name']}** {p['rating']} {p['position']} · 🎯{fmt(p['snipe'])} → +{fmt(p['profit'])} ({p['roi']}%)")
        e.add_field(name="📋 Résultats", value="\n".join(lines), inline=False)
        best = players[0]
        e.add_field(name="🏆 Meilleure", value=f"**{best['name']}** — +**{fmt(best['profit'])}** coins ({best['roi']}% ROI)", inline=False)
    else:
        e.add_field(name="😔 Aucun résultat", value="Aucune affaire trouvée.\nUtilise `/status` pour vérifier.", inline=False)
    e.set_footer(text=f"FC26 Bot · {'curl-cffi ✅' if CURL_OK else 'aiohttp'} · Taxe EA 5%")
    return e

# ── Commandes ─────────────────────────────────────────────────────────────────
PC = [
    app_commands.Choice(name="🖥️ PC",         value="pc"),
    app_commands.Choice(name="🟢 Xbox",        value="xbox"),
    app_commands.Choice(name="🔵 PlayStation", value="playstation"),
]

@tree.command(name="plateforme", description="🎮 Choisis ta plateforme")
@app_commands.choices(plateforme=PC)
async def cmd_plateforme(interaction: discord.Interaction, plateforme: str):
    user_prefs.setdefault(str(interaction.user.id),{})["platform"] = plateforme
    _save("user_prefs.json", user_prefs)
    pl = PLATFORMS[plateforme]
    await interaction.response.send_message(embed=discord.Embed(title=f"✅ {pl['emoji']} {pl['label']}", color=pl["color"]), ephemeral=True)

@tree.command(name="setchannel", description="📢 Salon des alertes automatiques")
@app_commands.choices(plateforme=[*PC, app_commands.Choice(name="🌐 Toutes", value="all")])
async def cmd_setchannel(interaction: discord.Interaction, salon: discord.TextChannel, plateforme: str = "all"):
    gid = str(interaction.guild_id)
    channels.setdefault(gid, {})
    for p in (list(PLATFORMS.keys()) if plateforme=="all" else [plateforme]):
        channels[gid][p] = salon.id
    _save("channels.json", channels)
    e = discord.Embed(title="✅ Salon configuré !", color=0x57F287, description=f"Alertes → {salon.mention}\n⚡ Scan permanent · curl-cffi Chrome TLS !")
    await interaction.response.send_message(embed=e)

@tree.command(name="scan", description="🔍 Scan immédiat")
@app_commands.choices(plateforme=PC)
async def cmd_scan(interaction: discord.Interaction, plateforme: str = None):
    await interaction.response.defer()
    pl = plateforme or get_plat(interaction.user.id)
    players = await scrape_futbin(platform=pl)
    await interaction.followup.send(embed=summary_embed(players, pl, "🔍 Scan Général"))
    for p in players[:3]: await interaction.followup.send(embed=alert_embed(p, pl))

@tree.command(name="snipe", description="⚡ Affaires dans ton budget")
@app_commands.choices(plateforme=PC)
async def cmd_snipe(interaction: discord.Interaction, budget: int = 50000, plateforme: str = None):
    await interaction.response.defer()
    pl = plateforme or get_plat(interaction.user.id)
    players = await scrape_futbin({"max_price": budget}, pl)
    await interaction.followup.send(embed=summary_embed(players, pl, "⚡ Snipe", f"Budget: {fmt(budget)}"))
    for p in players[:3]: await interaction.followup.send(embed=alert_embed(p, pl))

@tree.command(name="erreurs", description="🚨 Erreurs de prix")
@app_commands.choices(plateforme=PC)
async def cmd_erreurs(interaction: discord.Interaction, reduction: int = 20, plateforme: str = None):
    await interaction.response.defer()
    pl = plateforme or get_plat(interaction.user.id)
    players = await scrape_futbin({"min_discount": reduction}, pl)
    await interaction.followup.send(embed=summary_embed(players, pl, "🚨 Erreurs de Prix", f"-{reduction}% min"))
    for p in players[:5]: await interaction.followup.send(embed=alert_embed(p, pl))

@tree.command(name="position", description="📍 Par position")
@app_commands.choices(pos=[app_commands.Choice(name=p, value=p) for p in POSITIONS], plateforme=PC)
async def cmd_position(interaction: discord.Interaction, pos: str, min_profit: int = 500, plateforme: str = None):
    await interaction.response.defer()
    pl = plateforme or get_plat(interaction.user.id)
    players = await scrape_futbin({"position": pos, "min_profit": min_profit}, pl)
    await interaction.followup.send(embed=summary_embed(players, pl, f"📍 {pos}"))
    for p in players[:3]: await interaction.followup.send(embed=alert_embed(p, pl))

@tree.command(name="nation", description="🌍 Par nationalité")
@app_commands.choices(plateforme=PC)
async def cmd_nation(interaction: discord.Interaction, nation: str, min_profit: int = 500, plateforme: str = None):
    await interaction.response.defer()
    pl = plateforme or get_plat(interaction.user.id)
    players = await scrape_futbin({"nation": nation, "min_profit": min_profit}, pl)
    await interaction.followup.send(embed=summary_embed(players, pl, f"🌍 {nation}"))
    for p in players[:3]: await interaction.followup.send(embed=alert_embed(p, pl))

@tree.command(name="ligue", description="🏆 Par ligue")
@app_commands.choices(plateforme=PC)
async def cmd_ligue(interaction: discord.Interaction, ligue: str, min_profit: int = 500, plateforme: str = None):
    await interaction.response.defer()
    pl = plateforme or get_plat(interaction.user.id)
    players = await scrape_futbin({"league": ligue, "min_profit": min_profit}, pl)
    await interaction.followup.send(embed=summary_embed(players, pl, f"🏆 {ligue}"))
    for p in players[:3]: await interaction.followup.send(embed=alert_embed(p, pl))

@tree.command(name="meta", description="🔥 Top cartes méta")
@app_commands.choices(plateforme=PC)
async def cmd_meta(interaction: discord.Interaction, min_note: int = 87, plateforme: str = None):
    await interaction.response.defer()
    pl = plateforme or get_plat(interaction.user.id)
    players = await scrape_futbin({"min_rating": min_note}, pl)
    await interaction.followup.send(embed=summary_embed(players, pl, f"🔥 Meta {min_note}+"))
    for p in players[:3]: await interaction.followup.send(embed=alert_embed(p, pl))

@tree.command(name="premium", description="💎 Grosses cartes")
@app_commands.choices(plateforme=PC)
async def cmd_premium(interaction: discord.Interaction, min_prix: int = 50000, min_profit: int = 5000, plateforme: str = None):
    await interaction.response.defer()
    pl = plateforme or get_plat(interaction.user.id)
    players = await scrape_futbin({"min_price": min_prix, "min_profit": min_profit}, pl)
    await interaction.followup.send(embed=summary_embed(players, pl, "💎 Premium"))
    for p in players[:3]: await interaction.followup.send(embed=alert_embed(p, pl))

@tree.command(name="budget", description="💰 Petit budget")
@app_commands.choices(plateforme=PC)
async def cmd_budget(interaction: discord.Interaction, max_prix: int = 10000, plateforme: str = None):
    await interaction.response.defer()
    pl = plateforme or get_plat(interaction.user.id)
    players = await scrape_futbin({"max_price": max_prix}, pl)
    await interaction.followup.send(embed=summary_embed(players, pl, "💰 Budget"))
    for p in players[:5]: await interaction.followup.send(embed=alert_embed(p, pl))

@tree.command(name="status", description="📡 Test live du scraping")
async def cmd_status(interaction: discord.Interaction):
    await interaction.response.defer()
    # Info statique
    e = discord.Embed(title="📡 Statut du Bot", color=0x5865F2, timestamp=datetime.utcnow())
    e.add_field(name="🔧 Mode scraping", value="curl-cffi Chrome TLS ✅" if CURL_OK else "aiohttp ⚠️", inline=True)
    e.add_field(name="🔍 Scans", value=str(stats.get("scans",0)), inline=True)
    e.add_field(name="🔔 Alertes", value=str(stats.get("sent",0)), inline=True)
    e.add_field(name="❌ Erreurs", value=str(stats.get("errors",0)), inline=True)
    e.add_field(name="🕐 Dernier scan", value=stats.get("last","jamais"), inline=True)
    await interaction.followup.send(embed=e)

    # Test live
    t0 = datetime.utcnow()
    players = await scrape_futbin(platform="pc")
    sec = (datetime.utcnow()-t0).seconds

    r = discord.Embed(timestamp=datetime.utcnow())
    if players:
        r.title  = f"✅ Scraping OK — {len(players)} joueurs en {sec}s"
        r.color  = 0x57F287
        lines = [f"• **{p['name']}** {p['rating']} {p['position']} — Snipe {fmt(p['snipe'])} · +{fmt(p['profit'])}" for p in players[:5]]
        r.add_field(name="Top 5 PC", value="\n".join(lines), inline=False)
    else:
        r.title = "❌ Scraping échoué"
        r.color = 0xED4245
        r.add_field(name="Cause", value="Futbin bloqué ou indisponible.\nRéessaie dans 1 minute.", inline=False)
    await interaction.followup.send(embed=r)

@tree.command(name="stats", description="📊 Statistiques")
async def cmd_stats(interaction: discord.Interaction):
    e = discord.Embed(title="📊 FC26 Bot — Stats", color=0x5865F2, timestamp=datetime.utcnow())
    e.add_field(name="🔔 Alertes", value=str(stats.get("sent",0)), inline=True)
    e.add_field(name="🔍 Scans", value=str(stats.get("scans",0)), inline=True)
    e.add_field(name="💰 Profit affiché", value=f"{fmt(stats.get('profit',0))} coins", inline=True)
    e.add_field(name="🔧 Source", value=stats.get("source","—"), inline=True)
    await interaction.response.send_message(embed=e)

@tree.command(name="aide", description="❓ Guide complet")
async def cmd_aide(interaction: discord.Interaction):
    e = discord.Embed(title="🤖 FC26 Ultimate Trading Bot", color=0x5865F2,
        description="Scan Futbin · curl-cffi Chrome TLS · Cloudflare contourné")
    e.add_field(name="⚙️ Setup", value="`/plateforme` · `/setchannel #salon`", inline=False)
    e.add_field(name="🔍 Scans", value="`/scan` · `/snipe [budget]` · `/erreurs [%]`\n`/meta [note]` · `/premium` · `/budget [max]`", inline=False)
    e.add_field(name="🎯 Filtres", value="`/position ST` · `/nation France` · `/ligue Premier League`", inline=False)
    e.add_field(name="📡 Debug", value="`/status` · `/stats`", inline=False)
    e.add_field(name="💡 Démarrage", value="1. `/plateforme PC`\n2. `/setchannel #alertes toutes`\n3. Profit automatique ! 💰", inline=False)
    await interaction.response.send_message(embed=e)

# ── Scan automatique ──────────────────────────────────────────────────────────
@tasks.loop(seconds=SCAN_DELAY)
async def auto_scan():
    stats["scans"] = stats.get("scans",0)+1
    stats["last"]  = datetime.utcnow().strftime("%H:%M:%S")
    for guild in bot.guilds:
        gid = str(guild.id)
        for platform, channel_id in channels.get(gid,{}).items():
            channel = bot.get_channel(channel_id)
            if not channel: continue
            try:
                players = await scrape_futbin(platform=platform)
                sent = 0
                for p in players:
                    if sent >= MAX_ALERTS: break
                    key = f"{gid}_{platform}_{p['id']}_{p['snipe']}"
                    if key in sent_alerts: continue
                    await channel.send(embed=alert_embed(p, platform))
                    sent_alerts[key] = datetime.utcnow().isoformat()
                    stats["sent"]   = stats.get("sent",0)+1
                    stats["profit"] = stats.get("profit",0)+p["profit"]
                    sent += 1
                    await asyncio.sleep(2)
                if len(sent_alerts) > 1000:
                    ks = list(sent_alerts.keys())
                    for k in ks[:-800]: del sent_alerts[k]
                _save("sent_alerts.json", sent_alerts)
                _save("stats.json", stats)
            except Exception as ex:
                log.error(f"auto_scan [{platform}]: {ex}")
        await asyncio.sleep(8)

@bot.event
async def on_ready():
    log.info(f"Connecté : {bot.user}")
    for guild in bot.guilds:
        try:
            synced = await tree.sync(guild=guild)
            log.info(f"✅ {len(synced)} commandes sync → {guild.name}")
        except Exception as ex:
            log.warning(f"sync guild: {ex}")
    try:
        g = await tree.sync()
        log.info(f"✅ {len(g)} commandes sync global")
    except Exception as ex:
        log.warning(f"sync global: {ex}")
    auto_scan.start()
    log.info(f"🤖 Prêt — scan toutes les {SCAN_DELAY}s | curl-cffi: {CURL_OK}")
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="le marché FC26 💹"))

if __name__ == "__main__":
    bot.run(TOKEN)
