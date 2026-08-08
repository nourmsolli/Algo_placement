"""
main.py - Web UI
=================
Interface web du projet, en plus de l'API (Swagger) exposée par l'API Gateway :

  - Formulaire utilisateur : demande de placement de VM, sans toucher à Swagger.
  - Espace admin : historique des réservations, verrous de région actifs,
    inventaire vSphere par backend, santé des microservices.

Ce service ne contient aucune logique métier : il relaie les appels aux
microservices existants (API Gateway, Booking Service, Inventory Service,
+ /health de chacun) et met le résultat en forme.
"""

import os
import logging
import httpx
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("web_ui")

app = FastAPI(title="VM Placement - Web UI", version="1.0.0")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

API_GATEWAY_URL = os.getenv("API_GATEWAY_URL", "http://api_gateway:8000")
# URL jointe depuis le navigateur de l'utilisateur (donc l'hôte exposé, pas le
# nom de service interne au réseau Docker utilisé par API_GATEWAY_URL).
SWAGGER_URL = os.getenv("API_GATEWAY_PUBLIC_URL", "http://localhost:8000/docs")
templates.env.globals["swagger_url"] = SWAGGER_URL

# Services surveillés dans l'onglet "Santé" de l'admin (nom affiché -> URL).
MONITORED_SERVICES = {
    "api_gateway": API_GATEWAY_URL,
    "orchestrator": os.getenv("ORCHESTRATOR_URL", "http://orchestrator:8000"),
    "inventory_service": os.getenv("INVENTORY_SERVICE_URL", "http://inventory_service:8000"),
    "filter_service": os.getenv("FILTER_SERVICE_URL", "http://filter_service:8000"),
    "booking_service": os.getenv("BOOKING_SERVICE_URL", "http://booking_service:8000"),
    "affinity_service": os.getenv("AFFINITY_SERVICE_URL", "http://affinity_service:8000"),
    "drs_adapter_service": os.getenv("DRS_ADAPTER_SERVICE_URL", "http://drs_adapter_service:8000"),
    "scoring_service": os.getenv("SCORING_SERVICE_URL", "http://scoring_service:8000"),
}

BOOKING_SERVICE_URL = MONITORED_SERVICES["booking_service"]
INVENTORY_SERVICE_URL = MONITORED_SERVICES["inventory_service"]

OFFER_CODES = ["PRF", "WS", "GPU", "ECO"]


@app.get("/health")
def health():
    return {"status": "ok", "service": "web_ui"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/request", response_class=HTMLResponse)
def request_form(request: Request):
    return templates.TemplateResponse(
        request,
        "request.html",
        {"offer_codes": OFFER_CODES, "form": {}, "result": None, "error": None},
    )


@app.post("/request", response_class=HTMLResponse)
async def submit_request(
    request: Request,
    backend: str = Form(...),
    region: str = Form(...),
    offer_code: str = Form(...),
    nb_vms: int = Form(1),
    cpu_size: int = Form(0),
    ram_size: int = Form(0),
    storage_size: int = Form(0),
    target_placement: str = Form("cluster"),
    anti_affinity: str = Form(""),
    vm_group: str = Form(""),
    gpu_memory: str = Form(""),
    upgrade_domains: str = Form(""),
):
    payload = {
        "region": region,
        "offer_code": offer_code,
        "nb_vms": nb_vms,
        "cpu_size": cpu_size,
        "ram_size": ram_size,
        "storage_size": storage_size,
        "target_placement": target_placement,
        "anti_affinity": anti_affinity or None,
        "vm_group": vm_group or None,
        "gpu_memory": int(gpu_memory) if gpu_memory.strip() else None,
        "upgrade_domains": [d.strip() for d in upgrade_domains.split(",") if d.strip()] or None,
    }
    form_values = {**payload, "backend": backend}

    result, error = None, None
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{API_GATEWAY_URL}/api/v1/placements",
                params={"backend": backend},
                json=payload,
                timeout=900.0,
            )
            response.raise_for_status()
            result = response.json()
        except httpx.HTTPStatusError as e:
            detail = e.response.json().get("detail", str(e)) if e.response.content else str(e)
            error = f"Le pipeline de placement a refusé la demande : {detail}"
        except httpx.RequestError as e:
            error = f"API Gateway injoignable ({API_GATEWAY_URL}) : {e}"

    return templates.TemplateResponse(
        request,
        "request.html",
        {
            "offer_codes": OFFER_CODES,
            "form": form_values,
            "result": result,
            "error": error,
        },
    )


# ---------------------------------------------------------------------------
# Espace admin
# ---------------------------------------------------------------------------

@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    return templates.TemplateResponse(request, "admin/dashboard.html")


@app.get("/admin/bookings", response_class=HTMLResponse)
async def admin_bookings(request: Request):
    bookings, error = [], None
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(f"{BOOKING_SERVICE_URL}/bookings", timeout=10.0)
            response.raise_for_status()
            bookings = response.json()
        except httpx.HTTPError as e:
            error = f"Booking Service injoignable : {e}"
    return templates.TemplateResponse(
        request, "admin/bookings.html", {"bookings": bookings, "error": error}
    )


@app.get("/admin/locks", response_class=HTMLResponse)
async def admin_locks(request: Request):
    locks, error = [], None
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(f"{BOOKING_SERVICE_URL}/locks", timeout=10.0)
            response.raise_for_status()
            locks = response.json()
        except httpx.HTTPError as e:
            error = f"Booking Service injoignable : {e}"
    return templates.TemplateResponse(
        request, "admin/locks.html", {"locks": locks, "error": error}
    )


@app.get("/admin/inventory", response_class=HTMLResponse)
async def admin_inventory(request: Request, backend: str = "DC1"):
    inventory, error = None, None
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(f"{INVENTORY_SERVICE_URL}/inventory/{backend}", timeout=60.0)
            response.raise_for_status()
            inventory = response.json()
        except httpx.HTTPStatusError as e:
            detail = e.response.json().get("detail", str(e)) if e.response.content else str(e)
            error = detail
        except httpx.RequestError as e:
            error = f"Inventory Service injoignable : {e}"
    return templates.TemplateResponse(
        request,
        "admin/inventory.html",
        {"backend": backend, "inventory": inventory, "error": error},
    )


@app.get("/admin/health", response_class=HTMLResponse)
async def admin_health(request: Request):
    statuses = []
    async with httpx.AsyncClient() as client:
        for name, url in MONITORED_SERVICES.items():
            try:
                response = await client.get(f"{url}/health", timeout=5.0)
                ok = response.status_code == 200
                statuses.append({"name": name, "url": url, "ok": ok})
            except httpx.RequestError:
                statuses.append({"name": name, "url": url, "ok": False})
    return templates.TemplateResponse(request, "admin/health.html", {"statuses": statuses})
