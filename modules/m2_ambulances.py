"""Module M2 — Emplacement des ambulances.

Type    : Localisation (Couverture maximale, PLNE)
Outils  : gurobipy, Folium
Données : communes.csv, sites_candidats.csv, temps de trajet
Branche : m2_ambulances
"""

import math
import time

import gurobipy as gp
from gurobipy import GRB


# =====================================================================
# Outils
# =====================================================================

def temps_trajet(point_a, point_b, vitesse):
    """Temps en minutes entre deux points (x, y)."""
    xa, ya = point_a
    xb, yb = point_b
    distance = math.sqrt((xa - xb) ** 2 + (ya - yb) ** 2)
    return distance / vitesse


def preparer_donnees(data):
    """Transforme les tableaux pandas du projet en dictionnaires pour le modèle."""
    df_communes = data["communes"]
    df_sites = data["sites_candidats"]

    communes = {}
    for _, ligne in df_communes.iterrows():
        communes[ligne["nom"]] = (ligne["x"], ligne["y"], ligne["population"])

    df_bases = df_sites[df_sites["type"] == "base_ambulance"]

    bases = {}
    for _, ligne in df_bases.iterrows():
        bases[ligne["id"]] = (ligne["x"], ligne["y"])

    return communes, bases


def retirer_bases_en_panne(bases, params):
    """Étape 2 : enlève les bases en panne de la liste des bases candidates."""
    bases_en_panne = params.get("bases_en_panne", [])   # [] = aucune panne par défaut
    bases_actives = {}
    for j, position in bases.items():
        if j not in bases_en_panne:
            bases_actives[j] = position
    return bases_actives


# =====================================================================
# Méthode exacte : couverture maximale avec Gurobi
# =====================================================================

def solve(data: dict, params: dict) -> dict:
    """Résout le problème de couverture maximale pour les bases d'ambulances.

    Paramètres attendus dans `params` :
        - nb_ambulances (int) : nombre K de bases à activer.
        - rayon_minutes (float) : rayon de couverture R en minutes.
        - vitesse (float, optionnel) : vitesse moyenne en km/min (1.0 par défaut).
        - bases_en_panne (list, optionnel) : id des bases qui ne peuvent pas être ouvertes.
    """
    # --- Lire les entrées ---
    communes, bases = preparer_donnees(data)   # communes {nom: (x, y, pop)}, bases {id: (x, y)}
    bases = retirer_bases_en_panne(bases, params)   # une base en panne ne peut pas être ouverte
    population = {i: c[2] for i, c in communes.items()}
    R = params["rayon_minutes"]
    K = params["nb_ambulances"]
    vitesse = params.get("vitesse", 1.0)   # 1.0 par défaut si non fourni

    debut = time.time()   # on démarre le chronomètre

    # --- Calculer N_i : les bases qui couvrent chaque commune ---
    couverture = {}
    for i, (xi, yi, _) in communes.items():
        couverture[i] = []
        for j, (xj, yj) in bases.items():
            t = temps_trajet((xi, yi), (xj, yj), vitesse)
            if t <= R:
                couverture[i].append(j)

    # --- Construire le modèle ---
    m = gp.Model("couverture_maximale")
    m.Params.OutputFlag = 0    # cache les lignes techniques de Gurobi

    # ===== 1. LES VARIABLES =====
    y = m.addVars(bases, vtype=GRB.BINARY, name="y")      # y_j
    z = m.addVars(communes, vtype=GRB.BINARY, name="z")   # z_i

    # ===== 2. L'OBJECTIF =====
    m.setObjective(gp.quicksum(population[i] * z[i] for i in population), GRB.MAXIMIZE)

    # ===== 3. LES CONTRAINTES =====
    for i in communes:
        m.addConstr(z[i] <= gp.quicksum(y[j] for j in couverture[i]), name=f"couv_{i}")
    m.addConstr(gp.quicksum(y[j] for j in bases) <= K, name="nb_ambulances")

    # ===== 4. RÉSOUDRE =====
    m.optimize()

    temps = time.time() - debut   # on arrête le chronomètre
    if m.Status != GRB.OPTIMAL:
        return {"statut": "infaisable", "objectif": 0.0, "solution": {}, "temps": round(temps, 3)}

    # --- Préparer le résultat ---
    bases_ouvertes = []
    for j in bases:
        if y[j].X > 0.5:
            bases_ouvertes.append(j)
    communes_couvertes = []
    for i in communes:
        if z[i].X > 0.5:
            communes_couvertes.append(i)

    return {
        "statut": "optimal",
        "objectif": round(m.ObjVal, 2) + 0.0,
        "solution": {
            "bases_ouvertes": bases_ouvertes,
            "communes_couvertes": communes_couvertes,
        },
        "temps": round(temps, 3),
    }


# =====================================================================
# Méthode de comparaison : heuristique gloutonne
# =====================================================================

def glouton(data: dict, params: dict) -> dict:
    """Heuristique gloutonne : ajoute à chaque tour la base qui couvre le plus de nouvelle population."""
    communes, bases = preparer_donnees(data)
    bases = retirer_bases_en_panne(bases, params)
    population = {i: c[2] for i, c in communes.items()}
    R = params["rayon_minutes"]
    K = params["nb_ambulances"]
    vitesse = params.get("vitesse", 1.0)

    debut = time.time()

    # --- M_j : les communes couvertes par chaque base ---
    couvre = {}
    for j, (xj, yj) in bases.items():
        couvre[j] = []
        for i, (xi, yi, _) in communes.items():
            t = temps_trajet((xi, yi), (xj, yj), vitesse)
            if t <= R:
                couvre[j].append(i)

    # --- On choisit les bases une par une ---
    bases_choisies = []
    deja_couvertes = set()      # un "set" = un ensemble sans doublons

    for _ in range(K):
        meilleure_base = None
        meilleur_gain = 0
        for j in bases:
            if j in bases_choisies:
                continue        # on saute les bases déjà prises
            gain = sum(population[i] for i in couvre[j] if i not in deja_couvertes)
            if gain > meilleur_gain:
                meilleur_gain = gain
                meilleure_base = j
        if meilleure_base is None:
            break               # plus aucune base n'apporte quelqu'un : on arrête
        bases_choisies.append(meilleure_base)
        deja_couvertes.update(couvre[meilleure_base])

    temps = time.time() - debut
    return {
        "statut": "faisable",   # une heuristique ne garantit pas l'optimum
        "objectif": sum(population[i] for i in deja_couvertes),
        "solution": {"bases_ouvertes": bases_choisies, "communes_couvertes": sorted(deja_couvertes)},
        "temps": round(temps, 3),
    }