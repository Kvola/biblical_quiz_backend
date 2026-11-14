# -*- coding: utf-8 -*-
"""Générateur de questions des matchs (game/questions.py), lancé par Odoo.

La batterie complète, sans Odoo : python3 -m unittest game.tests_standalone -v
"""
import random

from odoo.tests.common import BaseCase, tagged

from ..game import questions as Q
from ..game.tests_standalone import Checks, all_curated_ids


@tagged('post_install', '-at_install')
class TestQuestions(Checks, BaseCase):

    def test_parse_and_labels(self):
        self.check_parse_and_labels()

    def test_counts_and_validity(self):
        for passages, count in ((['43:3'], 15), (['1:1-3'], 15), (['45:8', '43:1'], 20)):
            for lang in ('fr', 'en'):
                qs = Q.build_questions(passages, lang, count, random.Random(0))
                self.assertEqual(len(qs), count)
                self.assert_valid(qs, passages, lang)

    def test_deterministic(self):
        a = Q.build_questions(['45:8', '43:1'], 'fr', 15, random.Random(9), all_curated_ids())
        b = Q.build_questions(['45:8', '43:1'], 'fr', 15, random.Random(9), all_curated_ids())
        self.assertEqual(a, b)
        self.assertTrue(any(q['id'].startswith(Q.ID_PREFIX + 'book-') for q in a))

    def test_tiny_passage(self):
        qs = Q.build_questions(['43:3:16-18'], 'fr', 10, random.Random(0))
        self.assertEqual(len(qs), Q.available_count(['43:3:16-18'], 'fr'))
        self.assert_valid(qs, ['43:3:16-18'], 'fr')

    def test_exclude_ids(self):
        first = Q.build_questions(['43:3'], 'en', 10, random.Random(0))
        ex = frozenset(q['id'] for q in first)
        second = Q.build_questions(['43:3'], 'en', 10, random.Random(1), ex)
        self.assertFalse(ex & {q['id'] for q in second})
