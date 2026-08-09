"""Tests unitaires du Filter Service : logique métier pure (aucun I/O)."""

from conftest import load_service_module
from shared.models import PlacementRequest, VsphereInventory, Cluster, ESX, ResourcePool, DataStore

filter_service = load_service_module("filter_service")


def make_cluster(name, *, cpu=32, ram=64, storage_cap=1000, storage_free=900, esx_version="8.0.2"):
    return Cluster(
        name=name,
        cpu_hz=2400,
        resource_pool=[
            ResourcePool(name=f"{name}-rp", max_allocable_cpu=cpu, cpu_entitled=0, max_allocable_memory=ram, memory_entitled=0)
        ],
        hosts=[ESX(name=f"{name}-esx1", max_allocable_cpu=cpu, max_allocable_memory=ram, esx_version=esx_version)],
        datastore=DataStore(name=f"{name}-ds", capacity=storage_cap, freespace=storage_free, accessible=True),
        best_availability_score=10.0,
    )


def make_request(**overrides):
    base = dict(
        region="EU-WEST", offer_code="PRF", nb_vms=2,
        cpu_size=4, ram_size=16, storage_size=100, target_placement="cluster",
    )
    base.update(overrides)
    return PlacementRequest(**base)


PRF_SRE_RULES = {"code": "PRF_SRE", "naming": [{"pos": 1, "val": "PRF"}, {"pos": 2, "val": "SRE"}], "max_usage": 0.8, "storage_resiliency": 2}


def test_cluster_matches_naming_ok():
    ok, reason = filter_service.cluster_matches_naming(make_cluster("DC1-PRF-SRE-01"), PRF_SRE_RULES)
    assert ok
    assert reason == ""


def test_cluster_matches_naming_ko():
    ok, reason = filter_service.cluster_matches_naming(make_cluster("DC1-ECO-STD-01"), PRF_SRE_RULES)
    assert not ok
    assert "PRF_SRE" in reason


def test_cluster_has_enough_storage_ok():
    cluster = make_cluster("DC1-PRF-SRE-01", storage_cap=1000, storage_free=900)
    ok, _ = filter_service.cluster_has_enough_storage(cluster, make_request(storage_size=100, nb_vms=2), PRF_SRE_RULES)
    assert ok


def test_cluster_has_enough_storage_ko():
    cluster = make_cluster("DC1-PRF-SRE-01", storage_cap=200, storage_free=50)
    ok, reason = filter_service.cluster_has_enough_storage(cluster, make_request(storage_size=100, nb_vms=2), PRF_SRE_RULES)
    assert not ok
    assert "Stockage insuffisant" in reason


def test_cluster_cpu_slots_matches_expected_count():
    cluster = make_cluster("DC1-PRF-SRE-01", cpu=32)
    assert filter_service.cluster_cpu_slots(cluster, make_request(cpu_size=4, nb_vms=2)) == 2


def test_cluster_cpu_slots_returns_partial_capacity_not_full_request():
    cluster = make_cluster("DC1-PRF-SRE-01", cpu=4)
    # 4 vCPU dispo, chaque VM en demande 4 -> seulement 1 slot pour 2 VMs demandées
    assert filter_service.cluster_cpu_slots(cluster, make_request(cpu_size=4, nb_vms=2)) == 1


def test_filter_resilient_selects_matching_cluster_and_eliminates_others():
    good = make_cluster("DC1-PRF-SRE-01")
    bad_naming = make_cluster("DC1-ECO-STD-01")
    inventory = VsphereInventory(name="DC1", region="EU-WEST", clusters=[bad_naming, good])
    req = make_request()
    rules = filter_service.get_rules_for_request(req)

    result = filter_service.filter_resilient(req, inventory, rules)

    names = [c.name for c in result.candidate_clusters]
    assert "DC1-PRF-SRE-01" in names
    assert "DC1-ECO-STD-01" not in names
    assert "DC1-ECO-STD-01" in result.eliminated


def test_filter_resilient_no_candidate_when_nothing_matches():
    bad_naming = make_cluster("DC1-ECO-STD-01")
    inventory = VsphereInventory(name="DC1", region="EU-WEST", clusters=[bad_naming])
    req = make_request()
    rules = filter_service.get_rules_for_request(req)

    result = filter_service.filter_resilient(req, inventory, rules)

    assert result.candidate_clusters == []
    assert "DC1-ECO-STD-01" in result.eliminated


def test_filter_non_resilient_returns_one_domain_per_vm():
    d1 = make_cluster("DC1-PRF-SNR-0001")
    d2 = make_cluster("DC1-PRF-SNR-0002")
    inventory = VsphereInventory(name="DC1", region="EU-WEST", clusters=[d1, d2])
    req = make_request(target_placement="esx", nb_vms=2)
    rules = filter_service.get_rules_for_request(req)

    result = filter_service.filter_non_resilient(req, inventory, rules)

    assert len(result.candidate_clusters) == 2


def test_filter_non_resilient_respects_explicit_upgrade_domains_order():
    d1 = make_cluster("DC1-PRF-SNR-0001")
    d2 = make_cluster("DC1-PRF-SNR-0002")
    inventory = VsphereInventory(name="DC1", region="EU-WEST", clusters=[d1, d2])
    req = make_request(target_placement="esx", nb_vms=2, upgrade_domains=["DC1-PRF-SNR-0002", "DC1-PRF-SNR-0001"])
    rules = filter_service.get_rules_for_request(req)

    result = filter_service.filter_non_resilient(req, inventory, rules)

    assert [c.name for c in result.candidate_clusters] == ["DC1-PRF-SNR-0002", "DC1-PRF-SNR-0001"]
