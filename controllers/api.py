# -*- coding: utf-8 -*-
"""API de l'application Quiz Biblique pour le jeu en ligne."""
import logging

import psycopg2
from psycopg2 import errorcodes

from odoo import _, fields
from odoo.exceptions import AccessDenied, AccessError, UserError, ValidationError
from odoo.http import Controller, request, route
from odoo.tools import email_normalize

from odoo.addons.bus.websocket import WebsocketConnectionHandler

from ..models.bq_config import PREFIX, bq_param
from ..models.bq_match import BOUNDS, GameRefusal, MAX_PASSAGES, now_ts
from ..models.bq_notify import PUSH_APP_ID
from .common import (API, RL_CREATE, RL_OTP_PER_IDENTIFIER, RL_OTP_REQUEST, RL_OTP_VERIFY, RL_PUBLIC, RL_SEARCH,
                     account_route, arg_int, current_session, fail, guard, ok, player_route, rate_limited,
                     session_token)

_logger = logging.getLogger(__name__)

OTP_SCOPE = 'quiz_biblique'


def _phone_countries(env):
    raw = env['ir.config_parameter'].sudo().get_param(PREFIX + 'phone_countries') or 'CI'
    codes = [c.strip().upper() for c in raw.split(',') if len(c.strip()) == 2]
    return env['res.country'].sudo().search([('code', 'in', codes or ['CI'])])


def _identifier(kw):
    identifier = (kw.get('identifier') or '').strip()
    if identifier and '@' not in identifier and kw.get('country'):
        code = str(kw.get('country')).strip().upper()
        country = request.env['res.country'].sudo().search([('code', '=', code)], limit=1) if len(code) == 2 else None
        identifier = request.env['otp.code'].sudo()._norm_phone(identifier, country=country or None)
    return identifier


def _player_of(partner):
    return request.env['bq.player'].sudo().search([('partner_id', '=', partner.id)], limit=1)


def _me(partner, player):
    data = {'partner_id': partner.id, 'phone': partner.phone or '', 'player': None}
    if player:
        data['player'] = {
            'id': player.id, 'pseudo': player.pseudo, 'lang': player.lang,
            'channel': 'bq.player.%s' % player.channel_token,
            'match_id': player.match_id.id or None, 'match_code': player.match_id.code or None,
            'match_state': player.match_id.state or None,
            'tournament_id': player.tournament_id.id or None,
            'tournament_name': player.tournament_id.name or None,
            'stats': {'played': player.matches_played, 'won': player.matches_won,
                      'tournaments_won': player.tournaments_won},
        }
    return data


def _portal_user(partner):
    """Compte portail (notifications push) ; aucun accès au back-office."""
    partner = partner.sudo()
    user = partner.with_context(active_test=False).user_ids[:1]
    if user:
        return user if user.active else None
    login = partner.email or partner.phone
    if not login:
        return None
    Users = request.env['res.users'].sudo().with_context(no_reset_password=True)
    if Users.with_context(active_test=False).search_count([('login', '=', login)]):
        login = '%s.%s' % (login, partner.id)
    return Users.create({'name': partner.name or login, 'login': login, 'partner_id': partner.id,
                         'group_ids': [(6, 0, [request.env.ref('base.group_portal').id])]})


def _match_for(player, match_id, watch=False):
    match = request.env['bq.match'].sudo().browse(int(match_id or 0)).exists()
    if not match:
        raise AccessError(_("Match introuvable."))
    if not watch and not match.participant_ids.filtered(lambda p: p.player_id == player):
        raise AccessError(_("Vous ne participez pas à ce match."))
    return match


def _try_advance(match):
    """Fait avancer le match si personne d'autre ne le fait déjà (verrou sans attente)."""
    if match.state != 'running':
        return
    try:
        with request.env.cr.savepoint():
            request.env.cr.execute('SELECT id FROM bq_match WHERE id = %s FOR UPDATE NOWAIT', [match.id],
                                   log_exceptions=False)
            match.invalidate_recordset()
            match.participant_ids.invalidate_recordset()
            match.question_ids.invalidate_recordset()
            match._advance(now_ts())
    except psycopg2.errors.LockNotAvailable:
        pass  # un autre joueur est en train de le faire avancer
    except psycopg2.OperationalError as e:
        if e.pgcode not in (errorcodes.SERIALIZATION_FAILURE, errorcodes.LOCK_NOT_AVAILABLE):
            raise


def _match_payload(match, player=None):
    state = match._state()
    me = match.participant_ids.filtered(lambda p: player and p.player_id == player)[:1]
    return {'match': state, 'me': me.id or None, 'channel': 'bq.match.%s' % match.sudo().channel_token}


def _tournament_payload(tournament, player=None):
    state = tournament._state()
    entry = tournament._entry_of(player) if player else None
    return {'tournament': state, 'my_entry_id': entry.id if entry else None,
            'channel': 'bq.tournament.%s' % tournament.sudo().channel_token}


def _tournament_for(tournament_id):
    t = request.env['bq.tournament'].sudo().browse(int(tournament_id or 0)).exists()
    if not t:
        raise AccessError(_("Tournoi introuvable."))
    return t


def _team_for(team_id):
    team = request.env['bq.team'].sudo().browse(int(team_id or 0)).exists()
    if not team:
        raise AccessError(_("Équipe introuvable."))
    return team


def _player_by_id(player_id):
    p = request.env['bq.player'].sudo().browse(int(player_id or 0)).exists()
    if not p or not p.active:
        raise UserError(_("Joueur introuvable."))
    return p


class QuizGameApi(Controller):

    # ================================================================== configuration
    @route(API + '/config', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    def config(self, **kw):
        env = request.env
        return ok(
            server_now=int(now_ts() * 1000),
            websocket_version=WebsocketConnectionHandler._VERSION,
            limits={
                'max_players': bq_param(env, 'max_players'), 'team_max_size': bq_param(env, 'team_max_size'),
                'team_min_size': bq_param(env, 'team_min_size'),
                'tournament_max_entries': bq_param(env, 'tournament_max_entries'),
                'max_passages': MAX_PASSAGES,
                'question_seconds': list(BOUNDS['question_seconds']), 'hand_seconds': list(BOUNDS['hand_seconds']),
                'question_count': list(BOUNDS['question_count']),
            },
            defaults={
                'question_seconds': bq_param(env, 'question_seconds_default'),
                'hand_seconds': bq_param(env, 'hand_seconds_default'),
                'question_count': bq_param(env, 'question_count_default'),
                'forfeit_minutes': bq_param(env, 'forfeit_minutes_default'),
            },
        )

    # ================================================================== connexion
    @route(API + '/auth/countries', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    def countries(self, **kw):
        lang = 'en_US' if kw.get('lang') == 'en' else 'fr_FR'
        Country = request.env['res.country'].sudo()
        if request.env['res.lang']._lang_get(lang):
            Country = Country.with_context(lang=lang)
        countries = Country.browse(_phone_countries(request.env).ids).sorted('name')
        company = request.env.company.country_id
        default = company if company in countries else countries[:1]
        return ok(countries=[{'code': c.code, 'name': c.name, 'dial': str(c.phone_code)} for c in countries],
                  default=default.code)

    @route(API + '/auth/request-code', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    def request_code(self, **kw):
        refusal = rate_limited('otp_request', RL_OTP_REQUEST)
        if refusal:
            return refusal
        try:
            identifier = _identifier(kw)
        except ValidationError as error:
            return fail('BAD_IDENTIFIER', str(error))
        if not identifier:
            return fail('BAD_REQUEST', _("Indiquez votre numéro de téléphone."))
        if rate_limited('otp_request_id', RL_OTP_PER_IDENTIFIER, subject=identifier.lower().replace(' ', '')[:120]):
            return ok(sent=True, identifier=identifier)
        try:
            request.env['otp.code'].sudo()._issue(identifier, OTP_SCOPE)
        except ValidationError as error:
            return fail('BAD_IDENTIFIER', str(error))
        except Exception:  # noqa: BLE001 — un canal d'envoi en panne ne doit rien révéler
            _logger.exception("Quiz Biblique : envoi du code impossible")
        return ok(sent=True, identifier=identifier)

    @route(API + '/auth/verify-code', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    def verify_code(self, **kw):
        refusal = rate_limited('otp_verify', RL_OTP_VERIFY)
        if refusal:
            return refusal
        try:
            identifier = _identifier(kw)
        except ValidationError:
            return fail('BAD_CODE', _("Code incorrect ou expiré."))
        code = (kw.get('code') or '').strip()
        if not identifier or not code:
            return fail('BAD_REQUEST', _("Numéro et code sont requis."))
        try:
            record = request.env['otp.code'].sudo()._verify(identifier, OTP_SCOPE, code)
        except AccessDenied:
            return fail('BAD_CODE', _("Code incorrect ou expiré."))
        partner = self._partner_for(record)
        try:
            with request.env.cr.savepoint():
                _portal_user(partner)
        except Exception:  # noqa: BLE001 — sans compte portail, seulement pas de notification push
            _logger.warning("Quiz Biblique : compte portail impossible pour %s", partner.id)
        token = request.env['bq.api.session'].sudo()._issue(
            partner, device_label=(kw.get('device') or '')[:120] or 'Quiz Biblique')
        return ok(token=token, **_me(partner, _player_of(partner)))

    @staticmethod
    def _partner_for(otp_record):
        Partner = request.env['res.partner'].sudo()
        email, phone = otp_record.email or '', otp_record.phone or ''
        partner = Partner.browse()
        if email:
            partner = Partner.search([('email_normalized', '=', email_normalize(email) or email)], limit=1)
        if not partner and phone:
            partner = Partner.search([('phone_sanitized', '=', phone)], limit=1)
        if not partner and phone:
            partner = Partner.search([('phone', '=', phone)], limit=1)
        if partner:
            return partner
        return Partner.create({'name': phone or email, 'email': email or False, 'phone': phone or False})

    @route(API + '/auth/logout', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    def logout(self, **kw):
        device_token = (kw.get('device_token') or '').strip()
        session = current_session()
        if device_token and session and 'push.device.token' in request.env:
            request.env['push.device.token'].sudo().unregister(device_token, app_id=PUSH_APP_ID)
        request.env['bq.api.session'].sudo()._revoke(session_token())
        return ok()

    @route(API + '/me', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @account_route
    def me(self, partner, session, **kw):
        return ok(**_me(partner, _player_of(partner)))

    @route(API + '/devices', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @account_route
    def device(self, partner, session, **kw):
        token = (kw.get('device_token') or '').strip()
        if not token or 'push.device.token' not in request.env:
            return ok(registered=False)
        user = _portal_user(partner)
        if not user:
            return ok(registered=False)
        platform = kw.get('platform') if kw.get('platform') in ('android', 'ios', 'web') else 'android'
        request.env['push.device.token'].sudo().register(user.id, token, app_id=PUSH_APP_ID, platform=platform,
                                                        device_label=kw.get('device') or 'Quiz Biblique')
        return ok(registered=True)

    # ================================================================== profil de joueur
    @route(API + '/player/register', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @account_route
    def player_register(self, partner, session, **kw):
        """Devenir joueur : choisir son pseudo (une fois par compte)."""
        player = _player_of(partner)
        lang = kw.get('lang') if kw.get('lang') in ('fr', 'en') else 'fr'
        if player:
            if not player.active:
                player.write({'active': True, 'pseudo': kw.get('pseudo') or player.pseudo, 'lang': lang})
            return ok(**_me(partner, player))
        player = request.env['bq.player'].sudo().create(
            {'partner_id': partner.id, 'pseudo': kw.get('pseudo') or '', 'lang': lang})
        if partner.name in (partner.phone, partner.email):
            partner.sudo().name = player.pseudo
        return ok(**_me(partner, player))

    @route(API + '/player/update', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def player_update(self, player, **kw):
        vals = {}
        if kw.get('pseudo') and kw['pseudo'] != player.pseudo:
            vals['pseudo'] = kw['pseudo']
        if kw.get('lang') in ('fr', 'en'):
            vals['lang'] = kw['lang']
        if vals:
            player.write(vals)
        return ok(**_me(player.partner_id, player))

    @route(API + '/player/delete', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def player_delete(self, player, **kw):
        """Supprimer son compte joueur : quitte la partie en cours, pseudo effacé, sessions coupées."""
        if player.match_id:
            player.match_id.leave(player)
        if player.tournament_id:
            player.tournament_id.unregister(player)
        player.write({'active': False, 'match_id': False, 'tournament_id': False,
                      'pseudo': 'Joueur %s' % player.id})
        request.env['bq.api.session'].sudo().search([('partner_id', '=', player.partner_id.id)]).write(
            {'revoked': True})
        return ok()

    @route(API + '/player/search', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def player_search(self, player, **kw):
        refusal = rate_limited('search', RL_SEARCH, subject='p%s' % player.id)
        if refusal:
            return refusal
        from ..models.bq_player import norm_key
        key = norm_key(kw.get('q') or '')
        if len(key) < 2:
            return ok(players=[])
        found = request.env['bq.player'].sudo().search(
            [('pseudo_key', '=like', key + '%'), ('banned', '=', False), ('id', '!=', player.id)], limit=10)
        return ok(players=[dict(p._public(), busy=bool(p.match_id or p.tournament_id)) for p in found])

    @route(API + '/player/report', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def player_report(self, player, **kw):
        target = _player_by_id(kw.get('player_id'))
        if target == player:
            raise UserError(_("Vous ne pouvez pas vous signaler vous-même."))
        Report = request.env['bq.report'].sudo()
        if not Report.search_count([('player_id', '=', target.id), ('reporter_id', '=', player.id),
                                    ('pseudo', '=', target.pseudo)]):
            Report.create({'player_id': target.id, 'reporter_id': player.id, 'pseudo': target.pseudo,
                           'reason': (kw.get('reason') or '')[:200]})
        return ok()

    @route(API + '/player/history', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def player_history(self, player, **kw):
        parts = request.env['bq.match.participant'].sudo().search(
            [('player_id', '=', player.id), ('match_id.state', '=', 'finished')], limit=30, order='id desc')
        items = []
        for p in parts:
            m = p.match_id
            items.append({
                'match_id': m.id, 'code': m.code, 'mode': m.mode, 'kind': m.kind,
                'finished_at': fields.Datetime.to_string(m.finished_at) if m.finished_at else None,
                'passages_label': m.passages_label, 'rank': p.rank, 'score': p.score,
                'players': len(m.participant_ids), 'won': bool(
                    (m.winner_participant_id and m.winner_participant_id == p) or
                    (m.winner_team_id and p.team_id == m.winner_team_id)),
                'tournament': m.tournament_id.name or None,
            })
        return ok(matches=items)

    @route(API + '/invitations', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def invitations(self, player, **kw):
        lines = request.env['bq.team.member'].sudo().search([
            ('player_id', '=', player.id), ('state', '=', 'invited'), '|',
            ('team_id.match_id.state', '=', 'lobby'), ('team_id.tournament_id.state', '=', 'registration')])
        return ok(invitations=[{
            'team_id': line.team_id.id, 'team_name': line.team_id.name, 'captain': line.team_id.captain_id.pseudo,
            'match_id': line.team_id.match_id.id or None, 'match_code': line.team_id.match_id.code or None,
            'tournament_id': line.team_id.tournament_id.id or None,
            'tournament_name': line.team_id.tournament_id.name or None,
        } for line in lines])

    # ================================================================== codes, listes
    @route(API + '/passages/check', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def passages_check(self, player, **kw):
        """Aperçu avant création : libellés et nombre de questions possibles."""
        from ..game import questions as qgen
        lang = kw.get('lang') if kw.get('lang') in ('fr', 'en') else player.lang
        codes = kw.get('passages') or []
        if not isinstance(codes, list) or not codes:
            return ok(passages=[], available=0)
        labels = []
        for code in codes[:MAX_PASSAGES]:
            try:
                labels.append({'code': str(code), 'label': qgen.passage_label(qgen.parse_passage(str(code)), lang)})
            except ValueError as e:
                return fail('BAD_PASSAGE', str(e), code=str(code))
        available = qgen.available_count([p['code'] for p in labels], lang, cap=BOUNDS['question_count'][1])
        return ok(passages=labels, available=available)

    @route(API + '/join', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def join_code(self, player, **kw):
        """Un code (saisi, QR, lien) : rejoint le match, ou ouvre la fiche du tournoi."""
        code = (kw.get('code') or '').strip().upper().replace(' ', '')
        match = request.env['bq.match'].sudo().search([('code', '=', code)], limit=1)
        if match:
            if match.kind == 'duel':
                if not match.participant_ids.filtered(lambda p: p.player_id == player):
                    raise AccessError(_("Ce duel de tournoi est réservé à ses joueurs."))
            elif not match.participant_ids.filtered(lambda p: p.player_id == player and not p.left):
                match.join(player)
            return ok(kind='match', **_match_payload(match, player))
        tournament = request.env['bq.tournament'].sudo().search([('code', '=', code)], limit=1)
        if tournament:
            return ok(kind='tournament', **_tournament_payload(tournament, player))
        return fail('UNKNOWN_CODE', _("Aucun match ni tournoi avec ce code."))

    @route(API + '/open', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def open_games(self, player, **kw):
        """Matchs ouverts et tournois aux inscriptions ouvertes (visibles dans la liste)."""
        matches = request.env['bq.match'].sudo().search(
            [('state', '=', 'lobby'), ('kind', '=', 'free'), ('is_public', '=', True)], limit=50)
        tournaments = request.env['bq.tournament'].sudo().search(
            [('state', '=', 'registration'), ('is_public', '=', True)], limit=20)
        return ok(
            matches=[{
                'id': m.id, 'code': m.code, 'mode': m.mode, 'lang': m.lang, 'passages_label': m.passages_label,
                'creator': m.creator_id.pseudo or '', 'players': m.player_count, 'max_players': m.max_players,
                'question_count': m.question_count, 'question_seconds': m.question_seconds,
            } for m in matches if m.player_count < m.max_players],
            tournaments=[{
                'id': t.id, 'code': t.code, 'name': t.name, 'mode': t.mode, 'lang': t.lang,
                'passages_label': t.passages_label, 'organizer': t.organizer_id.pseudo or '',
                'entries': t.entry_count, 'max_entries': t.max_entries, 'min_entries': t.min_entries,
            } for t in tournaments],
        )

    # ================================================================== match
    @route(API + '/match/create', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def match_create(self, player, **kw):
        refusal = rate_limited('create', RL_CREATE, subject='p%s' % player.id)
        if refusal:
            return refusal
        match = request.env['bq.match'].sudo().create_free(player, kw)
        return ok(**_match_payload(match, player))

    @route(API + '/match/state', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def match_state(self, player, **kw):
        match = _match_for(player, kw.get('match_id'))
        _try_advance(match)
        match.participant_ids.filtered(lambda p: p.player_id == player)._seen()
        if kw.get('version') and int(kw['version']) == match.version:
            return ok(unchanged=True, version=match.version, server_now=int(now_ts() * 1000))
        return ok(**_match_payload(match, player))

    @route(API + '/match/tick', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def match_tick(self, player, **kw):
        """Envoyé par les téléphones à chaque échéance (fin de question, de main, d'affichage)."""
        return self.match_state(**kw)

    @route(API + '/match/watch', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    def match_watch(self, **kw):
        """Écran de projection : l'état d'un match par son code, sans compte (rien de secret n'y figure)."""
        refusal = rate_limited('watch', RL_PUBLIC)
        if refusal:
            return refusal
        code = (kw.get('code') or '').strip().upper().replace(' ', '')

        def _do():
            match = request.env['bq.match'].sudo().search([('code', '=', code)], limit=1)
            if not match:
                raise GameRefusal('UNKNOWN_CODE', _("Aucun match avec ce code."))
            _try_advance(match)
            return ok(**_match_payload(match))
        return guard(_do)

    @route(API + '/match/buzz', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def match_buzz(self, player, **kw):
        match = _match_for(player, kw.get('match_id'))
        match.buzz(player)
        return ok(**_match_payload(match, player))

    @route(API + '/match/answer', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def match_answer(self, player, **kw):
        match = _match_for(player, kw.get('match_id'))
        correct = match.answer(player, kw.get('choice'))
        return ok(correct=correct, **_match_payload(match, player))

    @route(API + '/match/leave', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def match_leave(self, player, **kw):
        match = _match_for(player, kw.get('match_id'))
        if match.kind == 'duel' and match.tournament_id:
            # Quitter un duel, c'est abandonner le tournoi.
            match.tournament_id.unregister(player)
        else:
            match.leave(player)
        return ok()

    @route(API + '/match/kick', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def match_kick(self, player, **kw):
        match = _match_for(player, kw.get('match_id'))
        match.kick(player, arg_int(kw, 'participant_id'))
        return ok(**_match_payload(match, player))

    @route(API + '/match/settings', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def match_settings(self, player, **kw):
        match = _match_for(player, kw.get('match_id'))
        values = {k: kw.get(k) for k in ('lang', 'passages', 'question_count', 'question_seconds', 'hand_seconds',
                                          'is_public') if k in kw}
        match.update_settings(player, values)
        return ok(**_match_payload(match, player))

    @route(API + '/match/start', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def match_start(self, player, **kw):
        match = _match_for(player, kw.get('match_id'))
        match.start(player)
        return ok(**_match_payload(match, player))

    @route(API + '/match/history', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def match_history(self, player, **kw):
        match = _match_for(player, kw.get('match_id'))
        if match.state not in ('finished', 'cancelled'):
            raise UserError(_("Le match n'est pas terminé."))
        return ok(questions=match._history(), **_match_payload(match, player))

    @route(API + '/match/rematch', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def match_rematch(self, player, **kw):
        """« Revanche » : nouveau match aux mêmes réglages ; les autres joueurs le rejoignent par son code."""
        old = _match_for(player, kw.get('match_id'))
        if old.state != 'finished' or old.kind != 'free':
            raise UserError(_("Revanche possible à la fin d'un match libre."))
        values = {'mode': old.mode, 'lang': old.lang, 'passages': [c for c, _l in (old.passage_codes or [])],
                  'question_count': old.question_count, 'question_seconds': old.question_seconds,
                  'hand_seconds': old.hand_seconds, 'is_public': False, 'max_players': old.max_players}
        match = request.env['bq.match'].sudo().create_free(player, values)
        others = old.participant_ids.player_id - player
        request.env['bq.notify']._players_event(others, 'rematch', {'code': match.code, 'from_match_id': old.id},
                                                _("Revanche !"),
                                                _("%(p)s propose une revanche : code %(c)s.", p=player.pseudo,
                                                  c=match.code))
        return ok(**_match_payload(match, player))

    @route(API + '/match/team/create', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def match_team_create(self, player, **kw):
        match = _match_for(player, kw.get('match_id'))
        match.create_team(player, kw.get('name'))
        return ok(**_match_payload(match, player))

    # ================================================================== équipes
    def _team_result(self, team, player):
        if team.exists() and team.tournament_id:
            return ok(**_tournament_payload(team.tournament_id, player))
        if team.exists() and team.match_id:
            return ok(**_match_payload(team.match_id, player))
        return ok()

    def _lock_context(self, team):
        holder = team.match_id or team.tournament_id
        holder._lock()
        return holder

    @route(API + '/team/invite', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def team_invite(self, player, **kw):
        team = _team_for(kw.get('team_id'))
        holder = self._lock_context(team)
        team.invite(player, _player_by_id(kw.get('player_id')))
        holder._bump()
        return self._team_result(team, player)

    @route(API + '/team/request', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def team_request(self, player, **kw):
        team = _team_for(kw.get('team_id'))
        holder = self._lock_context(team)
        team.request(player)
        holder._bump()
        return self._team_result(team, player)

    @route(API + '/team/accept', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def team_accept(self, player, **kw):
        """L'invité accepte (sans ``player_id``), ou le capitaine accepte la demande de ``player_id``."""
        team = _team_for(kw.get('team_id'))
        holder = self._lock_context(team)
        target = _player_by_id(kw['player_id']) if kw.get('player_id') else player
        team.accept(player, target)
        holder._bump()
        return self._team_result(team, player)

    @route(API + '/team/decline', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def team_decline(self, player, **kw):
        team = _team_for(kw.get('team_id'))
        holder = self._lock_context(team)
        target = _player_by_id(kw['player_id']) if kw.get('player_id') else player
        team.decline(player, target)
        holder._bump()
        return self._team_result(team, player)

    @route(API + '/team/leave', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def team_leave(self, player, **kw):
        team = _team_for(kw.get('team_id'))
        holder = self._lock_context(team)
        team.leave(player)
        holder._bump()
        return self._team_result(team, player) if team.exists() else ok()

    @route(API + '/team/rename', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def team_rename(self, player, **kw):
        team = _team_for(kw.get('team_id'))
        holder = self._lock_context(team)
        team._check_open()
        team._check_captain(player)
        name = (kw.get('name') or '').strip()[:40]
        if len(name) < 2:
            raise UserError(_("Nom d'équipe trop court."))
        team.name = name
        holder._bump()
        return self._team_result(team, player)

    # ================================================================== tournois
    @route(API + '/tournament/create', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def tournament_create(self, player, **kw):
        refusal = rate_limited('create', RL_CREATE, subject='p%s' % player.id)
        if refusal:
            return refusal
        t = request.env['bq.tournament'].sudo().create_tournament(player, kw)
        return ok(**_tournament_payload(t, player))

    @route(API + '/tournament/state', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def tournament_state(self, player, **kw):
        t = _tournament_for(kw.get('tournament_id'))
        if t.state == 'running':
            now = now_ts()
            if t.duel_ids.filtered(lambda d: d.state == 'ready_wait' and d.deadline and now >= d.deadline):
                t._lock()
                t._check_deadlines(now)
        return ok(**_tournament_payload(t, player))

    @route(API + '/tournament/mine', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def tournament_mine(self, player, **kw):
        """Tournois organisés ou joués (récents)."""
        entries = request.env['bq.tournament.entry'].sudo().search(
            ['|', ('player_id', '=', player.id), ('team_id.member_ids.player_id', '=', player.id)],
            limit=20, order='id desc')
        organized = request.env['bq.tournament'].sudo().search([('organizer_id', '=', player.id)], limit=20)
        tournaments = (entries.tournament_id | organized).sorted('id', reverse=True)[:20]
        return ok(tournaments=[{
            'id': t.id, 'code': t.code, 'name': t.name, 'mode': t.mode, 'state': t.state,
            'organizer': t.organizer_id.pseudo or '', 'entries': t.entry_count,
            'winner': t.winner_entry_id.display_name if t.winner_entry_id else None,
        } for t in tournaments])

    @route(API + '/tournament/register', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def tournament_register(self, player, **kw):
        t = _tournament_for(kw.get('tournament_id'))
        t.register(player)
        return ok(**_tournament_payload(t, player))

    @route(API + '/tournament/unregister', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def tournament_unregister(self, player, **kw):
        t = _tournament_for(kw.get('tournament_id'))
        t.unregister(player)
        return ok(**_tournament_payload(t, player))

    @route(API + '/tournament/team/create', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def tournament_team_create(self, player, **kw):
        t = _tournament_for(kw.get('tournament_id'))
        t.create_team(player, kw.get('name'))
        return ok(**_tournament_payload(t, player))

    @route(API + '/tournament/start', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def tournament_start(self, player, **kw):
        t = _tournament_for(kw.get('tournament_id'))
        t.start(player)
        return ok(**_tournament_payload(t, player))

    @route(API + '/tournament/cancel', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def tournament_cancel(self, player, **kw):
        t = _tournament_for(kw.get('tournament_id'))
        t.cancel(player)
        return ok(**_tournament_payload(t, player))

    @route(API + '/tournament/round-passages', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def tournament_round_passages(self, player, **kw):
        t = _tournament_for(kw.get('tournament_id'))
        t.set_round_passages(player, arg_int(kw, 'round'), kw.get('passages') or [])
        return ok(**_tournament_payload(t, player))

    @route(API + '/duel/ready', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    @player_route
    def duel_ready(self, player, **kw):
        duel = request.env['bq.tournament.duel'].sudo().browse(arg_int(kw, 'duel_id')).exists()
        if not duel:
            raise AccessError(_("Duel introuvable."))
        duel.ready(player)
        return ok(**_tournament_payload(duel.tournament_id, player))
