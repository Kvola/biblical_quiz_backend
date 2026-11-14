# -*- coding: utf-8 -*-
"""Équipes : créées par un capitaine qui invite les membres.

Une équipe vit le temps d'UN match par équipes (``match_id``) ou d'UN
tournoi entre équipes (``tournament_id``) — jamais les deux.

Deux chemins mènent dans une équipe, tous deux sous le contrôle du
capitaine : il **invite** un joueur (qui accepte), ou un joueur **demande**
à entrer (et le capitaine accepte).
"""
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from .bq_config import bq_param


class BqTeam(models.Model):
    _name = 'bq.team'
    _description = "Équipe (Quiz Biblique)"
    _order = 'id'

    name = fields.Char("Nom", required=True)
    captain_id = fields.Many2one('bq.player', "Capitaine", required=True, ondelete='restrict')
    match_id = fields.Many2one('bq.match', "Match", ondelete='cascade', index=True)
    tournament_id = fields.Many2one('bq.tournament', "Tournoi", ondelete='cascade', index=True)
    member_ids = fields.One2many('bq.team.member', 'team_id', "Membres et invitations")
    member_count = fields.Integer("Membres", compute='_compute_member_count')

    _one_context = models.Constraint(
        'CHECK ((match_id IS NULL) <> (tournament_id IS NULL))',
        "Une équipe appartient à un match OU à un tournoi.")

    @api.depends('member_ids.state')
    def _compute_member_count(self):
        for team in self:
            team.member_count = len(team.member_ids.filtered(lambda m: m.state == 'member'))

    # ------------------------------------------------------------------ lecture
    def _members(self):
        return self.member_ids.filtered(lambda m: m.state == 'member').player_id

    def _context(self):
        return self.match_id or self.tournament_id

    def _public(self, with_pending=True):
        self.ensure_one()
        data = {
            'id': self.id,
            'name': self.name,
            'captain_id': self.captain_id.id,
            'members': [p._public() for p in self._members()],
        }
        if with_pending:
            data['pending'] = [
                {'player_id': m.player_id.id, 'pseudo': m.player_id.pseudo, 'kind': m.state}
                for m in self.member_ids if m.state in ('invited', 'requested')
            ]
        return data

    # ------------------------------------------------------------------ règles
    def _check_open(self):
        holder = self._context()
        open_state = 'lobby' if self.match_id else 'registration'
        if holder.state != open_state:
            raise UserError(_("Les équipes ne peuvent plus changer : la partie a commencé."))

    def _check_captain(self, player):
        if self.captain_id != player:
            raise AccessError(_("Seul le capitaine de l'équipe peut faire cela."))

    def _check_room(self):
        if self.member_count >= bq_param(self.env, 'team_max_size'):
            raise UserError(_("L'équipe est complète (%s joueurs au plus).", bq_param(self.env, 'team_max_size')))

    def _line(self, player):
        return self.member_ids.filtered(lambda m: m.player_id == player)[:1]

    def _player_team(self, player):
        """L'équipe dont ``player`` est MEMBRE dans le même match ou tournoi (vide sinon)."""
        domain = [('player_id', '=', player.id), ('state', '=', 'member')]
        if self.match_id:
            domain.append(('team_id.match_id', '=', self.match_id.id))
        else:
            domain.append(('team_id.tournament_id', '=', self.tournament_id.id))
        return self.env['bq.team.member'].sudo().search(domain, limit=1).team_id

    # ------------------------------------------------------------------ actions
    def invite(self, captain, player):
        """Le capitaine invite ``player`` (par son pseudo)."""
        self.ensure_one()
        self._check_open()
        self._check_captain(captain)
        self._check_room()
        if player == captain:
            raise UserError(_("Vous êtes déjà dans l'équipe."))
        player._ensure_can_play()
        if self._player_team(player):
            raise UserError(_("%s est déjà dans une équipe.", player.pseudo))
        line = self._line(player)
        if line.state == 'requested':
            return self.accept(captain, player)
        if not line:
            self.env['bq.team.member'].sudo().create({'team_id': self.id, 'player_id': player.id, 'state': 'invited'})
            self.env['bq.notify']._invitation(self, player)
        return self

    def request(self, player):
        """``player`` demande à entrer dans l'équipe."""
        self.ensure_one()
        self._check_open()
        self._check_room()
        player._ensure_can_play()
        if self._player_team(player):
            raise UserError(_("Vous êtes déjà dans une équipe."))
        line = self._line(player)
        if line.state == 'invited':
            return self.accept(player, player)
        if not line:
            self.env['bq.team.member'].sudo().create({'team_id': self.id, 'player_id': player.id, 'state': 'requested'})
            self.env['bq.notify']._join_request(self, player)
        return self

    def accept(self, actor, player):
        """Accepter : l'invité accepte une invitation, ou le capitaine accepte une demande."""
        self.ensure_one()
        self._check_open()
        line = self._line(player)
        if not line or line.state == 'member':
            raise UserError(_("Aucune invitation ni demande en attente."))
        if line.state == 'invited' and actor != player:
            raise AccessError(_("Seul le joueur invité peut accepter l'invitation."))
        if line.state == 'requested' and actor != self.captain_id:
            raise AccessError(_("Seul le capitaine accepte une demande."))
        self._check_room()
        if self._player_team(player):
            line.sudo().unlink()
            raise UserError(_("%s est déjà dans une équipe.", player.pseudo))
        # Entrer dans l'équipe, c'est entrer dans le match ou le tournoi.
        self._context()._team_member_joined(self, player)
        line.sudo().state = 'member'
        # Ses autres invitations et demandes dans ce même match ou tournoi deviennent sans objet.
        others = self.env['bq.team.member'].sudo().search([
            ('player_id', '=', player.id), ('state', 'in', ('invited', 'requested')), ('id', '!=', line.id),
            ('team_id.match_id', '=', self.match_id.id) if self.match_id
            else ('team_id.tournament_id', '=', self.tournament_id.id)])
        others.unlink()
        return self

    def decline(self, actor, player):
        """Refuser une invitation (l'invité) ou une demande (le capitaine), ou retirer un membre (le capitaine)."""
        self.ensure_one()
        self._check_open()
        line = self._line(player)
        if not line:
            return self
        if actor != player and actor != self.captain_id:
            raise AccessError(_("Seul le capitaine peut retirer un membre."))
        if line.state == 'member':
            return self.leave(player)
        line.sudo().unlink()
        return self

    def leave(self, player):
        """``player`` quitte l'équipe. Le capitaine qui part passe la main ; une équipe vide disparaît."""
        self.ensure_one()
        self._check_open()
        line = self._line(player)
        if not line:
            return self
        was_member = line.state == 'member'
        line.sudo().unlink()
        if was_member:
            self._context()._team_member_left(self, player)
        if player == self.captain_id:
            heirs = self.member_ids.filtered(lambda m: m.state == 'member').sorted('id')
            if heirs:
                self.sudo().captain_id = heirs[0].player_id
            else:
                self._dissolve()
        return self

    def _dissolve(self):
        """Supprime l'équipe et ses invitations (ses membres restent dans le match ou le tournoi, sans équipe)."""
        for team in self:
            holder = team._context()
            for member in team._members():
                holder._team_member_left(team, member)
            holder._team_dissolved(team)
            team.sudo().unlink()


class BqTeamMember(models.Model):
    _name = 'bq.team.member'
    _description = "Membre d'une équipe"
    _order = 'id'

    team_id = fields.Many2one('bq.team', required=True, ondelete='cascade', index=True)
    player_id = fields.Many2one('bq.player', required=True, ondelete='cascade', index=True)
    state = fields.Selection([
        ('invited', "Invité"),
        ('requested', "Demande"),
        ('member', "Membre"),
    ], required=True, default='invited')

    _uniq = models.Constraint('UNIQUE (team_id, player_id)', "Ce joueur figure déjà dans l'équipe.")
