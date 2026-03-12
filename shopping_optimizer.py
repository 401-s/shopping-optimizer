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
os.makedirs("/app/data/logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/app/data/logs/shopping-optimizer.log")
    ]
)
log = logging.getLogger(__name__)

# ── Configuratie ──────────────────────────────────────────────────────────────
HA_URL            = os.getenv("HA_URL")
HA_TOKEN          = os.getenv("HA_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL      = os.getenv("CLAUDE_MODEL", "claude-opus-4-5")
WEBHOOK_SECRET    = os.getenv("WEBHOOK_SECRET")
PORT              = int(os.getenv("PORT", "8099"))
REQUEST_TIMEOUT   = int(os.getenv("REQUEST_TIMEOUT", "10"))

HEADERS = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json",
}

# ── Home Assistant API ────────────────────────────────────────────────────────

def get_shopping_list() -> list[dict]:
    """Haal alle niet-aangevinkte items op uit de HA shopping list."""
    resp = requests.get(f"{HA_URL}/api/shopping_list", headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return [item for item in resp.json() if not item.get("complete", False)]


def delete_item(item_name: str):
    """Verwijder één item op basis van naam via de todo service."""
    resp = requests.post(
        f"{HA_URL}/api/services/todo/remove_item",
        headers=HEADERS,
        json={
            "entity_id": "todo.shopping_list",
            "item": item_name
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()


def add_item(name: str):
    """Voeg een item toe aan de HA shopping list."""
    resp = requests.post(
        f"{HA_URL}/api/shopping_list/item",
        headers=HEADERS,
        json={"name": name},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()


def notify_preview(categories: dict[str, list[str]], original_count: int):
    """Stuur een persistente HA-notificatie met het optimalisatievoorstel."""
    lines = [f"**{len(sum(categories.values(), []))} items** (was {original_count}):\n"]
    for cat, cat_items in sorted(categories.items()):
        lines.append(f"**{cat}**")
        for name in cat_items:
            lines.append(f"- {name}")
    lines.append("\nBevestig via *Bevestig optimalisatie* of annuleer via *Annuleer optimalisatie*.")

    requests.post(
        f"{HA_URL}/api/services/persistent_notification/create",
        headers=HEADERS,
        json={
            "title": "Optimalisatie voorstel",
            "message": "\n".join(lines),
            "notification_id": "shopping_optimizer_preview",
        },
        timeout=REQUEST_TIMEOUT,
    )


def dismiss_preview_notification():
    """Verwijder de preview-notificatie na bevestigen of annuleren."""
    requests.post(
        f"{HA_URL}/api/services/persistent_notification/dismiss",
        headers=HEADERS,
        json={"notification_id": "shopping_optimizer_preview"},
        timeout=REQUEST_TIMEOUT,
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
        model=CLAUDE_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )

    response_text = message.content[0].text.strip()
    # Verwijder optionele markdown code fences (```json ... ``` of ``` ... ```)
    if response_text.startswith("```"):
        response_text = response_text.lstrip("`")
        if response_text.startswith("json"):
            response_text = response_text[4:]
        if response_text.endswith("```"):
            response_text = response_text[:-3]
        response_text = response_text.strip()

    return json.loads(response_text)


# ── Pending changes (in-memory) ───────────────────────────────────────────────

_pending: dict = {}  # Slaat preview resultaat op totdat bevestigd of geannuleerd


# ── Optimizer logica ──────────────────────────────────────────────────────────

def _build_categories(optimized: list[dict]) -> dict[str, list[str]]:
    categories: dict[str, list[str]] = {}
    for item in optimized:
        cat = item["category"]
        categories.setdefault(cat, []).append(item["name"])
    return categories


def _apply_changes(original_items: list[dict], categories: dict[str, list[str]]):
    """Verwijder oude items en voeg geoptimaliseerde items toe aan HA."""
    log.info("Oude items verwijderen...")
    for item in original_items:
        delete_item(item["name"])

    log.info("Nieuwe items toevoegen...")
    for cat, cat_items in sorted(categories.items()):
        for name in cat_items:
            add_item(f"[{cat}] {name}")


def run_preview() -> dict:
    """
    Haal de lijst op en laat Claude optimaliseren, maar sla de wijzigingen op
    zonder ze toe te passen. Geeft een preview terug ter bevestiging.
    """
    global _pending

    log.info("Ophalen van HA shopping list (preview)...")
    items = get_shopping_list()

    if not items:
        log.info("Lijst is leeg, niets te doen.")
        _pending = {}
        return {"status": "ok", "message": "Lijst is leeg, niets te doen.", "items": 0}

    log.info(f"{len(items)} items gevonden: {[i['name'] for i in items]}")

    log.info("Claude optimaliseert de lijst (preview)...")
    optimized = optimize_with_claude(items)
    categories = _build_categories(optimized)

    log.info(f"Voorgestelde lijst ({len(optimized)} items):")
    for cat, cat_items in sorted(categories.items()):
        log.info(f"  [{cat}] {', '.join(cat_items)}")

    _pending = {"original_items": items, "categories": categories}

    notify_preview(categories, len(items))

    return {
        "status": "preview",
        "message": "Bekijk de voorgestelde wijzigingen. Stuur POST /confirm om toe te passen of POST /cancel om te annuleren.",
        "original_count": len(items),
        "optimized_count": len(optimized),
        "categories": {cat: items for cat, items in categories.items()}
    }


def run_confirm() -> dict:
    """Pas de opgeslagen preview-wijzigingen toe."""
    global _pending

    if not _pending:
        return {"status": "error", "message": "Geen wijzigingen klaar. Stuur eerst een POST naar /preview."}

    original_items = _pending["original_items"]
    categories = _pending["categories"]
    optimized_count = sum(len(v) for v in categories.values())

    _apply_changes(original_items, categories)
    _pending = {}
    dismiss_preview_notification()

    log.info("✅ Optimalisatie bevestigd en toegepast!")
    return {
        "status": "ok",
        "message": "Shopping list geoptimaliseerd!",
        "original_count": len(original_items),
        "optimized_count": optimized_count,
        "categories": {cat: items for cat, items in categories.items()}
    }


def run_cancel() -> dict:
    """Gooi de opgeslagen preview-wijzigingen weg."""
    global _pending

    if not _pending:
        return {"status": "ok", "message": "Geen wijzigingen om te annuleren."}

    _pending = {}
    dismiss_preview_notification()
    log.info("Optimalisatie geannuleerd, lijst ongewijzigd.")
    return {"status": "ok", "message": "Wijzigingen geannuleerd. Lijst is niet aangepast."}


def run_optimizer() -> dict:
    """Voer de volledige optimalisatie direct uit (zonder bevestigingsstap)."""
    log.info("Ophalen van HA shopping list...")
    items = get_shopping_list()

    if not items:
        log.info("Lijst is leeg, niets te doen.")
        return {"status": "ok", "message": "Lijst is leeg, niets te doen.", "items": 0}

    log.info(f"{len(items)} items gevonden: {[i['name'] for i in items]}")

    log.info("Claude optimaliseert de lijst...")
    optimized = optimize_with_claude(items)
    categories = _build_categories(optimized)

    log.info(f"Geoptimaliseerde lijst ({len(optimized)} items):")
    for cat, cat_items in sorted(categories.items()):
        log.info(f"  [{cat}] {', '.join(cat_items)}")

    _apply_changes(items, categories)

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

    def _authorized(self) -> bool:
        if not WEBHOOK_SECRET:
            return True
        token = self.headers.get("X-Webhook-Secret", "")
        return token == WEBHOOK_SECRET

    def do_POST(self):
        if not self._authorized():
            log.warning(f"Ongeautoriseerde aanvraag van {self.address_string()}")
            self.send_json(401, {"error": "Ongeautoriseerd"})
            return

        handlers = {
            "/optimize": ("🛒 Optimize aanvraag ontvangen van HA", run_optimizer),
            "/preview":  ("🔍 Preview aanvraag ontvangen van HA", run_preview),
            "/confirm":  ("✅ Confirm aanvraag ontvangen van HA", run_confirm),
            "/cancel":   ("❌ Cancel aanvraag ontvangen van HA", run_cancel),
        }

        if self.path in handlers:
            msg, fn = handlers[self.path]
            log.info(msg)
            try:
                result = fn()
                self.send_json(200, result)
            except Exception as e:
                log.error(f"Fout tijdens {self.path}: {e}")
                self.send_json(500, {"status": "error", "message": str(e)})
        else:
            self.send_json(404, {"error": "Niet gevonden"})


# ── Start ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not HA_URL:
        raise RuntimeError("HA_URL niet ingesteld")
    if not HA_TOKEN:
        raise RuntimeError("HA_TOKEN niet ingesteld")
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY niet ingesteld")

    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    log.info(f"🚀 Shopping List Optimizer gestart op poort {PORT}")
    log.info(f"   POST http://0.0.0.0:{PORT}/optimize  → optimaliseer lijst")
    log.info(f"   GET  http://0.0.0.0:{PORT}/health    → health check")
    server.serve_forever()
