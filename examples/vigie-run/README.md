# vigie-run : dépôt privé d'exécution de Vigie

Ce dossier est le **modèle** du dépôt privé `HLeviel/vigie-run`. Le dépôt public `HLeviel/vigie`
contient le code, sans aucun secret ni donnée réelle ; **l'exécution réelle** (vrais critères,
vraies entreprises ciblées, CV, secrets, état, logs) a lieu ici, dans un dépôt **privé** que tu crées
toi-même. Il ne contient pas de code Python : il installe une **version taguée** de Vigie puis
l'exécute.

```
vigie-run/                       # PRIVÉ
├── config/radar.yaml            # vrais critères et vraies entreprises ciblées
├── cv/                          # CV réel (utilisé plus tard par match et cv)
├── .github/workflows/radar.yml  # copie de ce modèle
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

Il n'y a **pas** de secret pour la configuration : `config/radar.yaml` est un fichier du dépôt privé.

## 5. Choisir la version de Vigie

Le workflow installe Vigie depuis un **tag** du dépôt public, fixé par `VIGIE_REF` en tête de
`radar.yml` (jamais `main`) :

```yaml
env:
  VIGIE_REF: v0.1.0
```

Mettre à jour Vigie, c'est changer cette ligne (à la main, ou avec Renovate configuré dans `vigie-run`).
Les tags peuvent être déplacés : pour une garantie plus forte, remplace le tag par le SHA du commit
(`@<sha>`).

## 6. Premier lancement

Actions > radar > Run workflow. Le journal ne montre que des compteurs. La première exécution crée
`radar.db` dans le bucket. La planification (`schedule`, 05:17 UTC) est déjà dans le modèle : elle
s'exécute une fois ce dépôt prêt ; avant que les secrets existent, `check` échoue tôt et rien n'est
collecté.

## Repli sans S3

Si le bucket n'est pas encore en place, `radar-state-in-repo.yml.example` (même dossier) commite
`state/radar.db` dans `vigie-run` à la fin de chaque exécution. Le workflow reçoit alors
`contents: write`, ce qu'il ne faut accorder que dans ce cas ; à renommer en `radar.yml` à la place
du modèle S3, puis à abandonner dès que S3 est prêt (copier `state/radar.db` vers le bucket).

## Si un secret fuite

Révoquer et régénérer la clé chez le fournisseur **avant** de nettoyer l'historique.
