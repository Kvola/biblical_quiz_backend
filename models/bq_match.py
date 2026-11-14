# -*- coding: utf-8 -*-
"""Le match : salle d'attente, puis questions à la main, puis classement.

Règles (validées avec le porteur du projet)
-------------------------------------------
* Chaque question a son temps (30 s par défaut, réglable au lancement).
* Le premier qui « prend la main » a ``hand_seconds`` (5 s par défaut) pour
  répondre ; **pendant la main, le chrono de la question est en pause**.
* Bonne réponse : +10, la question est close. Mauvaise réponse, ou main
  laissée filer : −5, et ce joueur — ou, en équipe, **toute son équipe** —
  ne peut plus répondre à cette question ; le chrono reprend pour les autres.
* Personne ne répond avant la fin du temps : 0 pour tous.
* Uniquement des questions à choix multiples (un vrai/faux se joue à pile ou face).
* Duel de tournoi à égalité : questions décisives, le premier qui répond juste gagne.

Le serveur est le seul arbitre
------------------------------
Il tient les chronos et décide qui a pris la main le premier, à l'heure où
il reçoit l'appui. Les bonnes réponses ne quittent le serveur qu'une fois la
question close. Chaque action prend d'abord le verrou de ligne du match
(``SELECT … FOR UPDATE``) : deux appuis simultanés passent l'un après l'autre,
et le second trouve la main déjà prise.

Le temps avance sans horloge côté serveur : chaque requête (appui, réponse,
lecture de l'état, ``tick`` envoyé par les téléphones à l'échéance) appelle
d'abord :meth:`_advance`, qui rejoue les échéances passées **à leur heure
exacte** (pas à l'heure de la requête). Une tâche planifiée rattrape chaque
minute les matchs que plus personne ne fait avancer.

Temps réel : après chaque changement, l'état public (sans les réponses) part
sur le canal secret du match (bus Odoo / WebSocket) ; un téléphone sans
WebSocket interroge ``/match/state``.
"""
import logging
import random
import secrets
import time
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from .bq_config import bq_param

_logger = logging.getLogger(__name__)

CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'
COUNTDOWN_SECONDS = 3.0
#: Délai réseau toléré après l'échéance AFFICHÉE (l'appui parti à temps arrive un peu après).
BUZZ_GRACE = 0.3
ANSWER_GRACE = 0.8
POINTS_RIGHT = 10
POINTS_WRONG = -5
TIEBREAK_EXTRA = 3
MAX_PASSAGES = 10

BOUNDS = {
    'question_seconds': (10, 120),
    'hand_seconds': (3, 15),
    'question_count': (5, 30),
}


class GameRefusal(UserError):
    """Refus métier avec un code lisible par l'application (``HAND_TAKEN``, ``TOO_LATE``…)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def now_ts():
    return time.time()


def ms(ts):
    return int(round(ts * 1000)) if ts else None


def new_code(env):
    rnd = random.SystemRandom()
    for _attempt in range(50):
        code = ''.join(rnd.choice(CODE_ALPHABET) for _i in range(6))
        if not env['bq.match'].sudo().search_count([('code', '=', code)]) and \
                not env['bq.tournament'].sudo().search_count([('code', '=', code)]):
            return code
    raise UserError(_("Impossible de créer un code. Réessayez."))


def clamp_setting(key, value, default):
    low, high = BOUNDS[key]
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def check_passages(codes, lang, question_count):
    """Valide les passages. Rend [(code, libellé)] ou lève UserError (message pour le joueur)."""
    from ..game import questions as qgen
    if not isinstance(codes, list) or not codes:
        raise UserError(_("Choisissez au moins un passage."))
    if len(codes) > MAX_PASSAGES:
        raise UserError(_("%s passages au plus.", MAX_PASSAGES))
    out = []
    for code in codes:
        try:
            passage = qgen.parse_passage(str(code))
        except ValueError as e:
            raise UserError(str(e)) from e
        out.append((str(code), qgen.passage_label(passage, lang)))
    available = qgen.available_count([c for c, _l in out], lang, cap=question_count)
    if available < question_count:
        raise UserError(_(
            "Ces passages ne permettent que %(n)s question(s), il en faut %(need)s. "
            "Ajoutez un passage, élargissez-le, ou réduisez le nombre de questions.",
            n=available, need=question_count))
    return out


class BqMatch(models.Model):
    _name = 'bq.match'
    _description = "Match (Quiz Biblique)"
    _order = 'id desc'

    name = fields.Char("Nom", compute='_compute_name', store=True)
    code = fields.Char("Code", required=True, readonly=True, index=True, copy=False)
    channel_token = fields.Char("Canal", required=True, readonly=True, copy=False, groups='base.group_system',
                                default=lambda self: secrets.token_urlsafe(24))
    kind = fields.Selection([('free', "Match libre"), ('duel', "Duel de tournoi")], required=True, default='free')
    mode = fields.Selection([('solo', "Individuel"), ('team', "Par équipes")], "Formule", required=True,
                            default='solo')
    state = fields.Selection([
        ('lobby', "Salle d'attente"),
        ('running', "En cours"),
        ('finished', "Terminé"),
        ('cancelled', "Annulé"),
    ], "État", required=True, default='lobby', index=True)
    phase = fields.Selection([
        ('countdown', "Compte à rebours"),
        ('open', "Question ouverte"),
        ('hand', "Un joueur a la main"),
        ('reveal', "Réponse affichée"),
    ], "Phase")
    creator_id = fields.Many2one('bq.player', "Créé par", ondelete='set null')
    tournament_id = fields.Many2one('bq.tournament', "Tournoi", ondelete='cascade', index=True)
    duel_id = fields.Many2one('bq.tournament.duel', "Duel", ondelete='set null')
    lang = fields.Selection([('fr', "Français (Segond 1910)"), ('en', "English (KJV)")], "Langue",
                            required=True, default='fr')
    passage_codes = fields.Json("Passages (codes)")
    passages_label = fields.Char("Passages")
    question_count = fields.Integer("Nombre de questions", required=True, default=10)
    question_seconds = fields.Integer("Temps par question (s)", required=True, default=30)
    hand_seconds = fields.Integer("Durée de la main (s)", required=True, default=5)
    reveal_seconds = fields.Integer("Affichage de la réponse (s)", required=True, default=6)
    max_players = fields.Integer("Joueurs au plus", required=True, default=10)
    is_public = fields.Boolean("Visible dans la liste des matchs ouverts", default=True)

    version = fields.Integer("Version", default=1, readonly=True)
    q_index = fields.Integer("Question en cours (0 = la première)", readonly=True)
    q_deadline = fields.Float("Fin de la question (horodatage)", digits=(16, 3), readonly=True)
    q_remaining = fields.Float("Temps restant mis en pause (s)", digits=(16, 3), readonly=True)
    phase_until = fields.Float("Fin de la phase (horodatage)", digits=(16, 3), readonly=True)
    hand_participant_id = fields.Many2one('bq.match.participant', "A la main", readonly=True)
    hand_deadline = fields.Float("Fin de la main (horodatage)", digits=(16, 3), readonly=True)
    hand_started = fields.Float("Prise de main (horodatage)", digits=(16, 3), readonly=True)
    locked_keys = fields.Json("Ne peuvent plus répondre", readonly=True)
    tiebreak = fields.Boolean("Questions décisives", readonly=True)
    result = fields.Selection([
        ('played', "Joué jusqu'au bout"),
        ('tiebreak', "Départagé par question décisive"),
        ('tiebreak_rule', "Départagé au règlement"),
        ('abandon', "Abandon"),
        ('idle', "Clos faute d'activité"),
    ], "Issue", readonly=True)
    winner_participant_id = fields.Many2one('bq.match.participant', "Vainqueur", readonly=True)
    winner_team_id = fields.Many2one('bq.team', "Équipe gagnante", readonly=True)

    started_at = fields.Datetime("Commencé le", readonly=True)
    finished_at = fields.Datetime("Terminé le", readonly=True)
    last_activity = fields.Datetime("Dernière activité", readonly=True, default=fields.Datetime.now, index=True)

    participant_ids = fields.One2many('bq.match.participant', 'match_id', "Joueurs")
    team_ids = fields.One2many('bq.team', 'match_id', "Équipes")
    duel_team_ids = fields.Many2many('bq.team', 'bq_match_duel_team_rel', 'match_id', 'team_id',
                                     "Équipes du duel", help="Duel de tournoi : les deux équipes inscrites au tournoi.")
    question_ids = fields.One2many('bq.match.question', 'match_id', "Questions")
    player_count = fields.Integer("Nombre de joueurs", compute='_compute_player_count')

    _code_uniq = models.Constraint('UNIQUE (code)', "Ce code existe déjà.")

    @api.depends('code', 'mode', 'passages_label')
    def _compute_name(self):
        for match in self:
            match.name = '%s — %s' % (match.code or '', match.passages_label or '')

    @api.depends('participant_ids.left')
    def _compute_player_count(self):
        for match in self:
            match.player_count = len(match.participant_ids.filtered(lambda p: not p.left))

    # ================================================================== outils
    def _lock(self):
        """Verrou de ligne du match ; relit l'état depuis la base."""
        self.ensure_one()
        self.env.cr.execute('SELECT id FROM bq_match WHERE id = %s FOR UPDATE', [self.id])
        self.invalidate_recordset()
        self.participant_ids.invalidate_recordset()
        self.question_ids.invalidate_recordset()
        self.team_ids.invalidate_recordset()
        return self

    def _teams(self):
        """Les équipes du match : les siennes (match libre) ou celles du tournoi (duel)."""
        return self.duel_team_ids if self.kind == 'duel' else self.team_ids

    def _active(self):
        return self.participant_ids.filtered(lambda p: not p.left)

    def _participant(self, player, required=True):
        part = self.participant_ids.filtered(lambda p: p.player_id == player)[:1]
        if required and (not part or part.left):
            raise AccessError(_("Vous ne participez pas à ce match."))
        return part

    def _current_question(self):
        return self.question_ids.filtered(lambda q: q.sequence == self.q_index)[:1]

    def _key(self, participant):
        return 't%s' % participant.team_id.id if self.mode == 'team' else 'p%s' % participant.id

    def _eligible_keys(self):
        return {self._key(p) for p in self._active() if self.mode == 'solo' or p.team_id}

    def _bump(self):
        """Nouvelle version de l'état ; diffusion unique, à la validation de la transaction."""
        for match in self:
            match.sudo().write({'version': match.version + 1, 'last_activity': fields.Datetime.now()})
        cr = self.env.cr
        pending = cr.precommit.data.setdefault('bq.match.broadcast', set())
        first = not pending
        pending.update(self.ids)
        if first:
            env = self.env

            @cr.precommit.add
            def _broadcast():
                ids = cr.precommit.data.pop('bq.match.broadcast', set())
                for match in env['bq.match'].sudo().browse(sorted(ids)).exists():
                    match._send_state()

    def _send_state(self):
        self.env['bus.bus'].sudo()._sendone('bq.match.%s' % self.sudo().channel_token, 'bq_match', self._state())

    # ================================================================== état public
    def _state(self):
        """Ce que voient tous les joueurs. Jamais la bonne réponse avant que la question soit close."""
        self.ensure_one()
        match = self.sudo()
        now = now_ts()
        participants = []
        for p in match.participant_ids.sorted('id'):
            participants.append({
                'id': p.id, 'player_id': p.player_id.id, 'pseudo': p.player_id.pseudo,
                'team_id': p.team_id.id or None, 'score': p.score, 'correct': p.correct_count,
                'wrong': p.wrong_count, 'left': p.left, 'online': bool(p.last_seen and now - p.last_seen < 30),
                'ready': p.ready, 'rank': p.rank or None,
            })
        teams = []
        for t in match._teams().sorted('id'):
            data = t._public()
            members = match.participant_ids.filtered(lambda p, t=t: p.team_id == t)
            data['score'] = sum(members.mapped('score'))
            teams.append(data)
        data = {
            'id': match.id, 'code': match.code, 'version': match.version, 'server_now': ms(now),
            'kind': match.kind, 'mode': match.mode, 'state': match.state, 'phase': match.phase or None,
            'lang': match.lang, 'creator_id': match.creator_id.id or None, 'is_public': match.is_public,
            'passages': [{'code': c, 'label': lbl} for c, lbl in (match.passage_codes or [])],
            'passages_label': match.passages_label or '',
            'settings': {
                'question_count': match.question_count, 'question_seconds': match.question_seconds,
                'hand_seconds': match.hand_seconds, 'max_players': match.max_players,
                'reveal_seconds': match.reveal_seconds,
            },
            'participants': participants, 'teams': teams,
            'phase_until': ms(match.phase_until) if match.phase in ('countdown', 'reveal') else None,
            'question': None, 'result': None,
            'tournament': {'id': match.tournament_id.id, 'name': match.tournament_id.name,
                           'duel_id': match.duel_id.id, 'round': match.duel_id.round}
            if match.tournament_id else None,
        }
        if match.state == 'running' and match.phase != 'countdown':
            q = match._current_question()
            if q:
                regular = match.question_ids.filtered(lambda x: not x.is_tiebreak)
                question = {
                    'index': q.sequence + 1, 'total': len(regular), 'tiebreak': q.is_tiebreak,
                    'prompt': q.prompt, 'options': q.options or [], 'ref_label': q.ref_label,
                    'source': q.source, 'locked': match.locked_keys or [],
                    'deadline': ms(match.q_deadline) if match.phase == 'open' else None,
                    'remaining_ms': int((match.q_remaining or 0) * 1000) if match.phase == 'hand' else None,
                    'hand': {'participant_id': match.hand_participant_id.id,
                             'deadline': ms(match.hand_deadline)} if match.phase == 'hand' else None,
                }
                if match.phase == 'reveal':
                    question.update(q._reveal_data())
                data['question'] = question
        if match.state == 'finished':
            data['result'] = match._result_data()
        return data

    def _result_data(self):
        self.ensure_one()
        return {
            'outcome': self.result,
            'winner_participant_id': self.winner_participant_id.id or None,
            'winner_team_id': self.winner_team_id.id or None,
            'ranking': [{'participant_id': p.id, 'rank': p.rank, 'score': p.score}
                        for p in self.participant_ids.sorted(lambda p: (p.rank or 999, -p.score, p.id))],
        }

    def _history(self):
        """Toutes les questions jouées, avec la réponse et l'explication (fin de match)."""
        self.ensure_one()
        out = []
        for q in self.question_ids.filtered(lambda x: x.state == 'done').sorted('sequence'):
            item = {'index': q.sequence + 1, 'tiebreak': q.is_tiebreak, 'prompt': q.prompt,
                    'options': q.options or [], 'ref_label': q.ref_label, 'source': q.source}
            item.update(q._reveal_data())
            out.append(item)
        return out

    # ================================================================== moteur
    def _advance(self, now=None):
        """Rejoue toutes les échéances passées. Rend True si l'état a changé. Verrou déjà pris."""
        self.ensure_one()
        now = now or now_ts()
        changed = False
        for _guard in range(500):
            if not self._step(now):
                break
            changed = True
        if changed:
            self._bump()
        return changed

    def _step(self, now):
        if self.state != 'running':
            return False
        if self.phase == 'countdown' and now >= self.phase_until:
            self._open_question(self.phase_until)
            return True
        if self.phase == 'open' and now >= self.q_deadline + BUZZ_GRACE:
            self._reveal(self.q_deadline + BUZZ_GRACE)
            return True
        if self.phase == 'hand' and now >= self.hand_deadline + ANSWER_GRACE:
            holder = self.hand_participant_id
            self._wrong(holder, None, self.hand_deadline + ANSWER_GRACE, kind='timeout')
            return True
        if self.phase == 'reveal' and now >= self.phase_until:
            self._next(self.phase_until)
            return True
        return False

    def _open_question(self, at, index=None):
        if index is not None:
            self.q_index = index
        q = self._current_question()
        q.write({'state': 'asked', 'asked_at': at})
        self.write({
            'phase': 'open', 'q_deadline': at + self.question_seconds, 'q_remaining': 0,
            'hand_participant_id': False, 'hand_deadline': 0, 'hand_started': 0, 'locked_keys': [],
        })

    def _resume(self, at):
        """La main est rendue : le chrono de la question reprend là où il s'était arrêté."""
        if not (self._eligible_keys() - set(self.locked_keys or [])):
            self._reveal(at)
            return
        self.write({'phase': 'open', 'q_deadline': at + (self.q_remaining or 0), 'q_remaining': 0,
                    'hand_participant_id': False, 'hand_deadline': 0, 'hand_started': 0})

    def _reveal(self, at, winner=None):
        q = self._current_question()
        q.write({'state': 'done', 'winner_participant_id': winner.id if winner else False})
        self.write({'phase': 'reveal', 'phase_until': at + self.reveal_seconds, 'hand_participant_id': False,
                    'hand_deadline': 0, 'hand_started': 0, 'q_remaining': 0})

    def _next(self, at):
        if self.tiebreak:
            # Mort subite : la première bonne réponse décisive termine le duel.
            winner = self._current_question().winner_participant_id
            if winner:
                self._finish(at, outcome='tiebreak', decided_by=winner)
                return
        else:
            regular = self.question_ids.filtered(lambda q: not q.is_tiebreak)
            if self.q_index + 1 < len(regular):
                self._open_question(at, self.q_index + 1)
                return
        if self.kind == 'duel' and self._sides_tied():
            extras = self.question_ids.filtered(lambda q: q.is_tiebreak and q.state == 'pending').sorted('sequence')
            if extras:
                self.tiebreak = True
                self._open_question(at, extras[0].sequence)
                return
            self._finish(at, outcome='tiebreak_rule')
            return
        self._finish(at, outcome='played')

    def _sides(self):
        """Les deux camps d'un duel : (participants ou équipes)."""
        if self.mode == 'team':
            return self._teams().sorted('id')
        return self.participant_ids.sorted('id')

    def _side_score(self, side):
        if side._name == 'bq.team':
            return sum(self.participant_ids.filtered(lambda p: p.team_id == side).mapped('score'))
        return side.score

    def _sides_tied(self):
        sides = self._sides()
        return len(sides) == 2 and self._side_score(sides[0]) == self._side_score(sides[1])

    # ------------------------------------------------------------------ actions de jeu
    def buzz(self, player, now=None):
        """Prendre la main. Le verrou de ligne départage les appuis simultanés."""
        self.ensure_one()
        now = now or now_ts()
        self._lock()
        self._advance(now)
        part = self._participant(player)
        part._seen(now)
        if self.state != 'running' or self.phase not in ('open', 'hand'):
            raise GameRefusal('NOT_OPEN', _("Aucune question n'est ouverte."))
        if self.phase == 'hand':
            raise GameRefusal('HAND_TAKEN', _("%s a pris la main avant vous.", self.hand_participant_id.player_id.pseudo))
        if self.mode == 'team' and not part.team_id:
            raise GameRefusal('NO_TEAM', _("Vous n'êtes dans aucune équipe."))
        if self._key(part) in (self.locked_keys or []):
            raise GameRefusal('LOCKED', _("Vous ne pouvez plus répondre à cette question."))
        if now >= self.q_deadline + BUZZ_GRACE:
            raise GameRefusal('TOO_LATE', _("Le temps est écoulé."))
        remaining = max(0.0, self.q_deadline - now)
        self.write({
            'phase': 'hand', 'q_remaining': remaining, 'hand_participant_id': part.id,
            'hand_started': now, 'hand_deadline': now + self.hand_seconds,
        })
        self._bump()
        return part

    def answer(self, player, choice, now=None):
        """Répondre pendant sa main. ``choice`` = indice dans les propositions affichées."""
        self.ensure_one()
        now = now or now_ts()
        self._lock()
        self._advance(now)
        part = self._participant(player)
        part._seen(now)
        if self.state != 'running' or self.phase != 'hand' or self.hand_participant_id != part:
            raise GameRefusal('NO_HAND', _("Vous n'avez pas la main (le temps est peut-être écoulé)."))
        q = self._current_question()
        try:
            choice = int(choice)
        except (TypeError, ValueError):
            choice = -1
        if choice < 0 or choice >= len(q.options or []):
            raise GameRefusal('BAD_CHOICE', _("Réponse inconnue."))
        if choice == q.answer:
            self._right(part, choice, now)
        else:
            self._wrong(part, choice, now, kind='wrong')
        self._bump()
        return choice == q.answer

    def _log(self, part, choice, at, kind, points):
        q = self._current_question()
        self.env['bq.match.answer'].sudo().create({
            'question_id': q.id, 'participant_id': part.id, 'team_id': part.team_id.id or False,
            'kind': kind, 'choice': choice if choice is not None else -1, 'points': points, 'at': at,
            'delay': max(0.0, (self.hand_started or at) - (q.asked_at or at)),
        })

    def _right(self, part, choice, at):
        # Questions décisives : seul compte QUI répond juste en premier, pas les points.
        points = 0 if self.tiebreak else POINTS_RIGHT
        part.write({'score': part.score + points, 'correct_count': part.correct_count + 1})
        self._log(part, choice, at, 'correct', points)
        self._reveal(at, winner=part)

    def _wrong(self, part, choice, at, kind):
        points = 0 if self.tiebreak else POINTS_WRONG
        part.write({'score': part.score + points, 'wrong_count': part.wrong_count + 1})
        self._log(part, choice, at, kind, points)
        self.locked_keys = list(set(self.locked_keys or []) | {self._key(part)})
        self._resume(at)

    # ------------------------------------------------------------------ fin
    def _finish(self, at, outcome='played', decided_by=None):
        """Classement, vainqueur, statistiques ; libère les joueurs (match libre) ou prévient le tournoi."""
        self.ensure_one()
        if self.state == 'finished':
            return
        parts = self.participant_ids
        ordered = parts.sorted(lambda p: (-p.score, -p.correct_count, p.wrong_count, p.id))
        winner_part = self.env['bq.match.participant']
        winner_team = self.env['bq.team']
        if self.mode == 'team':
            teams = self._teams()
            if decided_by:
                winner_team = decided_by.team_id
            elif len(teams) == 2:
                s0, s1 = self._side_score(teams[0]), self._side_score(teams[1])
                if s0 != s1:
                    winner_team = teams[0] if s0 > s1 else teams[1]
                elif self.kind == 'duel':
                    winner_team = self._rule_tiebreak(teams)
            elif len(teams) == 1:
                winner_team = teams
        else:
            if decided_by:
                winner_part = decided_by
            else:
                active = ordered.filtered(lambda p: not p.left) or ordered
                top = active.filtered(lambda p: p.score == active[0].score) if active else active
                if len(top) == 1:
                    winner_part = top
                elif self.kind == 'duel' and len(top) == 2:
                    winner_part = self._rule_tiebreak(top)
        if outcome == 'tiebreak_rule' and not (winner_part or winner_team):
            outcome = 'played'

        def is_winner(p):
            return bool((winner_part and p == winner_part) or (winner_team and p.team_id == winner_team))

        # Rangs : le vainqueur désigné d'abord (départage, abandon), puis les points ; partagés à égalité.
        ordered = parts.sorted(lambda p: (0 if is_winner(p) else 1, -p.score, -p.correct_count, p.wrong_count, p.id))
        rank, last = 0, None
        for i, p in enumerate(ordered, start=1):
            key = (is_winner(p), p.score) if self.mode == 'solo' else (is_winner(p), p.team_id.id)
            if key != last:
                rank, last = i, key
            p.rank = rank
        self.write({
            'state': 'finished', 'phase': False, 'result': outcome, 'finished_at': fields.Datetime.now(),
            'winner_participant_id': winner_part.id, 'winner_team_id': winner_team.id,
            'hand_participant_id': False,
        })
        # Statistiques des joueurs.
        for p in parts:
            p.player_id.sudo().write({
                'matches_played': p.player_id.matches_played + 1,
                'matches_won': p.player_id.matches_won + (1 if is_winner(p) else 0),
            })
        if self.kind == 'free':
            parts.player_id._release(match=self)
        elif self.duel_id:
            self.duel_id._match_finished(self)

    def _rule_tiebreak(self, sides):
        """Égalité persistante après les questions décisives : moins de mauvaises réponses,
        puis réponses justes les plus rapides, puis tirage au sort (journalisé)."""
        def stats(side):
            parts = self.participant_ids.filtered(
                lambda p: p.team_id == side) if side._name == 'bq.team' else side
            answers = self.env['bq.match.answer'].sudo().search([('participant_id', 'in', parts.ids)])
            wrong = len(answers.filtered(lambda a: a.kind != 'correct'))
            speed = sum(answers.filtered(lambda a: a.kind == 'correct').mapped('delay'))
            return wrong, speed
        a, b = sides[0], sides[1]
        sa, sb = stats(a), stats(b)
        if sa != sb:
            return a if sa < sb else b
        pick = random.SystemRandom().choice([a, b])
        _logger.info("Match %s : égalité parfaite, tirage au sort (%s).", self.code, pick.display_name)
        return pick

    # ================================================================== salle d'attente
    @api.model
    def _live_free_count(self):
        return self.sudo().search_count([('kind', '=', 'free'), ('state', 'in', ('lobby', 'running'))])

    @api.model
    def create_free(self, player, values):
        """Un joueur crée un match libre ; il y entre aussitôt."""
        player._ensure_can_play()
        # Verrou consultatif : deux créations simultanées ne dépassent pas la limite à elles deux.
        self.env.cr.execute("SELECT pg_advisory_xact_lock(hashtext('bq.match.create'))")
        if self._live_free_count() >= bq_param(self.env, 'max_live_matches'):
            raise GameRefusal('SERVER_BUSY', _("Trop de matchs en cours en ce moment. Réessayez dans quelques minutes."))
        mode = values.get('mode') if values.get('mode') in ('solo', 'team') else 'solo'
        vals = self._settings_from(values, player)
        team_max = bq_param(self.env, 'team_max_size')
        vals.update({
            'code': new_code(self.env), 'kind': 'free', 'mode': mode, 'creator_id': player.id,
            'is_public': bool(values.get('is_public', True)),
            'max_players': 2 * team_max if mode == 'team' else bq_param(self.env, 'max_players'),
        })
        if mode == 'solo' and values.get('max_players'):
            try:
                vals['max_players'] = max(2, min(vals['max_players'], int(values['max_players'])))
            except (TypeError, ValueError):
                pass
        match = self.sudo().create(vals)
        match._add_participant(player)
        match._bump()
        return match

    @api.model
    def _settings_from(self, values, player=None, tournament=None):
        """Réglages saisis, bornés ; passages validés (assez de questions)."""
        env = self.env
        lang = values.get('lang') if values.get('lang') in ('fr', 'en') else (player.lang if player else 'fr')
        count = clamp_setting('question_count', values.get('question_count'), bq_param(env, 'question_count_default'))
        passages = check_passages(values.get('passages'), lang, count)
        return {
            'lang': lang,
            'passage_codes': [[c, lbl] for c, lbl in passages],
            'passages_label': ' ; '.join(lbl for _c, lbl in passages),
            'question_count': count,
            'question_seconds': clamp_setting('question_seconds', values.get('question_seconds'),
                                              bq_param(env, 'question_seconds_default')),
            'hand_seconds': clamp_setting('hand_seconds', values.get('hand_seconds'),
                                          bq_param(env, 'hand_seconds_default')),
            'reveal_seconds': bq_param(env, 'reveal_seconds'),
        }

    def _add_participant(self, player, team=None):
        part = self.participant_ids.filtered(lambda p: p.player_id == player)[:1]
        if part and not part.left:
            if team:
                part.team_id = team
            return part
        if self.kind == 'free':
            player._engage(match=self)
        if part:
            part.write({'left': False, 'team_id': team.id if team else False})
            return part
        return self.env['bq.match.participant'].sudo().create({
            'match_id': self.id, 'player_id': player.id, 'team_id': team.id if team else False,
            'last_seen': now_ts(),
        })

    def _check_lobby(self):
        if self.state != 'lobby':
            raise GameRefusal('NOT_LOBBY', _("Ce match a déjà commencé ou est terminé."))

    def _check_creator(self, player):
        if self.creator_id != player:
            raise AccessError(_("Seul le créateur du match peut faire cela."))

    def join(self, player):
        self.ensure_one()
        self._lock()
        if self.kind != 'free':
            raise AccessError(_("Ce duel de tournoi est réservé à ses joueurs."))
        self._check_lobby()
        if self._participant(player, required=False) and not self._participant(player, required=False).left:
            return self
        if len(self._active()) >= self.max_players:
            raise GameRefusal('FULL', _("Ce match est complet."))
        self._add_participant(player)
        self._bump()
        return self

    def leave(self, player, now=None):
        """Quitter. En salle d'attente : on sort (le créateur passe la main). En cours : abandon."""
        self.ensure_one()
        now = now or now_ts()
        self._lock()
        self._advance(now)
        part = self._participant(player, required=False)
        if not part or part.left:
            return self
        if self.state == 'lobby':
            team = part.team_id
            if team:
                team.leave(player)
            part.sudo().unlink()
            if self.kind == 'free':
                player._release(match=self)
            if self.creator_id == player:
                heirs = self._active().sorted('id')
                if heirs:
                    self.creator_id = heirs[0].player_id
                else:
                    self._cancel()
        elif self.state == 'running':
            part.left = True
            if self.kind == 'free':
                player._release(match=self)
            if self.phase == 'hand' and self.hand_participant_id == part:
                self._wrong(part, None, now, kind='timeout')
            self._check_abandon(now)
        self._bump()
        return self

    def _check_abandon(self, at):
        """Plus assez de joueurs (ou plus d'équipe adverse) : le match s'arrête."""
        if self.state != 'running':
            return
        active = self._active()
        if self.mode == 'team':
            teams_alive = active.team_id
            if len(teams_alive) < 2:
                self._finish(at, outcome='abandon', decided_by=active[:1] if teams_alive else None)
        elif len(active) < 2:
            self._finish(at, outcome='abandon', decided_by=active[:1] or None)

    def kick(self, creator, participant_id):
        self.ensure_one()
        self._lock()
        self._check_lobby()
        self._check_creator(creator)
        part = self.participant_ids.filtered(lambda p: p.id == int(participant_id))
        if not part or part.player_id == creator:
            raise UserError(_("Joueur introuvable."))
        player = part.player_id
        if part.team_id:
            part.team_id.leave(player)
        part.sudo().unlink()
        if self.kind == 'free':
            player._release(match=self)
        self._bump()
        return self

    def update_settings(self, creator, values):
        self.ensure_one()
        self._lock()
        self._check_lobby()
        self._check_creator(creator)
        if self.kind != 'free':
            raise AccessError(_("Les réglages d'un duel sont ceux du tournoi."))
        merged = {
            'lang': self.lang, 'passages': [c for c, _l in (self.passage_codes or [])],
            'question_count': self.question_count, 'question_seconds': self.question_seconds,
            'hand_seconds': self.hand_seconds,
        }
        merged.update({k: v for k, v in values.items() if v is not None})
        vals = self._settings_from(merged, creator)
        if 'is_public' in values:
            vals['is_public'] = bool(values['is_public'])
        self.write(vals)
        self._bump()
        return self

    def start(self, creator, now=None):
        self.ensure_one()
        self._lock()
        self._check_lobby()
        self._check_creator(creator)
        self._begin(now or now_ts())
        return self

    def _begin(self, now):
        """Vérifie les joueurs, tire les questions, lance le compte à rebours."""
        active = self._active()
        if self.mode == 'team':
            team_min = bq_param(self.env, 'team_min_size')
            teams = self._teams()
            if len(teams) != 2:
                raise GameRefusal('TEAMS', _("Il faut exactement deux équipes."))
            for team in teams:
                members = active.filtered(lambda p, t=team: p.team_id == t)
                if len(members) < team_min and self.kind == 'free':
                    raise GameRefusal('TEAMS', _("L'équipe « %(team)s » doit compter au moins %(n)s joueurs.",
                                                 team=team.name, n=team_min))
            # Les joueurs restés sans équipe ne jouent pas : ils sont libérés.
            for part in active.filtered(lambda p: not p.team_id):
                if self.kind == 'free':
                    part.player_id._release(match=self)
                part.sudo().unlink()
            if self.kind == 'free':
                self.team_ids.member_ids.filtered(lambda m: m.state != 'member').sudo().unlink()
        elif len(active) < 2:
            raise GameRefusal('PLAYERS', _("Il faut au moins deux joueurs."))
        self._draw_questions()
        self.write({
            'state': 'running', 'phase': 'countdown', 'phase_until': now + COUNTDOWN_SECONDS, 'q_index': 0,
            'started_at': fields.Datetime.now(), 'locked_keys': [],
        })
        self._bump()

    def _draw_questions(self):
        from ..game import questions as qgen
        extra = TIEBREAK_EXTRA if self.kind == 'duel' else 0
        codes = [c for c, _l in (self.passage_codes or [])]
        exclude = frozenset()
        if self.tournament_id:
            # Dans un tournoi, chaque duel a ses propres questions autant que possible.
            exclude = frozenset(self.env['bq.match.question'].sudo().search(
                [('match_id.tournament_id', '=', self.tournament_id.id)]).mapped('qid'))
        rnd = random.Random(secrets.randbits(64))
        items = qgen.build_questions(codes, self.lang, self.question_count + extra, rnd, exclude_ids=exclude)
        if len(items) < self.question_count and exclude:
            items = qgen.build_questions(codes, self.lang, self.question_count + extra, rnd)
        if len(items) < self.question_count:
            raise GameRefusal('QUESTIONS', _("Pas assez de questions pour ces passages."))
        self.question_ids.sudo().unlink()
        self.env['bq.match.question'].sudo().create([{
            'match_id': self.id, 'sequence': i, 'qid': it['id'], 'prompt': it['prompt'],
            'options': it['options'], 'answer': it['answer'], 'explain': it.get('explain') or '',
            'ref': it.get('ref') or '', 'ref_label': it.get('ref_label') or '', 'source': it.get('source'),
            'is_tiebreak': i >= self.question_count,
        } for i, it in enumerate(items)])

    def _cancel(self):
        for match in self:
            if match.state in ('finished', 'cancelled'):
                continue
            if match.kind == 'free':
                match.participant_ids.player_id._release(match=match)
            match.write({'state': 'cancelled', 'phase': False})
            match._bump()

    # ------------------------------------------------------------------ équipes (rappels de bq.team)
    def create_team(self, player, name):
        self.ensure_one()
        self._lock()
        self._check_lobby()
        if self.mode != 'team':
            raise UserError(_("Ce match se joue en individuel."))
        part = self._participant(player)
        if part.team_id:
            raise UserError(_("Vous êtes déjà dans une équipe."))
        if len(self.team_ids) >= 2:
            raise UserError(_("Les deux équipes existent déjà : demandez à entrer dans l'une d'elles."))
        name = (name or '').strip()[:40] or _("Équipe de %s", player.pseudo)
        team = self.env['bq.team'].sudo().create({'name': name, 'captain_id': player.id, 'match_id': self.id})
        self.env['bq.team.member'].sudo().create({'team_id': team.id, 'player_id': player.id, 'state': 'member'})
        part.team_id = team
        self._bump()
        return team

    def _team_member_joined(self, team, player):
        if len(self._active().filtered(lambda p: p.player_id != player)) >= self.max_players:
            raise GameRefusal('FULL', _("Ce match est complet."))
        self._add_participant(player, team=team)
        self._bump()

    def _team_member_left(self, team, player):
        part = self.participant_ids.filtered(lambda p: p.player_id == player and p.team_id == team)
        part.team_id = False
        self._bump()

    def _team_dissolved(self, team):
        self._bump()

    # ================================================================== tâches planifiées
    @api.model
    def _cron_advance(self):
        """Fait avancer les matchs que plus personne ne fait avancer ; ferme les parties abandonnées."""
        now = now_ts()
        lobby_idle = fields.Datetime.now() - timedelta(minutes=bq_param(self.env, 'lobby_idle_minutes'))
        running_idle = fields.Datetime.now() - timedelta(minutes=bq_param(self.env, 'running_idle_minutes'))
        for match in self.sudo().search([('state', '=', 'running')]):
            try:
                with self.env.cr.savepoint():
                    match._lock()
                    match._advance(now)
                    if match.state == 'running' and match.last_activity < running_idle and \
                            not any(p.last_seen and now - p.last_seen < 120 for p in match.participant_ids):
                        match._finish(now, outcome='idle')
                        match._bump()
            except Exception:  # noqa: BLE001 — un match en échec ne bloque pas les autres
                _logger.exception("Match %s : avance planifiée impossible", match.code)
        stale = self.sudo().search([('state', '=', 'lobby'), ('kind', '=', 'free'), ('last_activity', '<', lobby_idle)])
        for match in stale:
            match._lock()
            match._cancel()


class BqMatchParticipant(models.Model):
    _name = 'bq.match.participant'
    _description = "Joueur dans un match"
    _order = 'id'

    match_id = fields.Many2one('bq.match', required=True, ondelete='cascade', index=True)
    player_id = fields.Many2one('bq.player', "Joueur", required=True, ondelete='cascade', index=True)
    pseudo = fields.Char(related='player_id.pseudo')
    team_id = fields.Many2one('bq.team', "Équipe", ondelete='set null')
    score = fields.Integer("Points")
    correct_count = fields.Integer("Bonnes réponses")
    wrong_count = fields.Integer("Erreurs")
    rank = fields.Integer("Rang")
    left = fields.Boolean("A quitté")
    ready = fields.Boolean("Prêt")
    last_seen = fields.Float("Vu pour la dernière fois (horodatage)", digits=(16, 3))

    _uniq = models.Constraint('UNIQUE (match_id, player_id)', "Ce joueur est déjà dans le match.")

    def _seen(self, now=None):
        """Présence : écrite au plus toutes les 10 s (écriture accessoire, sur une autre ligne que le match)."""
        now = now or now_ts()
        for part in self:
            if not part.last_seen or now - part.last_seen > 10:
                part.sudo().last_seen = now


class BqMatchQuestion(models.Model):
    _name = 'bq.match.question'
    _description = "Question d'un match"
    _order = 'match_id, sequence'

    match_id = fields.Many2one('bq.match', required=True, ondelete='cascade', index=True)
    sequence = fields.Integer("Ordre", required=True)
    qid = fields.Char("Identifiant", index=True)
    prompt = fields.Text("Question", required=True)
    options = fields.Json("Propositions")
    answer = fields.Integer("Bonne réponse (indice)")
    explain = fields.Text("Explication")
    ref = fields.Char("Référence")
    ref_label = fields.Char("Passage")
    source = fields.Selection([('reviewed', "Relue"), ('generated', "Tirée du texte")], "Origine")
    is_tiebreak = fields.Boolean("Question décisive")
    state = fields.Selection([('pending', "À venir"), ('asked', "Posée"), ('done', "Close")], default='pending',
                             required=True)
    asked_at = fields.Float("Posée à (horodatage)", digits=(16, 3))
    winner_participant_id = fields.Many2one('bq.match.participant', "A répondu juste", ondelete='set null')
    answer_ids = fields.One2many('bq.match.answer', 'question_id', "Réponses")

    def _reveal_data(self):
        self.ensure_one()
        return {
            'answer': self.answer, 'explain': self.explain or '', 'ref': self.ref or '',
            'winner_participant_id': self.winner_participant_id.id or None,
            'answers': [{'participant_id': a.participant_id.id, 'kind': a.kind, 'choice': a.choice,
                         'points': a.points} for a in self.answer_ids.sorted('id')],
        }


class BqMatchAnswer(models.Model):
    _name = 'bq.match.answer'
    _description = "Réponse donnée pendant un match"
    _order = 'id'

    question_id = fields.Many2one('bq.match.question', required=True, ondelete='cascade', index=True)
    participant_id = fields.Many2one('bq.match.participant', required=True, ondelete='cascade', index=True)
    team_id = fields.Many2one('bq.team', ondelete='set null')
    kind = fields.Selection([('correct', "Juste"), ('wrong', "Fausse"), ('timeout', "Main laissée filer")],
                            required=True)
    choice = fields.Integer("Choix")
    points = fields.Integer("Points")
    at = fields.Float("À (horodatage)", digits=(16, 3))
    delay = fields.Float("Délai avant la prise de main (s)", digits=(16, 3))
