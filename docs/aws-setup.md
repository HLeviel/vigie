# Mise en place AWS et GitHub pour la collecte planifiée

Ce que `radar.yml` suppose. À faire une fois, avec un compte AWS que tu contrôles. Les commandes
utilisent des valeurs à remplacer : `<BUCKET>`, `<ACCOUNT_ID>`, `<REGION>`.

Rien de ceci n'a pu être exécuté ni vérifié depuis l'environnement de développement (pas d'accès
AWS) : le code S3 est testé avec un faux client, le workflow est validé statiquement.

## 1. Bucket d'état (privé, versionné, chiffré)

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

## 2. Fournisseur OIDC GitHub (une fois par compte)

```bash
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com --client-id-list sts.amazonaws.com
```

(Si le fournisseur existe déjà dans le compte, passer cette étape.)

## 3. Rôle IAM limité au dépôt et à l'environnement

Politique de confiance (`trust.json`) :

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
        "token.actions.githubusercontent.com:sub": "repo:HLeviel/vigie:environment:radar"
      }
    }
  }]
}
```

Un job qui utilise un environnement reçoit un `sub` fondé sur l'environnement, pas sur la branche :
la restriction à `main` se fait donc **dans GitHub** (étape 4), sur l'environnement `radar`.

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

`s3:ListBucket` (au-delà des deux actions de la spec) est nécessaire pour que S3 réponde « objet
absent » (404) à la première exécution au lieu de « accès refusé » (403) ; sans lui, une première
exécution est indistinguable d'une erreur d'accès, et `radar` s'arrête par prudence.

```bash
aws iam create-role --role-name vigie-radar --assume-role-policy-document file://trust.json
aws iam put-role-policy --role-name vigie-radar --policy-name state-access \
  --policy-document file://state-access.json
```

## 4. Environnement GitHub « radar »

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
| `VIGIE_RADAR_CONFIG` | Contenu YAML de `config/radar.yaml` (critères et entreprises **réels**) |

Settings > Code security : activer *Secret scanning* et *Push protection*.

## 5. Premier lancement

Actions > radar > Run workflow. Le journal ne montre que des compteurs. La première exécution crée
`radar.db` dans le bucket. Une fois satisfait, décommenter `schedule` dans `radar.yml` (05:17 UTC).

## Si un secret fuite

Révoquer et régénérer la clé chez le fournisseur **avant** de nettoyer l'historique.
