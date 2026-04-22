# Image Python officielle avec Debian — pip fonctionne parfaitement
FROM python:3.11-slim

# Dépendances système pour Chrome/Playwright
RUN apt-get update && apt-get install -y \
    wget curl gnupg \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libxkbcommon0 \
    libx11-6 libxcomposite1 libxdamage1 libxext6 \
    libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libexpat1 libxcb1 \
    fonts-liberation libappindicator3-1 \
    --no-install-recommends && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Installe les dépendances Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Installe Playwright + Chrome
RUN playwright install chromium
RUN playwright install-deps chromium

# Copie le code
COPY . .

CMD ["python", "bot.py"]
