# -*- coding: utf-8 -*-
"""Conventions de l'API du jeu en ligne (mêmes que les autres applications Kvola).

Routes ``type='jsonrpc'``, ``POST`` ; le statut métier voyage dans la
réponse : ``{"success": true, ...}`` ou ``{"success": false, "error":
{"code", "message"}}``. ``AUTH_REQUIRED`` renvoie l'application à la
connexion ; ``AUTH_010`` = trop de tentatives.

Les routes travaillent en ``sudo`` et filtrent par appartenance (le joueur
ne voit que ses matchs, ses équipes, ses invitations).
"""
import functools
import logging

from odoo import _
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request

from odoo.addons.otp_auth.tools.client_ip import client_ip

from ..models.bq_match import GameRefusal

_logger = logging.getLogger(__name__)

API = '/quiz-biblique/api/v1'

# (requêtes, fenêtre, blocage) en secondes
RL_OTP_REQUEST = (8, 600, 600)
RL_OTP_VERIFY = (15, 600, 600)
RL_OTP_PER_IDENTIFIER = (6, 3600, 3600)
RL_CREATE = (20, 600, 600)        # créations de matchs / tournois par session
RL_SEARCH = (60, 60, 60)          # recherche de pseudos par session
RL_PUBLIC = (120, 60, 60)         # lecture publique (écran de projection) par IP

CODE_UNAUTHORIZED = 'AUTH_REQUIRED'
CODE_RATE_LIMITED = 'AUTH_010'


def ok(**payload):
    return dict(payload, success=True)


def fail(code, message, **details):
    response = {'success': False, 'error': {'code': code, 'message': message},
                'error_code': code, 'message': message}
    if details:
        response['error']['details'] = details
    return response


def rate_limited(bucket, limits, subject=None):
    key = "quiz-biblique:%s:%s" % (bucket, subject or client_ip(request))
    blocked, seconds = request.env['otp.rate.limit'].sudo().check_and_record(key, *limits)
    if blocked:
        return fail(CODE_RATE_LIMITED, _("Trop de tentatives. Réessayez dans %s minute(s).", max(1, round(seconds / 60))),
                    retry_after=seconds)
    return None


def session_token():
    header = request.httprequest.headers.get('Authorization', '')
    return header[7:].strip() if header.lower().startswith('bearer ') else ''


def current_session():
    return request.env['bq.api.session'].sudo()._resolve_session(session_token())


def guard(func, *args, **kwargs):
    """Traduit les refus métier en réponses, sans fuite de détails internes."""
    try:
        with request.env.cr.savepoint():
            return func(*args, **kwargs)
    except GameRefusal as e:
        return fail(e.code, str(e.args[0]) if e.args else _("Action impossible."))
    except AccessError as e:
        return fail('FORBIDDEN', str(e.args[0]) if e.args else _("Accès refusé."))
    except (UserError, ValidationError) as e:
        return fail('REFUSED', str(e.args[0]) if e.args else _("Action impossible."))


def account_route(func):
    """Session valable ; injecte ``partner`` (le joueur n'a peut-être pas encore de pseudo)."""
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        session = current_session()
        if not session:
            return fail(CODE_UNAUTHORIZED, _("Session expirée. Reconnectez-vous."))
        kwargs.pop('partner', None)
        kwargs.pop('session', None)
        return guard(func, self, *args, partner=session.partner_id, session=session, **kwargs)
    return wrapper


def player_route(func):
    """Session valable ET profil de joueur ; injecte ``player``."""
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        session = current_session()
        if not session:
            return fail(CODE_UNAUTHORIZED, _("Session expirée. Reconnectez-vous."))
        player = request.env['bq.player'].sudo().search([('partner_id', '=', session.partner_id.id)], limit=1)
        if not player:
            return fail('NO_PLAYER', _("Choisissez d'abord votre pseudo."))
        if player.banned:
            return fail('BANNED', _("Votre compte joueur est suspendu.") + (
                ' ' + player.ban_reason if player.ban_reason else ''))
        kwargs.pop('player', None)
        return guard(func, self, *args, player=player, **kwargs)
    return wrapper


def arg_int(kw, key):
    try:
        return int(kw.get(key))
    except (TypeError, ValueError) as e:
        raise UserError(_("« %s » doit être un nombre entier.", key)) from e
