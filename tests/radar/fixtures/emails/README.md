# Emails d'alerte (fixtures)

| Fichier | Origine |
|---|---|
| `france_travail.eml`, `france_travail_single.eml` | **Gabarit réel** (vrais emails France Travail reçus le 22 et le 24/11/2026), **valeurs fictives** |
| `apec.eml`, `wttj.eml`, `linkedin.eml` | **Synthétiques et non vérifiés** (aucune vraie alerte reçue) |
| `linkedin_hostile.eml` | Synthétique : texte caché et motifs d'injection de prompt |
| `apec_format_change.eml` | Synthétique : aucun lien d'offre reconnu (format inconnu) |
| `unknown_sender.eml` | Synthétique : expéditeur hors liste blanche |

## Les fixtures France Travail

Le balisage (tables, commentaires conditionnels, gabarit MJML, liens `at_*`, ordre contrat / temps de
travail / extrait / « Offre n° ») est celui d'un vrai email. Tout le reste est remplacé : destinataire,
nom de l'utilisateur, nom de l'alerte et mots-clés de recherche, offres, entreprises, identifiants,
textes, en-têtes de transport et d'authentification (`Received`, `DKIM-Signature`, `Authentication-Results`,
`X-Pole-Emploi-ID`…). Elles couvrent les variantes observées : offre avec ou sans entreprise, lieu réduit
au département, Corse (`2B`), identifiant numérique, contrats CDI / CDD / Intérim, temps plein, partiel et
« Non renseigné », salaire écrit dans le texte, mention « offre confiée à France Travail par… ».

Les vrais emails ne sont **jamais** committés. `test_email_fixtures.py` refuse tout `.eml` qui contiendrait
un en-tête de transport ou d'authentification, un destinataire non fictif, une adresse de messagerie
grand public ou un long jeton de suivi dans un lien : à respecter pour les prochains exemples réels
(APEC, WTTJ, LinkedIn).
