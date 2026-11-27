"""Tests unitaires et d'intégration pour le module M5 (planning des blocs opératoires)."""

import io

from app import create_app
from data.loader import load_data
from modules.m5_blocs import resoudre_heuristique, solve

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


def test_m5_donnees_communes_et_loader():
    """Vérifie que `load_data()` charge les 3 CSV M5 et que `solve` trouve une solution optimale."""
    donnees = load_data()
    assert {"hopitaux", "personnel", "operations"}.issubset(donnees.keys())
    assert {"id", "duree", "chirurgien", "priorite"}.issubset(donnees["operations"].columns)

    result = solve(
        data=donnees,
        params={"nb_salles": 4, "horaires_ouverture": (8, 16), "duree_creneau": 30},
    )
    assert result["statut"] == "optimal"
    assert len(result["solution"]["planning"]) == len(donnees["operations"])
    assert result["solution"]["heures_sup"] == 0.0


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


def test_m5_respect_indisponibilites_chirurgiens():
    """Vérifie qu'aucune opération ne chevauche une indisponibilité de son chirurgien."""
    donnees = {
        "operations": [
            {"id": "OP-1", "duree": 60, "chirurgien": "CHIR-A", "priorite": 1},
            {"id": "OP-2", "duree": 60, "chirurgien": "CHIR-A", "priorite": 2},
        ]
    }
    result = solve(
        data=donnees,
        params={
            "nb_salles": 2,
            "horaires_ouverture": (8, 12),
            "duree_creneau": 30,
            "indisponibilites": [{"chirurgien": "CHIR-A", "debut": "08:00", "fin": "10:00"}],
        },
    )
    assert result["statut"] == "optimal"
    for op in result["solution"]["planning"]:
        # CHIR-A est indisponible de 08:00 (480) à 10:00 (600)
        assert op["debut_min"] >= 600


def test_m5_gestion_heures_supplementaires():
    """Vérifie l'interdiction vs l'autorisation contrôlée d'heures supplémentaires."""
    donnees = {
        "operations": [
            {"id": "OP-1", "duree": 120, "chirurgien": "CHIR-1", "priorite": 1},
            {"id": "OP-2", "duree": 120, "chirurgien": "CHIR-2", "priorite": 2},
        ]
    }
    # 1 salle, 8h-11h (180 min) pour 240 min d'opérations -> impossible sans heures sup
    sans_sup = solve(
        data=donnees,
        params={"nb_salles": 1, "horaires_ouverture": (8, 11), "duree_creneau": 30, "max_heures_sup": 0.0},
    )
    assert sans_sup["statut"] == "infaisable"

    # Avec 2h d'heures sup autorisées -> optimal avec exactement 1.0 h supplémentaire
    avec_sup = solve(
        data=donnees,
        params={"nb_salles": 1, "horaires_ouverture": (8, 11), "duree_creneau": 30, "max_heures_sup": 2.0},
    )
    assert avec_sup["statut"] == "optimal"
    assert avec_sup["solution"]["heures_sup"] == 1.0
    assert avec_sup["solution"]["heures_sup_par_salle"]["Salle 1"] == 1.0


def test_m5_mode_objectif_priorite():
    """En mode priorité, une opération urgente passe avant une opération normale de même durée."""
    donnees = {
        "operations": [
            {"id": "OP-NORMALE", "duree": 60, "chirurgien": "CHIR-1", "priorite": 3},
            {"id": "OP-URGENTE", "duree": 60, "chirurgien": "CHIR-1", "priorite": 1},
        ]
    }
    result = solve(
        data=donnees,
        params={
            "nb_salles": 1,
            "horaires_ouverture": (8, 12),
            "duree_creneau": 30,
            "mode_objectif": "priorite",
        },
    )
    assert result["statut"] == "optimal"
    par_id = {op["id"]: op for op in result["solution"]["planning"]}
    assert par_id["OP-URGENTE"]["debut_min"] < par_id["OP-NORMALE"]["debut_min"]


def test_m5_comparaison_gurobi_vs_heuristique():
    """Vérifie que la comparaison Gurobi vs heuristique gloutonne est cohérente."""
    donnees = load_data()
    params = {
        "nb_salles": 4,
        "horaires_ouverture": (8, 16),
        "duree_creneau": 30,
        "mode_objectif": "priorite",
        "comparer": True,
    }
    res_gurobi = solve(data=donnees, params=params)
    res_heur = resoudre_heuristique(data=donnees, params=params)

    assert res_gurobi["statut"] == "optimal"
    assert res_heur["statut"] in {"optimal", "faisable"}
    assert res_gurobi["objectif"] <= res_heur["objectif"]

    methodes = res_gurobi["solution"]["comparaison"]["methodes"]
    assert len(methodes) == 2
    assert "Gurobi" in methodes[0]["nom"]
    assert "Heuristique" in methodes[1]["nom"]


def test_m5_cas_infaisable():
    """Vérifie que `solve` retourne `infaisable` si la durée dépasse l'amplitude maximale."""
    donnees = {
        "operations": [
            {"id": "OP-LONGUE", "duree": 300, "chirurgien": "CHIR-A", "priorite": 1},
        ]
    }
    result = solve(
        data=donnees,
        params={"nb_salles": 1, "horaires_ouverture": (8, 10), "duree_creneau": 30, "max_heures_sup": 1.0},
    )
    assert result["statut"] == "infaisable"
    assert result["solution"]["planning"] == []


def test_m5_routes_flask_gantt_et_csv():
    """Vérifie GET /m5/, POST complet avec Gantt Plotly, scénario saturé et import CSV."""
    client = create_app().test_client()

    # 1. Page initiale chargée depuis data/*.csv
    reponse_get = client.get("/m5/")
    assert reponse_get.status_code == 200
    assert b"OP-01" in reponse_get.data

    # 2. Téléchargement du CSV d'exemple
    reponse_csv_exemple = client.get("/m5/exemple-csv")
    assert reponse_csv_exemple.status_code == 200
    assert b"id,duree,chirurgien,priorite" in reponse_csv_exemple.data

    # 3. POST formulaire complet + vérification Gantt Plotly + tableau de comparaison
    reponse_post = client.post(
        "/m5/",
        data={
            "action": "resoudre",
            "nb_salles": "3",
            "heure_debut": "8",
            "heure_fin": "16",
            "duree_creneau": "30",
            "max_heures_sup": "2",
            "mode_objectif": "priorite",
            "indisponibilites_presentes": "1",
            "op_id[]": ["OP-A", "OP-B", "OP-C"],
            "op_duree[]": ["90", "60", "120"],
            "op_chirurgien[]": ["CHIR-1", "CHIR-1", "CHIR-2"],
            "op_priorite[]": ["1", "2", "1"],
            "indisp_chirurgien[]": ["CHIR-1"],
            "indisp_debut[]": ["11:00"],
            "indisp_fin[]": ["12:00"],
        },
    )
    assert reponse_post.status_code == 200
    assert b"optimal" in reponse_post.data
    assert b"plotly-graph-div" in reponse_post.data
    assert b"Heuristique gloutonne" in reponse_post.data

    # 4. Scénario saturé (heures supplémentaires)
    reponse_sature = client.post("/m5/", data={"action": "scenario_sature"})
    assert reponse_sature.status_code == 200
    assert b"plotly-graph-div" in reponse_sature.data

    # 5. Import d'un fichier CSV
    contenu_csv = b"id,duree,chirurgien,priorite\nCSV-1,60,CHIR-X,1\nCSV-2,90,CHIR-Y,2\n"
    reponse_import = client.post(
        "/m5/",
        data={
            "action": "importer_csv",
            "nb_salles": "2",
            "heure_debut": "8",
            "heure_fin": "16",
            "duree_creneau": "30",
            "max_heures_sup": "2",
            "mode_objectif": "makespan",
            "indisponibilites_presentes": "1",
            "fichier_csv": (io.BytesIO(contenu_csv), "mes_operations.csv"),
        },
        content_type="multipart/form-data",
    )
    assert reponse_import.status_code == 200
    assert b"CSV-1" in reponse_import.data
    assert b"CSV-2" in reponse_import.data
    assert b"plotly-graph-div" in reponse_import.data
