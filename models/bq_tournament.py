# -*- coding: utf-8 -*-
"""Tournoi à élimination directe : des duels jusqu'au vainqueur final.

* Un tournoi oppose soit des joueurs, soit des équipes — jamais un mélange.
* Inscriptions ouvertes jusqu'au lancement par l'organisateur (entre le
  minimum et le maximum de participants qu'il a fixés, dans la limite du
  serveur).
* Au lancement : tirage au sort du tableau. Si le nombre de participants
  n'est pas une puissance de deux, certains sont qualifiés d'office au
  premier tour (« exempt »), répartis dans le tableau.
* Chaque duel s'ouvre dès que ses deux adversaires sont connus. Les deux camps
  confirment « Prêt » ; le match démarre alors. Passé le délai de forfait,
  le camp prêt gagne ; si aucun ne l'est, les deux sont éliminés et
  l'adversaire suivant passe sans jouer.
* Égalité dans un duel : questions décisives (voir bq.match).
* Un joueur éliminé est libéré : il peut rejoindre une autre partie.
"""
import logging
import math
import random
import secrets
import time

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from .bq_config import bq_param
from .bq_match import GameRefusal, check_passages, clamp_setting, ms, new_code, now_ts

_logger = logging.getLogger(__name__)


class BqTournament(models.Model):
    _name = 'bq.tournament'
    _description = "Tournoi (Quiz Biblique)"
    _order = 'id desc'

    name = fields.Char("Nom", required=True)
    code = fields.Char("Code", required=True, readonly=True, index=True, copy=False)
    channel_token = fields.Char("Canal", required=True, readonly=True, copy=False, groups='base.group_system',
                                default=lambda self: secrets.token_urlsafe(24))
    organizer_id = fields.Many2one('bq.player', "Organisateur", ondelete='set null')
    mode = fields.Selection([('solo', "Entre joueurs"), ('team', "Entre équipes")], "Formule", required=True,
                            default='solo')
    state = fields.Selection([
        ('registration', "Inscriptions"),
        ('running', "En cours"),
        ('finished', "Terminé"),
        ('cancelled', "Annulé"),
    ], "État", required=True, default='registration', index=True)
    is_public = fields.Boolean("Visible dans la liste", default=True)
    min_entries = fields.Integer("Participants au moins", required=True, default=4)
    max_entries = fields.Integer("Participants au plus", required=True, default=16)
    lang = fields.Selection([('fr', "Français (Segond 1910)"), ('en', "English (KJV)")], "Langue",
                            required=True, default='fr')
    passage_codes = fields.Json("Passages (codes)")
    passages_label = fields.Char("Passages")
    round_passages = fields.Json("Passages par tour",
                                 help="{tour: [[code, libellé], …]} : remplace les passages du tournoi pour ce tour.")
    question_count = fields.Integer("Questions par duel", required=True, default=10)
    question_seconds = fields.Integer("Temps par question (s)", required=True, default=30)
    hand_seconds = fields.Integer("Durée de la main (s)", required=True, default=5)
    forfeit_minutes = fields.Integer("Forfait après (min)", required=True, default=10)
    rounds = fields.Integer("Nombre de tours", readonly=True)
    version = fields.Integer(default=1, readonly=True)
    winner_entry_id = fields.Many2one('bq.tournament.entry', "Vainqueur", readonly=True)
    started_at = fields.Datetime("Commencé le", readonly=True)
    finished_at = fields.Datetime("Terminé le", readonly=True)

    entry_ids = fields.One2many('bq.tournament.entry', 'tournament_id', "Participants")
    duel_ids = fields.One2many('bq.tournament.duel', 'tournament_id', "Duels")
    team_ids = fields.One2many('bq.team', 'tournament_id', "Équipes")
    match_ids = fields.One2many('bq.match', 'tournament_id', "Matchs")
    entry_count = fields.Integer("Inscrits", compute='_compute_entry_count')

    _code_uniq = models.Constraint('UNIQUE (code)', "Ce code existe déjà.")

    @api.depends('entry_ids')
    def _compute_entry_count(self):
        for t in self:
            t.entry_count = len(t.entry_ids)

    # ================================================================== outils
    def _lock(self):
        self.ensure_one()
        self.env.cr.execute('SELECT id FROM bq_tournament WHERE id = %s FOR UPDATE', [self.id])
        self.invalidate_recordset()
        self.entry_ids.invalidate_recordset()
        self.duel_ids.invalidate_recordset()
        self.team_ids.invalidate_recordset()
        return self

    def _bump(self):
        for t in self:
            t.sudo().version = t.version + 1
        cr = self.env.cr
        pending = cr.precommit.data.setdefault('bq.tournament.broadcast', set())
        first = not pending
        pending.update(self.ids)
        if first:
            env = self.env

            @cr.precommit.add
            def _broadcast():
                ids = cr.precommit.data.pop('bq.tournament.broadcast', set())
                for t in env['bq.tournament'].sudo().browse(sorted(ids)).exists():
                    env['bus.bus'].sudo()._sendone('bq.tournament.%s' % t.channel_token, 'bq_tournament', t._state())

    def _check_organizer(self, player):
        if self.organizer_id != player:
            raise AccessError(_("Seul l'organisateur du tournoi peut faire cela."))

    def _check_registration(self):
        if self.state != 'registration':
            raise GameRefusal('NOT_REGISTRATION', _("Les inscriptions sont closes."))

    def _entry_of(self, player):
        """L'inscription de ``player`` (joueur inscrit, ou membre d'une équipe inscrite)."""
        for entry in self.entry_ids:
            if entry.player_id == player or (entry.team_id and player in entry.team_id._members()):
                return entry
        return self.env['bq.tournament.entry']

    def _passages_for_round(self, round_no):
        override = (self.round_passages or {}).get(str(round_no))
        return override or self.passage_codes or []

    # ================================================================== état public
    def _state(self):
        self.ensure_one()
        t = self.sudo()
        entries = []
        for e in t.entry_ids.sorted('id'):
            data = {'id': e.id, 'name': e.display_name, 'state': e.state, 'eliminated_round': e.eliminated_round or None}
            if e.player_id:
                data['player'] = e.player_id._public()
            if e.team_id:
                data['team'] = e.team_id._public()
            entries.append(data)
        duels = []
        for d in t.duel_ids.sorted(lambda d: (d.round, d.position)):
            duels.append({
                'id': d.id, 'round': d.round, 'position': d.position, 'state': d.state, 'result': d.result or None,
                'entry_a_id': d.entry_a_id.id or None, 'entry_b_id': d.entry_b_id.id or None,
                'ready_a': d.ready_a, 'ready_b': d.ready_b, 'winner_entry_id': d.winner_entry_id.id or None,
                'match_id': d.match_id.id or None, 'match_code': d.match_id.code or None,
                'deadline': ms(d.deadline) if d.state == 'ready_wait' else None,
                'score_a': d.score_a, 'score_b': d.score_b,
            })
        return {
            'id': t.id, 'code': t.code, 'name': t.name, 'version': t.version, 'server_now': ms(now_ts()),
            'mode': t.mode, 'state': t.state, 'organizer_id': t.organizer_id.id or None,
            'organizer_pseudo': t.organizer_id.pseudo or '', 'is_public': t.is_public,
            'lang': t.lang, 'passages_label': t.passages_label or '',
            'passages': [{'code': c, 'label': lbl} for c, lbl in (t.passage_codes or [])],
            'round_passages': {r: [{'code': c, 'label': lbl} for c, lbl in v]
                               for r, v in (t.round_passages or {}).items()},
            'settings': {'question_count': t.question_count, 'question_seconds': t.question_seconds,
                         'hand_seconds': t.hand_seconds, 'forfeit_minutes': t.forfeit_minutes,
                         'min_entries': t.min_entries, 'max_entries': t.max_entries,
                         'team_min_size': bq_param(self.env, 'team_min_size'),
                         'team_max_size': bq_param(self.env, 'team_max_size')},
            'rounds': t.rounds, 'entries': entries, 'duels': duels,
            'winner_entry_id': t.winner_entry_id.id or None,
        }

    # ================================================================== création, inscriptions
    @api.model
    def create_tournament(self, player, values):
        player._ensure_can_play()
        self.env.cr.execute("SELECT pg_advisory_xact_lock(hashtext('bq.tournament.create'))")
        live = self.sudo().search_count([('state', 'in', ('registration', 'running'))])
        if live >= bq_param(self.env, 'max_live_tournaments'):
            raise GameRefusal('SERVER_BUSY', _("Trop de tournois en cours en ce moment. Réessayez plus tard."))
        name = (values.get('name') or '').strip()[:60]
        if len(name) < 3:
            raise UserError(_("Donnez un nom au tournoi (3 caractères au moins)."))
        mode = values.get('mode') if values.get('mode') in ('solo', 'team') else 'solo'
        lang = values.get('lang') if values.get('lang') in ('fr', 'en') else player.lang
        count = clamp_setting('question_count', values.get('question_count'), bq_param(self.env, 'question_count_default'))
        passages = check_passages(values.get('passages'), lang, count)
        cap = bq_param(self.env, 'tournament_max_entries')
        try:
            max_entries = max(2, min(cap, int(values.get('max_entries') or 16)))
            min_entries = max(2, min(max_entries, int(values.get('min_entries') or 2)))
            forfeit = max(1, min(1440, int(values.get('forfeit_minutes') or bq_param(self.env, 'forfeit_minutes_default'))))
        except (TypeError, ValueError) as e:
            raise UserError(_("Nombres de participants invalides.")) from e
        t = self.sudo().create({
            'name': name, 'code': new_code(self.env), 'organizer_id': player.id, 'mode': mode, 'lang': lang,
            'is_public': bool(values.get('is_public', True)),
            'min_entries': min_entries, 'max_entries': max_entries, 'forfeit_minutes': forfeit,
            'passage_codes': [[c, lbl] for c, lbl in passages],
            'passages_label': ' ; '.join(lbl for _c, lbl in passages),
            'question_count': count,
            'question_seconds': clamp_setting('question_seconds', values.get('question_seconds'),
                                              bq_param(self.env, 'question_seconds_default')),
            'hand_seconds': clamp_setting('hand_seconds', values.get('hand_seconds'),
                                          bq_param(self.env, 'hand_seconds_default')),
        })
        if values.get('play'):
            if mode == 'solo':
                t.register(player)
            else:
                t.create_team(player, values.get('team_name'))
        t._bump()
        return t

    def set_round_passages(self, organizer, round_no, codes):
        self.ensure_one()
        self._lock()
        self._check_organizer(organizer)
        round_no = int(round_no)
        if self.state not in ('registration', 'running'):
            raise UserError(_("Le tournoi est terminé."))
        if self.duel_ids.filtered(lambda d: d.round == round_no and d.match_id):
            raise UserError(_("Ce tour a déjà commencé."))
        data = dict(self.round_passages or {})
        if codes:
            passages = check_passages(codes, self.lang, self.question_count)
            data[str(round_no)] = [[c, lbl] for c, lbl in passages]
        else:
            data.pop(str(round_no), None)
        self.round_passages = data
        self._bump()
        return self

    def _check_room(self):
        if len(self.entry_ids) >= self.max_entries:
            raise GameRefusal('FULL', _("Le tournoi est complet."))

    def register(self, player):
        """Tournoi entre joueurs : ``player`` s'inscrit."""
        self.ensure_one()
        self._lock()
        self._check_registration()
        if self.mode != 'solo':
            raise UserError(_("Ce tournoi se joue entre équipes : créez une équipe ou rejoignez-en une."))
        if self._entry_of(player):
            return self
        self._check_room()
        player._engage(tournament=self)
        self.env['bq.tournament.entry'].sudo().create({'tournament_id': self.id, 'player_id': player.id})
        self._bump()
        return self

    def unregister(self, player):
        """Se désinscrire (inscriptions ouvertes) ; en cours, c'est abandonner."""
        self.ensure_one()
        self._lock()
        entry = self._entry_of(player)
        if not entry:
            return self
        if self.state == 'registration':
            if entry.team_id:
                entry.team_id.leave(player)
            else:
                entry.sudo().unlink()
                player._release(tournament=self)
        elif self.state == 'running':
            if entry.team_id:
                # Un membre qui part : son équipe continue sans lui.
                player._release(tournament=self)
                line = entry.team_id.member_ids.filtered(lambda m: m.player_id == player)
                line.sudo().unlink()
                if not entry.team_id._members():
                    entry._forfeit_everything()
            else:
                player._release(tournament=self)
                entry._forfeit_everything()
        self._bump()
        return self

    def create_team(self, player, name):
        self.ensure_one()
        self._lock()
        self._check_registration()
        if self.mode != 'team':
            raise UserError(_("Ce tournoi se joue entre joueurs."))
        if self._entry_of(player):
            raise UserError(_("Vous êtes déjà inscrit à ce tournoi."))
        self._check_room()
        player._engage(tournament=self)
        name = (name or '').strip()[:40] or _("Équipe de %s", player.pseudo)
        if self.team_ids.filtered(lambda t: t.name.lower() == name.lower()):
            raise UserError(_("Une équipe porte déjà ce nom dans ce tournoi."))
        team = self.env['bq.team'].sudo().create({'name': name, 'captain_id': player.id, 'tournament_id': self.id})
        self.env['bq.team.member'].sudo().create({'team_id': team.id, 'player_id': player.id, 'state': 'member'})
        self.env['bq.tournament.entry'].sudo().create({'tournament_id': self.id, 'team_id': team.id})
        self._bump()
        return team

    # rappels de bq.team
    def _team_member_joined(self, team, player):
        player._engage(tournament=self)
        self._bump()

    def _team_member_left(self, team, player):
        player._release(tournament=self)
        self._bump()

    def _team_dissolved(self, team):
        self.entry_ids.filtered(lambda e: e.team_id == team).sudo().unlink()
        self._bump()

    # ================================================================== lancement et tableau
    def start(self, organizer):
        self.ensure_one()
        self._lock()
        self._check_organizer(organizer)
        self._check_registration()
        team_min = bq_param(self.env, 'team_min_size')
        if self.mode == 'team':
            # Les équipes incomplètes ne sont pas retenues : leurs joueurs sont libérés.
            for entry in self.entry_ids.filtered(lambda e: len(e.team_id._members()) < team_min):
                team = entry.team_id
                members = team._members()
                self.env['bq.notify']._players_event(members, 'tournament_team_dropped', {
                    'tournament_id': self.id, 'name': self.name},
                    _("Tournoi « %s »", self.name), _("Votre équipe n'était pas complète : elle n'a pas été retenue."))
                members._release(tournament=self)
                entry.sudo().unlink()
                team.sudo().unlink()
            self.team_ids.member_ids.filtered(lambda m: m.state != 'member').sudo().unlink()
        entries = self.entry_ids
        if len(entries) < max(2, self.min_entries):
            raise GameRefusal('ENTRIES', _("Il faut au moins %(n)s participants (inscrits valables : %(have)s).",
                                           n=max(2, self.min_entries), have=len(entries)))
        self._build_bracket(entries)
        self.write({'state': 'running', 'started_at': fields.Datetime.now()})
        self._resolve_ready_duels(now_ts())
        self._bump()
        return self

    def _build_bracket(self, entries):
        rnd = random.SystemRandom()
        order = list(entries)
        rnd.shuffle(order)
        n = len(order)
        rounds = max(1, math.ceil(math.log2(n)))
        size = 2 ** rounds
        byes = size - n
        Duel = self.env['bq.tournament.duel'].sudo()
        self.duel_ids.unlink()
        by_round = {}
        for r in range(rounds, 0, -1):
            count = size // (2 ** r)
            by_round[r] = [Duel.create({'tournament_id': self.id, 'round': r, 'position': p}) for p in range(count)]
        for r in range(1, rounds):
            for p, duel in enumerate(by_round[r]):
                duel.write({'next_duel_id': by_round[r + 1][p // 2].id, 'next_side': 'a' if p % 2 == 0 else 'b'})
        pairs = by_round[1]
        bye_positions = {math.floor(i * len(pairs) / byes) for i in range(byes)} if byes else set()
        for p, duel in enumerate(pairs):
            a = order.pop()
            b = order.pop() if p not in bye_positions else None
            duel.write({'entry_a_id': a.id, 'a_known': True, 'entry_b_id': b.id if b else False, 'b_known': True})
        for i, entry in enumerate(entries):
            entry.seed = i + 1
        self.rounds = rounds

    def _resolve_ready_duels(self, now):
        """Ouvre les duels dont les deux camps sont connus ; règle les exemptions en chaîne."""
        for _guard in range(1000):
            progressed = False
            for duel in self.duel_ids.filtered(lambda d: d.state == 'pending' and d.a_known and d.b_known) \
                    .sorted(lambda d: (d.round, d.position)):
                duel._open_or_skip(now)
                progressed = True
            if not progressed:
                break

    def _duel_done(self, duel):
        """Propage le vainqueur (ou l'absence de vainqueur) au duel suivant, ou clôt le tournoi."""
        loser_entries = (duel.entry_a_id | duel.entry_b_id) - duel.winner_entry_id
        for entry in loser_entries:
            entry.write({'state': 'eliminated', 'eliminated_round': duel.round})
            entry._players()._release(tournament=self)
            self.env['bq.notify']._players_event(entry._players(), 'tournament_eliminated', {'tournament_id': self.id},
                                                 _("Tournoi « %s »", self.name),
                                                 _("Vous êtes éliminé au tour %s. Merci d'avoir joué !", duel.round))
        nxt = duel.next_duel_id
        if nxt:
            side = duel.next_side
            nxt.write({'entry_%s_id' % side: duel.winner_entry_id.id or False, '%s_known' % side: True})
            self._resolve_ready_duels(now_ts())
        else:
            self._finish(duel.winner_entry_id)
        self._bump()

    def _finish(self, winner):
        self.write({'state': 'finished', 'winner_entry_id': winner.id or False,
                    'finished_at': fields.Datetime.now()})
        if winner:
            winner.state = 'winner'
            for p in winner._players():
                p.sudo().tournaments_won = p.tournaments_won + 1
            self.env['bq.notify']._players_event(winner._players(), 'tournament_won', {'tournament_id': self.id},
                                                 _("Tournoi « %s »", self.name), _("Bravo, vous avez gagné le tournoi !"))
        players = self.entry_ids._players()
        players._release(tournament=self)

    def action_cancel(self):
        """Back-office."""
        for t in self:
            t.sudo().cancel()

    def cancel(self, organizer=None):
        self.ensure_one()
        self._lock()
        if organizer:
            self._check_organizer(organizer)
        if self.state in ('finished', 'cancelled'):
            return self
        for match in self.match_ids.filtered(lambda m: m.state in ('lobby', 'running')):
            match._lock()
            match.write({'state': 'cancelled', 'phase': False})
            match._bump()
        players = self.entry_ids._players() | self.team_ids.member_ids.player_id
        self.env['bq.notify']._players_event(players, 'tournament_cancelled', {'tournament_id': self.id},
                                             _("Tournoi « %s »", self.name), _("Le tournoi a été annulé."))
        players._release(tournament=self)
        self.write({'state': 'cancelled'})
        self._bump()
        return self

    # ================================================================== tâches planifiées
    def _check_deadlines(self, now=None):
        """Forfaits des duels restés sans « Prêt » des deux camps."""
        now = now or now_ts()
        for t in self:
            for duel in t.duel_ids.filtered(lambda d: d.state == 'ready_wait' and d.deadline and now >= d.deadline):
                duel._forfeit(now)

    @api.model
    def _cron_deadlines(self):
        now = now_ts()
        for t in self.sudo().search([('state', '=', 'running')]):
            if not t.duel_ids.filtered(lambda d: d.state == 'ready_wait' and d.deadline and now >= d.deadline):
                continue
            try:
                with self.env.cr.savepoint():
                    t._lock()
                    t._check_deadlines(now)
            except Exception:  # noqa: BLE001
                _logger.exception("Tournoi %s : forfaits impossibles à appliquer", t.code)


class BqTournamentEntry(models.Model):
    _name = 'bq.tournament.entry'
    _description = "Participant d'un tournoi"
    _order = 'id'

    tournament_id = fields.Many2one('bq.tournament', required=True, ondelete='cascade', index=True)
    player_id = fields.Many2one('bq.player', "Joueur", ondelete='cascade', index=True)
    team_id = fields.Many2one('bq.team', "Équipe", ondelete='cascade', index=True)
    seed = fields.Integer("Tirage")
    state = fields.Selection([('in', "En lice"), ('eliminated', "Éliminé"), ('winner', "Vainqueur")],
                             default='in', required=True)
    eliminated_round = fields.Integer("Éliminé au tour")

    _player_or_team = models.Constraint(
        'CHECK ((player_id IS NULL) <> (team_id IS NULL))', "Un participant est un joueur OU une équipe.")
    _player_uniq = models.Constraint('UNIQUE (tournament_id, player_id)', "Déjà inscrit.")

    @api.depends('player_id.pseudo', 'team_id.name')
    def _compute_display_name(self):
        for e in self:
            e.display_name = e.player_id.pseudo or e.team_id.name or '?'

    def _players(self):
        players = self.env['bq.player']
        for e in self:
            players |= e.player_id | e.team_id._members()
        return players

    def _forfeit_everything(self):
        """Abandon en cours de tournoi : le duel en attente ou en cours est perdu."""
        for entry in self:
            duel = entry.tournament_id.duel_ids.filtered(
                lambda d, e=entry: d.state in ('pending', 'ready_wait', 'playing') and e in (d.entry_a_id | d.entry_b_id))
            for d in duel:
                if d.state == 'playing' and d.match_id.state == 'running':
                    m = d.match_id
                    m._lock()
                    for part in m.participant_ids.filtered(lambda p: p.player_id in entry._players() or (
                            entry.team_id and p.team_id == entry.team_id)):
                        part.left = True
                    m._check_abandon(now_ts())
                    m._bump()
                elif d.state in ('pending', 'ready_wait'):
                    other = (d.entry_a_id | d.entry_b_id) - entry
                    d._close(other[:1], 'forfeit')
            if entry.state == 'in':
                entry.write({'state': 'eliminated'})


class BqTournamentDuel(models.Model):
    _name = 'bq.tournament.duel'
    _description = "Duel d'un tournoi"
    _order = 'tournament_id, round, position'

    tournament_id = fields.Many2one('bq.tournament', required=True, ondelete='cascade', index=True)
    round = fields.Integer("Tour", required=True)
    position = fields.Integer("Place dans le tour", required=True)
    entry_a_id = fields.Many2one('bq.tournament.entry', "Camp A", ondelete='set null')
    entry_b_id = fields.Many2one('bq.tournament.entry', "Camp B", ondelete='set null')
    a_known = fields.Boolean("Camp A connu")
    b_known = fields.Boolean("Camp B connu")
    next_duel_id = fields.Many2one('bq.tournament.duel', "Duel suivant", ondelete='set null')
    next_side = fields.Selection([('a', "A"), ('b', "B")], "Camp au duel suivant")
    state = fields.Selection([
        ('pending', "En attente des adversaires"),
        ('ready_wait', "En attente des joueurs"),
        ('playing', "En cours"),
        ('done', "Terminé"),
    ], default='pending', required=True)
    result = fields.Selection([
        ('played', "Joué"), ('bye', "Qualifié d'office"), ('forfeit', "Forfait"),
        ('double_forfeit', "Double forfait"),
    ], "Issue")
    ready_a = fields.Boolean("A prêt")
    ready_b = fields.Boolean("B prêt")
    deadline = fields.Float("Forfait à (horodatage)", digits=(16, 3))
    match_id = fields.Many2one('bq.match', "Match", ondelete='set null')
    winner_entry_id = fields.Many2one('bq.tournament.entry', "Vainqueur", ondelete='set null')
    score_a = fields.Integer("Points A")
    score_b = fields.Integer("Points B")

    def _side_of(self, player):
        t = self.tournament_id
        entry = t._entry_of(player)
        if entry and entry == self.entry_a_id:
            return 'a'
        if entry and entry == self.entry_b_id:
            return 'b'
        return None

    def _open_or_skip(self, now):
        """Les deux camps sont connus : on ouvre le duel, ou on règle une exemption."""
        a, b = self.entry_a_id, self.entry_b_id
        if a and b:
            t = self.tournament_id
            codes = t._passages_for_round(self.round)
            match = self.env['bq.match'].sudo().create({
                'code': new_code(self.env), 'kind': 'duel', 'mode': t.mode, 'tournament_id': t.id,
                'duel_id': self.id, 'creator_id': t.organizer_id.id, 'lang': t.lang,
                'passage_codes': codes, 'passages_label': ' ; '.join(lbl for _c, lbl in codes),
                'question_count': t.question_count, 'question_seconds': t.question_seconds,
                'hand_seconds': t.hand_seconds, 'reveal_seconds': bq_param(self.env, 'reveal_seconds'),
                'max_players': 2 * bq_param(self.env, 'team_max_size'), 'is_public': False,
            })
            if t.mode == 'team':
                match.duel_team_ids = [(6, 0, (a.team_id | b.team_id).ids)]
            for entry in (a, b):
                for player in entry._players():
                    match._add_participant(player, team=entry.team_id or None)
            self.write({'state': 'ready_wait', 'match_id': match.id, 'deadline': now + 60 * t.forfeit_minutes})
            match._bump()
            players = a._players() | b._players()
            self.env['bq.notify']._players_event(
                players, 'duel_ready', {'tournament_id': t.id, 'duel_id': self.id, 'match_id': match.id},
                _("Tournoi « %s » : à vous de jouer !", t.name),
                _("Votre duel du tour %(r)s (%(a)s contre %(b)s) vous attend. Confirmez « Prêt » dans les "
                  "%(m)s minutes.", r=self.round, a=a.display_name, b=b.display_name, m=t.forfeit_minutes))
        elif a or b:
            self._close(a or b, 'bye')
        else:
            self._close(self.env['bq.tournament.entry'], 'double_forfeit')

    def ready(self, player, now=None):
        """Un joueur du camp confirme « Prêt ». Les deux camps prêts : le match démarre."""
        self.ensure_one()
        now = now or now_ts()
        t = self.tournament_id
        t._lock()
        t._check_deadlines(now)
        if self.state != 'ready_wait':
            raise GameRefusal('NOT_WAITING', _("Ce duel n'attend plus de confirmation."))
        side = self._side_of(player)
        if not side:
            raise AccessError(_("Vous ne jouez pas ce duel."))
        self['ready_%s' % side] = True
        match = self.match_id
        match._lock()
        part = match.participant_ids.filtered(lambda p: p.player_id == player)
        part.write({'ready': True})
        part._seen(now)
        if self.ready_a and self.ready_b:
            self.state = 'playing'
            match._begin(now)
        match._bump()
        t._bump()
        return self

    def _forfeit(self, now):
        if self.state != 'ready_wait':
            return
        if self.ready_a and not self.ready_b:
            self._close(self.entry_a_id, 'forfeit')
        elif self.ready_b and not self.ready_a:
            self._close(self.entry_b_id, 'forfeit')
        elif not self.ready_a and not self.ready_b:
            self._close(self.env['bq.tournament.entry'], 'double_forfeit')

    def _close(self, winner, result):
        if self.match_id and self.match_id.state in ('lobby', 'running'):
            self.match_id.write({'state': 'cancelled', 'phase': False})
            self.match_id._bump()
        self.write({'state': 'done', 'result': result, 'winner_entry_id': winner.id or False})
        self.tournament_id._duel_done(self)

    def _match_finished(self, match):
        """Rappel de bq.match à la fin du duel."""
        if self.state == 'done':
            return
        a, b = self.entry_a_id, self.entry_b_id
        if match.mode == 'team':
            score_a = match._side_score(a.team_id)
            score_b = match._side_score(b.team_id)
            winner = a if match.winner_team_id == a.team_id else b if match.winner_team_id == b.team_id else None
        else:
            pa = match.participant_ids.filtered(lambda p: p.player_id == a.player_id)
            pb = match.participant_ids.filtered(lambda p: p.player_id == b.player_id)
            score_a, score_b = pa.score, pb.score
            winner = a if match.winner_participant_id == pa else b if match.winner_participant_id == pb else None
        if not winner:
            # Le moteur désigne toujours un vainqueur en duel ; filet de sécurité.
            winner = random.SystemRandom().choice([a, b])
        self.write({'score_a': score_a, 'score_b': score_b})
        self.tournament_id._lock()
        self._close(winner, 'played')
