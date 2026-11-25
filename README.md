# biblical_quiz_backend

Module Odoo 19 de l'application **Quiz Biblique** (dépôt `biblical_quiz`) :

- le **jeu en ligne** (facultatif pour les utilisateurs) : comptes joueurs (numéro vérifié par WhatsApp via `otp_auth`, pseudo unique), matchs à la main de 2 à 10 joueurs ou entre deux équipes, tournois à élimination directe ;
- la déclaration de l'application sur la page publique `/apps` (socle `mobile_app_release`) et son guide d'utilisation.

Le reste de l'application fonctionne hors ligne et sans compte.

## Le jeu en ligne

| Élément | Où |
|---|---|
| Règles du match (main, chrono en pause, points, départage) | `models/bq_match.py` (docstring) |
| Tournois (tableau, exemptions, forfaits) | `models/bq_tournament.py` |
| Équipes (capitaine, invitations, demandes) | `models/bq_team.py` |
| Joueurs, engagement unique, signalements | `models/bq_player.py` |
| Réglages (limites, durées par défaut) | `models/bq_config.py` — Quiz Biblique → Configuration → Réglages |
| API JSON-RPC `/quiz-biblique/api/v1` | `controllers/api.py` |
| Page d'invitation `/quiz-biblique/rejoindre/<code>` | `controllers/public.py` |
| Générateur de questions (Python pur) | `game/` — contenu copié par `biblical_quiz/scripts/sync_backend_content.py` |

**Arbitrage** : le serveur tient les chronos, décide qui a pris la main (verrou de ligne du match) et ne livre la bonne réponse qu'une fois la question close. Le temps avance à chaque requête (les téléphones envoient `/match/tick` à chaque échéance) ; une tâche planifiée par minute rattrape les matchs abandonnés.

**Temps réel** : l'état de chaque match et tournoi part sur un canal secret du bus Odoo (WebSocket, port gevent derrière nginx) ; sans WebSocket, l'application interroge le serveur. Une application mobile doit envoyer l'en-tête `Origin` (Odoo refuse la poignée de main sans lui).

**Notifications push** : envoyées par `push_notification_hub` (identifiant `quiz_biblique`) après validation de la transaction ; l'application ne les reçoit qu'une fois Firebase configuré pour `ci.ivorycocoa.biblical_quiz`.

**Installation sur un serveur où l'application a été déclarée à la main** : le `pre_init_hook` rattache la fiche `quiz-biblique` existante (et ses versions publiées) au module au lieu d'en créer une seconde.

## Ce qu'il apporte

| Adresse | Sert |
|---|---|
| `/apps/quiz-biblique` | La page de téléchargement (bouton, QR code, marche à suivre) |
| `/apps/quiz-biblique/guide` | Le guide en ligne |
| `/apps/quiz-biblique/guide.pdf` | Le même guide, en PDF |

Les versions (APK) se publient depuis le back-office (menu du socle), elles ne figurent pas dans ce module. `support_note` n'est volontairement pas livré : le gestionnaire le règle depuis le back-office.

## Le guide

Source unique : `biblical_quiz/docs/GUIDE_UTILISATEUR.md`. Le fichier `reports/user_guide.xml` est **produit** ; ne le modifiez pas à la main. Après une modification du Markdown, depuis le dossier `sunday_jobs` :

```bash
python3 docs/md_to_guide_qweb.py quiz-biblique
```

## Tests

```bash
cd ../odoo_dev && ./test.sh biblical_quiz_backend
```

## Mise en production

Comme les autres modules : `./pull_addons.sh --clone biblical_quiz_backend` puis `./update_module.sh biblical_quiz_backend iyf.ebng.world install prod`, puis publier l'APK depuis le back-office.

## Licence

LGPL-3

## Questions de compréhension rédigées par IA (19.0.3.0.0)

Une banque de questions par chapitre, pour le « Quiz sur mes lectures » de l'application.

- **Mise en service** : Quiz Biblique → Configuration → Réglages → *Questions rédigées par IA* →
  renseigner la **clé Anthropic** (paramètre `biblical_quiz.anthropic_api_key`). Sans clé, l'application
  indique que le service n'est pas disponible. Essai réel sans rien enregistrer :
  `odoo shell … < scripts/test_ia.py`.
- **Fonctionnement** : un lecteur demande un chapitre (`/ai/request`, quota gratuit par appareil et par
  jour) → le chapitre est mis en file → la tâche planifiée *rédiger les chapitres en file* appelle Claude
  deux fois (rédaction, puis relecture indépendante à l'aveugle), applique les contrôles de
  `game/ai_bank.py`, enregistre les questions → l'application les lit (`/ai/bank`) et les garde hors ligne.
  Un chapitre n'est rédigé qu'une fois pour tous les lecteurs.
- **Relecture** : Questions IA → *À relire*. *Valider* enlève la mention « non relu » dans l'application
  et rend la question utilisable dans les matchs ; *Retirer* cesse de la servir. Corriger un texte suffit à
  le faire parvenir aux téléphones (révision du chapitre).
- **Validation en masse** (19.0.3.1.0) : dans *À relire*, cocher des questions puis *Valider la sélection* (ou *Retirer la sélection*) ; dans *Chapitres*, *Valider les questions de ces chapitres*, et sur la fiche d'un chapitre *Valider toutes les questions*. Une question signalée par un lecteur ou retirée n'est jamais validée en masse : elle reste à ouvrir.
- **Signalements** des lecteurs : une question jamais relue est retirée d'office au 3e appareil qui la
  signale (réglable).
- **Coût mesuré** (essais réels du 24 novembre 2026, claude-opus-5-5) : Philémon, 9 questions, 0,23 $ ; Psaume 23, 4 questions, 0,10 $, 39 secondes.
- **Dépense** : plafond quotidien du serveur (40 chapitres par défaut), plafond par réseau, journal
  *Rédactions et coût* (jetons, coût estimé, questions écartées et pourquoi). *Préparer des chapitres*
  fait rédiger d'avance un livre, hors quota des lecteurs.
- Dépendance Python : `anthropic` (déjà requise par les modules Comprendre, Génie en herbe, Carnet Auto).

