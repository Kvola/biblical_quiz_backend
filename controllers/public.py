# -*- coding: utf-8 -*-
"""Lien d'invitation partagé (WhatsApp, QR code) : /quiz-biblique/rejoindre/<code>.

Le QR code se lit avec l'appareil photo du téléphone ; le lien ouvre cette
page, qui propose d'ouvrir l'application (Android), de jouer dans le
navigateur, ou d'installer l'application.
"""
import re
from html import escape
from urllib.parse import quote

from odoo.http import Controller, request, route

SLUG = 'quiz-biblique'
PACKAGE = 'ci.ivorycocoa.biblical_quiz'


class QuizPublic(Controller):

    @route(['/quiz-biblique/rejoindre/<string:code>', '/quiz-biblique/join/<string:code>'],
           type='http', auth='public', methods=['GET'], sitemap=False)
    def join_page(self, code, **kw):
        code = re.sub(r'[^A-Z0-9]', '', (code or '').upper())[:6]
        en = '/join/' in request.httprequest.path  # « rejoindre » contient aussi « join »
        base = request.httprequest.host_url.rstrip('/')
        install = '%s/apps/%s' % (base, SLUG)
        web = '%s/apps/%s/web/?join=%s' % (base, SLUG, code)
        intent = 'intent://join/%s#Intent;scheme=quizbiblique;package=%s;S.browser_fallback_url=%s;end' % (
            code, PACKAGE, quote(install, safe=''))
        t = {
            'title': "Join a Bible Quiz game" if en else "Rejoindre une partie de Quiz Biblique",
            'lead': "Game code:" if en else "Code de la partie :",
            'app': "Open in the app" if en else "Ouvrir dans l'application",
            'web': "Play in the browser" if en else "Jouer dans le navigateur",
            'install': "Install the app (Android)" if en else "Installer l'application (Android)",
            'hint': ("No app? Play in the browser, or install it, then type the code under Play → Join."
                     if en else "Pas encore l'application ? Jouez dans le navigateur, ou installez-la puis "
                                "saisissez le code dans Jouer → Rejoindre."),
        }
        html = """<!doctype html><html lang="%(lang)s"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>%(title)s</title>
<style>
body{font-family:system-ui,sans-serif;background:#F7F4EC;color:#1E2A38;margin:0;padding:24px 16px}
main{max-width:460px;margin:auto;text-align:center}
.code{font-size:44px;letter-spacing:6px;font-weight:700;color:#1E3A5F;margin:8px 0 24px}
a.b{display:block;padding:14px;border-radius:12px;margin:10px 0;text-decoration:none;font-weight:600}
.p{background:#1E3A5F;color:#fff}.s{background:#fff;color:#1E3A5F;border:2px solid #1E3A5F}
p.h{color:#5b6573;font-size:14px;margin-top:20px}
@media(prefers-color-scheme:dark){body{background:#121820;color:#E8E3D6}.code{color:#9EC1EA}
.s{background:#121820;color:#9EC1EA;border-color:#9EC1EA}.p{background:#9EC1EA;color:#121820}p.h{color:#a9b2bf}}
</style></head><body><main>
<h1>%(title)s</h1><div>%(lead)s</div><div class="code">%(code)s</div>
<a class="b p" href="%(intent)s">%(app)s</a>
<a class="b s" href="%(web)s">%(web_label)s</a>
<a class="b s" href="%(install)s">%(install_label)s</a>
<p class="h">%(hint)s</p></main></body></html>""" % {
            'lang': 'en' if en else 'fr', 'title': escape(t['title']), 'lead': escape(t['lead']),
            'code': escape(code), 'intent': escape(intent), 'app': escape(t['app']), 'web': escape(web),
            'web_label': escape(t['web']), 'install': escape(install), 'install_label': escape(t['install']),
            'hint': escape(t['hint']),
        }
        return request.make_response(html, headers=[('Content-Type', 'text/html; charset=utf-8'),
                                                    ('Cache-Control', 'no-store')])
