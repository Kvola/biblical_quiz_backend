"""Réponses d'IA factices sur Jean 3, pour les tests (aucun appel réel)."""


def good(verse=16, n=0):
    """Une question correcte ; `n` la rend distincte."""
    base = {
        16: dict(
            q_fr="Selon ce passage, pourquoi Dieu a-t-il donné son Fils unique ?",
            q_en="According to this passage, why did God give his only begotten Son?",
            choices_fr=["Pour que celui qui croit en lui ait la vie éternelle", "Pour que le monde entier soit jugé",
                        "Pour que les pharisiens gardent la loi", "Pour que Nicodème devienne son disciple"],
            choices_en=["So that whoever believes in him may have everlasting life", "So that the whole world may be judged",
                        "So that the Pharisees may keep the law", "So that Nicodemus may become his disciple"],
            answer=0,
            explain_fr="Le verset dit que Dieu a donné son Fils « afin que quiconque croit en lui ne périsse point ».",
            explain_en="The verse says God gave his Son “that whosoever believeth in him should not perish”.",
            quote_fr="afin que quiconque croit en lui ne périsse point",
            quote_en="that whosoever believeth in him should not perish"),
        3: dict(
            q_fr="Que faut-il, selon Jésus, pour voir le royaume de Dieu ?",
            q_en="According to Jesus, what is needed to see the kingdom of God?",
            choices_fr=["Garder toute la loi", "Naître de nouveau", "Être docteur d'Israël", "Venir de nuit"],
            choices_en=["To keep the whole law", "To be born again", "To be a master of Israel", "To come by night"],
            answer=1,
            explain_fr="Jésus répond à Nicodème que si un homme ne naît de nouveau, il ne peut voir le royaume de Dieu.",
            explain_en="Jesus answers Nicodemus that unless a man is born again he cannot see the kingdom of God.",
            quote_fr="si un homme ne naît de nouveau", quote_en="Except a man be born again"),
        19: dict(
            q_fr="Pourquoi les hommes ont-ils préféré les ténèbres à la lumière ?",
            q_en="Why did men love darkness rather than light?",
            choices_fr=["Ils ne connaissaient pas la lumière", "La lumière n'était pas encore venue",
                        "Leurs œuvres étaient mauvaises", "Ils craignaient les pharisiens"],
            choices_en=["They did not know the light", "The light had not yet come",
                        "Their deeds were evil", "They feared the Pharisees"],
            answer=2,
            explain_fr="Le texte donne la raison : leurs œuvres étaient mauvaises.",
            explain_en="The text gives the reason: their deeds were evil.",
            quote_fr="parce que leurs oeuvres étaient mauvaises", quote_en="because their deeds were evil"),
    }[verse]
    q = dict(base, verse=verse, skill='cause_effect')
    if n:
        q['q_fr'] += ' (%d)' % n
        q['q_en'] += ' (%d)' % n
    return q


def generation(*questions):
    return {'questions': list(questions) or [good(16), good(3), good(19)]}


def verdict(n, letter, **flags):
    v = {'n': n, 'answer_fr': letter, 'answer_en': letter, 'single_answer': True, 'same_meaning': True,
         'from_text': True, 'neutral': True, 'explanation_ok': True, 'note': ''}
    v.update(flags)
    return v


def verdicts(*items):
    return {'verdicts': list(items) or [verdict(1, 'A'), verdict(2, 'B'), verdict(3, 'C')]}
