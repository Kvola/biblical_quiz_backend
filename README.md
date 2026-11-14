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
