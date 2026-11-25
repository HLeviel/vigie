"""Variantes comparées : chacune donne un score par offre ; le classement en découle."""

import math
import re
from collections import Counter

from vigie.match.evaluation.dataset import EvalItem
from vigie.match.profile import Profile, normalize_skill
from vigie.match.text import clean_text, offer_text

_TOKEN = re.compile(r"[a-z0-9+#]{2,}")

BASELINE_DATE = "baseline:date"
BASELINE_LEXICAL = "baseline:lexical"
BASELINE_TFIDF = "baseline:tfidf"


def date_scores(items: list[EvalItem]) -> dict[str, float]:
    """Classement actuel du digest avant ``match`` : le plus récent d'abord."""
    return {i.uid: i.offer.published_at.timestamp() for i in items}


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(normalize_skill(clean_text(text)))


def profile_document(profile: Profile) -> str:
    parts = [profile.summary, *profile.target_roles]
    for exp in profile.experiences:
        parts += [exp.title, exp.description, *exp.skills]
    parts += [*profile.skills.must_have, *profile.skills.nice_to_have]
    return " . ".join(p for p in parts if p)


def tfidf_scores(profile: Profile, items: list[EvalItem]) -> dict[str, float]:
    """Similarité TF-IDF (cosinus) entre le profil et chaque offre, sans embeddings.

    L'IDF est calculée sur le corpus des offres évaluées plus le profil : cette variante dépend
    donc du jeu (utile pour une évaluation, pas pour scorer une offre isolée).
    """
    docs = {i.uid: Counter(_tokens(offer_text(i.offer, 10_000))) for i in items}
    profile_tf = Counter(_tokens(profile_document(profile)))
    corpus = [*docs.values(), profile_tf]
    df: Counter[str] = Counter()
    for doc in corpus:
        df.update(doc.keys())
    n = len(corpus)

    def vector(tf: Counter[str]) -> dict[str, float]:
        return {
            term: (1 + math.log(count)) * (math.log((1 + n) / (1 + df[term])) + 1)
            for term, count in tf.items()
        }

    def cosine(a: dict[str, float], b: dict[str, float]) -> float:
        dot = sum(w * b.get(term, 0.0) for term, w in a.items())
        norm = math.sqrt(sum(w * w for w in a.values())) * math.sqrt(sum(w * w for w in b.values()))
        return 0.0 if norm == 0 else dot / norm

    reference = vector(profile_tf)
    return {uid: cosine(vector(tf), reference) for uid, tf in docs.items()}
