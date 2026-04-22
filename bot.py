"""
FC26 ULTIMATE TRADING BOT — Sources multiples anti-blocage
Sources : FUT.GG + Futwiz + Futbin (fallback)
curl-cffi Chrome TLS fingerprint
"""

import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio, json, os, re, logging, random
from datetime import datetime
from typing import Optional
from bs4 import BeautifulSoup

try:
    from curl_cffi.requests import AsyncSession
    CURL_OK = True
except ImportError:
    import aiohttp
    CURL_OK = False

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()])
log = logging.getLogger(__name__)
log.info(f"curl-cffi: {CURL_OK}")

TOKEN      = os.getenv("DISCORD_TOKEN", "VOTRE_TOKEN_ICI")
MIN_PROFIT = 500
EA_TAX     = 0.05
SCAN_DELAY = 90
MAX_ALERTS = 6

PLATFORMS = {
    "pc":          {"label": "PC",          "emoji": "🖥️", "color": 0x5865F2},
    "xbox":        {"label": "Xbox",        "emoji": "🟢", "color": 0x107C10},
    "playstation": {"label": "PlayStation", "emoji": "🔵", "color": 0x003791},
}
POSITIONS = ["ST","CF","CAM","CM","CDM","LW","RW","LB","RB","CB","GK","LM","RM","LWB","RWB"]
IMPERSONATE = ["chrome124","chrome123","chrome120","chrome110","chrome107"]

def _load(p, d):
    return json.load(open(p)) if os.path.exists(p) else d
def _save(p, d):
    json.dump(d, open(p,"w"), indent=2, ensure_ascii=False)

user_prefs  = _load("user_prefs.json", {})
channels    = _load("channels.json", {})
sent_alerts = _load("sent_alerts.json", {})
stats       = _load("stats.json", {"sent":0,"profit":0,"scans":0,"errors":0,"last":"—","source":"—"})

def fmt(n):
    if n>=1_000_000: return f"{n/1_000_000:.2f}M"
    if n>=1_000: return f"{n/1_000:.1f}K"
    return str(n)

def parse_price(v):
    if not v: return 0
    t = str(v).upper().strip()
    if t in ("FREE","N/A","—","-","","0","NO PRICE","NULL","NONE"): return 0
    t = t.replace(",","").replace(" ","").replace("\xa0","").replace("'","")
    try:
        if "M" in t: return int(float(t.replace("M",""))*1_000_000)
        if "K" in t: return int(float(t.replace("K",""))*1_000)
        val = int(re.sub(r"[^\d]","",t) or 0)
        return val if 200 <= val <= 50_000_000 else 0
    except: return 0

def calc(snipe, market):
    sell = int(market*(1-EA_TAX))
    profit = sell-snipe
    roi = round(profit/snipe*100,1) if snipe>0 else 0
    disc = round((market-snipe)/market*100,1) if market>0 else 0
    return {"sell":sell,"profit":profit,"roi":roi,"discount":disc}

def roi_bar(roi):
    return "█"*min(int(roi/4),12)+"░"*(12-min(int(roi/4),12))

def get_plat(uid):
    return user_prefs.get(str(uid),{}).get("platform","pc")

# ── Requête HTTP ──────────────────────────────────────────────────────────────
async def fetch(url: str, params: dict = None, json_mode=False) -> Optional[str]:
    qs = ("?"+("&".join(f"{k}={v}" for k,v in params.items()))) if params else ""
    full = url+qs
    hdrs_base = {
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8",
        "Referer": "https://www.google.com/",
        "Cache-Control": "no-cache",
    }
    if json_mode:
        hdrs_base["Accept"] = "application/json,text/javascript,*/*;q=0.01"
        hdrs_base["X-Requested-With"] = "XMLHttpRequest"
    else:
        hdrs_base["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

    if CURL_OK:
        try:
            async with AsyncSession() as s:
                r = await s.get(full, impersonate=random.choice(IMPERSONATE),
                    headers=hdrs_base, timeout=20)
                log.info(f"HTTP {r.status_code} {url[:60]}")
                if r.status_code==200: return r.text
        except Exception as e:
            log.debug(f"curl-cffi: {e}")
    try:
        import aiohttp as ah
        async with ah.ClientSession() as s:
            async with s.get(full, headers={
                **hdrs_base,
                "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
            }, timeout=ah.ClientTimeout(total=20)) as r:
                log.info(f"aiohttp HTTP {r.status} {url[:60]}")
                if r.status==200: return await r.text()
    except Exception as e:
        log.debug(f"aiohttp: {e}")
    return None

# ── SOURCE 1 : FUT.GG ─────────────────────────────────────────────────────────
async def fetch_futgg(filters: dict, platform: str) -> list[dict]:
    """FUT.GG — moins protégé que Futbin, données FC26 complètes"""
    plat_map = {"pc":"pc","xbox":"xbox","playstation":"ps"}
    p_str = plat_map[platform]
    params = {"page":"1","per_page":"40","sort":"cheapest","platform":p_str}
    if filters.get("position"):   params["position"]   = filters["position"].lower()
    if filters.get("min_rating"): params["min_ovr"]    = str(filters["min_rating"])
    if filters.get("max_rating"): params["max_ovr"]    = str(filters["max_rating"])
    if filters.get("max_price"):  params["max_price"]  = str(filters["max_price"])
    if filters.get("min_price"):  params["min_price"]  = str(filters["min_price"])

    players = []
    for url in ["https://www.fut.gg/api/fut/players/", "https://fut.gg/api/fut/players/"]:
        try:
            text = await fetch(url, params, json_mode=True)
            if not text: continue
            text = text.strip()
            if not (text.startswith("{") or text.startswith("[")): continue
            data = json.loads(text)
            raw = data if isinstance(data,list) else data.get("results",data.get("players",data.get("data",[])))
            if not raw: continue
            for p in raw:
                prices = p.get("prices",{}) or {}
                pc_prices = prices.get("pc",{}) or prices.get("PC",{})
                ps_prices = prices.get("ps",{}) or prices.get("PS",{}) or prices.get("console",{})
                xb_prices = prices.get("xbox",{}) or prices.get("XB",{})
                plat_prices = {"pc":pc_prices,"xbox":xb_prices,"playstation":ps_prices}[platform]
                market = parse_price(plat_prices.get("min") or plat_prices.get("price") or p.get("price") or 0)
                snipe  = parse_price(plat_prices.get("min") or market)
                if market < 500: continue
                if snipe <= 0 or snipe > market: snipe = market
                c = calc(snipe, market)
                if c["profit"] < MIN_PROFIT: continue
                pid = str(p.get("id",""))
                players.append({
                    "id": pid,
                    "name":     p.get("name") or p.get("known_as","?"),
                    "rating":   int(p.get("rating") or p.get("ovr") or 0),
                    "position": str(p.get("position","?")).upper(),
                    "club":     p.get("club","—"), "nation": p.get("nation","—"),
                    "league":   p.get("league","—"),
                    "snipe": snipe, "market": market,
                    "image": p.get("image","") or f"https://www.fut.gg/static/media/{pid}.png",
                    "url": f"https://www.fut.gg/players/{pid}/",
                    "source":"futgg", **c,
                })
            if players:
                log.info(f"FUT.GG: {len(players)} joueurs")
                break
        except Exception as e:
            log.debug(f"futgg {url}: {e}")
    return players

# ── SOURCE 2 : FUTWIZ ────────────────────────────────────────────────────────
async def fetch_futwiz(filters: dict, platform: str) -> list[dict]:
    plat_map = {"pc":"pc","xbox":"xbox","playstation":"ps"}
    p_str = plat_map[platform]
    params = {"page":"1","sort":f"{p_str}_price","order":"asc","per_page":"40"}
    if filters.get("position"):   params["position"]   = filters["position"].lower()
    if filters.get("min_rating"): params["rating_min"] = str(filters["min_rating"])
    if filters.get("max_price"):  params["max_price"]  = str(filters["max_price"])
    if filters.get("min_price"):  params["min_price"]  = str(filters["min_price"])

    players = []
    for url in ["https://www.futwiz.com/en/fc26/players","https://www.futwiz.com/en/fc25/players"]:
        try:
            html = await fetch(url, params)
            if not html: continue
            soup = BeautifulSoup(html,"html.parser")
            cards = soup.select(".player-card,.player-item,.table-player,table tbody tr")
            log.info(f"Futwiz {url}: {len(cards)} éléments")
            for card in cards[:40]:
                try:
                    name_el = card.select_one("[class*=name],h3,h4,td:nth-child(1) a")
                    rat_el  = card.select_one("[class*=rating],[class*=ovr],td:nth-child(2)")
                    pos_el  = card.select_one("[class*=position],[class*=pos],td:nth-child(3)")
                    img_el  = card.select_one("img")
                    link_el = card.select_one("a[href*=player]")
                    price_els = card.select("[class*=price],[data-price],td:nth-child(4),td:nth-child(5),td:nth-child(6)")
                    prices = sorted(set([parse_price(el.get_text()) for el in price_els if parse_price(el.get_text())>=200]))
                    if not prices: continue
                    market = prices[-1]
                    snipe  = prices[0] if len(prices)>=2 and prices[0]<market else int(market*0.88)
                    if market<500: continue
                    c = calc(snipe,market)
                    if c["profit"]<MIN_PROFIT: continue
                    href = link_el.get("href","") if link_el else ""
                    pid  = re.search(r"/(\d+)",href)
                    pid  = pid.group(1) if pid else "0"
                    img  = img_el.get("src","") if img_el else ""
                    players.append({
                        "id": pid,
                        "name":     name_el.get_text(strip=True) if name_el else "Joueur",
                        "rating":   int(re.sub(r"\D","",rat_el.get_text()) or "0") if rat_el else 0,
                        "position": pos_el.get_text(strip=True).upper() if pos_el else "?",
                        "club":"—","nation":"—","league":"—",
                        "snipe":snipe,"market":market,
                        "image": img if img.startswith("http") else "",
                        "url": ("https://www.futwiz.com"+href) if href else "https://www.futwiz.com/en/fc26/players",
                        "source":"futwiz", **c,
                    })
                except: pass
            if players:
                log.info(f"Futwiz: {len(players)} joueurs")
                break
        except Exception as e:
            log.debug(f"futwiz: {e}")
    return players

# ── SOURCE 3 : FUTBIN (dernier recours) ──────────────────────────────────────
async def fetch_futbin(filters: dict, platform: str) -> list[dict]:
    sort_map = {"pc":"pc_price","xbox":"xbox_price","playstation":"ps_price"}
    params = {"sort":sort_map[platform],"order":"asc","per_page":"40","page":"1"}
    if filters.get("position"):   params["position"]   = filters["position"]
    if filters.get("min_rating"): params["min_rating"]  = str(filters["min_rating"])
    if filters.get("max_price"):  params["max_price"]   = str(filters["max_price"])
    if filters.get("min_price"):  params["min_price"]   = str(filters["min_price"])

    players = []
    for url in ["https://www.futbin.com/players","https://www.futbin.com/26/players"]:
        try:
            html = await fetch(url, params)
            if not html or "Just a moment" in html or "Enable JavaScript" in html: continue
            soup = BeautifulSoup(html,"html.parser")
            rows = soup.select("table#repTb tbody tr, table tbody tr")
            for row in rows[:40]:
                try:
                    cols = row.select("td")
                    if len(cols)<5: continue
                    link_el = row.select_one("a[href*='/player/']")
                    name_el = row.select_one(".player-name,.pname,td:nth-child(1) a")
                    img_el  = row.select_one("img")
                    rat_el  = cols[1] if len(cols)>1 else None
                    pos_el  = cols[2] if len(cols)>2 else None
                    all_p   = sorted(set([parse_price(c.get_text()) for c in cols if parse_price(c.get_text())>=200]))
                    if not all_p: continue
                    market = all_p[-1]
                    snipe  = all_p[0] if len(all_p)>=2 and all_p[0]<market else int(market*0.88)
                    if market<500: continue
                    c = calc(snipe,market)
                    if c["profit"]<MIN_PROFIT: continue
                    href = link_el.get("href","") if link_el else ""
                    pid  = re.search(r"/player/(\d+)",href)
                    pid  = pid.group(1) if pid else "0"
                    img  = (img_el.get("src") or img_el.get("data-src","")) if img_el else ""
                    players.append({
                        "id":pid,
                        "name":     name_el.get_text(strip=True) if name_el else "Joueur",
                        "rating":   int(re.sub(r"\D","",rat_el.get_text()) or "0") if rat_el else 0,
                        "position": pos_el.get_text(strip=True).upper() if pos_el else "?",
                        "club":"—","nation":"—","league":"—",
                        "snipe":snipe,"market":market,
                        "image": img if img.startswith("http") else f"https://cdn.futbin.com/content/fifa25/img/players/{pid}.png",
                        "url": ("https://www.futbin.com"+href) if href else "https://www.futbin.com/players",
                        "source":"futbin", **c,
                    })
                except: pass
            if players:
                log.info(f"Futbin {url}: {len(players)} joueurs")
                break
        except Exception as e:
            log.debug(f"futbin: {e}")
    return players

# ── ORCHESTRATEUR ─────────────────────────────────────────────────────────────
async def scrape_futbin(filters: dict = None, platform: str = "pc") -> list[dict]:
    if filters is None: filters = {}
    players = []

    # Essaie les 3 sources dans l'ordre
    for source_fn, name in [(fetch_futgg,"FUT.GG"),(fetch_futwiz,"Futwiz"),(fetch_futbin,"Futbin")]:
        try:
            players = await source_fn(filters, platform)
            if players:
                log.info(f"✅ Source: {name} — {len(players)} joueurs")
                stats["source"] = name
                break
        except Exception as e:
            log.error(f"{name} error: {e}")

    if not players:
        stats["errors"] = stats.get("errors",0)+1
        _save("stats.json",stats)
        return []

    mp   = filters.get("min_profit", MIN_PROFIT)
    mind = filters.get("min_discount", 0)
    maxd = filters.get("max_discount", 100)
    players = [p for p in players if p["profit"]>=mp and mind<=p["discount"]<=maxd]
    players.sort(key=lambda x:x["profit"], reverse=True)
    stats["last"] = datetime.utcnow().strftime("%H:%M:%S")
    return players[:25]

# ── Bot ───────────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot  = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ── Embeds ────────────────────────────────────────────────────────────────────
SRC_EMOJI = {"FUT.GG":"✅ FUT.GG","Futwiz":"🔄 Futwiz","Futbin":"⚠️ Futbin","—":"—"}

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
    e.add_field(name="🔗", value=f"[Voir la carte]({p['url']})", inline=False)
    if p.get("image","").startswith("http"):
        e.set_thumbnail(url=p["image"])
    e.set_footer(text=f"FC26 Bot · {SRC_EMOJI.get(p.get('source',''),'?')} · Taxe EA 5%")
    return e

def summary_embed(players, platform, title, desc=""):
    pl = PLATFORMS[platform]
    e  = discord.Embed(title=f"{pl['emoji']} {title} — {len(players)} opportunité(s)",
        description=desc or "Meilleures affaires", color=pl["color"], timestamp=datetime.utcnow())
    if players:
        lines=[]
        for p in players[:10]:
            dot = "🟢" if p["roi"]>20 else ("🟡" if p["roi"]>10 else "🔴")
            lines.append(f"{dot} **{p['name']}** {p['rating']} {p['position']} · 🎯{fmt(p['snipe'])} → +{fmt(p['profit'])} ({p['roi']}%)")
        e.add_field(name="📋 Résultats", value="\n".join(lines), inline=False)
        best=players[0]
        e.add_field(name="🏆 Meilleure", value=f"**{best['name']}** +**{fmt(best['profit'])}** coins ({best['roi']}% ROI)", inline=False)
    else:
        e.add_field(name="😔 Aucun résultat", value="Aucune affaire trouvée. Utilise `/status` pour vérifier.", inline=False)
    e.set_footer(text=f"FC26 Bot · Source: {SRC_EMOJI.get(stats.get('source','—'),'?')} · Taxe EA 5%")
    return e

# ── Commandes ─────────────────────────────────────────────────────────────────
PC = [
    app_commands.Choice(name="🖥️ PC",         value="pc"),
    app_commands.Choice(name="🟢 Xbox",        value="xbox"),
    app_commands.Choice(name="🔵 PlayStation", value="playstation"),
]

@tree.command(name="plateforme", description="🎮 Choisis ta plateforme par défaut")
@app_commands.choices(plateforme=PC)
async def cmd_plateforme(interaction: discord.Interaction, plateforme: str):
    user_prefs.setdefault(str(interaction.user.id),{})["platform"] = plateforme
    _save("user_prefs.json", user_prefs)
    pl = PLATFORMS[plateforme]
    await interaction.response.send_message(embed=discord.Embed(title=f"✅ {pl['emoji']} {pl['label']}", color=pl["color"]), ephemeral=True)

@tree.command(name="setchannel", description="📢 Salon pour les alertes automatiques")
@app_commands.choices(plateforme=[*PC, app_commands.Choice(name="🌐 Toutes", value="all")])
async def cmd_setchannel(interaction: discord.Interaction, salon: discord.TextChannel, plateforme: str = "all"):
    gid = str(interaction.guild_id)
    channels.setdefault(gid,{})
    for p in (list(PLATFORMS.keys()) if plateforme=="all" else [plateforme]):
        channels[gid][p] = salon.id
    _save("channels.json", channels)
    await interaction.response.send_message(embed=discord.Embed(title="✅ Salon configuré !",
        description=f"Alertes → {salon.mention}", color=0x57F287))

@tree.command(name="scan", description="🔍 Scan immédiat des meilleures affaires")
@app_commands.choices(plateforme=PC)
async def cmd_scan(interaction: discord.Interaction, plateforme: str = None):
    await interaction.response.defer()
    pl = plateforme or get_plat(interaction.user.id)
    players = await scrape_futbin(platform=pl)
    await interaction.followup.send(embed=summary_embed(players, pl, "🔍 Scan Général"))
    for p in players[:3]: await interaction.followup.send(embed=alert_embed(p, pl))

@tree.command(name="snipe", description="⚡ Meilleures affaires dans ton budget")
@app_commands.describe(budget="Budget max en coins (ex: 50000)")
@app_commands.choices(plateforme=PC)
async def cmd_snipe(interaction: discord.Interaction, budget: int = 50000, plateforme: str = None):
    await interaction.response.defer()
    pl = plateforme or get_plat(interaction.user.id)
    players = await scrape_futbin({"max_price": budget}, pl)
    await interaction.followup.send(embed=summary_embed(players, pl, "⚡ Snipe", f"Budget: {fmt(budget)} coins"))
    for p in players[:3]: await interaction.followup.send(embed=alert_embed(p, pl))

@tree.command(name="erreurs", description="🚨 Cartes massivement sous-évaluées")
@app_commands.describe(reduction="Réduction minimum en % (ex: 20)")
@app_commands.choices(plateforme=PC)
async def cmd_erreurs(interaction: discord.Interaction, reduction: int = 20, plateforme: str = None):
    await interaction.response.defer()
    pl = plateforme or get_plat(interaction.user.id)
    players = await scrape_futbin({"min_discount": reduction}, pl)
    await interaction.followup.send(embed=summary_embed(players, pl, "🚨 Erreurs de Prix", f"-{reduction}% min"))
    for p in players[:5]: await interaction.followup.send(embed=alert_embed(p, pl))

@tree.command(name="position", description="📍 Cherche par position (ST, CAM, CB...)")
@app_commands.choices(pos=[app_commands.Choice(name=p, value=p) for p in POSITIONS], plateforme=PC)
async def cmd_position(interaction: discord.Interaction, pos: str, min_profit: int = 500, plateforme: str = None):
    await interaction.response.defer()
    pl = plateforme or get_plat(interaction.user.id)
    players = await scrape_futbin({"position": pos, "min_profit": min_profit}, pl)
    await interaction.followup.send(embed=summary_embed(players, pl, f"📍 {pos}"))
    for p in players[:3]: await interaction.followup.send(embed=alert_embed(p, pl))

@tree.command(name="nation", description="🌍 Cherche par nationalité")
@app_commands.choices(plateforme=PC)
async def cmd_nation(interaction: discord.Interaction, nation: str, min_profit: int = 500, plateforme: str = None):
    await interaction.response.defer()
    pl = plateforme or get_plat(interaction.user.id)
    players = await scrape_futbin({"nation": nation, "min_profit": min_profit}, pl)
    await interaction.followup.send(embed=summary_embed(players, pl, f"🌍 {nation}"))
    for p in players[:3]: await interaction.followup.send(embed=alert_embed(p, pl))

@tree.command(name="ligue", description="🏆 Cherche par ligue")
@app_commands.choices(plateforme=PC)
async def cmd_ligue(interaction: discord.Interaction, ligue: str, min_profit: int = 500, plateforme: str = None):
    await interaction.response.defer()
    pl = plateforme or get_plat(interaction.user.id)
    players = await scrape_futbin({"league": ligue, "min_profit": min_profit}, pl)
    await interaction.followup.send(embed=summary_embed(players, pl, f"🏆 {ligue}"))
    for p in players[:3]: await interaction.followup.send(embed=alert_embed(p, pl))

@tree.command(name="meta", description="🔥 Top cartes méta FC26")
@app_commands.choices(plateforme=PC)
async def cmd_meta(interaction: discord.Interaction, min_note: int = 87, plateforme: str = None):
    await interaction.response.defer()
    pl = plateforme or get_plat(interaction.user.id)
    players = await scrape_futbin({"min_rating": min_note}, pl)
    await interaction.followup.send(embed=summary_embed(players, pl, f"🔥 Meta {min_note}+"))
    for p in players[:3]: await interaction.followup.send(embed=alert_embed(p, pl))

@tree.command(name="premium", description="💎 Grosses cartes, gros profits")
@app_commands.choices(plateforme=PC)
async def cmd_premium(interaction: discord.Interaction, min_prix: int = 50000, min_profit: int = 5000, plateforme: str = None):
    await interaction.response.defer()
    pl = plateforme or get_plat(interaction.user.id)
    players = await scrape_futbin({"min_price": min_prix, "min_profit": min_profit}, pl)
    await interaction.followup.send(embed=summary_embed(players, pl, "💎 Premium"))
    for p in players[:3]: await interaction.followup.send(embed=alert_embed(p, pl))

@tree.command(name="budget", description="💰 Petit budget, volume élevé")
@app_commands.choices(plateforme=PC)
async def cmd_budget(interaction: discord.Interaction, max_prix: int = 10000, plateforme: str = None):
    await interaction.response.defer()
    pl = plateforme or get_plat(interaction.user.id)
    players = await scrape_futbin({"max_price": max_prix}, pl)
    await interaction.followup.send(embed=summary_embed(players, pl, "💰 Budget"))
    for p in players[:5]: await interaction.followup.send(embed=alert_embed(p, pl))

@tree.command(name="joueur", description="🔎 Recherche un joueur spécifique FC26")
@app_commands.describe(nom="Nom du joueur (ex: Mbappe, Bellingham, Salah)")
@app_commands.choices(plateforme=PC)
async def cmd_joueur(interaction: discord.Interaction, nom: str, plateforme: str = None):
    await interaction.response.defer()
    pl = plateforme or get_plat(interaction.user.id)
    players = await scrape_futbin({"search": nom}, pl)
    # Filtre par nom
    filtered = [p for p in players if nom.lower() in p["name"].lower()]
    if not filtered: filtered = players
    await interaction.followup.send(embed=summary_embed(filtered, pl, f"🔎 {nom}"))
    for p in filtered[:3]: await interaction.followup.send(embed=alert_embed(p, pl))

@tree.command(name="top", description="🏆 Top 10 meilleures affaires toutes catégories")
@app_commands.choices(plateforme=PC)
async def cmd_top(interaction: discord.Interaction, plateforme: str = None):
    await interaction.response.defer()
    pl = plateforme or get_plat(interaction.user.id)
    results = []
    for filt in [{"min_rating":85},{"max_price":50000},{"min_discount":15}]:
        try:
            p = await scrape_futbin(filt, pl)
            results.extend(p)
            await asyncio.sleep(1)
        except: pass
    seen,unique=[],set()
    for p in results:
        if p["id"] not in seen:
            seen.append(p["id"]); unique.append(p)
    unique.sort(key=lambda x:x["profit"], reverse=True)
    plat = PLATFORMS[pl]
    e = discord.Embed(title=f"🏆 Top 10 {plat['emoji']} {plat['label']}", color=plat["color"], timestamp=datetime.utcnow())
    lines=[]
    for i,p in enumerate(unique[:10],1):
        m = "🥇" if i==1 else ("🥈" if i==2 else ("🥉" if i==3 else f"{i}."))
        lines.append(f"{m} **{p['name']}** {p['rating']} {p['position']} · 🎯{fmt(p['snipe'])} → +{fmt(p['profit'])} ({p['roi']}%)")
    e.add_field(name="Classement", value="\n".join(lines) if lines else "Aucun résultat", inline=False)
    await interaction.followup.send(embed=e)
    for p in unique[:3]: await interaction.followup.send(embed=alert_embed(p, pl))

@tree.command(name="meilleursfiltre", description="🧠 Analyse tout le marché FC26 et trouve les meilleures opportunités")
@app_commands.choices(plateforme=PC)
async def cmd_meilleursfiltre(interaction: discord.Interaction, plateforme: str = None):
    await interaction.response.defer()
    pl = plateforme or get_plat(interaction.user.id)
    await interaction.followup.send(embed=discord.Embed(title="🧠 Analyse complète en cours...",
        description="Scan de 8 filtres simultanés. 30-60 secondes.", color=0xFEE75C))
    configs = [
        ("🚨 Erreurs Prix",  {"min_discount":20}),
        ("⚡ Snipe 50K",     {"max_price":50000}),
        ("💎 Premium",       {"min_price":100000}),
        ("🔥 Meta 87+",      {"min_rating":87}),
        ("📍 Attaquants",    {"position":"ST"}),
        ("📍 Milieux CAM",   {"position":"CAM"}),
        ("📍 Défenseurs",    {"position":"CB"}),
        ("💰 Budget",        {"max_price":15000}),
    ]
    all_r=[]
    for label,filt in configs:
        try:
            p = await scrape_futbin(filt, pl)
            for x in p: x["filtre"]=label
            all_r.extend(p)
            await asyncio.sleep(2)
        except: pass
    seen,unique=set(),[]
    for p in all_r:
        if p["id"] not in seen:
            seen.add(p["id"]); unique.append(p)
    unique.sort(key=lambda x:x["profit"], reverse=True)
    if not unique:
        await interaction.followup.send(embed=discord.Embed(title="❌ Aucun résultat",description="Sources indisponibles. Réessaie dans 2 minutes.",color=0xED4245))
        return
    plat = PLATFORMS[pl]
    recap = discord.Embed(title=f"🧠 Analyse Marché FC26 — {len(unique)} opportunités",
        description=f"Scan {len(configs)} filtres · {plat['emoji']} {plat['label']}",
        color=0x5865F2, timestamp=datetime.utcnow())
    by_f={}
    for p in unique:
        f=p.get("filtre","?")
        if f not in by_f: by_f[f]=[]
        if len(by_f[f])<2: by_f[f].append(p)
    for label,plist in list(by_f.items())[:5]:
        if not plist: continue
        lines=[f"• **{p['name']}** {p['rating']} {p['position']} · 🎯{fmt(p['snipe'])} → +{fmt(p['profit'])} ({p['roi']}%)" for p in plist]
        recap.add_field(name=label, value="\n".join(lines), inline=False)
    best=unique[0]
    recap.add_field(name="🏆 MEILLEURE AFFAIRE",
        value=f"**{best['name']}** {best['rating']} {best['position']} | Snipe {fmt(best['snipe'])} → +{fmt(best['profit'])} ({best['roi']}% ROI)",
        inline=False)
    await interaction.followup.send(embed=recap)
    for p in unique[:5]: await interaction.followup.send(embed=alert_embed(p, pl))

@tree.command(name="status", description="📡 Vérifie les sources de données en live")
async def cmd_status(interaction: discord.Interaction):
    await interaction.response.defer()
    e = discord.Embed(title="📡 Test des sources FC26", color=0xFEE75C, timestamp=datetime.utcnow())
    e.add_field(name="🔧 curl-cffi", value="✅ Actif" if CURL_OK else "⚠️ Inactif", inline=True)
    e.add_field(name="🔍 Scans", value=str(stats.get("scans",0)), inline=True)
    e.add_field(name="❌ Erreurs", value=str(stats.get("errors",0)), inline=True)
    e.add_field(name="🕐 Dernier scan", value=stats.get("last","jamais"), inline=True)
    e.add_field(name="📊 Dernière source", value=stats.get("source","—"), inline=True)
    await interaction.followup.send(embed=e)
    # Test live chaque source
    for src_fn, name, url in [
        (fetch_futgg, "FUT.GG", "fut.gg"),
        (fetch_futwiz, "Futwiz", "futwiz.com"),
        (fetch_futbin, "Futbin", "futbin.com"),
    ]:
        try:
            t0 = datetime.utcnow()
            players = await src_fn({}, "pc")
            sec = (datetime.utcnow()-t0).seconds
            if players:
                r = discord.Embed(title=f"✅ {name} — {len(players)} joueurs en {sec}s", color=0x57F287)
                lines=[f"• **{p['name']}** {p['rating']} {p['position']} — Snipe {fmt(p['snipe'])} · +{fmt(p['profit'])}" for p in players[:4]]
                r.add_field(name="Top résultats", value="\n".join(lines), inline=False)
            else:
                r = discord.Embed(title=f"❌ {name} — Bloqué ou vide", color=0xED4245)
            await interaction.followup.send(embed=r)
        except Exception as ex:
            await interaction.followup.send(embed=discord.Embed(title=f"❌ {name} — Erreur: {str(ex)[:100]}", color=0xED4245))
        await asyncio.sleep(2)

@tree.command(name="stats", description="📊 Statistiques du bot")
async def cmd_stats(interaction: discord.Interaction):
    e = discord.Embed(title="📊 FC26 Bot — Stats", color=0x5865F2, timestamp=datetime.utcnow())
    e.add_field(name="🔔 Alertes", value=str(stats.get("sent",0)), inline=True)
    e.add_field(name="🔍 Scans", value=str(stats.get("scans",0)), inline=True)
    e.add_field(name="💰 Profit affiché", value=f"{fmt(stats.get('profit',0))} coins", inline=True)
    e.add_field(name="📊 Source active", value=stats.get("source","—"), inline=True)
    await interaction.response.send_message(embed=e)

@tree.command(name="aide", description="❓ Guide complet du bot")
async def cmd_aide(interaction: discord.Interaction):
    e = discord.Embed(title="🤖 FC26 Ultimate Trading Bot", color=0x5865F2,
        description="3 sources de données · FUT.GG + Futwiz + Futbin · Taxe EA 5%")
    e.add_field(name="⚙️ Setup", value="`/plateforme` · `/setchannel #salon`", inline=False)
    e.add_field(name="🔍 Scans", value="`/scan` · `/snipe [budget]` · `/erreurs [%]`\n`/meta [note]` · `/premium` · `/budget [max]` · `/top`", inline=False)
    e.add_field(name="🎯 Filtres", value="`/position ST` · `/nation France` · `/ligue Premier League`\n`/joueur Mbappe` · `/meilleursfiltre`", inline=False)
    e.add_field(name="📡 Debug", value="`/status` — test live des 3 sources", inline=False)
    e.add_field(name="💡 Démarrage", value="1. `/plateforme PC`\n2. `/setchannel #alertes toutes`\n3. `/status` pour vérifier\n4. Profit automatique ! 💰", inline=False)
    await interaction.response.send_message(embed=e)

# ── Scan automatique ──────────────────────────────────────────────────────────
@tasks.loop(seconds=SCAN_DELAY)
async def auto_scan():
    stats["scans"] = stats.get("scans",0)+1
    stats["last"]  = datetime.utcnow().strftime("%H:%M:%S")
    for guild in bot.guilds:
        gid = str(guild.id)
        for platform, channel_id in channels.get(gid,{}).items():
            ch = bot.get_channel(channel_id)
            if not ch: continue
            try:
                players = await scrape_futbin(platform=platform)
                sent=0
                for p in players:
                    if sent>=MAX_ALERTS: break
                    key=f"{gid}_{platform}_{p['id']}_{p['snipe']}"
                    if key in sent_alerts: continue
                    await ch.send(embed=alert_embed(p, platform))
                    sent_alerts[key] = datetime.utcnow().isoformat()
                    stats["sent"]   = stats.get("sent",0)+1
                    stats["profit"] = stats.get("profit",0)+p["profit"]
                    sent+=1
                    await asyncio.sleep(2)
                if len(sent_alerts)>1000:
                    ks=list(sent_alerts.keys())
                    for k in ks[:-800]: del sent_alerts[k]
                _save("sent_alerts.json",sent_alerts)
                _save("stats.json",stats)
            except Exception as ex:
                log.error(f"auto_scan [{platform}]: {ex}")
        await asyncio.sleep(8)

@bot.event
async def on_ready():
    log.info(f"Connecté : {bot.user}")
    for guild in bot.guilds:
        try:
            synced = await tree.sync(guild=guild)
            log.info(f"✅ {len(synced)} commandes → {guild.name}")
        except Exception as ex:
            log.warning(f"sync: {ex}")
    try:
        g = await tree.sync()
        log.info(f"✅ {len(g)} commandes global")
    except: pass
    auto_scan.start()
    log.info(f"🤖 Prêt | curl-cffi:{CURL_OK} | scan/{SCAN_DELAY}s")
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="marché FC26 💹"))

if __name__ == "__main__":
    bot.run(TOKEN)
