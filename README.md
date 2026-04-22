# 💹 FC26 Ultimate Trading Bot

Alertes instantanées dès qu'une bonne affaire est trouvée sur Futbin.
PC · Xbox · PlayStation · Scan permanent 24/24

---

## 🚀 Mise en ligne sur Railway (depuis Android)

### 1. Créer le bot Discord
1. discord.com/developers → New Application
2. Onglet **Bot** → Add Bot → copie le **Token**
3. Active **Message Content Intent**
4. OAuth2 → scopes: `bot` + `applications.commands`
5. Permissions: `Send Messages`, `Embed Links`, `Use Slash Commands`
6. Invite le bot sur ton serveur

### 2. Mettre les fichiers sur GitHub
- Installe l'app **GitHub** sur Android
- Crée un repo → upload `bot.py`, `requirements.txt`, `railway.toml`

### 3. Déployer sur Railway
1. railway.app → New Project → GitHub repo
2. Variables → ajoute `DISCORD_TOKEN = ton_token`
3. ✅ Le bot tourne 24/24 !

---

## ⚙️ Configuration sur Discord

### Étape 1 — Choisir ta plateforme
```
/plateforme PC
```
ou Xbox ou PlayStation. Le bot utilise les prix de ta plateforme.

### Étape 2 — Définir le salon d'alertes
```
/setchannel #alertes-trading toutes
```
Dès qu'une affaire est trouvée, elle arrive automatiquement dans ce salon.

---

## 📋 Toutes les commandes

| Commande | Description |
|----------|-------------|
| `/plateforme` | Choisis PC / Xbox / PlayStation |
| `/setchannel` | Salon pour les alertes automatiques |
| `/scan` | Scan manuel immédiat |
| `/snipe [budget]` | Affaires dans ton budget |
| `/erreurs [%]` | Cartes massivement sous-évaluées |
| `/meta [note]` | Top cartes compétitives |
| `/premium [prix]` | Grosses cartes, gros profits |
| `/budget [max]` | Petit budget, volume élevé |
| `/position <POS>` | Filtrer par position |
| `/nation <pays>` | Filtrer par nationalité |
| `/ligue <ligue>` | Filtrer par ligue |
| `/joueur <nom>` | Rechercher un joueur précis |
| `/stats` | Statistiques du bot |
| `/aide` | Guide complet |

---

## 💡 Stratégie recommandée

1. `/plateforme PC` — définis ta plateforme
2. `/setchannel #snipe toutes` — active les alertes auto
3. Les alertes arrivent en temps réel avec : Snipe Price, Sells Price, Profit, photo du joueur
4. Snipe la carte, revends au prix marché → profit garanti après taxe EA 5%

---

## 🔧 Paramètres (bot.py)
```python
MIN_PROFIT   = 500   # Profit minimum pour alerter (coins)
SCAN_DELAY   = 30    # Secondes entre chaque scan
MAX_ALERTS   = 5     # Max alertes par scan (anti-spam)
EA_TAX       = 0.05  # Taxe EA 5%
```
