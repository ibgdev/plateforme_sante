"""Tests unitaires pour le module M5 (planning des blocs opératoires)."""

from app import create_app
from modules.m5_blocs import solve

EXPECTED_KEYS = {"statut", "objectif", "solution", "temps"}


def test_m5_solve_contract():
    """Vérifie que `solve` respecte le contrat de retour commun."""
    result = solve(data={}, params={"nb_salles": 4, "duree_creneau": 30})
    assert EXPECTED_KEYS.issubset(result.keys())
    assert result["statut"] in {"optimal", "faisable"}
    assert isinstance(result["objectif"], float)
    assert isinstance(result["temps"], float)
    assert result["temps"] >= 0.0
    assert len(result["solution"]["planning"]) > 0


def test_m5_sans_chevauchement_salle_et_chirurgien():
    """Vérifie qu'aucune salle ni aucun chirurgien n'est doublement réservé."""
    donnees = {
        "operations": [
            {"id": "OP-1", "duree": 60, "chirurgien": "CHIR-A", "priorite": 1},
            {"id": "OP-2", "duree": 60, "chirurgien": "CHIR-A", "priorite": 2},
            {"id": "OP-3", "duree": 90, "chirurgien": "CHIR-B", "priorite": 1},
        ]
    }
    result = solve(
        data=donnees,
        params={"nb_salles": 2, "horaires_ouverture": (8, 12), "duree_creneau": 30},
    )
    assert result["statut"] == "optimal"
    planning = result["solution"]["planning"]
    assert len(planning) == 3

    for i in range(len(planning)):
        for j in range(i + 1, len(planning)):
            op1, op2 = planning[i], planning[j]
            chevauchement = op1["debut_min"] < op2["fin_min"] and op2["debut_min"] < op1["fin_min"]
            if op1["salle"] == op2["salle"]:
                assert not chevauchement
            if op1["chirurgien"] == op2["chirurgien"]:
                assert not chevauchement


def test_m5_cas_infaisable():
    """Vérifie que `solve` retourne `infaisable` si la durée dépasse l'ouverture."""
    donnees = {
        "operations": [
            {"id": "OP-LONGUE", "duree": 300, "chirurgien": "CHIR-A", "priorite": 1},
        ]
    }
    result = solve(
        data=donnees,
        params={"nb_salles": 1, "horaires_ouverture": (8, 10), "duree_creneau": 30},
    )
    assert result["statut"] == "infaisable"
    assert result["solution"]["planning"] == []


def test_m5_routes_flask():
    """Vérifie que les routes GET et POST de `/m5/` répondent correctement."""
    client = create_app().test_client()

    reponse_get = client.get("/m5/")
    assert reponse_get.status_code == 200

    reponse_post = client.post(
        "/m5/",
        data={"nb_salles": "3", "heure_debut": "8", "heure_fin": "16", "duree_creneau": "30"},
    )
    assert reponse_post.status_code == 200
    assert b"optimal" in reponse_post.data
