"""Solveur M6 : planning du personnel avec OR-Tools CP-SAT.

Le modèle affecte chaque infirmier à zéro ou un service par jour. Il couvre
les besoins quotidiens, les repos après nuit, les jours de repos et les limites
de contrat. Les préférences sont facultatives et interviennent comme pénalités.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import isfinite
from time import perf_counter
from typing import Any


SERVICES = ("matin", "soir", "nuit")
COUTS_PAR_DEFAUT = {"matin": 1.0, "soir": 1.15, "nuit": 1.5}
HEURES_PAR_DEFAUT = {service: 8 for service in SERVICES}


def _enregistrements_personnel(data: dict, params: dict) -> list[dict]:
    """Récupère les infirmiers fournis ou crée un effectif à partir de N."""
    personnel = params.get("infirmiers", data.get("personnel"))
    if personnel is None:
        nombre = params.get("nb_infirmiers", 0)
        if not isinstance(nombre, int) or isinstance(nombre, bool) or nombre < 1:
            raise ValueError("nb_infirmiers doit être un entier positif.")
        return [{"id": f"I{i + 1}"} for i in range(nombre)]

    # pandas.DataFrame expose to_dict sans devenir une dépendance du module.
    if hasattr(personnel, "to_dict") and not isinstance(personnel, Mapping):
        personnel = personnel.to_dict(orient="records")
    if not isinstance(personnel, Sequence) or isinstance(personnel, (str, bytes)):
        raise ValueError("infirmiers/personnel doit être une liste d'enregistrements.")
    result = []
    for index, personne in enumerate(personnel):
        if not isinstance(personne, Mapping):
            raise ValueError("Chaque infirmier doit être décrit par un dictionnaire.")
        enregistrement = dict(personne)
        enregistrement.setdefault("id", f"I{index + 1}")
        result.append(enregistrement)
    if not result:
        raise ValueError("L'effectif doit contenir au moins un infirmier.")
    if "nb_infirmiers" in params and params["nb_infirmiers"] != len(result):
        raise ValueError("nb_infirmiers doit correspondre à la liste des infirmiers.")
    return result


def _besoin_journalier(besoins: dict, service: str, jour: int, nb_jours: int) -> int:
    """Accepte un besoin constant, une liste indexée ou un dictionnaire par jour."""
    valeur = besoins.get(service, 0)
    if isinstance(valeur, Mapping):
        valeur = valeur.get(jour, valeur.get(str(jour), 0))
    elif isinstance(valeur, Sequence) and not isinstance(valeur, (str, bytes)):
        if len(valeur) != nb_jours:
            raise ValueError(f"Le besoin '{service}' doit avoir {nb_jours} valeurs.")
        valeur = valeur[jour]
    if not isinstance(valeur, (int, float)) or isinstance(valeur, bool) or valeur < 0 or int(valeur) != valeur:
        raise ValueError(f"Le besoin '{service}' doit être un entier positif ou nul.")
    return int(valeur)


def _indisponibilites(personne: Mapping, nb_jours: int) -> set[tuple[int, str | None]]:
    """Normalise les jours indisponibles en indices 0..nb_jours-1.

    Formats acceptés : [0, 3] ou [{"jour": 0, "service": "nuit"}].
    Les indices de jour commencent à zéro.
    """
    resultat: set[tuple[int, str | None]] = set()
    for entree in personne.get("indisponibilites", []) or []:
        if isinstance(entree, Mapping):
            jour = entree.get("jour")
            service = entree.get("service")
        else:
            jour, service = entree, None
        if not isinstance(jour, int) or isinstance(jour, bool):
            raise ValueError("Chaque indisponibilité doit contenir un jour entier.")
        if jour < 0 or jour >= nb_jours:
            raise ValueError(f"Jour d'indisponibilité hors période : {jour}.")
        if service is not None and service not in SERVICES:
            raise ValueError(f"Service d'indisponibilité inconnu : {service}.")
        resultat.add((jour, service))
    return resultat


def _preferences(params: dict, identifiants: set[Any], nb_jours: int, penalite_defaut: float) -> list[dict]:
    preferences = params.get("preferences", []) or []
    if not isinstance(preferences, Sequence) or isinstance(preferences, (str, bytes)):
        raise ValueError("preferences doit être une liste de préférences.")
    normalisees = []
    for preference in preferences:
        if not isinstance(preference, Mapping):
            raise ValueError("Chaque préférence doit être un dictionnaire.")
        infirmier = preference.get("infirmier")
        jour = preference.get("jour")
        service = preference.get("service")
        if infirmier not in identifiants or not isinstance(jour, int) or isinstance(jour, bool):
            raise ValueError("Une préférence doit référencer un infirmier et un jour valides.")
        if jour < 0 or jour >= nb_jours:
            raise ValueError("Les jours des préférences sont des indices de 0 à nb_jours - 1.")
        if service not in SERVICES:
            raise ValueError(f"Service préféré inconnu : {service}.")
        souhaite = preference.get("souhaite", True)
        if not isinstance(souhaite, bool):
            raise ValueError("Le champ souhaite d'une préférence doit être booléen.")
        penalite = preference.get("penalite", penalite_defaut)
        if not isinstance(penalite, (int, float)) or isinstance(penalite, bool) or not isfinite(penalite) or penalite < 0:
            raise ValueError("La pénalité d'une préférence doit être un nombre positif ou nul.")
        normalisees.append({
            "infirmier": infirmier,
            "jour": jour,
            "service": service,
            "souhaite": souhaite,
            "penalite": float(penalite),
        })
    return normalisees


def solve(data: dict, params: dict) -> dict:
    """Construit un planning sur 7 ou 28 jours.

    Paramètres principaux :
      - ``nb_infirmiers`` et ``nb_jours`` (7 ou 28) ;
      - ``besoins_min`` : service -> entier, liste journalière, ou mapping jour -> entier ;
      - ``couts_services`` : coûts positifs/non négatifs, par défaut matin=1,
        soir=1.15 et nuit=1.5 ;
      - ``penalite_preferences`` et ``preferences`` (optionnelles) ;
      - ``heures_services`` (8 h par défaut) et ``max_heures_semaine`` (40 h) ;
      - ``max_jours_semaine`` (5 par défaut), éventuellement précisé par infirmier
        avec ``max_jours_semaine`` ou dans ``contrats``.

    Les jours des préférences et indisponibilités commencent à zéro. Les
    indisponibilités peuvent cibler une journée entière ou un service précis.
    """
    if not isinstance(data, dict) or not isinstance(params, dict):
        raise ValueError("data et params doivent être des dictionnaires.")
    nb_jours = params.get("nb_jours", 7)
    if not isinstance(nb_jours, int) or isinstance(nb_jours, bool) or nb_jours not in (7, 28):
        raise ValueError("nb_jours doit être égal à 7 ou 28.")
    personnel = _enregistrements_personnel(data, params)
    identifiants = [personne["id"] for personne in personnel]
    if len(set(identifiants)) != len(identifiants):
        raise ValueError("Les identifiants des infirmiers doivent être uniques.")

    besoins = params.get("besoins_min", {}) or {}
    if not isinstance(besoins, Mapping):
        raise ValueError("besoins_min doit être un dictionnaire.")
    demandes = {(d, s): _besoin_journalier(besoins, s, d, nb_jours) for d in range(nb_jours) for s in SERVICES}

    couts_param = params.get("couts_services", {}) or {}
    heures_param = params.get("heures_services", {}) or {}
    if not isinstance(couts_param, Mapping) or not isinstance(heures_param, Mapping):
        raise ValueError("couts_services et heures_services doivent être des dictionnaires.")
    couts = dict(COUTS_PAR_DEFAUT)
    couts.update(couts_param)
    heures = dict(HEURES_PAR_DEFAUT)
    heures.update(heures_param)
    for service in SERVICES:
        for nom, valeurs in (("coût", couts), ("durée", heures)):
            valeur = valeurs[service]
            if not isinstance(valeur, (int, float)) or isinstance(valeur, bool) or not isfinite(valeur) or valeur < 0:
                raise ValueError(f"La {nom} du service {service} doit être positive ou nulle.")
    penalite_defaut = params.get("penalite_preferences", 10.0)
    if not isinstance(penalite_defaut, (int, float)) or isinstance(penalite_defaut, bool) or not isfinite(penalite_defaut) or penalite_defaut < 0:
        raise ValueError("penalite_preferences doit être positive ou nulle.")
    preferences = _preferences(params, set(identifiants), nb_jours, float(penalite_defaut))

    contrats = params.get("contrats", {}) or {}
    if not isinstance(contrats, Mapping):
        raise ValueError("contrats doit associer un nom de contrat à ses limites.")
    max_jours_defaut = params.get("max_jours_semaine", 5)
    max_heures_defaut = params.get("max_heures_semaine", 40)
    if not isinstance(max_jours_defaut, int) or isinstance(max_jours_defaut, bool) or not 0 <= max_jours_defaut <= 5:
        raise ValueError("max_jours_semaine doit être compris entre 0 et 5 (au moins deux repos par semaine).")
    if not isinstance(max_heures_defaut, (int, float)) or isinstance(max_heures_defaut, bool) or not isfinite(max_heures_defaut) or max_heures_defaut < 0:
        raise ValueError("max_heures_semaine doit être positif ou nul.")

    try:
        from ortools.sat.python import cp_model
    except ImportError as exc:
        raise RuntimeError("OR-Tools est requis pour résoudre M6. Installez les dépendances de requirements.txt.") from exc

    modele = cp_model.CpModel()
    affectation = {
        (n, d, s): modele.NewBoolVar(f"x_{n}_{d}_{s}")
        for n in range(len(personnel)) for d in range(nb_jours) for s in SERVICES
    }

    # Au plus une affectation par personne et par jour.
    for n in range(len(personnel)):
        for d in range(nb_jours):
            modele.Add(sum(affectation[n, d, s] for s in SERVICES) <= 1)

    # Couverture minimale demandée pour chaque service chaque jour.
    for d in range(nb_jours):
        for s in SERVICES:
            modele.Add(sum(affectation[n, d, s] for n in range(len(personnel))) >= demandes[d, s])

    # Après une nuit, aucune affectation n'est possible le jour suivant.
    for n in range(len(personnel)):
        for d in range(nb_jours - 1):
            modele.Add(affectation[n, d, "nuit"] + sum(affectation[n, d + 1, s] for s in SERVICES) <= 1)

    # Limites de contrat et deux jours de repos minimum par semaine.
    for n, personne in enumerate(personnel):
        limites = dict(contrats.get(personne.get("contrat"), {}) or {})
        limites.update({k: v for k, v in personne.items() if k in ("max_jours_semaine", "max_heures_semaine")})
        max_jours = limites.get("max_jours_semaine", max_jours_defaut)
        max_heures = limites.get("max_heures_semaine", max_heures_defaut)
        if not isinstance(max_jours, int) or isinstance(max_jours, bool) or not 0 <= max_jours <= 5:
            raise ValueError(f"Limite max_jours_semaine invalide pour {personne['id']}.")
        if not isinstance(max_heures, (int, float)) or isinstance(max_heures, bool) or not isfinite(max_heures) or max_heures < 0:
            raise ValueError(f"Limite max_heures_semaine invalide pour {personne['id']}.")
        for debut in range(0, nb_jours, 7):
            jours = range(debut, min(debut + 7, nb_jours))
            travaille = [sum(affectation[n, d, s] for s in SERVICES) for d in jours]
            modele.Add(sum(travaille) <= max_jours)
            modele.Add(sum(round(float(heures[s]) * 100) * affectation[n, d, s]
                           for d in jours for s in SERVICES) <= round(float(max_heures) * 100))

        # Indisponibilités issues du formulaire ou de personnel.csv.
        for d, service in _indisponibilites(personne, nb_jours):
            if service is None:
                modele.Add(sum(affectation[n, d, s] for s in SERVICES) == 0)
            else:
                modele.Add(affectation[n, d, service] == 0)

    # Coûts entiers (centièmes) pour représenter les coefficients décimaux CP-SAT.
    echelle = 100
    objectif = [round(float(couts[s]) * echelle) * affectation[n, d, s]
                for n in range(len(personnel)) for d in range(nb_jours) for s in SERVICES]
    for preference in preferences:
        n = identifiants.index(preference["infirmier"])
        variable = affectation[n, preference["jour"], preference["service"]]
        penalite = round(preference["penalite"] * echelle)
        objectif.append(penalite * (1 - variable if preference["souhaite"] else variable))
    modele.Minimize(sum(objectif))

    solveur = cp_model.CpSolver()
    limite_temps = params.get("limite_temps", 30.0)
    if not isinstance(limite_temps, (int, float)) or isinstance(limite_temps, bool) or not isfinite(limite_temps) or limite_temps <= 0:
        raise ValueError("limite_temps doit être un nombre strictement positif.")
    nb_workers = params.get("nb_workers", 8)
    if not isinstance(nb_workers, int) or isinstance(nb_workers, bool) or nb_workers < 1:
        raise ValueError("nb_workers doit être un entier strictement positif.")
    solveur.parameters.max_time_in_seconds = float(limite_temps)
    solveur.parameters.num_search_workers = nb_workers
    debut = perf_counter()
    statut_modele = solveur.Solve(modele)
    temps = perf_counter() - debut

    if statut_modele == cp_model.INFEASIBLE:
        return {"statut": "infaisable", "objectif": 0.0, "solution": {}, "temps": temps}
    if statut_modele not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        # Le contrat commun ne distingue pas « inconnu » de « infaisable ».
        # Une exception évite d'annoncer à tort une preuve d'infaisabilité.
        raise RuntimeError("Le solveur n'a trouvé aucune solution dans le délai imparti; l'infaisabilité n'est pas prouvée.")

    planning = []
    heures_par_infirmier = {}
    nuits_par_infirmier = {}
    jours_travailles = {}
    for n, personne in enumerate(personnel):
        jours_planning = []
        heures_total = 0.0
        nuits_total = 0
        travailles_total = 0
        for d in range(nb_jours):
            service_jour = next((s for s in SERVICES if solveur.Value(affectation[n, d, s])), None)
            jours_planning.append(service_jour)
            if service_jour is not None:
                heures_total += heures[service_jour]
                nuits_total += service_jour == "nuit"
                travailles_total += 1
        planning.append({"infirmier": personne["id"], "jours": jours_planning})
        heures_par_infirmier[personne["id"]] = heures_total
        nuits_par_infirmier[personne["id"]] = nuits_total
        jours_travailles[personne["id"]] = travailles_total

    charges = list(heures_par_infirmier.values())
    moyenne = sum(charges) / len(charges)
    ecart = max(charges) - min(charges)
    solution = {
        "planning": planning,
        "jours": list(range(nb_jours)),
        "services": list(SERVICES),
        "heures_par_infirmier": heures_par_infirmier,
        "nuits_par_infirmier": nuits_par_infirmier,
        "jours_travailles_par_infirmier": jours_travailles,
        "equite": {"moyenne_heures": moyenne, "ecart_max_min_heures": ecart},
    }
    statut = "optimal" if statut_modele == cp_model.OPTIMAL else "faisable"
    return {"statut": statut, "objectif": solveur.ObjectiveValue() / echelle,
            "solution": solution, "temps": temps}
