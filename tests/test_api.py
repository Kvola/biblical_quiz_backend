# -*- coding: utf-8 -*-
"""L'API JSON-RPC du jeu en ligne, de bout en bout (jeton porteur, enveloppe success)."""
import json

from odoo.tests import HttpCase, tagged

API = '/quiz-biblique/api/v1'


@tagged('post_install', '-at_install')
class TestApi(HttpCase):

    def call(self, path, token=None, **params):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer %s' % token
        r = self.url_open(API + path, data=json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params}),
                          headers=headers)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertNotIn('error', body, body)
        return body['result']

    def account(self, phone):
        partner = self.env['res.partner'].create({'name': phone, 'phone': phone})
        return self.env['bq.api.session'].sudo()._issue(partner)

    def test_parcours_complet(self):
        self.assertTrue(self.call('/config')['websocket_version'])
        ta, tb = self.account('+2250700000001'), self.account('+2250700000002')
        self.assertEqual(self.call('/me')['error_code'], 'AUTH_REQUIRED')
        me = self.call('/me', ta)
        self.assertIsNone(me['player'])
        self.assertEqual(self.call('/match/create', ta, passages=['43:3'])['error_code'], 'NO_PLAYER')
        self.assertTrue(self.call('/player/register', ta, pseudo='Alice', lang='fr')['player']['channel'])
        refused = self.call('/player/register', tb, pseudo='alice')
        self.assertEqual(refused['error_code'], 'REFUSED')
        self.call('/player/register', tb, pseudo='Bruno')
        check = self.call('/passages/check', ta, passages=['43:3', '1:1-2'], lang='fr')
        self.assertEqual([p['label'] for p in check['passages']], ['Jean 3', 'Genèse 1–2'])
        self.assertGreaterEqual(check['available'], 10)
        created = self.call('/match/create', ta, passages=['43:3'], question_count=5, mode='solo')
        self.assertTrue(created['success'], created)
        code, match_id = created['match']['code'], created['match']['id']
        self.assertTrue(created['channel'].startswith('bq.match.'))
        listed = self.call('/open', tb)
        self.assertIn(code, [m['code'] for m in listed['matches']])
        joined = self.call('/join', tb, code=code.lower())
        self.assertEqual(joined['kind'], 'match')
        self.assertEqual(len(joined['match']['participants']), 2)
        # Seul le créateur lance.
        self.assertEqual(self.call('/match/start', tb, match_id=match_id)['error_code'], 'FORBIDDEN')
        started = self.call('/match/start', ta, match_id=match_id)
        self.assertEqual(started['match']['phase'], 'countdown')
        # Un inconnu ne lit pas l'état d'un match… sauf par l'écran de projection (code), sans réponse.
        tc = self.account('+2250700000003')
        self.call('/player/register', tc, pseudo='Carole')
        self.assertEqual(self.call('/match/state', tc, match_id=match_id)['error_code'], 'FORBIDDEN')
        watched = self.call('/match/watch', code=code)
        self.assertEqual(watched['match']['id'], match_id)
        self.assertIsNone(watched['me'])
        # Pendant le compte à rebours, pas de question ouverte.
        self.assertEqual(self.call('/match/buzz', ta, match_id=match_id)['error_code'], 'NOT_OPEN')
        # Version inchangée : réponse courte.
        version = started['match']['version']
        short = self.call('/match/state', ta, match_id=match_id, version=version)
        self.assertTrue(short.get('unchanged'))
        self.assertTrue(self.call('/match/leave', tb, match_id=match_id)['success'])
        state = self.call('/match/state', ta, match_id=match_id)['match']
        self.assertEqual(state['state'], 'finished')
        self.assertEqual(state['result']['outcome'], 'abandon')
        self.assertTrue(self.call('/player/history', ta)['matches'])

    def test_page_d_invitation(self):
        page = self.url_open('/quiz-biblique/rejoindre/ab12cd')
        self.assertEqual(page.status_code, 200)
        self.assertIn('AB12CD', page.text)
        self.assertIn('scheme=quizbiblique', page.text)
        self.assertIn('/apps/quiz-biblique/web/?join=AB12CD', page.text)
        # Le lien français donne la page en français (« rejoindre » contient « join »).
        self.assertIn('Rejoindre une partie de Quiz Biblique', page.text)
        self.assertIn('Join a Bible Quiz game', self.url_open('/quiz-biblique/join/ab12cd').text)
