"""
shared/rules.py
================
Reprend les 5 jeux de règles métier de ton monolithe (ECO_STD_SRE_RULES,
WS_SRE_RULES, GPU_SRE_RULES, PRF_SRE_RULES, PRF_SNR_RULES).

Ce fichier centralise la SÉLECTION de la bonne règle, pour qu'orchestrator,
filter_service et inventory_service utilisent tous la même logique.
"""

from shared.models import PlacementRequest

ECO_STD_SRE_RULES = {
    "code": "ECO_STD_SRE",
    "naming": [{"pos": 1, "val": "STD"}, {"pos": 2, "val": "SRE"}],
    "max_usage": 0.80,
    "cpu_to_vcpu_ratio": 8,
    "storage_resiliency": 1.5,
}

WS_SRE_RULES = {
    "code": "WS_SRE",
    "naming": [{"pos": 1, "val": "PDT"}, {"pos": 2, "val": "SRE"}],
    "max_usage": 0.80,
    "cpu_to_vcpu_ratio": 8,
    "storage_resiliency": 1.5,
}

GPU_SRE_RULES = {
    "code": "GPU_SRE",
    "naming": [{"pos": 1, "val": "GPU"}, {"pos": 2, "val": "SRE"}],
    "max_usage": 0.80,
    "cpu_to_vcpu_ratio": 1,
    "storage_resiliency": 1.5,
}

PRF_SRE_RULES = {
    "code": "PRF_SRE",
    "naming": [{"pos": 1, "val": "PRF"}, {"pos": 2, "val": "SRE"}],
    "max_usage": 0.80,
    "cpu_to_vcpu_ratio": 4,
    "storage_resiliency": 2,
}

PRF_SNR_RULES = {
    "code": "PRF_SNR",
    "naming": [{"pos": 1, "val": "PRF"}, {"pos": 2, "val": "SNR"}],
    "max_usage": 0.80,
    "cpu_to_vcpu_ratio": 2,
    "storage_resiliency": 1,
}


def get_rules_for_request(request: PlacementRequest) -> dict:
    """
    Sélectionne le bon jeu de règles :
    - gpu_memory renseigné                          -> GPU_SRE_RULES
    - target_placement == "esx" (upgrade domains)    -> PRF_SNR_RULES
    - sinon, selon offer_code : "WS" -> WS, "PRF" -> PRF, autre -> ECO_STD
    """
    if request.gpu_memory:
        return GPU_SRE_RULES
    if request.target_placement == "esx":
        return PRF_SNR_RULES
    match request.offer_code.upper():
        case "WS":
            return WS_SRE_RULES
        case "PRF":
            return PRF_SRE_RULES
        case _:
            return ECO_STD_SRE_RULES
