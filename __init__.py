# -*- coding: utf-8 -*-
from . import controllers
from . import models

XMLID = 'mobile_app_quiz_biblique'


def pre_init_hook(env):
    """Adopte une application « quiz-biblique » déclarée à la main avant ce module.

    Sur un serveur où l'application a été créée depuis le back-office (pour y
    publier des APK), créer la déclaration du module heurterait l'unicité du
    slug. On rattache donc la fiche existante — et ses versions publiées — à
    l'identifiant du module, qui la met ensuite à jour normalement.
    """
    if 'mobile.app' not in env:
        return
    app = env['mobile.app'].sudo().with_context(active_test=False).search([('slug', '=', 'quiz-biblique')], limit=1)
    if not app:
        return
    Data = env['ir.model.data'].sudo()
    if Data.search_count([('module', '=', 'biblical_quiz_backend'), ('name', '=', XMLID)]):
        return
    Data.create({'module': 'biblical_quiz_backend', 'name': XMLID, 'model': 'mobile.app', 'res_id': app.id,
                 'noupdate': False})
