# 🛒 Shopping List Optimizer

Optimaliseert je Home Assistant boodschappenlijst via Claude AI:
- Voegt duplicaten samen (ook bij verschillende spellingen of hoeveelheden)
- Groepeert items op categorie, in de volgorde van een looproute door de supermarkt (zelf aan te passen)
- Vaste afspraken per product, bijv. hagelslag altijd bij *Brood & Bakkerij*
- Houdt bij wat elke optimalisatie kost (per run en per maand)
- Optioneel met bevestigingsstap: eerst een voorstel als HA-notificatie, daarna bevestigen of annuleren
- Optioneel een melding op je telefoon met knoppen *Bevestig* / *Annuleer* / *Ongedaan maken*
- Laatste optimalisatie ongedaan maken met één knop
- Draait als Docker container op Unraid
- Triggerbaar via knoppen op je HA dashboard

Vereist Home Assistant 2024.10 of nieuwer (gebruikt de `todo.*` services en de nieuwe automation-syntax).

## Veiligheid van je lijst

- Nieuwe items worden eerst toegevoegd; pas daarna worden de originele items verwijderd (op uid). Gaat er halverwege iets mis, dan raak je niets kwijt.
- Claude moet bij elk item aangeven uit welke originele items het bestaat. Valt er een item weg, dan wordt het voorstel afgekeurd en blijft de lijst ongewijzigd.
- Is de lijst tussen voorstel en bevestiging gewijzigd (item afgevinkt of verwijderd), dan wordt het voorstel geweigerd en moet je een nieuw voorstel maken.
- Eerdere categorie-prefixen (`[Groente & Fruit] ...`) worden gestript, dus je kunt de optimizer vaker achter elkaar draaien.
- Na elke optimalisatie wordt de oude lijst bewaard. `/undo` zet hem terug, zolang de toegevoegde items nog op de lijst staan; items die je daarna zelf hebt toegevoegd blijven staan.

## Installatie

### 1. Maak de map aan op Unraid
```bash
mkdir -p /mnt/user/appdata/shopping-optimizer
chown -R 99:100 /mnt/user/appdata/shopping-optimizer
```

> De container draait als `nobody:users` (99:100). Zonder de `chown` kan hij geen logs en openstaande voorstellen opslaan.

### 2. Maak een docker-compose.yml aan
```bash
nano /mnt/user/appdata/shopping-optimizer/docker-compose.yml
```

Plak de volgende inhoud en vul je eigen waarden in:
```yaml
services:
  shopping-optimizer:
    image: ghcr.io/401-s/shopping-optimizer:latest
    pull_policy: always
    container_name: shopping-optimizer
    restart: unless-stopped
    ports:
      - "8099:8099"
    environment:
      - HA_URL=http://jouw_ha_ip:8123
      - HA_TOKEN=jouw_ha_token
      - ANTHROPIC_API_KEY=jouw_anthropic_key
      - WEBHOOK_SECRET=kies_een_sterk_geheim
      - CLAUDE_MODEL=claude-opus-5
      - CLAUDE_EFFORT=low
      - CLAUDE_FALLBACKS=default
      - TODO_ENTITY=todo.shopping_list
      - REQUEST_TIMEOUT=10
      - PORT=8099
    volumes:
      - /mnt/user/appdata/shopping-optimizer:/app/data
```

> Het image wordt automatisch gebouwd door GitHub Actions en gepubliceerd op
> `ghcr.io/401-s/shopping-optimizer`. Door `pull_policy: always` haalt elke Compose Up
> de nieuwste versie op.

### 3. Start de container via Unraid Compose Manager
- Ga in Unraid naar **Apps → Compose Manager**
- Klik op **Add New Stack**
- Geef de stack een naam (bijv. `shopping-optimizer`)
- Stel het pad in op `/mnt/user/appdata/shopping-optimizer`
- Klik op **Compose Up**

### 4. Configureer Home Assistant
Voeg de inhoud van `ha_configuration.yaml` toe aan je `configuration.yaml` en herstart HA.
Daarin staan ook voorbeelden voor dashboardknoppen: één knop om direct te optimaliseren, of drie knoppen (voorstel / bevestig / annuleer), plus een knop *Ongedaan maken*.

### 5. (Optioneel) Meldingen met knoppen op je telefoon
1. Zoek de naam van je telefoon-notify-service: in HA onder **Ontwikkelhulpmiddelen → Acties**, zoek op `notify.mobile_app`. Bijvoorbeeld `notify.mobile_app_pixel_8`.
2. Zet in je `docker-compose.yml`: `NOTIFY_SERVICE=mobile_app_pixel_8` en herstart de container.
3. Zorg dat de automation *Boodschappenlijst optimizer — telefoonknoppen* uit `ha_configuration.yaml` in HA staat. Die koppelt de knoppen in de melding aan de optimizer.

Je krijgt dan het voorstel op je telefoon met *Bevestig* en *Annuleer*, en na het toepassen een melding met *Ongedaan maken*.

## Winkelvolgorde en vaste afspraken

Maak (optioneel) `/mnt/user/appdata/shopping-optimizer/categorieen.json` aan; een voorbeeld staat in [`categorieen.example.json`](categorieen.example.json):

```json
{
  "volgorde": ["Groente & Fruit", "Brood & Bakkerij", "Zuivel & Eieren", "Dranken", "Overig"],
  "vast": {
    "hagelslag": "Brood & Bakkerij",
    "wc-papier": "Schoonmaak & Verzorging"
  }
}
```

- **`volgorde`**: de categorieën in de volgorde van jouw looproute. Je kunt categorieën weglaten, hernoemen of toevoegen (bijv. `"Drogisterij"`). `Overig` wordt automatisch achteraan toegevoegd als hij ontbreekt.
- **`vast`**: producten die altijd in een bepaalde categorie horen. Claude krijgt ze mee, en de optimizer dwingt ze daarna ook zelf af. Een afspraak geldt als het product als los woord in de naam staat: `hagelslag` geldt ook voor `2 pakken hagelslag`, maar `melk` niet voor `karnemelk`.

Het bestand wordt bij elke run opnieuw gelezen, dus je hoeft de container niet te herstarten. Staat er een fout in, dan krijg je een HA-notificatie *Optimalisatie mislukt* met uitleg en blijft je lijst ongewijzigd.

## Kosten

Na elke run logt de optimizer het tokengebruik en een schatting van de kosten, plus het totaal van de huidige maand. De kosten staan ook in de voorstel-notificatie en in de melding na direct optimaliseren. De maandtotalen staan in `/app/data/usage.json` en in de uitvoer van `GET /health`.

De schatting gebruikt de standaard API-prijzen die in de code staan (`PRICES`); bij een onbekend model worden alleen de tokens gelogd.

## Configuratie

| Variabele | Verplicht | Standaard | Beschrijving |
|-----------|-----------|-----------|--------------|
| `HA_URL` | ja | | URL van Home Assistant |
| `HA_TOKEN` | ja | | Long-Lived Access Token |
| `ANTHROPIC_API_KEY` | ja | | Anthropic API key |
| `WEBHOOK_SECRET` | ja | | Geheim dat HA meestuurt in de `X-Webhook-Secret` header |
| `CLAUDE_MODEL` | nee | `claude-opus-5` | Claude-model. Goedkoper/sneller kan met `claude-sonnet-5` of `claude-haiku-4-5`; zet dan `CLAUDE_FALLBACKS` leeg (en bij Haiku ook `CLAUDE_EFFORT`) |
| `CLAUDE_EFFORT` | nee | `low` | Denkinspanning (`low`–`max`). Leeg laten voor modellen die dit niet ondersteunen (zoals Haiku) |
| `CLAUDE_FALLBACKS` | nee | leeg (uit) | `default` laat de API een geweigerd verzoek automatisch op een ander model herhalen. Alleen voor Claude Opus 5 / Fable; de voorbeeld-compose zet het aan |
| `TODO_ENTITY` | nee | `todo.shopping_list` | Welke todo-lijst geoptimaliseerd wordt |
| `NOTIFY_SERVICE` | nee | leeg (uit) | Notify-service van je telefoon, bijv. `mobile_app_pixel_8`, voor meldingen met knoppen |
| `REQUEST_TIMEOUT` | nee | `10` | Timeout (seconden) voor aanroepen naar HA |
| `PORT` | nee | `8099` | Poort van de webserver |
| `DATA_DIR` | nee | `/app/data` | Map voor logs, openstaand voorstel, undo, kosten en `categorieen.json` |

## Endpoints

Alle POST-endpoints vereisen de header `X-Webhook-Secret`. Er draait maximaal één actie tegelijk; een tweede aanvraag krijgt `409`.

| Methode | URL | Beschrijving |
|---------|-----|--------------|
| GET | `/health` | Controleert of de container draait; toont ook de kosten per maand |
| POST | `/preview` | Maakt op de achtergrond een voorstel en toont het als HA-notificatie (`202`) |
| POST | `/confirm` | Past het openstaande voorstel toe |
| POST | `/cancel` | Gooit het openstaande voorstel weg |
| POST | `/optimize` | Optimaliseert direct, zonder bevestiging, op de achtergrond (`202`) |
| POST | `/undo` | Draait de laatste optimalisatie terug |

Fouten bij achtergrondtaken verschijnen als HA-notificatie *Optimalisatie mislukt* en in `/app/data/logs/shopping-optimizer.log`.

## Updaten en terug naar een vorige versie

Updaten: **Compose Down → Compose Up** in Compose Manager; `pull_policy: always` haalt dan de nieuwste `latest` op.

Beschikbare tags:

| Tag | Wat |
|-----|-----|
| `latest` | Laatste versie van `main` |
| `sha-abc1234` | Precies één commit, handig om terug te gaan |
| `1.2.0` / `1.2` | Een versie, als er een tag `v1.2.0` is gezet |

Terug naar een vorige versie: zet in je compose bijvoorbeeld
`image: ghcr.io/401-s/shopping-optimizer:sha-abc1234` en doe Compose Down → Up.
De beschikbare tags staan op https://github.com/401-s/shopping-optimizer/pkgs/container/shopping-optimizer.

## Tokens aanmaken

**Home Assistant token:**
1. Ga naar je HA profiel → Long-Lived Access Tokens
2. Klik op Create Token

**Anthropic API key:**
1. Ga naar https://console.anthropic.com/settings/keys
2. Klik op Create Key

## Ontwikkelen

```bash
pip install -r requirements-dev.txt
pytest
```

Bij elke push naar `main` en elke pull request draait GitHub Actions de tests en bouwt het Docker image.
Na een geslaagde push naar `main` (of een versietag zoals `v1.2.0`) wordt het image gepubliceerd op
`ghcr.io/401-s/shopping-optimizer`.
