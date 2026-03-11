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

### 2. Maak een docker-compose.yml aan
```bash
nano /mnt/user/appdata/shopping-optimizer/docker-compose.yml
```

Plak de volgende inhoud en vul je eigen waarden in:
```yaml
# Locatie op Unraid: /mnt/user/appdata/shopping-optimizer

services:
  shopping-optimizer:
    build: https://github.com/401-s/shopping-optimizer.git
    container_name: shopping-optimizer
    restart: unless-stopped
    ports:
      - "8099:8099"
    environment:
      - HA_URL=http://jouw_ha_ip:8123
      - HA_TOKEN=jouw_ha_token
      - ANTHROPIC_API_KEY=jouw_anthropic_key
      - WEBHOOK_SECRET=kies_een_sterk_geheim
      - CLAUDE_MODEL=claude-opus-4-5
      - REQUEST_TIMEOUT=10
      - PORT=8099
```

> Docker haalt de broncode automatisch van GitHub — je hoeft de repo niet te clonen.

### 3. Start de container via Unraid Compose Manager
- Ga in Unraid naar **Apps → Compose Manager**
- Klik op **Add New Stack**
- Geef de stack een naam (bijv. `shopping-optimizer`)
- Stel het pad in op `/mnt/user/appdata/shopping-optimizer`
- Klik op **Compose Up**

### 4. Configureer Home Assistant
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
