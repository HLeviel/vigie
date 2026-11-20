# Fixtures

`france_travail_search.json` est une réponse **reconstruite** d'après la connaissance du schéma de l'API Offres d'emploi v2. Elle **n'a pas été enregistrée** depuis l'API (l'accès est suspendu), et toutes les valeurs (entreprises, contacts) sont fictives. À remplacer par une réponse réelle anonymisée dès que l'API est de nouveau accessible (voir `docs/decisions.md`).

`adzuna_search.json` suit la structure de l'exemple de la documentation officielle Adzuna (`results`, `id`, `title`, `description`, `created`, `redirect_url`, `company.display_name`, `location.area`, `salary_min/max`, `salary_is_predicted`, `contract_type`). Les valeurs sont **fictives** ; les noms de départements dans `location.area` et les valeurs de `contract_type` pour la France n'ont pas été vérifiés sur une réponse réelle (aucune clé disponible). À remplacer par une réponse réelle anonymisée.
