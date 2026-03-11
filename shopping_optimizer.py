#!/usr/bin/env python3
"""
Shopping List Optimizer voor Home Assistant
- Draait als webserver op poort 8099
- HA stuurt een POST naar /optimize om de optimizer te starten
- Haalt de shopping list op via de HA REST API
- Voegt duplicaten samen (slim, via Claude)
- Groepeert items op categorie
- Schrijft de opgeschoonde lijst terug naar HA
"""

import os
import json
import logging
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from anthropic import Anthropic

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── Configuratie ──────────────────────────────────────────────────────────────
HA_URL            = os.getenv("HA_URL", "http://192.168.1.108:8123")
HA_TOKEN          = os.getenv("HA_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
PORT              = int(os.getenv("PORT", "8099"))

HEADERS = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json",
}

# ── Home Assistant API ────────────────────────────────────────────────────────

def get_shopping_list() -> list[dict]:
    """Haal alle niet-aangevinkte items op uit de HA shopping list."""
    resp = requests.get(f"{HA_URL}/api/shopping_list", headers=HEADERS)
    resp.raise_for_status()
    return [item for item in resp.json() if not item.get("complete", False)]


def delete_item(item_name: str):
    """Verwijder één item op basis van naam via de todo service."""
    requests.post(
        f"{HA_URL}/api/services/todo/remove_item",
        headers=HEADERS,
        json={
            "entity_id": "todo.shopping_list",
            "item": item_name
        }
    )


def add_item(name: str):
    """Voeg een item toe aan de HA shopping list."""
    requests.post(
        f"{HA_URL}/api/shopping_list/item",
        headers=HEADERS,
        json={"name": name}
    )


# ── Claude: samenvoegen + categoriseren ──────────────────────────────────────

def optimize_with_claude(items: list[dict]) -> list[dict]:
    """
    Stuur de ruwe shopping list naar Claude.
    Claude voegt duplicaten samen en groepeert op categorie.
    Geeft een lijst terug van: {"name": "2 uien", "category": "Groente & Fruit"}
    """
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    raw_items = [item["name"] for item in items]
    items_text = "\n".join(f"- {name}" for name in raw_items)

    prompt = f"""Je krijgt een boodschappenlijst. Doe het volgende:
1. Voeg duplicaten samen (ook als ze anders gespeld zijn, bijv. "ui" en "2 uien" → "3 uien")
2. Groepeer elk item onder één van deze categorieën:
   - Groente & Fruit
   - Vlees & Vis
   - Zuivel & Eieren
   - Brood & Bakkerij
   - Diepvries
   - Dranken
   - Pasta, Rijst & Granen
   - Sauzen & Conserven
   - Snoep & Snacks
   - Schoonmaak & Verzorging
   - Overig

Geef je antwoord ALLEEN als JSON array, geen uitleg, geen markdown. Formaat:
[
  {{"name": "3 uien", "category": "Groente & Fruit"}},
  ...
]

Boodschappenlijst:
{items_text}"""

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )

    response_text = message.content[0].text.strip()
    if response_text.startswith("```"):
        response_text = response_text.split("```")[1]
        if response_text.startswith("json"):
            response_text = response_text[4:]

    return json.loads(response_text)


# ── Optimizer logica ──────────────────────────────────────────────────────────

def run_optimizer() -> dict:
    """Voer de volledige optimalisatie uit. Geeft een resultaat dict terug."""
    log.info("Ophalen van HA shopping list...")
    items = get_shopping_list()

    if not items:
        log.info("Lijst is leeg, niets te doen.")
        return {"status": "ok", "message": "Lijst is leeg, niets te doen.", "items": 0}

    log.info(f"{len(items)} items gevonden: {[i['name'] for i in items]}")

    log.info("Claude optimaliseert de lijst...")
    optimized = optimize_with_claude(items)

    categories: dict[str, list[str]] = {}
    for item in optimized:
        cat = item["category"]
        categories.setdefault(cat, []).append(item["name"])

    log.info(f"Geoptimaliseerde lijst ({len(optimized)} items):")
    for cat, cat_items in sorted(categories.items()):
        log.info(f"  [{cat}] {', '.join(cat_items)}")

    log.info("Oude items verwijderen...")
    for item in items:
        delete_item(item["name"])

    log.info("Nieuwe items toevoegen...")
    for cat, cat_items in sorted(categories.items()):
        for name in cat_items:
            add_item(f"[{cat}] {name}")

    log.info("✅ Optimalisatie voltooid!")
    return {
        "status": "ok",
        "message": "Shopping list geoptimaliseerd!",
        "original_count": len(items),
        "optimized_count": len(optimized),
        "categories": {cat: items for cat, items in categories.items()}
    }


# ── Webhook server ────────────────────────────────────────────────────────────

class WebhookHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        log.info(f"{self.address_string()} - {format % args}")

    def send_json(self, code: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self.send_json(200, {"status": "ok"})
        else:
            self.send_json(404, {"error": "Niet gevonden"})

    def do_POST(self):
        if self.path == "/optimize":
            log.info("🛒 Optimize aanvraag ontvangen van HA")
            try:
                result = run_optimizer()
                self.send_json(200, result)
            except Exception as e:
                log.error(f"Fout tijdens optimalisatie: {e}")
                self.send_json(500, {"status": "error", "message": str(e)})
        else:
            self.send_json(404, {"error": "Niet gevonden"})


# ── Start ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not HA_TOKEN:
        raise RuntimeError("HA_TOKEN niet ingesteld")
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY niet ingesteld")

    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    log.info(f"🚀 Shopping List Optimizer gestart op poort {PORT}")
    log.info(f"   POST http://192.168.1.108:{PORT}/optimize  → optimaliseer lijst")
    log.info(f"   GET  http://192.168.1.108:{PORT}/health    → health check")
    server.serve_forever()
