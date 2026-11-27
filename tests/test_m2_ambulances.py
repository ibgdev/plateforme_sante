"""Tests unitaires pour le module M2 (emplacement des ambulances)."""

import pandas as pd
import pytest

from modules.m2_ambulances import glouton, solve

CLES_ATTENDUES = {"statut", "objectif", "solution", "temps"}


@pytest.fixture
def data():
    """Petit jeu de données : 4 communes, 3 bases d'ambulances et 1 site de clinique."""
    df_communes = pd.DataFrame({
        "id": [1, 2, 3, 4],
        "nom": ["A", "B", "C", "D"],
        "x": [-8, 7, 22, 36],
        "y": [0, 3, 4, 5],
        "population": [5000, 3000, 8000, 2000],
    })
    df_sites = pd.DataFrame({
        "id": [1, 2, 3, 4],
        "x": [0, 15, 30, 10],
        "y": [0, 0, 0, 10],
        "cout_fixe": [100, 120, 90, 80],
        "type": ["base_ambulance", "base_ambulance", "base_ambulance", "clinique"],
    })
    return {"communes": df_communes, "sites_candidats": df_sites}


def test_m2_solve_contrat(data):
    """`solve` respecte le contrat de retour commun."""
    resultat = solve(data, {"nb_ambulances": 2, "rayon_minutes": 10})
    assert CLES_ATTENDUES.issubset(resultat.keys())


def test_m2_solve_optimum(data):
    """Avec R = 10 et K = 2, l'optimum couvre toute la population avec les bases 1 et 3."""
    resultat = solve(data, {"nb_ambulances": 2, "rayon_minutes": 10})
    assert resultat["statut"] == "optimal"
    assert resultat["objectif"] == 18000
    assert sorted(resultat["solution"]["bases_ouvertes"]) == [1, 3]


def test_m2_rayon_trop_petit(data):
    """Avec R = 5, aucune base n'atteint de commune : personne n'est couvert."""
    resultat = solve(data, {"nb_ambulances": 3, "rayon_minutes": 5})
    assert resultat["objectif"] == 0


def test_m2_glouton_moins_bon(data):
    """Le glouton trouve 16 000, moins bien que l'optimum de Gurobi (18 000)."""
    exact = solve(data, {"nb_ambulances": 2, "rayon_minutes": 10})
    approx = glouton(data, {"nb_ambulances": 2, "rayon_minutes": 10})
    assert approx["objectif"] == 16000
    assert approx["objectif"] <= exact["objectif"]

def test_m2_panne_base_1(data):
    """Si la base 1 est en panne, on ouvre les bases 2 et 3 : A n'est plus couverte (13 000 au lieu de 18 000)."""
    params = {"nb_ambulances": 2, "rayon_minutes": 10, "bases_en_panne": [1]}
    resultat = solve(data, params)
    assert 1 not in resultat["solution"]["bases_ouvertes"]
    assert resultat["objectif"] == 13000
    assert "A" not in resultat["solution"]["communes_couvertes"]
    # Le glouton ne doit pas non plus utiliser la base en panne
    approx = glouton(data, params)
    assert 1 not in approx["solution"]["bases_ouvertes"]


def test_m2_toutes_les_bases_en_panne(data):
    """Si toutes les bases sont en panne, personne n'est couvert (et le programme ne plante pas)."""
    resultat = solve(data, {"nb_ambulances": 2, "rayon_minutes": 10, "bases_en_panne": [1, 2, 3]})
    assert resultat["objectif"] == 0
    assert resultat["solution"]["bases_ouvertes"] == []



def test_m2_hausse_demande_deplace_une_base(data):
    """Avec 1 seule ambulance, on choisit la base 2 (B + C = 11 000).
    Si la demande de D triple (+200 %), la base 3 devient meilleure (C + D = 8 000 + 6 000 = 14 000)."""
    normal = solve(data, {"nb_ambulances": 1, "rayon_minutes": 10})
    hausse = solve(data, {"nb_ambulances": 1, "rayon_minutes": 10, "hausse_demande": {"D": 200}})
    assert normal["solution"]["bases_ouvertes"] == [2]
    assert hausse["solution"]["bases_ouvertes"] == [3]
    assert hausse["objectif"] == 14000