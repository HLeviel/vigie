# vigie-run : dépôt privé d'exécution de Vigie

Ce dossier est le **modèle** du dépôt privé `HLeviel/vigie-run`. Le dépôt public `HLeviel/vigie`
contient le code, sans aucun secret ni donnée réelle ; **l'exécution réelle** (vrais critères,
vraies entreprises ciblées, CV, secrets, état, logs) a lieu ici, dans un dépôt **privé** que tu crées
toi-même. Il ne contient pas de code Python : il installe une **version taguée** de Vigie puis
l'exécute.

```
vigie-run/                       # PRIVÉ
├── config/radar.yaml            # vrais critères et vraies entreprises ciblées
├── profile/profile.yaml         # vrai profil (module match ; modèle fictif fourni ici)
├── cv/                          # CV réel (utilisé plus tard par match et cv)
├── labels/                      # CSV annotés à importer (module match, voir la section 7)
├── .github/workflows/radar.yml  # copie de ce modèle
├── .github/workflows/match-label-export.yml   # module match : export du CSV à annoter
├── .github/workflows/match-label-import.yml   # module match : import des étiquettes
├── .github/workflows/match-eval.yml           # module match : rapport d'évaluation
└── README.md
```

> Ce modèle n'a pas pu être exécuté pour de vrai depuis l'environnement de développement (pas d'accès
> AWS, SMTP ni GitHub). Le premier lancement manuel (étape 6) sert à le valider.

## 1. Créer le dépôt privé

1. Sur GitHub, créer `vigie-run` en **privé**, sans fichier initial.
2. Y copier `config/radar.yaml` et `.github/workflows/radar.yml` de ce dossier.
3. Remplacer le contenu de `config/radar.yaml` par tes **vrais** critères (`searches`, `filters`) et tes
   **vraies** entreprises (`ats_companies`, avec le `token` de leur page carrières). Ce fichier est
   fourni par `--config` ; il ne doit jamais être commité dans le dépôt public.
   Pour le module `match`, copier aussi `profile/profile.yaml` (fictif) et le remplacer par ton vrai
   profil ; `vigie match check` le valide sans appel réseau.
4. Activer *Secret scanning* et *Push protection* (Settings > Code security).

## 2. Bucket S3 d'état (privé, versionné, chiffré)

Les commandes utilisent des valeurs à remplacer : `<BUCKET>`, `<ACCOUNT_ID>`, `<REGION>`.

```bash
aws s3api create-bucket --bucket <BUCKET> --region <REGION> \
  --create-bucket-configuration LocationConstraint=<REGION>
aws s3api put-public-access-block --bucket <BUCKET> --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-versioning --bucket <BUCKET> --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket <BUCKET> --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
```

L'objet est `radar.db` à la racine du bucket. Il contient les offres retenues : il reste privé.

## 3. Authentification AWS par OIDC (aucune clé AWS stockée)

Fournisseur OIDC GitHub (une fois par compte AWS ; passer l'étape s'il existe déjà) :

```bash
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com --client-id-list sts.amazonaws.com
```

Politique de confiance du rôle (`trust.json`), **restreinte à `vigie-run` et à son environnement `radar`**.
GitHub met désormais les **identifiants numériques** du propriétaire et du dépôt dans le `sub` du jeton
(`repo:<propriétaire>@<id>/<dépôt>@<id>:environment:radar`) : la politique les exige, ce qui la rend aussi
insensible à un dépôt renommé ou recréé sous le même nom. Remplace `<OWNER_ID>` et `<REPO_ID>` par les
valeurs de ton compte et de ton dépôt (voir l'encadré « Trouver le `sub` réel » plus bas) :

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Federated": "arn:aws:iam::<ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com"},
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": "repo:HLeviel@<OWNER_ID>/vigie-run@<REPO_ID>:environment:radar"
      }
    }
  }]
}
```

Un job qui utilise un environnement reçoit un `sub` fondé sur l'environnement, pas sur la branche : la
restriction à `main` se fait donc **dans GitHub** (étape 4), sur l'environnement `radar`.

**Trouver le `sub` réel.** En cas d'erreur `Not authorized to perform sts:AssumeRoleWithWebIdentity`,
ajoute temporairement cette étape avant `configure-aws-credentials` (elle n'affiche que des
revendications, jamais le jeton) et compare le `sub` à `trust.json`, caractère par caractère :

```yaml
      - name: Debug OIDC (temporaire)
        run: |
          curl -sS -H "Authorization: bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
            "$ACTIONS_ID_TOKEN_REQUEST_URL&audience=sts.amazonaws.com" \
          | python3 -c "
          import sys, json, base64
          p = json.load(sys.stdin)['value'].split('.')[1]
          p += '=' * (-len(p) % 4)
          c = json.loads(base64.urlsafe_b64decode(p))
          print(json.dumps({k: c.get(k) for k in ('sub', 'aud', 'repository', 'environment', 'ref')}, indent=1))
          "
```

Les identifiants numériques s'y trouvent (ex. `repo:HLeviel@13158585/vigie-run@1469974815:environment:radar`).
Dans les logs, GitHub remplace par `***` toute chaîne égale à la valeur d'un secret (le nom du dépôt
peut donc apparaître masqué si un secret porte le même nom) : c'est sans danger. Pour un dépôt plus
ancien dont le `sub` n'a pas d'identifiants (`repo:HLeviel/vigie-run:environment:radar`), utilise ce
format-là. Retire l'étape de debug une fois le rôle fonctionnel.

Politique de permissions (`state-access.json`), limitée à l'objet d'état :

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": "arn:aws:s3:::<BUCKET>/radar.db"
    },
    {
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::<BUCKET>",
      "Condition": {"StringEquals": {"s3:prefix": "radar.db"}}
    }
  ]
}
```

`s3:ListBucket` (au-delà des deux actions habituelles) est nécessaire pour que S3 réponde « objet
absent » (404) à la première exécution au lieu de « accès refusé » (403) ; sans lui, une première
exécution est indiscernable d'une erreur d'accès, et Vigie s'arrête par prudence.

```bash
aws iam create-role --role-name vigie-radar --assume-role-policy-document file://trust.json
aws iam put-role-policy --role-name vigie-radar --policy-name state-access \
  --policy-document file://state-access.json
```

### Module `match` : permission Bedrock (seulement si `embeddings.provider: bedrock`)

Un deuxième document donne `bedrock:InvokeModel` sur les **seuls** modèles utilisés, jamais `*`
(`bedrock-access.json`, à attacher au même rôle avec `put-role-policy`). L'ARN d'un modèle de
fondation a la forme ci-dessous (forme standard, non vérifiée sur ton compte : région et modèle
doivent être activés dans Bedrock) :

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "bedrock:InvokeModel",
      "Resource": "arn:aws:bedrock:<REGION>::foundation-model/<ID_DU_MODELE>"
    }
  ]
}
```

Le modèle est celui de `match.embeddings.model` ; seuls Amazon Titan Text Embeddings V2
(`amazon.titan-embed-text-v2…`) et Cohere Embed Multilingual v3 (`cohere.embed-multilingual-v3…`)
sont pris en charge pour l'instant. Les modèles de fournisseurs tiers (Cohere) doivent en plus être
activés dans la console Bedrock (non vérifié ici). Un profil d'inférence
interrégional demanderait d'autres ARN : non couvert ici.

```bash
aws iam put-role-policy --role-name vigie-radar --policy-name bedrock-access \
  --policy-document file://bedrock-access.json
```

## 4. Environnement GitHub « radar » (dans `vigie-run`)

Settings > Environments > New environment : `radar`.

- **Deployment branches and tags** : « Selected branches », uniquement `main`.
- **Variables** (non secrètes) : `AWS_ROLE_ARN` (ARN du rôle `vigie-radar`) et `AWS_REGION`.
- **Secrets** de l'environnement (pas en secrets de dépôt) :

| Secret | Contenu |
|---|---|
| `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | Clés Adzuna |
| `IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD`, `IMAP_FOLDER` | Boîte des alertes (mot de passe d'application) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `DIGEST_TO` | Envoi du digest (`DIGEST_TO` : une ou plusieurs adresses séparées par des virgules) |
| `VIGIE_STATE_BUCKET` | Nom du bucket |
| `OPENROUTER_API_KEY` | Clé OpenRouter, **seulement** pour l'étage LLM du module `match` (section 8) |

Il n'y a **pas** de secret pour la configuration : `config/radar.yaml` est un fichier du dépôt privé.

## 5. Choisir la version de Vigie

Le workflow installe Vigie depuis un **tag** du dépôt public, fixé par `VIGIE_REF` en tête de
`radar.yml` (jamais `main`) :

```yaml
env:
  VIGIE_REF: v0.3.0
```

Mettre à jour Vigie, c'est changer cette ligne (à la main, ou avec Renovate configuré dans `vigie-run`).
Les tags peuvent être déplacés : pour une garantie plus forte, remplace le tag par le SHA du commit
(`@<sha>`).

## 6. Premier lancement

Actions > radar > Run workflow. Le journal ne montre que des compteurs. La première exécution crée
`radar.db` dans le bucket. La planification (`schedule`, 05:17 UTC) est déjà dans le modèle : elle
s'exécute une fois ce dépôt prêt ; avant que les secrets existent, `check` échoue tôt et rien n'est
collecté.

## 7. Annoter sans environnement de développement (module `match`)

Annoter des offres sert à mesurer la qualité du classement (objectif : **150 offres annotées au
moins, dont 30 pertinentes**). Tout se fait depuis GitHub et un tableur, sans rien installer. Prérequis :
copier les trois workflows `match-*.yml` dans `.github/workflows/` de `vigie-run`, avoir une section
`match:` dans `config/radar.yaml` (avec `profile/profile.yaml`) et le même environnement `radar` que
pour `radar.yml` (mêmes secrets et mêmes variables). Ces workflows lisent (et, pour l'import et
l'évaluation, réécrivent) l'état S3 comme `radar run`, avec le même `concurrency`.

1. **Exporter.** Actions > `match-label-export` > Run workflow (champ `n`, 50 par défaut). Le run dépose
   `labels-AAAA-MM-JJ.csv` en **artefact** (conservé 1 jour, visible des seuls collaborateurs de ce
   dépôt privé) : télécharge-le depuis la page du run et décompresse l'archive.
2. **Annoter dans un tableur** (Excel, LibreOffice, Numbers, Google Sheets). Le fichier est en UTF-8
   avec séparateur `;` : il s'ouvre directement dans Excel en français. Renseigne uniquement la colonne
   `label` : `p` ou `pertinent`, `m` ou `peut-etre`, `n` ou `non` (sans casse ni accents) ; une cellule
   vide veut dire « non annotée ». Les offres sont dans un ordre aléatoire et **sans score ni rang**,
   pour ne pas t'influencer. Modifier une autre colonne n'a aucun effet. Enregistre en CSV (UTF-8).
3. **Importer.** Dépose le fichier annoté dans le dossier `labels/` du dépôt, sur la branche `main`
   (interface web : *Add file > Upload files*). Le workflow `match-label-import` se lance seul : il
   importe **tous** les fichiers `labels/*.csv` (le nom le plus tardif l'emporte en cas de conflit),
   renvoie l'état vers S3 et n'affiche que des compteurs : lignes lues, annotées, ignorées, en
   erreur (avec le numéro de ligne et le motif, jamais le contenu). Une ligne en erreur n'empêche pas
   l'import des autres. Les fichiers déjà importés sont rejoués à chaque dépôt : leurs étiquettes
   écrasent celles posées autrement entre-temps.
4. **Évaluer.** Actions > `match-eval` > Run workflow : le rapport (Markdown + SVG) est déposé en
   artefact (7 jours). Il ne contient que des agrégats : copie-le dans `docs/eval/` du dépôt public.
   Pour un modèle d'embedding `local`, coche « Installer le modèle local » (installation volumineuse,
   non vérifiée) ; pour `bedrock`, le rôle IAM doit autoriser `bedrock:InvokeModel` sur le modèle (voir
   la section 3).

Les CSV et les artefacts contiennent des offres réelles : ne les commite jamais dans le dépôt public.
Annotations conservées : elles ne dépendent pas de la version du profil ; réannote seulement à la
demande (`vigie match label --relabel --export …`).

## 8. Étage LLM du module `match` (OpenRouter)

Après le score hybride, un LLM peut réévaluer les `N` meilleures offres (note de 0 à 100,
recommandation `postuler`, `à voir` ou `passer`, points forts, écarts, risques). Il passe par
**OpenRouter** : une seule clé pour tous les modèles, et le modèle se choisit **dans la
configuration**, sans toucher au code.

1. Créer un compte OpenRouter, une clé d'API et provisionner des crédits ; ajouter la clé comme
   **secret de l'environnement `radar`** sous le nom `OPENROUTER_API_KEY` (comme les autres secrets,
   section 4). `radar.yml` ne l'expose qu'à l'étape `vigie radar run`, `match-eval.yml` qu'à l'évaluation.
2. Dans `config/radar.yaml`, choisir le modèle (identifiant OpenRouter `fournisseur/modèle`, à lire
   sur openrouter.ai) et activer l'étage. Aucun modèle n'est imposé :

```yaml
match:
  llm:
    enabled: true
    model: "<fournisseur>/<modèle>"   # modifiable à tout moment
    temperature: 0
    max_tokens: 800                    # par appel
    top_n: 10                          # offres réévaluées par exécution
    max_calls_per_run: 15              # budget d'appels (nouvelles tentatives comprises)
    max_total_tokens_per_run: 60000    # budget de jetons (entrée + sortie)
```

3. **Changer de modèle** : modifier `match.llm.model` ici, dans `vigie-run`, sans aucune PR dans le dépôt
   public. Le cache des évaluations inclut l'identifiant du modèle : les offres encore à notifier sont
   réévaluées avec le nouveau modèle, les anciens scores restent en base pour comparaison.

**Ce qui part chez OpenRouter, puis chez le fournisseur du modèle** : une version nettoyée de ton
profil (résumé, rôles cibles, compétences, titres, descriptions et compétences des expériences, préférences ;
**sans noms d'entreprise ni dates**) et, pour chaque offre évaluée, son titre, son entreprise, son lieu, son
contrat, son salaire et sa description (nettoyée, bornée à 6 000 caractères). Pas de lien, pas d'identifiant,
pas de coordonnées. Chaque requête demande `provider.data_collection: "deny"` (exclut les fournisseurs
qui conservent ou réutilisent les données) et `provider.require_parameters: true` (n'utilise qu'un
fournisseur qui respecte le schéma de sortie). **Vérifie aussi les réglages de confidentialité de ton
compte OpenRouter avant d'activer l'étage** : cette option ne les remplace pas.

**Coût et limites** : chaque offre évaluée coûte un appel (deux au plus avec la nouvelle tentative). Le budget
d'appels et de jetons arrête l'étage proprement (message « budget atteint », les offres restantes gardent
leur score hybride). Le rapport d'exécution et le pied du digest ne contiennent que des compteurs
(candidates, évaluées, en cache, invalides, appels, jetons). Le tarif d'un modèle n'est pas calculé : il se lit
chez OpenRouter.

**En cas de panne** (clé refusée, crédits épuisés, modèle qui ne gère pas la sortie structurée stricte ou
`data_collection: "deny"`, réseau) : l'étage est ignoré pour l'exécution, les offres restent triées par score
hybride, le digest part avec une mention, et `radar run` sort en **code 6**. Le message de la console nomme
la cause (code HTTP et paramètres exigés), jamais une offre. Avec un autre modèle, relancer.

> Cet étage n'a jamais été exécuté contre le vrai service : la forme des requêtes et des réponses suit la
> documentation d'OpenRouter, mais le code HTTP renvoyé quand aucun fournisseur ne convient n'y est pas
> précisé. Ne l'active par défaut que si l'évaluation (`match-eval.yml`, au moins deux modèles via
> `match.evaluation.llm_models`) montre un gain de `hybrid+llm` sur `hybrid`.

## Repli sans S3

Si le bucket n'est pas encore en place, `radar-state-in-repo.yml.example` (même dossier) commite
`state/radar.db` dans `vigie-run` à la fin de chaque exécution. Le workflow reçoit alors
`contents: write`, ce qu'il ne faut accorder que dans ce cas ; à renommer en `radar.yml` à la place
du modèle S3, puis à abandonner dès que S3 est prêt (copier `state/radar.db` vers le bucket).

## Si un secret fuite

Révoquer et régénérer la clé chez le fournisseur **avant** de nettoyer l'historique.
