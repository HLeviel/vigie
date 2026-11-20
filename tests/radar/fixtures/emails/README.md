# Emails d'alerte (fixtures)

**Synthétiques et non vérifiés.** L'utilisateur n'avait pas encore reçu de vraie alerte au moment de
l'écriture : ces `.eml` imitent la structure supposée (liens d'offres, texte voisin, pied de page),
pas le format réel de France Travail, APEC, WTTJ ou LinkedIn. Tous les noms, adresses et liens sont
fictifs. À **remplacer par de vrais emails anonymisés** (noms, adresses, jetons de suivi remplacés),
puis ajuster `sources/email_alerts/*` (motifs de liens, lecture du texte voisin).

- `linkedin_hostile.eml` : texte caché et motifs d'injection de prompt (pour les tests de sécurité).
- `apec_format_change.eml` : aucun lien d'offre reconnu (format inconnu).
- `unknown_sender.eml` : expéditeur hors liste blanche.
