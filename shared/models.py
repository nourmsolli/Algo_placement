"""
shared/models.py
=================
Ce fichier contient les "objets" que tous les microservices se passent entre
eux. Pourquoi un fichier partagé : les services communiquent par HTTP (JSON),
donc on remplace les dataclasses de ton monolithe par des modèles Pydantic
qui valident et sérialisent automatiquement les données.

Chaque service embarque sa propre copie de ce fichier (copié dans le
Dockerfile), donc si tu modifies un modèle, pense à le recopier partout où
il est utilisé.
"""

from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional


# ---------------------------------------------------------------------------
# 1. Objets qui décrivent l'infrastructure vSphere
# ---------------------------------------------------------------------------

class DataStore(BaseModel):
    """Un datastore vSphere (espace de stockage partagé par les ESX d'un cluster)."""
    name: str
    capacity: float          # en Go
    freespace: float         # en Go
    accessible: bool = False


class ESX(BaseModel):
    """Un hyperviseur physique (host ESXi)."""
    name: str
    max_allocable_cpu: float
    cpu_allocated: float = 0.0
    max_allocable_memory: float
    memory_allocated: float = 0.0
    cpu_usage: float = 0.0
    memory_usage: float = 0.0
    best_availability_score: float = 0.0   # plus c'est bas, plus l'ESX est disponible
    datastore: Optional[DataStore] = None
    esx_version: str = ""    # version ESXi (ex: "8.0.2"), pour esx_version_filter


class ResourcePool(BaseModel):
    """Le pool de ressources agrégées d'un cluster (somme des ESX, marge N-1)."""
    name: str
    max_allocable_memory: float
    memory_entitled: float
    memory_usage: float = 0.0
    best_availability_score: float = 0.0
    max_allocable_cpu: float = 0.0
    cpu_entitled: float = 0.0
    cpu_usage: float = 0.0


class AffinityRule(BaseModel):
    """Une règle DRS d'anti-affinité déclarée sur un cluster vSphere."""
    name: str
    member_hosts: list[str] = Field(default_factory=list)


class Cluster(BaseModel):
    """Un cluster vSphere = plusieurs ESX + un pool de ressources."""
    name: str
    cpu_hz: int
    resource_pool: list[ResourcePool] = Field(default_factory=list)
    hosts: list[ESX] = Field(default_factory=list)
    best_availability_score: float = 1000.0
    datastore: Optional[DataStore] = None
    vm_slots: int = 0
    affinity_rules: list[AffinityRule] = Field(default_factory=list)
    # Attributs personnalisés vSphere du cluster (nom -> valeur), notamment
    # les attributs GPU : "attr_GpuBufferAllocableGb", "attr_GpuNbProfil<OFFER>Allocable"
    custom_attributes: dict[str, str] = Field(default_factory=dict)
    # Noms des VM groups DRS déclarés sur le cluster (rule.vmGroupName)
    vm_group_names: list[str] = Field(default_factory=list)


class VsphereInventory(BaseModel):
    """L'inventaire complet d'un backend vSphere."""
    name: str
    region: str = ""
    clusters: list[Cluster] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 2. La demande de placement
# ---------------------------------------------------------------------------

class PlacementRequest(BaseModel):
    """Une demande de création de VM. Équivalent de ton PlacementDto."""
    region: str
    offer_code: str                      # ex: "PRF", "ECO", "WS"
    nb_vms: int = 1
    cpu_size: int = 0                    # vCPU demandés par VM
    ram_size: int = 0                    # Go de RAM demandés par VM
    storage_size: int = 0                # Go de stockage demandés par VM
    target_placement: str = "cluster"    # "cluster" (résilient) ou "esx" (non résilient)
    anti_affinity: Optional[str] = None  # nom de la règle d'anti-affinité DRS à respecter
    vm_group: Optional[str] = None
    esx_version_filter: Optional[list[str]] = None
    gpu_memory: Optional[int] = None
    # Placement non-résilient uniquement : liste ordonnée des upgrade domains
    # souhaités, un par VM. Si absent, le système choisit lui-même.
    upgrade_domains: Optional[list[str]] = None


# ---------------------------------------------------------------------------
# 3. Le résultat renvoyé par l'orchestrateur
# ---------------------------------------------------------------------------

class SinglePlacement(BaseModel):
    """Un emplacement précis pour UNE VM (placement non-résilient : une VM
    par upgrade domain)."""
    cluster_name: str
    host_name: Optional[str] = None
    datastore_name: str
    score: float


class PlacementResult(BaseModel):
    """Le résultat final du pipeline de placement."""
    cluster_name: str
    host_name: Optional[str] = None       # rempli seulement si target_placement == "esx"
    datastore_name: str
    vm_slots: int
    score: float
    region: str
    logs: list[str] = Field(default_factory=list)
    # Un emplacement par VM (utile surtout en placement non-résilient) :
    # VM 1 -> placements[0], VM 2 -> placements[1], etc.
    placements: list[SinglePlacement] = Field(default_factory=list)
