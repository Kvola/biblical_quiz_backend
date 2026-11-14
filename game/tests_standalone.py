"""Tests du générateur de questions, sans Odoo.

    cd biblical_quiz_backend && python3 -m unittest game.tests_standalone -v

Les vérifications sont dans `Checks` (simple mixin), réutilisé par le test Odoo
tests/test_questions.py.
"""
import random
import time
import unittest

from . import questions as Q
from .books import book_name
from .text import normalize

GEN = Q.ID_PREFIX


def all_curated_ids():
    return frozenset(q['id'] for q in Q.curated_questions())


class Checks:
    """Vérifications communes (mixin pour unittest.TestCase ou odoo.tests.BaseCase)."""

    def assert_valid(self, qs, passages, lang):
        """Invariants d'un questionnaire : propositions, réponse, unicité, portée."""
        parsed = [Q.parse_passage(p) for p in passages]
        bible = Q.get_bible(lang)
        ids, verses = set(), set()
        for q in qs:
            self.assertEqual(set(q), {'id', 'prompt', 'options', 'answer', 'explain', 'ref', 'ref_label', 'source'})
            self.assertNotIn(q['id'], ids, 'id en double')
            ids.add(q['id'])
            self.assertTrue(2 <= len(q['options']) <= 4, q)
            self.assertEqual(len(set(q['options'])), len(q['options']), q)
            self.assertTrue(0 <= q['answer'] < len(q['options']), q)
            # Jamais de vrai/faux.
            low = {o.strip().lower() for o in q['options']}
            self.assertFalse(low <= {'vrai', 'faux', 'true', 'false'}, q)
            start = Q._parse_ref(q['ref'])[:3]
            self.assertTrue(any(p.contains(*start) for p in parsed), q['ref'])
            self.assertEqual(q['ref_label'], Q.ref_label(q['ref'], lang))
            if q['source'] == 'reviewed':
                self.assertFalse(q['id'].startswith(GEN))
                continue
            self.assertEqual(q['source'], 'generated')
            self.assertTrue(q['id'].startswith(GEN))
            self.assertEqual(len(q['options']), 4)
            kind = q['id'][len(GEN):].split('-')[0]
            self.assertIn(kind, ('cloze', 'book', 'order'))
            if kind == 'order':
                used = [tuple(int(x) for x in c.split(':')) for c in q['id'][len(GEN) + 6:].split('_')]
            else:
                used = [start]
            for key in used:
                self.assertNotIn(key, verses, 'verset employé deux fois')
                verses.add(key)
            text = bible.text(*start)
            if kind == 'cloze':
                # Le blanc est présent une fois, et le mot retiré est la bonne réponse.
                self.assertEqual(q['prompt'].count('_____'), 1)
                quoted = q['prompt'].split('\n', 1)[1]
                blanked = quoted[2:-2] if lang == 'fr' else quoted[1:-1]
                self.assertEqual(blanked.replace('_____', q['options'][q['answer']]), text)
                self.assertTrue(q['id'].endswith('-' + normalize(q['options'][q['answer']])))
            elif kind == 'book':
                self.assertIn(text, q['prompt'])
                self.assertEqual(q['options'][q['answer']], book_name(start[0], lang))
            else:
                excerpt = q['options'][q['answer']]
                self.assertTrue(text.startswith(excerpt.rstrip('…')), q)
                self.assertEqual(start, min(used))

    def check_parse_and_labels(self):
        p = Q.parse_passage('43:3:1-21')
        self.assertEqual((p.book, p.start_chapter, p.start_verse, p.end_chapter, p.end_verse), (43, 3, 1, 3, 21))
        self.assertEqual(Q.passage_label(p, 'fr'), 'Jean 3:1-21')
        self.assertEqual(Q.passage_label(p, 'en'), 'John 3:1-21')
        self.assertEqual(Q.passage_label('1:1-3', 'fr'), 'Genèse 1–3')
        self.assertEqual(Q.passage_label('1:1-3', 'en'), 'Genesis 1–3')
        self.assertEqual(Q.passage_label('43:3', 'fr'), 'Jean 3')
        self.assertEqual(Q.passage_label('43:3:16', 'fr'), 'Jean 3:16')
        self.assertEqual(Q.passage_label('43:3:36-4:2', 'fr'), 'Jean 3:36–4:2')
        whole = Q.parse_passage('43:3')
        self.assertTrue(whole.whole_chapters)
        self.assertEqual((whole.start_verse, whole.end_verse), (1, 36))
        self.assertEqual(Q.parse_passage(' 1:1-3 ').code, '1:1-3')
        self.assertEqual(Q.parse_passage('1:2-2').code, '1:2')
        for bad in ('43:22', '43:3:37', '43:3:5-3', '43:3-2', '67:1', '0:1', 'Jean 3', '', '43:3:1-4:99', '43:0'):
            with self.assertRaises(ValueError, msg=bad):
                Q.parse_passage(bad)
        with self.assertRaisesRegex(ValueError, 'Chapitre inexistant : Jean 22'):
            Q.parse_passage('43:22')
        with self.assertRaisesRegex(ValueError, 'Verset inexistant : Jean 3:37'):
            Q.parse_passage('43:3:37')


class QuestionsTest(Checks, unittest.TestCase):

    def test_parse_and_labels(self):
        self.check_parse_and_labels()

    def test_bad_lang(self):
        with self.assertRaises(ValueError):
            Q.build_questions(['43:3'], 'de', 5, random.Random(0))

    def test_deterministic(self):
        for lang in ('fr', 'en'):
            a = Q.build_questions(['45:8', '43:1'], lang, 20, random.Random(42))
            b = Q.build_questions(['45:8', '43:1'], lang, 20, random.Random(42))
            self.assertEqual(a, b)
            c = Q.build_questions(['45:8', '43:1'], lang, 20, random.Random(43))
            self.assertNotEqual(a, c)

    def test_counts(self):
        for passages, count in ((['43:3'], 15), (['1:1-3'], 15), (['19:23'], 5), (['45:8', '43:1'], 20)):
            for lang in ('fr', 'en'):
                for seed in range(3):
                    qs = Q.build_questions(passages, lang, count, random.Random(seed))
                    self.assertEqual(len(qs), count, (passages, lang, seed))
                    self.assert_valid(qs, passages, lang)

    def test_generated_only(self):
        """Sans les questions relues : le générateur seul tient la quantité."""
        ex = all_curated_ids()
        for passages, count in ((['43:3'], 12), (['1:1-3'], 15), (['45:8', '43:1'], 15)):
            for lang in ('fr', 'en'):
                qs = Q.build_questions(passages, lang, count, random.Random(7), ex)
                self.assertEqual(len(qs), count, (passages, lang))
                self.assertTrue(all(q['source'] == 'generated' for q in qs))
                self.assert_valid(qs, passages, lang)

    def test_two_books_which_book(self):
        ex = all_curated_ids()
        for lang in ('fr', 'en'):
            qs = Q.build_questions(['45:8', '43:1'], lang, 15, random.Random(1), ex)
            book_qs = [q for q in qs if q['id'].startswith(GEN + 'book-')]
            self.assertTrue(book_qs)
            names = {book_name(43, lang), book_name(45, lang)}
            for q in book_qs:
                self.assertTrue(names <= set(q['options']), q['options'])
            # Un seul livre : pas de question « Dans quel livre ».
            qs = Q.build_questions(['43:3'], lang, 15, random.Random(1), ex)
            self.assertFalse([q for q in qs if q['id'].startswith(GEN + 'book-')])

    def test_order_questions(self):
        qs = Q.build_questions(['1:1-50'], 'fr', 15, random.Random(2), all_curated_ids())
        self.assertTrue([q for q in qs if q['id'].startswith(GEN + 'order-')])
        self.assert_valid(qs, ['1:1-50'], 'fr')

    def test_tiny_passage(self):
        for lang in ('fr', 'en'):
            for passage in ('43:3:16-18', '19:23:1-3', '43:11:35'):
                qs = Q.build_questions([passage], lang, 10, random.Random(0))
                self.assertLessEqual(len(qs), 10)
                self.assertEqual(len(qs), Q.available_count([passage], lang))
                self.assert_valid(qs, [passage], lang)
        self.assertGreaterEqual(len(Q.build_questions(['43:3:16-18'], 'en', 10, random.Random(0))), 1)

    def test_verse_granularity(self):
        """Une partie de chapitre : aucune question hors des versets choisis."""
        ex = all_curated_ids()
        for lang in ('fr', 'en'):
            qs = Q.build_questions(['43:3:1-15'], lang, 10, random.Random(3), ex)
            self.assertTrue(qs)
            self.assert_valid(qs, ['43:3:1-15'], lang)

    def test_overlapping_passages(self):
        qs = Q.build_questions(['43:3', '43:3:1-10', '43:3:5'], 'fr', 30, random.Random(0))
        self.assert_valid(qs, ['43:3'], 'fr')

    def test_curated_in_scope(self):
        qs = Q.build_questions(['43:3'], 'fr', 15, random.Random(0))
        reviewed = [q for q in qs if q['source'] == 'reviewed']
        self.assertTrue(reviewed)
        self.assertEqual(qs[:len(reviewed)], reviewed, 'les relues viennent d\'abord')
        by_id = {q['id']: q for q in Q.curated_questions()}
        for q in reviewed:
            src = by_id[q['id']]
            self.assertEqual(q['prompt'], src['q']['fr'])
            self.assertEqual(q['options'][q['answer']], src['choices']['fr'][src['answer']])
            self.assertEqual(sorted(q['options']), sorted(src['choices']['fr']))
        # Hors portée : aucune relue sur Jean 3 dans un match sur Genèse 1.
        qs = Q.build_questions(['1:1'], 'en', 15, random.Random(0))
        self.assertTrue(all(q['ref'].startswith('1:1:') for q in qs))

    def test_exclude_ids(self):
        first = Q.build_questions(['43:3'], 'fr', 12, random.Random(0))
        ex = frozenset(q['id'] for q in first)
        second = Q.build_questions(['43:3'], 'fr', 12, random.Random(0), ex)
        self.assertTrue(second)
        self.assertFalse(ex & {q['id'] for q in second})
        # Un verset déjà donné à compléter n'est pas repris.
        seen = {q['ref'] for q in first if q['source'] == 'generated'}
        self.assertFalse(seen & {q['ref'] for q in second if q['source'] == 'generated'})

    def test_available_count(self):
        n = Q.available_count(['43:3'], 'fr')
        self.assertTrue(15 <= n <= 40, n)
        self.assertEqual(Q.available_count(['1:1-3'], 'fr', cap=40), 40)
        self.assertLessEqual(Q.available_count(['19:23'], 'fr'), 10)

    def test_performance(self):
        ex = all_curated_ids()
        t = time.perf_counter()
        for lang in ('fr', 'en'):
            qs = Q.build_questions(['1:1-50'], lang, 15, random.Random(5), ex)
            self.assertEqual(len(qs), 15)
        self.assertLess(time.perf_counter() - t, 3.0)


if __name__ == '__main__':
    unittest.main()
