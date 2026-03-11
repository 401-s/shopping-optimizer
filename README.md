# 🛒 Shopping List Optimizer

Optimaliseert automatisch je Home Assistant shopping list via Claude AI:
- Voegt duplicaten samen (ook bij verschillende spellingen of hoeveelheden)
- Groepeert items op categorie (Groente & Fruit, Vlees & Vis, etc.)
- Draait als Docker container op Unraid
- Triggerbaar via een knop op je HA dashboard

## Installatie

### 1. Maak de map aan op Unraid
```bash
mkdir -p /mnt/user/appdata/shopping-optimizer
```

### 2. Maak een .env bestand aan
> **Belangrijk:** Dit moet bestaan vóórdat je de container start.

```bash
nano /mnt/user/appdata/shopping-optimizer/.env
```

Vul je tokens in:
```
HA_URL=http://jouw_ha_ip:8123
HA_TOKEN=jouw_ha_token
ANTHROPIC_API_KEY=jouw_anthropic_key

# Optioneel
WEBHOOK_SECRET=kies_een_sterk_geheim
CLAUDE_MODEL=claude-opus-4-5
REQUEST_TIMEOUT=10
PORT=8099
```

### 3. Maak een docker-compose.yml aan
```bash
nano /mnt/user/appdata/shopping-optimizer/docker-compose.yml
```

Plak de volgende inhoud:
```yaml
# Locatie op Unraid: /mnt/user/appdata/shopping-optimizer
# Zet hier ook je .env bestand neer

services:
  shopping-optimizer:
    build: https://github.com/401-s/shopping-optimizer.git
    container_name: shopping-optimizer
    restart: unless-stopped
    ports:
      - "${PORT:-8099}:${PORT:-8099}"
    env_file:
      - /mnt/user/appdata/shopping-optimizer/.env
    environment:
      - PORT=${PORT:-8099}
```

> Docker haalt de broncode automatisch van GitHub — je hoeft de repo niet te clonen.

### 4. Start de container via Unraid Compose Manager
- Ga in Unraid naar **Apps → Compose Manager**
- Klik op **Add New Stack**
- Geef de stack een naam (bijv. `shopping-optimizer`)
- Stel het pad in op `/mnt/user/appdata/shopping-optimizer`
- Klik op **Compose Up**

### 5. Configureer Home Assistant
Voeg de inhoud van `ha_configuration.yaml` toe aan je `configuration.yaml` en herstart HA.

Voeg daarna een Button card toe aan je dashboard:
```yaml
type: button
name: Optimaliseer boodschappenlijst
icon: mdi:cart-check
tap_action:
  action: perform-action
  perform_action: script.optimize_shopping_list
```

## Endpoints

| Methode | URL | Beschrijving |
|---------|-----|--------------|
| GET | `/health` | Controleert of de container draait |
| POST | `/optimize` | Start de optimalisatie |

## Tokens aanmaken

**Home Assistant token:**
1. Ga naar je HA profiel → Long-Lived Access Tokens
2. Klik op Create Token

**Anthropic API key:**
1. Ga naar https://console.anthropic.com/settings/keys
2. Klik op Create Key
