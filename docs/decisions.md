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
| 20/11/2026 | **Non vérifié** : tout le mapping France Travail (`id`, `intitule`, `entreprise.nom`, `lieuTravail.libelle/codePostal`, `typeContrat`, `natureContrat`, `alternance`, `salaire.libelle`, `dateCreation`, `origineOffre.urlOrigine`) vient de ma connaissance du schéma, pas d'une réponse réelle | API suspendue (accès impossible) ; l'utilisateur a demandé de finir comme possible | Étape 3, dès que l'API répond |
| 20/11/2026 | **Non vérifié** : la fixture `france_travail_search.json` est reconstruite et fictive, pas enregistrée | Idem ; marquée comme telle dans `tests/radar/fixtures/README.md` | Étape 3 |
| 20/11/2026 | **Non vérifié** : codes de contrat `CDI`, `CDD`, `MIS`, `LIB`, `FRA` ; les autres (`SAI`, `CCE`, `REP`, `TTI`, `DIN`, `DDI`) donnent `AUTRE` | Spec : traduction des codes dans la source ; liste de mémoire | Étape 3 |
| 20/11/2026 | L'alternance est détectée par l'indicateur `alternance` ou par `natureContrat` (apprentissage, professionnalisation) et l'emporte sur `typeContrat` | La spec demande de vérifier si c'est un type de contrat ou une indication séparée ; je traite les deux | Étape 3 |
| 20/11/2026 | Filtre `typeContrat` limité à CDI, CDD, MIS, LIB ; STAGE et ALTERNANCE n'ont pas de code et sont filtrés ensuite par `filters.py` | Pas de code connu pour ces types | Étape 4 (filtres) |
| 20/11/2026 | **Non vérifié** : `minCreationDate` et `maxCreationDate` au format `AAAA-MM-JJThh:mm:ssZ`, envoyés ensemble ; fenêtre `max_age_days` bornant `since` | Format de mémoire ; `max_age_days` évite de remonter plus loin que la recherche | Étape 3 |
| 20/11/2026 | Une requête par (mot-clé, département) ; les mots-clés sont des alternatives, dédoublonnées par identifiant | On ignore si `motsCles` combine en ET ou en OU ; un département par requête par prudence | Étape 3 |
| 20/11/2026 | **Non vérifié** : scope par défaut `api_offresdemploiv2 o2dsoffre`, surchargeable par `FT_SCOPE` | La spec renvoie à la doc de l'application ; valeur de mémoire | Étape 3 |
| 20/11/2026 | **Non vérifié** : réponse `204` ou corps vide = aucun résultat, `resultats` = liste ; fin de pagination quand une page a moins de 150 offres | Ne dépend pas du statut `206` ni de `Content-Range`, dont le comportement exact est ignoré | Étape 3 |
| 20/11/2026 | Pages `0-149` à `900-1049`, puis `1000-1149` (chevauche la précédente, doublons éliminés par identifiant) | Respecte début ≤ 1000 et fin ≤ 1149 sans dépasser 1 150 offres | Étape 3 |
| 20/11/2026 | Plafond atteint : fenêtre de dates coupée en deux jusqu'à 1 h ; en dessous, résultats tronqués avec avertissement ; les résultats de la fenêtre plafonnée sont écartés au profit de ses moitiés | La spec demande de découper par département ou fenêtre de dates ; les départements sont déjà séparés | Étape 4 |
| 20/11/2026 | 401 sur une recherche : token renouvelé une fois, puis erreur | Token expiré avant l'heure prévue | Étape 3 |
| 20/11/2026 | URL de repli `https://candidat.francetravail.fr/offres/recherche/detail/{id}` si `urlOrigine` manque (**non vérifié**) | `url` est obligatoire dans `Offer` | Étape 3 |
| 20/11/2026 | Offre brute invalide (id, titre ou date manquant ou invalide) ignorée et comptée dans `skipped`, sans interrompre la source | Une offre défectueuse ne doit pas faire échouer la collecte ; le message ne contient aucun intitulé | Étape 4 (pipeline, `RunReport`) |
| 20/11/2026 | `raw` conserve la réponse brute, `contact` compris | La spec demande le brut pour le débogage ; la base reste privée | Étape 4 (stockage) |
| 19/11/2026 | `Retry-After` plafonné à 60 s, backoff 1 s puis 2 s (plafond 30 s), 3 tentatives | La spec fixe 3 tentatives et le respect de `Retry-After`, pas les bornes | Étape 6 (workflow) |
| 19/11/2026 | Tout statut 4xx/5xx définitif lève `SourceHttpError` avec l'URL masquée ; les erreurs réseau n'exposent pas l'URL | Les messages `httpx` contiennent l'URL, donc d'éventuels secrets en paramètre | Étape 4 (pipeline) |
| 19/11/2026 | Paramètres masqués : `app_key`, `app_id`, `client_secret`, plus `client_id` et `access_token` | La spec en cite trois ; `client_id` est aussi un secret ici | Étape 3 |
| 19/11/2026 | Pause minimale entre requêtes par client (une instance par source), 0,2 s par défaut | La spec dit « par source » sans préciser la portée | Étape 4 (pipeline) |
| 19/11/2026 | Registre : dictionnaire `REGISTRY` de fabriques `Settings -> Source` | Satisfait « un fichier et une entrée, sans toucher au pipeline » | Étape 4 |

## Étape 3 (spec 1.2) : source Adzuna

| Date | Décision | Raison | À confirmer à |
|---|---|---|---|
| 20/11/2026 | Étape 3 = Adzuna (spec 1.2). La source France Travail déjà fusionnée (PR 3) reste dans le code mais n'est pas requise ; elle sera désactivée par défaut dans la configuration YAML | La spec 1.2 la passe en « en attente, livrée désactivée » (étape 9) | Étape 4 (config) |
| 20/11/2026 | Confirmé par la doc officielle **et par une vraie réponse** (recherche « ingénieur ia » à Lyon) : `results`, `count` (total, ici 36 pour 10 résultats), `id`, `title`, `description` (extrait d'environ 500 caractères terminé par « … », sans HTML), `created`, `redirect_url`, `company.display_name`, `location.display_name/area`, `salary_min/max`, `salary_is_predicted` (chaîne `"0"`), `contract_time` | Doc Adzuna et appel réel de l'utilisateur | Étape 3 (fait) |
| 20/11/2026 | Requêtes avec l'en-tête `Accept: application/json` et le paramètre `content-type=application/json` | La doc dit que l'API répond en JSONP sans l'en-tête ; le paramètre est vérifié par un appel réel | Étape 3 (fait) |
| 20/11/2026 | Vérifiés sur appel réel : `what`, `where` en texte libre (« Lyon », « Rhône », « Paris »), `distance` et `max_days_old` (testés avec `where=Lyon&distance=20&max_days_old=3`), `results_per_page` (10 testé), page 1, `content-type`. **Non vérifiés** : `results_per_page=50`, les pages suivantes | Reste à tester avec de vraies clés | Étape 3 |
| 20/11/2026 | Pagination arrêtée quand une page a moins de 50 résultats ou que `page x 50 >= count` (`count` est un total, vérifié) ; plafond de 20 pages par mot-clé et zone avec avertissement (**non vérifié** : limite réelle de l'API) | Évite une boucle infinie | Étape 3 |
| 20/11/2026 | Un département devient `where` = son nom (« Rhône », « Paris », table `geo.py`), **vérifié** par l'utilisateur ; une commune devient `where` = le texte saisi (nom de ville, pas un code INSEE) + `distance` en km, **vérifié** avec `where=Lyon&distance=20` | `where` fonctionne comme une recherche plein texte sur la localisation | Étape 3 (fait pour les départements) |
| 20/11/2026 | Recherche par département : les offres dont le département déduit de `location.area` diffère du département demandé sont écartées ; une offre sans département connu est conservée | `where` étant du plein texte, « Loire » ramène aussi la Haute-Loire et la Loire-Atlantique | Étape 3 (fait) |
| 20/11/2026 | Département d'une offre déduit des éléments de `location.area` qui correspondent à un nom de département | Nécessaire au `dedup_key` ; **vérifié** : `area` = France, région, département, ville (et parfois commune), ex. [France, Auvergne-Rhône-Alpes, Rhône, Lyon, Villeurbanne] | Étape 3 (fait) |
| 20/11/2026 | Salaire `salary_is_predicted` vrai : `salary_min` et `salary_max` ignorés (`None`) | Une estimation d'Adzuna n'est pas un salaire annoncé ; la spec dit de reprendre les montants « tels quels », à arbitrer. La réponse réelle n'a que des valeurs `"0"` : ce cas n'a donc pas été observé | Étape 3 |
| 20/11/2026 | Salaires Adzuna considérés comme annuels en euros, sans conversion | Non documenté ; valeurs réelles (40 000 à 95 000) cohérentes avec un annuel brut | Étape 3 |
| 20/11/2026 | `contract_type` `permanent` = CDI (**observé** sur 1 offre sur 10) et `contract` = CDD (**non observé**) ; un titre explicite (alternance, stage, intérim, freelance) l'emporte | Le champ est absent de 9 offres réelles sur 10 ; un « Stage - … » réel est ainsi détecté par son titre | Étape 3 |
| 20/11/2026 | `description` Adzuna stockée telle quelle (extrait), HTML supprimé ; `postal_code` laissé vide | **Vérifié** : extrait tronqué, pas de code postal dans la réponse | Étape 3 (fait) |
| 20/11/2026 | Département inconnu dans une recherche : `ValueError` sans le code dans le message | Les critères ne doivent pas apparaître dans les logs | Étape 4 (validation de la config) |
| 20/11/2026 | Les bibliothèques `httpx` et `httpcore` sont forcées au niveau WARNING | `httpx` journalise l'URL complète au niveau INFO, donc `app_key` : fuite détectée par un test | Étape 3 (fait) |
| 20/11/2026 | Fixture `adzuna_search.json` = vraie réponse anonymisée (entreprises renommées, identifiants remplacés, `adref` supprimé, `utm_source` neutralisé, descriptions fictives) ; `adzuna_edge_cases.json` reste synthétique | `adref` et `utm_source` identifient l'application ; les entreprises et annonces réelles n'ont pas à être publiées | Étape 3 (fait) |

## Étape 4 : stockage, dédoublonnage, filtres, pipeline, CLI

| Date | Décision | Raison | À confirmer à |
|---|---|---|---|
| 20/11/2026 | Configuration dans `radar/config.py` (modèles pydantic stricts, `extra=forbid`), `SearchQuery` aussi strict | Une faute de frappe (`departement`) ne doit pas être ignorée en silence ; le fichier n'est pas dans l'arborescence de la spec | Étape 4 (relecture de la PR) |
| 20/11/2026 | Messages d'erreur de configuration : chemin et message pydantic seulement, jamais la valeur saisie ; erreur YAML avec ligne et colonne seulement | Les critères réels sont confidentiels et les logs Actions sont publics | Étape 4 |
| 20/11/2026 | Ordre de résolution de la configuration : `--config`, puis secret `VIGIE_RADAR_CONFIG`, puis `config/radar.yaml` | La spec cite le secret et le fichier, pas l'ordre | Étape 8 (workflow) |
| 20/11/2026 | Sources `ats` et `email_alerts` acceptées par la configuration mais ignorées avec un message tant qu'elles ne sont pas dans le registre | Elles arrivent aux étapes 5 et 6 ; l'exemple de la spec les active | Étapes 5 et 6 |
| 20/11/2026 | `france_travail` activée sans identifiants : « en attente d'accès », ignorée, sans erreur (code de sortie inchangé) ; avec identifiants : exécutée | Spec 1.2 ; « identifiants valides » ne peut pas être vérifié sans appel réseau, `check` ne teste que la présence | Étape 9 |
| 20/11/2026 | `--source` ne peut désigner qu'une source activée dans la configuration, sinon code 1 | La spec ne dit pas si `--source` peut forcer une source désactivée | Étape 4 |
| 20/11/2026 | `vigie radar digest` n'est pas encore implémentée ; `run`, `check` et `stats` le sont. Point d'entrée `vigie` (groupe `radar`) défini dans `radar/cli.py` | La commande dépend de `digest.py` (étape 7) ; pas de fichier `cli.py` racine dans l'arborescence | Étape 7 |
| 20/11/2026 | `--dry-run` travaille sur une copie en mémoire de la base (ou une base vide) ; rien n'est écrit sur disque, historique des runs compris | La spec demande « sans écrire en base » mais le dédoublonnage a besoin de l'état existant | Étape 4 |
| 20/11/2026 | `--since` accepte `AAAA-MM-JJ` (minuit UTC) ; sans lui : dernier run réussi (`success`) moins 1 jour, sinon la plus grande ancienneté des recherches | La spec donne la règle de la marge d'un jour ; un run partiel n'est pas « réussi », ce qui recollecte ses sources | Étape 4 |
| 20/11/2026 | Statut d'un run : `success` (aucune source en échec), `partial`, `failed` (toutes en échec) ; une source est en échec si elle lève une exception. Les offres invalides ignorées comptent dans `errors` sans causer d'échec | Codes de sortie 0, 3 et 2 de la spec ; une offre mal formée ne doit pas faire échouer la source | Étape 4 |
| 20/11/2026 | Les logs et la sortie console n'affichent que des compteurs et des noms de variables ; `error_message` (type d'exception et message déjà masqué) reste dans la base, pas dans les logs | Spec section 7 | Étape 8 (logs CI) |
| 20/11/2026 | Chemin de la base : `--db`, sinon `VIGIE_DB_PATH`, sinon `~/.local/share/vigie/radar.db` | La spec donne le défaut et dit « configurable » sans préciser comment | Étape 8 |
| 20/11/2026 | Dédoublonnage entre sources seulement : deux offres d'une même source ayant la même clé restent distinctes ; l'alias n'est pas stocké comme offre (table `offer_aliases` seule) ; l'offre canonique est la première vue | La spec parle d'« une autre source » ; l'alias n'apparaît pas dans le digest | Étape 4 |
| 20/11/2026 | Une offre alias d'une offre filtrée est elle aussi écartée (non réévaluée) | Évite des doublons dans le digest ; une description plus complète d'une autre source n'est pas réexaminée | Étape 6 (alertes email, descriptions vides) |
| 20/11/2026 | Clé de dédoublonnage : département ; à défaut ville normalisée (préfixe département, parenthèses, virgule et numéro d'arrondissement retirés) ; à défaut seulement entreprise et titre. Entreprise absente = chaîne vide | Spec 1.2 ; l'extraction de la ville est un choix (« Villeurbanne, Lyon » donne `villeurbanne`) | Étape 6 (vrais emails) |
| 20/11/2026 | Une offre rencontrée par plusieurs recherches n'est évaluée que par la première (ancienneté et contrat de cette recherche) | Évite de réévaluer ; une offre rejetée par une recherche mais acceptable pour une autre peut être manquée | Étape 4 |
| 20/11/2026 | Filtre de contrat : seules les offres au contrat connu et différent des types demandés sont écartées ; `AUTRE` est conservé | Adzuna ne renseigne le contrat que dans 1 offre sur 10 | Étape 4 |
| 20/11/2026 | Exclusions de titre et mots requis : mots entiers sur texte normalisé (minuscules, sans accents, sans ponctuation), termes de plusieurs mots acceptés ; une entreprise exclue l'est si son nom contient le terme comme mots entiers (« Infotel » exclut « Infotel Corp. ») | La spec dit « mots entiers » pour les titres seulement | Étape 4 |
| 20/11/2026 | Motifs d'exclusion stockés : `age`, `contract`, `title:<terme>`, `company:<terme>`, `require_any` | `filtered_reason` doit servir à ajuster les règles | Étape 4 |
| 20/11/2026 | Offre stockée avec `raw` en JSON, `published_at` et `fetched_at` en ISO UTC, `suspicious` en entier ; table `processed_emails` créée dès la migration 1 mais sans méthode | La table fait partie de la section 11 ; son usage est à l'étape 6 | Étape 6 |
| 20/11/2026 | Pas de lecture de variable d'environnement hors `settings.py` : `VIGIE_DB_PATH` y est ajoutée | Règle 1 de la section 7 | Étape 4 |

## Étape 5 : sources ATS

| Date | Décision | Raison | À confirmer à |
|---|---|---|---|
| 20/11/2026 | **Vérifié sur de vraies API publiques** (un appel par plateforme, sans clé) : Greenhouse, Lever, Ashby, SmartRecruiters, Recruitee. Un test jetable des adaptateurs sur ces réponses réelles a donné 0 offre ignorée | La spec demande de vérifier chaque endpoint avant implémentation ; ces API sont publiques | Étape 5 (fait) |
| 20/11/2026 | Fixtures `ats_*.json` : structure réelle (champs, types, formats de dates, entités HTML), valeurs fictives ; aucune donnée d'entreprise réelle n'est committée | Les offres réelles désignent des entreprises ; seule la structure compte pour les tests | Étape 5 (fait) |
| 20/11/2026 | **Workable non implémenté** : le plateforme reste acceptée par la configuration, mais ses entreprises sont comptées en erreur non fatale et `check` signale leur nombre | Deux appels à l'endpoint du widget ont reçu une limitation Cloudflare (429, code 1015) ; sa structure n'a donc pas pu être vérifiée | Étape 5, à reprendre |
| 20/11/2026 | `Offer.source` = `ats:<plateforme>` ; `company` = le nom saisi dans la configuration, pas celui de l'API | Spec section 5 ; le nom configuré est celui que l'utilisateur connaît | Étape 5 |
| 20/11/2026 | Le registre prend désormais `(Settings, RadarConfig)` : la source ATS a besoin de la liste d'entreprises | Les fabriques ne recevaient que `Settings` | Étape 5 (fait) |
| 20/11/2026 | Chaque entreprise n'est interrogée qu'une fois par exécution (cache en mémoire) ; les offres ATS ignorent la zone et les mots-clés de la recherche, qui sont appliqués par `filters.py` | Les API ATS ne cherchent pas ; évite d'appeler N fois la même entreprise pour N recherches | Étape 5 |
| 20/11/2026 | `since` n'est pas utilisé par les sources ATS : toutes les offres ouvertes sont renvoyées, l'ancienneté étant filtrée ensuite sur `published_at` | Une entreprise ajoutée à la configuration doit voir ses offres existantes, pas seulement les nouvelles | Étape 5 |
| 20/11/2026 | Éligibilité géographique : en France (code pays FR, mot « France », ou une des ~60 grandes villes) ou en télétravail compatible (indicateur ou mot « remote »/« télétravail », sans code pays hors Europe, et soit « remote » seul, soit une zone européenne nommée) | La spec dit « France ou télétravail » ; les offres « Remote - US » de la réponse réelle d'Ashby seraient inutilisables pour un candidat en France | Étape 5 (à relire sur tes entreprises) |
| 20/11/2026 | Limite connue : une ville française absente de la liste et sans mention de « France » ni de code pays n'est pas reconnue (offre manquée) | Liste volontairement courte et non exhaustive | Étape 5 (à relire) |
| 20/11/2026 | Greenhouse : pas de contrat, de salaire ni de télétravail dans l'API ; contrat déduit du titre seulement, télétravail déduit du texte | Constaté sur la réponse réelle | Étape 5 (fait) |
| 20/11/2026 | Lever : `Full-time` n'est pas un CDI (reste `AUTRE`) ; `Internship` donne `STAGE`, `Fixed-Term` donne `CDD` ; `workplaceType` remote/hybrid/onsite donne le télétravail | Valeurs observées : Full-time, Internship, Scholarship, Fixed-Term | Étape 5 |
| 20/11/2026 | Ashby : seules les offres `isListed` sont gardées ; `Intern` donne `STAGE` ; seuls `FullTime` et `Remote` ont été observés ; la rémunération (`includeCompensation`) n'est pas utilisée | Autres valeurs non observées | Étape 5 |
| 20/11/2026 | SmartRecruiters : liste filtrée sur `country=fr` (les offres en télétravail rattachées à un autre pays ne sont pas récupérées) ; description via un appel de détail par offre retenue, 50 au plus par exécution ; détail en échec : description vide ; URL publique reconstruite si besoin ; un identifiant inconnu renvoie 200 et une liste vide, non signalable | Constaté sur les réponses réelles ; limite le nombre d'appels | Étape 5 |
| 20/11/2026 | SmartRecruiters : `permanent` donne `CDI`, `contract` donne `CDD` ; un titre explicite l'emporte (« Apprenti(e) » est `contract` chez SmartRecruiters) | Valeurs observées sur des offres réelles | Étape 5 (fait) |
| 20/11/2026 | Recruitee : seules les offres `published` ; éligible si un des lieux est en France ; contrats `fulltime_permanent` donne `CDI`, `fulltime_fixed_term` donne `CDD` ; la conversion du salaire (`year`, `month`, devise EUR) n'a jamais été vue sur des données réelles | Aucune offre réelle observée n'avait de salaire | Étape 5 |
| 20/11/2026 | Erreur sur une entreprise (404, 5xx) : comptée dans `errors`, non fatale ; la source échoue seulement si toutes les entreprises échouent ; seuls des compteurs sont journalisés (jamais de nom d'entreprise ni de token) | Spec : 404 signalée sans faire échouer la source ; les entreprises ciblées sont confidentielles | Étape 5 |
| 20/11/2026 | Titre « apprenti(e) » reconnu comme alternance (`contract_from_title` partagé avec Adzuna) | Découvert sur une offre réelle SmartRecruiters classée `contract` | Étape 5 (fait) |

## Étape 6 : alertes email

| Date | Décision | Raison | À confirmer à |
|---|---|---|---|
| 20/11/2026 | **Aucun email réel disponible** : les parseurs sont une heuristique générique fondée sur les liens d'offres de chaque expéditeur ; **tous les motifs de liens sont non vérifiés** (France Travail `candidat.francetravail.fr/offres/recherche/detail/<id>`, APEC `apec.fr/candidat/recherche-emploi.html/emploi/detail-offre/<id>`, WTTJ `welcometothejungle.com/<fr|en>/companies/<entreprise>/jobs/<id>`, LinkedIn `linkedin.com/[comm/]jobs/view/[<slug>-]<id>`) | L'utilisateur vient de s'abonner aux alertes ; la spec demande de vérifier sur de vrais emails. Si les vrais liens passent par un domaine de suivi, aucun lien ne sera reconnu : l'email sera signalé, sans offre (comportement prévu par la spec) | Étape 6, dès les premiers vrais emails |
| 20/11/2026 | Les fixtures `.eml` sont synthétiques (structure supposée, noms et liens fictifs) et signalées comme telles | Même raison ; à remplacer par de vrais emails anonymisés | Étape 6 |
| 20/11/2026 | Lecture du texte : le plus grand bloc parent (4 niveaux au plus) qui ne contient que les liens d'une offre ; dans un email « à plat », le lien et ses 4 frères suivants. Première ligne après le titre = entreprise, deuxième = lieu, le reste = description ; WTTJ : entreprise tirée de l'adresse du lien (`exemple-ia` donne `exemple ia`) | Ordre supposé, non vérifié ; l'entreprise et le lieu seront probablement faux sur certains formats réels | Étape 6 |
| 20/11/2026 | Texte de lien générique (« Voir l'offre », « Postuler », « See job »…) ignoré pour le titre, qui se rabat sur la première ligne utile du bloc ; offre sans titre utilisable écartée | Évite des titres du type « Voir l'offre » | Étape 6 |
| 20/11/2026 | Lecture IMAP : `select(readonly=True)`, `BODY.PEEK`, seules les commandes `SEARCH` et `FETCH` (vérifié par un faux serveur dans les tests), connexion SSL sur le port 993 fixe, nom de dossier mis entre guillemets ; pas de variable `IMAP_PORT` | La spec impose la lecture seule et fournit quatre variables ; le port n'est pas précisé | Étape 6 (test avec ta vraie boîte) |
| 20/11/2026 | Les en-têtes (From, Message-ID, Date) sont lus d'abord ; le corps n'est téléchargé que pour un expéditeur autorisé et un message non traité, 300 au plus par exécution (le reste attend la suivante) | Ne pas lire le contenu du courrier sans rapport ; borner le volume | Étape 6 |
| 20/11/2026 | Recherche IMAP `SINCE` (précision d'un jour) avec la date `since` du pipeline ; message sans `Message-ID` : identifiant synthétique stable (hash de l'expéditeur et de la date) | `since` = dernier run réussi moins 1 jour, sinon la plus grande ancienneté des recherches | Étape 6 |
| 20/11/2026 | Liste blanche : `@domaine` accepte le domaine et ses sous-domaines (pas `x-domaine`), une adresse complète doit être identique ; l'en-tête From n'est pas authentifié (DKIM, `Authentication-Results` non vérifiés) : c'est un filtre, pas une garantie | Un expéditeur falsifié reste possible ; le contenu est de toute façon traité comme non fiable | Étape 6 |
| 20/11/2026 | Un message est marqué « traité » seulement après la remise de ses offres au pipeline ; un message sans offre extraite n'est pas marqué (relu à chaque exécution tant qu'il est dans la fenêtre `SINCE`), compte une erreur et déclenche un avertissement (expéditeur et date) sans faire échouer la source | Permet de réexaminer l'email après correction du parseur ; la spec demande l'avertissement et l'erreur | Étape 6 |
| 20/11/2026 | Une offre d'alerte a pour `published_at` la date de l'email ; `raw` ne garde que l'expéditeur (clé), le `Message-ID` et le lien nettoyé, jamais le corps ni l'adresse de l'utilisateur | Le corps contient des données personnelles ; la base reste privée mais autant ne pas les copier | Étape 6 |
| 20/11/2026 | Liens normalisés (fragment et paramètres de suivi retirés : `utm_*`, `trackingId`, `refId`, `lipi`, `token`, `sig`, `ref`, `source`, `campaign`…) avant stockage ; à défaut d'identifiant dans le lien, `source_id` = 16 premiers caractères du SHA-256 du lien normalisé | Spec : « hash du lien normalisé (sans paramètres de suivi) » ; les jetons de suivi peuvent identifier l'utilisateur | Étape 6 |
| 20/11/2026 | Texte caché supprimé avant lecture (`display:none`, `visibility:hidden`, `font-size:0`, `opacity:0`, `max-height:0`, `mso-hide`, attribut `hidden`, scripts, styles) ; le texte visible est conservé tel quel et signalé par `suspicious` s'il contient une injection | Le texte caché est un vecteur classique d'injection de prompt dans les emails | Étape 6 |
| 20/11/2026 | Seule la partie HTML est lue ; un email en texte seul ne donne aucune offre (donc un avertissement) | Pas d'extraction de liens dans du texte brut sans exemple réel | Étape 6 |
| 20/11/2026 | Les offres ne dépendent pas de la recherche : la boîte n'est lue qu'une fois par exécution ; `contract_type` déduit du titre seulement ; département de repli du dédoublonnage = ville du lieu | Les alertes n'ont pas de contrat structuré ni de département | Étape 6 |
| 20/11/2026 | Erreur IMAP (connexion, authentification, dossier, lecture) : `MailboxError` au message fixe, sans hôte, identifiant ni texte serveur, cause d'origine masquée ; la source échoue (code 3 ou 2) | Spec : aucun secret dans les logs ni les exceptions | Étape 6 |
| 20/11/2026 | `email_alerts` activée exige `IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD`, `IMAP_FOLDER` (code 1 sinon) ; `IMAP_USER` est un `SecretStr` ; l'exemple de configuration la garde à `true`, à passer à `false` sans boîte configurée | Spec section 7 | Étape 6 |
| 20/11/2026 | Les clés de `allowed_senders` doivent être `france_travail`, `apec`, `wttj` ou `linkedin` (un parseur par expéditeur), sinon erreur de configuration | Une clé inconnue ne servirait à rien | Étape 6 |
| 20/11/2026 | Nouveau protocole `StoreAware` : le pipeline passe le stockage aux sources qui en ont besoin (`attach_store`) ; dépendance ajoutée : `beautifulsoup4` (analyseur `html.parser`) | La table `processed_emails` appartient au stockage ; `beautifulsoup4` est dans la stack de la spec | Étape 6 (fait) |
