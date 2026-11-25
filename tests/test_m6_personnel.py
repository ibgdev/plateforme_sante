"""Tests du modèle et de l'interface Flask du module M6."""

import pytest

from modules.m6_personnel import SERVICES, solve


EXPECTED_KEYS = {"statut", "objectif", "solution", "temps"}


def _parametres(nb_infirmiers=3, nb_jours=7, besoins=None, **supplement):
    params = {
        "nb_infirmiers": nb_infirmiers,
        "nb_jours": nb_jours,
        "besoins_min": besoins or {},
        "limite_temps": 10,
        "nb_workers": 1,
    }
    params.update(supplement)
    return params


def test_solve_refuse_une_periode_non_supportee_sans_solveur():
    with pytest.raises(ValueError, match="7 ou 28"):
        solve({}, {"nb_infirmiers": 3, "nb_jours": 14})


def test_solve_refuse_plus_de_cinq_jours_par_semaine():
    with pytest.raises(ValueError, match="deux repos"):
        solve({}, _parametres(max_jours_semaine=6))


def test_solve_contrat_et_couverture():
    pytest.importorskip("ortools")
    besoins = {"matin": [1] * 7, "soir": [0] * 7, "nuit": [1, 0, 0, 0, 0, 0, 0]}
    resultat = solve({}, _parametres(3, besoins=besoins))

    assert EXPECTED_KEYS.issubset(resultat)
    assert resultat["statut"] in {"optimal", "faisable"}
    assert resultat["temps"] >= 0
    assert len(resultat["solution"]["planning"]) == 3
    planning = {ligne["infirmier"]: ligne["jours"] for ligne in resultat["solution"]["planning"]}
    for jour in range(7):
        assert sum(lignes[jour] == "matin" for lignes in planning.values()) >= 1
        assert sum(lignes[jour] == "nuit" for lignes in planning.values()) >= besoins["nuit"][jour]
    for jours in planning.values():
        assert sum(service is not None for service in jours) <= 5
        for jour in range(6):
            if jours[jour] == "nuit":
                assert jours[jour + 1] is None


def test_solve_retourne_infaisable_si_la_couverture_est_impossible():
    pytest.importorskip("ortools")
    besoins = {"matin": [2, 0, 0, 0, 0, 0, 0]}
    resultat = solve({}, _parametres(1, besoins=besoins))
    assert resultat["statut"] == "infaisable"
    assert EXPECTED_KEYS.issubset(resultat)


def test_couts_et_preferences_sont_configurables():
    pytest.importorskip("ortools")
    besoins = {"matin": [1, 0, 0, 0, 0, 0, 0]}
    params = _parametres(
        2,
        besoins=besoins,
        couts_services={"matin": 2, "soir": 3, "nuit": 4},
        preferences=[{"infirmier": "I1", "jour": 0, "service": "matin", "souhaite": True}],
        penalite_preferences=8,
    )
    resultat = solve({}, params)
    assert resultat["statut"] == "optimal"
    assert resultat["objectif"] == pytest.approx(2)


def test_route_affiche_formulaire_et_transmet_les_parametres(monkeypatch):
    pytest.importorskip("flask")
    from app import create_app
    from routes import m6_routes

    app = create_app()
    reponses = []

    def faux_solve(data, params):
        reponses.append((data, params))
        return {"statut": "infaisable", "objectif": 0.0, "solution": {}, "temps": 0.01}

    monkeypatch.setattr(m6_routes, "solve", faux_solve)
    client = app.test_client()
    page = client.get("/m6/")
    assert page.status_code == 200
    assert "Générer le planning" in page.get_data(as_text=True)

    formulaire = {
        "nb_jours": "7",
        "limite_temps": "5",
        "cout_matin": "1",
        "cout_soir": "1.15",
        "cout_nuit": "1.5",
        "heures_matin": "8",
        "heures_soir": "8",
        "heures_nuit": "8",
        "penalite_preferences": "10",
        "infirmier_id[]": ["I1"],
        "contrat[]": ["standard"],
        "max_jours[]": ["5"],
        "max_heures[]": ["40"],
        "indisponibilites[]": [""],
    }
    for service in SERVICES:
        for jour in range(28):
            formulaire[f"besoin_{service}_{jour}"] = "0"
    formulaire["besoin_matin_0"] = "1"
    reponse = client.post("/m6/", data=formulaire)
    assert reponse.status_code == 200
    assert len(reponses) == 1
    data, params = reponses[0]
    assert data == {}
    assert params["infirmiers"][0]["id"] == "I1"
    assert params["nb_jours"] == 7
    assert params["besoins_min"]["matin"][0] == 1
