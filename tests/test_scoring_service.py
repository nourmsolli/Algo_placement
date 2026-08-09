"""Tests unitaires du Scoring Service : logique métier pure (aucun I/O)."""

import pytest
from fastapi import HTTPException
from conftest import load_service_module
from shared.models import PlacementRequest, Cluster, ESX, DataStore

scoring_service = load_service_module("scoring_service")


def make_cluster(name, score, esx_names=None):
    hosts = [
        ESX(name=f"{name}-{h}", max_allocable_cpu=32, max_allocable_memory=64, best_availability_score=score)
        for h in (esx_names or ["esx1"])
    ]
    return Cluster(
        name=name,
        cpu_hz=2400,
        hosts=hosts,
        datastore=DataStore(name=f"{name}-ds", capacity=1000, freespace=900, accessible=True),
        best_availability_score=score,
        vm_slots=2,
    )


def make_request(**overrides):
    base = dict(
        region="EU-WEST", offer_code="PRF", nb_vms=2,
        cpu_size=4, ram_size=16, storage_size=100, target_placement="cluster",
    )
    base.update(overrides)
    return PlacementRequest(**base)


def test_score_resilient_picks_lowest_score():
    best = make_cluster("DC1-PRF-SRE-01", score=5.0)
    worse = make_cluster("DC1-PRF-SRE-02", score=20.0)

    result = scoring_service.score_resilient(make_request(), [worse, best])

    assert result.cluster_name == "DC1-PRF-SRE-01"
    assert result.score == 5.0
    assert result.host_name is None


def test_score_resilient_no_datastore_raises():
    cluster = Cluster(name="DC1-PRF-SRE-01", cpu_hz=2400, hosts=[], best_availability_score=5.0, datastore=None)
    with pytest.raises(HTTPException):
        scoring_service.score_resilient(make_request(), [cluster])


def test_score_non_resilient_one_placement_per_domain_in_order():
    d1 = make_cluster("DC1-PRF-SNR-0001", score=3.0)
    d2 = make_cluster("DC1-PRF-SNR-0002", score=4.0)

    result = scoring_service.score_non_resilient(make_request(target_placement="esx", nb_vms=2), [d1, d2])

    assert len(result.placements) == 2
    assert result.placements[0].cluster_name == "DC1-PRF-SNR-0001"
    assert result.placements[0].host_name == "DC1-PRF-SNR-0001-esx1"
    assert result.placements[1].cluster_name == "DC1-PRF-SNR-0002"


def test_score_non_resilient_not_enough_domains_raises():
    d1 = make_cluster("DC1-PRF-SNR-0001", score=3.0)
    with pytest.raises(HTTPException):
        scoring_service.score_non_resilient(make_request(target_placement="esx", nb_vms=2), [d1])


def test_compute_best_placement_no_clusters_raises_404():
    payload = scoring_service.ScoringRequest(placement_request=make_request(), candidate_clusters=[])
    with pytest.raises(HTTPException) as excinfo:
        scoring_service.compute_best_placement(payload)
    assert excinfo.value.status_code == 404
