"""Tests unitaires et d'intégration pour le module M5 (planning des blocs opératoires)."""

import io
import json

from app import create_app
from data.loader import load_data
from modules.m5_blocs import inserer_urgence, resoudre_cpsat, resoudre_heuristique, solve

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
        assert op["debut_min"] >= 600


def test_m5_gestion_heures_supplementaires():
    """Vérifie l'interdiction vs l'autorisation contrôlée d'heures supplémentaires."""
    donnees = {
        "operations": [
            {"id": "OP-1", "duree": 120, "chirurgien": "CHIR-1", "priorite": 1},
            {"id": "OP-2", "duree": 120, "chirurgien": "CHIR-2", "priorite": 2},
        ]
    }
    sans_sup = solve(
        data=donnees,
        params={"nb_salles": 1, "horaires_ouverture": (8, 11), "duree_creneau": 30, "max_heures_sup": 0.0},
    )
    assert sans_sup["statut"] == "infaisable"

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


def test_m5_triple_comparaison_gurobi_cpsat_heuristique():
    """Vérifie la cohérence des 3 méthodes : Gurobi == CP-SAT à l'optimal et <= Heuristique."""
    donnees = load_data()
    params = {
        "nb_salles": 4,
        "horaires_ouverture": (8, 16),
        "duree_creneau": 30,
        "mode_objectif": "priorite",
        "comparer": True,
    }
    res_gurobi = solve(data=donnees, params=params)
    res_cp = resoudre_cpsat(data=donnees, params=params)
    res_heur = resoudre_heuristique(data=donnees, params=params)

    assert res_gurobi["statut"] == "optimal"
    assert res_cp["statut"] == "optimal"
    assert res_heur["statut"] in {"optimal", "faisable"}
    assert res_gurobi["objectif"] == res_cp["objectif"]
    assert res_gurobi["objectif"] <= res_heur["objectif"]

    methodes = res_gurobi["solution"]["comparaison"]["methodes"]
    assert len(methodes) == 3
    assert "Gurobi" in methodes[0]["nom"]
    assert "CP-SAT" in methodes[1]["nom"]
    assert "Heuristique" in methodes[2]["nom"]
    assert methodes[1]["ecart_pct"] == 0.0


def test_m5_insertion_urgence_fige_passe_et_reoptimise():
    """Vérifie que l'insertion d'urgence fige les ops commencées avant t0 et place l'urgence >= t0."""
    donnees = load_data()
    params = {
        "nb_salles": 4,
        "horaires_ouverture": (8, 16),
        "duree_creneau": 30,
        "max_heures_sup": 2.0,
        "mode_objectif": "makespan",
    }
    res_initial = solve(data=donnees, params=params)
    assert res_initial["statut"] == "optimal"
    planning_initial = res_initial["solution"]["planning"]

    res_urg = inserer_urgence(
        planning_existant=planning_initial,
        urgence={"id": "URG-01", "duree": 60, "chirurgien": "CHIR-1"},
        heure_arrivee="10:00",
        data=donnees,
        params=params,
    )
    assert EXPECTED_KEYS.issubset(res_urg.keys())
    assert res_urg["statut"] == "optimal"

    nouveau_planning = res_urg["solution"]["planning"]
    assert len(nouveau_planning) == len(planning_initial) + 1

    # Vérifier que l'urgence commence bien à >= 10:00 (600 min) et est marquée est_urgence
    urg_planifiee = next(op for op in nouveau_planning if op["id"] == "URG-01")
    assert urg_planifiee["est_urgence"] is True
    assert urg_planifiee["debut_min"] >= 600

    # Vérifier que toutes les opérations commencées avant 10:00 sont restées strictement figées
    nouveau_par_id = {op["id"]: op for op in nouveau_planning}
    for op_init in planning_initial:
        if op_init["debut_min"] < 600:
            op_apres = nouveau_par_id[op_init["id"]]
            assert op_apres["figee"] is True
            assert op_apres["salle"] == op_init["salle"]
            assert op_apres["debut_min"] == op_init["debut_min"]
            assert op_apres["fin_min"] == op_init["fin_min"]

    # Vérifier l'absence totale de chevauchement salle & chirurgien après ré-optimisation
    for i in range(len(nouveau_planning)):
        for j in range(i + 1, len(nouveau_planning)):
            op1, op2 = nouveau_planning[i], nouveau_planning[j]
            chev = op1["debut_min"] < op2["fin_min"] and op2["debut_min"] < op1["fin_min"]
            if op1["salle"] == op2["salle"]:
                assert not chev
            if op1["chirurgien"] == op2["chirurgien"]:
                assert not chev

    impact = res_urg["solution"]["impact_urgence"]
    assert impact["urgence_id"] == "URG-01"
    assert impact["heure_arrivee"] == "10:00"
    assert impact["nb_figees"] > 0


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

    reponse_get = client.get("/m5/")
    assert reponse_get.status_code == 200
    assert b"OP-01" in reponse_get.data

    reponse_csv_exemple = client.get("/m5/exemple-csv")
    assert reponse_csv_exemple.status_code == 200
    assert b"id,duree,chirurgien,priorite" in reponse_csv_exemple.data

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
    assert b"OR-Tools CP-SAT" in reponse_post.data
    assert b"Heuristique gloutonne" in reponse_post.data

    reponse_sature = client.post("/m5/", data={"action": "scenario_sature"})
    assert reponse_sature.status_code == 200
    assert b"plotly-graph-div" in reponse_sature.data

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


def test_m5_route_coloration_export_et_insertion_urgence():
    """Vérifie la coloration Gantt par chirurgien, l'export CSV et l'insertion d'urgence en web."""
    client = create_app().test_client()
    donnees = load_data()

    # 1. Coloration Gantt par chirurgien
    reponse_chir = client.post(
        "/m5/",
        data={
            "action": "resoudre",
            "nb_salles": "4",
            "heure_debut": "8",
            "heure_fin": "16",
            "duree_creneau": "30",
            "max_heures_sup": "2",
            "mode_objectif": "makespan",
            "coloration_gantt": "chirurgien",
        },
    )
    assert reponse_chir.status_code == 200
    assert b"Vue par chirurgien" in reponse_chir.data
    assert b"plotly-graph-div" in reponse_chir.data
    assert b"Gurobi PLNE" in reponse_chir.data
    assert b"filtre-salle" in reponse_chir.data
    assert b"filtre-chirurgien" in reponse_chir.data

    # 2. Export CSV du planning optimisé
    res_init = solve(
        data=donnees,
        params={"nb_salles": 4, "horaires_ouverture": (8, 16), "duree_creneau": 30, "mode_objectif": "priorite"},
    )
    planning_json = json.dumps(res_init["solution"]["planning"], ensure_ascii=False)
    reponse_export = client.post("/m5/exporter-planning", data={"planning_json": planning_json})
    assert reponse_export.status_code == 200
    texte_csv = reponse_export.get_data(as_text=True)
    assert "salle,id,chirurgien,priorite,debut,fin,duree_min,en_heures_sup" in texte_csv
    assert "OP-01" in texte_csv

    # 3. Insertion d'une urgence via le formulaire web
    params_json = json.dumps(
        {
            "nb_salles": 4,
            "heure_debut": 8,
            "heure_fin": 16,
            "duree_creneau": 30,
            "max_heures_sup": 2.0,
            "mode_objectif": "priorite",
            "coloration_gantt": "priorite",
        }
    )
    ops_json = json.dumps(donnees["operations"].to_dict(orient="records"), ensure_ascii=False)
    indispos_json = json.dumps([{"chirurgien": "CHIR-1", "debut": "11:00", "fin": "12:00"}])

    reponse_urg = client.post(
        "/m5/",
        data={
            "action": "inserer_urgence",
            "planning_initial_json": planning_json,
            "parametres_json": params_json,
            "operations_json": ops_json,
            "indisponibilites_json": indispos_json,
            "urgence_heure": "10:00",
            "urgence_duree": "60",
            "urgence_chirurgien": "CHIR-1",
        },
    )
    assert reponse_urg.status_code == 200
    corps = reponse_urg.get_data(as_text=True)
    assert "URG-01" in corps
    assert "Impact de l'urgence" in corps or "Impact de l&#39;urgence" in corps
    assert "Arrivée urgence 10:00" in corps or "Arriv\\u00e9e urgence 10:00" in corps
    assert "plotly-graph-div" in corps
