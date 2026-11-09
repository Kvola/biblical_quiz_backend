# -*- coding: utf-8 -*-
"""L'application Quiz Biblique, déclarée sur le socle de publication."""
from odoo.tests import HttpCase, tagged


@tagged('post_install', '-at_install')
class TestQuizBibliqueApp(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = cls.env.ref('biblical_quiz_backend.mobile_app_quiz_biblique')

    def test_lapplication_est_declaree(self):
        self.assertEqual(self.app.slug, 'quiz-biblique')
        self.assertEqual(self.app.name, 'Quiz Biblique')
        self.assertTrue(self.app.summary)
        self.assertTrue(self.app.icon, "sans icône, on ne reconnaît pas ce qu'on installe")
        self.assertTrue(self.app.listed)

    def test_le_premier_lancement_ne_parle_pas_de_compte_a_creer(self):
        """Laissée vide, la page dirait « créez votre compte » : faux pour une
        application entièrement hors ligne."""
        self.assertIn('aucun compte', self.app.first_run_note.lower())

    def test_la_note_de_contact_appartient_au_gestionnaire(self):
        """Le module ne livre pas `support_note` : c'est la valeur de départ du
        socle, que le gestionnaire retouche sans qu'une mise à jour l'écrase."""
        defaut = self.env['mobile.app']._fields['support_note'].default(self.env['mobile.app'])
        self.assertEqual(self.app.support_note, defaut)

    def test_la_page_publique_existe(self):
        page = self.url_open('/apps/quiz-biblique')
        self.assertEqual(page.status_code, 200)
        self.assertIn('Quiz Biblique', page.text)
        self.assertIn('/apps/quiz-biblique/guide', page.text)
