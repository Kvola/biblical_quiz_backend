# -*- coding: utf-8 -*-
"""Le jeu en ligne : règles de la main, chrono en pause, équipes, tournois.

Le temps est passé explicitement (``now``) : les tests rejouent des secondes
précises sans attendre.
"""
import time

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged

from ..models.bq_match import ANSWER_GRACE, BUZZ_GRACE, COUNTDOWN_SECONDS, GameRefusal

T0 = float(int(time.time()))
JEAN_3 = ['43:3']


class GameCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Player = cls.env['bq.player'].sudo()
        cls.Match = cls.env['bq.match'].sudo()
        cls.Tournament = cls.env['bq.tournament'].sudo()
        cls._n = 0

    def player(self, pseudo):
        partner = self.env['res.partner'].create({'name': pseudo, 'phone': '+22507%08d' % (len(pseudo) * 1000 + id(pseudo) % 1000)})
        return self.Player.create({'partner_id': partner.id, 'pseudo': pseudo})

    def match(self, creator, **values):
        vals = {'passages': JEAN_3, 'question_count': 5, 'lang': 'fr'}
        vals.update(values)
        return self.Match.create_free(creator, vals)

    def started(self, *players, **values):
        m = self.match(players[0], **values)
        for p in players[1:]:
            m.join(p)
        m.start(players[0], now=T0)
        m._lock()
        m._advance(T0 + COUNTDOWN_SECONDS)
        self.assertEqual(m.phase, 'open')
        return m

    def q(self, m):
        return m._current_question()

    def part(self, m, player):
        return m.participant_ids.filtered(lambda p: p.player_id == player)

    def wrong_choice(self, m):
        return (self.q(m).answer + 1) % len(self.q(m).options)


@tagged('post_install', '-at_install')
class TestPlayers(GameCase):

    def test_pseudo_unique_sans_accents_ni_casse(self):
        self.player('Élodie')
        with self.assertRaises(ValidationError):
            self.player('elodie')
        with self.assertRaises(ValidationError):
            self.player('E-lodie')

    def test_pseudo_refuse(self):
        for bad in ('ab', 'x' * 25, 'Satan', 'con', '???'):
            with self.assertRaises(ValidationError, msg=bad):
                self.player(bad)
        # Un mot court interdit n'est refusé qu'entier.
        self.assertTrue(self.player('Constance'))

    def test_un_seul_engagement_a_la_fois(self):
        a, b, c = self.player('Alice'), self.player('Bruno'), self.player('Carole')
        m1 = self.match(a)
        self.assertEqual(a.match_id, m1)
        with self.assertRaises(UserError):
            self.match(a)
        m2 = self.match(b)
        with self.assertRaises(UserError):
            m2.join(a)
        t = self.Tournament.create_tournament(c, {'name': 'Tournoi', 'passages': JEAN_3, 'question_count': 5})
        with self.assertRaises(UserError):
            t.register(a)
        t.register(c)
        with self.assertRaises(UserError):
            m1.join(c)
        # Quitter libère.
        m1.leave(a)
        self.assertFalse(a.match_id)
        m2.join(a)

    def test_limite_de_matchs_simultanes(self):
        self.env['ir.config_parameter'].sudo().set_param('biblical_quiz.max_live_matches', '1')
        self.match(self.player('Alice'))
        with self.assertRaises(GameRefusal) as cm:
            self.match(self.player('Bruno'))
        self.assertEqual(cm.exception.code, 'SERVER_BUSY')

    def test_limite_de_joueurs(self):
        self.env['ir.config_parameter'].sudo().set_param('biblical_quiz.max_players', '2')
        m = self.match(self.player('Alice'))
        m.join(self.player('Bruno'))
        with self.assertRaises(GameRefusal) as cm:
            m.join(self.player('Carole'))
        self.assertEqual(cm.exception.code, 'FULL')

    def test_passages_insuffisants(self):
        with self.assertRaises(UserError):
            self.match(self.player('Alice'), passages=['43:11:35'], question_count=10)
        with self.assertRaises(UserError):
            self.match(self.player('Bruno'), passages=['43:99'])


@tagged('post_install', '-at_install')
class TestHand(GameCase):

    def setUp(self):
        super().setUp()
        self.a, self.b, self.c = self.player('Alice'), self.player('Bruno'), self.player('Carole')

    def test_la_reponse_ne_quitte_pas_le_serveur_avant_la_fin(self):
        m = self.started(self.a, self.b)
        state = m._state()
        self.assertNotIn('answer', state['question'])
        self.assertNotIn('explain', state['question'])
        m.buzz(self.a, now=T0 + 5)
        self.assertNotIn('answer', m._state()['question'])
        m.answer(self.a, self.q(m).answer, now=T0 + 6)
        self.assertEqual(m.phase, 'reveal')
        self.assertIn('answer', m._state()['question'])

    def test_bonne_reponse(self):
        m = self.started(self.a, self.b)
        m.buzz(self.a, now=T0 + 10)
        self.assertEqual(m.phase, 'hand')
        self.assertTrue(m.answer(self.a, self.q(m).answer, now=T0 + 12))
        self.assertEqual(self.part(m, self.a).score, 10)
        self.assertEqual(m.phase, 'reveal')
        # Après l'affichage, la question suivante.
        m._advance(m.phase_until + 0.01)
        self.assertEqual((m.phase, m.q_index), ('open', 1))

    def test_main_prise_par_un_autre(self):
        m = self.started(self.a, self.b)
        m.buzz(self.a, now=T0 + 10)
        with self.assertRaises(GameRefusal) as cm:
            m.buzz(self.b, now=T0 + 10.01)
        self.assertEqual(cm.exception.code, 'HAND_TAKEN')

    def test_erreur_moins_cinq_et_chrono_en_pause(self):
        m = self.started(self.a, self.b, self.c, question_seconds=30, hand_seconds=5)
        asked = T0 + COUNTDOWN_SECONDS
        m.buzz(self.a, now=asked + 10)                   # 20 s restantes, mises en pause
        self.assertAlmostEqual(m.q_remaining, 20, places=3)
        self.assertFalse(m.answer(self.a, self.wrong_choice(m), now=asked + 14))  # 4 s de main
        self.assertEqual(self.part(m, self.a).score, -5)
        self.assertEqual(m.phase, 'open')
        # Le chrono reprend : 20 s à partir de la réponse, pas 16.
        self.assertAlmostEqual(m.q_deadline, asked + 14 + 20, places=3)
        with self.assertRaises(GameRefusal) as cm:
            m.buzz(self.a, now=asked + 15)
        self.assertEqual(cm.exception.code, 'LOCKED')
        m.buzz(self.b, now=asked + 15)
        self.assertEqual(m.hand_participant_id, self.part(m, self.b))

    def test_main_laissee_filer(self):
        m = self.started(self.a, self.b, hand_seconds=5)
        asked = T0 + COUNTDOWN_SECONDS
        m.buzz(self.a, now=asked + 2)
        # Dans la marge réseau, la réponse est encore acceptée…
        m._advance(asked + 7 + ANSWER_GRACE / 2)
        self.assertEqual(m.phase, 'hand')
        # … au-delà, la main est perdue : −5 et le chrono reprend pour les autres.
        m._advance(asked + 7 + ANSWER_GRACE + 0.01)
        self.assertEqual(m.phase, 'open')
        self.assertEqual(self.part(m, self.a).score, -5)
        self.assertEqual(m.question_ids.answer_ids.kind, 'timeout')
        with self.assertRaises(GameRefusal):
            m.answer(self.a, self.q(m).answer, now=asked + 9)

    def test_tous_bloques_la_question_se_ferme(self):
        m = self.started(self.a, self.b)
        m.buzz(self.a, now=T0 + 5)
        m.answer(self.a, self.wrong_choice(m), now=T0 + 6)
        m.buzz(self.b, now=T0 + 7)
        m.answer(self.b, self.wrong_choice(m), now=T0 + 8)
        self.assertEqual(m.phase, 'reveal')
        self.assertFalse(self.q(m).winner_participant_id)

    def test_personne_ne_repond(self):
        m = self.started(self.a, self.b, question_seconds=10)
        asked = T0 + COUNTDOWN_SECONDS
        m._advance(asked + 10 + BUZZ_GRACE / 2)
        self.assertEqual(m.phase, 'open')
        m._advance(asked + 10 + BUZZ_GRACE + 0.01)
        self.assertEqual(m.phase, 'reveal')
        self.assertEqual([p.score for p in m.participant_ids], [0, 0])

    def test_trop_tard(self):
        m = self.started(self.a, self.b, question_seconds=10)
        with self.assertRaises(GameRefusal) as cm:
            m.buzz(self.a, now=T0 + COUNTDOWN_SECONDS + 10 + BUZZ_GRACE + 1)
        self.assertEqual(cm.exception.code, 'NOT_OPEN')

    def test_match_complet_classement_et_liberation(self):
        m = self.started(self.a, self.b, self.c)
        t = T0 + 10
        for i in range(5):
            self.assertEqual(m.q_index, i)
            winner = self.a if i < 3 else self.b
            m.buzz(winner, now=t)
            m.answer(winner, self.q(m).answer, now=t + 1)
            t = m.phase_until + 0.01
            m._advance(t)
            t += 1
        self.assertEqual(m.state, 'finished')
        self.assertEqual(m.winner_participant_id.player_id, self.a)
        ranks = {p.player_id.pseudo: p.rank for p in m.participant_ids}
        self.assertEqual(ranks, {'Alice': 1, 'Bruno': 2, 'Carole': 3})
        self.assertEqual((self.a.matches_played, self.a.matches_won), (1, 1))
        self.assertFalse(self.a.match_id | self.b.match_id | self.c.match_id)
        history = m._history()
        self.assertEqual(len(history), 5)
        self.assertIn('explain', history[0])

    def test_abandon(self):
        m = self.started(self.a, self.b)
        m.leave(self.b, now=T0 + 6)
        self.assertEqual(m.state, 'finished')
        self.assertEqual(m.result, 'abandon')
        self.assertEqual(m.winner_participant_id.player_id, self.a)

    def test_createur_qui_part_passe_la_main(self):
        m = self.match(self.a)
        m.join(self.b)
        m.leave(self.a)
        self.assertEqual(m.creator_id, self.b)
        m.leave(self.b)
        self.assertEqual(m.state, 'cancelled')


@tagged('post_install', '-at_install')
class TestTeams(GameCase):

    def setUp(self):
        super().setUp()
        self.ps = [self.player(n) for n in ('Anne', 'Bertrand', 'Chloé', 'Didier', 'Emma')]

    def test_match_par_equipes(self):
        a1, a2, b1, b2, x = self.ps
        m = self.match(a1, mode='team')
        ta = m.create_team(a1, 'Lions')
        ta.invite(a1, a2)
        self.assertEqual(a2.match_id.id, False, "inviter n'engage pas")
        ta.accept(a2, a2)
        self.assertEqual(a2.match_id, m)
        m.join(b1)
        tb = m.create_team(b1, 'Aigles')
        with self.assertRaises(GameRefusal):
            m.start(a1, now=T0)          # Aigles : un seul joueur
        m.join(b2)
        tb.request(b2)
        with self.assertRaises(AccessError):
            tb.accept(b2, b2)            # seul le capitaine accepte une demande
        tb.accept(b1, b2)
        m.join(x)                        # sans équipe : ne jouera pas
        with self.assertRaises(UserError):
            m.create_team(x, 'Troisième')
        m.start(a1, now=T0)
        self.assertFalse(x.match_id, "le joueur sans équipe est libéré au lancement")
        m._advance(T0 + COUNTDOWN_SECONDS)
        # L'erreur d'Anne bloque TOUTE son équipe.
        m.buzz(a1, now=T0 + 5)
        m.answer(a1, self.wrong_choice(m), now=T0 + 6)
        with self.assertRaises(GameRefusal) as cm:
            m.buzz(a2, now=T0 + 7)
        self.assertEqual(cm.exception.code, 'LOCKED')
        m.buzz(b2, now=T0 + 7)
        m.answer(b2, self.q(m).answer, now=T0 + 8)
        state = m._state()
        scores = {t['name']: t['score'] for t in state['teams']}
        self.assertEqual(scores, {'Lions': -5, 'Aigles': 10})

    def test_capitaine_qui_part(self):
        a1, a2 = self.ps[:2]
        m = self.match(a1, mode='team')
        team = m.create_team(a1, 'Lions')
        team.invite(a1, a2)
        team.accept(a2, a2)
        m.leave(a1)
        self.assertEqual(team.captain_id, a2)
        self.assertEqual(m.creator_id, a2)


@tagged('post_install', '-at_install')
class TestTournament(GameCase):

    def play_duel(self, duel, winner_player, now):
        """Les deux camps se déclarent prêts ; ``winner_player`` répond juste à toutes les questions."""
        players = duel.entry_a_id._players()[:1] | duel.entry_b_id._players()[:1]
        for p in players:
            duel.ready(p, now=now)
        m = duel.match_id
        self.assertEqual(m.state, 'running')
        m._lock()
        t = now + COUNTDOWN_SECONDS
        m._advance(t)
        while m.state == 'running':
            m.buzz(winner_player, now=t + 1)
            m.answer(winner_player, self.q(m).answer, now=t + 2)
            t = m.phase_until + 0.01
            m._advance(t)
        return t

    def test_tournoi_individuel_complet(self):
        ps = [self.player(n) for n in ('Paul', 'Pierre', 'Jacques', 'Jean', 'André')]
        org = self.player('Orga')
        t = self.Tournament.create_tournament(org, {'name': 'Coupe', 'passages': JEAN_3, 'question_count': 5,
                                                    'min_entries': 2, 'max_entries': 8})
        self.assertFalse(org.tournament_id, "l'organisateur n'est pas forcément joueur")
        for p in ps:
            t.register(p)
        self.assertTrue(all(p.tournament_id == t for p in ps))
        t.start(org)
        self.assertEqual(t.rounds, 3)
        round1 = t.duel_ids.filtered(lambda d: d.round == 1)
        self.assertEqual(len(round1), 4)
        self.assertEqual(len(round1.filtered(lambda d: d.result == 'bye')), 3)
        now = T0
        for _round in range(10):
            waiting = t.duel_ids.filtered(lambda d: d.state == 'ready_wait')
            if not waiting:
                break
            for duel in waiting:
                winner = duel.entry_a_id._players()[:1]
                now = self.play_duel(duel, winner, now) + 1
        self.assertEqual(t.state, 'finished')
        self.assertTrue(t.winner_entry_id)
        champion = t.winner_entry_id.player_id
        self.assertEqual(champion.tournaments_won, 1)
        self.assertFalse(any(p.tournament_id for p in ps), "tout le monde est libéré à la fin")

    def test_forfait_et_double_forfait(self):
        ps = [self.player(n) for n in ('Marc', 'Luc', 'Tite', 'Jude')]
        t = self.Tournament.create_tournament(ps[0], {'name': 'Coupe', 'passages': JEAN_3, 'question_count': 5,
                                                      'forfeit_minutes': 5, 'play': True})
        for p in ps[1:]:
            t.register(p)
        t.start(ps[0])
        d1, d2 = t.duel_ids.filtered(lambda d: d.round == 1).sorted('position')
        d1.ready(d1.entry_a_id.player_id, now=T0)
        t._check_deadlines(d1.deadline + 1)
        self.assertEqual((d1.result, d1.winner_entry_id), ('forfeit', d1.entry_a_id))
        self.assertEqual((d2.result, d2.winner_entry_id.id), ('double_forfeit', False))
        # Le vainqueur de d1 passe la finale sans adversaire.
        self.assertEqual(t.state, 'finished')
        self.assertEqual(t.winner_entry_id, d1.entry_a_id)
        loser = d1.entry_b_id.player_id
        self.assertFalse(loser.tournament_id, "un éliminé est libéré")

    def test_egalite_question_decisive(self):
        a, b = self.player('Ruth'), self.player('Noémi')
        t = self.Tournament.create_tournament(a, {'name': 'Duel', 'passages': ['43:3', '43:4'], 'question_count': 5,
                                                  'play': True})
        t.register(b)
        t.start(a)
        duel = t.duel_ids
        duel.ready(a, now=T0)
        duel.ready(b, now=T0)
        m = duel.match_id
        self.assertEqual(len(m.question_ids.filtered('is_tiebreak')), 3)
        m._lock()
        now = T0 + COUNTDOWN_SECONDS
        m._advance(now)
        # Personne ne répond aux 5 questions : égalité 0-0.
        while not m.tiebreak and m.state == 'running':
            now = max(m.q_deadline or 0, m.phase_until or 0) + BUZZ_GRACE + 0.01
            m._advance(now)
        self.assertTrue(m.tiebreak)
        self.assertTrue(self.q(m).is_tiebreak)
        # Une erreur en question décisive ne coûte rien ; la bonne réponse de Noémi décide.
        m.buzz(a, now=now + 1)
        m.answer(a, self.wrong_choice(m), now=now + 2)
        self.assertEqual(self.part(m, a).score, 0)
        m.buzz(b, now=now + 3)
        m.answer(b, self.q(m).answer, now=now + 4)
        m._advance(m.phase_until + 0.01)
        self.assertEqual(m.state, 'finished')
        self.assertEqual(m.result, 'tiebreak')
        self.assertEqual(m.winner_participant_id.player_id, b)
        self.assertEqual(t.winner_entry_id.player_id, b)

    def test_tournoi_par_equipes_equipe_incomplete_ecartee(self):
        ps = [self.player(n) for n in ('Abel', 'Caïn', 'Seth', 'Énoch', 'Noé', 'Sem')]
        org = self.player('Orga')
        t = self.Tournament.create_tournament(org, {'name': 'Équipes', 'mode': 'team', 'passages': JEAN_3,
                                                    'question_count': 5, 'min_entries': 2})
        t1 = t.create_team(ps[0], 'Alpha')
        t1.invite(ps[0], ps[1])
        t1.accept(ps[1], ps[1])
        t2 = t.create_team(ps[2], 'Beta')
        t2.invite(ps[2], ps[3])
        t2.accept(ps[3], ps[3])
        t.create_team(ps[4], 'Gamma')                   # seul : sera écartée
        with self.assertRaises(UserError):
            t.register(ps[5])                            # pas d'inscription individuelle
        t.start(org)
        self.assertEqual(len(t.entry_ids), 2)
        self.assertFalse(ps[4].tournament_id)
        duel = t.duel_ids
        self.assertEqual(duel.match_id.mode, 'team')
        self.assertEqual(len(duel.match_id.participant_ids), 4)
        self.play_duel(duel, ps[1], T0)
        self.assertEqual(t.winner_entry_id.team_id, t1)

    def test_annulation_libere(self):
        a, b = self.player('Ruth'), self.player('Boaz')
        t = self.Tournament.create_tournament(a, {'name': 'Coupe', 'passages': JEAN_3, 'question_count': 5, 'play': True})
        t.register(b)
        t.cancel(a)
        self.assertFalse(a.tournament_id | b.tournament_id)
        with self.assertRaises(AccessError):
            t2 = self.Tournament.create_tournament(a, {'name': 'Coupe 2', 'passages': JEAN_3, 'question_count': 5})
            t2.cancel(b)
