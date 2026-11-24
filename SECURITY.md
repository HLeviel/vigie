# Sécurité

## Architecture : code public, exécution privée

Ce dépôt est **public** : il contient le code, les tests, la CI et des données **fictives**. Il n'a
**aucun secret**, et sa CI tourne uniquement sur des fixtures. L'exécution réelle (secrets, vrais
critères, vraies entreprises ciblées, CV, état, logs) a lieu dans un dépôt **privé** `vigie-run`, qui
installe une version taguée de Vigie : voir [`examples/vigie-run/`](examples/vigie-run/README.md).

Règles pour contribuer :

- jamais de secret, de critère de recherche réel, de CV ni de vrai email dans ce dépôt ;
- jamais de déclencheur `pull_request_target` dans un workflow (il exécuterait du code de forks avec
  des droits élevés) ;
- les actions tierces sont épinglées par SHA de commit ;
- les exemples d'emails (`tests/radar/fixtures/emails/`) sont anonymisés : un test refuse les en-têtes
  de transport, les destinataires réels et les jetons de suivi.

## Signaler une vulnérabilité

Ouvrir un [avis de sécurité privé](https://github.com/HLeviel/vigie/security/advisories/new) (onglet
Security > Report a vulnerability) plutôt qu'une issue publique.

## En cas de fuite d'un secret

1. **Révoquer et régénérer la clé chez le fournisseur** (Adzuna, mot de passe d'application Gmail ou
   SMTP, rôle AWS…) **avant toute autre action**.
2. Mettre à jour le secret dans l'environnement `radar` de `vigie-run`.
3. Seulement ensuite, nettoyer l'historique Git si nécessaire : une clé révoquée est sans danger, une
   clé encore valide dans un historique « nettoyé » peut déjà avoir été copiée.

## Protections à activer

- GitHub : *Secret scanning* et *Push protection* (Settings > Code security), sur ce dépôt et sur
  `vigie-run`.
- Local : `pre-commit install` active le hook `gitleaks` (voir `.pre-commit-config.yaml`).
