# -*- coding: utf-8 -*-
"""Réglages du jeu en ligne (Quiz Biblique → Configuration → Réglages).

Tous lus par :func:`bq_param` : une valeur absente ou illisible retombe sur la
valeur par défaut, bornée. Une faute de frappe dans le back-office ne doit
jamais casser un match.
"""
from odoo import api, fields, models

#: clé → (défaut, minimum, maximum)
PARAMS = {
    'max_players': (10, 2, 50),             # joueurs par match libre (individuel)
    'team_max_size': (5, 2, 20),            # joueurs par équipe
    'team_min_size': (2, 1, 20),            # joueurs par équipe, au minimum
    'max_live_matches': (50, 1, 10000),     # matchs libres ouverts ou en cours, en même temps
    'max_live_tournaments': (5, 1, 1000),   # tournois ouverts ou en cours, en même temps
    'tournament_max_entries': (32, 2, 256),
    'question_seconds_default': (30, 5, 300),
    'hand_seconds_default': (5, 2, 60),
    'question_count_default': (10, 1, 100),
    'reveal_seconds': (6, 2, 30),           # affichage de la réponse et de son explication
    'forfeit_minutes_default': (10, 1, 1440),
    'lobby_idle_minutes': (30, 5, 1440),    # salle d'attente abandonnée → annulée
    'running_idle_minutes': (10, 2, 1440),  # match sans aucune activité → clos
}

PREFIX = 'biblical_quiz.'


def bq_param(env, key):
    default, low, high = PARAMS[key]
    raw = env['ir.config_parameter'].sudo().get_param(PREFIX + key)
    try:
        value = int(raw) if raw not in (None, False, '') else default
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    bq_max_players = fields.Integer(
        "Joueurs par match (au plus)", config_parameter=PREFIX + 'max_players', default=10,
        help="Match individuel : de 2 joueurs à ce nombre.")
    bq_team_max_size = fields.Integer(
        "Joueurs par équipe (au plus)", config_parameter=PREFIX + 'team_max_size', default=5)
    bq_team_min_size = fields.Integer(
        "Joueurs par équipe (au moins)", config_parameter=PREFIX + 'team_min_size', default=2)
    bq_max_live_matches = fields.Integer(
        "Matchs simultanés (au plus)", config_parameter=PREFIX + 'max_live_matches', default=50,
        help="Matchs libres en salle d'attente ou en cours en même temps, sur tout le serveur. "
             "Au-delà, la création d'un match est refusée (« réessayez plus tard »). "
             "Les duels de tournoi n'y sont pas comptés : ils sont bornés par le nombre de tournois.")
    bq_max_live_tournaments = fields.Integer(
        "Tournois simultanés (au plus)", config_parameter=PREFIX + 'max_live_tournaments', default=5)
    bq_tournament_max_entries = fields.Integer(
        "Participants par tournoi (au plus)", config_parameter=PREFIX + 'tournament_max_entries', default=32)
    bq_question_seconds_default = fields.Integer(
        "Temps par question (s, par défaut)", config_parameter=PREFIX + 'question_seconds_default', default=30)
    bq_hand_seconds_default = fields.Integer(
        "Durée de la main (s, par défaut)", config_parameter=PREFIX + 'hand_seconds_default', default=5)
    bq_question_count_default = fields.Integer(
        "Questions par match (par défaut)", config_parameter=PREFIX + 'question_count_default', default=10)
    bq_reveal_seconds = fields.Integer(
        "Affichage de la réponse (s)", config_parameter=PREFIX + 'reveal_seconds', default=6)
    bq_forfeit_minutes_default = fields.Integer(
        "Forfait d'un duel de tournoi (min, par défaut)", config_parameter=PREFIX + 'forfeit_minutes_default',
        default=10)
    bq_banned_words = fields.Char(
        "Mots interdits dans les pseudos", config_parameter=PREFIX + 'banned_words',
        help="Séparés par des virgules ; s'ajoutent à la liste livrée avec le module.")

    bq_phone_countries = fields.Char(
        "Pays proposés à la connexion", config_parameter=PREFIX + 'phone_countries', default='CI',
        help="Codes ISO séparés par des virgules (ex. CI,SN,BF,FR).")

    @api.model
    def _bq_limits(self):
        return {key: bq_param(self.env, key) for key in PARAMS}
