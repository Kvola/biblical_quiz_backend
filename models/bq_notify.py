# -*- coding: utf-8 -*-
"""Prévenir un joueur : sur son canal temps réel (application ouverte) et par
notification push (application fermée).

Une notification qui échoue n'annule jamais l'action qui l'a déclenchée.
Le push part APRÈS la validation de la transaction : un appel à Firebase ne
doit ni ralentir ni bloquer une action de jeu, et on ne prévient pas d'une
action annulée.
"""
import logging

from odoo import _, api, models

_logger = logging.getLogger(__name__)

PUSH_APP_ID = 'quiz_biblique'


class BqNotify(models.AbstractModel):
    _name = 'bq.notify'
    _description = "Notifications du Quiz Biblique"

    @api.model
    def _players_event(self, players, event, payload, title=None, body=None):
        players = players.sudo().filtered('active')
        if not players:
            return
        bus = self.env['bus.bus'].sudo()
        for player in players:
            bus._sendone('bq.player.%s' % player.channel_token, 'bq_player',
                         dict(payload, event=event, title=title or '', body=body or ''))
        if title and 'push.notification' in self.env:
            users = players.partner_id.user_ids.filtered('active')
            if users:
                self._push_after_commit(users.ids, title, body or '', dict(payload, event=event))

    def _push_after_commit(self, user_ids, title, body, data):
        cr = self.env.cr
        registry = self.env.registry
        uid = self.env.uid
        payload = {k: str(v) for k, v in data.items()}

        @cr.postcommit.add
        def _send():
            try:
                with registry.cursor() as new_cr:
                    env = api.Environment(new_cr, uid, {})
                    env['push.notification'].sudo().send(
                        user_ids, title, body, data=payload, app_id=PUSH_APP_ID,
                        source_module='biblical_quiz_backend')
            except Exception:  # noqa: BLE001 — la notification est accessoire
                _logger.exception("Quiz Biblique : notification non envoyée (%s)", title)

    @api.model
    def _invitation(self, team, player):
        holder = team.match_id or team.tournament_id
        where = (_("le match %s", team.match_id.code) if team.match_id
                 else _("le tournoi « %s »", team.tournament_id.name))
        self._players_event(player, 'team_invitation', {
            'team_id': team.id, 'team_name': team.name, 'captain': team.captain_id.pseudo,
            'match_id': team.match_id.id or '', 'tournament_id': team.tournament_id.id or '',
            'code': holder.code,
        }, _("Invitation dans une équipe"),
            _("%(captain)s vous invite dans l'équipe « %(team)s » pour %(where)s.",
              captain=team.captain_id.pseudo, team=team.name, where=where))

    @api.model
    def _join_request(self, team, player):
        self._players_event(team.captain_id, 'team_request', {
            'team_id': team.id, 'team_name': team.name, 'player_id': player.id, 'pseudo': player.pseudo,
            'match_id': team.match_id.id or '', 'tournament_id': team.tournament_id.id or '',
        }, _("Demande pour votre équipe"),
            _("%(pseudo)s demande à entrer dans l'équipe « %(team)s ».", pseudo=player.pseudo, team=team.name))
