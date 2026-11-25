"""Rapport d'évaluation publiable : Markdown + graphique SVG, agrégats seulement.

Le rapport ne contient **aucun titre d'offre ni nom d'entreprise** : uniquement des effectifs, des
métriques, des noms de variantes (donc de modèles) et le début de l'empreinte du profil.
"""

from xml.sax.saxutils import escape

from vigie.match.evaluation.dataset import MIN_LABELED, MIN_RELEVANT
from vigie.match.evaluation.metrics import METRICS, PRIMARY
from vigie.match.evaluation.runner import Comparison, EvalResult, VariantResult

CHART_METRIC = "ndcg@10"


def _pct(value: float) -> str:
    return f"{value:.3f}"


def _cell(result: VariantResult, metric: str) -> str:
    low, high = result.intervals[metric]
    return f"{_pct(result.metrics[metric])} [{_pct(low)} ; {_pct(high)}]"


def _comparison(c: Comparison) -> str:
    return f"{c.mean:+.3f} [{c.low:+.3f} ; {c.high:+.3f}] : {c.verdict}"


def render_markdown(result: EvalResult, svg_name: str | None = None) -> str:
    d, t, m = result.dataset, result.tuning, result.measure
    date = result.generated_at.strftime("%Y-%m-%d")
    lines = [
        f"# Évaluation du classement `match` du {date}",
        "",
        f"- Version du profil (empreinte tronquée) : `{result.profile_hash[:12]}`",
        f"- Jeu annoté : {d.total} offres ({d.relevant} pertinentes, {d.maybe} peut-être, "
        f"{d.irrelevant} non pertinentes)",
        f"- Découpage fixe : {t.total} offres pour le réglage, {m.total} pour la mesure "
        f"({m.relevant} pertinentes)",
        f"- Intervalles de confiance à 95 % par bootstrap apparié ({result.draws} tirages) sur "
        "la partie de mesure",
        "",
    ]
    if not d.meets_goal:
        lines += [
            f"> **Jeu insuffisant** : l'objectif est de {MIN_LABELED} offres annotées dont "
            f"{MIN_RELEVANT} pertinentes. Ne pas tirer de décision de ce rapport.",
            "",
        ]
    names = list(METRICS)
    lines += [
        "## Métriques (partie de mesure)",
        "",
        "| Variante | " + " | ".join(names) + " |",
        "|---|" + "---|" * len(names),
    ]
    for r in result.variants:
        lines.append(f"| `{r.name}` | " + " | ".join(_cell(r, n) for n in names) + " |")
    lines += [
        "",
        "## Écart avec `baseline:date` (même tirages, écart = variante − date)",
        "",
        "Un écart dont l'intervalle contient 0 n'est pas présenté comme un gain.",
        "",
        "| Variante | " + " | ".join(PRIMARY) + " |",
        "|---|" + "---|" * len(PRIMARY),
    ]
    for name, comps in result.vs_date.items():
        lines.append(f"| `{name}` | " + " | ".join(_comparison(comps[p]) for p in PRIMARY) + " |")
    lines += [
        "",
        "## Comparaisons ciblées",
        "",
        "| Comparaison | Métrique | Écart |",
        "|---|---|---|",
    ]
    for c in result.targeted:
        lines.append(f"| `{c.a}` − `{c.b}` | {c.metric} | {_comparison(c)} |")
    tuned = result.tuned
    lines += [
        "",
        "## Réglage (partie de réglage seulement)",
        "",
        f"- Modèle retenu pour le variant hybride : `{tuned.model}` (meilleur nDCG@10 "
        "sémantique seul)",
        f"- Poids : sémantique {tuned.semantic_weight}, lexical {tuned.lexical_weight}",
        f"- Normalisation du cosinus : plancher {tuned.floor}, plafond {tuned.ceiling}",
        "",
        "## Temps de calcul et coût",
        "",
        "| Variante | Temps par offre | Offres plongées pendant cette évaluation |",
        "|---|---|---|",
    ]
    for r in result.variants:
        if r.name.startswith("emb:"):
            seconds = (
                f"{r.seconds_per_offer * 1000:.1f} ms"
                if r.seconds_per_offer is not None
                else "non mesuré (vecteurs déjà en cache)"
            )
            lines.append(f"| `{r.name}` | {seconds} | {r.offers_embedded} |")
        else:
            lines.append(f"| `{r.name}` | négligeable (sans fournisseur) | 0 |")
    lines += [
        "",
        "Coût par exécution : le tarif des fournisseurs n'est pas renseigné ici (non vérifié) ; "
        "le coût d'un fournisseur `local` est nul, celui de `bedrock` se déduit du nombre "
        "d'offres plongées par exécution et du tarif en vigueur.",
        "",
        "## Limites",
        "",
        "- `hybrid+llm` n'est pas évalué (lot M3).",
        "- Les annotations sont conservées d'une version du profil à l'autre : elles ne sont "
        "revues qu'à la demande.",
        "- Les décisions (modèle par défaut, poids, seuil `min_score`) se reportent dans la spec "
        "avec ce rapport.",
    ]
    if svg_name:
        lines += ["", f"![{CHART_METRIC} par variante]({svg_name})"]
    return "\n".join(lines) + "\n"


def render_svg(result: EvalResult) -> str:
    """Barres de nDCG@10 par variante avec l'intervalle de confiance (graphique simple)."""
    left, row, width, bar_max = 260, 30, 760, 440
    height = 50 + row * len(result.variants)
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{CHART_METRIC} par variante">',
        f"<title>{CHART_METRIC} par variante (intervalle à 95 %)</title>",
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="10" y="20" font-family="sans-serif" font-size="14" fill="#1b1f24">'
        f"{CHART_METRIC} par variante (barre : valeur, trait : intervalle à 95 %)</text>",
    ]
    for index, r in enumerate(result.variants):
        y = 40 + index * row
        value = r.metrics[CHART_METRIC]
        low, high = r.intervals[CHART_METRIC]
        out += [
            f'<text x="10" y="{y + 15}" font-family="sans-serif" font-size="12" fill="#1b1f24">'
            f"{escape(r.name)}</text>",
            f'<rect x="{left}" y="{y}" width="{max(value, 0) * bar_max:.1f}" height="18" '
            'fill="#0b5cad"/>',
            f'<line x1="{left + low * bar_max:.1f}" y1="{y + 9}" x2="{left + high * bar_max:.1f}" '
            f'y2="{y + 9}" stroke="#1b1f24" stroke-width="2"/>',
            f'<text x="{left + bar_max + 10}" y="{y + 14}" font-family="sans-serif" '
            f'font-size="12" fill="#57606a">{_pct(value)}</text>',
        ]
    out.append("</svg>")
    return "\n".join(out) + "\n"
