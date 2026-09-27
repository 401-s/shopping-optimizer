#!/usr/bin/env python3
"""
Shopping List Optimizer voor Home Assistant
- Draait als webserver op poort 8099
- HA stuurt een POST naar /preview of /optimize om de optimizer te starten
- Haalt de boodschappenlijst op via de HA todo services
- Voegt duplicaten samen en groepeert items op categorie (via Claude)
- Schrijft de opgeschoonde lijst terug naar HA
"""

import hmac
import json
import logging
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler

import requests
from anthropic import Anthropic

log = logging.getLogger("shopping_optimizer")

# ── Configuratie ──────────────────────────────────────────────────────────────
HA_URL            = (os.getenv("HA_URL") or "").rstrip("/")
HA_TOKEN          = os.getenv("HA_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL      = os.getenv("CLAUDE_MODEL", "claude-opus-5")
CLAUDE_EFFORT     = os.getenv("CLAUDE_EFFORT", "low")          # leeg = niet meesturen
CLAUDE_FALLBACKS  = os.getenv("CLAUDE_FALLBACKS", "")          # "default" = aan (Opus 5 / Fable)
WEBHOOK_SECRET    = os.getenv("WEBHOOK_SECRET")
TODO_ENTITY       = os.getenv("TODO_ENTITY", "todo.shopping_list")
NOTIFY_SERVICE    = os.getenv("NOTIFY_SERVICE", "").removeprefix("notify.")  # bijv. mobile_app_pixel_8
DATA_DIR          = os.getenv("DATA_DIR", "/app/data")
PORT              = int(os.getenv("PORT", "8099"))
REQUEST_TIMEOUT   = int(os.getenv("REQUEST_TIMEOUT", "10"))

HEADERS = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json",
}

# Volgorde = looproute door de supermarkt; zo komt de lijst ook in HA te staan.
CATEGORIES = [
    "Groente & Fruit",
    "Brood & Bakkerij",
    "Vlees & Vis",
    "Zuivel & Eieren",
    "Pasta, Rijst & Granen",
    "Sauzen & Conserven",
    "Snoep & Snacks",
    "Dranken",
    "Schoonmaak & Verzorging",
    "Diepvries",
    "Overig",
]

# Alleen bekende categorie-prefixen strippen, zodat bijv. "[2x] melk" blijft staan.
_PREFIX_RE = re.compile(r"^\[(?:" + "|".join(re.escape(c) for c in CATEGORIES) + r")\]\s*")

NOTIFY_PREVIEW = "shopping_optimizer_preview"
NOTIFY_RESULT  = "shopping_optimizer_result"
PHONE_TAG      = "shopping_optimizer"

# Acties op de telefoonmelding; een HA-automation koppelt ze aan de endpoints.
ACTION_CONFIRM = "SHOPPING_OPTIMIZER_CONFIRM"
ACTION_CANCEL  = "SHOPPING_OPTIMIZER_CANCEL"
ACTION_UNDO    = "SHOPPING_OPTIMIZER_UNDO"


class StaleListError(Exception):
    """De lijst in HA is gewijzigd sinds het voorstel gemaakt werd."""


class PartialApplyError(Exception):
    """Toevoegen liep halverwege mis; originele items staan er nog."""


def strip_prefix(name: str) -> str:
    """Verwijder (eventueel meerdere) categorie-prefixen van een eerdere run."""
    while True:
        stripped = _PREFIX_RE.sub("", name, count=1)
        if stripped == name:
            return name.strip()
        name = stripped


# ── Home Assistant API ────────────────────────────────────────────────────────

def _call_service(domain: str, service: str, data: dict, return_response: bool = False) -> dict:
    url = f"{HA_URL}/api/services/{domain}/{service}"
    if return_response:
        url += "?return_response"
    resp = requests.post(url, headers=HEADERS, json=data, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def get_shopping_list() -> list[dict]:
    """Haal alle niet-afgevinkte items op als [{"uid": ..., "name": ...}]."""
    data = _call_service(
        "todo", "get_items",
        {"entity_id": TODO_ENTITY, "status": "needs_action"},
        return_response=True,
    )
    items = (data.get("service_response") or {}).get(TODO_ENTITY, {}).get("items", [])
    return [
        {"uid": item["uid"], "name": item["summary"]}
        for item in items
        if item.get("status", "needs_action") == "needs_action"
    ]


def add_item(name: str):
    _call_service("todo", "add_item", {"entity_id": TODO_ENTITY, "item": name})


def remove_items(uids: list[str]):
    """Verwijder items op uid in één service-aanroep."""
    if uids:
        _call_service("todo", "remove_item", {"entity_id": TODO_ENTITY, "item": uids})


def notify(title: str, message: str, notification_id: str):
    _call_service("persistent_notification", "create", {
        "title": title,
        "message": message,
        "notification_id": notification_id,
    })


def dismiss(notification_id: str):
    _call_service("persistent_notification", "dismiss", {"notification_id": notification_id})


def notify_phone(title: str, message: str, actions: list[tuple[str, str]] | None = None):
    """Melding via de HA Companion-app, optioneel met actieknoppen. Doet niets zonder NOTIFY_SERVICE."""
    if not NOTIFY_SERVICE:
        return
    data: dict = {"tag": PHONE_TAG}
    if actions:
        data["actions"] = [{"action": action, "title": label} for action, label in actions]
    _call_service("notify", NOTIFY_SERVICE, {"title": title, "message": message, "data": data})


def clear_phone():
    if NOTIFY_SERVICE:
        _call_service("notify", NOTIFY_SERVICE, {"message": "clear_notification", "data": {"tag": PHONE_TAG}})


def _safe(fn, *args):
    """Voor niet-kritieke notificaties: fout loggen in plaats van de run af te breken."""
    try:
        fn(*args)
    except Exception as e:
        log.warning(f"{fn.__name__} mislukt: {e}")


# ── Claude: samenvoegen + categoriseren ──────────────────────────────────────

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "sources": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["name", "category", "sources"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}

_client: Anthropic | None = None


def _anthropic() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


def optimize_with_claude(names: list[str]) -> list[dict]:
    """
    Stuur de ruwe boodschappenlijst naar Claude.
    Geeft een lijst terug van: {"name": "3 uien", "category": "Groente & Fruit", "sources": [1, 4]}
    """
    items_text = "\n".join(f"{i}. {name}" for i, name in enumerate(names, start=1))
    prompt = f"""Je krijgt een genummerde boodschappenlijst. Doe het volgende:
1. Voeg duplicaten samen, ook als ze anders gespeld zijn of een hoeveelheid hebben
   (bijv. "ui" en "2 uien" → "3 uien"). Laat items die geen duplicaat zijn ongewijzigd.
2. Geef elk item precies één categorie uit de toegestane lijst.
3. Vermeld bij elk item in "sources" de nummers van alle originele items waaruit het bestaat.
   Elk origineel nummer moet bij minstens één item voorkomen; voeg geen nieuwe items toe.

Zet de categorie niet in de naam en houd de namen in het Nederlands.

Boodschappenlijst:
{items_text}"""

    output_config = {"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}}
    if CLAUDE_EFFORT:
        output_config["effort"] = CLAUDE_EFFORT
    request = dict(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        messages=[{"role": "user", "content": prompt}],
        output_config=output_config,
    )

    client = _anthropic()
    if CLAUDE_FALLBACKS:
        message = client.beta.messages.create(
            **request,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks=CLAUDE_FALLBACKS,
        )
    else:
        message = client.messages.create(**request)

    if message.stop_reason == "refusal":
        raise RuntimeError("Claude weigerde het verzoek (stop_reason=refusal).")
    if message.stop_reason == "max_tokens":
        raise RuntimeError(
            "Claude's antwoord is afgekapt (max_tokens bereikt). Probeer de lijst te verkleinen."
        )

    text = next((block.text for block in message.content if block.type == "text"), None)
    if text is None:
        raise RuntimeError(f"Geen tekst in Claude-antwoord (stop_reason={message.stop_reason}).")

    try:
        items = json.loads(text)["items"]
    except (json.JSONDecodeError, KeyError, TypeError):
        log.error(f"Kon Claude-antwoord niet parsen (lengte={len(text)}). Laatste 500 tekens: {text[-500:]!r}")
        raise

    validate_result(items, len(names))
    return items


def validate_result(items: list[dict], original_count: int):
    """Controleer dat Claude geen items heeft weggelaten of verzonnen."""
    covered: set[int] = set()
    for item in items:
        item["name"] = strip_prefix(item.get("name", ""))
        if not item["name"]:
            raise ValueError("Claude gaf een item zonder naam terug.")
        if item.get("category") not in CATEGORIES:
            raise ValueError(f"Onbekende categorie van Claude: {item.get('category')!r}")
        sources = item.get("sources") or []
        if not sources:
            raise ValueError(f"Item {item['name']!r} hoort bij geen enkel origineel item.")
        for s in sources:
            if not 1 <= s <= original_count:
                raise ValueError(f"Ongeldig bronnummer {s} bij {item['name']!r}.")
            covered.add(s)

    missing = set(range(1, original_count + 1)) - covered
    if missing:
        raise ValueError(f"Claude liet originele items weg (nummers {sorted(missing)}); lijst niet aangepast.")


# ── Plan maken en toepassen ───────────────────────────────────────────────────

def _build_categories(optimized: list[dict]) -> dict[str, list[str]]:
    """Groepeer op categorie, in winkelvolgorde."""
    categories: dict[str, list[str]] = {cat: [] for cat in CATEGORIES}
    for item in optimized:
        categories[item["category"]].append(item["name"])
    return {cat: names for cat, names in categories.items() if names}


def _count(categories: dict[str, list[str]]) -> int:
    return sum(len(names) for names in categories.values())


def build_plan() -> dict | None:
    """Haal de lijst op en laat Claude een voorstel maken. None als de lijst leeg is."""
    log.info("Ophalen van HA boodschappenlijst...")
    items = get_shopping_list()
    if not items:
        log.info("Lijst is leeg, niets te doen.")
        return None

    names = [strip_prefix(item["name"]) for item in items]
    log.info(f"{len(items)} items gevonden: {names}")

    log.info(f"Claude ({CLAUDE_MODEL}) optimaliseert de lijst...")
    categories = _build_categories(optimize_with_claude(names))

    log.info(f"Voorgestelde lijst ({_count(categories)} items):")
    for cat, cat_items in categories.items():
        log.info(f"  [{cat}] {', '.join(cat_items)}")

    return {"original": items, "categories": categories}


def _replace(remove_uids: list[str], add_names: list[str]):
    """
    Eerst nieuwe items toevoegen, daarna de oude items op uid verwijderen.
    Gaat er halverwege iets mis, dan staan de oude items er nog.
    """
    log.info(f"{len(add_names)} items toevoegen...")
    for added, name in enumerate(add_names):
        try:
            add_item(name)
        except Exception as e:
            done = ", ".join(add_names[:added]) or "geen"
            raise PartialApplyError(
                f"Toevoegen mislukt na {added} van {len(add_names)} items ({e}). "
                f"De oude items staan er nog. Al toegevoegd: {done}. "
                "Verwijder die en probeer het opnieuw."
            ) from e

    log.info(f"{len(remove_uids)} oude items verwijderen...")
    try:
        remove_items(remove_uids)
    except Exception as e:
        raise PartialApplyError(
            f"Alle nieuwe items zijn toegevoegd, maar het verwijderen van de oude items mislukte ({e}). "
            "Verwijder de oude items met de hand."
        ) from e


def apply_plan(plan: dict):
    """Pas een plan toe en bewaar wat nodig is om het ongedaan te maken."""
    current_uids = {item["uid"] for item in get_shopping_list()}
    gone = [o["name"] for o in plan["original"] if o["uid"] not in current_uids]
    if gone:
        raise StaleListError(
            f"De lijst is gewijzigd sinds het voorstel ({', '.join(gone)} niet meer aanwezig). "
            "Maak een nieuw voorstel."
        )

    new_names = [f"[{cat}] {name}" for cat, names in plan["categories"].items() for name in names]
    _replace([o["uid"] for o in plan["original"]], new_names)
    _set_state("undo", {"restore": [o["name"] for o in plan["original"]], "added": new_names})


def undo_last():
    """Zet de lijst terug naar hoe hij was vóór de laatste optimalisatie."""
    undo = _state["undo"]
    uids_by_name: dict[str, list[str]] = {}
    for item in get_shopping_list():
        uids_by_name.setdefault(item["name"], []).append(item["uid"])

    remove_uids = []
    for name in undo["added"]:
        if not uids_by_name.get(name):
            raise StaleListError(
                f"{name!r} staat niet meer op de lijst; ongedaan maken is niet meer veilig mogelijk."
            )
        remove_uids.append(uids_by_name[name].pop())

    _replace(remove_uids, undo["restore"])


def _summary(plan: dict) -> dict:
    return {
        "original_count": len(plan["original"]),
        "optimized_count": _count(plan["categories"]),
        "categories": plan["categories"],
    }


# ── Status op schijf (overleeft een herstart) ─────────────────────────────────
# "pending": voorstel dat wacht op bevestiging
# "undo":    wat nodig is om de laatste optimalisatie terug te draaien

_state: dict[str, dict | None] = {"pending": None, "undo": None}


def _state_file(key: str) -> str:
    return os.path.join(DATA_DIR, f"{key}.json")


def _set_state(key: str, value: dict | None):
    _state[key] = value
    path = _state_file(key)
    try:
        if value is None:
            if os.path.exists(path):
                os.remove(path)
        else:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(value, f, ensure_ascii=False)
            os.replace(tmp, path)
    except OSError as e:
        log.warning(f"Kon {key} niet op schijf bewaren ({e}); het gaat verloren bij een herstart.")


def _load_state():
    for key in _state:
        try:
            with open(_state_file(key), encoding="utf-8") as f:
                _state[key] = json.load(f)
            log.info(f"Opgeslagen {key} geladen van schijf.")
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError) as e:
            log.warning(f"Kon opgeslagen {key} niet laden: {e}")


# ── Acties ────────────────────────────────────────────────────────────────────

def _notify_done(s: dict):
    message = f"{s['optimized_count']} items (was {s['original_count']})."
    _safe(notify, "Boodschappenlijst geoptimaliseerd", message, NOTIFY_RESULT)
    _safe(notify_phone, "Boodschappenlijst geoptimaliseerd", message, [(ACTION_UNDO, "Ongedaan maken")])


def job_preview():
    """Maak een voorstel en toon het als HA-notificatie (achtergrondtaak)."""
    plan = build_plan()
    _set_state("pending", plan)
    if plan is None:
        _safe(dismiss, NOTIFY_PREVIEW)
        _safe(clear_phone)
        notify("Boodschappenlijst", "Lijst is leeg, niets te doen.", NOTIFY_RESULT)
        return

    header = f"{_count(plan['categories'])} items (was {len(plan['original'])})"
    lines = [f"**{header}**:\n"]
    for cat, cat_items in plan["categories"].items():
        lines.append(f"**{cat}**")
        lines.extend(f"- {name}" for name in cat_items)
    lines.append("\nBevestig via *Bevestig optimalisatie* of annuleer via *Annuleer optimalisatie*.")
    notify("Optimalisatie voorstel", "\n".join(lines), NOTIFY_PREVIEW)

    phone_lines = [f"{header}:"]
    phone_lines += [f"{cat}: {', '.join(cat_items)}" for cat, cat_items in plan["categories"].items()]
    _safe(notify_phone, "Optimalisatie voorstel", "\n".join(phone_lines),
          [(ACTION_CONFIRM, "Bevestig"), (ACTION_CANCEL, "Annuleer")])


def job_optimize():
    """Maak een voorstel en pas het direct toe (achtergrondtaak)."""
    plan = build_plan()
    if plan is None:
        return
    apply_plan(plan)
    log.info("✅ Optimalisatie voltooid!")
    _notify_done(_summary(plan))


def _close_preview():
    _set_state("pending", None)
    _safe(dismiss, NOTIFY_PREVIEW)
    _safe(clear_phone)


def run_confirm() -> tuple[int, dict]:
    """Pas het opgeslagen voorstel toe."""
    plan = _state["pending"]
    if not plan:
        return 409, {"status": "error", "message": "Geen voorstel klaar. Stuur eerst een POST naar /preview."}

    try:
        apply_plan(plan)
    except (StaleListError, PartialApplyError) as e:
        # Voorstel weggooien: nog eens bevestigen zou items dubbel toevoegen.
        log.warning(str(e))
        _close_preview()
        _safe(notify_phone, "Optimalisatie mislukt", str(e))
        return 409, {"status": "error", "message": str(e)}

    _close_preview()
    log.info("✅ Optimalisatie bevestigd en toegepast!")
    s = _summary(plan)
    _notify_done(s)
    return 200, {"status": "ok", "message": "Boodschappenlijst geoptimaliseerd!", **s}


def run_cancel() -> tuple[int, dict]:
    """Gooi het opgeslagen voorstel weg."""
    if not _state["pending"]:
        return 200, {"status": "ok", "message": "Geen voorstel om te annuleren."}
    _close_preview()
    log.info("Optimalisatie geannuleerd, lijst ongewijzigd.")
    return 200, {"status": "ok", "message": "Voorstel geannuleerd. Lijst is niet aangepast."}


def run_undo() -> tuple[int, dict]:
    """Draai de laatste optimalisatie terug."""
    undo = _state["undo"]
    if not undo:
        return 409, {"status": "error", "message": "Er is niets om ongedaan te maken."}

    try:
        undo_last()
    except (StaleListError, PartialApplyError) as e:
        # Na een halve mislukking zou nog eens proberen items dubbel toevoegen.
        if isinstance(e, PartialApplyError):
            _set_state("undo", None)
        log.warning(str(e))
        _safe(notify_phone, "Ongedaan maken mislukt", str(e))
        return 409, {"status": "error", "message": str(e)}

    _set_state("undo", None)
    _safe(clear_phone)
    message = f"Optimalisatie ongedaan gemaakt ({len(undo['restore'])} items teruggezet)."
    _safe(notify, "Boodschappenlijst", message, NOTIFY_RESULT)
    log.info("↩️ " + message)
    return 200, {"status": "ok", "message": message, "restored_count": len(undo["restore"])}


# ── Webhook server ────────────────────────────────────────────────────────────

# Eén actie tegelijk: voorkomt dat twee knopdrukken elkaars lijst overschrijven.
_busy = threading.Lock()


def _start_background(path: str, job) -> bool:
    if not _busy.acquire(blocking=False):
        return False

    def worker():
        try:
            job()
        except Exception as e:
            log.exception(f"Fout tijdens {path}")
            _safe(notify, "Optimalisatie mislukt", str(e), NOTIFY_RESULT)
            _safe(notify_phone, "Optimalisatie mislukt", str(e))
        finally:
            _busy.release()

    threading.Thread(target=worker, daemon=True).start()
    return True


class WebhookHandler(BaseHTTPRequestHandler):

    BACKGROUND = {
        "/preview":  ("🔍 Preview aanvraag ontvangen", job_preview),
        "/optimize": ("🛒 Optimize aanvraag ontvangen", job_optimize),
    }
    SYNC = {
        "/confirm": ("✅ Confirm aanvraag ontvangen", run_confirm),
        "/cancel":  ("❌ Cancel aanvraag ontvangen", run_cancel),
        "/undo":    ("↩️ Undo aanvraag ontvangen", run_undo),
    }

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
            self.send_json(200, {"status": "ok", "busy": _busy.locked(),
                                 "pending": bool(_state["pending"]), "undo": bool(_state["undo"])})
        else:
            self.send_json(404, {"error": "Niet gevonden"})

    def _authorized(self) -> bool:
        token = self.headers.get("X-Webhook-Secret", "")
        return hmac.compare_digest(token.encode("utf-8"), (WEBHOOK_SECRET or "").encode("utf-8"))

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)

        if not self._authorized():
            log.warning(f"Ongeautoriseerde aanvraag van {self.address_string()}")
            self.send_json(401, {"error": "Ongeautoriseerd"})
            return

        busy = {"status": "busy", "message": "Er loopt al een optimalisatie, probeer het zo opnieuw."}

        if self.path in self.BACKGROUND:
            msg, job = self.BACKGROUND[self.path]
            log.info(msg)
            if _start_background(self.path, job):
                self.send_json(202, {"status": "accepted",
                                     "message": "Gestart; het resultaat verschijnt als HA-notificatie."})
            else:
                self.send_json(409, busy)
        elif self.path in self.SYNC:
            msg, fn = self.SYNC[self.path]
            log.info(msg)
            if not _busy.acquire(blocking=False):
                self.send_json(409, busy)
                return
            try:
                self.send_json(*fn())
            except Exception as e:
                log.exception(f"Fout tijdens {self.path}")
                self.send_json(500, {"status": "error", "message": str(e)})
            finally:
                _busy.release()
        else:
            self.send_json(404, {"error": "Niet gevonden"})


# ── Start ─────────────────────────────────────────────────────────────────────

def setup_logging():
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    error = None
    try:
        log_dir = os.path.join(DATA_DIR, "logs")
        os.makedirs(log_dir, exist_ok=True)
        handlers.append(RotatingFileHandler(
            os.path.join(log_dir, "shopping-optimizer.log"),
            maxBytes=1_000_000, backupCount=5, encoding="utf-8",
        ))
    except OSError as e:
        error = e
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=handlers)
    if error:
        log.warning(f"Kan niet naar {DATA_DIR} schrijven ({error}); alleen console-logging.")


def main():
    setup_logging()
    for name, value in [("HA_URL", HA_URL), ("HA_TOKEN", HA_TOKEN),
                        ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY), ("WEBHOOK_SECRET", WEBHOOK_SECRET)]:
        if not value:
            raise RuntimeError(f"{name} niet ingesteld")

    _load_state()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), WebhookHandler)
    server.daemon_threads = True
    log.info(f"🚀 Shopping List Optimizer gestart op poort {PORT} (model {CLAUDE_MODEL}, lijst {TODO_ENTITY})")
    log.info("   POST /preview   → maak voorstel (resultaat als HA-notificatie)")
    log.info("   POST /confirm   → pas voorstel toe")
    log.info("   POST /cancel    → annuleer voorstel")
    log.info("   POST /optimize  → optimaliseer direct, zonder bevestiging")
    log.info("   POST /undo      → draai de laatste optimalisatie terug")
    if NOTIFY_SERVICE:
        log.info(f"   Telefoonmeldingen via notify.{NOTIFY_SERVICE}")
    log.info("   GET  /health    → health check")
    server.serve_forever()


if __name__ == "__main__":
    main()
