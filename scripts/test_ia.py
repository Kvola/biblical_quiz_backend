# -*- coding: utf-8 -*-
"""Essai réel des questions rédigées par IA (Claude) sur le serveur, SANS RIEN ENREGISTRER.

À lancer dans le shell Odoo du serveur, par exemple :
    docker exec -i odoo19-web-prod-iyf odoo shell -d iyf.ebng.world --no-http \
        < addons/biblical_quiz_backend/scripts/test_ia.py

Rédige les questions d'un chapitre court (Philémon), les passe aux contrôles
automatiques puis à la relecture indépendante : deux appels à Claude, quelques
dizaines de centimes au plus. Affiche ce qui est gardé, ce qui est écarté et
pourquoi, puis ANNULE tout (rollback) : la base reste inchangée.
"""
BOOK, CHAPTER = 57, 1
try:
    from odoo.addons.biblical_quiz_backend.game import ai_bank
    from odoo.addons.biblical_quiz_backend.models.bq_ai import DEFAULT_MODEL, price_of
    C = env['bq.ai.chapter'].sudo()
    print('Clé présente :', bool(C._api_key()), '| suspendu :', C._suspended(),
          '| modèle :', C._param('ai_model') or DEFAULT_MODEL + ' (défaut)')
    data, t_in, t_out, model = C._call_claude(ai_bank.GENERATION_PROMPT, ai_bank.generation_message(BOOK, CHAPTER),
                                              ai_bank.GENERATION_SCHEMA, 'high')
    kept, dropped = ai_bank.validate(BOOK, CHAPTER, data)
    print('Rédaction : %s questions reçues, %s passent les contrôles automatiques' % (
        len(data.get('questions') or []), len(kept)))
    if kept:
        verdicts, v_in, v_out, _m = C._call_claude(ai_bank.VERIFY_PROMPT, ai_bank.verify_message(BOOK, CHAPTER, kept),
                                                   ai_bank.VERIFY_SCHEMA, 'medium')
        t_in, t_out = t_in + v_in, t_out + v_out
        kept, refused = ai_bank.apply_verdicts(kept, verdicts)
        dropped += refused
    price = price_of(model) or (0, 0)
    print('Modèle utilisé :', model, '| jetons :', t_in, '+', t_out,
          '| coût ≈ %.4f $ pour ce chapitre' % ((t_in * price[0] + t_out * price[1]) / 1e6))
    print('\nGARDÉES (%s) :' % len(kept))
    for q in kept:
        print('- v.%s  %s\n        → %s\n        EN : %s → %s' % (
            q['verse'], q['q']['fr'], q['choices']['fr'][q['answer']], q['q']['en'], q['choices']['en'][q['answer']]))
    print('\nÉCARTÉES (%s) :' % len(dropped))
    for q, why in dropped:
        title = (q.get('q_fr') or (q.get('q') or {}).get('fr')) if isinstance(q, dict) else q
        print('- %s\n        motif : %s' % (title, why))
finally:
    env.cr.rollback()
    print('\nAnnulé : rien n’a été enregistré.')
