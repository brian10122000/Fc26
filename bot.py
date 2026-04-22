"""
FC26 ULTIMATE TRADING BOT
Solution anti-Cloudflare : ScraperAPI (gratuit 1000 req/mois)
+ proxies publics rotatifs en fallback
"""

import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio, json, os, re, logging, random
from datetime import datetime
from typing import Optional
from bs4 import BeautifulSoup
import aiohttp

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()])
log = logging.getLogger(__name__)

TOKEN         = os.getenv("DISCORD_TOKEN", "VOTRE_TOKEN_ICI")
SCRAPER_KEY   = os.getenv("SCRAPER_API_KEY", "")   # Clé ScraperAPI (gratuit)
MIN_PROFIT    = 500
EA_TAX        = 0.05
SCAN_DELAY    = 120
MAX_ALERTS    = 6

PLATFORMS = {
    "pc":          {"label": "PC",          "emoji": "🖥️", "color": 0x5865F2},
    "xbox":        {"label": "Xbox",        "emoji": "🟢", "color": 0x107C10},
    "playstation": {"label": "PlayStation", "emoji": "🔵", "color": 0x003791},
}
POSITIONS = ["ST","CF","CAM","CM","CDM","LW","RW","LB","RB","CB","GK","LM","RM","LWB","RWB"]

def _load(p, d): return json.load(open(p)) if os.path.exists(p) else d
def _save(p, d): json.dump(d, open(p,"w"), indent=2, ensure_ascii=False)

user_prefs  = _load("user_prefs.json", {})
channels    = _load("channels.json", {})
sent_alerts = _load("sent_alerts.json", {})
stats       = _load("stats.json", {"sent":0,"profit":0,"scans":0,"errors":0,"last":"—","source":"—","proxy_ok":False})

def fmt(n):
    if n>=1_000_000: return f"{n/1_000_000:.2f}M"
    if n>=1_000: return f"{n/1_000:.1f}K"
    return str(n)

def parse_price(v):
    if not v: return 0
    t = str(v).upper().strip()
    if t in ("FREE","N/A","—","-","","0","NO PRICE","NULL","NONE","UNTRADEABLE"): return 0
    t = t.replace(",","").replace(" ","").replace("\xa0","").replace("'","")
    try:
        if "M" in t: return int(float(t.replace("M",""))*1_000_000)
        if "K" in t: return int(float(t.replace("K",""))*1_000)
        val = int(re.sub(r"[^\d]","",t) or 0)
        return val if 200<=val<=50_000_000 else 0
    except: return 0

def calc(snipe, market):
    sell=int(market*(1-EA_TAX)); profit=sell-snipe
    roi=round(profit/snipe*100,1) if snipe>0 else 0
    disc=round((market-snipe)/market*100,1) if market>0 else 0
    return {"sell":sell,"profit":profit,"roi":roi,"discount":disc}

def roi_bar(roi):
    f=min(int(roi/4),12); return "█"*f+"░"*(12-f)

def get_plat(uid): return user_prefs.get(str(uid),{}).get("platform","pc")

# ── FETCH via ScraperAPI (résout Cloudflare) ──────────────────────────────────
async def fetch_via_scraperapi(target_url: str) -> Optional[str]:
    """ScraperAPI contourne Cloudflare — 1000 req gratuites/mois"""
    if not SCRAPER_KEY:
        return None
    api_url = f"http://api.scraperapi.com?api_key={SCRAPER_KEY}&url={target_url}&render=false"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(api_url, timeout=aiohttp.ClientTimeout(total=30)) as r:
                log.info(f"ScraperAPI HTTP {r.status} → {target_url[:60]}")
                if r.status==200:
                    text = await r.text()
                    if "Just a moment" not in text and "Enable JavaScript" not in text:
                        return text
    except Exception as e:
        log.debug(f"scraperapi: {e}")
    return None

# ── FETCH direct (pour les sites sans Cloudflare) ─────────────────────────────
async def fetch_direct(url: str, params: dict = None) -> Optional[str]:
    qs = ("?"+("&".join(f"{k}={v}" for k,v in params.items()))) if params else ""
    full = url+qs
    headers = {
        "User-Agent": random.choice([
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        ]),
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8",
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(full, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as r:
                log.info(f"Direct HTTP {r.status} {url[:60]}")
                if r.status==200:
                    text = await r.text()
                    if "Just a moment" not in text: return text
    except Exception as e:
        log.debug(f"direct: {e}")
    return None

# ── PARSE HTML Futbin ─────────────────────────────────────────────────────────
def parse_futbin_html(html: str, platform: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    players = []
    rows = soup.select("table#repTb tbody tr, #repTb tbody tr, table tbody tr")
    log.info(f"parse_futbin_html: {len(rows)} lignes")
    for row in rows[:60]:
        try:
            cols = row.select("td")
            if len(cols)<4: continue
            link_el = row.select_one("a[href*='/player/']")
            name_el = row.select_one(".player-name,.pname,td:nth-child(1) a")
            img_el  = row.select_one("img")
            rat_el  = cols[1] if len(cols)>1 else None
            pos_el  = cols[2] if len(cols)>2 else None
            all_p   = sorted(set([parse_price(c.get_text()) for c in cols if parse_price(c.get_text())>=200]))
            if not all_p: continue
            market=all_p[-1]; snipe=all_p[0] if len(all_p)>=2 and all_p[0]<market else int(market*0.9)
            if market<500: continue
            c=calc(snipe,market)
            if c["profit"]<MIN_PROFIT: continue
            href=link_el.get("href","") if link_el else ""
            pid=re.search(r"/player/(\d+)",href); pid=pid.group(1) if pid else "0"
            img=(img_el.get("src") or img_el.get("data-src","")) if img_el else ""
            players.append({
                "id":pid,
                "name": name_el.get_text(strip=True) if name_el else "Joueur",
                "rating": int(re.sub(r"\D","",rat_el.get_text()) or "0") if rat_el else 0,
                "position": pos_el.get_text(strip=True).upper() if pos_el else "?",
                "club":"—","nation":"—","league":"—",
                "snipe":snipe,"market":market,
                "image": img if img.startswith("http") else f"https://cdn.futbin.com/content/fifa26/img/players/{pid}.png",
                "url": ("https://www.futbin.com"+href) if href else "https://www.futbin.com/players",
                "source":"futbin", **c,
            })
        except: pass
    return players

# ── ORCHESTRATEUR ─────────────────────────────────────────────────────────────
async def scrape_futbin(filters: dict = None, platform: str = "pc") -> list[dict]:
    if filters is None: filters = {}
    sort_map={"pc":"pc_price","xbox":"xbox_price","playstation":"ps_price"}
    params={"sort":sort_map[platform],"order":"asc","per_page":"60","page":"1"}
    if filters.get("position"):   params["position"]  =filters["position"]
    if filters.get("min_rating"): params["min_rating"] =str(filters["min_rating"])
    if filters.get("max_price"):  params["max_price"]  =str(filters["max_price"])
    if filters.get("min_price"):  params["min_price"]  =str(filters["min_price"])
    qs="&".join(f"{k}={v}" for k,v in params.items())

    players=[]
    urls_to_try=[
        "https://www.futbin.com/players",
        "https://www.futbin.com/26/players",
    ]

    for base_url in urls_to_try:
        if players: break
        full_url=f"{base_url}?{qs}"

        # Méthode 1 : ScraperAPI (contourne Cloudflare)
        if SCRAPER_KEY:
            html = await fetch_via_scraperapi(full_url)
            if html:
                players = parse_futbin_html(html, platform)
                if players:
                    log.info(f"✅ ScraperAPI + Futbin: {len(players)} joueurs")
                    stats["source"]="ScraperAPI ✅"
                    stats["proxy_ok"]=True
                    break

        # Méthode 2 : Direct (fonctionne si Cloudflare non actif)
        html = await fetch_direct(base_url, params)
        if html:
            players = parse_futbin_html(html, platform)
            if players:
                log.info(f"✅ Direct Futbin: {len(players)} joueurs")
                stats["source"]="Futbin Direct ✅"
                break

    if not players:
        log.warning("⚠️ Toutes sources bloquées — ScraperAPI requis")
        stats["errors"]=stats.get("errors",0)+1
        stats["source"]="❌ Bloqué"
        _save("stats.json",stats)
        return []

    mp=filters.get("min_profit",MIN_PROFIT)
    mind=filters.get("min_discount",0)
    maxd=filters.get("max_discount",100)
    players=[p for p in players if p["profit"]>=mp and mind<=p["discount"]<=maxd]
    players.sort(key=lambda x:x["profit"],reverse=True)
    stats["last"]=datetime.utcnow().strftime("%H:%M:%S")
    _save("stats.json",stats)
    return players[:25]

# ── Bot ───────────────────────────────────────────────────────────────────────
intents=discord.Intents.default(); intents.message_content=True
bot=commands.Bot(command_prefix="!",intents=intents); tree=bot.tree

def alert_embed(p,platform):
    pl=PLATFORMS[platform]
    color=0xED4245 if p["discount"]>=20 else(0xFF8C00 if p["discount"]>=12 else 0x57F287)
    fire="🚨🚨🚨" if p["discount"]>=25 else("🚨🚨" if p["discount"]>=15 else"🚨")
    e=discord.Embed(title=f"{pl['emoji']} @{pl['label']}  ·  {fire}",url=p["url"],color=color,timestamp=datetime.utcnow())
    e.add_field(name=f"👤 {p['name']}  •  {p['rating']} {p['position']}",value=f"🌍 {p['nation']}  ·  🏆 {p['league']}  ·  ⚽ {p['club']}",inline=False)
    e.add_field(name="🎯 Snipe Price",value=f"**{fmt(p['snipe'])}** 🪙",inline=True)
    e.add_field(name="💵 Sells Price",value=f"**{fmt(p['sell'])}** 🪙",inline=True)
    e.add_field(name="💰 Profit",value=f"**+{fmt(p['profit'])}** 🪙",inline=True)
    e.add_field(name="🏷️ Réduction",value=f"**-{p['discount']}%**",inline=True)
    e.add_field(name="📈 ROI",value=f"**{p['roi']}%**",inline=True)
    e.add_field(name="📊",value=f"`{roi_bar(p['roi'])}`",inline=True)
    e.add_field(name="🔗",value=f"[Voir la carte]({p['url']})",inline=False)
    if p.get("image","").startswith("http"): e.set_thumbnail(url=p["image"])
    e.set_footer(text=f"FC26 Bot · {stats.get('source','?')} · Taxe EA 5%")
    return e

def summary_embed(players,platform,title,desc=""):
    pl=PLATFORMS[platform]
    e=discord.Embed(title=f"{pl['emoji']} {title} — {len(players)} opportunité(s)",
        description=desc or "Meilleures affaires FC26",color=pl["color"],timestamp=datetime.utcnow())
    if players:
        lines=[f"{'🟢' if p['roi']>20 else('🟡' if p['roi']>10 else'🔴')} **{p['name']}** {p['rating']} {p['position']} · 🎯{fmt(p['snipe'])} → +{fmt(p['profit'])} ({p['roi']}%)" for p in players[:10]]
        e.add_field(name="📋 Résultats",value="\n".join(lines),inline=False)
        best=players[0]
        e.add_field(name="🏆 Meilleure",value=f"**{best['name']}** +**{fmt(best['profit'])}** ({best['roi']}% ROI)",inline=False)
    else:
        needs_key = "" if SCRAPER_KEY else "\n\n⚠️ **Configure `/setscraperkey` pour débloquer le scraping !**"
        e.add_field(name="😔 Aucun résultat",value=f"Sources bloquées par Cloudflare.{needs_key}",inline=False)
    e.set_footer(text=f"FC26 Bot · {stats.get('source','—')} · Taxe EA 5%")
    return e

PC=[
    app_commands.Choice(name="🖥️ PC",value="pc"),
    app_commands.Choice(name="🟢 Xbox",value="xbox"),
    app_commands.Choice(name="🔵 PlayStation",value="playstation"),
]

@tree.command(name="setscraperkey",description="🔑 Configure ta clé ScraperAPI pour débloquer le scraping")
@app_commands.describe(key="Ta clé API depuis scraperapi.com (gratuit, 1000 req/mois)")
async def cmd_setscraperkey(interaction: discord.Interaction, key: str):
    global SCRAPER_KEY
    SCRAPER_KEY = key
    os.environ["SCRAPER_API_KEY"] = key
    await interaction.response.send_message(embed=discord.Embed(
        title="✅ Clé ScraperAPI configurée !",
        description="Le scraping Futbin est maintenant débloqué.\nTape `/status` pour vérifier.",
        color=0x57F287), ephemeral=True)

@tree.command(name="plateforme",description="🎮 Choisis ta plateforme")
@app_commands.choices(plateforme=PC)
async def cmd_plateforme(interaction: discord.Interaction, plateforme: str):
    user_prefs.setdefault(str(interaction.user.id),{})["platform"]=plateforme
    _save("user_prefs.json",user_prefs)
    pl=PLATFORMS[plateforme]
    await interaction.response.send_message(embed=discord.Embed(title=f"✅ {pl['emoji']} {pl['label']}",color=pl["color"]),ephemeral=True)

@tree.command(name="setchannel",description="📢 Salon pour les alertes automatiques")
@app_commands.choices(plateforme=[*PC,app_commands.Choice(name="🌐 Toutes",value="all")])
async def cmd_setchannel(interaction: discord.Interaction, salon: discord.TextChannel, plateforme: str="all"):
    gid=str(interaction.guild_id); channels.setdefault(gid,{})
    for p in(list(PLATFORMS.keys()) if plateforme=="all" else[plateforme]): channels[gid][p]=salon.id
    _save("channels.json",channels)
    await interaction.response.send_message(embed=discord.Embed(title="✅ Salon configuré !",description=f"Alertes → {salon.mention}",color=0x57F287))

@tree.command(name="scan",description="🔍 Scan immédiat")
@app_commands.choices(plateforme=PC)
async def cmd_scan(interaction: discord.Interaction, plateforme: str=None):
    await interaction.response.defer()
    pl=plateforme or get_plat(interaction.user.id)
    players=await scrape_futbin(platform=pl)
    await interaction.followup.send(embed=summary_embed(players,pl,"🔍 Scan Général"))
    for p in players[:3]: await interaction.followup.send(embed=alert_embed(p,pl))

@tree.command(name="snipe",description="⚡ Affaires dans ton budget")
@app_commands.describe(budget="Budget max en coins")
@app_commands.choices(plateforme=PC)
async def cmd_snipe(interaction: discord.Interaction, budget: int=50000, plateforme: str=None):
    await interaction.response.defer()
    pl=plateforme or get_plat(interaction.user.id)
    players=await scrape_futbin({"max_price":budget},pl)
    await interaction.followup.send(embed=summary_embed(players,pl,"⚡ Snipe",f"Budget: {fmt(budget)} coins"))
    for p in players[:3]: await interaction.followup.send(embed=alert_embed(p,pl))

@tree.command(name="erreurs",description="🚨 Cartes sous-évaluées")
@app_commands.choices(plateforme=PC)
async def cmd_erreurs(interaction: discord.Interaction, reduction: int=20, plateforme: str=None):
    await interaction.response.defer()
    pl=plateforme or get_plat(interaction.user.id)
    players=await scrape_futbin({"min_discount":reduction},pl)
    await interaction.followup.send(embed=summary_embed(players,pl,"🚨 Erreurs de Prix",f"-{reduction}% min"))
    for p in players[:5]: await interaction.followup.send(embed=alert_embed(p,pl))

@tree.command(name="position",description="📍 Par position")
@app_commands.choices(pos=[app_commands.Choice(name=p,value=p) for p in POSITIONS],plateforme=PC)
async def cmd_position(interaction: discord.Interaction, pos: str, min_profit: int=500, plateforme: str=None):
    await interaction.response.defer()
    pl=plateforme or get_plat(interaction.user.id)
    players=await scrape_futbin({"position":pos,"min_profit":min_profit},pl)
    await interaction.followup.send(embed=summary_embed(players,pl,f"📍 {pos}"))
    for p in players[:3]: await interaction.followup.send(embed=alert_embed(p,pl))

@tree.command(name="nation",description="🌍 Par nationalité")
@app_commands.choices(plateforme=PC)
async def cmd_nation(interaction: discord.Interaction, nation: str, min_profit: int=500, plateforme: str=None):
    await interaction.response.defer()
    pl=plateforme or get_plat(interaction.user.id)
    players=await scrape_futbin({"nation":nation,"min_profit":min_profit},pl)
    await interaction.followup.send(embed=summary_embed(players,pl,f"🌍 {nation}"))
    for p in players[:3]: await interaction.followup.send(embed=alert_embed(p,pl))

@tree.command(name="ligue",description="🏆 Par ligue")
@app_commands.choices(plateforme=PC)
async def cmd_ligue(interaction: discord.Interaction, ligue: str, min_profit: int=500, plateforme: str=None):
    await interaction.response.defer()
    pl=plateforme or get_plat(interaction.user.id)
    players=await scrape_futbin({"league":ligue,"min_profit":min_profit},pl)
    await interaction.followup.send(embed=summary_embed(players,pl,f"🏆 {ligue}"))
    for p in players[:3]: await interaction.followup.send(embed=alert_embed(p,pl))

@tree.command(name="meta",description="🔥 Top cartes méta")
@app_commands.choices(plateforme=PC)
async def cmd_meta(interaction: discord.Interaction, min_note: int=87, plateforme: str=None):
    await interaction.response.defer()
    pl=plateforme or get_plat(interaction.user.id)
    players=await scrape_futbin({"min_rating":min_note},pl)
    await interaction.followup.send(embed=summary_embed(players,pl,f"🔥 Meta {min_note}+"))
    for p in players[:3]: await interaction.followup.send(embed=alert_embed(p,pl))

@tree.command(name="premium",description="💎 Grosses cartes")
@app_commands.choices(plateforme=PC)
async def cmd_premium(interaction: discord.Interaction, min_prix: int=50000, min_profit: int=5000, plateforme: str=None):
    await interaction.response.defer()
    pl=plateforme or get_plat(interaction.user.id)
    players=await scrape_futbin({"min_price":min_prix,"min_profit":min_profit},pl)
    await interaction.followup.send(embed=summary_embed(players,pl,"💎 Premium"))
    for p in players[:3]: await interaction.followup.send(embed=alert_embed(p,pl))

@tree.command(name="budget",description="💰 Petit budget")
@app_commands.choices(plateforme=PC)
async def cmd_budget(interaction: discord.Interaction, max_prix: int=10000, plateforme: str=None):
    await interaction.response.defer()
    pl=plateforme or get_plat(interaction.user.id)
    players=await scrape_futbin({"max_price":max_prix},pl)
    await interaction.followup.send(embed=summary_embed(players,pl,"💰 Budget"))
    for p in players[:5]: await interaction.followup.send(embed=alert_embed(p,pl))

@tree.command(name="top",description="🏆 Top 10 meilleures affaires")
@app_commands.choices(plateforme=PC)
async def cmd_top(interaction: discord.Interaction, plateforme: str=None):
    await interaction.response.defer()
    pl=plateforme or get_plat(interaction.user.id)
    results=[]
    for filt in [{"min_rating":85},{"max_price":50000},{"min_discount":15}]:
        try: results.extend(await scrape_futbin(filt,pl)); await asyncio.sleep(2)
        except: pass
    seen,unique=set(),[]
    for p in results:
        if p["id"] not in seen: seen.add(p["id"]); unique.append(p)
    unique.sort(key=lambda x:x["profit"],reverse=True)
    plat=PLATFORMS[pl]
    e=discord.Embed(title=f"🏆 Top 10 {plat['emoji']} {plat['label']}",color=plat["color"],timestamp=datetime.utcnow())
    medals=["🥇","🥈","🥉"]+[f"{i}." for i in range(4,11)]
    lines=[f"{medals[i]} **{p['name']}** {p['rating']} {p['position']} · 🎯{fmt(p['snipe'])} → +{fmt(p['profit'])} ({p['roi']}%)" for i,p in enumerate(unique[:10])]
    e.add_field(name="Classement",value="\n".join(lines) if lines else"Aucun résultat",inline=False)
    await interaction.followup.send(embed=e)
    for p in unique[:3]: await interaction.followup.send(embed=alert_embed(p,pl))

@tree.command(name="status",description="📡 Test live du scraping")
async def cmd_status(interaction: discord.Interaction):
    await interaction.response.defer()
    has_key = bool(SCRAPER_KEY)
    e=discord.Embed(title="📡 Statut du Bot FC26",color=0x5865F2,timestamp=datetime.utcnow())
    e.add_field(name="🔑 ScraperAPI",value="✅ Configurée" if has_key else "❌ Non configurée",inline=True)
    e.add_field(name="🔍 Scans",value=str(stats.get("scans",0)),inline=True)
    e.add_field(name="❌ Erreurs",value=str(stats.get("errors",0)),inline=True)
    e.add_field(name="📊 Source",value=stats.get("source","—"),inline=True)
    e.add_field(name="🕐 Dernier scan",value=stats.get("last","jamais"),inline=True)
    if not has_key:
        e.add_field(name="⚠️ Action requise",
            value="1. Va sur **scraperapi.com** → créé un compte gratuit\n2. Copie ta clé API\n3. Dans Railway → Variables → ajoute `SCRAPER_API_KEY = ta_clé`\n4. Redéploie",
            inline=False)
    await interaction.followup.send(embed=e)
    if has_key:
        players=await scrape_futbin(platform="pc")
        r=discord.Embed(timestamp=datetime.utcnow())
        if players:
            r.title=f"✅ Scraping OK — {len(players)} joueurs trouvés"
            r.color=0x57F287
            lines=[f"• **{p['name']}** {p['rating']} {p['position']} — Snipe {fmt(p['snipe'])} · +{fmt(p['profit'])}" for p in players[:5]]
            r.add_field(name="Top 5 PC",value="\n".join(lines),inline=False)
        else:
            r.title="❌ Scraping échoué malgré la clé"
            r.color=0xED4245
            r.add_field(name="Cause", value="Vérifie que ta clé ScraperAPI est valide sur scraperapi.com", inline=False)
        await interaction.followup.send(embed=r)

@tree.command(name="stats",description="📊 Statistiques")
async def cmd_stats(interaction: discord.Interaction):
    e=discord.Embed(title="📊 FC26 Bot — Stats",color=0x5865F2,timestamp=datetime.utcnow())
    e.add_field(name="🔔 Alertes",value=str(stats.get("sent",0)),inline=True)
    e.add_field(name="🔍 Scans",value=str(stats.get("scans",0)),inline=True)
    e.add_field(name="💰 Profit affiché",value=f"{fmt(stats.get('profit',0))} coins",inline=True)
    await interaction.response.send_message(embed=e)

@tree.command(name="aide",description="❓ Guide complet")
async def cmd_aide(interaction: discord.Interaction):
    e=discord.Embed(title="🤖 FC26 Ultimate Trading Bot",color=0x5865F2)
    e.add_field(name="⚙️ Setup OBLIGATOIRE",value="1. Créé un compte gratuit sur **scraperapi.com**\n2. Railway → Variables → `SCRAPER_API_KEY = ta_clé`\n3. Redéploie → Tape `/status`",inline=False)
    e.add_field(name="🔍 Scans",value="`/scan` · `/snipe [budget]` · `/erreurs [%]`\n`/meta [note]` · `/premium` · `/budget [max]` · `/top`",inline=False)
    e.add_field(name="🎯 Filtres",value="`/position ST` · `/nation France` · `/ligue Premier League`",inline=False)
    e.add_field(name="📡 Debug",value="`/status` · `/stats`",inline=False)
    await interaction.response.send_message(embed=e)

@tasks.loop(seconds=SCAN_DELAY)
async def auto_scan():
    stats["scans"]=stats.get("scans",0)+1
    stats["last"]=datetime.utcnow().strftime("%H:%M:%S")
    if not SCRAPER_KEY: return  # Pas de clé = pas de scan
    for guild in bot.guilds:
        gid=str(guild.id)
        for platform,channel_id in channels.get(gid,{}).items():
            ch=bot.get_channel(channel_id)
            if not ch: continue
            try:
                players=await scrape_futbin(platform=platform)
                sent=0
                for p in players:
                    if sent>=MAX_ALERTS: break
                    key=f"{gid}_{platform}_{p['id']}_{p['snipe']}"
                    if key in sent_alerts: continue
                    await ch.send(embed=alert_embed(p,platform))
                    sent_alerts[key]=datetime.utcnow().isoformat()
                    stats["sent"]=stats.get("sent",0)+1
                    stats["profit"]=stats.get("profit",0)+p["profit"]
                    sent+=1
                    await asyncio.sleep(2)
                if len(sent_alerts)>1000:
                    ks=list(sent_alerts.keys())
                    for k in ks[:-800]: del sent_alerts[k]
                _save("sent_alerts.json",sent_alerts)
                _save("stats.json",stats)
            except Exception as ex: log.error(f"auto_scan [{platform}]: {ex}")
        await asyncio.sleep(10)

@bot.event
async def on_ready():
    log.info(f"Connecté : {bot.user} | ScraperAPI: {'✅' if SCRAPER_KEY else '❌ NON CONFIGURÉE'}")
    for guild in bot.guilds:
        try:
            s=await tree.sync(guild=guild)
            log.info(f"✅ {len(s)} commandes → {guild.name}")
        except Exception as ex: log.warning(f"sync: {ex}")
    try:
        g=await tree.sync(); log.info(f"✅ {len(g)} commandes global")
    except: pass
    auto_scan.start()
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching,name="marché FC26 💹"))

if __name__=="__main__":
    bot.run(TOKEN)
