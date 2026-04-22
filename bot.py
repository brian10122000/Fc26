"""
╔══════════════════════════════════════════════════════════════════╗
║     FC26 ULTIMATE TRADING BOT — Playwright Edition              ║
║  Vrai navigateur Chrome → contourne Cloudflare à 100%           ║
║  Scan permanent · PC/Xbox/PS · Alertes instantanées             ║
╚══════════════════════════════════════════════════════════════════╝
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
from playwright.async_api import async_playwright, Browser, BrowserContext

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
TOKEN       = os.getenv("DISCORD_TOKEN", "VOTRE_TOKEN_ICI")
MIN_PROFIT  = 500
EA_TAX      = 0.05
SCAN_DELAY  = 60    # secondes entre chaque scan complet
MAX_ALERTS  = 8     # max alertes par scan

PLATFORMS = {
    "pc":          {"label": "PC",          "emoji": "🖥️",  "color": 0x5865F2},
    "xbox":        {"label": "Xbox",        "emoji": "🟢",  "color": 0x107C10},
    "playstation": {"label": "PlayStation", "emoji": "🔵",  "color": 0x003791},
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
stats       = _load("stats.json", {
    "total_sent": 0, "total_profit": 0,
    "scans": 0, "errors": 0,
    "last_scan": "jamais", "last_source": "—"
})

# ─── Browser global (réutilisé entre scans) ───────────────────────────────────
_playwright = None
_browser: Optional[Browser] = None

async def get_browser() -> Browser:
    global _playwright, _browser
    if _browser and _browser.is_connected():
        return _browser
    log.info("🚀 Lancement du navigateur Chrome...")
    _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--disable-web-security",
            "--disable-features=VizDisplayCompositor",
            "--window-size=1920,1080",
        ]
    )
    log.info("✅ Navigateur Chrome prêt")
    return _browser

async def new_context() -> BrowserContext:
    browser = await get_browser()
    ctx = await browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent=random.choice([
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        ]),
        locale="fr-FR",
        timezone_id="Europe/Paris",
        extra_http_headers={
            "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8",
        }
    )
    # Masque le fait que c'est Playwright
    await ctx.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
        Object.defineProperty(navigator, 'languages', { get: () => ['fr-FR','fr','en-US','en'] });
        window.chrome = { runtime: {} };
    """)
    return ctx

# ─── Helpers ──────────────────────────────────────────────────────────────────
def fmt(n: int) -> str:
    if n >= 1_000_000: return f"{n/1_000_000:.2f}M"
    if n >= 1_000:     return f"{n/1_000:.1f}K"
    return str(n)

def parse_price(v) -> int:
    if not v or str(v).strip() in ("N/A","—","-","","0"): return 0
    t = str(v).upper().replace(",","").replace(" ","").replace("\xa0","").replace("'","").strip()
    try:
        if "M" in t: return int(float(t.replace("M","")) * 1_000_000)
        if "K" in t: return int(float(t.replace("K","")) * 1_000)
        digits = re.sub(r"[^\d]","",t)
        return int(digits) if digits else 0
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

def get_user_platform(uid) -> str:
    return user_prefs.get(str(uid), {}).get("platform", "pc")

# ─── SCRAPING PLAYWRIGHT ──────────────────────────────────────────────────────

PLATFORM_SORT = {
    "pc":          "pc_price",
    "xbox":        "xbox_price",
    "playstation": "ps_price",
}

async def scrape_futbin(filters: dict = None, platform: str = "pc") -> list[dict]:
    """
    Scrape Futbin avec Playwright (vrai Chrome).
    Contourne Cloudflare à 100%.
    """
    if filters is None: filters = {}
    sort_key = PLATFORM_SORT.get(platform, "pc_price")

    # Construction URL avec filtres
    params = []
    params.append(f"sort={sort_key}")
    params.append("order=asc")
    params.append("per_page=60")
    if filters.get("position"):   params.append(f"position={filters['position']}")
    if filters.get("nation"):     params.append(f"nation={filters['nation']}")
    if filters.get("league"):     params.append(f"league={filters['league']}")
    if filters.get("min_rating"): params.append(f"min_rating={filters['min_rating']}")
    if filters.get("max_rating"): params.append(f"max_rating={filters['max_rating']}")
    if filters.get("min_price"):  params.append(f"min_price={filters['min_price']}")
    if filters.get("max_price"):  params.append(f"max_price={filters['max_price']}")

    url = "https://www.futbin.com/25/players?" + "&".join(params)
    log.info(f"Scraping: {url}")

    ctx  = None
    page = None
    players = []

    try:
        ctx  = await new_context()
        page = await ctx.new_page()

        # Bloque les ressources inutiles pour aller plus vite
        await page.route("**/*.{png,jpg,jpeg,gif,svg,woff,woff2,ttf,mp4,mp3}", lambda r: r.abort())
        await page.route("**/ads/**", lambda r: r.abort())
        await page.route("**/analytics**", lambda r: r.abort())
        await page.route("**/google-analytics**", lambda r: r.abort())

        # Navigation avec timeout généreux
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # Attend que le tableau soit chargé
        try:
            await page.wait_for_selector("table#repTb, .players-table, #repTb", timeout=15000)
        except:
            # Parfois le contenu est dans un autre sélecteur
            await page.wait_for_timeout(4000)

        # Scroll pour charger le contenu dynamique
        await page.evaluate("window.scrollTo(0, 500)")
        await page.wait_for_timeout(1500)

        # Extraction des données via JavaScript dans la page
        raw_data = await page.evaluate("""
        () => {
            const results = [];
            
            // Méthode 1 : Tableau principal Futbin
            const rows = document.querySelectorAll('table#repTb tbody tr, #repTb tbody tr');
            rows.forEach(row => {
                try {
                    const cols = row.querySelectorAll('td');
                    if (cols.length < 6) return;
                    
                    const linkEl  = row.querySelector('a[href*="/player/"]');
                    const nameEl  = row.querySelector('.player-name, .pname, td:nth-child(1) a, .player_name');
                    const imgEl   = row.querySelector('img');
                    const ratingEl= cols[1];
                    const posEl   = cols[2];
                    
                    // Récupère tous les prix dans la ligne
                    const prices = [];
                    cols.forEach(col => {
                        const text = col.innerText.trim();
                        const cleaned = text.replace(/,/g,'').replace(/K/i,'000').replace(/M/i,'000000');
                        const n = parseInt(cleaned);
                        if (n > 200) prices.push(n);
                    });
                    
                    if (prices.length < 2) return;
                    
                    const href = linkEl ? linkEl.getAttribute('href') : '';
                    const pidMatch = href.match(/\\/player\\/(\\d+)/);
                    
                    results.push({
                        name:     nameEl ? nameEl.innerText.trim() : '?',
                        rating:   ratingEl ? parseInt(ratingEl.innerText) || 0 : 0,
                        position: posEl ? posEl.innerText.trim().toUpperCase() : '?',
                        price1:   prices[0],
                        price2:   prices[1],
                        href:     href,
                        pid:      pidMatch ? pidMatch[1] : '',
                        imgSrc:   imgEl ? (imgEl.src || imgEl.dataset.src || '') : '',
                    });
                } catch(e) {}
            });
            
            // Méthode 2 : Cards (layout alternatif)
            if (results.length === 0) {
                const cards = document.querySelectorAll('.player-card-container, .player-item, [class*="player-row"]');
                cards.forEach(card => {
                    try {
                        const nameEl   = card.querySelector('[class*="name"], h3, h4');
                        const ratingEl = card.querySelector('[class*="rating"], [class*="ovr"]');
                        const posEl    = card.querySelector('[class*="position"], [class*="pos"]');
                        const priceEls = card.querySelectorAll('[class*="price"], [data-price]');
                        const imgEl    = card.querySelector('img');
                        const linkEl   = card.querySelector('a');
                        
                        const prices = [];
                        priceEls.forEach(el => {
                            const n = parseInt(el.innerText.replace(/[^\\d]/g,''));
                            if (n > 200) prices.push(n);
                        });
                        if (prices.length < 1) return;
                        
                        const href = linkEl ? linkEl.getAttribute('href') : '';
                        const pidMatch = href ? href.match(/\\/(\\d+)/) : null;
                        
                        results.push({
                            name:     nameEl ? nameEl.innerText.trim() : '?',
                            rating:   ratingEl ? parseInt(ratingEl.innerText) || 0 : 0,
                            position: posEl ? posEl.innerText.trim().toUpperCase() : '?',
                            price1:   prices[0],
                            price2:   prices[1] || prices[0],
                            href:     href,
                            pid:      pidMatch ? pidMatch[1] : '',
                            imgSrc:   imgEl ? imgEl.src : '',
                        });
                    } catch(e) {}
                });
            }
            
            return results;
        }
        """)

        log.info(f"Playwright raw: {len(raw_data)} lignes extraites")

        for row in raw_data:
            try:
                market = row.get("price1", 0)
                snipe  = row.get("price2", 0)

                # Logique prix : le snipe doit être <= market
                if snipe > market:
                    market, snipe = snipe, market
                if market < 500 or snipe <= 0:
                    snipe = market

                c = calc(snipe, market)
                if c["profit"] < MIN_PROFIT:
                    continue

                pid     = row.get("pid","")
                img_src = row.get("imgSrc","")
                if not img_src or "http" not in img_src:
                    img_src = f"https://cdn.futbin.com/content/fifa25/img/players/{pid}.png"

                href = row.get("href","")
                if href and not href.startswith("http"):
                    href = "https://www.futbin.com" + href

                players.append({
                    "id":       pid,
                    "name":     row.get("name","?"),
                    "rating":   row.get("rating", 0),
                    "position": row.get("position","?"),
                    "club":     "—", "nation": "—", "league": "—",
                    "snipe":    snipe,
                    "market":   market,
                    "image":    img_src,
                    "url":      href or "https://www.futbin.com/25/players",
                    "source":   "playwright",
                    **c,
                })
            except Exception as e:
                log.debug(f"player parse: {e}")

        log.info(f"✅ Playwright: {len(players)} joueurs avec profit >= {MIN_PROFIT}")
        stats["last_source"] = "playwright_futbin"

    except Exception as e:
        log.error(f"scrape_futbin error: {e}")
        stats["errors"] = stats.get("errors", 0) + 1
    finally:
        if page:
            try: await page.close()
            except: pass
        if ctx:
            try: await ctx.close()
            except: pass

    # Filtres finaux
    min_p     = filters.get("min_profit", MIN_PROFIT)
    min_disc  = filters.get("min_discount", 0)
    max_disc  = filters.get("max_discount", 100)

    players = [p for p in players
               if p["profit"] >= min_p
               and p["discount"] >= min_disc
               and p["discount"] <= max_disc]

    players.sort(key=lambda x: x["profit"], reverse=True)
    return players[:25]

# ─── Bot Setup ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot  = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ─── Embeds ───────────────────────────────────────────────────────────────────
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
    embed.add_field(name="🔗 Page joueur",  value=f"[Voir sur Futbin]({p['url']})", inline=False)
    if p.get("image") and p["image"].startswith("http"):
        embed.set_thumbnail(url=p["image"])
    embed.set_footer(text=f"FC26 Bot · Taxe EA 5% incluse · {datetime.utcnow().strftime('%H:%M:%S')}")
    return embed

def build_summary_embed(players: list, platform: str, title: str, desc: str = "") -> discord.Embed:
    plat  = PLATFORMS[platform]
    embed = discord.Embed(
        title=f"{plat['emoji']} {title} — {len(players)} opportunité(s)",
        description=desc or "Meilleures affaires trouvées",
        color=plat["color"], timestamp=datetime.utcnow()
    )
    if players:
        lines = []
        for p in players[:10]:
            dot = "🟢" if p["roi"] > 20 else ("🟡" if p["roi"] > 10 else "🔴")
            lines.append(f"{dot} **{p['name']}** {p['rating']} {p['position']} · 🎯{fmt(p['snipe'])} → +{fmt(p['profit'])} ({p['roi']}%)")
        embed.add_field(name="📋 Résultats", value="\n".join(lines), inline=False)
        best = players[0]
        embed.add_field(
            name="🏆 Meilleure affaire",
            value=f"**{best['name']}** — Snipe {fmt(best['snipe'])} · +**{fmt(best['profit'])}** coins ({best['roi']}% ROI)",
            inline=False
        )
    else:
        embed.add_field(name="😔 Aucun résultat",
            value="Aucune affaire avec ces critères.\nEssaie `/status` pour vérifier le scraping.", inline=False)
    embed.set_footer(text="FC26 Bot · Source: Futbin via Chrome · Taxe EA 5%")
    return embed

# ─── Commandes ────────────────────────────────────────────────────────────────
PLAT_CHOICES = [
    app_commands.Choice(name="🖥️ PC",         value="pc"),
    app_commands.Choice(name="🟢 Xbox",        value="xbox"),
    app_commands.Choice(name="🔵 PlayStation", value="playstation"),
]

@tree.command(name="plateforme", description="🎮 Choisis ta plateforme")
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

@tree.command(name="setchannel", description="📢 Salon pour les alertes automatiques")
@app_commands.choices(plateforme=[*PLAT_CHOICES, app_commands.Choice(name="🌐 Toutes", value="all")])
async def cmd_setchannel(interaction: discord.Interaction, salon: discord.TextChannel, plateforme: str = "all"):
    gid = str(interaction.guild_id)
    channels.setdefault(gid, {})
    for p in (list(PLATFORMS.keys()) if plateforme == "all" else [plateforme]):
        channels[gid][p] = salon.id
    _save("channels.json", channels)
    embed = discord.Embed(title="✅ Salon configuré !", color=0x57F287,
        description=f"Alertes → {salon.mention}\n⚡ Scan permanent via Chrome · Cloudflare contourné !")
    await interaction.response.send_message(embed=embed)

@tree.command(name="scan", description="🔍 Scan immédiat")
@app_commands.choices(plateforme=PLAT_CHOICES)
async def cmd_scan(interaction: discord.Interaction, plateforme: str = None):
    await interaction.response.defer()
    plat    = plateforme or get_user_platform(interaction.user.id)
    players = await scrape_futbin(platform=plat)
    await interaction.followup.send(embed=build_summary_embed(players, plat, "🔍 Scan Général"))
    for p in players[:3]:
        await interaction.followup.send(embed=build_alert_embed(p, plat))

@tree.command(name="snipe", description="⚡ Affaires dans ton budget")
@app_commands.choices(plateforme=PLAT_CHOICES)
async def cmd_snipe(interaction: discord.Interaction, budget: int = 50000, plateforme: str = None):
    await interaction.response.defer()
    plat    = plateforme or get_user_platform(interaction.user.id)
    players = await scrape_futbin({"max_price": budget}, plat)
    await interaction.followup.send(embed=build_summary_embed(players, plat, "⚡ Snipe", f"Budget: {fmt(budget)}"))
    for p in players[:3]: await interaction.followup.send(embed=build_alert_embed(p, plat))

@tree.command(name="erreurs", description="🚨 Cartes massivement sous-évaluées")
@app_commands.choices(plateforme=PLAT_CHOICES)
async def cmd_erreurs(interaction: discord.Interaction, reduction: int = 20, plateforme: str = None):
    await interaction.response.defer()
    plat    = plateforme or get_user_platform(interaction.user.id)
    players = await scrape_futbin({"min_discount": reduction}, plat)
    await interaction.followup.send(embed=build_summary_embed(players, plat, "🚨 Erreurs de Prix", f"-{reduction}% min"))
    for p in players[:5]: await interaction.followup.send(embed=build_alert_embed(p, plat))

@tree.command(name="position", description="📍 Par position")
@app_commands.choices(pos=[app_commands.Choice(name=p, value=p) for p in POSITIONS], plateforme=PLAT_CHOICES)
async def cmd_position(interaction: discord.Interaction, pos: str, min_profit: int = 500, plateforme: str = None):
    await interaction.response.defer()
    plat    = plateforme or get_user_platform(interaction.user.id)
    players = await scrape_futbin({"position": pos, "min_profit": min_profit}, plat)
    await interaction.followup.send(embed=build_summary_embed(players, plat, f"📍 {pos}"))
    for p in players[:3]: await interaction.followup.send(embed=build_alert_embed(p, plat))

@tree.command(name="nation", description="🌍 Par nationalité")
@app_commands.choices(plateforme=PLAT_CHOICES)
async def cmd_nation(interaction: discord.Interaction, nation: str, min_profit: int = 500, plateforme: str = None):
    await interaction.response.defer()
    plat    = plateforme or get_user_platform(interaction.user.id)
    players = await scrape_futbin({"nation": nation, "min_profit": min_profit}, plat)
    await interaction.followup.send(embed=build_summary_embed(players, plat, f"🌍 {nation}"))
    for p in players[:3]: await interaction.followup.send(embed=build_alert_embed(p, plat))

@tree.command(name="ligue", description="🏆 Par ligue")
@app_commands.choices(plateforme=PLAT_CHOICES)
async def cmd_ligue(interaction: discord.Interaction, ligue: str, min_profit: int = 500, plateforme: str = None):
    await interaction.response.defer()
    plat    = plateforme or get_user_platform(interaction.user.id)
    players = await scrape_futbin({"league": ligue, "min_profit": min_profit}, plat)
    await interaction.followup.send(embed=build_summary_embed(players, plat, f"🏆 {ligue}"))
    for p in players[:3]: await interaction.followup.send(embed=build_alert_embed(p, plat))

@tree.command(name="meta", description="🔥 Top cartes méta")
@app_commands.choices(plateforme=PLAT_CHOICES)
async def cmd_meta(interaction: discord.Interaction, min_note: int = 87, plateforme: str = None):
    await interaction.response.defer()
    plat    = plateforme or get_user_platform(interaction.user.id)
    players = await scrape_futbin({"min_rating": min_note}, plat)
    await interaction.followup.send(embed=build_summary_embed(players, plat, f"🔥 Meta {min_note}+"))
    for p in players[:3]: await interaction.followup.send(embed=build_alert_embed(p, plat))

@tree.command(name="premium", description="💎 Grosses cartes")
@app_commands.choices(plateforme=PLAT_CHOICES)
async def cmd_premium(interaction: discord.Interaction, min_prix: int = 50000, min_profit: int = 5000, plateforme: str = None):
    await interaction.response.defer()
    plat    = plateforme or get_user_platform(interaction.user.id)
    players = await scrape_futbin({"min_price": min_prix, "min_profit": min_profit}, plat)
    await interaction.followup.send(embed=build_summary_embed(players, plat, "💎 Premium"))
    for p in players[:3]: await interaction.followup.send(embed=build_alert_embed(p, plat))

@tree.command(name="budget", description="💰 Petit budget")
@app_commands.choices(plateforme=PLAT_CHOICES)
async def cmd_budget(interaction: discord.Interaction, max_prix: int = 10000, plateforme: str = None):
    await interaction.response.defer()
    plat    = plateforme or get_user_platform(interaction.user.id)
    players = await scrape_futbin({"max_price": max_prix}, plat)
    await interaction.followup.send(embed=build_summary_embed(players, plat, "💰 Budget"))
    for p in players[:5]: await interaction.followup.send(embed=build_alert_embed(p, plat))

@tree.command(name="status", description="📡 Vérifie que le scraping fonctionne")
async def cmd_status(interaction: discord.Interaction):
    await interaction.response.defer()
    embed = discord.Embed(title="📡 Test du Scraping en live...", color=0xFEE75C, timestamp=datetime.utcnow())
    embed.add_field(name="🔄 Scans", value=str(stats.get("scans",0)), inline=True)
    embed.add_field(name="🔔 Alertes", value=str(stats.get("total_sent",0)), inline=True)
    embed.add_field(name="❌ Erreurs", value=str(stats.get("errors",0)), inline=True)
    embed.add_field(name="🕐 Dernier scan", value=stats.get("last_scan","jamais"), inline=True)
    embed.add_field(name="🌐 Source", value=stats.get("last_source","—"), inline=True)
    await interaction.followup.send(embed=embed)

    # Test live
    t0 = datetime.utcnow()
    players = await scrape_futbin(platform="pc")
    elapsed = (datetime.utcnow() - t0).seconds

    result = discord.Embed(timestamp=datetime.utcnow())
    if players:
        result.title = f"✅ Scraping OK — {len(players)} joueurs trouvés en {elapsed}s"
        result.color = 0x57F287
        lines = [f"• **{p['name']}** {p['rating']} {p['position']} — Snipe {fmt(p['snipe'])} · +{fmt(p['profit'])} coins" for p in players[:5]]
        result.add_field(name="Top 5 affaires PC", value="\n".join(lines), inline=False)
    else:
        result.title = "❌ Scraping échoué"
        result.color = 0xED4245
        result.add_field(name="Cause probable", value=(
            "• Futbin chargé / maintenance\n"
            "• Playwright pas encore initialisé\n"
            "• Réessaie dans 1 minute"
        ), inline=False)
    await interaction.followup.send(embed=result)

@tree.command(name="stats", description="📊 Statistiques")
async def cmd_stats(interaction: discord.Interaction):
    embed = discord.Embed(title="📊 FC26 Trading Bot — Stats", color=0x5865F2, timestamp=datetime.utcnow())
    embed.add_field(name="🔔 Alertes envoyées", value=str(stats.get("total_sent",0)), inline=True)
    embed.add_field(name="🔍 Scans", value=str(stats.get("scans",0)), inline=True)
    embed.add_field(name="❌ Erreurs", value=str(stats.get("errors",0)), inline=True)
    embed.add_field(name="💰 Profit total affiché", value=f"{fmt(stats.get('total_profit',0))} coins", inline=True)
    embed.add_field(name="🕐 Dernier scan", value=stats.get("last_scan","jamais"), inline=True)
    await interaction.response.send_message(embed=embed)

@tree.command(name="aide", description="❓ Guide complet")
async def cmd_aide(interaction: discord.Interaction):
    embed = discord.Embed(title="🤖 FC26 Ultimate Trading Bot", color=0x5865F2,
        description="Scan Futbin avec un vrai Chrome — Cloudflare contourné !")
    embed.add_field(name="⚙️ Setup", value="`/plateforme` · `/setchannel #salon`", inline=False)
    embed.add_field(name="🔍 Scans", value="`/scan` · `/snipe [budget]` · `/erreurs [%]`\n`/meta [note]` · `/premium` · `/budget [max]`", inline=False)
    embed.add_field(name="🎯 Filtres", value="`/position ST` · `/nation France` · `/ligue Premier League`", inline=False)
    embed.add_field(name="📡 Debug", value="`/status` — test live du scraping", inline=False)
    embed.add_field(name="💡 Démarrage rapide", value="1. `/plateforme PC`\n2. `/setchannel #alertes toutes`\n3. Profit automatique ! 💰", inline=False)
    await interaction.response.send_message(embed=embed)

# ─── Scan automatique permanent ───────────────────────────────────────────────
@tasks.loop(seconds=SCAN_DELAY)
async def auto_scan():
    stats["scans"] = stats.get("scans", 0) + 1
    stats["last_scan"] = datetime.utcnow().strftime("%H:%M:%S")

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
                players = await scrape_futbin(platform=platform)
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
                    await asyncio.sleep(2)

                # Nettoyage anti-mémoire
                if len(sent_alerts) > 1000:
                    keys = list(sent_alerts.keys())
                    for k in keys[:-800]: del sent_alerts[k]

                _save("sent_alerts.json", sent_alerts)
                _save("stats.json", stats)

            except Exception as e:
                log.error(f"auto_scan [{platform}] {gid}: {e}")

        await asyncio.sleep(8)

@bot.event
async def on_ready():
    log.info(f"Connecté : {bot.user}")

    # ── FORCE SYNC COMMANDES sur chaque serveur ──────────────────────────────
    # Supprime d'abord toutes les anciennes commandes globales
    tree.clear_commands(guild=None)
    await tree.sync()
    log.info("🗑️ Anciennes commandes globales effacées")

    # Re-sync sur chaque serveur individuellement (instantané, pas de délai)
    for guild in bot.guilds:
        try:
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
            log.info(f"✅ {len(synced)} commandes sync sur : {guild.name}")
        except Exception as e:
            log.warning(f"Sync guild {guild.name}: {e}")

    # Sync global aussi (peut prendre 1h pour Discord)
    await tree.sync()
    log.info("✅ Sync global envoyé")

    # ── Pré-chauffe Chrome ───────────────────────────────────────────────────
    log.info("🚀 Initialisation Chrome...")
    try:
        await get_browser()
        log.info("✅ Chrome prêt !")
    except Exception as e:
        log.error(f"Chrome init error: {e}")

    auto_scan.start()
    log.info(f"🤖 Bot prêt — Scan toutes les {SCAN_DELAY}s")
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="le marché FC26 💹")
    )

if __name__ == "__main__":
    bot.run(TOKEN)
