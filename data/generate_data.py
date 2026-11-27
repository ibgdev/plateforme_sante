"""Script de génération du jeu de données commun (Section 3.1).

Option B (socle M5 extensible) :
Génère de manière déterministe (`SEED = 42`) les 3 fichiers CSV utilisés par M5 :
- `data/hopitaux.csv`
- `data/personnel.csv`
- `data/operations.csv`

La structure est pensée pour que les autres modules puissent facilement ajouter
la génération des 5 autres CSV (`communes.csv`, `sites_candidats.csv`,
`patients.csv`, `medicaments.csv`, `fournisseurs.csv`).
"""

from pathlib import Path
import random

import pandas as pd

SEED = 42
DATA_DIR = Path(__file__).resolve().parent


def generer_hopitaux(data_dir: Path = DATA_DIR) -> pd.DataFrame:
    """Génère `hopitaux.csv` avec des capacités hospitalières cohérentes."""
    hopitaux = [
        {"id": "H1", "nom": "CHU Centre", "x": 10.0, "y": 15.0, "nb_lits": 120, "nb_salles_operation": 4},
        {"id": "H2", "nom": "Hôpital Nord", "x": 24.5, "y": 32.0, "nb_lits": 85, "nb_salles_operation": 3},
        {"id": "H3", "nom": "Clinique Sud", "x": 6.5, "y": 5.0, "nb_lits": 60, "nb_salles_operation": 2},
    ]
    df = pd.DataFrame(hopitaux)
    df.to_csv(data_dir / "hopitaux.csv", index=False)
    return df


def generer_personnel(data_dir: Path = DATA_DIR) -> pd.DataFrame:
    """Génère `personnel.csv` (chirurgiens pour M5 et infirmiers compatibles M6)."""
    personnel = [
        # Chirurgiens (utilisés par M5) avec plages d'indisponibilité réalistes
        {"id": "CHIR-1", "role": "chirurgien", "contrat": "temps_plein", "indisponibilites": "11:00-12:00"},
        {"id": "CHIR-2", "role": "chirurgien", "contrat": "temps_plein", "indisponibilites": "08:00-09:00"},
        {"id": "CHIR-3", "role": "chirurgien", "contrat": "temps_plein", "indisponibilites": "14:00-15:00"},
        {"id": "CHIR-4", "role": "chirurgien", "contrat": "temps_partiel", "indisponibilites": "13:00-16:00"},
    ]
    # Infirmiers (utilisés par M6) avec contrats standards
    for index in range(1, 13):
        personnel.append(
            {
                "id": f"I{index}",
                "role": "infirmier",
                "contrat": "standard",
                "indisponibilites": "",
            }
        )

    df = pd.DataFrame(personnel)
    df.to_csv(data_dir / "personnel.csv", index=False)
    return df


def generer_operations(seed: int = SEED, data_dir: Path = DATA_DIR) -> pd.DataFrame:
    """Génère `operations.csv` : 12 opérations réalistes (30 min à 4 h) réalisables sur 8 h."""
    rng = random.Random(seed)

    # Opérations équilibrées par chirurgien afin de respecter les indisponibilités
    # et garantir une solution faisable sans heures sup sur 4 salles (08:00-16:00).
    catalogue = [
        ("CHIR-1", 120, 1),
        ("CHIR-1", 90, 2),
        ("CHIR-1", 60, 3),
        ("CHIR-2", 150, 1),
        ("CHIR-2", 90, 2),
        ("CHIR-2", 60, 2),
        ("CHIR-3", 180, 1),
        ("CHIR-3", 60, 2),
        ("CHIR-3", 90, 3),
        ("CHIR-4", 90, 1),
        ("CHIR-4", 60, 2),
        ("CHIR-4", 60, 3),
    ]
    rng.shuffle(catalogue)

    operations = [
        {
            "id": f"OP-{idx:02d}",
            "duree": duree,
            "chirurgien": chirurgien,
            "priorite": priorite,
        }
        for idx, (chirurgien, duree, priorite) in enumerate(catalogue, start=1)
    ]
    df = pd.DataFrame(operations)
    df.to_csv(data_dir / "operations.csv", index=False)
    return df


def generate_all(seed: int = SEED, data_dir: Path = DATA_DIR) -> dict[str, pd.DataFrame]:
    """Génère les fichiers CSV communs de manière déterministe (`SEED = 42`)."""
    data_dir.mkdir(parents=True, exist_ok=True)
    return {
        "hopitaux": generer_hopitaux(data_dir=data_dir),
        "personnel": generer_personnel(data_dir=data_dir),
        "operations": generer_operations(seed=seed, data_dir=data_dir),
    }


if __name__ == "__main__":
    fichiers = generate_all()
    for nom, df in fichiers.items():
        print(f"Généré data/{nom}.csv ({len(df)} lignes)")
