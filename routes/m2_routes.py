"""Routes Flask — Module M2 : Emplacement des ambulances.

Étape 1 : version de base (K, R, Gurobi contre glouton).
Étape 2 : panne de bases (cases à cocher) + comparaison situation normale / scénario.
Étape 3 : carte Folium des zones couvertes.
"""

import math

import folium
import pandas as pd
from flask import Blueprint, render_template, request

from modules.m2_ambulances import glouton, solve

bp = Blueprint("m2", __name__, url_prefix="/m2")

# ----- Données de démonstration (x, y en km, région de Sfax) -----
COMMUNES = pd.DataFrame({
    "nom": ["Sfax Médina", "Sakiet Ezzit", "Sakiet Eddaïer", "Chihia", "El Aïn", "Thyna", "Gremda",
            "Agareb", "El Hencha", "Jebeniana", "Mahrès", "Bir Ali Ben Khalifa", "Menzel Chaker", "Kerkennah"],
    "x": [30, 28, 34, 24, 26, 30, 22, 10, 40, 48, 18, 2, 5, 58],
    "y": [30, 36, 36, 34, 26, 22, 30, 30, 56, 58, 8, 12, 45, 42],
    "population": [120000, 60000, 55000, 40000, 50000, 45000, 35000,
                   38000, 30000, 45000, 32000, 25000, 22000, 15000],
})

BASES = pd.DataFrame({
    "id": [1, 2, 3, 4, 5, 6, 7, 8],
    "x": [34, 34, 18, 12, 44, 16, 6, 52],
    "y": [34, 26, 36, 28, 56, 10, 42, 44],
    "type": ["base_ambulance"] * 8,
})

# ----- Carte : le point (x=30, y=30) est placé sur Sfax -----
LAT_SFAX, LNG_SFAX = 34.74, 10.76


def vers_gps(x, y):
    """Convertit (x, y) en km en (latitude, longitude). 1 degré de latitude ≈ 111 km."""
    lat = LAT_SFAX + (y - 30) / 111
    lng = LNG_SFAX + (x - 30) / (111 * math.cos(math.radians(LAT_SFAX)))
    return [lat, lng]


def dessiner_carte(resultat, bases_en_panne, R):
    """Carte : bases (vert = ouverte, gris = fermée, rouge = en panne) et communes couvertes ou non."""
    carte = folium.Map(location=[LAT_SFAX, LNG_SFAX], zoom_start=9)
    bases_ouvertes = resultat["solution"]["bases_ouvertes"]
    communes_couvertes = resultat["solution"]["communes_couvertes"]

    # 1. Les bases
    for _, b in BASES.iterrows():
        position = vers_gps(b["x"], b["y"])
        if b["id"] in bases_en_panne:
            couleur, texte = "red", "en panne"
        elif b["id"] in bases_ouvertes:
            couleur, texte = "green", "ouverte"
            # Cercle = zone atteinte en R minutes (vitesse 1 km/min, donc rayon = R km)
            folium.Circle(position, radius=R * 1000, color="#155e75", fill=True, fill_opacity=0.1).add_to(carte)
        else:
            couleur, texte = "gray", "fermée"
        folium.Marker(position, tooltip=f"Base {b['id']} ({texte})",
                      icon=folium.Icon(color=couleur, icon="plus-sign")).add_to(carte)

    # 2. Les communes : taille selon la population
    for _, c in COMMUNES.iterrows():
        couverte = c["nom"] in communes_couvertes
        folium.CircleMarker(
            vers_gps(c["x"], c["y"]),
            radius=4 + c["population"] / 15000,
            color="#16a34a" if couverte else "#dc2626",
            fill=True, fill_opacity=0.7,
            tooltip=f"{c['nom']} : {c['population']} hab. ({'couverte' if couverte else 'NON couverte'})",
        ).add_to(carte)

    return carte.get_root().render()


@bp.route("/", methods=["GET", "POST"])
def page_m2():
    """Lit K, R et les pannes, appelle solve() et glouton(), puis affiche les résultats."""
    K = 3            # valeurs affichées à la première ouverture
    R = 10
    bases_en_panne = []
    resultat = None
    resultat_glouton = None
    reference = None      # situation normale, calculée seulement s'il y a un scénario
    carte_html = None

    if request.method == "POST":
        # 1. Lire ce que l'utilisateur a tapé
        K = int(request.form["nb_ambulances"])
        R = float(request.form["rayon_minutes"])
        # Les cases cochées arrivent sous forme de liste de textes ["1", "4"] : on les convertit en nombres
        bases_en_panne = [int(j) for j in request.form.getlist("bases_en_panne")]

        # 2. Préparer data et params comme dans les tests
        data = {"communes": COMMUNES, "sites_candidats": BASES}
        params = {"nb_ambulances": K, "rayon_minutes": R, "bases_en_panne": bases_en_panne}

        # 3. Résoudre avec les deux méthodes
        resultat = solve(data, params)
        resultat_glouton = glouton(data, params)

        # 4. S'il y a un scénario (panne), on calcule aussi la situation normale pour comparer
        if bases_en_panne:
            reference = solve(data, {"nb_ambulances": K, "rayon_minutes": R})

        # 5. La carte
        carte_html = dessiner_carte(resultat, bases_en_panne, R)

    return render_template(
        "m2_ambulances.html",
        K=K, R=R,
        bases_en_panne=bases_en_panne,
        resultat=resultat,
        resultat_glouton=resultat_glouton,
        reference=reference,
        carte_html=carte_html,
        population_totale=int(COMMUNES["population"].sum()),
        communes=COMMUNES.to_dict("records"),
        bases=BASES.to_dict("records"),
    )