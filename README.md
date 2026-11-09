# biblical_quiz_backend

Module Odoo 19 qui déclare l'application **Quiz Biblique** (dépôt `biblical_quiz`) sur la page publique de téléchargement `/apps` du socle `mobile_app_release`, avec son guide d'utilisation.

L'application fonctionne **entièrement hors ligne et sans compte** : elle n'a pas de serveur. Ce module ne contient donc que la déclaration `mobile.app` et le guide.

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
