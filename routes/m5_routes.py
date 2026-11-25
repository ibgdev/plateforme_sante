"""Routes Flask — Module M5 : Planning des blocs opératoires."""

from flask import Blueprint, render_template, request

from modules.m5_blocs import solve

bp = Blueprint("m5", __name__, url_prefix="/m5")

PARAMETRES_PAR_DEFAUT = {
    "nb_salles": 4,
    "heure_debut": 8,
    "heure_fin": 16,
    "duree_creneau": 30,
}


@bp.route("/", methods=["GET", "POST"])
def page_m5():
    """Affiche la page du module M5 et traite la résolution via `modules.m5_blocs.solve`."""
    parametres = dict(PARAMETRES_PAR_DEFAUT)
    resultat = None
    erreur = None

    if request.method == "POST":
        try:
            parametres["nb_salles"] = int(request.form.get("nb_salles", PARAMETRES_PAR_DEFAUT["nb_salles"]))
            parametres["heure_debut"] = int(request.form.get("heure_debut", PARAMETRES_PAR_DEFAUT["heure_debut"]))
            parametres["heure_fin"] = int(request.form.get("heure_fin", PARAMETRES_PAR_DEFAUT["heure_fin"]))
            parametres["duree_creneau"] = int(
                request.form.get("duree_creneau", PARAMETRES_PAR_DEFAUT["duree_creneau"])
            )

            params_solve = {
                "nb_salles": parametres["nb_salles"],
                "horaires_ouverture": (parametres["heure_debut"], parametres["heure_fin"]),
                "duree_creneau": parametres["duree_creneau"],
            }
            resultat = solve(data={}, params=params_solve)
        except ValueError:
            erreur = "Veuillez saisir des valeurs entières valides pour tous les paramètres."

    return render_template(
        "m5_blocs.html",
        parametres=parametres,
        resultat=resultat,
        erreur=erreur,
    )
