# -*- coding: utf-8 -*-
{
    'name': "Quiz Biblique — jeu en ligne, page de téléchargement et guide",
    'version': '19.0.2.0.1',
    'category': 'Productivity',
    'summary': "Serveur du jeu en ligne de l'application Quiz Biblique (matchs à la main, "
               "équipes, tournois) ; page /apps et guide d'utilisation",
    'description': """
Quiz Biblique — serveur
=======================

L'application fonctionne hors ligne et sans compte. Ce module ajoute le
**jeu en ligne**, pour qui choisit de devenir joueur (numéro vérifié par code
WhatsApp, pseudo unique) :

* **Matchs** de 2 à 10 joueurs (réglable), ou entre **deux équipes** de 2 à
  5 joueurs (réglable), sur un ou plusieurs passages bibliques choisis.
  Le premier qui prend la main a 5 s pour répondre, chrono de la question
  en pause ; bonne réponse +10, erreur ou main laissée filer −5.
* **Tournois** à élimination directe, entre joueurs ou entre équipes.
* Un joueur ne joue qu'une partie (match ou tournoi) à la fois ; le nombre de
  matchs et de tournois simultanés est borné (Réglages).
* Le serveur est le seul arbitre (chronos, prise de main, bonnes réponses) ;
  temps réel par le bus Odoo (WebSocket), repli par interrogation.
* Questions : celles, relues, de l'application, puis des questions tirées du
  texte biblique (``game/``, contenu copié par ``biblical_quiz/scripts/sync_backend_content.py``).

Il déclare aussi l'application sur ``/apps`` (socle ``mobile_app_release``)
et publie son guide (``/apps/quiz-biblique/guide`` et ``.pdf``), produit
depuis ``biblical_quiz/docs/GUIDE_UTILISATEUR.md`` par
``docs/md_to_guide_qweb.py`` : ne pas modifier ``reports/user_guide.xml`` à la main.
    """,
    'author': 'ICP',
    'website': 'https://ivorycocoa.ci',
    'depends': ['bus', 'mobile_app_release', 'otp_auth', 'push_notification_hub'],
    'data': [
        'security/bq_security.xml',
        'security/ir.model.access.csv',
        'reports/user_guide.xml',
        'data/mobile_app.xml',
        'data/bq_cron.xml',
        'views/bq_views.xml',
    ],
    'pre_init_hook': 'pre_init_hook',
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}
