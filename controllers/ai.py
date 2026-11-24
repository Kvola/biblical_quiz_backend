# -*- coding: utf-8 -*-
"""Questions de compréhension rédigées par IA : routes de l'application.

Sans compte : l'application s'identifie par son identifiant d'installation
(anonyme, haché à l'arrivée). Lire la banque est libre ; demander un chapitre
nouveau consomme le quota gratuit du jour.
"""
import hashlib

from odoo import _
from odoo.http import Controller, request, route

from odoo.addons.otp_auth.tools.client_ip import client_ip

from ..models.bq_ai import AiRefusal
from .common import API, fail, ok, rate_limited

RL_BANK = (300, 60, 60)        # lectures de la banque par IP (une salle entière sur le même Wi-Fi)
RL_REQUEST = (30, 600, 600)    # demandes de chapitres par IP, avant même le quota
RL_REPORT = (30, 600, 600)     # signalements par IP


def _ip_hash():
    ip = client_ip(request) or ''
    return hashlib.sha256(ip.encode()).hexdigest()[:32] if ip else False


def _guard(func):
    """Refus métier → réponse ; le code machine (``reason``) sert à l'application.

    Pas de point de sauvegarde ici : une demande VALIDE sa transaction (la place
    dans la file et la trace d'un refus de quota doivent survivre à la requête),
    ce qui détruirait le point de sauvegarde sous nos pieds.
    """
    try:
        return func()
    except AiRefusal as e:
        return fail('AI_' + e.reason.upper(), str(e.args[0]), reason=e.reason)


class QuizAiApi(Controller):

    @route(API + '/ai/bank', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    def ai_bank(self, **kw):
        """État des chapitres demandés, et leurs questions si le téléphone n'a pas la dernière révision."""
        limited = rate_limited('ai-bank', RL_BANK)
        if limited:
            return limited
        env = request.env

        def _do():
            Chapter = env['bq.ai.chapter'].sudo()
            device = env['bq.ai.device'].sudo()._for_key(kw.get('device')) if kw.get('device') else None
            chapters = kw.get('chapters') if isinstance(kw.get('chapters'), list) else []
            return ok(chapters=Chapter._bank(chapters, kw.get('known')), quota=Chapter._quota_info(device))
        return _guard(_do)

    @route(API + '/ai/request', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    def ai_request(self, **kw):
        """Demande la rédaction d'un chapitre absent de la banque (quota gratuit du jour)."""
        limited = rate_limited('ai-request', RL_REQUEST)
        if limited:
            return limited
        env = request.env

        def _do():
            Chapter = env['bq.ai.chapter'].sudo()
            device = env['bq.ai.device'].sudo()._for_key(kw.get('device'))
            record = Chapter._request(device, _ip_hash(), kw.get('chapter'))
            return ok(chapter=record._payload(), quota=Chapter._quota_info(device))
        return _guard(_do)

    @route(API + '/ai/report', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    def ai_report(self, **kw):
        """Un lecteur signale une question (fausse, ambiguë, orientée…)."""
        limited = rate_limited('ai-report', RL_REPORT)
        if limited:
            return limited
        env = request.env

        def _do():
            device = env['bq.ai.device'].sudo()._for_key(kw.get('device'))
            question = env['bq.ai.question'].sudo()._report(device, kw.get('question'), kw.get('reason'),
                                                             kw.get('note'))
            return ok(withdrawn=question.state == 'withdrawn', message=_("Merci : la question sera relue."))
        return _guard(_do)
