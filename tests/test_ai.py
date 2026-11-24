# -*- coding: utf-8 -*-
"""Banque de questions rédigées par IA : quota, file, contrôles, relecture, signalements.

Aucun appel réel à Claude : ``_call_claude`` est remplacé.
"""
import json
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import HttpCase, TransactionCase, tagged

from ..game import ai_bank, ai_fixtures as F
from ..game import questions as qgen
from ..models.bq_ai import AiCallError, AiRefusal, BqAiChapter

API = '/quiz-biblique/api/v1'
KEY_A, KEY_B = 'appareil-test-0000000001', 'appareil-test-0000000002'


def fake_claude(generation=None, verdicts=None, fail=None):
    """Remplace ``_call_claude`` : 1er appel = rédaction, 2e = relecture."""
    calls = []

    def _call(self, system, message, schema, effort):
        calls.append(schema)
        if fail:
            raise fail
        if schema is ai_bank.GENERATION_SCHEMA:
            return (generation if generation is not None else F.generation(), 3000, 4000, 'claude-opus-5-5')
        return (verdicts if verdicts is not None else F.verdicts(), 5000, 800, 'claude-opus-5-5')
    return patch.object(BqAiChapter, '_call_claude', _call), calls


class AiCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Chapter = cls.env['bq.ai.chapter'].sudo()
        cls.Param = cls.env['ir.config_parameter'].sudo()
        cls.Param.set_param('biblical_quiz.anthropic_api_key', 'sk-test')
        cls.dev_a = cls.env['bq.ai.device'].sudo()._for_key(KEY_A)
        cls.dev_b = cls.env['bq.ai.device'].sudo()._for_key(KEY_B)

    def ready(self, code='43:3', device=None, **fake):
        patcher, calls = fake_claude(**fake)
        record = self.Chapter._request(device or self.dev_a, 'ip1', code)
        with patcher:
            self.Chapter._cron_generate()
        return record, calls


@tagged('post_install', '-at_install')
class TestAiBank(AiCase):

    def test_demande_file_redaction_lecture(self):
        self.assertEqual(self.Chapter._bank(['43:3', 'x', '43:99'])[0], {'code': '43:3', 'state': 'none', 'rev': 0})
        self.assertEqual(self.Chapter._quota_info(self.dev_a)['remaining'], 3)
        record = self.Chapter._request(self.dev_a, 'ip1', '43:3')
        self.assertEqual(record.state, 'queued')
        self.assertEqual(self.Chapter._quota_info(self.dev_a)['remaining'], 2)
        # Un autre lecteur demande le même chapitre : rien ne lui est compté.
        self.assertEqual(self.Chapter._request(self.dev_b, 'ip2', '43:3'), record)
        self.assertEqual(self.Chapter._quota_info(self.dev_b)['remaining'], 3)
        patcher, calls = fake_claude()
        with patcher:
            self.assertEqual(self.Chapter._cron_generate(), 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual((record.state, record.rev, record.served_count, record.unreviewed_count), ('ready', 1, 3, 3))
        self.assertEqual(record.request_state(), ['done'])
        gen = record.generation_ids
        self.assertEqual((gen.state, gen.kept, gen.input_tokens, gen.output_tokens), ('done', 3, 8000, 4800))
        self.assertAlmostEqual(gen.cost, (8000 * 4 + 4800 * 20) / 1e6, places=5)
        bank = self.Chapter._bank(['43:3'])[0]
        q = bank['questions'][0]
        self.assertTrue(q['id'].startswith('gen-ai-'))
        self.assertEqual((q['kind'], q['ref'], q['reviewed'], len(q['choices']['en'])), ('choice', '43:3:16', False, 4))
        self.assertNotIn('_start', q)
        # Le téléphone qui a déjà cette révision ne retélécharge rien.
        self.assertNotIn('questions', self.Chapter._bank(['43:3'], {'43:3': 1})[0])
        self.assertIn('questions', self.Chapter._bank(['43:3'], {'43:3': 'x'})[0])
        # Chapitre prêt : le redemander est gratuit.
        self.assertEqual(self.Chapter._request(self.dev_b, 'ip2', '43:3').state, 'ready')
        self.assertEqual(self.Chapter._quota_info(self.dev_b)['remaining'], 3)

    def test_quota_par_appareil_et_plafonds(self):
        for code in ('43:1', '43:2', '43:3'):
            self.Chapter._request(self.dev_a, 'ip1', code)
        try:    # pas assertRaises : il annulerait la trace « refused » qu'on veut voir
            self.Chapter._request(self.dev_a, 'ip1', '43:4')
            self.fail("quota non appliqué")
        except AiRefusal as e:
            self.assertEqual(e.reason, 'quota')
        self.assertEqual(self.env['bq.ai.request'].search_count([('state', '=', 'refused')]), 1)
        self.assertFalse(self.Chapter.search([('code', '=', '43:4')]))
        # Plafond du serveur.
        self.Param.set_param('biblical_quiz.ai_daily_global_cap', '3')
        with self.assertRaises(AiRefusal) as e:
            self.Chapter._request(self.dev_b, 'ip2', '43:5')
        self.assertEqual(e.exception.reason, 'server_quota')
        self.assertEqual(self.Chapter._quota_info(self.dev_b)['limited'], 'server_quota')
        # Plafond par réseau.
        self.Param.set_param('biblical_quiz.ai_daily_global_cap', '40')
        self.Param.set_param('biblical_quiz.ai_ip_daily_cap', '3')
        with self.assertRaises(AiRefusal) as e:
            self.Chapter._request(self.dev_b, 'ip1', '43:5')
        self.assertEqual(e.exception.reason, 'network_quota')
        self.dev_b.blocked = True
        with self.assertRaises(AiRefusal) as e:
            self.Chapter._request(self.dev_b, 'ip2', '43:5')
        self.assertEqual(e.exception.reason, 'blocked')

    def test_sans_cle_ou_suspendu(self):
        self.Param.set_param('biblical_quiz.ai_suspended', 'True')
        with self.assertRaises(AiRefusal) as e:
            self.Chapter._request(self.dev_a, 'ip1', '43:3')
        self.assertEqual(e.exception.reason, 'disabled')
        self.Param.set_param('biblical_quiz.ai_suspended', False)
        self.Param.set_param('biblical_quiz.anthropic_api_key', False)
        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': ''}):
            with self.assertRaises(AiRefusal) as e:
                self.Chapter._request(self.dev_a, 'ip1', '43:3')
            self.assertEqual(e.exception.reason, 'not_configured')
            self.assertFalse(self.Chapter._quota_info(self.dev_a)['enabled'])
        for bad in ('99:1', '43', None):
            with self.assertRaises(AiRefusal):
                self.Chapter._request(self.dev_a, 'ip1', bad)
        with self.assertRaises(AiRefusal):
            self.env['bq.ai.device'].sudo()._for_key('court')

    def test_panne_reessais_puis_echec_rend_le_quota(self):
        record = self.Chapter._request(self.dev_a, 'ip1', '43:3')
        patcher, _calls = fake_claude(fail=AiCallError('service indisponible', retry=True))
        with patcher:
            self.Chapter._cron_generate()
            self.assertEqual((record.state, record.attempts), ('queued', 1))
            self.assertEqual(self.Chapter._quota_info(self.dev_a)['remaining'], 2)
            self.Chapter._cron_generate()
            self.Chapter._cron_generate()
        self.assertEqual((record.state, record.attempts), ('failed', 3))
        self.assertEqual(record.request_state(), ['failed'])
        self.assertEqual(self.Chapter._quota_info(self.dev_a)['remaining'], 3)
        # Une nouvelle demande le remet en file.
        self.assertEqual(self.Chapter._request(self.dev_a, 'ip1', '43:3').state, 'queued')
        self.assertEqual(record.attempts, 0)

    def test_erreur_definitive_sans_reessai(self):
        record, calls = self.ready(fail=AiCallError("erreur de l'API (401)", retry=False))
        self.assertEqual((record.state, len(calls)), ('failed', 1))

    def test_rien_ne_passe_les_controles(self):
        bad = dict(F.good(16), quote_fr="une citation inventée de toutes pièces")
        record, calls = self.ready(generation=F.generation(bad))
        self.assertEqual((record.state, len(calls)), ('empty', 1))      # pas de relecture à payer pour rien
        self.assertIn('citation justificative', record.generation_ids.dropped_detail)
        self.assertEqual(record.request_state(), ['rejected'])
        self.assertEqual(self.Chapter._quota_info(self.dev_a)['remaining'], 2)   # appel facturé : compté
        with self.assertRaises(AiRefusal) as e:
            self.Chapter._request(self.dev_b, 'ip2', '43:3')
        self.assertEqual(e.exception.reason, 'no_questions')

    def test_relecture_independante_ecarte(self):
        verdicts = F.verdicts(F.verdict(1, 'B'), F.verdict(2, 'B', single_answer=False, note="B aussi"),
                              F.verdict(3, 'C'))
        record, _calls = self.ready(verdicts=verdicts)
        self.assertEqual((record.state, record.served_count), ('ready', 1))
        self.assertEqual(record.question_ids.verse, 19)
        self.assertIn('B aussi', record.generation_ids.dropped_detail)
        record2, _calls = self.ready('43:4', verdicts={'verdicts': []})
        self.assertEqual(record2.state, 'empty')

    def test_redaction_interrompue(self):
        record = self.Chapter._request(self.dev_a, 'ip1', '43:3')
        record.write({'state': 'pending', 'pending_since': '2020-01-01 00:00:00', 'attempts': 1})
        patcher, _calls = fake_claude()
        with patcher:
            self.Chapter._cron_generate()
        self.assertEqual(record.state, 'ready')
        record.write({'state': 'pending', 'pending_since': '2020-01-01 00:00:00', 'attempts': 3})
        self.Param.set_param('biblical_quiz.ai_suspended', 'True')
        self.Chapter._cron_generate()
        self.assertEqual(record.state, 'failed')

    def test_relecture_humaine_revision_et_nouvelle_redaction(self):
        record, _calls = self.ready()
        first = record.question_ids.sorted('id')[0]
        first.action_validate()
        self.assertEqual(record.rev, 2)
        self.assertTrue(self.Chapter._bank(['43:3'])[0]['questions'][0]['reviewed'])
        first.q_fr = "Question corrigée ?"
        self.assertEqual(record.rev, 3)
        first.quote_fr = "sans effet sur ce qui est servi"
        self.assertEqual(record.rev, 3)
        record.question_ids[1].action_withdraw()
        self.assertEqual((record.rev, record.served_count, record.unreviewed_count), (4, 2, 1))
        with self.assertRaises(UserError):
            first.choices_fr = "a\nb\nc"
        with self.assertRaises(UserError):
            first.answer = 7
        with self.assertRaises(UserError):
            first.verse = 99
        # Nouvelle rédaction : la question relue reste, les non relues sont remplacées.
        record.action_queue()
        patcher, _calls = fake_claude(generation=F.generation(F.good(3, 1)), verdicts=F.verdicts(F.verdict(1, 'B')))
        with patcher:
            self.Chapter._cron_generate()
        self.assertEqual(record.state, 'ready')
        states = sorted(record.question_ids.mapped('state'))
        self.assertEqual(states, ['auto', 'validated', 'withdrawn'])
        self.assertIn(first, record.question_ids)

    def test_signalements(self):
        record, _calls = self.ready()
        q1, q2 = record.question_ids.sorted('id')[:2]
        Question = self.env['bq.ai.question'].sudo()
        rev = record.rev
        pid = 'gen-ai-%s' % q1.id
        Question._report(self.dev_a, pid, 'wrong', 'x' * 900)
        Question._report(self.dev_a, pid, 'several', None)          # même appareil : un seul signalement
        self.assertEqual((q1.report_count, q1.report_ids.reason, q1.state), (1, 'several', 'auto'))
        Question._report(self.dev_b, pid, 'n-importe-quoi', '')
        self.assertEqual(q1.report_ids.sorted('id')[-1].reason, 'other')
        dev_c = self.env['bq.ai.device'].sudo()._for_key('appareil-test-0000000003')
        Question._report(dev_c, pid, 'doctrine', 'orientée')
        self.assertEqual(q1.state, 'withdrawn')
        self.assertIn('3 signalements', q1.withdrawn_reason)
        self.assertEqual(record.rev, rev + 1)
        self.assertEqual(len(self.Chapter._bank(['43:3'])[0]['questions']), 2)
        # Une question relue n'est jamais retirée d'office.
        q2.action_validate()
        for device in (self.dev_a, self.dev_b, dev_c):
            Question._report(device, 'gen-ai-%s' % q2.id, 'wrong', '')
        self.assertEqual((q2.state, q2.report_count), ('validated', 3))
        for bad in ('gen-ai-999999999', 'gen-read-1', None):
            with self.assertRaises(AiRefusal):
                Question._report(self.dev_a, bad, 'wrong', '')

    def test_matchs_seulement_les_questions_relues(self):
        record, _calls = self.ready()
        passages = [qgen.parse_passage('43:3')]
        self.assertEqual(self.Chapter._match_bank(passages), [])
        record.question_ids.sorted('id')[0].action_validate()
        bank = self.Chapter._match_bank(passages)
        self.assertEqual((len(bank), bank[0]['_start']), (1, (43, 3, 16)))
        self.assertEqual(self.Chapter._match_bank([qgen.parse_passage('43:4')]), [])

    def test_preparer_un_livre(self):
        wizard = self.env['bq.ai.prepare'].create({'book': '57', 'chapter_from': 1, 'chapter_to': 9})
        action = wizard.action_prepare()
        self.assertEqual(action['domain'], [('book', '=', 57)])
        chapters = self.Chapter.search([('book', '=', 57)])
        self.assertEqual(chapters.mapped('state'), ['queued'])     # Philémon n'a qu'un chapitre
        self.assertFalse(self.env['bq.ai.request'].search([]))     # hors quota des lecteurs
        with self.assertRaises(UserError):
            self.env['bq.ai.prepare'].create({'book': '57', 'chapter_from': 3, 'chapter_to': 2}).action_prepare()
        with self.assertRaises(UserError):
            self.Chapter.create({'book': 57, 'chapter': 2})


@tagged('post_install', '-at_install')
class TestAiApi(HttpCase):

    def call(self, path, **params):
        r = self.url_open(API + path, data=json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params}),
                          headers={'Content-Type': 'application/json'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertNotIn('error', body, body)
        return body['result']

    def test_routes(self):
        self.env['ir.config_parameter'].sudo().set_param('biblical_quiz.anthropic_api_key', 'sk-test')
        bank = self.call('/ai/bank', chapters=['43:3'], device=KEY_A)
        self.assertEqual((bank['chapters'][0]['state'], bank['quota']['remaining']), ('none', 3))
        self.assertTrue(self.call('/ai/bank', chapters='x')['success'])            # sans appareil, forme douteuse
        asked = self.call('/ai/request', device=KEY_A, chapter='43:3')
        self.assertEqual((asked['chapter']['state'], asked['quota']['remaining']), ('queued', 2))
        self.assertEqual(self.call('/ai/request', device='x', chapter='43:3')['error_code'], 'AI_BAD_DEVICE')
        self.assertEqual(self.call('/ai/request', device=KEY_A, chapter='0:0')['error_code'], 'AI_BAD_CHAPTER')
        patcher, _calls = fake_claude()
        with patcher:
            self.env['bq.ai.chapter'].sudo()._cron_generate()
        bank = self.call('/ai/bank', chapters=['43:3'], known={'43:3': 0}, device=KEY_A)
        questions = bank['chapters'][0]['questions']
        self.assertEqual(len(questions), 3)
        reported = self.call('/ai/report', device=KEY_A, question=questions[0]['id'], reason='wrong', note='test')
        self.assertEqual((reported['success'], reported['withdrawn']), (True, False))
        self.assertEqual(self.call('/ai/report', device=KEY_A, question='gen-ai-0')['error']['details']['reason'],
                         'bad_question')
