"""Routes Flask — Module M5 : Planning des blocs opératoires."""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from flask import Blueprint, Response, render_template, request
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from data.loader import load_data
from modules.m5_blocs import MODES_OBJECTIF, _operations_par_defaut, inserer_urgence, solve

bp = Blueprint("m5", __name__, url_prefix="/m5")

COULEURS_PRIORITE = {
    0: ("#be123c", "Urgence vitale"),
    1: ("#e11d48", "Priorité 1 — Urgente"),
    2: ("#d97706", "Priorité 2 — Moyenne"),
    3: ("#0284c7", "Priorité 3 — Normale"),
}

PALETTE_CHIRURGIENS = (
    "#0f766e",
    "#2563eb",
    "#7c3aed",
    "#d97706",
    "#db2777",
    "#059669",
    "#dc2626",
    "#4f46e5",
)

MODES_COLORATION = ("priorite", "chirurgien")


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
            "coloration_gantt": "priorite",
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


def _ajouter_trace_gantt(
    fig: go.Figure,
    nom: str,
    couleur: str,
    ops_groupe: list[dict[str, Any]],
) -> None:
    """Ajoute un groupe de barres stylisées au diagramme de Gantt Plotly."""
    if not ops_groupe:
        return

    textes = []
    couleurs_bord = []
    epaisseurs_bord = []
    opacites = []
    statuts_visuels = []

    for op in ops_groupe:
        if op.get("est_urgence"):
            textes.append(f"URG · {op['id']} ({op['chirurgien']})")
            couleurs_bord.append("#881337")
            epaisseurs_bord.append(3.0)
            opacites.append(1.0)
            statuts_visuels.append("Nouvelle urgence")
        elif op.get("figee"):
            textes.append(f"{op['id']} [Figée]")
            couleurs_bord.append("#cbd5e1")
            epaisseurs_bord.append(1.5)
            opacites.append(0.62)
            statuts_visuels.append("Figée (en cours avant urgence)")
        elif op.get("deplacee"):
            textes.append(f"{op['id']} [Déplacée]")
            couleurs_bord.append("#1d4ed8")
            epaisseurs_bord.append(3.0)
            opacites.append(0.98)
            statuts_visuels.append("Déplacée après ré-optimisation")
        else:
            textes.append(f"{op['id']} ({op['chirurgien']})")
            couleurs_bord.append("#b91c1c" if op.get("en_heures_sup") else "#ffffff")
            epaisseurs_bord.append(2.5 if op.get("en_heures_sup") else 2.0)
            opacites.append(0.95)
            statuts_visuels.append("Planifiée")

    fig.add_trace(
        go.Bar(
            name=nom,
            orientation="h",
            y=[op["salle"] for op in ops_groupe],
            x=[(op["fin_min"] - op["debut_min"]) / 60.0 for op in ops_groupe],
            base=[op["debut_min"] / 60.0 for op in ops_groupe],
            text=textes,
            textposition="inside",
            insidetextanchor="middle",
            textfont={"color": "#ffffff", "size": 12},
            width=0.62,
            marker={
                "color": couleur,
                "opacity": opacites,
                "line": {"color": couleurs_bord, "width": epaisseurs_bord},
                "cornerradius": 6,
            },
            hovertemplate=(
                "<b>%{customdata[0]}</b> — %{y}<br>"
                "Chirurgien : <b>%{customdata[1]}</b><br>"
                "Horaire : %{customdata[2]} → %{customdata[3]} (%{customdata[4]} min)<br>"
                "Priorité : %{customdata[5]}<br>"
                "Heures sup : %{customdata[6]}<br>"
                "Statut : %{customdata[7]}<extra></extra>"
            ),
            customdata=[
                [
                    op["id"],
                    op["chirurgien"],
                    op["debut"],
                    op["fin"],
                    op["duree_min"],
                    "Urgence vitale" if op.get("est_urgence") else f"Priorité {op['priorite']}",
                    "Oui" if op.get("en_heures_sup") else "Non",
                    statuts_visuels[idx],
                ]
                for idx, op in enumerate(ops_groupe)
            ],
        )
    )


def _construire_graphe_comparaison_plotly(comparaison: dict[str, Any]) -> str | None:
    """Construit un graphique Plotly compact comparant objectif et temps des 3 méthodes."""
    methodes = comparaison.get("methodes", [])
    valides = [m for m in methodes if m.get("statut") in {"optimal", "faisable"}]
    if not valides:
        return None

    libelles_courts = []
    for m in valides:
        nom = m["nom"]
        if "Gurobi" in nom:
            libelles_courts.append("Gurobi PLNE")
        elif "CP-SAT" in nom:
            libelles_courts.append("OR-Tools CP-SAT")
        else:
            libelles_courts.append("Heuristique")

    objectifs = [float(m["objectif"]) for m in valides]
    temps = [float(m["temps"]) for m in valides]
    couleurs = ["#0e7490", "#0284c7", "#f59e0b"][: len(valides)]

    fig = make_subplots(
        rows=1,
        cols=2,
        subplot_titles=("Valeur de l'objectif (plus bas = meilleur)", "Temps de calcul (secondes)"),
        horizontal_spacing=0.14,
    )

    fig.add_trace(
        go.Bar(
            x=libelles_courts,
            y=objectifs,
            marker={"color": couleurs, "cornerradius": 6},
            text=[f"{v:.1f}" for v in objectifs],
            textposition="outside",
            showlegend=False,
            hovertemplate="<b>%{x}</b><br>Objectif : %{y:.2f}<extra></extra>",
        ),
        row=1,
        col=1,
    )

    fig.add_trace(
        go.Bar(
            x=libelles_courts,
            y=temps,
            marker={"color": couleurs, "cornerradius": 6},
            text=[f"{t:.4f} s" for t in temps],
            textposition="outside",
            showlegend=False,
            hovertemplate="<b>%{x}</b><br>Temps : %{y:.4f} s<extra></extra>",
        ),
        row=1,
        col=2,
    )

    max_obj = max(objectifs) if objectifs else 1.0
    max_t = max(temps) if temps else 0.01
    fig.update_yaxes(range=[0, max_obj * 1.22 if max_obj > 0 else 1.0], gridcolor="#e2e8f0", row=1, col=1)
    fig.update_yaxes(range=[0, max(max_t * 1.28, 0.002)], gridcolor="#e2e8f0", row=1, col=2)

    fig.update_layout(
        font={"family": "Inter, system-ui, -apple-system, sans-serif", "size": 12, "color": "#334155"},
        height=270,
        margin={"l": 45, "r": 25, "t": 45, "b": 35},
        plot_bgcolor="#f8fbfd",
        paper_bgcolor="#ffffff",
    )

    # include_plotlyjs=False car le diagramme de Gantt charge déjà Plotly sur la page
    return fig.to_html(
        full_html=False,
        include_plotlyjs=False,
        config={"displayModeBar": False, "responsive": True},
    )


def _construire_gantt_plotly(
    planning: list[dict[str, Any]],
    parametres: dict[str, Any],
    heure_arrivee_urgence_min: int | None = None,
) -> str:
    """Construit le diagramme de Gantt interactif par salle (coloré par priorité ou chirurgien)."""
    nb_salles = int(parametres["nb_salles"])
    heure_debut = int(parametres["heure_debut"])
    heure_fin = int(parametres["heure_fin"])
    max_heures_sup = float(parametres["max_heures_sup"])
    coloration = parametres.get("coloration_gantt", "priorite")

    salles_ordonnees = [f"Salle {s + 1}" for s in range(nb_salles)]
    fig = go.Figure()

    if coloration == "chirurgien":
        chirurgiens = sorted({op["chirurgien"] for op in planning})
        for idx, chir in enumerate(chirurgiens):
            couleur = PALETTE_CHIRURGIENS[idx % len(PALETTE_CHIRURGIENS)]
            ops_chir = [op for op in planning if op["chirurgien"] == chir]
            _ajouter_trace_gantt(fig, chir, couleur, ops_chir)
    else:
        for niveau in (0, 1, 2, 3):
            couleur, libelle = COULEURS_PRIORITE[niveau]
            ops_niveau = [op for op in planning if int(op["priorite"]) == niveau]
            _ajouter_trace_gantt(fig, libelle, couleur, ops_niveau)

    fin_reelle_h = max([heure_fin] + [op["fin_min"] / 60.0 for op in planning])
    fin_axe_h = max(heure_fin + (0.5 if max_heures_sup > 0 else 0.0), fin_reelle_h + 0.5)

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
        line_color="#dc2626",
        annotation_text=f"Fermeture {heure_fin:02d}:00",
        annotation_position="top right",
        annotation_font={"color": "#b91c1c", "size": 12},
    )

    if heure_arrivee_urgence_min is not None:
        h_urg = heure_arrivee_urgence_min / 60.0
        hh, mm = divmod(heure_arrivee_urgence_min, 60)
        fig.add_vline(
            x=h_urg,
            line_width=2,
            line_dash="dot",
            line_color="#2563eb",
            annotation_text=f"Arrivée urgence {hh:02d}:{mm:02d}",
            annotation_position="bottom right",
            annotation_font={"color": "#1d4ed8", "size": 12},
        )

    graduations = list(range(heure_debut, int(fin_axe_h) + 2))
    titre_coloration = "par chirurgien" if coloration == "chirurgien" else "par priorité"
    fig.update_layout(
        title={
            "text": f"Planning des blocs opératoires — Vue {titre_coloration}",
            "font": {"size": 16, "color": "#0f172a"},
            "x": 0.01,
        },
        font={"family": "Inter, system-ui, -apple-system, sans-serif", "color": "#334155"},
        barmode="overlay",
        height=max(340, 88 * nb_salles + 145),
        margin={"l": 85, "r": 30, "t": 75, "b": 50},
        xaxis={
            "title": "Horaire de la journée",
            "range": [heure_debut, fin_axe_h],
            "tickvals": graduations,
            "ticktext": [f"{h:02d}:00" for h in graduations],
            "showgrid": True,
            "gridcolor": "#e2e8f0",
            "zeroline": False,
        },
        yaxis={
            "title": "",
            "categoryorder": "array",
            "categoryarray": salles_ordonnees,
            "autorange": "reversed",
            "showgrid": False,
        },
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.03,
            "xanchor": "left",
            "x": 0,
            "bgcolor": "rgba(255,255,255,0.8)",
        },
        hoverlabel={"bgcolor": "#ffffff", "bordercolor": "#cbd5e1", "font": {"color": "#0f172a"}},
        plot_bgcolor="#f8fafc",
        paper_bgcolor="#ffffff",
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


@bp.route("/exporter-planning", methods=["POST"])
def exporter_planning_csv() -> Response:
    """Exporte le planning optimisé courant au format CSV (compatible Excel UTF-8)."""
    brut = request.form.get("planning_json", "[]")
    try:
        planning = json.loads(brut)
        if not isinstance(planning, list):
            planning = []
    except json.JSONDecodeError:
        planning = []

    tampon = io.StringIO()
    colonnes = ["salle", "id", "chirurgien", "priorite", "debut", "fin", "duree_min", "en_heures_sup"]
    ecrivain = csv.DictWriter(tampon, fieldnames=colonnes)
    ecrivain.writeheader()
    for op in planning:
        ecrivain.writerow(
            {
                "salle": op.get("salle", ""),
                "id": op.get("id", ""),
                "chirurgien": op.get("chirurgien", ""),
                "priorite": op.get("priorite", ""),
                "debut": op.get("debut", ""),
                "fin": op.get("fin", ""),
                "duree_min": op.get("duree_min", ""),
                "en_heures_sup": "Oui" if op.get("en_heures_sup") else "Non",
            }
        )

    # BOM UTF-8 pour une ouverture directe sans problème d'accents dans Excel
    return Response(
        "\ufeff" + tampon.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="planning_blocs_m5.csv"'},
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
    comp_graph_html = None
    planning_json = ""
    erreur = None
    info = None
    urgence_form = {"heure": "10:00", "duree": 60, "chirurgien": ""}

    if request.method == "POST":
        action = request.form.get("action", "resoudre").strip()

        if action == "reinitialiser":
            info = "Données réinitialisées à partir des fichiers communs (data/*.csv)."
            chirurgiens_disponibles = sorted({op["chirurgien"] for op in operations})
            urgence_form["chirurgien"] = chirurgiens_disponibles[0] if chirurgiens_disponibles else "CHIR-1"
            return render_template(
                "m5_blocs.html",
                parametres=parametres,
                hopitaux=hopitaux,
                operations=operations,
                indisponibilites=indisponibilites,
                chirurgiens_disponibles=chirurgiens_disponibles,
                urgence_form=urgence_form,
                resultat=None,
                graph_html=None,
                comp_graph_html=None,
                planning_json="",
                parametres_json="",
                operations_json="",
                indisponibilites_json="",
                erreur=None,
                info=info,
            )

        if action == "inserer_urgence":
            try:
                planning_initial = json.loads(request.form.get("planning_initial_json", "[]"))
                if request.form.get("parametres_json"):
                    parametres.update(json.loads(request.form["parametres_json"]))
                if request.form.get("operations_json"):
                    operations = json.loads(request.form["operations_json"])
                if request.form.get("indisponibilites_json"):
                    indisponibilites = json.loads(request.form["indisponibilites_json"])

                urg_heure = request.form.get("urgence_heure", "10:00").strip()
                urg_duree = int(request.form.get("urgence_duree", "60"))
                urg_chir = request.form.get("urgence_chirurgien", "").strip()
                urgence_form = {"heure": urg_heure, "duree": urg_duree, "chirurgien": urg_chir}

                if not planning_initial:
                    raise ValueError("Résolvez d'abord un planning initial avant d'insérer une urgence.")
                if urg_duree <= 0 or not urg_chir:
                    raise ValueError("Renseignez une durée (> 0) et un chirurgien pour l'urgence.")

                params_solve = {
                    "nb_salles": parametres["nb_salles"],
                    "horaires_ouverture": (parametres["heure_debut"], parametres["heure_fin"]),
                    "duree_creneau": parametres["duree_creneau"],
                    "max_heures_sup": parametres["max_heures_sup"],
                    "mode_objectif": parametres["mode_objectif"],
                    "indisponibilites": indisponibilites,
                }
                resultat = inserer_urgence(
                    planning_existant=planning_initial,
                    urgence={"duree": urg_duree, "chirurgien": urg_chir},
                    heure_arrivee=urg_heure,
                    data={},
                    params=params_solve,
                )
                planning = resultat["solution"].get("planning", [])
                if planning:
                    impact = resultat["solution"]["impact_urgence"]
                    graph_html = _construire_gantt_plotly(
                        planning,
                        parametres,
                        heure_arrivee_urgence_min=impact["heure_arrivee_min"],
                    )
                    planning_json = json.dumps(planning, ensure_ascii=False)
                    info = (
                        f"Urgence {impact['urgence_id']} insérée en {impact['salle_urgence']} "
                        f"({impact['horaire_urgence']}) : {impact['nb_figees']} figée(s), "
                        f"{impact['nb_deplacees']} déplacée(s), {impact['nb_inchangees']} inchangée(s)."
                    )
                else:
                    # Conserve le planning initial afin de pouvoir réessayer immédiatement
                    planning_json = json.dumps(planning_initial, ensure_ascii=False)
            except (ValueError, json.JSONDecodeError) as exc:
                erreur = f"Impossible d'insérer l'urgence : {exc}"
        elif action == "scenario_sature":
            parametres.update(
                {
                    "nb_salles": 2,
                    "heure_debut": 8,
                    "heure_fin": 16,
                    "duree_creneau": 30,
                    "max_heures_sup": 3.0,
                    "mode_objectif": "makespan",
                    "coloration_gantt": request.form.get("coloration_gantt", "priorite")
                    if request.form.get("coloration_gantt") in MODES_COLORATION
                    else "priorite",
                }
            )
            info = "Scénario saturé chargé : 2 salles pour 12 opérations (jusqu'à 3 h d'heures sup)."
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

                col_choisie = (
                    request.form.get("coloration_gantt", parametres["coloration_gantt"]).strip().lower()
                )
                if col_choisie in MODES_COLORATION:
                    parametres["coloration_gantt"] = col_choisie

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

        if erreur is None and action != "inserer_urgence":
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
            planning = resultat["solution"].get("planning", [])
            if planning:
                graph_html = _construire_gantt_plotly(planning, parametres)
                planning_json = json.dumps(planning, ensure_ascii=False)
                if resultat["solution"].get("comparaison"):
                    comp_graph_html = _construire_graphe_comparaison_plotly(resultat["solution"]["comparaison"])

    chirurgiens_disponibles = sorted({op["chirurgien"] for op in operations})
    if not urgence_form["chirurgien"]:
        urgence_form["chirurgien"] = chirurgiens_disponibles[0] if chirurgiens_disponibles else "CHIR-1"

    return render_template(
        "m5_blocs.html",
        parametres=parametres,
        hopitaux=hopitaux,
        operations=operations,
        indisponibilites=indisponibilites,
        chirurgiens_disponibles=chirurgiens_disponibles,
        urgence_form=urgence_form,
        resultat=resultat,
        graph_html=graph_html,
        comp_graph_html=comp_graph_html,
        planning_json=planning_json,
        parametres_json=json.dumps(parametres, ensure_ascii=False),
        operations_json=json.dumps(operations, ensure_ascii=False),
        indisponibilites_json=json.dumps(indisponibilites, ensure_ascii=False),
        erreur=erreur,
        info=info,
    )
