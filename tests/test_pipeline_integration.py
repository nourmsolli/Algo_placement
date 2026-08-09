"""
Test d'intégration : Filter Service -> Scoring Service, sur un inventaire
vSphere simulé (mock d'un vrai scan de l'Inventory Service ; pas besoin de
vSphere réel ni de lancer les microservices en HTTP).
"""

from conftest import load_service_module
from shared.models import PlacementRequest, VsphereInventory, Cluster, ESX, ResourcePool, DataStore

filter_service = load_service_module("filter_service")
scoring_service = load_service_module("scoring_service")


def make_cluster(name, *, score, cpu=32, ram=64):
    return Cluster(
        name=name,
        cpu_hz=2400,
        resource_pool=[
            ResourcePool(name=f"{name}-rp", max_allocable_cpu=cpu, cpu_entitled=0, max_allocable_memory=ram, memory_entitled=0)
        ],
        hosts=[ESX(name=f"{name}-esx1", max_allocable_cpu=cpu, max_allocable_memory=ram, best_availability_score=score)],
        datastore=DataStore(name=f"{name}-ds", capacity=1000, freespace=900, accessible=True),
        best_availability_score=score,
    )


def test_full_pipeline_resilient_picks_best_matching_cluster():
    # Simule un inventaire vSphere : un cluster hors convention de nommage
    # (meilleur score mais doit être éliminé AVANT le scoring), et deux
    # clusters valides avec des scores différents.
    wrong_naming = make_cluster("DC1-ECO-STD-01", score=1.0)
    ok_but_worse = make_cluster("DC1-PRF-SRE-02", score=50.0)
    best = make_cluster("DC1-PRF-SRE-01", score=5.0)
    inventory = VsphereInventory(name="DC1", region="EU-WEST", clusters=[wrong_naming, ok_but_worse, best])

    req = PlacementRequest(
        region="EU-WEST", offer_code="PRF", nb_vms=1,
        cpu_size=4, ram_size=16, storage_size=100, target_placement="cluster",
    )
    rules = filter_service.get_rules_for_request(req)

    filtered = filter_service.filter_resilient(req, inventory, rules)
    assert {c.name for c in filtered.candidate_clusters} == {"DC1-PRF-SRE-01", "DC1-PRF-SRE-02"}

    result = scoring_service.score_resilient(req, filtered.candidate_clusters)
    assert result.cluster_name == "DC1-PRF-SRE-01"  # meilleur score PARMI les clusters valides


def test_full_pipeline_non_resilient_one_vm_per_upgrade_domain():
    d1 = make_cluster("DC1-PRF-SNR-0001", score=3.0)
    d2 = make_cluster("DC1-PRF-SNR-0002", score=4.0)
    inventory = VsphereInventory(name="DC1", region="EU-WEST", clusters=[d2, d1])

    req = PlacementRequest(
        region="EU-WEST", offer_code="PRF", nb_vms=2,
        cpu_size=4, ram_size=16, storage_size=50, target_placement="esx",
    )
    rules = filter_service.get_rules_for_request(req)

    filtered = filter_service.filter_non_resilient(req, inventory, rules)
    result = scoring_service.score_non_resilient(req, filtered.candidate_clusters)

    assert len(result.placements) == 2
    # trié par meilleur score d'abord (best_availability_score le plus bas)
    assert result.placements[0].cluster_name == "DC1-PRF-SNR-0001"


def test_full_pipeline_no_matching_cluster_produces_empty_candidates():
    only_bad = make_cluster("DC1-ECO-STD-01", score=1.0)
    inventory = VsphereInventory(name="DC1", region="EU-WEST", clusters=[only_bad])

    req = PlacementRequest(
        region="EU-WEST", offer_code="PRF", nb_vms=1,
        cpu_size=4, ram_size=16, storage_size=100, target_placement="cluster",
    )
    rules = filter_service.get_rules_for_request(req)

    filtered = filter_service.filter_resilient(req, inventory, rules)

    assert filtered.candidate_clusters == []
    assert "DC1-ECO-STD-01" in filtered.eliminated
