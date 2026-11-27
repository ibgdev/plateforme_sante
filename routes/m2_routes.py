"""Routes Flask — Module M2 : Emplacement des ambulances.

Étape 1 : version de base (K, R, Gurobi contre glouton).
Étape 2 : panne de bases + comparaison situation normale / scénario.
Étape 3 : carte Folium des zones couvertes.
Étape 4 : scénarios de demande, tableaux modifiables et import CSV.
"""

import math

import folium
import pandas as pd
from flask import Blueprint, render_template, request

from modules.m2_ambulances import glouton, solve

bp = Blueprint("m2", __name__, url_prefix="/m2")

# ----- Données de démonstration (x, y en km, région de Sfax) -----
COMMUNES_DEMO = [
    ("Sfax Médina", 30, 30, 120000), ("Sakiet Ezzit", 28, 36, 60000), ("Sakiet Eddaïer", 34, 36, 55000),
    ("Chihia", 24, 34, 40000), ("El Aïn", 26, 26, 50000), ("Thyna", 30, 22, 45000),
    ("Gremda", 22, 30, 35000), ("Agareb", 10, 30, 38000), ("El Hencha", 40, 56, 30000),
    ("Jebeniana", 48, 58, 45000), ("Mahrès", 18, 8, 32000), ("Bir Ali Ben Khalifa", 2, 12, 25000),
    ("Menzel Chaker", 5, 45, 22000), ("Kerkennah", 58, 42, 15000),
]
BASES_DEMO = [(1, 34, 34), (2, 34, 26), (3, 18, 36), (4, 12, 28), (5, 44, 56), (6, 16, 10), (7, 6, 42), (8, 52, 44)]

# ----- Scénarios de demande : % de hausse par commune -----
SCENARIOS = {
    "normal": {"titre": "Normal", "hausses": {}},
    "ete": {"titre": "Été (touristes)", "hausses": {"Kerkennah": 400, "Mahrès": 100}},
    "foyer": {"titre": "Foyer épidémique (sud)", "hausses": {"Mahrès": 150, "Bir Ali Ben Khalifa": 100}},
}

# ----- Carte : le point (x=30, y=30) est placé sur Sfax -----
LAT_SFAX, LNG_SFAX = 34.74, 10.76


def donnees_demo():
    """Tableaux affichés à la première ouverture de la page."""
    communes = [{"nom": n, "x": x, "y": y, "population": p, "hausse": 0} for n, x, y, p in COMMUNES_DEMO]
    bases = [{"id": i, "x": x, "y": y, "en_panne": False} for i, x, y in BASES_DEMO]
    return communes, bases


def lire_tableaux(formulaire):
    """Lit les tableaux communes et bases envoyés par le formulaire (une liste par colonne)."""
    communes = []
    for nom, x, y, pop, hausse in zip(formulaire.getlist("commune_nom"), formulaire.getlist("commune_x"),
                                      formulaire.getlist("commune_y"), formulaire.getlist("commune_population"),
                                      formulaire.getlist("commune_hausse")):
        if nom.strip():   # on ignore les lignes sans nom
            communes.append({"nom": nom.strip(), "x": float(x), "y": float(y),
                             "population": float(pop), "hausse": float(hausse or 0)})

    bases = []
    for id_base, x, y, panne in zip(formulaire.getlist("base_id"), formulaire.getlist("base_x"),
                                    formulaire.getlist("base_y"), formulaire.getlist("base_panne")):
        if id_base.strip():
            id_base = id_base.strip()
            bases.append({"id": int(id_base) if id_base.isdigit() else id_base,
                          "x": float(x), "y": float(y), "en_panne": panne == "1"})
    return communes, bases


def lire_csv(fichier, colonnes):
    """Lit un fichier CSV (séparateur , ou ;) et vérifie les colonnes obligatoires."""
    tableau = pd.read_csv(fichier, sep=None, engine="python")
    tableau.columns = [c.strip().lower() for c in tableau.columns]
    manquantes = [c for c in colonnes if c not in tableau.columns]
    if manquantes:
        raise ValueError(f"colonnes manquantes dans le CSV : {', '.join(manquantes)} "
                         f"(colonnes attendues : {', '.join(colonnes)}).")
    return tableau.to_dict("records")


def vers_gps(x, y):
    """Convertit (x, y) en km en (latitude, longitude). 1 degré de latitude ≈ 111 km."""
    lat = LAT_SFAX + (y - 30) / 111
    lng = LNG_SFAX + (x - 30) / (111 * math.cos(math.radians(LAT_SFAX)))
    return [lat, lng]


def dessiner_carte(resultat, communes, bases, R):
    """Carte : bases (vert = ouverte, gris = fermée, rouge = en panne) et communes couvertes ou non."""
    carte = folium.Map(location=[LAT_SFAX, LNG_SFAX], zoom_start=9)
    bases_ouvertes = resultat["solution"]["bases_ouvertes"]
    communes_couvertes = resultat["solution"]["communes_couvertes"]

    # 1. Les bases
    for b in bases:
        position = vers_gps(b["x"], b["y"])
        if b["en_panne"]:
            couleur, texte = "red", "en panne"
        elif b["id"] in bases_ouvertes:
            couleur, texte = "green", "ouverte"
            # Cercle = zone atteinte en R minutes (vitesse 1 km/min, donc rayon = R km)
            folium.Circle(position, radius=R * 1000, color="#155e75", fill=True, fill_opacity=0.1).add_to(carte)
        else:
            couleur, texte = "gray", "fermée"
        folium.Marker(position, tooltip=f"Base {b['id']} ({texte})",
                      icon=folium.Icon(color=couleur, icon="plus-sign")).add_to(carte)

    # 2. Les communes : taille selon la demande du scénario
    for c in communes:
        couverte = c["nom"] in communes_couvertes
        demande = c["population"] * (1 + c["hausse"] / 100)
        folium.CircleMarker(
            vers_gps(c["x"], c["y"]),
            radius=4 + demande / 15000,
            color="#16a34a" if couverte else "#dc2626",
            fill=True, fill_opacity=0.7,
            tooltip=f"{c['nom']} : {demande:.0f} hab. ({'couverte' if couverte else 'NON couverte'})",
        ).add_to(carte)

    return carte.get_root().render()


@bp.route("/", methods=["GET", "POST"])
def page_m2():
    """Lit les paramètres et les tableaux, appelle solve() et glouton(), puis affiche les résultats."""
    K = 3            # valeurs affichées à la première ouverture
    R = 10
    scenario = "normal"
    communes, bases = donnees_demo()
    erreur = None
    resultat = resultat_glouton = reference = carte_html = None

    if request.method == "POST":
        try:
            # 1. Lire les paramètres et les tableaux
            K = int(request.form["nb_ambulances"])
            R = float(request.form["rayon_minutes"])
            scenario = request.form.get("scenario", "normal")
            communes, bases = lire_tableaux(request.form)

            # 2. Import CSV : un fichier envoyé remplace le tableau correspondant
            fichier = request.files.get("csv_communes")
            if fichier and fichier.filename:
                lignes = lire_csv(fichier, ["nom", "x", "y", "population"])
                communes = [{"nom": str(l["nom"]), "x": float(l["x"]), "y": float(l["y"]),
                             "population": float(l["population"]), "hausse": 0} for l in lignes]
            fichier = request.files.get("csv_bases")
            if fichier and fichier.filename:
                lignes = lire_csv(fichier, ["id", "x", "y"])
                bases = [{"id": l["id"], "x": float(l["x"]), "y": float(l["y"]), "en_panne": False} for l in lignes]

            # 3. Vérifications simples
            if not communes or not bases:
                raise ValueError("il faut au moins une commune et une base.")
            noms = [c["nom"] for c in communes]
            if len(set(noms)) != len(noms):
                raise ValueError("deux communes ont le même nom.")

            # 4. Préparer data et params comme dans les tests
            data = {
                "communes": pd.DataFrame(communes),
                "sites_candidats": pd.DataFrame([{**b, "type": "base_ambulance"} for b in bases]),
            }
            bases_en_panne = [b["id"] for b in bases if b["en_panne"]]
            hausse_demande = {c["nom"]: c["hausse"] for c in communes if c["hausse"] != 0}
            params = {"nb_ambulances": K, "rayon_minutes": R,
                      "bases_en_panne": bases_en_panne, "hausse_demande": hausse_demande}

            # 5. Résoudre avec les deux méthodes
            resultat = solve(data, params)
            resultat_glouton = glouton(data, params)

            # 6. S'il y a un scénario (panne ou hausse), on calcule aussi la situation normale
            if bases_en_panne or hausse_demande:
                reference = solve(data, {"nb_ambulances": K, "rayon_minutes": R})

            # 7. La carte
            carte_html = dessiner_carte(resultat, communes, bases, R)

        except (ValueError, KeyError) as exc:
            if "could not convert" in str(exc):   # float("") : une case numérique est vide
                erreur = "Données invalides : une case numérique est vide ou mal écrite."
            else:
                erreur = f"Données invalides : {exc}"

    # Demande totale normale et avec le scénario (pour le taux et les « sans ambulance »)
    population_normale = sum(c["population"] for c in communes)
    demande_scenario = sum(c["population"] * (1 + c["hausse"] / 100) for c in communes)

    return render_template(
        "m2_ambulances.html",
        K=K, R=R, scenario=scenario, scenarios=SCENARIOS,
        communes=communes, bases=bases, erreur=erreur,
        resultat=resultat, resultat_glouton=resultat_glouton,
        reference=reference, carte_html=carte_html,
        population_normale=population_normale, demande_scenario=demande_scenario,
    )