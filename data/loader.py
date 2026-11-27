"""Chargement centralisé des données communes (`data/`).

Chaque module et chaque route Flask peut importer `load_data()` afin de partager
exactement la même structure de dictionnaire `data`.
"""

from pathlib import Path
from typing import Any

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent

NOMS_CSV_COMMUNS = (
    "communes",
    "sites_candidats",
    "hopitaux",
    "patients",
    "personnel",
    "operations",
    "medicaments",
    "fournisseurs",
)


def load_data(data_dir: Path = DATA_DIR) -> dict[str, Any]:
    """Charge les CSV présents dans `data/` et retourne le dictionnaire `data`.

    Chaque fichier `<nom>.csv` existant est chargé sous la clé `<nom>` en tant
    que `pandas.DataFrame` (les cellules vides restent `""` pour éviter les `NaN`).
    """
    repertoire = Path(data_dir)
    donnees: dict[str, Any] = {}

    for nom in NOMS_CSV_COMMUNS:
        chemin = repertoire / f"{nom}.csv"
        if chemin.exists():
            donnees[nom] = pd.read_csv(chemin, keep_default_na=False)

    return donnees
