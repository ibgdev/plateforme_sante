"""Routes Flask — Module M5 : Planning des blocs opératoires."""

from __future__ import annotations

import csv
import io
from typing import Any

from flask import Blueprint, Response, render_template, request
import plotly.graph_objects as go

from data.loader import load_data
from modules.m5_blocs import MODES_OBJECTIF, _operations_par_defaut, solve

bp = Blueprint("m5", __name__, url_prefix="/m5")

COULEURS_PRIORITE = {
    1: ("#dc2626", "Priorité 1 (Urgente)"),
    2: ("#d97706", "Priorité 2 (Moyenne)"),
    3: ("#0284c7", "Priorité 3 (Normale)"),
}


def _charger_donnees_initiales() -> dict[str, Any]:
    """Charge l'état initial depuis `data/*.csv` avec repli sécurisé."""
    try:
        donnees = load_data()
    except NotImplementedError:
        donnees = {}

    hopitaux: list[dict[str, Any]] = []
    if "hopitaux" in donnees and not donnees["hopitaux"].empty:
        hopitaux = donnees["hopitaux"].to_dict(orient="records")

    nb_salles_defaut = int(hopitaux[0]["nb_salles_operation"]) if hopitaux else 4

    if "operations" in donnees and not donnees["operations"].empty:
        operations = [
            {
                "id": str(ligne["id"]),
                "duree": int(ligne["duree"]),
                "chirurgien": str(ligne["chirurgien"]),
                "priorite": int(ligne["priorite"]),
            }
            for ligne in donnees["operations"].to_dict(orient="records")
        ]
    else:
        operations = _operations_par_defaut()

    indisponibilites: list[dict[str, str]] = []
    if "personnel" in donnees and not donnees["personnel"].empty:
        for ligne in donnees["personnel"].to_dict(orient="records"):
            role = str(ligne.get("role", "")).strip().lower()
            if role and role != "chirurgien":
                continue
            brut = str(ligne.get("indisponibilites", "")).strip()
            if not brut:
                continue
            for morceau in brut.replace(",", ";").split(";"):
                morceau = morceau.strip()
                if "-" not in morceau:
                    continue
                debut_txt, fin_txt = morceau.split("-", 1)
                indisponibilites.append(
                    {
                        "chirurgien": str(ligne["id"]).strip(),
                        "debut": debut_txt.strip(),
                        "fin": fin_txt.strip(),
                    }
                )

    return {
        "parametres": {
            "nb_salles": nb_salles_defaut,
            "heure_debut": 8,
            "heure_fin": 16,
            "duree_creneau": 30,
            "max_heures_sup": 2.0,
            "mode_objectif": "makespan",
        },
        "hopitaux": hopitaux,
        "operations": operations,
        "indisponibilites": indisponibilites,
    }


def _lire_operations_formulaire(formulaire) -> list[dict[str, Any]]:
    """Lit les lignes d'opérations saisies dans le formulaire HTML."""
    ids = formulaire.getlist("op_id[]")
    durees = formulaire.getlist("op_duree[]")
    chirurgiens = formulaire.getlist("op_chirurgien[]")
    priorites = formulaire.getlist("op_priorite[]")

    if not ids:
        return []
    if not (len(ids) == len(durees) == len(chirurgiens) == len(priorites)):
        raise ValueError("Le tableau des opérations est incomplet.")

    operations: list[dict[str, Any]] = []
    ids_vus: set[str] = set()
    for index in range(len(ids)):
        op_id = ids[index].strip()
        chir = chirurgiens[index].strip()
        if not op_id and not chir and not durees[index].strip():
            continue
        if not op_id or not chir:
            raise ValueError(f"Ligne opération #{index + 1} : l'identifiant et le chirurgien sont requis.")
        if op_id in ids_vus:
            raise ValueError(f"Identifiant d'opération en doublon : {op_id}.")
        ids_vus.add(op_id)

        try:
            duree = int(durees[index])
            priorite = int(priorites[index])
        except ValueError as exc:
            raise ValueError(f"Opération {op_id} : durée et priorité doivent être des entiers.") from exc

        if duree <= 0:
            raise ValueError(f"Opération {op_id} : la durée doit être strictement positive.")
        if priorite not in (1, 2, 3):
            raise ValueError(f"Opération {op_id} : la priorité doit être comprise entre 1 et 3.")

        operations.append(
            {
                "id": op_id,
                "duree": duree,
                "chirurgien": chir,
                "priorite": priorite,
            }
        )

    if not operations:
        raise ValueError("Ajoutez au moins une opération avant de lancer la résolution.")
    return operations


def _lire_indisponibilites_formulaire(formulaire) -> list[dict[str, str]]:
    """Lit les plages d'indisponibilité chirurgiens saisies dans le formulaire."""
    chirurgiens = formulaire.getlist("indisp_chirurgien[]")
    debuts = formulaire.getlist("indisp_debut[]")
    fins = formulaire.getlist("indisp_fin[]")

    if not (len(chirurgiens) == len(debuts) == len(fins)):
        raise ValueError("Le tableau des indisponibilités est incomplet.")

    indispos: list[dict[str, str]] = []
    for index in range(len(chirurgiens)):
        chir = chirurgiens[index].strip()
        deb = debuts[index].strip()
        fin = fins[index].strip()
        if not chir and not deb and not fin:
            continue
        if not chir or not deb or not fin:
            raise ValueError(
                f"Indisponibilité #{index + 1} : renseignez le chirurgien, l'heure de début et l'heure de fin."
            )
        indispos.append({"chirurgien": chir, "debut": deb, "fin": fin})
    return indispos


def _parser_csv_operations(fichier) -> list[dict[str, Any]]:
    """Parse un fichier CSV téléversé contenant `id,duree,chirurgien,priorite`."""
    contenu = fichier.read().decode("utf-8-sig").strip()
    if not contenu:
        raise ValueError("Le fichier CSV importé est vide.")

    premiere_ligne = contenu.splitlines()[0]
    delimiteur = ";" if premiere_ligne.count(";") > premiere_ligne.count(",") else ","
    lecteur = csv.DictReader(io.StringIO(contenu), delimiter=delimiteur)

    colonnes_attendues = {"id", "duree", "chirurgien", "priorite"}
    colonnes_presentes = {col.strip().lower() for col in (lecteur.fieldnames or []) if col}
    if not colonnes_attendues.issubset(colonnes_presentes):
        manquantes = ", ".join(sorted(colonnes_attendues - colonnes_presentes))
        raise ValueError(f"Colonnes CSV manquantes : {manquantes}. Attendu : id,duree,chirurgien,priorite.")

    operations: list[dict[str, Any]] = []
    ids_vus: set[str] = set()
    for numero, ligne_brute in enumerate(lecteur, start=2):
        ligne = {k.strip().lower(): (v or "").strip() for k, v in ligne_brute.items() if k}
        if not any(ligne.values()):
            continue
        op_id = ligne["id"]
        chir = ligne["chirurgien"]
        if not op_id or not chir:
            raise ValueError(f"CSV ligne {numero} : 'id' et 'chirurgien' sont obligatoires.")
        if op_id in ids_vus:
            raise ValueError(f"CSV ligne {numero} : identifiant d'opération en doublon ({op_id}).")
        ids_vus.add(op_id)
        try:
            duree = int(ligne["duree"])
            priorite = int(ligne["priorite"])
        except ValueError as exc:
            raise ValueError(f"CSV ligne {numero} : 'duree' et 'priorite' doivent être des entiers.") from exc
        if duree <= 0 or priorite not in (1, 2, 3):
            raise ValueError(f"CSV ligne {numero} : durée > 0 et priorité dans {{1, 2, 3}} requises.")
        operations.append(
            {
                "id": op_id,
                "duree": duree,
                "chirurgien": chir,
                "priorite": priorite,
            }
        )

    if not operations:
        raise ValueError("Aucune opération valide n'a été trouvée dans le fichier CSV.")
    return operations


def _construire_gantt_plotly(
    planning: list[dict[str, Any]],
    parametres: dict[str, Any],
) -> str:
    """Construit le diagramme de Gantt interactif par salle en HTML Plotly."""
    nb_salles = int(parametres["nb_salles"])
    heure_debut = int(parametres["heure_debut"])
    heure_fin = int(parametres["heure_fin"])
    max_heures_sup = float(parametres["max_heures_sup"])

    salles_ordonnees = [f"Salle {s + 1}" for s in range(nb_salles)]
    fig = go.Figure()

    # Une trace par niveau de priorité pour une légende lisible et interactive
    for niveau in (1, 2, 3):
        couleur, libelle = COULEURS_PRIORITE[niveau]
        ops_niveau = [op for op in planning if int(op["priorite"]) == niveau]
        if not ops_niveau:
            continue

        fig.add_trace(
            go.Bar(
                name=libelle,
                orientation="h",
                y=[op["salle"] for op in ops_niveau],
                x=[(op["fin_min"] - op["debut_min"]) / 60.0 for op in ops_niveau],
                base=[op["debut_min"] / 60.0 for op in ops_niveau],
                text=[f"{op['id']} ({op['chirurgien']})" for op in ops_niveau],
                textposition="inside",
                insidetextanchor="middle",
                marker={
                    "color": couleur,
                    "line": {"color": "#ffffff", "width": 1.5},
                },
                hovertemplate=(
                    "<b>%{customdata[0]}</b> — %{y}<br>"
                    "Chirurgien : %{customdata[1]}<br>"
                    "Horaire : %{customdata[2]} → %{customdata[3]} (%{customdata[4]} min)<br>"
                    "Priorité : %{customdata[5]}<br>"
                    "Heures sup : %{customdata[6]}<extra></extra>"
                ),
                customdata=[
                    [
                        op["id"],
                        op["chirurgien"],
                        op["debut"],
                        op["fin"],
                        op["duree_min"],
                        op["priorite"],
                        "Oui" if op.get("en_heures_sup") else "Non",
                    ]
                    for op in ops_niveau
                ],
            )
        )

    fin_reelle_h = max([heure_fin] + [op["fin_min"] / 60.0 for op in planning])
    fin_axe_h = max(heure_fin + (0.5 if max_heures_sup > 0 else 0.0), fin_reelle_h + 0.5)

    # Zone visuelle des heures supplémentaires et ligne de fermeture officielle
    if fin_axe_h > heure_fin:
        fig.add_vrect(
            x0=heure_fin,
            x1=fin_axe_h,
            fillcolor="#fee2e2",
            opacity=0.35,
            layer="below",
            line_width=0,
        )
    fig.add_vline(
        x=heure_fin,
        line_width=2,
        line_dash="dash",
        line_color="#b91c1c",
        annotation_text=f"Fermeture {heure_fin:02d}:00",
        annotation_position="top right",
    )

    graduations = list(range(heure_debut, int(fin_axe_h) + 2))
    fig.update_layout(
        title="Diagramme de Gantt des blocs opératoires (par salle)",
        barmode="overlay",
        height=max(320, 95 * nb_salles + 140),
        margin={"l": 80, "r": 30, "t": 60, "b": 50},
        xaxis={
            "title": "Horaire de la journée",
            "range": [heure_debut, fin_axe_h],
            "tickvals": graduations,
            "ticktext": [f"{h:02d}:00" for h in graduations],
            "showgrid": True,
            "gridcolor": "#e2e8f0",
        },
        yaxis={
            "title": "Salles d'opération",
            "categoryorder": "array",
            "categoryarray": salles_ordonnees,
            "autorange": "reversed",
        },
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "left", "x": 0},
        plot_bgcolor="#ffffff",
    )

    return fig.to_html(
        full_html=False,
        include_plotlyjs=True,
        config={"displayModeBar": True, "responsive": True},
    )


@bp.route("/exemple-csv", methods=["GET"])
def telecharger_exemple_csv() -> Response:
    """Fournit un fichier `operations_exemple.csv` prêt à être modifié puis réimporté."""
    etat = _charger_donnees_initiales()
    tampon = io.StringIO()
    ecrivain = csv.DictWriter(tampon, fieldnames=["id", "duree", "chirurgien", "priorite"])
    ecrivain.writeheader()
    for op in etat["operations"]:
        ecrivain.writerow(op)

    return Response(
        tampon.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="operations_exemple.csv"'},
    )


@bp.route("/", methods=["GET", "POST"])
def page_m5():
    """Affiche la page du module M5 et traite la résolution via `modules.m5_blocs.solve`."""
    etat = _charger_donnees_initiales()
    parametres = etat["parametres"]
    hopitaux = etat["hopitaux"]
    operations = etat["operations"]
    indisponibilites = etat["indisponibilites"]

    resultat = None
    graph_html = None
    erreur = None
    info = None

    if request.method == "POST":
        action = request.form.get("action", "resoudre").strip()

        if action == "reinitialiser":
            info = "Données réinitialisées à partir des fichiers communs (data/*.csv)."
            return render_template(
                "m5_blocs.html",
                parametres=parametres,
                hopitaux=hopitaux,
                operations=operations,
                indisponibilites=indisponibilites,
                resultat=None,
                graph_html=None,
                erreur=None,
                info=info,
            )

        if action == "scenario_sature":
            parametres.update(
                {
                    "nb_salles": 2,
                    "heure_debut": 8,
                    "heure_fin": 16,
                    "duree_creneau": 30,
                    "max_heures_sup": 3.0,
                    "mode_objectif": "makespan",
                }
            )
            info = "Scénario saturé chargé (2 salles pour 12 opérations, jusqu'à 3 h d'heures sup)."
        else:
            try:
                parametres["nb_salles"] = int(request.form.get("nb_salles", parametres["nb_salles"]))
                parametres["heure_debut"] = int(request.form.get("heure_debut", parametres["heure_debut"]))
                parametres["heure_fin"] = int(request.form.get("heure_fin", parametres["heure_fin"]))
                parametres["duree_creneau"] = int(
                    request.form.get("duree_creneau", parametres["duree_creneau"])
                )
                parametres["max_heures_sup"] = float(
                    request.form.get("max_heures_sup", parametres["max_heures_sup"])
                )
                mode_choisi = request.form.get("mode_objectif", parametres["mode_objectif"]).strip().lower()
                if mode_choisi not in MODES_OBJECTIF:
                    raise ValueError(f"Mode d'objectif invalide : {mode_choisi}.")
                parametres["mode_objectif"] = mode_choisi

                # Lecture des opérations : CSV téléversé en priorité si présent, sinon tableau HTML
                fichier_csv = request.files.get("fichier_csv")
                if fichier_csv and fichier_csv.filename:
                    operations = _parser_csv_operations(fichier_csv)
                    info = f"{len(operations)} opérations importées depuis « {fichier_csv.filename} »."
                elif request.form.getlist("op_id[]"):
                    operations = _lire_operations_formulaire(request.form)

                if request.form.get("indisponibilites_presentes") == "1":
                    indisponibilites = _lire_indisponibilites_formulaire(request.form)
            except ValueError as exc:
                erreur = str(exc)

        if erreur is None:
            params_solve = {
                "nb_salles": parametres["nb_salles"],
                "horaires_ouverture": (parametres["heure_debut"], parametres["heure_fin"]),
                "duree_creneau": parametres["duree_creneau"],
                "max_heures_sup": parametres["max_heures_sup"],
                "mode_objectif": parametres["mode_objectif"],
                "indisponibilites": indisponibilites,
                "comparer": True,
            }
            resultat = solve(data={"operations": operations}, params=params_solve)
            if resultat["solution"].get("planning"):
                graph_html = _construire_gantt_plotly(resultat["solution"]["planning"], parametres)

    return render_template(
        "m5_blocs.html",
        parametres=parametres,
        hopitaux=hopitaux,
        operations=operations,
        indisponibilites=indisponibilites,
        resultat=resultat,
        graph_html=graph_html,
        erreur=erreur,
        info=info,
    )
