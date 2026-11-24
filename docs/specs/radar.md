# Spécification : module `vigie/radar`

> Version 1.5, 24 novembre 2026 (voir le journal des modifications en fin de document). Document destiné à l'implémentation par Claude Code.
> En cas d'ambiguïté ou de contradiction avec la documentation officielle d'une API, **s'arrêter et poser la question** plutôt que deviner.
> Points encore à vérifier : issues GitHub étiquetées `à-vérifier` (référencées ci-dessous par leur numéro).

## 1. Objectif

`radar` collecte chaque jour les nouvelles offres d'emploi correspondant à des critères de recherche, les normalise dans un format unique, élimine les doublons et les offres hors critères, puis produit un **digest** des nouveautés.

`radar` ne note pas les offres : le scoring appartient au module `match`. `radar` doit seulement fournir des offres propres, dédoublonnées et prêtes à être scorées.

Principes :
- **APIs officielles d'abord**, scraping uniquement en option et désactivé par défaut.
- **Aucun LLM** dans la collecte : code déterministe, testable, gratuit.
- **Code public, exécution privée** : le dépôt public `HLeviel/vigie` contient le code ; l'exécution réelle (secrets, critères, CV, état, logs) a lieu dans le dépôt privé `HLeviel/vigie-run` (section 14).
- **Aucun secret ni donnée personnelle dans le dépôt public.**
- Chaque source peut échouer sans faire échouer les autres.
- **Vérifier sur des données réelles avant de s'y fier** ; ce qui ne l'est pas encore est signalé comme tel et suivi par une issue.

## 2. Périmètre

### Inclus (MVP)
| Lot | Contenu | État au 24/11/2026 |
|---|---|---|
| M1 | Modèle `Offer`, sources **Adzuna**, **ATS** et **alertes email**, stockage SQLite, dédoublonnage, filtres, CLI, digest en fichier | implémenté |
| M2 | Envoi du digest par email, versions taguées, modèle du dépôt privé `vigie-run`, état sur S3 privé | implémenté et **vérifié en réel** le 24/11/2026 (`vigie-run` privé, état S3) |

### En attente
- **API France Travail « Offres d'emploi v2 »** : retirée du libre-service sur francetravail.io (accès restreint, sur demande). Source écrite et testée sur fixtures, **livrée désactivée**, non requise. Les offres France Travail arrivent par les **alertes email** (#19).

### Hors MVP (lot M3, ne pas implémenter maintenant)
- Source JobSpy (Indeed France, HelloWork, Google), optionnelle, désactivée par défaut, **jamais LinkedIn**.
- Déploiement AWS Lambda + EventBridge.

Prévoir l'architecture pour que ces sources s'ajoutent sans modifier le cœur.

### Exclus définitivement
- Scraping de l'APEC (interdit par ses CGU, art. 7.4 et 9), de LinkedIn (interdit par ses conditions d'utilisation, section 8.2) et de francetravail.fr (contournerait la restriction de l'API).
- Ouverture automatique des liens contenus dans les alertes email : ce serait du scraping des sites concernés.
- Candidature automatique.

## 3. Stack technique

- Python 3.12 (`requires-python >=3.12`), gestion du projet avec `uv`, layout `src/`, build `hatchling`.
- `httpx`, `tenacity`, `pydantic` v2 et `pydantic-settings`, `typer`, `PyYAML`, `jinja2`, `beautifulsoup4` (analyseur `html.parser`), `boto3` (S3), `tzdata`.
- `imaplib` (lecture seule) et `smtplib` de la bibliothèque standard ; SQLite via `sqlite3`.
- Qualité : `ruff` (règles `E, F, I, B, UP, S`), `mypy --strict` sur tout `src/vigie` (`boto3`/`botocore` exclus, faute de stubs), `pytest`, `respx`, `pytest-cov`.
- **Version** : écrite uniquement dans `pyproject.toml` ; `vigie.__version__` la lit par `importlib.metadata`. Un test refuse toute version écrite en clair dans `src/`. Versionnage sémantique strict (`X.Y.Z`, sans suffixe).

## 4. Arborescence

```
vigie/
├── src/vigie/
│   ├── __init__.py              # __version__ lu depuis les métadonnées du paquet
│   ├── settings.py              # seul lecteur des variables d'environnement (section 7)
│   └── radar/
│       ├── models.py            # Offer, SearchQuery, RunReport
│       ├── config.py            # schéma strict de la configuration YAML
│       ├── normalize.py         # texte, contrat, salaire, lieu, télétravail, suspicion
│       ├── geo.py               # départements (codes et noms)
│       ├── http.py              # client HTTP commun, masquage des secrets
│       ├── sources/
│       │   ├── __init__.py      # registre des sources
│       │   ├── base.py          # protocoles Source et StoreAware
│       │   ├── adzuna.py
│       │   ├── ats/             # base, common, location + un fichier par plateforme
│       │   ├── email_alerts/    # mailbox (IMAP), parsing + un parseur par expéditeur
│       │   └── france_travail.py  # en attente, désactivée
│       ├── dedup.py
│       ├── filters.py
│       ├── store.py             # SQLite, sans dépendance AWS
│       ├── state.py             # synchronisation de la base avec S3
│       ├── digest.py            # rendu HTML / Markdown / texte
│       ├── notify.py            # envoi SMTP
│       ├── pipeline.py
│       └── cli.py               # point d'entrée `vigie`, groupe `radar`
├── templates/digest.html.j2     # inclus dans la roue (vigie/templates)
├── config/radar.example.yaml    # critères et entreprises FICTIFS
├── scripts/                     # check_release_tag.py, check_wheel.py
├── tests/radar/fixtures/        # structures réelles, valeurs fictives (section 15)
├── examples/vigie-run/          # modèle du dépôt privé (section 14)
├── docs/                        # page GitHub Pages
├── SECURITY.md
├── .env.example
├── .pre-commit-config.yaml      # gitleaks, ruff-check, ruff-format
└── .github/workflows/
    ├── ci.yml
    └── release.yml
```

Le dépôt privé `vigie-run` (créé par l'utilisateur à partir de `examples/vigie-run/`, jamais commité dans le dépôt public) :
```
vigie-run/                       # PRIVÉ
├── config/radar.yaml            # vrais critères et vraies entreprises ciblées
├── cv/                          # CV réel (réservé aux modules match et cv)
├── .github/workflows/radar.yml
└── README.md
```

## 5. Modèle de données

### `Offer` (pydantic)
| Champ | Type | Règle |
|---|---|---|
| `uid` | str | **calculé** : `"{source}:{source_id}"`, clé primaire |
| `source` | str | `adzuna`, `ats:greenhouse`, `email:apec`… |
| `source_id` | str | identifiant de l'offre chez la source |
| `title` | str | obligatoire ; espaces normalisés ; refusé s'il est vide après normalisation |
| `company` | str \| None | |
| `location_label` | str \| None | libellé lisible (« 69 - Lyon 3e ») |
| `postal_code` | str \| None | |
| `department` | str \| None | code département (voir règles) |
| `contract_type` | enum | `CDI`, `CDD`, `INTERIM`, `FREELANCE`, `STAGE`, `ALTERNANCE`, `AUTRE` (défaut `AUTRE`) |
| `remote` | enum | `FULL`, `PARTIAL`, `NONE`, `UNKNOWN` |
| `salary_min` / `salary_max` | float \| None | annuel brut en euros ; `salary_min` ≤ `salary_max` |
| `salary_label` | str \| None | texte brut de la source |
| `description` | str | texte brut, HTML supprimé ; `""` par défaut ; peut être partiel |
| `url` | HttpUrl | obligatoire |
| `published_at` / `fetched_at` | datetime | **avec fuseau**, convertie en UTC ; une datetime naïve est refusée |
| `dedup_key` | str | **calculé**, voir section 9 |
| `suspicious` | bool | **calculé**, voir règles |
| `raw` | dict | données brutes utiles au débogage (voir chaque source) |

La description est une **entrée non fiable** : elle sera lue par un LLM dans `match`. `radar` la stocke sans jamais l'interpréter.

### Règles de normalisation (`normalize.py`)
- **HTML** : balises supprimées, `script` et `style` ignorés, retours à la ligne aux balises de bloc. Les caractères invisibles sont **conservés** dans la description (ils servent à la détection ; le nettoyage relève de `match` et du digest).
- **Salaire** :
  - « Annuel de X Euros à Y Euros » : repris tel quel ; un « sur N mois » y est ignoré.
  - « Mensuel de X Euros à Y Euros » : multiplié par N si le libellé dit « sur N mois » (1 à 24, décimales acceptées), sinon par 12 ; N hors bornes donne `None`.
  - Valeur unique : `salary_min` = `salary_max`. Tout autre format (horaire, texte libre) : `None`, le libellé reste dans `salary_label`.
- **Contrat** (libellés texte) : recherche dans l'ordre alternance (dont « apprenti(e) »), stage, freelance (« libéral », « portage » compris), intérim, CDI, CDD, pour qu'« apprentissage en CDD » donne `ALTERNANCE`. Chaque source traduit ses propres codes ; un type explicite dans le titre l'emporte sur un code générique.
- **Département** : le code postal prime sur le préfixe du libellé ; DOM sur 3 chiffres (97x, 98x) ; Corse `2A` de 20000 à 20199, `2B` à partir de 20200.
- **Télétravail** : une valeur fournie par l'API prime ; sinon déduit du titre et de la description, sans casse ni accents :
  - `FULL` : « 100 % télétravail/remote », « télétravail 100 % », « télétravail complet », « full remote », « fully remote » ;
  - `PARTIAL` : « télétravail partiel », « hybride », « N jour(s) de télétravail » ;
  - `NONE` : seulement si explicite (« pas de / sans / aucun télétravail », « télétravail non possible ») ;
  - signaux contradictoires : `PARTIAL` ; négation d'un « télétravail complet » : `UNKNOWN` ; sinon `UNKNOWN`.
- **Suspicion** (`suspicious`) : caractères invisibles (largeur nulle, contrôles bidirectionnels, trait d'union conditionnel) ou motifs d'injection FR/EN. Heuristique de signalement uniquement.

### `SearchQuery`
- Au moins un mot-clé obligatoire.
- Zone : **départements, ou commune + rayon (`distance_km`), jamais les deux** ; une commune exige un rayon et inversement. Zone facultative : une recherche sans zone déclenche un avertissement (sans nom de recherche ni mot-clé).
- `max_age_days` : 7 par défaut, strictement positif.

### `RunReport`
`run_id` texte, `started_at`, `finished_at` facultatif, statut, et un `SourceReport` par source : récupérées, nouvelles, doublons, filtrées, erreurs, durée.

## 6. Sources

### Interface commune
```python
class Source(Protocol):
    name: str

    def fetch(self, query: SearchQuery, since: datetime) -> Iterator[Offer]: ...


class StoreAware(Protocol):  # sources qui ont besoin du stockage (alertes email)
    def attach_store(self, store: Store) -> None: ...
```
- Registre `REGISTRY` : fabriques `(Settings, RadarConfig) -> Source`. Ajouter une source = un fichier et une entrée.
- Une exception dans une source est capturée par le pipeline et n'interrompt pas les autres.
- Une offre brute invalide (identifiant, titre, date ou URL manquants) est ignorée et comptée dans `errors`, sans faire échouer la source ; le message ne contient aucun intitulé.

### Comportement HTTP commun (`http.py`)
- Timeout de 10 s en connexion, 30 s en lecture. User-Agent `vigie-radar/<version> (+https://github.com/HLeviel/vigie)`.
- 3 tentatives sur erreur réseau, 429 et 5xx ; attente = `Retry-After` (secondes ou date HTTP, plafonné à 60 s), sinon 1 s puis 2 s (plafond 30 s). Pas de retry sur les autres 4xx.
- Pause minimale de 0,2 s entre deux requêtes d'un même client (un client par source).
- Échec définitif : `SourceHttpError` avec l'URL **masquée** ; une erreur réseau n'expose pas l'URL. Paramètres masqués : `app_key`, `app_id`, `client_secret`, `client_id`, `access_token`.
- Les journaux de `httpx` et `httpcore` sont forcés au niveau WARNING : au niveau INFO, ils écrivent l'URL complète, donc les clés.

### Adzuna (M1, source principale)
- `GET https://api.adzuna.com/v1/api/jobs/fr/search/{page}` avec `app_id`, `app_key`, `what`, `where`, `distance`, `max_days_old`, `results_per_page=50`, `content-type=application/json` et l'en-tête `Accept: application/json` (sans eux, l'API répond en JSONP).
- Zone : un département devient `where` = son **nom** (« Rhône »), puis les offres dont le département déduit de `location.area` diffère sont écartées (une offre sans département connu est gardée) ; une commune devient `where` = le nom saisi + `distance` en km.
- Pagination : arrêt quand une page a moins de 50 résultats ou que `page × 50 ≥ count` ; plafond de 20 pages par couple mot-clé/zone, avec avertissement (#20).
- Champs vérifiés sur de vraies réponses : `id`, `title`, `description` (extrait d'environ 500 caractères, sans HTML), `created`, `redirect_url`, `company.display_name`, `location.area` (France, région, département, ville), `salary_min`, `salary_max`, `contract_type`. Pas de code postal.
- Salaire : considéré annuel en euros ; **ignoré si `salary_is_predicted`** (estimation d'Adzuna, pas un salaire annoncé).
- Contrat : `permanent` = CDI, `contract` = CDD ; souvent absent (environ 9 offres sur 10), d'où la priorité au type lu dans le titre.

### ATS : pages carrières des entreprises ciblées (M1)
- Plateformes **vérifiées sur de vraies API publiques** : Greenhouse, Lever, Ashby, SmartRecruiters, Recruitee. **Workable** est acceptée par la configuration mais non implémentée : ses entreprises comptent comme erreurs non fatales et `check` les signale (#17).
- `company` = le nom saisi dans la configuration ; `source` = `ats:<plateforme>`.
- Chaque entreprise est interrogée **une seule fois par exécution** (cache en mémoire). Les API ATS ne cherchent pas : zone et mots-clés sont appliqués ensuite par les filtres. `since` n'est pas utilisé : toutes les offres ouvertes sont renvoyées et l'ancienneté est filtrée sur `published_at`.
- **Éligibilité géographique** : offre en France (code pays FR, mot « France » ou une ville de la liste) ou en télétravail compatible (indicateur ou mot « remote »/« télétravail », sans code pays hors Europe, « remote » seul ou zone européenne nommée). La liste de villes est trop courte : #18.
- Particularités :
  - Greenhouse : ni contrat, ni salaire, ni télétravail dans l'API ; tout est déduit du texte.
  - Lever : `Full-time` reste `AUTRE`, `Internship` = `STAGE`, `Fixed-Term` = `CDD` ; `workplaceType` donne le télétravail.
  - Ashby : seules les offres `isListed` ; `Intern` = `STAGE` ; rémunération non utilisée.
  - SmartRecruiters : liste filtrée sur `country=fr` ; description par un appel de détail, 50 au plus par exécution (échec : description vide) ; `permanent` = CDI, `contract` = CDD.
  - Recruitee : seules les offres `published` ; éligible si un des lieux est en France ; `fulltime_permanent` = CDI, `fulltime_fixed_term` = CDD ; conversion du salaire non vérifiée (#21).
- Erreur sur une entreprise (404, 5xx) : comptée, non fatale ; la source échoue seulement si **toutes** les entreprises échouent. Seuls des compteurs sont journalisés, jamais de nom d'entreprise ni de token.

### Alertes email (M1)
- **Boîte** : l'utilisateur fait arriver ses alertes dans un dossier dédié (`IMAP_FOLDER`, par exemple `Vigie/Alertes`).
- **Lecture IMAP strictement en lecture seule** : SSL sur le port 993, `select(readonly=True)`, `BODY.PEEK`, uniquement les commandes `SEARCH` et `FETCH`, nom de dossier entre guillemets. Mot de passe d'application uniquement.
- Recherche `SINCE` (précision d'un jour) sur la date `since` du pipeline. Les en-têtes (From, Message-ID, Date) sont lus d'abord ; le corps n'est téléchargé que pour un expéditeur autorisé et un message non traité, **300 au plus par exécution** (le reste à l'exécution suivante). Message sans `Message-ID` : identifiant stable dérivé de l'expéditeur et de la date.
- **Liste blanche** : `@domaine` accepte le domaine et ses sous-domaines (pas `x-domaine`) ; une adresse complète doit être identique. Clés autorisées : `france_travail`, `apec`, `wttj`, `linkedin`. L'en-tête From n'est pas authentifié : c'est un filtre, pas une garantie.
- **Extraction** : seule la partie HTML est lue (un email en texte seul ne donne aucune offre). Le texte caché est supprimé avant lecture (`display:none`, `visibility:hidden`, `font-size:0`, `opacity:0`, `max-height:0`, `mso-hide`, attribut `hidden`, scripts, styles). Un texte de lien générique (« Voir l'offre », « Postuler »…) ne sert pas de titre.
- **Liens** : jamais ouverts ; normalisés (fragment et paramètres de suivi retirés : `utm_*`, `at_*`, `trackingId`, `refId`, `lipi`, `token`, `sig`, `ref`, `source`, `campaign`…). `source_id` = identifiant extrait du lien, sinon les 16 premiers caractères du SHA-256 du lien normalisé.
- `published_at` = date de l'email ; contrat lu d'abord dans la ligne de contrat de l'alerte, sinon dans le titre ; département de repli = ville du lieu. `raw` ne garde que la clé d'expéditeur, le `Message-ID` et le lien nettoyé, jamais le corps ni l'adresse de l'utilisateur.
- **Suivi** : un message est marqué traité (`processed_emails`) seulement après remise de ses offres au pipeline. Un message dont aucune offre n'est extraite n'est pas marqué, compte une erreur et produit un avertissement (expéditeur et date seulement).
- Erreur IMAP : `MailboxError` au message fixe, sans hôte, identifiant ni texte du serveur ; la source échoue.
- **France Travail : vérifié sur de vrais emails** (expéditeur `nepasrepondre@offre.francetravail.fr`, un bloc `<table>` par offre, lien direct `candidat.francetravail.fr/offres/recherche/detail/<id>`). Titre = texte du lien ; entreprise = premier `<span>` (parfois absente) ; lieu = dernier `<span>` ; puis ligne de contrat et temps de travail ; « Offre n°… » et la mention du partenaire sont retirés de la description.
- **APEC, WTTJ et LinkedIn : non vérifiés** (heuristique générique, motifs de liens supposés) : #16.

### France Travail API (en attente, livrée désactivée)
- Activée sans identifiants : « en attente d'accès », ignorée sans erreur ; avec identifiants : exécutée.
- OAuth2 *client credentials* (token en mémoire uniquement, renouvelé 60 s avant expiration, et une fois sur un 401) ; scope par défaut `api_offresdemploiv2 o2dsoffre`, surchargeable par `FT_SCOPE`.
- Une requête par couple mot-clé/département ; pages `0-149` à `900-1049` puis `1000-1149` ; plafond atteint : fenêtre de dates coupée en deux jusqu'à 1 h.
- **Mapping, codes de contrat, format des dates et fixture : non vérifiés** (#19).

## 7. Gestion des secrets et des clés d'API

### Secrets
| Variable | Usage | Lot |
|---|---|---|
| `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | Adzuna | M1 |
| `IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD`, `IMAP_FOLDER` | alertes email (mot de passe d'application) | M1 |
| `FT_CLIENT_ID`, `FT_CLIENT_SECRET` | France Travail | en attente |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `DIGEST_TO` | envoi du digest | M2 |
| `VIGIE_STATE_BUCKET` | bucket S3 privé de l'état | M2 |

Variables non secrètes : `VIGIE_DB_PATH`, `FT_SCOPE` ; dans `vigie-run`, `AWS_ROLE_ARN` et `AWS_REGION` en variables d'environnement GitHub.

### Règles dans le code
1. **Un seul lecteur** : `src/vigie/settings.py` (`pydantic-settings`). Aucun autre module ne lit `os.environ`.
2. Secrets typés `SecretStr` (dont `IMAP_USER`, `SMTP_USER`, `DIGEST_TO`) ; `.get_secret_value()` seulement au moment de l'usage.
3. Aucun défaut : une variable absente vaut `None`. `Settings.require(fonctionnalités)` arrête le programme en **nommant** les variables manquantes, jamais leurs valeurs, selon la table `REQUIRED_BY_FEATURE` (`france_travail`, `adzuna`, `imap`, `email`, `state_s3`).
4. Aucun secret dans les logs, exceptions, URLs journalisées ou fixtures (voir `http.py`, IMAP, SMTP).

### En local
- `.env` à la racine, ignoré par git ; `.env.example` commité sans valeurs. Option : trousseau du système (`keyring`).

### Dans GitHub Actions
- **Le dépôt public n'a aucun secret** ; un test vérifie que ses seuls workflows sont `ci.yml` et `release.yml`, sans autre secret que `GITHUB_TOKEN` ni OIDC.
- Les secrets vivent dans `vigie-run`, environnement `radar`, limité à `main` par ses **branches de déploiement**. Ils ne sont exposés qu'aux deux étapes qui en ont besoin, jamais au job entier.
- OIDC sans clé AWS : la politique de confiance du rôle vise le `sub` **avec identifiants numériques** `repo:HLeviel@<OWNER_ID>/vigie-run@<REPO_ID>:environment:radar` (le README du modèle explique comment lire le `sub` réel). Droits : `s3:GetObject` et `s3:PutObject` sur l'objet `radar.db`, plus `s3:ListBucket` limité au préfixe `radar.db` (sans lui, S3 répond 403 au lieu de 404 au premier lancement).
- Actions épinglées par SHA ; `persist-credentials: false` partout sauf la variante « état dans le dépôt » ; aucun artefact ni cache ; seul `echo` autorisé : l'ajout du dossier des outils `uv` au `PATH` ; `pull_request_target` interdit (contrôlé par un test).

### Protections du dépôt
- *Secret scanning* et *Push protection* activés (Settings > Code security).
- `pre-commit` : `gitleaks` (analyse les modifications indexées), `ruff-check`, `ruff-format`. L'historique complet se vérifie à la main avec `gitleaks detect`.
- `SECURITY.md` : architecture publique/privée, signalement, procédure de fuite (révoquer la clé **avant** de nettoyer l'historique).

### Confidentialité de la recherche
- Critères réels, entreprises ciblées et CV vivent **uniquement dans `vigie-run`** ; le dépôt public ne contient que des données fictives.
- Défense en profondeur : les logs et la console n'affichent que des compteurs et des noms de variables. Messages d'erreur de configuration : chemin et règle seulement, jamais la valeur saisie. En cas d'échec, la console affiche le type d'exception et, **uniquement** pour `MailboxError`, `NotifyError`, `StateSyncError` et `MissingSettingsError`, leur message fixe ; jamais le message d'une `SourceHttpError` (son URL contient les critères).
- Le digest n'est jamais publié en artefact ni commité.

## 8. Configuration

- Schéma strict (`config.py`, pydantic, `extra=forbid`) : une clé mal orthographiée est une erreur. Erreur YAML : ligne et colonne seulement.
- Résolution : `--config`, sinon `config/radar.yaml` du dossier courant (usage local, ignoré par git). Dans `vigie-run` : `--config config/radar.yaml`.
- Base : `--db`, sinon `VIGIE_DB_PATH`, sinon `~/.local/share/vigie/radar.db`.

```yaml
sources:
  adzuna: { enabled: true }
  ats: { enabled: true }
  email_alerts: { enabled: true }     # exige les 4 variables IMAP, sinon false
  france_travail: { enabled: false }  # en attente d'accès

ats_companies:                        # entreprises FICTIVES dans l'exemple
  - { name: "Exemple IA", platform: greenhouse, token: "exempleia" }
  - { name: "Startup Data", platform: lever, token: "startupdata" }

email_alerts:
  allowed_senders:
    france_travail: ["@francetravail.fr"]
    apec: ["@apec.fr"]
    wttj: ["@welcometothejungle.com"]
    linkedin: ["@linkedin.com"]

searches:
  - name: data-ai-lyon
    keywords: ["data engineer", "machine learning", "IA"]
    departments: ["69"]
    contract_types: [CDI, FREELANCE]
    max_age_days: 7

filters:
  exclude_title: ["stage", "alternance", "commercial"]
  exclude_companies: []
  require_any: ["python", "data", "ia", "machine learning"]   # sur titre + description

digest:
  max_items: 50
  format: html          # html ou markdown (fichier local ; l'email contient toujours HTML + texte)
```

## 9. Dédoublonnage

1. **Même source** : `uid` unique en base ; une offre déjà vue n'est pas réinsérée, `last_seen_at` est mis à jour. Deux offres d'une même source ayant la même `dedup_key` restent distinctes.
2. **Entre sources** : `dedup_key` = SHA-256 de `entreprise | titre | lieu`, normalisés (minuscules, sans accents ni ponctuation ni espaces multiples ; mentions `H/F`, `F/H`, `H/F/X` retirées, avec ou sans parenthèses). Lieu = département ; à défaut, ville normalisée (préfixe de département, parenthèses, virgule et numéro d'arrondissement retirés) ; à défaut, rien. Entreprise absente = chaîne vide.
   - L'offre canonique est la **première vue** ; les suivantes sont enregistrées dans `offer_aliases` seulement et n'apparaissent pas dans le digest.
   - L'alias d'une offre filtrée est lui aussi écarté, sans réévaluation.

Le dédoublonnage sémantique (embeddings) viendra avec `match`.

## 10. Filtres

Après dédoublonnage, dans l'ordre : ancienneté, contrat, exclusions de titre, entreprises exclues, mots requis.
- Contrat : seules les offres au contrat **connu et différent** des types demandés sont écartées ; `AUTRE` est conservé.
- Titre, mots requis : mots entiers sur texte normalisé, termes de plusieurs mots acceptés. Entreprise exclue si son nom contient le terme en mots entiers (« Infotel » exclut « Infotel Corp. »).
- Motifs stockés dans `filtered_reason` : `age`, `contract`, `title:<terme>`, `company:<terme>`, `require_any`.
- Une offre vue par plusieurs recherches n'est évaluée que par la **première** (limite connue : #22).

## 11. Stockage et état

SQLite `radar.db`, migrations par `PRAGMA user_version`. Tables `offers` (champs de `Offer` + `first_seen_at`, `last_seen_at`, `filtered_reason`, `notified_at` ; `raw` en JSON, dates en ISO UTC), `offer_aliases`, `processed_emails`, `runs`, `run_sources`.

**État S3 (`state.py`, appelé par `run`)** :
- Téléchargement au début ; « objet absent » = premier lancement ; **tout autre échec de lecture arrête l'exécution (code 5) avant la collecte**, jamais de départ sur un état vide.
- Renvoi après **toute exécution terminée** (codes 0, 2, 3, 4), pas après une erreur de configuration ou un plantage.
- Écriture conditionnelle (`If-Match` avec l'ETag lu, `If-None-Match: *` au premier envoi), chiffrement SSE-S3 demandé ; conflit : code 5.
- Ordre : envoi du digest, marquage des offres, puis renvoi de l'état (un doublon vaut mieux qu'un digest perdu).
- `--dry-run` : état lu dans un dossier temporaire, rien n'est renvoyé.
- Repli sans S3 : variante `radar-state-in-repo.yml.example` (état commité dans `state/radar.db` de `vigie-run`, `contents: write`), jamais pour un dépôt public.

## 12. Digest

- Offres **nouvelles, non filtrées, non notifiées**, triées par date de publication décroissante, limitées à `max_items`.
- Par offre : titre (200 caractères max), entreprise et lieu (100), contrat (omis si `AUTRE`), salaire s'il est connu (« 45 000 – 55 000 € brut/an », « à partir de… », « jusqu'à… »), date en heure de Paris (jj/mm/aaaa), source, lien. Ni description, ni télétravail. Une offre `suspicious` est affichée avec un avertissement.
- Pied : la dernière exécution enregistrée (statut, compteurs par source, type d'exception en cas d'échec).
- Sécurité : HTML en autoescape Jinja, sans aucune ressource externe (styles en ligne) ; Markdown avec échappement des textes d'offres et URL encodées ; caractères invisibles, bidirectionnels et de contrôle retirés.
- Aucune offre et aucun échec : rien n'est produit ni envoyé. Échec d'une source sans offre : digest d'alerte court, à chaque exécution tant que l'échec dure.
- **Fichier** (`vigie radar digest`) : `digest.html` ou `digest.md` du dossier courant, permissions 0600, sortie standard seulement avec `--output -` ; ne marque rien comme notifié, sauf `--mark-notified`.
- **Email** (`run`) : parties texte brut et HTML ; TLS obligatoire (STARTTLS, ou TLS implicite sur le port 465), identifiants envoyés seulement après TLS, certificat vérifié, délai de 30 s ; expéditeur = `SMTP_USER` s'il ressemble à une adresse, sinon le premier destinataire ; `DIGEST_TO` accepte plusieurs adresses séparées par des virgules. `NotifyError` nomme l'étape SMTP en cause, jamais le texte du serveur. Après envoi réussi seulement, les offres reçoivent `notified_at`.

## 13. CLI

```
vigie radar run    [--config PATH] [--db PATH] [--dry-run] [--source NAME] [--since AAAA-MM-JJ] [--email/--no-email]
vigie radar digest [--config PATH] [--db PATH] [--output PATH|-] [--mark-notified]
vigie radar stats  [--db PATH] [--limit N]
vigie radar check  [--config PATH] [--ci]
vigie [-v|--verbose] radar …      # journaux détaillés (toujours sans secret ni critère)
```
- `run` envoie le digest si les cinq variables SMTP existent, sinon l'envoi est ignoré avec un message ; une configuration SMTP partielle est une erreur (code 1) avant toute collecte ; jamais d'envoi en `--dry-run`.
- `--dry-run` : travaille sur une copie en mémoire de la base ; rien n'est écrit, historique compris.
- `--since` : minuit UTC ; par défaut, dernier run `success` moins 1 jour, sinon la plus grande ancienneté des recherches.
- `--source` : uniquement une source activée dans la configuration (sinon code 1).
- `check` : vérifie configuration et présence des secrets sans appel réseau ; `--ci` exige en plus SMTP et `VIGIE_STATE_BUCKET`.
- Statut d'un run : `success`, `partial` (au moins une source en échec), `failed` (toutes en échec).
- **Codes de sortie** : `0` succès, `1` configuration ou secrets, `2` toutes les sources en échec, `3` au moins une source en échec, `4` envoi du digest échoué (offres restées à notifier), `5` état S3 inaccessible ou non synchronisé. Priorité : 5 > 4 > 3 > 2.

## 14. Exécution : code public, exécution privée

| Dépôt | Visibilité | Contenu | Secrets |
|---|---|---|---|
| `HLeviel/vigie` | public | code, tests, CI, données fictives, modèle `examples/vigie-run/` | aucun |
| `HLeviel/vigie-run` | privé | vraie config, CV, workflow planifié | tous, environnement `radar` |

### Dépôt public
- `ci.yml` (push, PR, et appelable par `workflow_call`) : `uv sync --locked`, `ruff check`, `ruff format --check`, `mypy`, `pytest`, puis construction de la roue et `check_wheel.py` (la roue doit contenir le modèle du digest). Aucun appel réseau, aucun secret.
- `release.yml` (tag `vX.Y.Z`) : relance la CI, vérifie que le tag égale la version de `pyproject.toml` (`check_release_tag.py`), puis crée la release (journal généré depuis les PR) **sauf si elle existe déjà**. Un workflow déclenché par un tag s'exécute dans la version du fichier **au commit tagué**.
- Publier une version : monter la version dans `pyproject.toml` et `VIGIE_REF` du modèle (un test exige l'égalité), fusionner, puis poser le tag. Ne jamais déplacer un tag publié : publier une version suivante.

### Dépôt privé (`examples/vigie-run/.github/workflows/radar.yml`)
- `schedule` (05:17 UTC) et `workflow_dispatch` ; le modèle étant dans `examples/`, GitHub ne l'exécute jamais dans le dépôt public. `concurrency` sans annulation, `timeout-minutes: 30`.
- Étapes : checkout de `vigie-run` ; `uv tool install "git+https://github.com/HLeviel/vigie@${VIGIE_REF}"` (tag, jamais `main` ; un SHA est encore plus sûr) ; OIDC AWS ; `vigie radar check --ci` ; `vigie radar run`, qui gère lui-même l'état S3 et l'envoi.
- `README.md` du modèle : création du dépôt privé, de l'environnement et de ses secrets, du rôle IAM et du bucket, lecture du `sub` OIDC.

## 15. Tests

- Unitaires : normalisation, validation des recherches, `dedup_key`, filtres, configuration, masquage des secrets (`repr`, logs, URLs).
- Sources avec `respx` : pagination, erreurs (401, 404, 429 avec `Retry-After`, 5xx), réponses vides, éligibilité géographique ATS.
- Alertes email : faux serveur IMAP (seules `SEARCH` et `FETCH` admises), un parseur par expéditeur, liste blanche, messages déjà traités, format inconnu, aucune requête HTTP émise.
- Pipeline et CLI : deux exécutions successives sans doublon, panne d'une source, codes de sortie, SMTP et S3 simulés (écritures conditionnelles comprises).
- Workflows et versions : contrôles statiques (secrets, `echo`, `pull_request_target`, `VIGIE_REF` = version), bloc `run:` de `release.yml` exécuté avec un faux `gh`, aucune version en clair dans `src/`.
- **Fixtures** : structure réelle, **toutes les valeurs fictives** ; marquées « synthétiques » quand aucune donnée réelle n'a été vue ; un test refuse tout `.eml` contenant des données personnelles. Les vraies réponses et les vrais emails ne sont jamais commités.
- Couverture visée : 85 % sur `src/vigie/radar` (98 % au 24/11/2026).

## 16. Critères d'acceptation

- [x] `check` nomme les variables manquantes sans aucune valeur.
- [x] Deux exécutions consécutives : aucun doublon, aucune offre renotifiée.
- [x] Panne d'une source : les autres produisent leurs offres ; code 3.
- [x] Dépôt public sans secret ni donnée réelle ; CI verte sans secret.
- [x] CI verte : lint, typage strict, tests, couverture ≥ 85 %.
- [ ] `run --dry-run` en local avec de vraies clés Adzuna, au moins deux entreprises ATS réelles et la vraie boîte mail.
- [x] Premier lancement réel de `vigie-run` : version taguée installée, digest reçu par email ; à la deuxième exécution, état S3 relu et aucune offre renvoyée.
- [x] Logs d'une exécution réelle : seulement des compteurs, aucun intitulé, critère ni secret.
- [ ] `gitleaks detect` sur tout l'historique : rien.

## 17. Suite

- Ajuster les filtres de la configuration réelle d'après les premiers digests ; c'est aussi le point de départ du module `match`.
- Issues `à-vérifier` : #16 à #22.
- Facultatif : source France Travail si l'accès est accordé.
- Pistes non décidées : convertir le salaire écrit en clair dans les alertes France Travail (« Salaire : 1 802,00 € à 2 600,00 € par mois ») ; vérifier SPF/DKIM/DMARC (`Authentication-Results`) pour durcir la liste blanche des alertes.

## 18. Décisions hors spec

- Une décision prise pendant l'implémentation sans que la spec la précise est décrite dans la **description de la PR**, section « Décisions » : décision, raison, vérifiée ou non.
- Un point qui reste à vérifier ou à trancher devient une **issue** étiquetée `à-vérifier`.
- Les décisions confirmées sont reportées dans cette spec à la version suivante.
- Pas de fichier de décisions dans le dépôt : `docs/decisions.md` est supprimé (son contenu est intégré à la version 1.4 et reste dans l'historique git).

## Journal des modifications

- **1.5 (24/11/2026)** : lot M2 vérifié en réel (`vigie-run` privé avec état S3 : digest reçu, deuxième exécution sans renvoi d'offres, logs limités aux compteurs) ; critères d'acceptation correspondants cochés.
- **1.4 (24/11/2026)** : intégration des décisions d'implémentation confirmées (normalisation, HTTP, Adzuna, ATS, alertes email dont France Travail vérifié sur de vrais emails, configuration stricte, dédoublonnage, filtres, état S3, digest, SMTP, CLI avec codes 4 et 5, workflows, version unique dans `pyproject.toml`, format du `sub` OIDC). Points non vérifiés renvoyés aux issues #16 à #22. Nouvelle règle de la section 18 : décisions dans les PR, questions dans les issues, suppression de `docs/decisions.md`.
- **1.3 (24/11/2026)** : architecture « code public, exécution privée » ; dépôt privé `vigie-run` ; `release.yml` et modèle `examples/vigie-run/` ; suppression de `VIGIE_RADAR_CONFIG` ; interdiction de `pull_request_target`.
- **1.2 (19/11/2026)** : API France Travail retirée du libre-service ; Adzuna source principale ; ATS et alertes email en M1.
- **1.1 (19/11/2026)** : salaire « sur N mois » ; règles de télétravail ; codes de contrat par source ; mot-clé obligatoire, zone facultative ; `docs/decisions.md`.
- **1.0 (19/11/2026)** : version initiale.
