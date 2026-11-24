# Fixtures

`france_travail_search.json` est une réponse **reconstruite** d'après la connaissance du schéma de l'API Offres d'emploi v2. Elle **n'a pas été enregistrée** depuis l'API (l'accès est suspendu), et toutes les valeurs (entreprises, contacts) sont fictives. À remplacer par une réponse réelle anonymisée dès que l'API est de nouveau accessible (voir `docs/specs/radar.md` et l'issue #19).

`adzuna_search.json` est une **vraie réponse** de l'API Adzuna (recherche « ingénieur ia » à Lyon, 10 résultats), **anonymisée** : noms d'entreprises remplacés (« Société A »…), identifiants d'offres remplacés, champs `adref` supprimés, `utm_source` neutralisé (ils identifient l'application), descriptions remplacées par un texte fictif de même forme (extrait terminé par « … »). Les titres, lieux, salaires, dates, catégories et la structure sont ceux de la réponse réelle.

`adzuna_edge_cases.json` est **synthétique** : il couvre des cas absents de la réponse réelle (salaire estimé, description HTML, alternance, offre minimale).

Les fichiers `ats_*.json` (Greenhouse, Lever, Ashby, SmartRecruiters, Recruitee) reprennent la **structure vérifiée le 20/11/2026 sur de vraies réponses des API publiques** (noms et types de champs, formats de dates, entités HTML de Greenhouse, `createdAt` en millisecondes de Lever…). Toutes les **valeurs sont fictives** (entreprises, identifiants, textes). Les valeurs de `salary` de Recruitee sont inventées : aucune offre réelle observée n'en renseignait.

Les `.eml` du dossier `emails/` sont **synthétiques et non vérifiés** (voir `emails/README.md`) : aucune vraie alerte n'était disponible.
