"""Fabrication des questions d'un match sur des passages bibliques choisis.

Portage de lib/domain/reading_quiz.dart (application Flutter), en Python pur
(bibliothèque standard seulement, aucun import d'Odoo) :

1. d'abord les questions RELUES (data/content/questions.json) dont le verset de
   départ est dans un des passages, mélangées ;
2. puis des questions tirées du texte biblique lui-même (justes par
   construction, id préfixé `gen-read-`) :
   - compléter un verset (nom propre ou nom commun retiré) ;
   - « Dans quel livre se trouve ce verset ? » (si au moins deux livres) ;
   - « Lequel de ces passages vient en premier ? » (si un livre a au moins
     quatre chapitres utilisables dans la portée).

Uniquement des questions à choix multiple (pas de vrai/faux). Aucun verset
n'est jamais modifié, hormis le mot retiré d'un texte à compléter.

La portée est au VERSET près : seuls les versets des passages servent de
questions. Les mots proposés à tort (distracteurs) viennent du texte des
passages ; si la portée est petite (moins de MIN_POOL_VERSES versets), on
élargit ce réservoir de mots aux chapitres entiers qui contiennent les
passages, puis, s'il le faut encore, aux chapitres voisins du même livre.
"""
from __future__ import annotations

import gzip
import json
import random
import re
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .books import BOOKS, book_genre, book_name
from .text import is_complete_sentence, normalize

DATA_DIR = Path(__file__).resolve().parent.parent / 'data' / 'content'
_BIBLE_FILES = {'fr': 'lsg.tsv.gz', 'en': 'kjv.tsv.gz'}
LANGS = ('fr', 'en')

ID_PREFIX = 'gen-read-'

# Taille minimale (en versets) du réservoir de mots pour les distracteurs.
MIN_POOL_VERSES = 60


# ---------------------------------------------------------------------------
# Bible et questions relues (chargées à la demande, une fois par processus)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_bibles: dict = {}
_curated: list | None = None


class Bible:
    """Une traduction en mémoire : _books[livre - 1][chapitre - 1][verset - 1] = texte."""

    def __init__(self, books):
        self._books = books

    def chapter_count(self, book):
        return len(self._books[book - 1])

    def verse_count(self, book, chapter):
        return len(self._books[book - 1][chapter - 1])

    def chapter(self, book, chapter):
        return self._books[book - 1][chapter - 1]

    def text(self, book, chapter, verse):
        return self._books[book - 1][chapter - 1][verse - 1]

    def has(self, book, chapter, verse=1):
        return (1 <= book <= 66 and 1 <= chapter <= self.chapter_count(book)
                and 1 <= verse <= self.verse_count(book, chapter))


def _load_bible(lang):
    books = [[] for _ in range(66)]
    with gzip.open(DATA_DIR / _BIBLE_FILES[lang], 'rt', encoding='utf-8') as f:
        for n, line in enumerate(f, 1):
            line = line.rstrip('\n')
            if not line:
                continue
            parts = line.split('\t')
            if len(parts) != 5:
                raise ValueError(f'Ligne {n} : 5 colonnes attendues')
            b, c, v = int(parts[0]), int(parts[1]), int(parts[2])
            chapters = books[b - 1]
            while len(chapters) < c:
                chapters.append([])
            verses = chapters[c - 1]
            if len(verses) != v - 1:
                raise ValueError(f'Ligne {n} : verset {b}:{c}:{v} hors séquence')
            verses.append(parts[4])
    for i, chapters in enumerate(books):
        if not chapters or any(not c for c in chapters):
            raise ValueError(f'Livre {i + 1} incomplet')
    return Bible(books)


def get_bible(lang):
    """Bible de la langue ('fr' = Louis Segond, 'en' = KJV), en cache."""
    _check_lang(lang)
    bible = _bibles.get(lang)
    if bible is None:
        with _lock:
            bible = _bibles.get(lang)
            if bible is None:
                bible = _bibles[lang] = _load_bible(lang)
    return bible


def curated_questions():
    """Questions relues à choix multiple, avec leur verset de départ (`_start`)."""
    global _curated
    if _curated is None:
        with _lock:
            if _curated is None:
                data = json.loads((DATA_DIR / 'questions.json').read_text(encoding='utf-8'))
                for q in data:
                    q['_start'] = _parse_ref(q['ref'])[:3]
                _curated = data
    return _curated


def _check_lang(lang):
    if lang not in LANGS:
        raise ValueError(f'Langue inconnue : {lang!r}')


# ---------------------------------------------------------------------------
# Références et passages
# ---------------------------------------------------------------------------

_REF_RE = re.compile(r'^(\d{1,2}):(\d{1,3}):(\d{1,3})(?:-(?:(\d{1,3}):)?(\d{1,3}))?$')
_CHAPTERS_RE = re.compile(r'^(\d{1,2}):(\d{1,3})(?:-(\d{1,3}))?$')


def _parse_ref(text):
    """« livre:chap:vers[-[chap:]vers] » -> (livre, c1, v1, c2, v2), sans vérifier la Bible."""
    m = _REF_RE.match(text.strip())
    if not m:
        raise ValueError(f'Référence mal formée : {text!r}')
    b, c1, v1 = int(m[1]), int(m[2]), int(m[3])
    c2 = c1 if m[5] is None else int(m[4] or m[2])
    v2 = v1 if m[5] is None else int(m[5])
    return b, c1, v1, c2, v2


def _cv_label(c1, v1, c2, v2):
    """« 3:16 », « 3:1-21 », « 1:1–2:3 »."""
    if (c1, v1) == (c2, v2):
        return f'{c1}:{v1}'
    if c1 == c2:
        return f'{c1}:{v1}-{v2}'
    return f'{c1}:{v1}–{c2}:{v2}'


def _cv_code(book, c1, v1, c2, v2):
    if (c1, v1) == (c2, v2):
        return f'{book}:{c1}:{v1}'
    if c1 == c2:
        return f'{book}:{c1}:{v1}-{v2}'
    return f'{book}:{c1}:{v1}-{c2}:{v2}'


def ref_label(code, lang):
    """Libellé d'une référence de versets : « Jean 3:16 » / « John 3:16 »."""
    b, c1, v1, c2, v2 = _parse_ref(code)
    return f'{book_name(b, lang)} {_cv_label(c1, v1, c2, v2)}'


@dataclass(frozen=True)
class Passage:
    """Un passage d'un seul livre, du verset (c1, v1) au verset (c2, v2) inclus.

    `whole_chapters` : saisi comme chapitre(s) entier(s) (« 43:3 », « 1:1-3 »)."""
    book: int
    start_chapter: int
    start_verse: int
    end_chapter: int
    end_verse: int
    whole_chapters: bool = False

    @property
    def code(self):
        """Forme canonique (« 43:3 », « 1:1-3 », « 43:3:1-21 »)."""
        if self.whole_chapters:
            if self.start_chapter == self.end_chapter:
                return f'{self.book}:{self.start_chapter}'
            return f'{self.book}:{self.start_chapter}-{self.end_chapter}'
        return _cv_code(self.book, self.start_chapter, self.start_verse, self.end_chapter, self.end_verse)

    def contains(self, book, chapter, verse):
        return (book == self.book
                and (self.start_chapter, self.start_verse) <= (chapter, verse) <= (self.end_chapter, self.end_verse))

    def verses(self, bible):
        """(chapitre, verset) couverts, dans l'ordre."""
        for c in range(self.start_chapter, self.end_chapter + 1):
            first = self.start_verse if c == self.start_chapter else 1
            last = self.end_verse if c == self.end_chapter else bible.verse_count(self.book, c)
            for v in range(first, last + 1):
                yield c, v


def parse_passage(code):
    """Analyse et valide un passage contre la Bible.

    Formes admises : 'livre:c1:v1-c2:v2', 'livre:c1:v1-v2', 'livre:c1:v1',
    'livre:c1' (chapitre entier), 'livre:c1-c2' (chapitres entiers).
    Lève ValueError (message en français) si le passage est mal formé ou n'existe pas.
    """
    if not isinstance(code, str):
        raise ValueError(f'Passage mal formé : {code!r}')
    text = code.strip()
    bible = get_bible('fr')  # même versification pour LSG et KJV
    m = _CHAPTERS_RE.match(text)
    if m:
        b, c1 = int(m[1]), int(m[2])
        c2 = int(m[3]) if m[3] else c1
        _check_book(b, code)
        if c2 < c1:
            raise ValueError(f'Passage invalide (fin avant le début) : {code!r}')
        for c in (c1, c2):
            if not bible.has(b, c):
                raise ValueError(f'Chapitre inexistant : {book_name(b, "fr")} {c}')
        return Passage(b, c1, 1, c2, bible.verse_count(b, c2), whole_chapters=True)
    if not _REF_RE.match(text):
        raise ValueError(f'Passage mal formé : {code!r}')
    b, c1, v1, c2, v2 = _parse_ref(text)
    _check_book(b, code)
    if (c2, v2) < (c1, v1):
        raise ValueError(f'Passage invalide (fin avant le début) : {code!r}')
    for c, v in ((c1, v1), (c2, v2)):
        if not bible.has(b, c):
            raise ValueError(f'Chapitre inexistant : {book_name(b, "fr")} {c}')
        if not bible.has(b, c, v):
            raise ValueError(f'Verset inexistant : {book_name(b, "fr")} {c}:{v}')
    return Passage(b, c1, v1, c2, v2)


def _check_book(b, code):
    if not 1 <= b <= 66:
        raise ValueError(f'Livre inconnu dans le passage : {code!r}')


def passage_label(passage, lang):
    """« Jean 3:1-21 », « Genèse 1–3 », « Jean 3 » / « John 3:1-21 »."""
    _check_lang(lang)
    if isinstance(passage, str):
        passage = parse_passage(passage)
    name = book_name(passage.book, lang)
    if passage.whole_chapters:
        if passage.start_chapter == passage.end_chapter:
            return f'{name} {passage.start_chapter}'
        return f'{name} {passage.start_chapter}–{passage.end_chapter}'
    return f'{name} {_cv_label(passage.start_chapter, passage.start_verse, passage.end_chapter, passage.end_verse)}'


# ---------------------------------------------------------------------------
# Construction d'un questionnaire
# ---------------------------------------------------------------------------

def build_questions(passages, lang, count, rnd, exclude_ids=frozenset()):
    """Questions à choix multiple pour un match sur `passages` (codes, voir parse_passage).

    Renvoie au plus `count` dicts {'id', 'prompt', 'options', 'answer', 'explain',
    'ref', 'ref_label', 'source'} ; `options` déjà mélangées, `answer` = indice de
    la bonne. Relues d'abord, puis générées. Peut en renvoyer moins si les
    passages sont trop courts (voir available_count).
    """
    _check_lang(lang)
    parsed = [p if isinstance(p, Passage) else parse_passage(p) for p in passages]
    if not parsed or count <= 0:
        return []
    exclude_ids = frozenset(exclude_ids)

    # 1) Questions relues, d'abord.
    curated = [q for q in curated_questions()
               if q['id'] not in exclude_ids and any(p.contains(*q['_start']) for p in parsed)]
    rnd.shuffle(curated)
    out = [_from_curated(q, lang, rnd) for q in curated[:count]]
    if len(out) >= count:
        return out

    # 2) Questions tirées du texte, pour compléter.
    gen = _Generator(get_bible(lang), lang, parsed, rnd, exclude_ids)
    for q in curated[:count]:
        gen.used.add(q['_start'])  # pas de texte à compléter sur un verset déjà interrogé
    out.extend(gen.generate(count - len(out)))
    return out


def available_count(passages, lang, cap=40):
    """Nombre de questions distinctes qu'on sait fabriquer (au plus `cap`, graine fixe)."""
    _check_lang(lang)
    codes = tuple(p.code if isinstance(p, Passage) else p for p in passages)
    return _available_count(codes, lang, cap)


@lru_cache(maxsize=512)
def _available_count(codes, lang, cap):
    return len(build_questions(list(codes), lang, cap, random.Random(0)))


def _from_curated(q, lang, rnd):
    choices = q['choices'][lang]
    order = list(range(len(choices)))
    rnd.shuffle(order)
    return {
        'id': q['id'],
        'prompt': q['q'][lang],
        'options': [choices[i] for i in order],
        'answer': order.index(q['answer']),
        'explain': q['explain'][lang],
        'ref': q['ref'],
        'ref_label': ref_label(q['ref'], lang),
        'source': 'reviewed',
    }


# ---------------------------------------------------------------------------
# Générateur (portage de _Generator)
# ---------------------------------------------------------------------------

# Noms divins et mots trop attendus : jamais la réponse à compléter (formes normalisées).
_NEVER_ANSWER = frozenset({
    'dieu', 'eternel', 'seigneur', 'christ', 'esprit', 'saint', 'jesus', 'jesus christ', 'saint esprit', 'messie',
    'lord', 'god', 'spirit', 'holy', 'ghost', 'almighty', 'amen', 'alleluia', 'selah', 'sela',
})

# Déterminants français -> genre ('m', 'f' ou None si indéterminé).
_DET_FR = {
    'le': 'm', 'un': 'm', 'ce': 'm', 'cet': 'm', 'du': 'm', 'au': 'm', 'mon': None, 'ton': None, 'son': None,
    'la': 'f', 'une': 'f', 'cette': 'f', 'ma': 'f', 'ta': 'f', 'sa': 'f',
    'les': None, 'des': None, 'aux': None, 'ces': None, 'mes': None, 'tes': None, 'ses': None, 'nos': None,
    'vos': None, 'leurs': None, 'notre': None, 'votre': None, 'leur': None, 'l': None,
}
_DET_EN = frozenset({'the', 'a', 'an', 'his', 'her', 'their', 'my', 'thy', 'our', 'your', 'this', 'these',
                     'those', 'its'})

_STOP_FR = frozenset({
    'avec', 'dans', 'pour', 'mais', 'comme', 'sans', 'sous', 'vers', 'chez', 'entre', 'tout', 'tous', 'toute',
    'toutes', 'cette', 'leur', 'leurs', 'elle', 'elles', 'nous', 'vous', 'ils', 'lui', 'sont', 'était', 'être',
    'avait', 'fait', 'dont', 'quand', 'alors', 'ainsi', 'aussi', 'point', 'même', 'autre', 'autres', 'chose',
})
_STOP_EN = frozenset({
    'unto', 'thee', 'thou', 'thine', 'thy', 'hath', 'shall', 'shalt', 'that', 'with', 'which', 'them', 'they',
    'their', 'from', 'have', 'there', 'were', 'when', 'this', 'into', 'upon', 'also', 'said', 'came', 'went',
    'things', 'thing', 'every', 'neither', 'whom', 'what', 'saith', 'doth', 'didst', 'hast', 'wilt',
})

# Mots qui suivent naturellement un nom (sinon le mot est probablement un adjectif).
_AFTER_NOUN_FR = frozenset({
    'de', 'du', 'des', 'd', 'et', 'ou', 'qui', 'que', 'qu', 'à', 'au', 'aux', 'dans', 'sur', 'pour', 'par', 'avec',
    'en', 'ne', 'n', 'se', 's', 'lui', 'leur', 'il', 'ils', 'elle', 'elles', 'est', 'fut', 'sont', 'était', 'étaient',
    'a', 'avait', 'sera', 'seront', 'devant', 'contre', 'selon', 'comme', 'car', 'mais', 'vers', 'sous',
})
_AFTER_NOUN_EN = frozenset({
    'of', 'and', 'that', 'which', 'who', 'in', 'to', 'unto', 'upon', 'from', 'with', 'for', 'was', 'were', 'is',
    'are', 'shall', 'will', 'be', 'had', 'hath', 'have', 'by', 'before', 'against', 'into', 'as', 'but', 'or', 'not',
})

# Mots à majuscule après « said, » (discours rapporté de la KJV) : pas des noms propres.
_NOT_NAMES = frozenset({'who', 'what', 'why', 'how', 'where', 'whence', 'whither', 'behold', 'lo', 'yea', 'nay',
                        'thou', 'ye', 'thy', 'thus', 'surely', 'verily', 'hear', 'come', 'let', 'give', 'take'})
_WORD_RE = re.compile(r'[^\W\d_](?:[^\W\d_]|-)*')  # \p{L}[\p{L}-]*
_SENTENCE_END_RE = re.compile(r'[.!?:;]\s*$')
_CLOZE_START_RE = re.compile(r'[.!?:;«“"]\s*$')
_PUNCT_RE = re.compile(r'[,.;:!?»”)]')
_STARTS_UPPER_RE = re.compile(r'^[«"“(]?\s*(\S)')
_DIGIT_RE = re.compile(r'\d')
_BEGAT_RE = re.compile(r'\b(engendra|begat)\b')
_SPACES_RE = re.compile(r'\s+')


def _upper(w):
    return w[:1].isupper()


class _Word:
    __slots__ = ('surface', 'genders', 'count', 'with_article')

    def __init__(self, surface):
        self.surface = surface
        self.genders = []  # liste ordonnée (pas d'ensemble : ordre déterministe)
        self.count = 0
        self.with_article = 0

    @property
    def gender(self):
        return self.genders[0] if len(self.genders) == 1 else None


class _Generator:
    def __init__(self, bible, lang, passages, rnd, exclude_ids):
        self.bible = bible
        self.lang = lang
        self.fr = lang != 'en'
        self.rnd = rnd
        self.exclude_ids = exclude_ids
        self.det = _DET_FR if self.fr else _DET_EN
        self.stop = _STOP_FR if self.fr else _STOP_EN
        self.after_noun = _AFTER_NOUN_FR if self.fr else _AFTER_NOUN_EN
        # Livres de la portée, dans l'ordre de la Bible.
        self.books = sorted({p.book for p in passages})
        # Versets de la portée (sans doublon si des passages se chevauchent).
        scope = set()
        for p in passages:
            scope.update((p.book, c, v) for c, v in p.verses(bible))
        self.scope = scope
        self.by_chapter: dict = {}  # (livre, chap) -> [(verset, texte)] utilisables, dans la portée
        self.proper: dict = {}  # forme normalisée -> _Word
        self.nouns: dict = {}
        self.lower_seen = set()
        self.used = set()  # (livre, chap, verset) déjà employés
        self._exclude_verses()
        self._analyse(self._pool(scope))
        top = sorted(self.proper.items(), key=lambda e: -e[1].count)[:3]
        self.too_frequent = {k for k, _ in top}

    def _exclude_verses(self):
        """Versets des questions générées déjà vues (exclude_ids) : on ne les réutilise pas."""
        for qid in self.exclude_ids:
            if not qid.startswith(ID_PREFIX):
                continue
            kind, _, rest = qid[len(ID_PREFIX):].partition('-')
            if kind == 'cloze':
                rest = rest.partition('-')[0]
            if kind in ('cloze', 'book'):
                try:
                    self.used.add(_parse_ref(rest)[:3])
                except ValueError:
                    pass

    def _pool(self, scope):
        """Versets dont on tire le vocabulaire : la portée, élargie si elle est petite."""
        if len(scope) >= MIN_POOL_VERSES:
            return sorted(scope)
        chapters = {(b, c) for b, c, _ in scope}
        total = sum(self.bible.verse_count(b, c) for b, c in chapters)
        # Chapitres voisins (alternativement avant/après), tant que le réservoir est trop petit.
        step = 1
        while total < MIN_POOL_VERSES:
            added = False
            for b, c in sorted(chapters):
                for n in (c - step, c + step):
                    if (b, n) not in chapters and 1 <= n <= self.bible.chapter_count(b):
                        chapters.add((b, n))
                        total += self.bible.verse_count(b, n)
                        added = True
            if not added:
                break
            step += 1
        return [(b, c, v) for b, c in sorted(chapters) for v in range(1, self.bible.verse_count(b, c) + 1)]

    # -- Analyse du texte ------------------------------------------------------
    def _analyse(self, pool):
        for b, c, v in pool:
            text = self.bible.text(b, c, v)
            matches = list(_WORD_RE.finditer(text))
            words = [m.group() for m in matches]
            for w in words:
                if not _upper(w):
                    self.lower_seen.add(normalize(w))
            proper_count = 0
            for i, w in enumerate(words):
                key = normalize(w)
                if len(key) < 3:
                    continue
                sentence_start = i == 0 or bool(_SENTENCE_END_RE.search(text[:matches[i].start()]))
                if _upper(w) and not sentence_start and w.upper() != w and key not in _NOT_NAMES:
                    proper_count += 1
                    p = self.proper.get(key)
                    if p is None:
                        p = self.proper[key] = _Word(w)
                    p.count += 1
                    # « l'Agneau », « le Père » : un titre précédé d'un article, pas un nom de personne.
                    if i > 0 and words[i - 1].lower() in self.det:
                        p.with_article += 1
                elif i > 0 and len(w) >= 4 and not _upper(w):
                    det = words[i - 1].lower()
                    if det in self.det and w.lower() not in self.stop and self._ends_noun_phrase(text, matches, i):
                        n = self.nouns.get(key)
                        if n is None:
                            n = self.nouns[key] = _Word(w)
                        n.count += 1
                        g = _DET_FR.get(det) if self.fr else None
                        if g is not None and g not in n.genders:
                            n.genders.append(g)
            if (b, c, v) in self.scope and self._usable(text, proper_count):
                self.by_chapter.setdefault((b, c), []).append((v, text))

    def _ends_noun_phrase(self, text, matches, i):
        """Le mot n° i est-il suivi d'une ponctuation, de la fin, ou d'un mot outil ? (sinon : adjectif probable)"""
        if i + 1 >= len(matches):
            return True
        if matches[i + 1].group().lower() in self.after_noun:
            return True
        return bool(_PUNCT_RE.search(text[matches[i].end():matches[i + 1].start()]))

    @staticmethod
    def _usable(text, proper_count):
        """Phrase complète, ni liste ni généalogie, de longueur raisonnable."""
        t = text.strip()
        if len(t) < 50 or len(t) > 260 or not is_complete_sentence(t):
            return False
        # Le verset doit aussi COMMENCER une phrase.
        m = _STARTS_UPPER_RE.match(t)
        if not m or not m.group(1).isupper():
            return False
        if proper_count > 4:  # liste de noms, généalogie
            return False
        if len(_DIGIT_RE.findall(t)) >= 3:  # dénombrements
            return False
        if len(_BEGAT_RE.findall(t)) >= 2:
            return False
        return True

    def _is_proper(self, key):
        p = self.proper.get(key)
        return (p is not None and key not in self.lower_seen and key not in _NOT_NAMES
                and p.with_article * 2 < p.count)

    # -- Plan --------------------------------------------------------------------
    def generate(self, need):
        out, ids = [], set()
        plan = [self._cloze, self._cloze]
        if len(self.books) >= 2:
            plan.append(self._which_book)
        plan.append(self._cloze)
        order_book = self._order_book()
        if order_book is not None:
            plan.append(lambda: self._order(order_book))
        misses, i = 0, 0
        # Échecs consécutifs : chaque question réussie consomme au moins un verset, la boucle finit.
        while len(out) < need and misses < len(plan) * 6:
            q = plan[i % len(plan)]()
            i += 1
            if q is None or q['id'] in ids or q['id'] in self.exclude_ids:
                misses += 1
                continue
            misses = 0
            ids.add(q['id'])
            out.append(q)
        return out

    def _pick_verse(self, where=None):
        """Un verset utilisable pas encore employé, en variant les chapitres."""
        chapters = list(self.by_chapter)
        self.rnd.shuffle(chapters)
        for b, c in chapters:
            verses = list(self.by_chapter[(b, c)])
            self.rnd.shuffle(verses)
            for v, text in verses:
                if (b, c, v) in self.used:
                    continue
                if where is not None and not where(text):
                    continue
                return b, c, v, text
        return None

    def _label(self, b, c, v):
        return f'{book_name(b, self.lang)} {c}:{v}'

    def _question(self, qid, prompt, options, answer, explain, ref):
        return {
            'id': qid, 'prompt': prompt, 'options': options, 'answer': answer, 'explain': explain,
            'ref': ref, 'ref_label': ref_label(ref, self.lang), 'source': 'generated',
        }

    # -- Compléter le verset ---------------------------------------------------
    def _cloze(self):
        # Contrairement à l'application (12 essais), on essaie jusqu'à épuisement des versets :
        # chaque verset sans candidat est marqué employé, la boucle finit.
        while True:
            picked = self._pick_verse()
            if picked is None:
                return None
            b, c, v, text = picked
            self.used.add((b, c, v))
            matches = list(_WORD_RE.finditer(text))
            words = [m.group() for m in matches]
            # Candidats : noms propres d'abord, puis noms communs introduits par un article.
            proper_cands, noun_cands = [], []
            for i in range(1, len(words)):
                key = normalize(words[i])
                if key in _NEVER_ANSWER or key in self.too_frequent:
                    continue
                sentence_start = bool(_CLOZE_START_RE.search(text[:matches[i].start()]))
                if self._is_proper(key) and _upper(words[i]) and not sentence_start:
                    proper_cands.append((matches[i], self.proper[key], True))
                    continue
                n = self.nouns.get(key)
                if n is not None and not _upper(words[i]):
                    if words[i - 1].lower() in self.det and self._ends_noun_phrase(text, matches, i):
                        noun_cands.append((matches[i], n, False))
            # Écart (ajout au portage) : un mot présent deux fois dans le verset se devinerait.
            norm_verse = normalize(text).split()
            candidates = [x for x in proper_cands + noun_cands
                          if len(normalize(x[0].group()).split()) > 1 or norm_verse.count(normalize(x[0].group())) == 1]
            if not candidates:
                continue
            match, word, proper = candidates[0]
            surface = match.group()
            distractors = self._distractors(surface, word, proper, text)
            if len(distractors) < 3:
                continue
            # On retire CETTE occurrence (le mot peut figurer deux fois dans le verset).
            blanked = text[:match.start()] + '_____' + text[match.end():]
            label = self._label(b, c, v)
            options = [surface] + distractors
            self.rnd.shuffle(options)
            if self.fr:
                prompt = f'Complétez le verset ({label}) :\n« {blanked} »'
                explain = f'{label} : « {text} »'
            else:
                prompt = f'Complete the verse ({label}):\n“{blanked}”'
                explain = f'{label}: “{text}”'
            code = f'{b}:{c}:{v}'
            return self._question(f'{ID_PREFIX}cloze-{code}-{normalize(surface)}', prompt, options,
                                  options.index(surface), explain, code)

    def _distractors(self, surface, word, proper, verse):
        """Mots de même nature, présents dans le réservoir mais pas dans le verset."""
        in_verse = f' {normalize(verse)} '
        surface_key = normalize(surface)
        plural = self._plural(surface)
        gender = word.gender
        candidates = []
        for key, w in (self.proper if proper else self.nouns).items():
            if key == surface_key or f' {key} ' in in_verse or key in _NEVER_ANSWER:
                continue
            if proper and not self._is_proper(key):
                continue
            if not proper:
                if self._plural(w.surface) != plural:
                    continue
                g = w.gender
                if gender is not None and g is not None and g != gender:
                    continue
            if abs(len(w.surface) - len(surface)) > 6:
                continue
            candidates.append(w)
        # Les plus présents d'abord (plausibles pour qui a lu), puis tirage.
        candidates.sort(key=lambda w: -w.count)
        top = candidates[:10]
        self.rnd.shuffle(top)
        seen, out = {surface_key}, []
        for w in top:
            k = normalize(w.surface)
            if k not in seen:
                seen.add(k)
                out.append(w.surface)
        return out[:3]

    def _plural(self, w):
        lw = w.lower()
        if self.fr:
            return lw.endswith('s') or lw.endswith('x')
        return lw.endswith('s') and not lw.endswith('ss')

    # -- Dans quel livre ? -----------------------------------------------------
    def _which_book(self):
        picked = self._pick_verse(where=lambda t: len(t) <= 220)
        if picked is None:
            return None
        b, c, v, text = picked
        self.used.add((b, c, v))
        others = [x for x in self.books if x != b]
        self.rnd.shuffle(others)
        picks = others[:3]
        if len(picks) < 3:
            # Moins de quatre livres choisis : on complète avec des livres du même genre.
            genre = book_genre(b)
            same = [x[0] for x in BOOKS if x[5] == genre and x[0] != b and x[0] not in picks]
            self.rnd.shuffle(same)
            rest = [x[0] for x in BOOKS if x[0] != b and x[0] not in picks and x[0] not in same]
            self.rnd.shuffle(rest)
            for x in same + rest:
                if len(picks) >= 3:
                    break
                picks.append(x)
        ids = [b] + picks
        self.rnd.shuffle(ids)
        label = self._label(b, c, v)
        if self.fr:
            prompt = f'Dans quel livre se trouve ce verset ?\n« {text} »'
            explain = f'Ce verset se trouve en {label}.'
        else:
            prompt = f'Which book is this verse from?\n“{text}”'
            explain = f'This verse is found in {label}.'
        code = f'{b}:{c}:{v}'
        return self._question(f'{ID_PREFIX}book-{code}', prompt, [book_name(x, self.lang) for x in ids],
                              ids.index(b), explain, code)

    # -- Ordre du récit --------------------------------------------------------
    def _order_book(self):
        """Un livre dont au moins quatre chapitres ont des versets utilisables (sinon pas de question d'ordre)."""
        per_book: dict = {}
        for b, _ in self.by_chapter:
            per_book[b] = per_book.get(b, 0) + 1
        eligible = [b for b, n in per_book.items() if n >= 4]
        self.rnd.shuffle(eligible)
        return eligible[0] if eligible else None

    def _order(self, book):
        chapters = sorted(c for b, c in self.by_chapter if b == book)
        if len(chapters) < 4:
            return None
        # Quatre chapitres répartis sur la portée, un verset court de chacun.
        step = len(chapters) / 4
        picks = []
        for k in range(4):
            lo = int(k * step)
            hi = max(lo + 1, int((k + 1) * step))
            c = chapters[lo + self.rnd.randrange(hi - lo)]
            verses = [(v, t) for v, t in self.by_chapter[(book, c)] if len(t) <= 200]
            self.rnd.shuffle(verses)
            free = [(v, t) for v, t in verses if (book, c, v) not in self.used]
            if not free:
                return None
            picks.append((c, free[0][0], free[0][1]))

        def excerpt(t):
            words = _SPACES_RE.split(t)
            return t if len(words) <= 12 else ' '.join(words[:12]) + '…'

        options = [excerpt(t) for _, _, t in picks]
        if len(set(options)) < 4:
            return None
        for c, v, _ in picks:
            self.used.add((book, c, v))
        first = min(picks, key=lambda p: (p[0], p[1]))
        correct = excerpt(first[2])
        self.rnd.shuffle(options)
        name = book_name(book, self.lang)
        refs = (', puis ' if self.fr else ', then ').join(self._label(book, c, v) for c, v, _ in sorted(picks))
        if self.fr:
            prompt = f'Dans {name}, lequel de ces passages vient en premier ?'
            explain = f'Dans l’ordre du livre : {refs}.'
        else:
            prompt = f'In {name}, which of these passages comes first?'
            explain = f'In the order of the book: {refs}.'
        qid = f'{ID_PREFIX}order-' + '_'.join(f'{book}:{c}:{v}' for c, v, _ in picks)
        return self._question(qid, prompt, options, options.index(correct), explain, f'{book}:{first[0]}:{first[1]}')
