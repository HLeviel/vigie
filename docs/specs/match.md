# Spécification : module `vigie/match`

> Version 1.1, 25 novembre 2026 (voir le journal des modifications). Document destiné à l'implémentation par Claude Code. Emplacement : `docs/specs/match.md`.
> Prérequis : `docs/specs/radar.md` (1.5). Les règles transverses de `radar` s'appliquent ici sauf mention contraire : secrets (section 7), confidentialité, fixtures fictives, décisions hors spec (section 18).
> En cas d'ambiguïté, ou si un modèle ou une API ne se comporte pas comme décrit, **s'arrêter et poser la question** plutôt que deviner.

## 1. Objectif

`match` attribue à chaque offre retenue par `radar` un **score d'adéquation** avec le profil de l'utilisateur, **explique** ce score (compétences trouvées, compétences manquantes, points de vigilance) et permet au digest de présenter les offres **de la plus pertinente à la moins pertinente**.

Le module a un second objectif, propre au portfolio : **mesurer** la qualité du classement. Chaque étage du scoring (règles, embeddings, LLM) doit prouver, chiffres à l'appui, qu'il améliore le classement par rapport à l'étage précédent.

Principes :
- **Scoring en étages, du moins cher au plus cher** : règles, puis embeddings, puis LLM sur les meilleures offres seulement.
- **Pas d'étage sans mesure** : un étage n'est activé par défaut que si l'évaluation montre un gain.
- **Explicable** : chaque score est accompagné de ses raisons, stockées et affichées.
- **Le texte des offres est une entrée non fiable** : il ne doit jamais pouvoir modifier le comportement du LLM.
- **Le profil et les offres réelles restent privés** : seules des données fictives et des **métriques agrégées** sont publiées.

## 2. Périmètre

| Lot | Contenu |
|---|---|
| M1 | Profil structuré, embeddings (deux fournisseurs), score hybride explicable, stockage, CLI, intégration au digest |
| M2 | Évaluation : annotation, jeu de données privé, métriques, comparaison des variantes, rapport publiable |
| M3 | Étage LLM : réévaluation des meilleures offres avec explication structurée, protégée contre l'injection, mesurée par l'évaluation de M2 |

Hors périmètre :
- Dédoublonnage sémantique des offres (piste pour plus tard, réutilisera les embeddings).
- Génération de CV ou de lettre (module `cv`).
- Récupération de la description complète d'une offre sur le site d'origine : interdite (section « Exclus » de `radar`). `match` travaille avec le texte que `radar` a stocké, même partiel.

## 3. Profil

### Fichier `profile.yaml`
Source unique du profil, réutilisée plus tard par le module `cv`. Le vrai fichier vit **uniquement dans `vigie-run`** (`profile/profile.yaml`) ; le dépôt public contient `config/profile.example.yaml`, **entièrement fictif**.

```yaml
summary: >                      # 3 à 6 lignes : positionnement, années d'expérience, domaines
  Ingénieur data et IA, 10 ans d'expérience...
target_roles: ["AI engineer", "ML engineer", "data engineer"]

skills:
  must_have: [python, rag, aws]          # compétences maîtrisées et revendiquées
  nice_to_have: [langchain, embeddings]  # en cours d'acquisition
  avoid: [sap, cobol]                    # technologies non souhaitées

experiences:                    # du plus récent au plus ancien
  - title: "Co-fondateur, CTO"
    company: "Exemple SAS"
    start: 2021-01
    end: null                   # null = en cours
    description: >
      Plateforme de mise en relation par vidéo et IA...
    skills: [python, aws, rag]

preferences:
  remote: [FULL, PARTIAL]       # valeurs de Offer.remote acceptées
  contract_types: [CDI, FREELANCE]
  min_salary: 55000             # annuel brut en euros ; ignoré si l'offre n'a pas de salaire
  languages: [fr, en]
```
- Schéma strict (pydantic, `extra=forbid`) ; mêmes règles d'erreur que la configuration de `radar` (jamais la valeur saisie dans un message).
- Compétences normalisées (minuscules, sans accents) ; une table de **synonymes** versionnée dans le code (`ml` = `machine learning`, `llm` = `large language model`, `aws` = `amazon web services`…) sert à la correspondance lexicale.
- `profile_hash` = SHA-256 du contenu normalisé : il identifie la version du profil utilisée pour un score. Un changement de profil invalide les scores et les embeddings du profil, pas ceux des offres.

### Option PDF
Non incluse en M1 : le profil se saisit en YAML. L'extraction d'un CV PDF vers `profile.yaml` est une piste du module `cv`.

## 4. Scoring

### Vue d'ensemble
```
offre retenue par radar
  → étage 0 : préférences (éliminatoires ou pénalités)
  → étage 1 : score hybride = sémantique (embeddings) + lexical (compétences)
  → étage 2 (M3) : LLM sur les N meilleures offres
  → score final 0-100 + raisons
```

### Étage 0 : préférences
Appliqué aux offres non filtrées par `radar`. Chaque règle donne soit une exclusion (score 0, raison stockée), soit une pénalité configurable :
- contrat connu absent de `preferences.contract_types` : exclusion ;
- télétravail connu absent de `preferences.remote` : pénalité (`UNKNOWN` jamais pénalisé) ;
- salaire maximum connu inférieur à `min_salary` : pénalité (salaire inconnu jamais pénalisé) ;
- compétence de `skills.avoid` dans le titre : pénalité.
Raisons stockées sous forme de codes : `pref:contract`, `pref:remote`, `pref:salary`, `pref:avoid:<compétence>`.

### Étage 1 : score hybride
- **Texte de l'offre** pour les embeddings : `titre + entreprise + description`, nettoyé (caractères invisibles retirés, espaces normalisés), tronqué à la limite du modèle. La longueur de la description est conservée comme indicateur de **confiance**.
- **Textes du profil** : `summary`, chaque expérience (titre + description + compétences), et une ligne par rôle cible. Chacun a son embedding.
- **Score sémantique** : similarité cosinus entre l'offre et chaque texte du profil ; score = moyenne des `k` meilleures similarités (`k` = 3 par défaut), ramenée sur 0-100 par une normalisation **fixée sur le jeu d'évaluation** (pas de normalisation par lot, pour que le score d'une offre ne dépende pas des autres offres du jour).
- **Score lexical** : couverture des compétences du profil retrouvées dans l'offre (mots entiers, synonymes compris), pondérée `must_have` > `nice_to_have`.
- **Score hybride** = `w_sem × sémantique + w_lex × lexical − pénalités`, borné à 0-100. Poids par défaut fixés après l'évaluation (M2) ; valeurs initiales : `w_sem = 0,7`, `w_lex = 0,3`.
- **Explication** : compétences trouvées, compétences `must_have` absentes, texte du profil le plus proche, pénalités appliquées, niveau de confiance (`faible` si la description fait moins de 300 caractères).

### Étage 2 : LLM (M3)
- Uniquement sur les `N` offres au meilleur score hybride de l'exécution (`N` = 10 par défaut) et dont la confiance n'est pas `faible`.
- Sortie **structurée et validée** (schéma pydantic) : `score` (0-100), `recommendation` (`postuler`, `à voir`, `passer`), `strengths` (3 au plus), `gaps` (3 au plus), `risks` (3 au plus), chacun en une phrase.
- Une réponse invalide est rejetée (une nouvelle tentative au plus) ; l'offre garde alors son score hybride.
- Score final = score LLM si disponible, sinon score hybride. Les deux sont stockés.
- Cache : pas de nouvel appel si l'offre, le profil (`profile_hash`), le modèle et la version du prompt n'ont pas changé.
- Budget : nombre maximal d'appels et de jetons par exécution, configurable ; au-delà, l'étage s'arrête proprement et le signale dans le rapport d'exécution.

### Protection contre l'injection (M3)
- Le prompt système contient seules les instructions ; le profil et l'offre sont passés comme **données**, dans des balises délimitées, avec la consigne explicite de ne suivre aucune instruction qu'elles contiendraient.
- Aucun outil, aucune navigation, aucune URL suivie : le LLM ne peut que répondre au schéma.
- Une offre `suspicious` (champ de `radar`) n'est **pas** envoyée au LLM : elle garde son score hybride, avec le marqueur `suspicious`.
- Le texte de l'offre est nettoyé avant envoi (caractères invisibles et bidirectionnels retirés, longueur bornée).
- Tests avec des offres hostiles (fixture `linkedin_hostile.eml` de `radar` et cas dédiés) : la sortie doit rester conforme au schéma et le score ne doit pas être imposé par le texte de l'offre.

## 5. Fournisseurs

Interfaces communes, un fichier par fournisseur, enregistrées dans un registre (même principe que les sources de `radar`) :
```python
class Embedder(Protocol):
    name: str  # ex. "local:<modèle>", "bedrock:<modèle>"
    dim: int

    def embed(self, texts: list[str], kind: Literal["query", "document"]) -> list[list[float]]: ...


class Judge(Protocol):  # M3
    name: str

    def assess(self, profile: Profile, offer: OfferView) -> Assessment: ...
```
- `kind` permet les préfixes ou modes propres à certains modèles (requête / document).
- Les vecteurs sont normalisés (norme 1) avant stockage.

### Embeddings (M1) : deux fournisseurs, pour pouvoir les comparer
| Fournisseur | Exécution | Candidats à évaluer (disponibilité et noms exacts à vérifier) |
|---|---|---|
| `local` | `sentence-transformers` sur CPU, dépendance optionnelle `vigie[local]` | modèles multilingues (français et anglais) de la famille E5 ou BGE, taille small ou base |
| `bedrock` | AWS Bedrock, authentification OIDC existante | modèles d'embedding multilingues proposés par Bedrock dans la région choisie |

- Le modèle par défaut est choisi **par l'évaluation** (M2) ; tant qu'elle n'est pas faite, aucun défaut n'est imposé : le modèle est obligatoire dans la configuration.
- `local` : modèle téléchargé une fois et mis en cache ; dans `vigie-run`, le cache Actions est acceptable ici (un modèle se retélécharge sans perte de données).
- `bedrock` : le rôle IAM de `vigie-run` reçoit `bedrock:InvokeModel` sur les **seuls ARN des modèles utilisés**.

### LLM (M3)
| Fournisseur | Authentification |
|---|---|
| `anthropic` | secret `ANTHROPIC_API_KEY` |
| `bedrock` | OIDC existant, `bedrock:InvokeModel` sur l'ARN du modèle |

- Modèle, température (0 par défaut) et version du prompt dans la configuration ; le prompt est un fichier versionné du dépôt public (il ne contient aucune donnée personnelle).
- **Données envoyées** : profil et offres partent chez le fournisseur choisi. Le README le signale ; vérifier la politique de conservation des données du fournisseur avant activation.

## 6. Configuration

Section `match` ajoutée à `config/radar.yaml` (même fichier, même schéma strict) :
```yaml
match:
  enabled: true
  profile: profile/profile.yaml
  embeddings:
    provider: local            # local ou bedrock
    model: "<id du modèle>"    # obligatoire tant que l'évaluation n'a pas fixé de défaut
  weights: { semantic: 0.7, lexical: 0.3 }
  penalties: { remote: 15, salary: 15, avoid: 20 }
  top_k_profile: 3
  llm:                         # M3
    enabled: false
    provider: anthropic        # anthropic ou bedrock
    model: "<id du modèle>"
    top_n: 10
    max_calls_per_run: 15
  digest:
    min_score: 40              # en dessous : offre comptée, pas listée
```

### Secrets ajoutés
| Variable | Usage | Lot |
|---|---|---|
| `ANTHROPIC_API_KEY` | étage LLM avec le fournisseur `anthropic` | M3 |

Bedrock n'ajoute aucun secret (OIDC). `settings.py` reste le seul lecteur des variables d'environnement ; `REQUIRED_BY_FEATURE` reçoit `llm_anthropic`.

## 7. Stockage

Mêmes base (`radar.db`) et mécanisme de migrations que `radar` ; la base reste synchronisée avec S3 par `radar run`.

Tables ajoutées :
- `embeddings` : `owner` (`offer:<uid>` ou `profile:<hash>:<n>`), `model`, `text_hash`, `dim`, `vector` (BLOB float32), `created_at`. Clé : (`owner`, `model`, `text_hash`). Pas de recalcul si le texte n'a pas changé.
- `scores` : `uid`, `profile_hash`, `stage` (`hybrid` ou `llm`), `model`, `prompt_version`, `score`, `details` (JSON : raisons, compétences, confiance, sortie LLM validée), `created_at`.
- `labels` (M2) : `uid`, `label` (`pertinent`, `peut-être`, `non pertinent`), `labeled_at`, `profile_hash`.

Recherche par similarité : calcul en mémoire (NumPy) sur les offres de l'exécution ; volume attendu : quelques centaines d'offres par jour. Une extension vectorielle de SQLite n'est pas nécessaire en M1.

## 8. Intégration avec `radar`

- `vigie radar run` : quand `match.enabled` est vrai, l'étape de scoring s'insère **entre la collecte et le digest**. Elle ne s'applique qu'aux offres nouvelles, non filtrées, non notifiées.
- Une panne de `match` (fournisseur indisponible, quota, profil invalide) **ne bloque pas le digest** : les offres sont envoyées sans score, triées par date comme aujourd'hui, avec une mention dans le pied du digest. Nouveau code de sortie `6` = scoring en échec, priorité 5 > 4 > 6 > 3 > 2.
- **Digest** quand des scores existent :
  - offres triées par score final décroissant ;
  - pour chaque offre : score, recommandation si étage LLM, une ligne d'explication (2 compétences trouvées au plus, 1 manquante au plus), marqueur de confiance faible ;
  - offres sous `min_score` : seulement comptées (« 12 offres sous le seuil ») ;
  - textes de l'explication échappés comme le reste du digest.
- Le rapport d'exécution (`run_sources` et logs) gagne une ligne `match` : offres scorées, exclues par préférence, appels LLM, échecs. Toujours des compteurs seulement.

## 9. Évaluation (M2)

### Jeu de données
- **Annotation par fichier CSV**, sans environnement de développement : l'échantillon est exporté dans un CSV, annoté dans un tableur (Excel, LibreOffice, Numbers ou Google Sheets), puis réimporté (détail ci-dessous). La commande interactive `vigie match label` reste disponible pour un usage local.
- Échantillon **stratifié** par source et par niveau de score, pour ne pas annoter que les offres déjà bien classées.
- Objectif : **150 offres annotées au moins, dont 30 pertinentes au moins**.
- Le jeu annoté est **privé** (table `labels` de la base). Ni lui ni les CSV ne sont jamais commités dans le dépôt public.
- Découpage fixe et reproductible (graine documentée) : 30 % pour régler les poids et la normalisation, 70 % pour mesurer. Aucun réglage sur la partie de mesure.

### Format du CSV d'annotation
- Fichier `labels-AAAA-MM-JJ.csv`, UTF-8 **avec BOM** et séparateur `;` (ouverture directe dans Excel en français), guillemets selon la RFC 4180, une offre par ligne, **ordre aléatoire**.
- Colonnes : `uid`, `label` (vide), `titre`, `entreprise`, `lieu`, `contrat`, `publiée` (jj/mm/aaaa), `source`, `extrait` (500 caractères au plus, retours à la ligne remplacés par des espaces), `lien`.
- **Aucun score ni rang** dans le fichier : l'annotation ne doit pas être influencée par le classement à évaluer.
- **Protection contre l'injection de formules** : toute cellule issue d'une offre qui commence par `=`, `+`, `-`, `@`, une tabulation ou un retour chariot est préfixée d'une apostrophe. Les caractères invisibles et bidirectionnels sont retirés.
- Valeurs acceptées dans `label`, sans casse ni accents : `p` ou `pertinent` ; `m` ou `peut-etre` ; `n` ou `non` ; cellule vide = non annotée.

### Import
- `vigie match label --import FICHIER` : lit le CSV (avec ou sans BOM, séparateur `;` ou `,` détecté), associe chaque ligne par `uid`, enregistre l'étiquette avec le `profile_hash` courant ; la dernière étiquette d'une offre remplace la précédente.
- Erreurs signalées par **numéro de ligne et motif** (`uid inconnu`, `étiquette invalide`), jamais par contenu ; une ligne en erreur n'empêche pas l'import des autres. Les colonnes autres que `uid` et `label` sont ignorées : modifier un titre dans le tableur n'a aucun effet.
- `--dry-run` : affiche seulement les compteurs (lignes lues, annotées, ignorées, en erreur).
- Résumé final : nombre total d'offres annotées, dont pertinentes, et rappel de l'objectif de la section suivante.

### Workflows d'annotation dans `vigie-run`
Pour annoter sans rien installer, le modèle `examples/vigie-run/` fournit trois workflows (déclenchement manuel ou par dépôt de fichier, même groupe `concurrency` que `radar.yml`, même environnement `radar`, état S3 lu et réécrit comme `radar run`) :
- `match-label-export.yml` (`workflow_dispatch`, entrée `n`, 50 par défaut) : produit le CSV et le dépose en **artefact** du run (conservation 1 jour). Le dépôt étant privé, l'artefact n'est visible que de ses collaborateurs. L'utilisateur le télécharge depuis l'onglet Actions.
- `match-label-import.yml` (déclenché par un push sur `labels/*.csv`, ou à la main) : l'utilisateur dépose le CSV annoté dans `labels/` de `vigie-run` (interface web GitHub, bouton « Add file > Upload files ») ; le workflow importe les étiquettes, renvoie l'état vers S3 et affiche le résumé (compteurs seulement).
- `match-eval.yml` (`workflow_dispatch`) : produit le rapport d'évaluation en artefact ; il ne contient que des agrégats et peut être copié dans `docs/eval/` du dépôt public.

### Variantes comparées
| Variante | Description |
|---|---|
| `baseline:date` | classement actuel du digest (par date) |
| `baseline:lexical` | score lexical seul |
| `baseline:tfidf` | similarité TF-IDF profil/offre, sans embeddings |
| `emb:<fournisseur>:<modèle>` | score sémantique seul, un par modèle candidat (2 à 3 modèles) |
| `hybrid:<modèle>` | score hybride avec le meilleur modèle |
| `hybrid+llm:<modèle>` | M3 : étage LLM sur les N meilleures |

### Métriques
- `precision@5`, `precision@10`, `nDCG@10` (gain : pertinent = 2, peut-être = 1), `recall@20`.
- Pour chaque variante : temps de calcul par offre et coût estimé par exécution.
- Intervalles de confiance par bootstrap (1 000 tirages) sur la partie de mesure : un écart dans l'intervalle n'est pas présenté comme un gain.

### Rapport
- `vigie match eval` produit un rapport Markdown : tableau des métriques par variante, graphique simple (SVG), taille du jeu, date, version du profil (hash tronqué), sans aucun titre d'offre ni nom d'entreprise.
- Le rapport est publiable : `docs/eval/match-YYYY-MM-DD.md` dans le dépôt public, généré dans `vigie-run` puis copié à la main.
- La CI du dépôt public teste le code d'évaluation sur un **petit jeu fictif** (`tests/match/fixtures/eval_fictif.json`), jamais sur des données réelles.

### Décisions tirées de l'évaluation
- Modèle d'embedding par défaut, poids, normalisation, seuil `min_score`.
- Activation par défaut de l'étage LLM seulement si `hybrid+llm` dépasse `hybrid` au-delà de l'intervalle de confiance sur `precision@10` ou `nDCG@10`.
- Ces décisions sont reportées dans cette spec (version suivante), avec le rapport qui les justifie.

## 10. CLI

```
vigie match score   [--config PATH] [--db PATH] [--all] [--dry-run]   # score les offres non scorées (ou toutes)
vigie match explain UID [--config PATH] [--db PATH]                    # détail d'un score (privé : affiche l'offre)
vigie match label   [--config PATH] [--db PATH] [--n 20] [--relabel]   # annotation interactive (M2, usage local)
vigie match label   --export FICHIER [--n 50] [--relabel]              # CSV à annoter (M2)
vigie match label   --import FICHIER [--dry-run]                       # import du CSV annoté (M2)
vigie match eval    [--config PATH] [--db PATH] [--output PATH]        # rapport d'évaluation (M2)
vigie match check   [--config PATH]                                    # profil, modèle, secrets, sans appel réseau
```
- `explain`, `label` et le CSV exporté contiennent des offres réelles : à n'utiliser qu'en local ou dans le dépôt privé, jamais dans le dépôt public.
- `score` est appelé automatiquement par `radar run` (section 8) ; la commande sert au recalcul, par exemple après un changement de profil.

## 11. Arborescence

```
src/vigie/match/
├── profile.py          # schéma, normalisation, synonymes, profile_hash
├── text.py             # préparation des textes offre et profil
├── embedders/          # base.py, local.py, bedrock.py
├── scoring.py          # étages 0 et 1, explication
├── judge/              # M3 : base.py, anthropic.py, bedrock.py, prompt_v1.md
├── store.py            # tables embeddings, scores, labels
├── evaluation/         # M2 : dataset.py, metrics.py, variants.py, report.py
└── cli.py
config/profile.example.yaml
tests/match/
docs/eval/              # rapports d'évaluation publiés (agrégats seulement)
```

## 12. Tests

- Profil : schéma, synonymes, `profile_hash` stable, invalidation au changement.
- Scoring : préférences (exclusions et pénalités, valeurs inconnues jamais pénalisées), couverture lexicale, normalisation fixe, bornes 0-100, explication complète, confiance faible.
- Embedders : faux fournisseur déterministe pour tous les tests ; `local` et `bedrock` testés avec des doubles (aucun téléchargement de modèle ni appel réseau en CI) ; vecteurs normalisés ; cache par `text_hash`.
- Intégration : `radar run` avec `match` activé trie le digest par score ; panne de `match` : digest envoyé quand même et code 6.
- Évaluation : métriques vérifiées sur des cas calculés à la main ; découpage reproductible ; rapport sans titre ni entreprise (test qui le vérifie).
- CSV d'annotation : aller-retour export puis import ; BOM et séparateur ; absence de score ; neutralisation des formules (`=`, `+`, `-`, `@`) ; étiquettes acceptées et refusées ; `uid` inconnu ; colonnes modifiées sans effet ; messages d'erreur sans contenu d'offre.
- Workflows d'annotation du modèle : contrôles statiques (déclencheurs, artefact à conservation courte, `concurrency` partagé, aucun `echo` de contenu).
- LLM (M3) : schéma de sortie, réponse invalide, cache, budget, offres hostiles, offre `suspicious` jamais envoyée.
- Couverture visée : 85 % sur `src/vigie/match`.

## 13. Critères d'acceptation

### M1
- [ ] `vigie match check` valide un profil et la configuration, sans appel réseau.
- [ ] Le digest réel est trié par score, avec une ligne d'explication par offre.
- [ ] Les embeddings d'une offre ne sont jamais recalculés si son texte n'a pas changé.
- [ ] Une panne du fournisseur d'embeddings laisse partir le digest (code 6).
- [ ] Aucune donnée de profil ou d'offre réelle dans le dépôt public.

### M2
- [ ] Annotation possible sans environnement de développement : export, annotation dans un tableur, import, entièrement via les workflows de `vigie-run`.
- [ ] 150 offres annotées au moins, dont 30 pertinentes.
- [ ] Rapport comparant au moins `baseline:date`, `baseline:tfidf`, deux modèles d'embedding et `hybrid`, avec intervalles de confiance.
- [ ] Rapport publié dans `docs/eval/`, sans titre d'offre ni nom d'entreprise.
- [ ] Modèle par défaut, poids et seuil fixés à partir du rapport et reportés dans la spec.

### M3
- [ ] Étage LLM évalué contre `hybrid` ; activé par défaut seulement si le gain dépasse l'intervalle de confiance.
- [ ] Tests d'injection verts ; aucune offre `suspicious` envoyée au LLM.
- [ ] Coût par exécution mesuré et borné par la configuration.

## 14. Ordre d'implémentation suggéré

1. `profile.py`, `config/profile.example.yaml`, section `match` de la configuration, `vigie match check`.
2. `text.py`, interface `Embedder`, faux fournisseur, stockage des embeddings.
3. `scoring.py` (étages 0 et 1) et explication, avec le faux fournisseur.
4. Fournisseur `local`, puis fournisseur `bedrock` (permission IAM documentée dans `examples/vigie-run/`).
5. Intégration dans `radar run` et dans le digest, code de sortie 6.
6. M2 : `label`, jeu de données, métriques, variantes, rapport (fait).
6 bis. M2 : export et import CSV, workflows `match-label-export.yml`, `match-label-import.yml` et `match-eval.yml` dans `examples/vigie-run/` ; premier rapport réel.
7. Mise à jour de la spec avec les choix issus de l'évaluation.
8. M3 : `Judge`, prompt versionné, protections, cache et budget ; évaluation `hybrid+llm`.

Livrer par petites PR, une étape par PR, chacune avec ses tests ; décisions hors spec dans la description de la PR (section 18 de `radar`).

## Journal des modifications

- **1.1 (25/11/2026)** : annotation par fichier CSV (export, tableur, import) et workflows d'annotation et d'évaluation dans `vigie-run`, pour annoter sans environnement de développement ; protection contre l'injection de formules ; aucun score dans le fichier à annoter.
- **1.0 (24/11/2026)** : version initiale.
