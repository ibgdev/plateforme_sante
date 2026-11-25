"""Module M5 — Planning des blocs opératoires.

Type    : Ordonnancement (Affectation salle × créneau)
Outils  : gurobipy (PLNE), Plotly (diagramme de Gantt — étape suivante)
Données : hopitaux.csv, personnel.csv, operations.csv
Branche : m5_blocs
"""

import math
import time
from typing import Any

import gurobipy as gp
from gurobipy import GRB


def _operations_par_defaut() -> list[dict[str, Any]]:
    """Retourne un petit jeu d'opérations réaliste utilisé si `data` est vide."""
    return [
        {"id": "OP-01", "duree": 90, "chirurgien": "CHIR-1", "priorite": 1},
        {"id": "OP-02", "duree": 120, "chirurgien": "CHIR-2", "priorite": 1},
        {"id": "OP-03", "duree": 60, "chirurgien": "CHIR-1", "priorite": 2},
        {"id": "OP-04", "duree": 150, "chirurgien": "CHIR-3", "priorite": 2},
        {"id": "OP-05", "duree": 60, "chirurgien": "CHIR-2", "priorite": 3},
    ]


def _extraire_operations(data: dict) -> list[dict[str, Any]]:
    """Extrait la liste des opérations depuis `data` (liste de dicts ou DataFrame)."""
    if not data or "operations" not in data or data["operations"] is None:
        return _operations_par_defaut()

    brut = data["operations"]
    if hasattr(brut, "to_dict"):
        enregistrements = brut.to_dict(orient="records")
    else:
        enregistrements = list(brut)

    return enregistrements


def _formater_heure(minutes_depuis_minuit: int) -> str:
    """Convertit un nombre de minutes depuis minuit au format HH:MM."""
    heures, minutes = divmod(minutes_depuis_minuit, 60)
    return f"{heures:02d}:{minutes:02d}"


def _resultat_infaisable(debut_chrono: float, raison: str = "") -> dict[str, Any]:
    """Construit une réponse normalisée pour un problème infaisable."""
    return {
        "statut": "infaisable",
        "objectif": 0.0,
        "solution": {
            "planning": [],
            "taux_occupation": {"global": 0.0},
            "heures_sup": 0.0,
            "message": raison,
        },
        "temps": round(time.perf_counter() - debut_chrono, 4),
    }


def solve(data: dict, params: dict) -> dict:
    """Résout un modèle initial d'ordonnancement des blocs opératoires avec Gurobi.

    Paramètres attendus dans `params` :
        - nb_salles (int) : nombre de salles d'opération (défaut : 4).
        - horaires_ouverture (tuple) : (heure_debut, heure_fin) en heures (défaut : (8, 16)).
        - duree_creneau (int) : durée d'un créneau en minutes (défaut : 30).

    Retour attendu :
        {
            "statut": str,       # "optimal", "faisable", "infaisable"
            "objectif": float,   # durée totale de la journée opératoire en minutes (makespan)
            "solution": dict,    # planning par salle/créneau, taux d'occupation, heures sup
            "temps": float,      # temps de calcul en secondes
        }
    """
    debut_chrono = time.perf_counter()
    params = params or {}

    nb_salles = int(params.get("nb_salles", 4))
    horaires = params.get("horaires_ouverture", (8, 16))
    heure_debut, heure_fin = int(horaires[0]), int(horaires[1])
    duree_creneau = int(params.get("duree_creneau", 30))
    temps_limite = float(params.get("temps_limite", 30.0))

    debut_min = heure_debut * 60
    fin_min = heure_fin * 60

    if nb_salles <= 0 or duree_creneau <= 0 or fin_min <= debut_min:
        return _resultat_infaisable(debut_chrono, "Paramètres horaires ou nombre de salles invalides.")

    operations = _extraire_operations(data)
    nb_creneaux = (fin_min - debut_min) // duree_creneau

    if not operations:
        return {
            "statut": "optimal",
            "objectif": 0.0,
            "solution": {
                "planning": [],
                "taux_occupation": {f"Salle {s + 1}": 0.0 for s in range(nb_salles)} | {"global": 0.0},
                "heures_sup": 0.0,
            },
            "temps": round(time.perf_counter() - debut_chrono, 4),
        }

    durees_creneaux: list[int] = []
    for op in operations:
        duree_op = int(op["duree"])
        if duree_op <= 0:
            return _resultat_infaisable(debut_chrono, f"Durée invalide pour l'opération {op['id']}.")
        nb_slots_op = math.ceil(duree_op / duree_creneau)
        if nb_slots_op > nb_creneaux:
            return _resultat_infaisable(
                debut_chrono,
                f"L'opération {op['id']} ({duree_op} min) dépasse la plage d'ouverture.",
            )
        durees_creneaux.append(nb_slots_op)

    # Modèle PLNE minimal avec Gurobi (OutputFlag=0 évite la création de gurobi.log)
    with gp.Env(empty=True) as env:
        env.setParam("OutputFlag", 0)
        env.start()
        with gp.Model("m5_blocs_operatoires", env=env) as modele:
            modele.Params.TimeLimit = temps_limite

            x = {}
            for i, d_i in enumerate(durees_creneaux):
                for s in range(nb_salles):
                    for t in range(nb_creneaux - d_i + 1):
                        x[i, s, t] = modele.addVar(vtype=GRB.BINARY, name=f"x_{i}_{s}_{t}")

            fin_journee = modele.addVar(lb=0.0, ub=float(nb_creneaux), vtype=GRB.CONTINUOUS, name="fin_journee")

            # 1. Chaque opération est planifiée exactement une fois
            for i, d_i in enumerate(durees_creneaux):
                modele.addConstr(
                    gp.quicksum(
                        x[i, s, t]
                        for s in range(nb_salles)
                        for t in range(nb_creneaux - d_i + 1)
                    )
                    == 1,
                    name=f"affectation_op_{i}",
                )

            # 2. Non-chevauchement dans chaque salle sur chaque créneau
            for s in range(nb_salles):
                for u in range(nb_creneaux):
                    modele.addConstr(
                        gp.quicksum(
                            x[i, s, t]
                            for i, d_i in enumerate(durees_creneaux)
                            for t in range(max(0, u - d_i + 1), min(u, nb_creneaux - d_i) + 1)
                        )
                        <= 1,
                        name=f"salle_{s}_creneau_{u}",
                    )

            # 3. Non-chevauchement par chirurgien sur chaque créneau
            ops_par_chirurgien: dict[str, list[int]] = {}
            for i, op in enumerate(operations):
                chirurgien = str(op.get("chirurgien", f"CHIR-{i}"))
                ops_par_chirurgien.setdefault(chirurgien, []).append(i)

            for chirurgien, indices_ops in ops_par_chirurgien.items():
                if len(indices_ops) < 2:
                    continue
                for u in range(nb_creneaux):
                    modele.addConstr(
                        gp.quicksum(
                            x[i, s, t]
                            for i in indices_ops
                            for s in range(nb_salles)
                            for t in range(
                                max(0, u - durees_creneaux[i] + 1),
                                min(u, nb_creneaux - durees_creneaux[i]) + 1,
                            )
                        )
                        <= 1,
                        name=f"chir_{chirurgien}_creneau_{u}",
                    )

            # 4. Fin de journée (makespan en nombre de créneaux)
            for i, d_i in enumerate(durees_creneaux):
                modele.addConstr(
                    gp.quicksum(
                        (t + d_i) * x[i, s, t]
                        for s in range(nb_salles)
                        for t in range(nb_creneaux - d_i + 1)
                    )
                    <= fin_journee,
                    name=f"makespan_op_{i}",
                )

            modele.setObjective(fin_journee, GRB.MINIMIZE)
            modele.optimize()

            if modele.Status == GRB.OPTIMAL:
                statut = "optimal"
            elif modele.Status == GRB.TIME_LIMIT and modele.SolCount > 0:
                statut = "faisable"
            else:
                return _resultat_infaisable(debut_chrono, "Aucune solution réalisable trouvée par Gurobi.")

            planning: list[dict[str, Any]] = []
            occupation_salles = [0 for _ in range(nb_salles)]

            for i, op in enumerate(operations):
                d_i = durees_creneaux[i]
                for s in range(nb_salles):
                    for t in range(nb_creneaux - d_i + 1):
                        if x[i, s, t].X > 0.5:
                            debut_op = debut_min + t * duree_creneau
                            fin_op = debut_op + d_i * duree_creneau
                            occupation_salles[s] += d_i * duree_creneau
                            planning.append(
                                {
                                    "id": op["id"],
                                    "salle": f"Salle {s + 1}",
                                    "chirurgien": op.get("chirurgien", "Non assigné"),
                                    "priorite": int(op.get("priorite", 1)),
                                    "debut": _formater_heure(debut_op),
                                    "fin": _formater_heure(fin_op),
                                    "debut_min": debut_op,
                                    "fin_min": fin_op,
                                    "duree_min": int(op["duree"]),
                                }
                            )

            planning.sort(key=lambda item: (item["salle"], item["debut_min"]))

            capacite_salle_min = nb_creneaux * duree_creneau
            taux_occupation: dict[str, float] = {
                f"Salle {s + 1}": round(100.0 * occupation_salles[s] / capacite_salle_min, 1)
                for s in range(nb_salles)
            }
            capacite_totale_min = nb_salles * capacite_salle_min
            taux_occupation["global"] = round(100.0 * sum(occupation_salles) / capacite_totale_min, 1)

            objectif_minutes = round(float(fin_journee.X) * duree_creneau, 2)

            return {
                "statut": statut,
                "objectif": objectif_minutes,
                "solution": {
                    "planning": planning,
                    "taux_occupation": taux_occupation,
                    "heures_sup": 0.0,
                },
                "temps": round(time.perf_counter() - debut_chrono, 4),
            }
