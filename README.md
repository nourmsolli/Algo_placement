# VM Placement Microservices

Migration du moteur de placement de VM (monolithe Python) vers une architecture
microservices, intégrée à VMware DRS. Projet réalisé dans le cadre du stage
IaaS chez Groupe La Poste.

## 1. Vue d'ensemble

```
Demande de création de VM
        │
        ▼
   API Gateway
        │
        ▼
   Orchestrateur (Placement Adapter Engine)
        │
        ├──► Inventory Service   (scanne vSphere, cache, PropertyCollector optimisé)
        ├──► Filter Service      (élimine les serveurs qui ne correspondent pas)
        ├──► Booking Service     (déduit les réservations récentes <2h + verrou anti-surallocation)
        ├──► Affinity Service    (vérifie les règles d'écartement entre VMs)
        ├──► DRS Adapter Service (lit la charge réelle des serveurs, temps réel)
        └──► Scoring Service     (calcule le score, retourne le meilleur serveur)
                │
                ▼
            Résultat  ──────► VMware DRS (vMotion, équilibrage continu)
```

## 2. Lancer le projet

### Windows (PowerShell) — sans Docker

```powershell
cd vm-placement-microservices
py -m venv .venv
.venv\Scripts\Activate.ps1

# Installer les dépendances de tous les services
Get-ChildItem .\services -Directory | ForEach-Object { pip install -r "$($_.FullName)\requirements.txt" }

# Configurer tes identifiants vSphere
Copy-Item .env.example .env
notepad .env   # remplis VSPHERE_DC1_HOST / _USER / _PASSWORD

# Débloquer les scripts si Windows les bloque
Unblock-File -Path .\run_local_real.ps1, .\stop_local_real.ps1

# Lancer
.\run_local_real.ps1
```

Va sur http://localhost:8000/docs pour tester.
Pour tout arrêter : `.\stop_local_real.ps1`

### Avec Docker (Linux/Mac/Windows)

```bash
cp .env.example .env      # puis renseigne tes identifiants vSphere
docker compose up --build
```

### Linux/Mac sans Docker

```bash
python3 -m venv .venv && source .venv/bin/activate
for d in services/*/; do pip install -r "$d/requirements.txt"; done
cp .env.example .env
./run_local.sh
```

## 3. Tester le pipeline complet

```bash
curl -X POST "http://localhost:8000/api/v1/placements?backend=DC1" \
  -H "Content-Type: application/json" \
  -d '{
        "region": "EU-WEST",
        "offer_code": "PRF",
        "nb_vms": 2,
        "cpu_size": 4,
        "ram_size": 16,
        "storage_size": 100,
        "target_placement": "cluster"
      }'
```

## 4. Fonctionnalités portées depuis le monolithe

| Fonctionnalité | Où dans les microservices |
|---|---|
| Scan vSphere + cache | `inventory_service` (scan classique ET optimisé PropertyCollector, cache par backend/ratio/mode) |
| Santé host/cluster (`host_activated`, `cluster_activated`) | `inventory_service/vsphere_client.py` |
| Convention de nommage par offre | `filter_service.cluster_matches_naming` |
| Filtre de version ESXi | `filter_service.cluster_matches_esx_version` |
| Règles VM group | `filter_service.cluster_has_vm_group` |
| Slots mémoire GPU + profils GPU allouables | `filter_service.gpu_memory_slots` / `gpu_allocable_profile_slots` |
| Pool de ressources "N-1" | `inventory_service._build_resource_pool` |
| Slots CPU / mémoire du pool | `filter_service.cluster_cpu_slots` / `cluster_memory_slots` |
| Espace disque avec résilience par offre | `filter_service.cluster_has_enough_storage` |
| 5 jeux de règles par offre | `shared/rules.py` |
| Anti-affinité DRS | `affinity_service` |
| Réservations récentes <2h | `booking_service` |
| Verrou anti-surallocation par région | `booking_service` (routes `/locks/*`, TTL 30s) |
| Upgrade domains / placement par ESX | `filter_service.filter_non_resilient` + `scoring_service.score_non_resilient` |
| Charge réelle au moment de la décision | `drs_adapter_service` (quickStats temps réel) |

### Exemples de requêtes de test

```jsonc
// Résilient classique (PRF)
{"region":"EU-WEST","offer_code":"PRF","nb_vms":2,"cpu_size":4,"ram_size":16,"storage_size":100,"target_placement":"cluster"}

// GPU
{"region":"EU-WEST","offer_code":"GPU","nb_vms":2,"cpu_size":4,"ram_size":16,"storage_size":50,"target_placement":"cluster","gpu_memory":24}

// Non-résilient : 2 VMs sur 2 upgrade domains
{"region":"EU-WEST","offer_code":"PRF","nb_vms":2,"cpu_size":4,"ram_size":16,"storage_size":50,"target_placement":"esx"}

// Upgrade domains explicites, ordre imposé
{"region":"EU-WEST","offer_code":"PRF","nb_vms":2,"cpu_size":4,"ram_size":16,"storage_size":50,"target_placement":"esx","upgrade_domains":["DC1-PRF-SNR-0093","DC1-PRF-SNR-0092"]}

// VM group + anti-affinité
{"region":"EU-WEST","offer_code":"PRF","nb_vms":2,"cpu_size":4,"ram_size":16,"storage_size":50,"target_placement":"cluster","vm_group":"demo-vm-group","anti_affinity":"anti-affinity-demo"}
```

## 5. Le scan optimisé (PropertyCollector)

L'Inventory Service utilise **par défaut** `scan_optimized()` : au lieu d'un
appel réseau par host/VM (méthode classique, `scan()`), il regroupe toutes
les propriétés nécessaires en 4 requêtes maximum via le PropertyCollector de
pyVmomi. Gain mesuré en conditions réelles : **de plus de 2 minutes à
~3-4 secondes**.

Pour comparer les deux toi-même :
```
GET http://localhost:8001/inventory/{backend_code}/compare-scan-speed
```

Pour forcer l'ancienne méthode (ex: si tu utilises l'anti-affinité, pas
encore portée dans la version optimisée) :
```
GET http://localhost:8001/inventory/DC1?optimized=false
```

## 6. Limites connues

- Les règles d'anti-affinité détaillées ne sont pas encore portées dans
  `scan_optimized()` (restent vides) — utilise `optimized=false` si tu en as besoin.
- Le verrou anti-surallocation est en SQLite, donc valable pour une seule
  instance du Booking Service — à porter sur Redis (`SET NX PX`) pour du
  multi-instances.
- Aucun test automatisé, aucune authentification sur l'API Gateway.
- CI/CD (`.gitlab-ci.yml`) : lint (erreurs critiques uniquement), build
  Docker de chaque service, validation du `docker-compose.yml`. Pas encore
  de tests automatisés ni de déploiement.

## 7. Arborescence

```
vm-placement-microservices/
├── docker-compose.yml
├── .env.example
├── run_local.sh              (Linux/Mac)
├── run_local_real.ps1        (Windows)
├── stop_local_real.ps1       (Windows)
├── shared/
│   ├── models.py
│   └── rules.py
└── services/
    ├── api_gateway/
    ├── orchestrator/
    ├── inventory_service/    (+ vsphere_client.py, cache.py)
    ├── filter_service/
    ├── booking_service/      (+ database.py)
    ├── affinity_service/
    ├── drs_adapter_service/
    └── scoring_service/
```
