"""Prompt versionné et construction des messages envoyés au juge.

Le prompt système (``prompt_v1.md``) ne contient que des instructions. Le profil et l'offre partent
dans le message utilisateur, comme **données** dans des balises délimitées. Les chevrons des
données sont neutralisés pour qu'un texte d'offre ne puisse pas fermer une balise.
"""

import json
from importlib import resources
from typing import Any

from vigie.match.judge.base import MAX_PROFILE_TEXT, OfferView
from vigie.match.profile import Profile
from vigie.match.text import clean_text, truncate

# À changer avec le fichier du prompt : le cache des évaluations en dépend (section 4).
PROMPT_VERSION = "v1"
PROMPT_FILE = f"prompt_{PROMPT_VERSION}.md"


def system_prompt() -> str:
    return resources.files("vigie.match.judge").joinpath(PROMPT_FILE).read_text(encoding="utf-8")


def _defang(text: str) -> str:
    """Remplace les chevrons : une donnée ne peut pas fermer ``</offre>`` ni ouvrir une balise."""
    return text.replace("<", "‹").replace(">", "›")


def profile_data(profile: Profile) -> dict[str, Any]:
    """Profil envoyé : sans noms d'entreprise ni dates (limite ce qui quitte le dépôt privé)."""
    return {
        "resume": truncate(clean_text(profile.summary), MAX_PROFILE_TEXT),
        "roles_cibles": [clean_text(role) for role in profile.target_roles],
        "competences": {
            "maitrisees": profile.skills.must_have,
            "en_acquisition_ou_secondaires": profile.skills.nice_to_have,
            "a_eviter": profile.skills.avoid,
        },
        "experiences": [
            {
                "titre": clean_text(exp.title),
                "description": truncate(clean_text(exp.description), MAX_PROFILE_TEXT),
                "competences": exp.skills,
            }
            for exp in profile.experiences
        ],
        "preferences": {
            "contrats": [c.value for c in profile.preferences.contract_types],
            "teletravail": [r.value for r in profile.preferences.remote],
            "salaire_minimum_brut_annuel": profile.preferences.min_salary,
        },
    }


def offer_data(offer: OfferView) -> dict[str, Any]:
    return {
        "titre": offer.title,
        "entreprise": offer.company,
        "lieu": offer.location,
        "contrat": offer.contract,
        "teletravail": offer.remote,
        "salaire": offer.salary,
        "description": offer.description,
    }


def user_message(profile: Profile, offer: OfferView) -> str:
    def block(tag: str, data: dict[str, Any]) -> str:
        return f"<{tag}>\n{_defang(json.dumps(data, ensure_ascii=False, indent=1))}\n</{tag}>"

    return block("profil", profile_data(profile)) + "\n" + block("offre", offer_data(offer))


def messages(profile: Profile, offer: OfferView) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": user_message(profile, offer)},
    ]
