# Décisions prises sans précision de la spec

Décisions d'implémentation de `radar` que `spec_radar.md` ne tranche pas. Une ligne par décision : date, décision, raison et l'étape où elle doit être confirmée. À corriger si elles ne conviennent pas.

## Étape 1 : squelette

| Date | Décision | Raison | À confirmer à |
|---||---|---|---|
| 19/11/2026 | Secrets absents = `None`, avec `Settings.require(features)` qui échoue en nommant les variables | La spec interdit une valeur par défaut, mais M2 n'exige pas les secrets M1 ; la validation se fait par source activée | Étape 4 (CLI `check`) |
| 19/11/2026 | Table `REQUIRED_BY_FEATURE` : `france_travail`, `adzuna`, `email`, `state_s3` | Relie chaque variable de la section 7 à la fonctionnalité qui en dépend | Étape 4 |
| 19/11/2026 | `VIGIE_RADAR_CONFIG` typé `SecretStr`, `VIGIE_STATE_BUCKET` en `str` | Les critères réels sont confidentiels ; le nom du bucket n'est pas un secret | Étape 6 |
| 19/11/2026 | `.python-version` à 3.12 et `requires-python >=3.12` | La spec fixe Python 3.12 | Étape 1 (fait) |
| 19/11/2026 | `mypy --strict` lancé sur tout `src/vigie`, pas seulement `radar` | Inclut `settings.py` ; plus strict que la spec, sans coût | Étape 4 |
| 19/11/2026 | Règles ruff `E, F, I, B, UP, S` | La spec dit « ruff » sans préciser les règles ; `S` pour la sécurité | Étape 4 |
| 19/11/2026 | Backend de build `hatchling`, bornes minimales de dépendances | Choix courant avec `uv` et le layout `src/` | Étape 1 (fait) |
| 19/11/2026 | CI : `setup-uv` épinglé par SHA, `uv sync --locked`, pas de `setup-python` | Spec : actions épinglées par SHA ; `uv` installe lui-même Python | Étape 4 (première CI réelle) |
| 19/11/2026 | Révisions `gitleaks` et `ruff-pre-commit` choisies de mémoire, hook gitleaks non exécuté | Pas de binaire disponible dans l'environnement de travail | Étape 4 |
| 19/11/2026 | `SECURITY.md` et activation du *Secret scanning* repoussés | Hors de la liste de l'étape 1 | Avant le lot M2 |
| 19/11/2026 | Variables `FRANCE_TRAVAIL_*` renommées `FT_*` | Alignement sur la section 7 | Étape 1 (fait) |

## Étape 2 : modèles

| Date | Décision | Raison | À confirmer à |
|---||---|---|---|
| 19/11/2026 | `uid`, `dedup_key` et `suspicious` sont des champs calculés, non saisis | Toujours cohérents avec `source`, `source_id`, `company`, `title`, `department` | Étape 4 (stockage SQLite) |
| 19/11/2026 | Datetime naïve refusée, datetime avec fuseau convertie en UTC | La spec exige de l'UTC ; refuser évite de deviner un fuseau | Étape 3 (dates France Travail) |
| 19/11/2026 | `salary_min` doit être ≤ `salary_max` | Garde-fou contre un mapping erroné | Étape 3 |
| 19/11/2026 | Titre vide refusé après normalisation des espaces | « Obligatoire » dans la spec | Étape 3 |
| 19/11/2026 | `description` vaut `""` par défaut, `contract_type` vaut `AUTRE` par défaut | Une offre sans description ou sans contrat reste exploitable | Étape 3 |
| 19/11/2026 | `SearchQuery` : départements ou commune + rayon, jamais les deux ; une commune exige `distance_km` et inversement | Reflète « départements, ou commune + rayon » ; évite une requête ambiguë | Étape 4 (chargement YAML) |
| 19/11/2026 | `max_age_days` vaut 7 par défaut et doit être > 0 | Valeur de l'exemple YAML de la spec | Étape 4 |
| 19/11/2026 | `RunReport` : `run_id` texte, `finished_at` facultatif, rapport par source (`SourceReport`) | Champs repris de la table `run_sources` de la section 11 | Étape 4 |
| 19/11/2026 | Au moins un mot-clé obligatoire pour toute recherche ; zone facultative avec avertissement journalisé | Validé, reporté en spec 1.1 | Étape 4 (chargement YAML) |
| 19/11/2026 | L'avertissement ne contient ni nom de recherche ni mot-clé | Les logs Actions sont publics (section 7) | Étape 6 (workflow) |

## Étape 2 : normalisation

| Date | Décision | Raison | À confirmer à |
|---||---|---|---|
| 19/11/2026 | HTML : balises supprimées, `script` et `style` ignorés, retours à la ligne aux balises de bloc | Texte lisible sans interpréter le contenu | Étape 3 (vraies descriptions) |
| 19/11/2026 | Les caractères invisibles sont conservés dans la description | Ils servent à repérer les textes suspects ; `match` décidera du nettoyage | Étape 5 / module `match` |
| 19/11/2026 | Mention de genre retirée de la clé : `H/F`, `F/H`, `H/F/X`, avec ou sans parenthèses | La spec cite `H/F`, `F/H`, `(H/F)` ; `X` ajouté par prudence | Étape 4 (dédoublonnage) |
| 19/11/2026 | Clé de dédoublonnage : entreprise ou département absents comptent pour une chaîne vide | La spec ne dit rien des champs manquants | Étape 4 |
| 19/11/2026 | Contrat reconnu par libellé texte ; ordre alternance, stage, freelance, intérim, CDI, CDD ; « libéral » et « portage » = `FREELANCE` ; `MIS` = `INTERIM` | La spec ne donne pas de table ; l'ordre évite qu'« apprentissage en CDD » devienne `CDD` | Étape 3 (codes réels France Travail) |
| 19/11/2026 | Salaire : formats « Annuel de… » et « Mensuel de… » seulement ; horaire et texte libre donnent `None` | Ce sont les formats cités par la spec | Étape 3 |
| 19/11/2026 | Salaire mensuel multiplié par N si « sur N mois » (1 à 24, décimaux acceptés), sinon par 12 ; N hors bornes donne `None` | Demande du 19 nov. ; les bornes évitent un montant aberrant | Étape 3 |
| 19/11/2026 | Un « sur N mois » sur un salaire annuel est ignoré | Le montant annuel est déjà total | Étape 3 |
| 19/11/2026 | Salaire à valeur unique : `salary_min` = `salary_max` | Pas de fourchette dans le libellé | Étape 3 |
| 19/11/2026 | Département : le code postal prime sur le préfixe du libellé | Plus fiable qu'un texte libre | Étape 3 |
| 19/11/2026 | Département : DOM sur 3 chiffres (97x, 98x) ; Corse `2A` pour 20000 à 20199, `2B` à partir de 20200 | Conventions INSEE courantes ; frontière à vérifier | Étape 3 |
| 19/11/2026 | Télétravail déduit seulement si `remote` n'est pas fourni explicitement | Une valeur issue d'une API prime sur une heuristique | Étape 3 |
| 19/11/2026 | Télétravail : FULL (« 100 % télétravail/remote », « télétravail complet », « fully remote », « télétravail 100 % », « full remote »), PARTIAL (« télétravail partiel », « hybride », « N jours de télétravail »), NONE (« pas de », « sans », « aucun télétravail », « télétravail impossible ») | Règles simples demandées le 19 nov. ; élargies à quelques variantes évidentes | Étape 3 |
| 19/11/2026 | Télétravail : signaux contradictoires donnent `PARTIAL` (spec 1.1) ; « pas/sans/non télétravail complet » ne compte pas comme FULL ni NONE et donne `UNKNOWN` | La spec fixe `PARTIAL` en cas de conflit ; la négation de « complet » n'est pas prévue | Étape 4 |
| 19/11/2026 | Texte suspect : caractères invisibles (espaces de largeur nulle, contrôles bidirectionnels, trait d'union conditionnel) et quelques motifs d'injection FR/EN | La spec dit « du type » sans fixer la liste ; heuristique de signalement seulement | Module `match` |

## Étape 3 : sources

| Date | Décision | Raison | À confirmer à |
|---|---|---|---|
| 19/11/2026 | Le mapping France Travail vers `Offer`, les formats de dates, les codes de contrat et le statut de la pagination ne sont pas implémentés | Documentation officielle inaccessible (page rendue en JavaScript) et aucune vraie réponse enregistrée ; la spec demande de s'arrêter plutôt que de deviner | Étape 3 |
| 19/11/2026 | `Retry-After` plafonné à 60 s, backoff 1 s puis 2 s (plafond 30 s), 3 tentatives | La spec fixe 3 tentatives et le respect de `Retry-After`, pas les bornes | Étape 6 (workflow) |
| 19/11/2026 | Tout statut 4xx/5xx définitif lève `SourceHttpError` avec l'URL masquée ; les erreurs réseau n'exposent pas l'URL | Les messages `httpx` contiennent l'URL, donc d'éventuels secrets en paramètre | Étape 4 (pipeline) |
| 19/11/2026 | Paramètres masqués : `app_key`, `app_id`, `client_secret`, plus `client_id` et `access_token` | La spec en cite trois ; `client_id` est aussi un secret ici | Étape 3 |
| 19/11/2026 | Pause minimale entre requêtes par client (une instance par source), 0,2 s par défaut | La spec dit « par source » sans préciser la portée | Étape 4 (pipeline) |
| 19/11/2026 | Registre : dictionnaire `REGISTRY` de fabriques `Settings -> Source` | Satisfait « un fichier et une entrée, sans toucher au pipeline » | Étape 4 |
