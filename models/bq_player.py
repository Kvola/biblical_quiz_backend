# -*- coding: utf-8 -*-
"""Joueurs du jeu en ligne, leurs sessions et les signalements de pseudos.

Devenir joueur est un choix : l'application reste entièrement utilisable
sans compte. Le joueur prouve son numéro par un code WhatsApp (otp_auth),
puis choisit un pseudo unique.

**Un seul engagement à la fois.** Un joueur est dans au plus UN match ou
UN tournoi (``match_id`` / ``tournament_id``). Ces deux champs ne sont
écrits que sous verrou de ligne du joueur (:meth:`_lock`), si bien que deux
inscriptions simultanées ne peuvent pas passer toutes les deux.
"""
import re
import secrets
import unicodedata

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .bq_config import PREFIX

PSEUDO_RE = re.compile(r"^[\w][\w .'\-]{1,18}[\w]$", re.UNICODE)

#: Mots refusés dans les pseudos (forme normalisée, sans accents). Les mots
#: courts ne sont refusés qu'entiers (« con » ne doit pas refuser « Constance »).
BANNED_DEFAULT = (
    'connard', 'connasse', 'salope', 'salaud', 'pute', 'putain', 'merde', 'encul', 'nique', 'niquer',
    'batard', 'bordel', 'pd', 'fdp', 'ntm', 'tg', 'con', 'conne', 'couille', 'bite', 'chatte', 'sexe', 'porno',
    'fuck', 'shit', 'bitch', 'asshole', 'dick', 'pussy', 'porn', 'sex', 'nigger', 'nigga', 'negro', 'nazi',
    'hitler', 'satan', 'lucifer', 'diable', 'devil', 'admin', 'administrateur', 'moderateur', 'moderator',
)


def norm_key(text):
    """Forme de comparaison : minuscules, sans accents ni séparateurs."""
    text = unicodedata.normalize('NFKD', text or '')
    text = ''.join(c for c in text if not unicodedata.combining(c)).lower()
    return re.sub(r'[^0-9a-z]+', '', text)


def _words(text):
    text = unicodedata.normalize('NFKD', text or '')
    text = ''.join(c for c in text if not unicodedata.combining(c)).lower()
    return [w for w in re.split(r'[^0-9a-z]+', text) if w]


class BqApiSession(models.Model):
    _name = 'bq.api.session'
    _description = "Session de l'application Quiz Biblique"
    _inherit = ['api.session.mixin']
    _session_ttl_days = 365


class BqPlayer(models.Model):
    _name = 'bq.player'
    _description = "Joueur (Quiz Biblique)"
    _order = 'pseudo'
    _rec_name = 'pseudo'

    partner_id = fields.Many2one('res.partner', "Personne", required=True, ondelete='restrict', index=True)
    phone = fields.Char(related='partner_id.phone', string="Téléphone")
    pseudo = fields.Char("Pseudo", required=True)
    pseudo_key = fields.Char("Pseudo (forme de comparaison)", required=True, index=True, readonly=True)
    lang = fields.Selection([('fr', "Français"), ('en', "English")], "Langue", default='fr', required=True)
    channel_token = fields.Char(
        "Canal personnel", required=True, readonly=True, copy=False, groups='base.group_system',
        default=lambda self: secrets.token_urlsafe(24),
        help="Secret du canal temps réel du joueur (invitations, tour de tournoi).")
    active = fields.Boolean(default=True)
    banned = fields.Boolean("Suspendu", help="Un joueur suspendu ne peut plus rejoindre ni créer de partie.")
    ban_reason = fields.Char("Motif de la suspension")
    match_id = fields.Many2one('bq.match', "Match en cours", readonly=True, index=True, ondelete='set null')
    tournament_id = fields.Many2one('bq.tournament', "Tournoi en cours", readonly=True, index=True,
                                    ondelete='set null')
    matches_played = fields.Integer("Matchs joués", readonly=True)
    matches_won = fields.Integer("Matchs gagnés", readonly=True)
    tournaments_won = fields.Integer("Tournois gagnés", readonly=True)
    report_ids = fields.One2many('bq.report', 'player_id', "Signalements")
    report_count = fields.Integer("Signalements à traiter", compute='_compute_report_count')

    _partner_uniq = models.Constraint('UNIQUE (partner_id)', "Cette personne est déjà inscrite comme joueur.")
    _pseudo_key_uniq = models.Constraint('UNIQUE (pseudo_key)', "Ce pseudo est déjà pris.")

    @api.depends('report_ids.state')
    def _compute_report_count(self):
        for player in self:
            player.report_count = len(player.report_ids.filtered(lambda r: r.state == 'open'))

    # ------------------------------------------------------------------ pseudo
    @api.model
    def _banned_words(self):
        extra = self.env['ir.config_parameter'].sudo().get_param(PREFIX + 'banned_words') or ''
        return set(BANNED_DEFAULT) | {norm_key(w) for w in extra.split(',') if norm_key(w)}

    @api.model
    def _check_pseudo(self, pseudo, exclude=None):
        """Rend le pseudo nettoyé, ou lève ValidationError (message montré au joueur)."""
        pseudo = re.sub(r'\s+', ' ', (pseudo or '').strip())
        if not PSEUDO_RE.match(pseudo):
            raise ValidationError(_("Le pseudo compte de 3 à 20 caractères : lettres, chiffres, espace, point, "
                                    "tiret ou apostrophe."))
        key = norm_key(pseudo)
        if len(key) < 3:
            raise ValidationError(_("Le pseudo doit contenir au moins trois lettres ou chiffres."))
        banned = self._banned_words()
        words = set(_words(pseudo))
        if any(w in words for w in banned) or any(len(w) >= 6 and w in key for w in banned):
            raise ValidationError(_("Ce pseudo n'est pas accepté. Choisissez-en un autre."))
        domain = [('pseudo_key', '=', key)]
        if exclude:
            domain.append(('id', '!=', exclude.id))
        if self.sudo().with_context(active_test=False).search_count(domain):
            raise ValidationError(_("Ce pseudo est déjà pris."))
        return pseudo

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('pseudo'):
                vals['pseudo'] = self._check_pseudo(vals['pseudo'])
                vals['pseudo_key'] = norm_key(vals['pseudo'])
        return super().create(vals_list)

    def write(self, vals):
        if 'pseudo' in vals:
            for player in self:
                pseudo = self._check_pseudo(vals['pseudo'], exclude=player)
                super(BqPlayer, player).write(dict(vals, pseudo=pseudo, pseudo_key=norm_key(pseudo)))
            return True
        return super().write(vals)

    # ------------------------------------------------------------------ engagement
    def _lock(self):
        """Verrou de ligne : les inscriptions concurrentes d'un même joueur passent l'une après l'autre."""
        if self.ids:
            self.env.cr.execute('SELECT id FROM bq_player WHERE id IN %s ORDER BY id FOR UPDATE', [tuple(self.ids)])
            self.invalidate_recordset(['match_id', 'tournament_id'])
        return self

    def _ensure_can_play(self):
        for player in self:
            if player.banned or not player.active:
                raise UserError(_("Votre compte joueur est suspendu."))

    def _engage(self, match=None, tournament=None):
        """Engage les joueurs dans ``match`` OU ``tournament``. Lève UserError si l'un est déjà pris ailleurs."""
        self._lock()
        self._ensure_can_play()
        for player in self:
            if match and player.match_id == match and not player.tournament_id:
                continue
            if tournament and player.tournament_id == tournament and not player.match_id:
                continue
            if player.match_id or player.tournament_id:
                raise UserError(_("%(pseudo)s participe déjà à %(what)s. Un joueur ne joue qu'une partie à la fois.",
                                  pseudo=player.pseudo, what=player._engagement_label()))
        values = {'match_id': match.id if match else False, 'tournament_id': tournament.id if tournament else False}
        self.sudo().write(values)

    def _release(self, match=None, tournament=None):
        """Libère les joueurs de ``match`` / ``tournament`` (sans toucher à un autre engagement)."""
        self._lock()
        for player in self:
            if match and player.match_id == match:
                player.sudo().match_id = False
            if tournament and player.tournament_id == tournament:
                player.sudo().tournament_id = False

    def _engagement_label(self):
        self.ensure_one()
        if self.match_id:
            return _("un match (code %s)", self.match_id.code)
        if self.tournament_id:
            return _("le tournoi « %s »", self.tournament_id.name)
        return ''

    def _public(self):
        return {'id': self.id, 'pseudo': self.pseudo}

    def action_unban(self):
        self.write({'banned': False, 'ban_reason': False})

    def action_release(self):
        """Back-office : libère un joueur resté engagé (incident)."""
        self.sudo().write({'match_id': False, 'tournament_id': False})


class BqReport(models.Model):
    _name = 'bq.report'
    _description = "Signalement d'un pseudo"
    _order = 'id desc'

    player_id = fields.Many2one('bq.player', "Joueur signalé", required=True, ondelete='cascade', index=True)
    reporter_id = fields.Many2one('bq.player', "Signalé par", required=True, ondelete='cascade')
    pseudo = fields.Char("Pseudo au moment du signalement", readonly=True)
    reason = fields.Char("Motif")
    state = fields.Selection([('open', "À traiter"), ('done', "Traité"), ('rejected', "Sans suite")],
                             default='open', required=True)

    _one_per_reporter = models.Constraint(
        'UNIQUE (player_id, reporter_id, pseudo)', "Vous avez déjà signalé ce pseudo.")

    def action_done(self):
        self.write({'state': 'done'})

    def action_reject(self):
        self.write({'state': 'rejected'})

    def action_ban(self):
        for report in self:
            report.player_id.write({'banned': True, 'ban_reason': report.reason or _("Pseudo signalé")})
        self.write({'state': 'done'})
