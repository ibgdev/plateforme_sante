"""Routes Flask du module M6 : planning du personnel."""

import json

from flask import Blueprint, render_template, request

from modules.m6_personnel import COUTS_PAR_DEFAUT, HEURES_PAR_DEFAUT, SERVICES, solve

bp = Blueprint("m6", __name__, url_prefix="/m6")

LIBELLES_SERVICES = {"matin": "Matin", "soir": "Soir", "nuit": "Nuit"}


def _valeur_nombre(formulaire, cle, defaut, entier=False):
    """Convertit un champ numérique et fournit un message exploitable à l'écran."""
    valeur = formulaire.get(cle, str(defaut))
    try:
        nombre = int(valeur) if entier else float(valeur)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"La valeur « {cle} » doit être numérique.") from exc
    return nombre


def _personnel_formulaire(formulaire):
    identifiants = formulaire.getlist("infirmier_id[]")
    contrats = formulaire.getlist("contrat[]")
    max_jours = formulaire.getlist("max_jours[]")
    max_heures = formulaire.getlist("max_heures[]")
    indisponibilites = formulaire.getlist("indisponibilites[]")
    nombre = len(identifiants)
    if not nombre or any(len(champ) != nombre for champ in (contrats, max_jours, max_heures, indisponibilites)):
        raise ValueError("La liste des infirmiers est vide ou incomplète.")

    personnel = []
    for index in range(nombre):
        identifiant = identifiants[index].strip()
        if not identifiant:
            raise ValueError("Chaque infirmier doit avoir un identifiant.")
        jours_indisponibles = []
        texte = indisponibilites[index].strip()
        if texte:
            try:
                jours_indisponibles = [int(jour.strip()) for jour in texte.split(",") if jour.strip()]
            except ValueError as exc:
                raise ValueError(f"Les indisponibilités de {identifiant} doivent être des jours (indices 0 à 27) séparés par des virgules.") from exc
        personne = {
            "id": identifiant,
            "contrat": contrats[index].strip() or "standard",
            "max_jours_semaine": int(max_jours[index]),
            "max_heures_semaine": float(max_heures[index]),
            "indisponibilites": jours_indisponibles,
        }
        personnel.append(personne)
    if len({personne["id"] for personne in personnel}) != len(personnel):
        raise ValueError("Les identifiants des infirmiers doivent être uniques.")
    return personnel


def _etat_initial():
    """Valeurs de démonstration affichées lors de la première ouverture."""
    return {
        "nb_jours": 7,
        "couts_services": dict(COUTS_PAR_DEFAUT),
        "heures_services": dict(HEURES_PAR_DEFAUT),
        "besoins_min": {service: [2 if service != "nuit" else 1] * 28 for service in SERVICES},
        "infirmiers": [
            {"id": f"I{i + 1}", "contrat": "standard", "max_jours_semaine": 5,
             "max_heures_semaine": 40, "indisponibilites": []}
            for i in range(12)
        ],
        "penalite_preferences": 10,
        "preferences_json": "",
        "limite_temps": 30,
    }


@bp.route("/", methods=["GET", "POST"])
def page_m6():
    """Affiche le formulaire et transmet les paramètres au solveur M6."""
    etat = _etat_initial()
    resultat = None
    erreur = None

    if request.method == "POST":
        try:
            nb_jours = _valeur_nombre(request.form, "nb_jours", 7, entier=True)
            if nb_jours not in (7, 28):
                raise ValueError("La période doit être de 7 ou 28 jours.")
            personnel = _personnel_formulaire(request.form)
            besoins = {
                service: [
                    _valeur_nombre(request.form, f"besoin_{service}_{jour}", 0, entier=True)
                    for jour in range(nb_jours)
                ]
                for service in SERVICES
            }
            couts = {service: _valeur_nombre(request.form, f"cout_{service}", COUTS_PAR_DEFAUT[service]) for service in SERVICES}
            heures = {service: _valeur_nombre(request.form, f"heures_{service}", HEURES_PAR_DEFAUT[service]) for service in SERVICES}
            penalite = _valeur_nombre(request.form, "penalite_preferences", 10)
            limite_temps = _valeur_nombre(request.form, "limite_temps", 30)
            preferences_brutes = request.form.get("preferences_json", "").strip()
            try:
                preferences = json.loads(preferences_brutes) if preferences_brutes else []
            except json.JSONDecodeError as exc:
                raise ValueError("Les préférences doivent être au format JSON valide.") from exc

            etat.update({
                "nb_jours": nb_jours,
                "couts_services": couts,
                "heures_services": heures,
                "besoins_min": besoins,
                "infirmiers": personnel,
                "penalite_preferences": penalite,
                "preferences_json": preferences_brutes,
                "limite_temps": limite_temps,
            })
            params = {
                "nb_jours": nb_jours,
                "nb_infirmiers": len(personnel),
                "infirmiers": personnel,
                "besoins_min": besoins,
                "couts_services": couts,
                "heures_services": heures,
                "penalite_preferences": penalite,
                "preferences": preferences,
                "limite_temps": limite_temps,
                "max_jours_semaine": 5,
                "max_heures_semaine": 40,
            }
            resultat = solve({}, params)
        except (ValueError, RuntimeError) as exc:
            erreur = str(exc)
            # Preserve submitted values when validation or solving fails.
            try:
                etat["nb_jours"] = int(request.form.get("nb_jours", 7))
            except ValueError:
                pass
            etat["preferences_json"] = request.form.get("preferences_json", "")
            etat["infirmiers"] = []
            for identifiant, contrat, jours, heures, absences in zip(
                request.form.getlist("infirmier_id[]"), request.form.getlist("contrat[]"),
                request.form.getlist("max_jours[]"), request.form.getlist("max_heures[]"),
                request.form.getlist("indisponibilites[]"),
            ):
                etat["infirmiers"].append({"id": identifiant, "contrat": contrat,
                                           "max_jours_semaine": jours, "max_heures_semaine": heures,
                                           "indisponibilites_texte": absences})
            for service in SERVICES:
                try:
                    etat["couts_services"][service] = float(request.form.get(f"cout_{service}", COUTS_PAR_DEFAUT[service]))
                    etat["heures_services"][service] = float(request.form.get(f"heures_{service}", HEURES_PAR_DEFAUT[service]))
                except ValueError:
                    pass
                etat["besoins_min"][service] = [
                    request.form.get(f"besoin_{service}_{jour}", "0") for jour in range(28)
                ]

    return render_template(
        "m6_personnel.html", etat=etat, resultat=resultat, erreur=erreur,
        services=SERVICES, libelles_services=LIBELLES_SERVICES,
    )
