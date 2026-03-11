# 🛒 Shopping List Optimizer

Optimaliseert automatisch je Home Assistant shopping list via Claude AI:
- Voegt duplicaten samen (ook bij verschillende spellingen of hoeveelheden)
- Groepeert items op categorie (Groente & Fruit, Vlees & Vis, etc.)
- Draait als Docker container op Unraid
- Triggerbaar via een knop op je HA dashboard

## Installatie

### 1. Clone de repo op Unraid
```bash
cd /mnt/user/appdata
git clone https://github.com/JOUW_GEBRUIKERSNAAM/shopping-optimizer
cd shopping-optimizer
```

### 2. Maak een .env bestand aan
```bash
cp .env.example .env
nano .env
```

Vul je tokens in:
```
HA_URL=http://192.168.1.108:8123
HA_TOKEN=jouw_ha_token
ANTHROPIC_API_KEY=jouw_anthropic_key
```

### 3. Start de container via Dockge
- Voeg de map toe in Dockge
- Klik op Deploy

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
