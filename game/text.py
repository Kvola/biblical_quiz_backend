"""Outils de texte, portés de l'application Flutter (lib/domain/search.dart,
lib/domain/book_challenge.dart)."""
import re

# Lettres accentuées et ligatures -> forme de base (minuscule).
_FOLD = {
    'à': 'a', 'á': 'a', 'â': 'a', 'ã': 'a', 'ä': 'a', 'å': 'a', 'ç': 'c', 'è': 'e', 'é': 'e', 'ê': 'e', 'ë': 'e',
    'ì': 'i', 'í': 'i', 'î': 'i', 'ï': 'i', 'ñ': 'n', 'ò': 'o', 'ó': 'o', 'ô': 'o', 'õ': 'o', 'ö': 'o',
    'ù': 'u', 'ú': 'u', 'û': 'u', 'ü': 'u', 'ý': 'y', 'ÿ': 'y', 'œ': 'oe', 'æ': 'ae', 'ß': 'ss',
}


def _is_word_char(ch):
    return 'a' <= ch <= 'z' or '0' <= ch <= '9'


def normalize(text):
    """Forme de recherche : minuscules, accents repliés, toute suite de caractères
    ni lettre ni chiffre réduite à une espace (« l'Éternel » -> « l eternel »)."""
    out = []
    pending_space = False
    for ch in text:
        folded = _FOLD.get(ch.lower(), ch.lower())
        if len(folded) == 1 and not _is_word_char(folded):
            pending_space = bool(out)
            continue
        if pending_space:
            out.append(' ')
            pending_space = False
        out.append(folded)
    return ''.join(out)


_COMPLETE_RE = re.compile(r'[.!?»"”]$')


def is_complete_sentence(text):
    """Un verset qui se termine au milieu d'une phrase ne se comprend pas seul."""
    return bool(_COMPLETE_RE.search(text.strip()))
