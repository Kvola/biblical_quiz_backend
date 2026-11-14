# -*- coding: utf-8 -*-
{
    'name': "Quiz Biblique — page de téléchargement et guide",
    'version': '19.0.1.0.3',
    'category': 'Productivity',
    'summary': "Déclare l'application Quiz Biblique sur la page publique /apps, "
               "avec son guide d'utilisation en ligne et en PDF",
    'description': """
Quiz Biblique — déclaration sur /apps
=====================================

L'application Quiz Biblique fonctionne entièrement hors ligne et sans compte :
elle n'a pas de serveur. Ce module la déclare seulement sur le socle
``mobile_app_release`` pour que l'on puisse la télécharger depuis ``/apps``,
et publie son guide d'utilisation (``/apps/quiz-biblique/guide`` et
``/apps/quiz-biblique/guide.pdf``).

Le guide est produit depuis ``biblical_quiz/docs/GUIDE_UTILISATEUR.md`` par
``docs/md_to_guide_qweb.py`` : ne pas modifier ``reports/user_guide.xml`` à la main.
    """,
    'author': 'ICP',
    'website': 'https://ivorycocoa.ci',
    'depends': ['mobile_app_release'],
    'data': [
        'reports/user_guide.xml',
        'data/mobile_app.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
