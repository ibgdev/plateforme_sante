"""Module M5 — Planning des blocs opératoires.

Type    : Ordonnancement (Affectation salle × créneau)
Outils  : gurobipy (PLNE exact), OR-Tools CP-SAT (comparaison exacte),
          heuristique gloutonne (comparaison rapide), Plotly (Gantt)
Données : hopitaux.csv, personnel.csv, operations.csv
Branche : m5_blocs
"""

from __future__ import annotations

from collections.abc import Mapping
import math
import time
from typing import Any

import gurobipy as gp
from gurobipy import GRB
from ortools.sat.python import cp_model


MODES_OBJECTIF = ("makespan", "priorite")


def _operations_par_defaut() -> list[dict[str, Any]]:
    """Retourne un petit jeu d'opérations réaliste utilisé si `data` est vide."""
    return [
        {"id": "OP-01", "duree": 90, "chirurgien": "CHIR-1", "priorite": 1},
        {"id": "OP-02", "duree": 120, "chirurgien": "CHIR-2", "priorite": 1},
        {"id": "OP-03", "duree": 60, "chirurgien": "CHIR-1", "priorite": 2},
        {"id": "OP-04", "duree": 150, "chirurgien": "CHIR-3", "priorite": 2},
        {"id": "OP-05", "duree": 60, "chirurgien": "CHIR-2", "priorite": 3},
    ]


def _extraire_enregistrements(source: Any) -> list[dict[str, Any]]:
    """Convertit un DataFrame pandas ou une séquence de dictionnaires en liste de dicts."""
    if source is None:
        return []
    if hasattr(source, "to_dict") and not isinstance(source, Mapping):
        return [dict(ligne) for ligne in source.to_dict(orient="records")]
    return [dict(ligne) for ligne in source]


def _extraire_operations(data: dict) -> list[dict[str, Any]]:
    """Extrait et normalise la liste des opérations depuis `data`."""
    if not data or "operations" not in data or data["operations"] is None:
        return _operations_par_defaut()

    enregistrements = _extraire_enregistrements(data["operations"])
    operations: list[dict[str, Any]] = []
    for index, op in enumerate(enregistrements, start=1):
        priorite_brute = int(op.get("priorite", 1))
        operations.append(
            {
                "id": str(op.get("id", f"OP-{index:02d}")).strip(),
                "duree": int(op["duree"]),
                "chirurgien": str(op.get("chirurgien", f"CHIR-{index}")).strip(),
                "priorite": max(0, priorite_brute),
                "est_urgence": bool(op.get("est_urgence", priorite_brute == 0)),
            }
        )
    return operations


def _extraire_nb_salles(data: dict, params: dict) -> int:
    """Détermine le nombre de salles d'opération depuis `params` ou `data['hopitaux']`."""
    if "nb_salles" in params and params["nb_salles"] is not None:
        return int(params["nb_salles"])
    if data and data.get("hopitaux") is not None:
        hopitaux = _extraire_enregistrements(data["hopitaux"])
        if hopitaux and "nb_salles_operation" in hopitaux[0]:
            return int(hopitaux[0]["nb_salles_operation"])
    return 4


def _parser_horaire_minutes(valeur: Any) -> int:
    """Convertit une heure (`'08:30'`, `'8h30'`, `8`, ou minutes `510`) en minutes depuis minuit."""
    if isinstance(valeur, (int, float)) and not isinstance(valeur, bool):
        nombre = float(valeur)
        return int(round(nombre * 60)) if nombre <= 24 else int(round(nombre))

    texte = str(valeur).strip().lower().replace("h", ":")
    if ":" in texte:
        parties = texte.split(":")
        heures = int(parties[0])
        minutes = int(parties[1]) if len(parties) > 1 and parties[1] else 0
        return heures * 60 + minutes
    return int(round(float(texte) * 60))


def _parser_plages_texte(texte: str) -> list[tuple[int, int]]:
    """Parse une chaîne de plages horaires de la forme `'11:00-12:00;14:00-15:00'`."""
    plages: list[tuple[int, int]] = []
    if not texte:
        return plages
    for morceau in str(texte).replace(",", ";").split(";"):
        morceau = morceau.strip()
        if not morceau or "-" not in morceau:
            continue
        debut_txt, fin_txt = morceau.split("-", 1)
        debut_m = _parser_horaire_minutes(debut_txt)
        fin_m = _parser_horaire_minutes(fin_txt)
        if fin_m > debut_m:
            plages.append((debut_m, fin_m))
    return plages


def _extraire_indisponibilites(data: dict, params: dict) -> dict[str, list[tuple[int, int]]]:
    """Extrait les plages d'indisponibilité par chirurgien (en minutes depuis minuit).

    Si `params['indisponibilites']` est fourni (même vide `[]`), il prévaut sur
    `data['personnel']` afin de permettre la modification directe depuis l'interface web.
    """
    indispos: dict[str, list[tuple[int, int]]] = {}

    if "indisponibilites" in params and params["indisponibilites"] is not None:
        source = params["indisponibilites"]
        if isinstance(source, Mapping):
            for chir, plages in source.items():
                chir_id = str(chir).strip()
                if isinstance(plages, str):
                    indispos.setdefault(chir_id, []).extend(_parser_plages_texte(plages))
                else:
                    for plage in plages:
                        if isinstance(plage, Mapping):
                            deb = _parser_horaire_minutes(plage["debut"])
                            fin = _parser_horaire_minutes(plage["fin"])
                        else:
                            deb = _parser_horaire_minutes(plage[0])
                            fin = _parser_horaire_minutes(plage[1])
                        if fin > deb:
                            indispos.setdefault(chir_id, []).append((deb, fin))
        else:
            for entree in source:
                chir_id = str(entree["chirurgien"]).strip()
                deb = _parser_horaire_minutes(entree["debut"])
                fin = _parser_horaire_minutes(entree["fin"])
                if chir_id and fin > deb:
                    indispos.setdefault(chir_id, []).append((deb, fin))
        return indispos

    if data and data.get("personnel") is not None:
        for personne in _extraire_enregistrements(data["personnel"]):
            role = str(personne.get("role", "")).strip().lower()
            if role and role != "chirurgien":
                continue
            chir_id = str(personne.get("id", "")).strip()
            plages_brutes = personne.get("indisponibilites", "")
            if not chir_id or not plages_brutes:
                continue
            if isinstance(plages_brutes, str):
                plages = _parser_plages_texte(plages_brutes)
            else:
                plages = [
                    (_parser_horaire_minutes(p[0]), _parser_horaire_minutes(p[1]))
                    for p in plages_brutes
                    if _parser_horaire_minutes(p[1]) > _parser_horaire_minutes(p[0])
                ]
            if plages:
                indispos.setdefault(chir_id, []).extend(plages)

    return indispos


def _creneaux_bloques(
    indisponibilites: dict[str, list[tuple[int, int]]],
    debut_min: int,
    duree_creneau: int,
    nb_creneaux_total: int,
) -> dict[str, set[int]]:
    """Convertit les plages horaires d'indisponibilité en indices de créneaux bloqués."""
    bloques: dict[str, set[int]] = {}
    for chir, plages in indisponibilites.items():
        indices: set[int] = set()
        for u in range(nb_creneaux_total):
            deb_slot = debut_min + u * duree_creneau
            fin_slot = deb_slot + duree_creneau
            if any(deb_ind < fin_slot and deb_slot < fin_ind for deb_ind, fin_ind in plages):
                indices.add(u)
        bloques[chir] = indices
    return bloques


def _poids_priorite(priorite: int) -> int:
    """Transforme un niveau de priorité (0 = urgence vitale, 1 = urgent) en poids décroissant."""
    if int(priorite) == 0:
        return 10
    return max(1, 4 - int(priorite))


def _formater_heure(minutes_depuis_minuit: int) -> str:
    """Convertit un nombre de minutes depuis minuit au format HH:MM."""
    heures, minutes = divmod(minutes_depuis_minuit, 60)
    return f"{heures:02d}:{minutes:02d}"


def _indice_salle_depuis_nom(nom_salle: str) -> int:
    """Extrait l'indice 0-based depuis un libellé comme `'Salle 2'`."""
    morceaux = str(nom_salle).strip().split()
    return max(0, int(morceaux[-1]) - 1)


def _resultat_infaisable(
    debut_chrono: float,
    raison: str = "",
    comparaison: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Construit une réponse normalisée pour un problème infaisable."""
    solution: dict[str, Any] = {
        "planning": [],
        "taux_occupation": {"global": 0.0},
        "heures_sup": 0.0,
        "heures_sup_par_salle": {},
        "fin_journee_heure": "-",
        "message": raison,
    }
    if comparaison is not None:
        solution["comparaison"] = comparaison
    return {
        "statut": "infaisable",
        "objectif": 0.0,
        "solution": solution,
        "temps": round(time.perf_counter() - debut_chrono, 4),
    }


def _calculer_indicateurs(
    affectations: list[tuple[int, int, int]],
    operations: list[dict[str, Any]],
    durees_creneaux: list[int],
    nb_salles: int,
    debut_min: int,
    duree_creneau: int,
    nb_creneaux_normaux: int,
    mode_objectif: str,
    penalite_heures_sup: float,
) -> dict[str, Any]:
    """Calcule le planning détaillé, les taux d'occupation, heures sup et objectif exact."""
    planning: list[dict[str, Any]] = []
    occupation_salles_min = [0 for _ in range(nb_salles)]
    sup_salles_creneaux = [0 for _ in range(nb_salles)]
    makespan_creneaux = 0
    somme_ponderee_creneaux = 0

    for i, s, t in affectations:
        op = operations[i]
        d_i = durees_creneaux[i]
        fin_slot = t + d_i
        debut_op = debut_min + t * duree_creneau
        fin_op = debut_min + fin_slot * duree_creneau
        occupation_salles_min[s] += d_i * duree_creneau

        depassement = max(0, fin_slot - nb_creneaux_normaux)
        if depassement > sup_salles_creneaux[s]:
            sup_salles_creneaux[s] = depassement
        if fin_slot > makespan_creneaux:
            makespan_creneaux = fin_slot

        somme_ponderee_creneaux += _poids_priorite(op["priorite"]) * fin_slot

        planning.append(
            {
                "id": op["id"],
                "salle": f"Salle {s + 1}",
                "chirurgien": op["chirurgien"],
                "priorite": int(op["priorite"]),
                "debut": _formater_heure(debut_op),
                "fin": _formater_heure(fin_op),
                "debut_min": debut_op,
                "fin_min": fin_op,
                "duree_min": int(op["duree"]),
                "en_heures_sup": depassement > 0,
                "est_urgence": bool(op.get("est_urgence", False)),
                "figee": bool(op.get("figee", False)),
                "deplacee": bool(op.get("deplacee", False)),
            }
        )

    planning.sort(key=lambda item: (item["salle"], item["debut_min"]))

    capacite_salle_min = nb_creneaux_normaux * duree_creneau
    taux_occupation: dict[str, float] = {
        f"Salle {s + 1}": round(100.0 * occupation_salles_min[s] / capacite_salle_min, 1)
        for s in range(nb_salles)
    }
    capacite_totale_min = nb_salles * capacite_salle_min
    taux_occupation["global"] = round(100.0 * sum(occupation_salles_min) / capacite_totale_min, 1)

    heures_sup_par_salle: dict[str, float] = {
        f"Salle {s + 1}": round(sup_salles_creneaux[s] * duree_creneau / 60.0, 2)
        for s in range(nb_salles)
    }
    total_sup_creneaux = sum(sup_salles_creneaux)
    total_heures_sup = round(total_sup_creneaux * duree_creneau / 60.0, 2)
    makespan_min = makespan_creneaux * duree_creneau

    if mode_objectif == "priorite":
        objectif = round(
            (somme_ponderee_creneaux + penalite_heures_sup * total_sup_creneaux) * duree_creneau,
            2,
        )
    else:
        objectif = round(
            (makespan_creneaux + penalite_heures_sup * total_sup_creneaux) * duree_creneau,
            2,
        )

    return {
        "objectif": objectif,
        "makespan_min": makespan_min,
        "fin_journee_heure": _formater_heure(debut_min + makespan_min),
        "planning": planning,
        "taux_occupation": taux_occupation,
        "heures_sup": total_heures_sup,
        "heures_sup_par_salle": heures_sup_par_salle,
    }


def _preparer_probleme(data: dict, params: dict) -> tuple[dict[str, Any] | None, str]:
    """Valide et prépare toutes les structures communes aux solveurs (Gurobi, CP-SAT, heuristique)."""
    params = params or {}
    nb_salles = _extraire_nb_salles(data, params)
    horaires = params.get("horaires_ouverture", (8, 16))
    debut_min = _parser_horaire_minutes(horaires[0])
    fin_min = _parser_horaire_minutes(horaires[1])
    duree_creneau = int(params.get("duree_creneau", 30))
    max_heures_sup = float(params.get("max_heures_sup", 2.0))
    penalite_heures_sup = float(params.get("penalite_heures_sup", 10.0))
    mode_objectif = str(params.get("mode_objectif", "makespan")).strip().lower()
    temps_limite = float(params.get("temps_limite", 30.0))

    if mode_objectif not in MODES_OBJECTIF:
        return None, f"Mode d'objectif inconnu : {mode_objectif}."
    if nb_salles <= 0 or duree_creneau <= 0 or fin_min <= debut_min:
        return None, "Paramètres horaires ou nombre de salles invalides."
    if max_heures_sup < 0 or penalite_heures_sup < 0:
        return None, "Les paramètres d'heures supplémentaires doivent être positifs ou nuls."

    operations = _extraire_operations(data)
    nb_creneaux_normaux = (fin_min - debut_min) // duree_creneau
    if nb_creneaux_normaux <= 0:
        return None, "La plage d'ouverture est inférieure à la durée d'un créneau."

    nb_creneaux_sup = int(math.floor((max_heures_sup * 60.0) / duree_creneau))
    nb_creneaux_total = nb_creneaux_normaux + nb_creneaux_sup

    durees_creneaux: list[int] = []
    for op in operations:
        duree_op = int(op["duree"])
        if duree_op <= 0:
            return None, f"Durée invalide pour l'opération {op['id']}."
        nb_slots_op = math.ceil(duree_op / duree_creneau)
        if nb_slots_op > nb_creneaux_total:
            return (
                None,
                f"L'opération {op['id']} ({duree_op} min) dépasse l'amplitude maximale autorisée.",
            )
        durees_creneaux.append(nb_slots_op)

    indisponibilites = _extraire_indisponibilites(data, params)
    bloques_chir = _creneaux_bloques(indisponibilites, debut_min, duree_creneau, nb_creneaux_total)

    debuts_valides: list[list[int]] = []
    for i, op in enumerate(operations):
        d_i = durees_creneaux[i]
        bloques = bloques_chir.get(op["chirurgien"], set())
        valides = [
            t
            for t in range(nb_creneaux_total - d_i + 1)
            if not any(u in bloques for u in range(t, t + d_i))
        ]
        if not valides:
            return (
                None,
                f"Aucun créneau disponible pour {op['id']} compte tenu des indisponibilités de {op['chirurgien']}.",
            )
        debuts_valides.append(valides)

    return {
        "nb_salles": nb_salles,
        "debut_min": debut_min,
        "fin_min": fin_min,
        "duree_creneau": duree_creneau,
        "nb_creneaux_normaux": nb_creneaux_normaux,
        "nb_creneaux_sup": nb_creneaux_sup,
        "nb_creneaux_total": nb_creneaux_total,
        "operations": operations,
        "durees_creneaux": durees_creneaux,
        "indisponibilites": indisponibilites,
        "bloques_chir": bloques_chir,
        "debuts_valides": debuts_valides,
        "mode_objectif": mode_objectif,
        "penalite_heures_sup": penalite_heures_sup,
        "temps_limite": temps_limite,
    }, ""


def _executer_heuristique(pb: dict[str, Any]) -> dict[str, Any]:
    """Heuristique gloutonne (Priorité / LPT — First-Fit) pour comparaison (Section 5.2)."""
    debut_chrono = time.perf_counter()
    operations = pb["operations"]
    durees_creneaux = pb["durees_creneaux"]
    nb_salles = pb["nb_salles"]
    nb_creneaux_total = pb["nb_creneaux_total"]
    debuts_valides = pb["debuts_valides"]

    if not operations:
        return {
            "statut": "optimal",
            "objectif": 0.0,
            "fin_journee_min": 0,
            "fin_journee_heure": _formater_heure(pb["debut_min"]),
            "heures_sup": 0.0,
            "planning": [],
            "taux_occupation": {f"Salle {s + 1}": 0.0 for s in range(nb_salles)} | {"global": 0.0},
            "heures_sup_par_salle": {f"Salle {s + 1}": 0.0 for s in range(nb_salles)},
            "temps": round(time.perf_counter() - debut_chrono, 4),
        }

    if pb["mode_objectif"] == "priorite":
        ordre = sorted(
            range(len(operations)),
            key=lambda i: (operations[i]["priorite"], -durees_creneaux[i], operations[i]["id"]),
        )
    else:
        ordre = sorted(
            range(len(operations)),
            key=lambda i: (-durees_creneaux[i], operations[i]["priorite"], operations[i]["id"]),
        )

    salles_occupees = [[False] * nb_creneaux_total for _ in range(nb_salles)]
    chir_occupes: dict[str, list[bool]] = {}
    affectations: list[tuple[int, int, int]] = []

    for i in ordre:
        op = operations[i]
        d_i = durees_creneaux[i]
        chir = op["chirurgien"]
        occupation_chir = chir_occupes.setdefault(chir, [False] * nb_creneaux_total)

        place = False
        for t in debuts_valides[i]:
            if any(occupation_chir[u] for u in range(t, t + d_i)):
                continue
            for s in range(nb_salles):
                if any(salles_occupees[s][u] for u in range(t, t + d_i)):
                    continue
                for u in range(t, t + d_i):
                    salles_occupees[s][u] = True
                    occupation_chir[u] = True
                affectations.append((i, s, t))
                place = True
                break
            if place:
                break

        if not place:
            return {
                "statut": "infaisable",
                "objectif": 0.0,
                "fin_journee_min": 0,
                "fin_journee_heure": "-",
                "heures_sup": 0.0,
                "planning": [],
                "taux_occupation": {"global": 0.0},
                "heures_sup_par_salle": {},
                "temps": round(time.perf_counter() - debut_chrono, 4),
            }

    ind = _calculer_indicateurs(
        affectations=affectations,
        operations=operations,
        durees_creneaux=durees_creneaux,
        nb_salles=nb_salles,
        debut_min=pb["debut_min"],
        duree_creneau=pb["duree_creneau"],
        nb_creneaux_normaux=pb["nb_creneaux_normaux"],
        mode_objectif=pb["mode_objectif"],
        penalite_heures_sup=pb["penalite_heures_sup"],
    )
    return {
        "statut": "faisable",
        "objectif": ind["objectif"],
        "fin_journee_min": ind["makespan_min"],
        "fin_journee_heure": ind["fin_journee_heure"],
        "heures_sup": ind["heures_sup"],
        "planning": ind["planning"],
        "taux_occupation": ind["taux_occupation"],
        "heures_sup_par_salle": ind["heures_sup_par_salle"],
        "temps": round(time.perf_counter() - debut_chrono, 4),
    }


def _executer_cpsat(pb: dict[str, Any]) -> dict[str, Any]:
    """Résout le même problème par programmation par contraintes (OR-Tools CP-SAT)."""
    debut_chrono = time.perf_counter()
    operations = pb["operations"]
    durees_creneaux = pb["durees_creneaux"]
    nb_salles = pb["nb_salles"]
    nb_creneaux_normaux = pb["nb_creneaux_normaux"]
    nb_creneaux_sup = pb["nb_creneaux_sup"]
    nb_creneaux_total = pb["nb_creneaux_total"]
    debuts_valides = pb["debuts_valides"]

    if not operations:
        return {
            "statut": "optimal",
            "objectif": 0.0,
            "fin_journee_min": 0,
            "fin_journee_heure": _formater_heure(pb["debut_min"]),
            "heures_sup": 0.0,
            "planning": [],
            "taux_occupation": {f"Salle {s + 1}": 0.0 for s in range(nb_salles)} | {"global": 0.0},
            "heures_sup_par_salle": {f"Salle {s + 1}": 0.0 for s in range(nb_salles)},
            "temps": round(time.perf_counter() - debut_chrono, 4),
        }

    modele = cp_model.CpModel()
    debut_vars = []
    fin_vars = []
    present: dict[tuple[int, int], cp_model.IntVar] = {}
    intervalles_par_salle: list[list[cp_model.IntervalVar]] = [[] for _ in range(nb_salles)]
    intervalles_par_chir: dict[str, list[cp_model.IntervalVar]] = {}

    for i, op in enumerate(operations):
        d_i = durees_creneaux[i]
        domaine_debut = cp_model.Domain.FromValues(debuts_valides[i])
        deb_v = modele.NewIntVarFromDomain(domaine_debut, f"debut_{i}")
        fin_v = modele.NewIntVar(d_i, nb_creneaux_total, f"fin_{i}")
        modele.Add(fin_v == deb_v + d_i)
        debut_vars.append(deb_v)
        fin_vars.append(fin_v)

        # Intervalle chirurgien (toujours actif)
        int_chir = modele.NewIntervalVar(deb_v, d_i, fin_v, f"int_chir_{i}")
        intervalles_par_chir.setdefault(op["chirurgien"], []).append(int_chir)

        # Intervalles optionnels par salle
        presences_op = []
        for s in range(nb_salles):
            p_is = modele.NewBoolVar(f"p_{i}_{s}")
            present[i, s] = p_is
            presences_op.append(p_is)
            int_salle = modele.NewOptionalIntervalVar(deb_v, d_i, fin_v, p_is, f"int_{i}_{s}")
            intervalles_par_salle[s].append(int_salle)

        modele.AddExactlyOne(presences_op)

    # Non-chevauchement par salle et par chirurgien
    for s in range(nb_salles):
        modele.AddNoOverlap(intervalles_par_salle[s])
    for intervalles in intervalles_par_chir.values():
        if len(intervalles) >= 2:
            modele.AddNoOverlap(intervalles)

    # Heures supplémentaires par salle
    sup_salle = [modele.NewIntVar(0, nb_creneaux_sup, f"sup_{s}") for s in range(nb_salles)]
    for i in range(len(operations)):
        for s in range(nb_salles):
            modele.Add(fin_vars[i] - nb_creneaux_normaux <= sup_salle[s]).OnlyEnforceIf(present[i, s])

    # Makespan
    fin_journee = modele.NewIntVar(0, nb_creneaux_total, "fin_journee")
    modele.AddMaxEquality(fin_journee, fin_vars)

    # Objectif entier (mise à l'échelle par 100 pour gérer les pénalités flottantes)
    echelle = 100
    pen_sup = round(pb["penalite_heures_sup"] * echelle)
    if pb["mode_objectif"] == "priorite":
        terme_principal = sum(
            _poids_priorite(operations[i]["priorite"]) * echelle * fin_vars[i]
            for i in range(len(operations))
        )
        modele.Minimize(terme_principal + pen_sup * sum(sup_salle))
    else:
        modele.Minimize(echelle * fin_journee + pen_sup * sum(sup_salle))

    solveur = cp_model.CpSolver()
    solveur.parameters.max_time_in_seconds = float(pb["temps_limite"])
    solveur.parameters.num_search_workers = 4
    statut_cp = solveur.Solve(modele)
    temps_cp = round(time.perf_counter() - debut_chrono, 4)

    if statut_cp not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {
            "statut": "infaisable",
            "objectif": 0.0,
            "fin_journee_min": 0,
            "fin_journee_heure": "-",
            "heures_sup": 0.0,
            "planning": [],
            "taux_occupation": {"global": 0.0},
            "heures_sup_par_salle": {},
            "temps": temps_cp,
        }

    affectations: list[tuple[int, int, int]] = []
    for i in range(len(operations)):
        t_val = int(solveur.Value(debut_vars[i]))
        s_val = next(s for s in range(nb_salles) if solveur.Value(present[i, s]) == 1)
        affectations.append((i, s_val, t_val))

    ind = _calculer_indicateurs(
        affectations=affectations,
        operations=operations,
        durees_creneaux=durees_creneaux,
        nb_salles=nb_salles,
        debut_min=pb["debut_min"],
        duree_creneau=pb["duree_creneau"],
        nb_creneaux_normaux=nb_creneaux_normaux,
        mode_objectif=pb["mode_objectif"],
        penalite_heures_sup=pb["penalite_heures_sup"],
    )
    return {
        "statut": "optimal" if statut_cp == cp_model.OPTIMAL else "faisable",
        "objectif": ind["objectif"],
        "fin_journee_min": ind["makespan_min"],
        "fin_journee_heure": ind["fin_journee_heure"],
        "heures_sup": ind["heures_sup"],
        "planning": ind["planning"],
        "taux_occupation": ind["taux_occupation"],
        "heures_sup_par_salle": ind["heures_sup_par_salle"],
        "temps": temps_cp,
    }


def resoudre_heuristique(data: dict, params: dict) -> dict[str, Any]:
    """Point d'entrée public pour exécuter l'heuristique gloutonne au format commun."""
    debut_chrono = time.perf_counter()
    pb, erreur = _preparer_probleme(data, params)
    if pb is None:
        return _resultat_infaisable(debut_chrono, erreur)

    res = _executer_heuristique(pb)
    return {
        "statut": res["statut"],
        "objectif": res["objectif"],
        "solution": {
            "planning": res["planning"],
            "taux_occupation": res["taux_occupation"],
            "heures_sup": res["heures_sup"],
            "heures_sup_par_salle": res["heures_sup_par_salle"],
            "fin_journee_heure": res["fin_journee_heure"],
        },
        "temps": res["temps"],
    }


def resoudre_cpsat(data: dict, params: dict) -> dict[str, Any]:
    """Point d'entrée public pour résoudre le problème avec OR-Tools CP-SAT au format commun."""
    debut_chrono = time.perf_counter()
    pb, erreur = _preparer_probleme(data, params)
    if pb is None:
        return _resultat_infaisable(debut_chrono, erreur)

    res = _executer_cpsat(pb)
    return {
        "statut": res["statut"],
        "objectif": res["objectif"],
        "solution": {
            "planning": res["planning"],
            "taux_occupation": res["taux_occupation"],
            "heures_sup": res["heures_sup"],
            "heures_sup_par_salle": res["heures_sup_par_salle"],
            "fin_journee_heure": res["fin_journee_heure"],
        },
        "temps": res["temps"],
    }


def _calculer_ecart_pct(statut_ref: str, obj_ref: float, statut_autre: str, obj_autre: float) -> float | None:
    """Calcule l'écart en pourcentage d'une méthode par rapport à Gurobi."""
    if statut_ref in {"optimal", "faisable"} and statut_autre in {"optimal", "faisable"} and obj_ref > 0:
        return round(100.0 * (obj_autre - obj_ref) / obj_ref, 2)
    return None


def _construire_comparaison(
    statut_gurobi: str,
    objectif_gurobi: float,
    makespan_min_gurobi: int,
    fin_heure_gurobi: str,
    heures_sup_gurobi: float,
    temps_gurobi: float,
    res_cpsat: dict[str, Any],
    res_heur: dict[str, Any],
) -> dict[str, Any]:
    """Assemble le tableau comparatif à 3 méthodes (Gurobi PLNE, OR-Tools CP-SAT, Heuristique)."""
    return {
        "methodes": [
            {
                "nom": "Gurobi (PLNE exact)",
                "statut": statut_gurobi,
                "objectif": objectif_gurobi,
                "fin_journee_min": makespan_min_gurobi,
                "fin_journee_heure": fin_heure_gurobi,
                "heures_sup": heures_sup_gurobi,
                "temps": temps_gurobi,
                "ecart_pct": 0.0 if statut_gurobi in {"optimal", "faisable"} else None,
            },
            {
                "nom": "OR-Tools CP-SAT (Contraintes exact)",
                "statut": res_cpsat["statut"],
                "objectif": res_cpsat["objectif"],
                "fin_journee_min": res_cpsat["fin_journee_min"],
                "fin_journee_heure": res_cpsat["fin_journee_heure"],
                "heures_sup": res_cpsat["heures_sup"],
                "temps": res_cpsat["temps"],
                "ecart_pct": _calculer_ecart_pct(
                    statut_gurobi, objectif_gurobi, res_cpsat["statut"], res_cpsat["objectif"]
                ),
            },
            {
                "nom": "Heuristique gloutonne (Priorité / LPT)",
                "statut": res_heur["statut"],
                "objectif": res_heur["objectif"],
                "fin_journee_min": res_heur["fin_journee_min"],
                "fin_journee_heure": res_heur["fin_journee_heure"],
                "heures_sup": res_heur["heures_sup"],
                "temps": res_heur["temps"],
                "ecart_pct": _calculer_ecart_pct(
                    statut_gurobi, objectif_gurobi, res_heur["statut"], res_heur["objectif"]
                ),
            },
        ]
    }


def solve(data: dict, params: dict) -> dict:
    """Résout le problème d'ordonnancement des blocs opératoires avec Gurobi (PLNE).

    Paramètres attendus dans `params` :
        - nb_salles (int) : nombre de salles d'opération (défaut : 4 ou `hopitaux.csv`).
        - horaires_ouverture (tuple) : `(heure_debut, heure_fin)` (défaut : `(8, 16)`).
        - duree_creneau (int) : durée d'un créneau en minutes (défaut : 30).
        - max_heures_sup (float) : plafond d'heures supplémentaires autorisées (défaut : 2.0).
        - mode_objectif (str) : `"makespan"` (défaut) ou `"priorite"`.
        - penalite_heures_sup (float) : pénalité par créneau supplémentaire (défaut : 10.0).
        - indisponibilites (list|dict) : plages d'indisponibilité des chirurgiens.
        - comparer (bool) : compare Gurobi, OR-Tools CP-SAT et l'heuristique (défaut : True).

    Retour attendu :
        {
            "statut": str,       # "optimal", "faisable", "infaisable"
            "objectif": float,   # valeur de la fonction objectif
            "solution": dict,    # planning, taux_occupation, heures_sup, comparaison...
            "temps": float,      # temps de calcul en secondes
        }
    """
    debut_chrono = time.perf_counter()
    params = params or {}
    comparer = bool(params.get("comparer", True))

    pb, erreur = _preparer_probleme(data, params)
    if pb is None:
        return _resultat_infaisable(debut_chrono, erreur)

    operations = pb["operations"]
    nb_salles = pb["nb_salles"]
    nb_creneaux_normaux = pb["nb_creneaux_normaux"]
    nb_creneaux_sup = pb["nb_creneaux_sup"]
    nb_creneaux_total = pb["nb_creneaux_total"]
    durees_creneaux = pb["durees_creneaux"]
    debuts_valides = pb["debuts_valides"]

    if not operations:
        temps_vide = round(time.perf_counter() - debut_chrono, 4)
        solution_vide: dict[str, Any] = {
            "planning": [],
            "taux_occupation": {f"Salle {s + 1}": 0.0 for s in range(nb_salles)} | {"global": 0.0},
            "heures_sup": 0.0,
            "heures_sup_par_salle": {f"Salle {s + 1}": 0.0 for s in range(nb_salles)},
            "fin_journee_heure": _formater_heure(pb["debut_min"]),
        }
        if comparer:
            res_cp = _executer_cpsat(pb)
            res_heur = _executer_heuristique(pb)
            solution_vide["comparaison"] = _construire_comparaison(
                "optimal", 0.0, 0, _formater_heure(pb["debut_min"]), 0.0, temps_vide, res_cp, res_heur
            )
        return {
            "statut": "optimal",
            "objectif": 0.0,
            "solution": solution_vide,
            "temps": temps_vide,
        }

    valides_set = [set(v) for v in debuts_valides]

    with gp.Env(empty=True) as env:
        env.setParam("OutputFlag", 0)
        env.start()
        with gp.Model("m5_blocs_operatoires", env=env) as modele:
            modele.Params.TimeLimit = pb["temps_limite"]

            x: dict[tuple[int, int, int], gp.Var] = {}
            for i in range(len(operations)):
                for s in range(nb_salles):
                    for t in debuts_valides[i]:
                        x[i, s, t] = modele.addVar(vtype=GRB.BINARY, name=f"x_{i}_{s}_{t}")

            fin_journee = modele.addVar(
                lb=0.0, ub=float(nb_creneaux_total), vtype=GRB.CONTINUOUS, name="fin_journee"
            )
            sup_salle = [
                modele.addVar(lb=0.0, ub=float(nb_creneaux_sup), vtype=GRB.CONTINUOUS, name=f"sup_{s}")
                for s in range(nb_salles)
            ]

            # 1. Affectation unique par opération
            for i in range(len(operations)):
                modele.addConstr(
                    gp.quicksum(x[i, s, t] for s in range(nb_salles) for t in debuts_valides[i]) == 1,
                    name=f"affectation_op_{i}",
                )

            # 2. Non-chevauchement par salle
            for s in range(nb_salles):
                for u in range(nb_creneaux_total):
                    termes = [
                        x[i, s, t]
                        for i, d_i in enumerate(durees_creneaux)
                        for t in range(max(0, u - d_i + 1), min(u, nb_creneaux_total - d_i) + 1)
                        if t in valides_set[i]
                    ]
                    if termes:
                        modele.addConstr(gp.quicksum(termes) <= 1, name=f"salle_{s}_creneau_{u}")

            # 3. Non-chevauchement par chirurgien
            ops_par_chirurgien: dict[str, list[int]] = {}
            for i, op in enumerate(operations):
                ops_par_chirurgien.setdefault(op["chirurgien"], []).append(i)

            for chir, indices_ops in ops_par_chirurgien.items():
                if len(indices_ops) < 2:
                    continue
                for u in range(nb_creneaux_total):
                    termes = [
                        x[i, s, t]
                        for i in indices_ops
                        for s in range(nb_salles)
                        for t in range(
                            max(0, u - durees_creneaux[i] + 1),
                            min(u, nb_creneaux_total - durees_creneaux[i]) + 1,
                        )
                        if t in valides_set[i]
                    ]
                    if termes:
                        modele.addConstr(gp.quicksum(termes) <= 1, name=f"chir_{chir}_creneau_{u}")

            # 4. Heures supplémentaires par salle
            if nb_creneaux_sup > 0:
                for i, d_i in enumerate(durees_creneaux):
                    for s in range(nb_salles):
                        termes_sup = [
                            (t + d_i - nb_creneaux_normaux) * x[i, s, t]
                            for t in debuts_valides[i]
                            if t + d_i > nb_creneaux_normaux
                        ]
                        if termes_sup:
                            modele.addConstr(gp.quicksum(termes_sup) <= sup_salle[s], name=f"sup_{i}_{s}")

            # 5. Fin de journée (makespan)
            for i, d_i in enumerate(durees_creneaux):
                modele.addConstr(
                    gp.quicksum(
                        (t + d_i) * x[i, s, t] for s in range(nb_salles) for t in debuts_valides[i]
                    )
                    <= fin_journee,
                    name=f"makespan_op_{i}",
                )

            cout_sup = pb["penalite_heures_sup"] * gp.quicksum(sup_salle)
            if pb["mode_objectif"] == "priorite":
                terme_priorite = gp.quicksum(
                    _poids_priorite(operations[i]["priorite"]) * (t + durees_creneaux[i]) * x[i, s, t]
                    for i in range(len(operations))
                    for s in range(nb_salles)
                    for t in debuts_valides[i]
                )
                modele.setObjective(terme_priorite + cout_sup, GRB.MINIMIZE)
            else:
                modele.setObjective(fin_journee + cout_sup, GRB.MINIMIZE)

            modele.optimize()
            temps_gurobi = round(time.perf_counter() - debut_chrono, 4)

            if modele.Status == GRB.OPTIMAL:
                statut = "optimal"
            elif modele.Status == GRB.TIME_LIMIT and modele.SolCount > 0:
                statut = "faisable"
            else:
                comparaison = None
                if comparer:
                    res_cp = _executer_cpsat(pb)
                    res_heur = _executer_heuristique(pb)
                    comparaison = _construire_comparaison(
                        "infaisable", 0.0, 0, "-", 0.0, temps_gurobi, res_cp, res_heur
                    )
                return _resultat_infaisable(
                    debut_chrono,
                    "Aucune solution réalisable trouvée par Gurobi avec les contraintes actuelles.",
                    comparaison=comparaison,
                )

            affectations: list[tuple[int, int, int]] = []
            for i in range(len(operations)):
                for s in range(nb_salles):
                    for t in debuts_valides[i]:
                        if x[i, s, t].X > 0.5:
                            affectations.append((i, s, t))

            ind = _calculer_indicateurs(
                affectations=affectations,
                operations=operations,
                durees_creneaux=durees_creneaux,
                nb_salles=nb_salles,
                debut_min=pb["debut_min"],
                duree_creneau=pb["duree_creneau"],
                nb_creneaux_normaux=nb_creneaux_normaux,
                mode_objectif=pb["mode_objectif"],
                penalite_heures_sup=pb["penalite_heures_sup"],
            )

            solution: dict[str, Any] = {
                "planning": ind["planning"],
                "taux_occupation": ind["taux_occupation"],
                "heures_sup": ind["heures_sup"],
                "heures_sup_par_salle": ind["heures_sup_par_salle"],
                "fin_journee_heure": ind["fin_journee_heure"],
                "mode_objectif": pb["mode_objectif"],
                "indisponibilites": {
                    chir: [(_formater_heure(d), _formater_heure(f)) for d, f in plages]
                    for chir, plages in pb["indisponibilites"].items()
                },
            }

            if comparer:
                res_cp = _executer_cpsat(pb)
                res_heur = _executer_heuristique(pb)
                solution["comparaison"] = _construire_comparaison(
                    statut,
                    ind["objectif"],
                    ind["makespan_min"],
                    ind["fin_journee_heure"],
                    ind["heures_sup"],
                    temps_gurobi,
                    res_cp,
                    res_heur,
                )

            return {
                "statut": statut,
                "objectif": ind["objectif"],
                "solution": solution,
                "temps": temps_gurobi,
            }


def inserer_urgence(
    planning_existant: list[dict[str, Any]],
    urgence: dict[str, Any],
    heure_arrivee: Any,
    data: dict | None = None,
    params: dict | None = None,
) -> dict[str, Any]:
    """Insère une opération urgente dans un planning existant via ré-optimisation Gurobi.

    Règles appliquées (Bonus « Pour aller plus loin », fiche M5) :
      - Les opérations déjà commencées avant `heure_arrivee` (`debut_min < t0`) sont figées
        (même salle, même horaire) et continuent d'occuper leur salle et leur chirurgien.
      - Les opérations non encore démarrées (`debut_min >= t0`) et la nouvelle urgence
        (qui ne peut démarrer qu'à partir de `heure_arrivee`) sont ré-optimisées ensemble
        pour placer l'urgence le plus tôt possible en minimisant heures sup et perturbations.
    """
    debut_chrono = time.perf_counter()
    data = data or {}
    params = params or {}

    nb_salles = _extraire_nb_salles(data, params)
    horaires = params.get("horaires_ouverture", (8, 16))
    debut_min = _parser_horaire_minutes(horaires[0])
    fin_min = _parser_horaire_minutes(horaires[1])
    duree_creneau = int(params.get("duree_creneau", 30))
    max_heures_sup = float(params.get("max_heures_sup", 2.0))
    penalite_heures_sup = float(params.get("penalite_heures_sup", 10.0))
    mode_objectif = str(params.get("mode_objectif", "makespan")).strip().lower()
    temps_limite = float(params.get("temps_limite", 30.0))

    if nb_salles <= 0 or duree_creneau <= 0 or fin_min <= debut_min or max_heures_sup < 0:
        return _resultat_infaisable(debut_chrono, "Paramètres horaires ou nombre de salles invalides.")
    if mode_objectif not in MODES_OBJECTIF:
        return _resultat_infaisable(debut_chrono, f"Mode d'objectif inconnu : {mode_objectif}.")

    nb_creneaux_normaux = (fin_min - debut_min) // duree_creneau
    nb_creneaux_sup = int(math.floor((max_heures_sup * 60.0) / duree_creneau))
    nb_creneaux_total = nb_creneaux_normaux + nb_creneaux_sup
    fin_max_min = debut_min + nb_creneaux_total * duree_creneau

    t0_min = _parser_horaire_minutes(heure_arrivee)
    if t0_min < debut_min or t0_min >= fin_max_min:
        return _resultat_infaisable(
            debut_chrono,
            f"L'heure d'arrivée de l'urgence ({_formater_heure(t0_min)}) est hors de l'amplitude opératoire.",
        )

    duree_urg = int(urgence.get("duree", 0))
    chir_urg = str(urgence.get("chirurgien", "")).strip()
    if duree_urg <= 0 or not chir_urg:
        return _resultat_infaisable(debut_chrono, "La durée (> 0) et le chirurgien de l'urgence sont requis.")

    # Génère un identifiant unique pour l'urgence si besoin
    ids_existants = {str(op["id"]) for op in planning_existant}
    urg_id = str(urgence.get("id", "")).strip() or "URG-01"
    compteur = 1
    while urg_id in ids_existants:
        compteur += 1
        urg_id = f"URG-{compteur:02d}"

    t0_slot = max(0, math.ceil((t0_min - debut_min) / duree_creneau))
    d_urg = math.ceil(duree_urg / duree_creneau)
    if t0_slot + d_urg > nb_creneaux_total:
        return _resultat_infaisable(
            debut_chrono,
            f"L'urgence {urg_id} ({duree_urg} min) arrivée à {_formater_heure(t0_min)} dépasse l'amplitude maximale.",
        )

    # Calcul des heures sup initiales (pour l'indicateur d'impact)
    sup_avant_salles = [0 for _ in range(nb_salles)]
    for op in planning_existant:
        s_idx = _indice_salle_depuis_nom(op["salle"])
        fin_slot_op = (int(op["fin_min"]) - debut_min) // duree_creneau
        dep = max(0, fin_slot_op - nb_creneaux_normaux)
        if s_idx < nb_salles and dep > sup_avant_salles[s_idx]:
            sup_avant_salles[s_idx] = dep
    heures_sup_avant = round(sum(sup_avant_salles) * duree_creneau / 60.0, 2)

    # Partition entre opérations figées (commencées avant t0) et reprogrammables (>= t0)
    ops_figees: list[dict[str, Any]] = []
    aff_figees: list[tuple[int, int, int]] = []
    durees_figees: list[int] = []
    bloques_salle: list[set[int]] = [set() for _ in range(nb_salles)]
    bloques_chir_figees: dict[str, set[int]] = {}
    min_sup_salle = [0 for _ in range(nb_salles)]
    min_makespan = 0

    ops_mobiles: list[dict[str, Any]] = []
    avant_par_id: dict[str, dict[str, Any]] = {}

    for op in planning_existant:
        deb_op = int(op["debut_min"])
        fin_op = int(op["fin_min"])
        duree_reelle = int(op.get("duree_min", op.get("duree", fin_op - deb_op)))
        chir = str(op["chirurgien"]).strip()
        s_idx = _indice_salle_depuis_nom(op["salle"])

        if deb_op < t0_min:
            t_fixe = max(0, (deb_op - debut_min) // duree_creneau)
            d_fixe = max(1, math.ceil((fin_op - deb_op) / duree_creneau))
            idx_f = len(ops_figees)
            ops_figees.append(
                {
                    "id": str(op["id"]),
                    "duree": duree_reelle,
                    "chirurgien": chir,
                    "priorite": int(op.get("priorite", 1)),
                    "est_urgence": bool(op.get("est_urgence", False)),
                    "figee": True,
                    "deplacee": False,
                }
            )
            durees_figees.append(d_fixe)
            aff_figees.append((idx_f, s_idx, t_fixe))
            for u in range(t_fixe, min(nb_creneaux_total, t_fixe + d_fixe)):
                if 0 <= s_idx < nb_salles:
                    bloques_salle[s_idx].add(u)
                bloques_chir_figees.setdefault(chir, set()).add(u)
            fin_slot_f = t_fixe + d_fixe
            if 0 <= s_idx < nb_salles:
                min_sup_salle[s_idx] = max(min_sup_salle[s_idx], max(0, fin_slot_f - nb_creneaux_normaux))
            min_makespan = max(min_makespan, fin_slot_f)
        else:
            avant_par_id[str(op["id"])] = {
                "salle": op["salle"],
                "s_idx": s_idx,
                "debut": op["debut"],
                "fin": op["fin"],
                "debut_min": deb_op,
                "t_slot": (deb_op - debut_min) // duree_creneau,
            }
            ops_mobiles.append(
                {
                    "id": str(op["id"]),
                    "duree": duree_reelle,
                    "chirurgien": chir,
                    "priorite": int(op.get("priorite", 1)),
                    "est_urgence": bool(op.get("est_urgence", False)),
                    "figee": False,
                    "deplacee": False,
                }
            )

    # Ajout de la nouvelle urgence parmi les opérations mobiles
    index_urgence = len(ops_mobiles)
    ops_mobiles.append(
        {
            "id": urg_id,
            "duree": duree_urg,
            "chirurgien": chir_urg,
            "priorite": 0,
            "est_urgence": True,
            "figee": False,
            "deplacee": False,
        }
    )

    durees_mobiles = [math.ceil(int(op["duree"]) / duree_creneau) for op in ops_mobiles]
    indisponibilites = _extraire_indisponibilites(data, params)
    bloques_indisp = _creneaux_bloques(indisponibilites, debut_min, duree_creneau, nb_creneaux_total)

    # Créneaux (s, t) valides pour chaque opération mobile (t >= t0_slot, hors salles/chirurgiens bloqués)
    paires_valides: list[list[tuple[int, int]]] = []
    for i, op in enumerate(ops_mobiles):
        d_i = durees_mobiles[i]
        chir = op["chirurgien"]
        bloques_c = bloques_indisp.get(chir, set()) | bloques_chir_figees.get(chir, set())
        paires: list[tuple[int, int]] = []
        for t in range(t0_slot, nb_creneaux_total - d_i + 1):
            if any(u in bloques_c for u in range(t, t + d_i)):
                continue
            for s in range(nb_salles):
                if any(u in bloques_salle[s] for u in range(t, t + d_i)):
                    continue
                paires.append((s, t))
        if not paires:
            return _resultat_infaisable(
                debut_chrono,
                f"Impossible de reprogrammer {op['id']} après {_formater_heure(t0_min)} "
                f"(essayez d'augmenter les heures supplémentaires ou de changer de chirurgien).",
            )
        paires_valides.append(paires)

    paires_set = [set(p) for p in paires_valides]

    with gp.Env(empty=True) as env:
        env.setParam("OutputFlag", 0)
        env.start()
        with gp.Model("m5_insertion_urgence", env=env) as modele:
            modele.Params.TimeLimit = temps_limite

            x: dict[tuple[int, int, int], gp.Var] = {}
            for i, paires in enumerate(paires_valides):
                for s, t in paires:
                    x[i, s, t] = modele.addVar(vtype=GRB.BINARY, name=f"xu_{i}_{s}_{t}")

            fin_journee = modele.addVar(
                lb=float(min_makespan),
                ub=float(nb_creneaux_total),
                vtype=GRB.CONTINUOUS,
                name="fin_journee",
            )
            sup_salle = [
                modele.addVar(
                    lb=float(min_sup_salle[s]),
                    ub=float(nb_creneaux_sup),
                    vtype=GRB.CONTINUOUS,
                    name=f"sup_{s}",
                )
                for s in range(nb_salles)
            ]

            # 1. Chaque opération mobile + l'urgence est planifiée exactement une fois
            for i, paires in enumerate(paires_valides):
                modele.addConstr(gp.quicksum(x[i, s, t] for s, t in paires) == 1, name=f"aff_{i}")

            # 2. Non-chevauchement par salle
            for s in range(nb_salles):
                for u in range(t0_slot, nb_creneaux_total):
                    termes = [
                        x[i, s, t]
                        for i, d_i in enumerate(durees_mobiles)
                        for t in range(max(t0_slot, u - d_i + 1), min(u, nb_creneaux_total - d_i) + 1)
                        if (s, t) in paires_set[i]
                    ]
                    if termes:
                        modele.addConstr(gp.quicksum(termes) <= 1, name=f"salle_{s}_{u}")

            # 3. Non-chevauchement par chirurgien
            ops_par_chir: dict[str, list[int]] = {}
            for i, op in enumerate(ops_mobiles):
                ops_par_chir.setdefault(op["chirurgien"], []).append(i)
            for chir, indices in ops_par_chir.items():
                if len(indices) < 2:
                    continue
                for u in range(t0_slot, nb_creneaux_total):
                    termes = [
                        x[i, s, t]
                        for i in indices
                        for s in range(nb_salles)
                        for t in range(
                            max(t0_slot, u - durees_mobiles[i] + 1),
                            min(u, nb_creneaux_total - durees_mobiles[i]) + 1,
                        )
                        if (s, t) in paires_set[i]
                    ]
                    if termes:
                        modele.addConstr(gp.quicksum(termes) <= 1, name=f"chir_{chir}_{u}")

            # 4. Heures sup par salle
            if nb_creneaux_sup > 0:
                for i, d_i in enumerate(durees_mobiles):
                    for s in range(nb_salles):
                        termes_sup = [
                            (t + d_i - nb_creneaux_normaux) * x[i, s, t]
                            for (s_p, t) in paires_valides[i]
                            if s_p == s and t + d_i > nb_creneaux_normaux
                        ]
                        if termes_sup:
                            modele.addConstr(gp.quicksum(termes_sup) <= sup_salle[s], name=f"sup_{i}_{s}")

            # 5. Makespan global
            for i, d_i in enumerate(durees_mobiles):
                modele.addConstr(
                    gp.quicksum((t + d_i) * x[i, s, t] for s, t in paires_valides[i]) <= fin_journee,
                    name=f"mk_{i}",
                )

            # Objectif : placer l'urgence le plus tôt possible + limiter heures sup + stabilité
            cout_urgence = 20.0 * gp.quicksum(
                (t + durees_mobiles[index_urgence]) * x[index_urgence, s, t]
                for s, t in paires_valides[index_urgence]
            )
            cout_sup = penalite_heures_sup * gp.quicksum(sup_salle)

            # Léger terme de stabilité pour éviter de déplacer inutilement des opérations existantes
            termes_stabilite = []
            for i, op in enumerate(ops_mobiles):
                if op["id"] in avant_par_id:
                    ref = avant_par_id[op["id"]]
                    for s, t in paires_valides[i]:
                        if s != ref["s_idx"] or t != ref["t_slot"]:
                            termes_stabilite.append(0.05 * x[i, s, t])
            cout_stabilite = gp.quicksum(termes_stabilite) if termes_stabilite else 0.0

            if mode_objectif == "priorite":
                terme_mode = gp.quicksum(
                    _poids_priorite(ops_mobiles[i]["priorite"]) * (t + durees_mobiles[i]) * x[i, s, t]
                    for i in range(len(ops_mobiles))
                    if i != index_urgence
                    for s, t in paires_valides[i]
                )
            else:
                terme_mode = fin_journee

            modele.setObjective(cout_urgence + terme_mode + cout_sup + cout_stabilite, GRB.MINIMIZE)
            modele.optimize()
            temps_calc = round(time.perf_counter() - debut_chrono, 4)

            if modele.Status == GRB.OPTIMAL:
                statut = "optimal"
            elif modele.Status == GRB.TIME_LIMIT and modele.SolCount > 0:
                statut = "faisable"
            else:
                return _resultat_infaisable(
                    debut_chrono,
                    f"Aucune ré-optimisation faisable pour l'urgence {urg_id} à {_formater_heure(t0_min)} "
                    f"(augmentez le plafond d'heures supplémentaires ou le nombre de salles).",
                )

            # Reconstruction du planning complet (opérations figées + opérations mobiles + urgence)
            toutes_ops = list(ops_figees) + list(ops_mobiles)
            toutes_durees = list(durees_figees) + list(durees_mobiles)
            decalage_idx = len(ops_figees)
            toutes_aff = list(aff_figees)

            deplacements: list[dict[str, Any]] = []
            nb_inchangees = 0
            infos_urgence: dict[str, Any] = {}

            for i, paires in enumerate(paires_valides):
                for s, t in paires:
                    if x[i, s, t].X > 0.5:
                        op = ops_mobiles[i]
                        deb_apres = debut_min + t * duree_creneau
                        fin_apres = deb_apres + durees_mobiles[i] * duree_creneau
                        salle_apres = f"Salle {s + 1}"

                        if i == index_urgence:
                            infos_urgence = {
                                "salle": salle_apres,
                                "debut": _formater_heure(deb_apres),
                                "fin": _formater_heure(fin_apres),
                                "debut_min": deb_apres,
                            }
                        elif op["id"] in avant_par_id:
                            ref = avant_par_id[op["id"]]
                            if salle_apres != ref["salle"] or deb_apres != ref["debut_min"]:
                                op["deplacee"] = True
                                deplacements.append(
                                    {
                                        "id": op["id"],
                                        "chirurgien": op["chirurgien"],
                                        "avant_salle": ref["salle"],
                                        "avant_debut": ref["debut"],
                                        "avant_fin": ref["fin"],
                                        "apres_salle": salle_apres,
                                        "apres_debut": _formater_heure(deb_apres),
                                        "apres_fin": _formater_heure(fin_apres),
                                        "decalage_min": deb_apres - ref["debut_min"],
                                    }
                                )
                            else:
                                nb_inchangees += 1

                        toutes_aff.append((decalage_idx + i, s, t))

            ind = _calculer_indicateurs(
                affectations=toutes_aff,
                operations=toutes_ops,
                durees_creneaux=toutes_durees,
                nb_salles=nb_salles,
                debut_min=debut_min,
                duree_creneau=duree_creneau,
                nb_creneaux_normaux=nb_creneaux_normaux,
                mode_objectif=mode_objectif,
                penalite_heures_sup=penalite_heures_sup,
            )

            impact_urgence = {
                "urgence_id": urg_id,
                "chirurgien": chir_urg,
                "duree_min": duree_urg,
                "heure_arrivee": _formater_heure(t0_min),
                "heure_arrivee_min": t0_min,
                "salle_urgence": infos_urgence["salle"],
                "horaire_urgence": f"{infos_urgence['debut']} → {infos_urgence['fin']}",
                "attente_urgence_min": max(0, infos_urgence["debut_min"] - t0_min),
                "nb_figees": len(ops_figees),
                "nb_deplacees": len(deplacements),
                "nb_inchangees": nb_inchangees,
                "deplacements": sorted(deplacements, key=lambda d: d["id"]),
                "heures_sup_avant": heures_sup_avant,
                "heures_sup_apres": ind["heures_sup"],
                "delta_heures_sup": round(ind["heures_sup"] - heures_sup_avant, 2),
            }

            return {
                "statut": statut,
                "objectif": ind["objectif"],
                "solution": {
                    "planning": ind["planning"],
                    "taux_occupation": ind["taux_occupation"],
                    "heures_sup": ind["heures_sup"],
                    "heures_sup_par_salle": ind["heures_sup_par_salle"],
                    "fin_journee_heure": ind["fin_journee_heure"],
                    "mode_objectif": mode_objectif,
                    "impact_urgence": impact_urgence,
                    "indisponibilites": {
                        chir: [(_formater_heure(d), _formater_heure(f)) for d, f in plages]
                        for chir, plages in indisponibilites.items()
                    },
                },
                "temps": temps_calc,
            }
