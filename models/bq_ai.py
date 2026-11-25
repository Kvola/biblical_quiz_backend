# -*- coding: utf-8 -*-
"""Banque de questions de compréhension rédigées par IA, un chapitre à la fois.

Principe : un chapitre n'est rédigé qu'UNE fois (français + anglais), contrôlé
(voir ``game/ai_bank.py``), puis servi à tous les lecteurs, qui le gardent hors
ligne. Le quota gratuit d'un appareil ne sert qu'à DEMANDER un chapitre que la
banque n'a pas encore ; lire la banque est libre.

La rédaction ne se fait jamais dans la requête du téléphone (deux appels à
Claude peuvent dépasser le temps accordé à une requête web) : la demande met le
chapitre en file, une tâche planifiée le rédige, l'application revient lire.

États d'un chapitre :
``queued`` (en file) → ``pending`` (rédaction en cours) → ``ready`` (servi) ;
``empty`` : rédigé mais rien n'a passé les contrôles (pas de nouvel essai
automatique) ; ``failed`` : le service n'a pas répondu après plusieurs essais
(une nouvelle demande le remet en file).
"""
import hashlib
import json
import logging
import os
import re
import time

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.modules import module as odoo_module

from ..game import ai_bank
from ..game.books import book_name
from .bq_config import PREFIX, bq_param

_logger = logging.getLogger(__name__)

DEFAULT_MODEL = 'claude-opus-5-5'
API_TIMEOUT = 180.0
API_RETRIES = 1
MAX_ATTEMPTS = 3            # essais de rédaction avant « failed »
STALE_MINUTES = 20          # « pending » plus vieux : processus interrompu
BATCH_PER_RUN = 3           # chapitres rédigés par passage de la tâche planifiée
MAX_BANK_CHAPTERS = 200     # chapitres lus par appel de /ai/bank

# Dollars par million de jetons (entrée, sortie) : estimation du coût, le modèle
# réellement utilisé (réponse de l'API) fait foi.
PRICES = {
    'claude-opus-5-5': (4.0, 20.0),
    'claude-opus-5': (5.0, 25.0),
    'claude-opus-4-8': (5.0, 25.0),
    'claude-sonnet-5-5': (2.0, 10.0),
    'claude-sonnet-5': (2.0, 10.0),
    'claude-haiku-5-5': (0.10, 0.50),
    'claude-fable-5-2': (8.0, 40.0),
}

REPORT_REASONS = [('wrong', "Réponse fausse"), ('several', "Plusieurs réponses justes"),
                  ('unclear', "Question peu claire"), ('doctrine', "Parti pris doctrinal"),
                  ('other', "Autre")]


def price_of(model):
    model = model or ''
    best = max((k for k in PRICES if model.startswith(k)), key=len, default=None)
    return PRICES.get(best)


def _testing():
    """Vrai pendant les tests d'Odoo (leur transaction ne doit jamais être validée)."""
    return bool(odoo_module.current_test)


class AiRefusal(UserError):
    """Refus avec un code machine : l'application affiche son propre message traduit."""

    def __init__(self, message, reason):
        super().__init__(message)
        self.reason = reason


class AiCallError(Exception):
    """L'appel à Claude n'a rien donné. ``billed`` : des jetons ont été consommés ;
    ``retry`` : un nouvel essai plus tard a un sens (panne, surcharge)."""

    def __init__(self, message, retry, usage=None):
        super().__init__(message)
        self.retry = retry
        self.usage = usage or (0, 0, '')


class BqAiDevice(models.Model):
    """Un appareil anonyme : l'identifiant d'installation de l'application, haché ici."""
    _name = 'bq.ai.device'
    _description = "Appareil (questions IA)"
    _order = 'last_seen desc, id desc'

    key_hash = fields.Char("Empreinte", required=True, index=True, readonly=True)
    blocked = fields.Boolean("Bloqué", help="Un appareil bloqué ne peut plus demander de chapitre ni signaler.")
    last_seen = fields.Datetime("Vu le", readonly=True)
    request_ids = fields.One2many('bq.ai.request', 'device_id', "Demandes")
    report_ids = fields.One2many('bq.ai.report', 'device_id', "Signalements")

    _key_unique = models.Constraint('UNIQUE(key_hash)', "Appareil déjà connu.")

    @api.depends('key_hash')
    def _compute_display_name(self):
        for d in self:
            d.display_name = _("Appareil n° %(id)s · %(hash)s", id=d.id, hash=(d.key_hash or '')[:8]) if d.id \
                else _("Appareil")

    @api.model
    def _for_key(self, device_key):
        key = str(device_key or '').strip()
        if not re.match(r'^[A-Za-z0-9_-]{16,128}$', key):
            raise AiRefusal(_("Identifiant d'appareil invalide."), 'bad_device')
        digest = hashlib.sha256(key.encode()).hexdigest()
        device = self.search([('key_hash', '=', digest)], limit=1)
        if not device:
            try:
                with self.env.cr.savepoint():
                    device = self.create({'key_hash': digest})
            except Exception:  # noqa: BLE001 — deux premières demandes simultanées du même appareil
                device = self.search([('key_hash', '=', digest)], limit=1)
        return device


class BqAiChapter(models.Model):
    _name = 'bq.ai.chapter'
    _description = "Chapitre de la banque de questions IA"
    _order = 'book, chapter'

    book = fields.Integer("Livre (1-66)", required=True, index=True)
    chapter = fields.Integer("Chapitre", required=True)
    code = fields.Char("Code", compute='_compute_code', store=True, index=True)
    state = fields.Selection([('queued', "En file"), ('pending', "Rédaction en cours"), ('ready', "Prête"),
                              ('empty', "Aucune question retenue"), ('failed', "Échec du service")],
                             "État", required=True, default='queued', index=True)
    rev = fields.Integer("Révision", default=0, readonly=True,
                         help="Augmente à chaque changement des questions servies : l'application recharge le chapitre.")
    attempts = fields.Integer("Essais", default=0, readonly=True)
    pending_since = fields.Datetime("En cours depuis", readonly=True)
    queued_at = fields.Datetime("Mis en file le", readonly=True)
    generated_at = fields.Datetime("Rédigé le", readonly=True)
    last_error = fields.Char("Dernière erreur", readonly=True)
    question_ids = fields.One2many('bq.ai.question', 'chapter_id', "Questions")
    generation_ids = fields.One2many('bq.ai.generation', 'chapter_id', "Rédactions")
    served_count = fields.Integer("Questions servies", compute='_compute_counts', store=True)
    unreviewed_count = fields.Integer("À relire", compute='_compute_counts', store=True)

    _chapter_unique = models.Constraint('UNIQUE(book, chapter)', "Ce chapitre est déjà dans la banque.")

    @api.depends('book', 'chapter')
    def _compute_code(self):
        for c in self:
            c.code = '%s:%s' % (c.book, c.chapter)

    @api.depends('question_ids.state')
    def _compute_counts(self):
        for c in self:
            c.served_count = len(c.question_ids.filtered(lambda q: q.state in ('auto', 'validated')))
            c.unreviewed_count = len(c.question_ids.filtered(lambda q: q.state == 'auto'))

    @api.depends('book', 'chapter')
    def _compute_display_name(self):
        for c in self:
            c.display_name = '%s %s' % (book_name(c.book, 'fr'), c.chapter) if 1 <= (c.book or 0) <= 66 else _("Chapitre")

    @api.constrains('book', 'chapter')
    def _check_exists(self):
        for c in self:
            try:
                ai_bank.parse_chapter('%s:%s' % (c.book, c.chapter))
            except ValueError as e:
                raise UserError(_("Ce chapitre n'existe pas dans la Bible.")) from e

    # ------------------------------------------------------------------ réglages
    @api.model
    def _param(self, key, default=''):
        return self.env['ir.config_parameter'].sudo().get_param(PREFIX + key, default)

    @api.model
    def _suspended(self):
        return self._param('ai_suspended') in ('True', 'true', '1')

    @api.model
    def _api_key(self):
        return (self._param('anthropic_api_key') or os.environ.get('ANTHROPIC_API_KEY') or '').strip()

    @api.model
    def _enabled(self):
        return bool(self._api_key()) and not self._suspended()

    # ------------------------------------------------------------------ quota
    @api.model
    def _today_count(self, domain):
        return self.env['bq.ai.request'].search_count(domain + [
            ('day', '=', fields.Date.context_today(self)), ('state', 'in', ('pending', 'done', 'rejected'))])

    @api.model
    def _quota_info(self, device):
        daily = bq_param(self.env, 'ai_daily_quota')
        used = self._today_count([('device_id', '=', device.id)]) if device else 0
        left_global = max(0, bq_param(self.env, 'ai_daily_global_cap') - self._today_count([]))
        enabled = self._enabled()
        blocked = bool(device and device.blocked)
        return {
            'enabled': enabled,
            'daily': daily,
            'remaining': min(max(0, daily - used), left_global) if enabled and not blocked else 0,
            'limited': 'blocked' if blocked else ('server_quota' if enabled and not left_global else ''),
        }

    # ------------------------------------------------------------------ lecture
    def _payload(self, known_rev=None):
        """Ce que l'application garde hors ligne pour ce chapitre."""
        self.ensure_one()
        state = self.state
        out = {'code': self.code, 'state': state, 'rev': self.rev}
        if state == 'ready' and known_rev != self.rev:
            out['questions'] = [q._payload() for q in self.question_ids.sorted('id')
                                if q.state in ('auto', 'validated')]
        return out

    @api.model
    def _bank(self, codes, known=None):
        """État (et questions, si la révision connue du téléphone est dépassée) de chaque chapitre demandé."""
        known = known if isinstance(known, dict) else {}
        wanted = []
        for code in list(codes or [])[:MAX_BANK_CHAPTERS]:
            try:
                wanted.append(ai_bank.parse_chapter(code))
            except ValueError:
                continue
        by_code = {c.code: c for c in self.search([('code', 'in', ['%s:%s' % bc for bc in wanted])])}
        out = []
        for book, chapter in dict.fromkeys(wanted):
            code = '%s:%s' % (book, chapter)
            record = by_code.get(code)
            if not record:
                out.append({'code': code, 'state': 'none', 'rev': 0})
                continue
            try:
                rev = int(known.get(code))
            except (TypeError, ValueError):
                rev = None
            out.append(record._payload(rev))
        return out

    # ------------------------------------------------------------------ demande
    @api.model
    def _request(self, device, ip_hash, code):
        """Un lecteur demande un chapitre. Renvoie le chapitre (prêt, ou mis en file).

        Rien n'est consommé si le chapitre existe déjà ou est déjà en file.
        Lève ``AiRefusal`` (quota, service suspendu…) sinon.
        """
        try:
            book, chapter = ai_bank.parse_chapter(code)
        except ValueError as e:
            raise AiRefusal(_("Chapitre inconnu."), 'bad_chapter') from e
        Request = self.env['bq.ai.request']
        record = self.search([('book', '=', book), ('chapter', '=', chapter)], limit=1)
        if record.state in ('ready', 'queued', 'pending'):
            return record
        if record.state == 'empty':
            raise AiRefusal(_("Ce chapitre ne se prête pas à des questions rédigées par IA."), 'no_questions')
        if not self._api_key():
            raise AiRefusal(_("Les questions rédigées par IA ne sont pas disponibles sur ce serveur."), 'not_configured')
        if self._suspended():
            raise AiRefusal(_("Les questions rédigées par IA sont suspendues pour le moment."), 'disabled')
        # Verrou sur l'appareil : deux demandes simultanées ne dépassent pas le quota.
        self.env.cr.execute('SELECT id FROM bq_ai_device WHERE id = %s FOR UPDATE', [device.id])
        if device.blocked:
            raise AiRefusal(_("Cet appareil ne peut plus demander de questions."), 'blocked')
        refusal = None
        if self._today_count([('device_id', '=', device.id)]) >= bq_param(self.env, 'ai_daily_quota'):
            refusal = AiRefusal(_("Vous avez atteint le nombre de chapitres du jour. Revenez demain."), 'quota')
        elif ip_hash and self._today_count([('ip_hash', '=', ip_hash)]) >= bq_param(self.env, 'ai_ip_daily_cap'):
            refusal = AiRefusal(_("Trop de demandes depuis ce réseau aujourd'hui."), 'network_quota')
        elif self._today_count([]) >= bq_param(self.env, 'ai_daily_global_cap'):
            refusal = AiRefusal(_("Le service a atteint sa limite du jour. Revenez demain."), 'server_quota')
        if refusal:
            # Trace sans appel ni coût : mesure le besoin au-delà du gratuit.
            Request.create({'device_id': device.id, 'ip_hash': ip_hash, 'state': 'refused',
                            'book': book, 'chapter': chapter, 'error': refusal.reason})
            if not _testing():
                self.env.cr.commit()
            raise refusal
        record = self._enqueue(book, chapter)
        if record.state == 'queued':
            Request.create({'device_id': device.id, 'ip_hash': ip_hash, 'state': 'pending',
                            'chapter_id': record.id, 'book': book, 'chapter': chapter})
        device.last_seen = fields.Datetime.now()
        if not _testing():
            self.env.cr.commit()
            self.env.ref('biblical_quiz_backend.cron_ai_generate')._trigger()
        return record

    @api.model
    def _enqueue(self, book, chapter):
        """Met le chapitre en file (le crée au besoin) ; sans effet s'il est prêt ou déjà en route."""
        record = self.search([('book', '=', book), ('chapter', '=', chapter)], limit=1)
        if not record:
            try:
                with self.env.cr.savepoint():
                    return self.create({'book': book, 'chapter': chapter, 'state': 'queued',
                                        'queued_at': fields.Datetime.now()})
            except Exception:  # noqa: BLE001 — créé à l'instant par une autre demande
                record = self.search([('book', '=', book), ('chapter', '=', chapter)], limit=1)
        self.env.cr.execute('SELECT id FROM bq_ai_chapter WHERE id = %s FOR UPDATE', [record.id])
        record.invalidate_recordset(['state'])
        if record.state in ('failed', 'empty'):
            record.write({'state': 'queued', 'attempts': 0, 'queued_at': fields.Datetime.now(), 'last_error': False})
        return record

    def request_state(self):
        """États des demandes de lecteurs attachées à ce chapitre (suivi, tests)."""
        return self.env['bq.ai.request'].search([('chapter_id', 'in', self.ids)], order='id').mapped('state')

    def action_validate_all(self):
        """Valide toutes les questions non relues de ces chapitres (sauf signalées : voir ``_validate_bulk``)."""
        Question = self.env['bq.ai.question']
        done, skipped = self.question_ids.filtered(lambda q: q.state == 'auto')._validate_bulk()
        return Question._bulk_notice(done, skipped)

    def action_queue(self):
        """Back-office : (re)mettre en file. Les questions déjà relues sont conservées."""
        for c in self:
            if c.state != 'pending':
                c.write({'state': 'queued', 'attempts': 0, 'queued_at': fields.Datetime.now(), 'last_error': False})
        if not _testing():
            self.env.ref('biblical_quiz_backend.cron_ai_generate')._trigger()

    # ------------------------------------------------------------------ rédaction
    @api.model
    def _client(self):
        import anthropic  # noqa: PLC0415 — dépendance déclarée au manifeste
        return anthropic.Anthropic(api_key=self._api_key(), timeout=API_TIMEOUT, max_retries=API_RETRIES)

    @api.model
    def _call_claude(self, system, message, schema, effort):
        """Un appel à Claude ; renvoie (données JSON, jetons entrée, jetons sortie, modèle).

        Isolé pour être remplacé dans les tests (aucun appel réel, aucun coût).
        """
        import anthropic  # noqa: PLC0415
        model = self._param('ai_model') or DEFAULT_MODEL
        try:
            response = self._client().beta.messages.create(
                model=model,
                max_tokens=16000,
                system=system,
                betas=['server-side-fallback-2026-07-01'],
                fallbacks='default',
                output_config={'effort': effort, 'format': {'type': 'json_schema', 'schema': schema}},
                messages=[{'role': 'user', 'content': message}],
            )
        except (anthropic.RateLimitError, anthropic.APITimeoutError, anthropic.APIConnectionError,
                anthropic.InternalServerError) as e:
            raise AiCallError("service indisponible (%s)" % type(e).__name__, retry=True) from e
        except anthropic.APIStatusError as e:
            _logger.warning("Quiz Biblique : erreur de l'API Claude %s", e.status_code)
            # 401/403/400 : clé ou réglage à corriger ; réessayer tout de suite ne sert à rien.
            raise AiCallError("erreur de l'API (%s)" % e.status_code, retry=e.status_code >= 500) from e
        usage = (response.usage.input_tokens or 0, response.usage.output_tokens or 0, response.model)
        if response.stop_reason in ('refusal', 'max_tokens'):
            raise AiCallError("réponse interrompue (%s)" % response.stop_reason, retry=False, usage=usage)
        payload = next((b.text for b in response.content if b.type == 'text'), '')
        try:
            data = json.loads(payload)
        except ValueError as e:
            raise AiCallError("réponse illisible", retry=True, usage=usage) from e
        return (data,) + usage

    def _write_and_keep(self, values):
        self.write(values)
        if not _testing():
            self.env.cr.commit()

    def _generate_now(self):
        """Rédige ce chapitre : appel, contrôles, relecture indépendante, enregistrement.

        Ne lève jamais : tout échec est journalisé et laisse le chapitre dans un état net.
        """
        self.ensure_one()
        Generation = self.env['bq.ai.generation']
        self._write_and_keep({'state': 'pending', 'pending_since': fields.Datetime.now(),
                              'attempts': self.attempts + 1})
        started = time.monotonic()
        tokens_in = tokens_out = 0
        model = ''
        kept, dropped = [], []
        error, retry = None, False
        try:
            data, t_in, t_out, model = self._call_claude(
                ai_bank.GENERATION_PROMPT, ai_bank.generation_message(self.book, self.chapter),
                ai_bank.GENERATION_SCHEMA, 'high')
            tokens_in, tokens_out = t_in, t_out
            kept, dropped = ai_bank.validate(self.book, self.chapter, data)
            if kept:
                verdicts, t_in, t_out, _m = self._call_claude(
                    ai_bank.VERIFY_PROMPT, ai_bank.verify_message(self.book, self.chapter, kept),
                    ai_bank.VERIFY_SCHEMA, 'medium')
                tokens_in, tokens_out = tokens_in + t_in, tokens_out + t_out
                kept, refused = ai_bank.apply_verdicts(kept, verdicts)
                dropped += refused
        except AiCallError as e:
            error, retry = str(e), e.retry
            tokens_in, tokens_out = tokens_in + e.usage[0], tokens_out + e.usage[1]
            model = model or e.usage[2]
            kept = []
        except Exception:  # noqa: BLE001 — la file ne doit jamais rester bloquée
            _logger.exception("Quiz Biblique : rédaction IA de %s interrompue", self.code)
            error, retry, kept = "erreur interne", True, []
        billed = bool(tokens_in or tokens_out)
        if kept:
            state, request_state = 'ready', 'done'
        elif error and retry and self.attempts < MAX_ATTEMPTS:
            state, request_state = 'queued', None       # nouvel essai au prochain passage
        elif error:
            state, request_state = 'failed', ('rejected' if billed else 'failed')
        else:
            state, request_state = 'empty', 'rejected'
        Generation.create({
            'chapter_id': self.id, 'state': 'done' if kept else ('failed' if error else 'empty'),
            'model': model, 'input_tokens': tokens_in, 'output_tokens': tokens_out,
            'kept': len(kept), 'dropped': len(dropped),
            'dropped_detail': '\n'.join('• %s — %s' % (_question_title(q), why) for q, why in dropped)[:8000],
            'duration_ms': int((time.monotonic() - started) * 1000), 'error': error,
        })
        values = {'state': state, 'pending_since': False, 'last_error': error}
        if kept:
            # Une nouvelle rédaction remplace les questions jamais relues ; les relues restent.
            self.question_ids.filtered(lambda q: q.state == 'auto').with_context(bq_ai_no_rev=True).unlink()
            self.env['bq.ai.question'].with_context(bq_ai_no_rev=True).create([{
                'chapter_id': self.id, 'verse': q['verse'], 'skill': q['skill'], 'answer': q['answer'],
                'q_fr': q['q']['fr'], 'q_en': q['q']['en'],
                'choices_fr': '\n'.join(q['choices']['fr']), 'choices_en': '\n'.join(q['choices']['en']),
                'explain_fr': q['explain']['fr'], 'explain_en': q['explain']['en'],
                'quote_fr': q['quote']['fr'], 'quote_en': q['quote']['en'],
            } for q in kept])
            values.update(rev=self.rev + 1, generated_at=fields.Datetime.now())
        if request_state:
            self.env['bq.ai.request'].search([('chapter_id', '=', self.id), ('state', '=', 'pending')]).write(
                {'state': request_state, 'error': error})
        self._write_and_keep(values)
        return state

    @api.model
    def _cron_generate(self):
        """Tâche planifiée : rédige les chapitres en file, les plus anciens d'abord."""
        limit = fields.Datetime.subtract(fields.Datetime.now(), minutes=STALE_MINUTES)
        for stale in self.search([('state', '=', 'pending'), ('pending_since', '<', limit)]):
            # Processus interrompu en pleine rédaction : on reprend, ou on renonce.
            if stale.attempts < MAX_ATTEMPTS:
                stale.state = 'queued'
            else:
                stale.write({'state': 'failed', 'last_error': "interrompue"})
                self.env['bq.ai.request'].search([('chapter_id', '=', stale.id), ('state', '=', 'pending')]).write(
                    {'state': 'failed', 'error': "interrompue"})
        if not self._enabled():
            return 0
        queued = self.search([('state', '=', 'queued')], order='queued_at, id', limit=BATCH_PER_RUN + 1)
        for record in queued[:BATCH_PER_RUN]:
            record._generate_now()
        if len(queued) > BATCH_PER_RUN and not _testing():
            self.env.ref('biblical_quiz_backend.cron_ai_generate')._trigger()
        return len(queued[:BATCH_PER_RUN])

    @api.model
    def _match_bank(self, passages):
        """Questions RELUES de la banque dont le verset est dans un des passages (matchs en ligne)."""
        chapters = {(p.book, c) for p in passages for c in range(p.start_chapter, p.end_chapter + 1)}
        if not chapters:
            return []
        records = self.env['bq.ai.question'].search([
            ('state', '=', 'validated'), ('chapter_id.code', 'in', ['%s:%s' % bc for bc in chapters])])
        return [q._payload(for_match=True) for q in records]


def _question_title(q):
    if not isinstance(q, dict):
        return '?'
    nested = q.get('q') if isinstance(q.get('q'), dict) else {}
    return str(q.get('q_fr') or nested.get('fr') or '?')[:160]


class BqAiQuestion(models.Model):
    _name = 'bq.ai.question'
    _description = "Question rédigée par IA"
    _order = 'chapter_id, verse, id'
    _rec_name = 'q_fr'

    chapter_id = fields.Many2one('bq.ai.chapter', "Chapitre", required=True, index=True, ondelete='cascade')
    book = fields.Integer(related='chapter_id.book', store=True)
    verse = fields.Integer("Verset", required=True)
    ref_label = fields.Char("Référence", compute='_compute_ref')
    verse_fr = fields.Text("Verset (Segond 1910)", compute='_compute_ref')
    verse_en = fields.Text("Verset (King James)", compute='_compute_ref')
    state = fields.Selection([('auto', "Non relue"), ('validated', "Relue"), ('withdrawn', "Retirée")],
                             "État", required=True, default='auto', index=True,
                             help="Non relue : servie avec la mention « rédigé par IA, non relu ». "
                                  "Relue : servie sans réserve, et utilisable dans les matchs. Retirée : plus servie.")
    skill = fields.Selection([('main_idea', "Idée principale"), ('meaning', "Sens d'une parole"),
                              ('cause_effect', "Cause et conséquence"), ('who_what', "Faits du récit"),
                              ('link', "Lien entre deux passages")], "Ce qu'elle vérifie", default='meaning')
    q_fr = fields.Char("Question (FR)", required=True)
    q_en = fields.Char("Question (EN)", required=True)
    choices_fr = fields.Text("Propositions (FR)", required=True, help="Quatre lignes, dans le même ordre qu'en anglais.")
    choices_en = fields.Text("Propositions (EN)", required=True)
    answer = fields.Integer("Bonne réponse (ligne, à partir de 0)", required=True)
    answer_fr = fields.Char("Bonne réponse (FR)", compute='_compute_answer_text')
    answer_en = fields.Char("Bonne réponse (EN)", compute='_compute_answer_text')
    explain_fr = fields.Text("Explication (FR)", required=True)
    explain_en = fields.Text("Explication (EN)", required=True)
    quote_fr = fields.Char("Appui dans le texte (FR)")
    quote_en = fields.Char("Appui dans le texte (EN)")
    report_ids = fields.One2many('bq.ai.report', 'question_id', "Signalements")
    report_count = fields.Integer("Nombre de signalements", compute='_compute_report_count', store=True)
    withdrawn_reason = fields.Char("Motif du retrait", readonly=True)

    @api.depends('report_ids')
    def _compute_report_count(self):
        for q in self:
            q.report_count = len(q.report_ids)

    @api.depends('chapter_id', 'verse')
    def _compute_ref(self):
        from ..game.questions import get_bible  # noqa: PLC0415
        for q in self:
            c = q.chapter_id
            ok = c and get_bible('fr').has(c.book, c.chapter, q.verse or 0)
            q.ref_label = '%s %s:%s' % (book_name(c.book, 'fr'), c.chapter, q.verse) if ok else False
            q.verse_fr = get_bible('fr').text(c.book, c.chapter, q.verse) if ok else False
            q.verse_en = get_bible('en').text(c.book, c.chapter, q.verse) if ok else False

    def _choices(self, lang):
        return [line.strip() for line in (self['choices_' + lang] or '').split('\n') if line.strip()]

    @api.depends('choices_fr', 'choices_en', 'answer')
    def _compute_answer_text(self):
        for q in self:
            for lang in ai_bank.LANGS:
                choices = q._choices(lang)
                q['answer_' + lang] = choices[q.answer] if 0 <= q.answer < len(choices) else False

    @api.constrains('choices_fr', 'choices_en', 'answer', 'verse', 'chapter_id')
    def _check_shape(self):
        from ..game.questions import get_bible  # noqa: PLC0415
        for q in self:
            fr, en = q._choices('fr'), q._choices('en')
            if len(fr) != 4 or len(en) != 4:
                raise UserError(_("Il faut exactement quatre propositions, une par ligne, en français et en anglais."))
            if len(set(fr)) != 4 or len(set(en)) != 4:
                raise UserError(_("Les quatre propositions doivent être différentes."))
            if not 0 <= q.answer < 4:
                raise UserError(_("La bonne réponse est le numéro de sa ligne : 0, 1, 2 ou 3."))
            if not get_bible('fr').has(q.chapter_id.book, q.chapter_id.chapter, q.verse):
                raise UserError(_("Ce verset n'existe pas dans le chapitre."))

    def _payload(self, for_match=False):
        """La question au format du contenu de l'application (``Question.parse``)."""
        self.ensure_one()
        c = self.chapter_id
        ref = ai_bank.ref_code(c.book, c.chapter, self.verse)
        return {
            'id': '%s%s' % (ai_bank.ID_PREFIX, self.id), 'kind': 'choice',
            'q': {'fr': self.q_fr, 'en': self.q_en},
            'choices': {'fr': self._choices('fr'), 'en': self._choices('en')},
            'answer': self.answer,
            'explain': {'fr': self.explain_fr, 'en': self.explain_en},
            'ref': ref, 'reviewed': self.state == 'validated',
            **({'_start': (c.book, c.chapter, self.verse)} if for_match else {}),
        }

    # Tout changement de ce qui est servi fait avancer la révision du chapitre.
    _SERVED_FIELDS = {'state', 'verse', 'q_fr', 'q_en', 'choices_fr', 'choices_en', 'answer', 'explain_fr',
                      'explain_en'}

    def _bump(self):
        if not self.env.context.get('bq_ai_no_rev'):
            for chapter in self.chapter_id:
                chapter.rev += 1

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._bump()
        return records

    def write(self, vals):
        result = super().write(vals)
        if self._SERVED_FIELDS & set(vals):
            self._bump()
        return result

    def unlink(self):
        chapters = self.chapter_id
        result = super().unlink()
        if not self.env.context.get('bq_ai_no_rev'):
            for chapter in chapters:
                chapter.rev += 1
        return result

    def action_validate(self):
        self.write({'state': 'validated', 'withdrawn_reason': False})

    def _validate_bulk(self):
        """Valide en masse. Renvoie (validées, laissées de côté).

        Deux gardes, parce qu'on valide ici sans ouvrir chaque question :
        une question SIGNALÉE par un lecteur et une question RETIRÉE ne passent
        jamais par la validation en masse — il faut les ouvrir et décider.
        """
        todo = self.filtered(lambda q: q.state == 'auto' and not q.report_count)
        skipped = self.filtered(lambda q: q.state != 'validated') - todo
        if todo:
            todo.write({'state': 'validated', 'withdrawn_reason': False})
        return todo, skipped

    @api.model
    def _bulk_notice(self, done, skipped):
        if done and not skipped:
            message, kind = _("%s question(s) validée(s).", len(done)), 'success'
        elif done:
            message, kind = _("%(done)s question(s) validée(s). %(skipped)s laissée(s) de côté (signalées ou "
                              "retirées) : à ouvrir une par une.", done=len(done), skipped=len(skipped)), 'warning'
        elif skipped:
            message, kind = _("Aucune validation : les %s question(s) choisies sont signalées ou retirées, "
                              "à ouvrir une par une.", len(skipped)), 'warning'
        else:
            message, kind = _("Rien à valider : ces questions sont déjà relues."), 'info'
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'title': _("Validation en masse"), 'message': message, 'type': kind, 'sticky': False,
                           'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'}}}

    def action_validate_bulk(self):
        """Bouton « Valider la sélection » de la liste."""
        done, skipped = self._validate_bulk()
        return self._bulk_notice(done, skipped)

    def action_withdraw(self):
        self.write({'state': 'withdrawn', 'withdrawn_reason': _("Retirée à la relecture")})

    def action_restore(self):
        self.write({'state': 'auto', 'withdrawn_reason': False})

    @api.model
    def _from_public_id(self, public_id):
        m = re.match(r'^%s(\d{1,12})$' % re.escape(ai_bank.ID_PREFIX), str(public_id or ''))
        return self.browse(int(m[1])).exists() if m else self.browse()

    @api.model
    def _report(self, device, public_id, reason, note):
        """Signalement d'un lecteur. Trop de signalements retirent une question jamais relue."""
        question = self._from_public_id(public_id)
        if not question:
            raise AiRefusal(_("Question inconnue."), 'bad_question')
        if device.blocked:
            raise AiRefusal(_("Cet appareil ne peut plus signaler."), 'blocked')
        reason = reason if reason in dict(REPORT_REASONS) else 'other'
        Report = self.env['bq.ai.report']
        existing = Report.search([('question_id', '=', question.id), ('device_id', '=', device.id)], limit=1)
        values = {'reason': reason, 'note': (str(note or '').strip()[:500]) or False}
        if existing:
            existing.write(values)
        else:
            Report.create(dict(values, question_id=question.id, device_id=device.id))
        device.last_seen = fields.Datetime.now()
        if question.state == 'auto' and question.report_count >= bq_param(self.env, 'ai_report_threshold'):
            question.write({'state': 'withdrawn',
                            'withdrawn_reason': _("Retirée d'office : %s signalements", question.report_count)})
        return question


class BqAiReport(models.Model):
    _name = 'bq.ai.report'
    _description = "Signalement d'une question IA"
    _order = 'id desc'

    question_id = fields.Many2one('bq.ai.question', "Question", required=True, index=True, ondelete='cascade')
    chapter_id = fields.Many2one(related='question_id.chapter_id', store=True)
    question_state = fields.Selection(related='question_id.state', string="État de la question")
    device_id = fields.Many2one('bq.ai.device', "Appareil", required=True, index=True, ondelete='cascade')
    reason = fields.Selection(REPORT_REASONS, "Motif", required=True, default='other')
    note = fields.Char("Précision")

    _one_per_device = models.Constraint('UNIQUE(question_id, device_id)', "Déjà signalée par cet appareil.")


class BqAiRequest(models.Model):
    """Demandes de chapitres par les lecteurs : sert au quota. ``refused`` = quota atteint (aucun coût)."""
    _name = 'bq.ai.request'
    _description = "Demande de questions IA"
    _order = 'id desc'

    device_id = fields.Many2one('bq.ai.device', "Appareil", required=True, index=True, ondelete='cascade')
    ip_hash = fields.Char("IP (empreinte)", index=True)
    day = fields.Date("Jour", required=True, index=True, default=fields.Date.context_today)
    state = fields.Selection([('pending', "En cours"), ('done', "Servie"), ('rejected', "Sans question"),
                              ('failed', "Échec du service"), ('refused', "Quota atteint")],
                             "État", required=True, default='pending', index=True)
    chapter_id = fields.Many2one('bq.ai.chapter', "Chapitre", index=True, ondelete='set null')
    book = fields.Integer("Livre")
    chapter = fields.Integer("Chapitre n°")
    error = fields.Char("Motif")


class BqAiGeneration(models.Model):
    """Journal des rédactions : coût réel et questions écartées (avec le motif)."""
    _name = 'bq.ai.generation'
    _description = "Rédaction IA d'un chapitre"
    _order = 'id desc'

    chapter_id = fields.Many2one('bq.ai.chapter', "Chapitre", required=True, index=True, ondelete='cascade')
    state = fields.Selection([('done', "Réussie"), ('empty', "Rien de retenu"), ('failed', "Échec")],
                             "Résultat", required=True)
    model = fields.Char("Modèle")
    input_tokens = fields.Integer("Jetons en entrée")
    output_tokens = fields.Integer("Jetons en sortie")
    cost = fields.Float("Coût estimé ($)", digits=(12, 4), compute='_compute_cost', store=True, aggregator='sum')
    kept = fields.Integer("Questions gardées")
    dropped = fields.Integer("Questions écartées")
    dropped_detail = fields.Text("Écartées, et pourquoi")
    duration_ms = fields.Integer("Durée (ms)", aggregator='avg')
    error = fields.Char("Erreur")

    @api.depends('model', 'input_tokens', 'output_tokens')
    def _compute_cost(self):
        for g in self:
            price = price_of(g.model)
            g.cost = (g.input_tokens * price[0] + g.output_tokens * price[1]) / 1e6 if price else 0.0


class BqAiPrepare(models.TransientModel):
    """Back-office : faire rédiger d'avance les chapitres d'un livre (hors quota des lecteurs)."""
    _name = 'bq.ai.prepare'
    _description = "Préparer des chapitres (questions IA)"

    book = fields.Selection([(str(n), book_name(n, 'fr')) for n in range(1, 67)], "Livre", required=True)
    chapter_from = fields.Integer("Du chapitre", default=1, required=True)
    chapter_to = fields.Integer("Au chapitre", default=1, required=True)
    redo = fields.Boolean("Rédiger de nouveau les chapitres déjà prêts",
                          help="Les questions déjà relues sont conservées ; les autres sont remplacées.")

    def action_prepare(self):
        self.ensure_one()
        from ..game.questions import get_bible  # noqa: PLC0415
        Chapter = self.env['bq.ai.chapter']
        book = int(self.book)
        last = get_bible('fr').chapter_count(book)
        first, to = max(1, self.chapter_from), min(last, self.chapter_to)
        if to < first:
            raise UserError(_("Ce livre a %s chapitre(s) : vérifiez l'intervalle.", last))
        if not Chapter._api_key():
            raise UserError(_("Renseignez d'abord la clé Anthropic dans les réglages."))
        for chapter in range(first, to + 1):
            record = Chapter._enqueue(book, chapter)
            if self.redo and record.state == 'ready':
                record.write({'state': 'queued', 'attempts': 0, 'queued_at': fields.Datetime.now()})
        if not _testing():
            self.env.ref('biblical_quiz_backend.cron_ai_generate')._trigger()
        return {'type': 'ir.actions.act_window', 'name': _("Chapitres"), 'res_model': 'bq.ai.chapter',
                'view_mode': 'list,form', 'domain': [('book', '=', book)]}
