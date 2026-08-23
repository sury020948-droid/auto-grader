import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Literal

_CIRCLED = {chr(cp): str(i + 1) for i, cp in enumerate(range(0x2460, 0x2474))}
_MULTI_SEP = re.compile(r"[,，/·ㆍ]|또는|및|\bor\b|\band\b", re.IGNORECASE)
_PREFIX = re.compile(r"^(?:정답|답)\s*[:：]?\s*")
_WRAP = re.compile(r"^[\[\{\(（〈<]+|[\\\]\}\)）〉>]+$")
_NUM_BUN = re.compile(r"^(\d+)번$")
_WS = re.compile(r"\s+")

AnswerType = Literal["multiple_choice", "numeric"]

MC_LETTERS = frozenset("ABCDEFGHIJ")
MC_DIGITS = frozenset("123456789")
MC_JAMO = frozenset("ㄱㄴㄷㄹㅁㅂㅅㅇㅈㅊㅋㅌㅍㅎ")

_RAW_NUMERIC_RE = re.compile(
    r"^[+-]?(?:\d{1,3}(?:,\d{3})+|\d{1,12})(?:\.\d{1,6})?$"
)
_CANON_NUMERIC_RE = re.compile(r"^[+-]?\d{1,12}(?:\.\d{1,6})?$")

_JAMO_COMPAT = (
    "ㄱㄲㄳㄴㄵㄶㄷㄹㄺㄻㄼㄽㄾㄿㅀㅁㅂㅄㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
    "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣㅥㅦㅧㅨㅩㅪㅫㅬㅭㅮㅯㅰㅱㅲㅳㅴㅵㅶㅷㅸㅹㅺㅻㅼㅽㅾㅿㆀㆁㆂㆃ"
)
_JAMO_REVERSE = {
    unicodedata.normalize("NFKC", ch): ch for ch in _JAMO_COMPAT
}


def clean_text(text: str) -> str:
    if not text:
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    buf = []
    for ch in t:
        if ch in _CIRCLED:
            buf.append(_CIRCLED[ch])
            buf.append(",")
        else:
            buf.append(ch)
    t = unicodedata.normalize("NFKC", "".join(buf))
    if any(ch in _JAMO_REVERSE for ch in t):
        t = "".join(_JAMO_REVERSE.get(ch, ch) for ch in t)
    return t


def _prep_token(token: str) -> str:
    q = _WRAP.sub("", token.strip())
    q = _WS.sub("", q)
    q = q.strip(".。、")
    q = _NUM_BUN.sub(r"\1", q)
    return q.upper()


def _is_mc_token(q: str) -> bool:
    return len(q) == 1 and (q in MC_LETTERS or q in MC_DIGITS or q in MC_JAMO)


_FRACTION_OR_RANGE = re.compile(r"\d\s*[/-]\s*\d|\d\s*[~–—]\s*\d")


def normalize_mc(raw: str | None) -> str:
    """Canonicalize a multiple-choice answer (single label or comma-set), else ''."""
    if raw is None:
        return ""
    t = clean_text(str(raw)).strip()
    if not t or _FRACTION_OR_RANGE.search(t):
        return ""
    t = _PREFIX.sub("", t).strip()
    parts = [p for p in _MULTI_SEP.split(t) if p and p.strip()]
    normed: list[str] = []
    seen: set[str] = set()
    for p in parts:
        q = _prep_token(p)
        if not _is_mc_token(q) or q in seen:
            continue
        seen.add(q)
        normed.append(q)
    if normed and all(q.isdigit() for q in normed):
        normed.sort(key=int)
    elif normed and all(_is_jamo_label(q) for q in normed):
        pass
    return ",".join(normed)


def _is_jamo_label(q: str) -> bool:
    return q in MC_JAMO


def normalize_numeric(raw: str | None) -> str:
    """Canonicalize a single numeric answer ('1,234' -> '1234', '3.0' -> '3'), else ''."""
    if raw is None:
        return ""
    t = clean_text(str(raw)).strip()
    if not t:
        return ""
    cand = _numeric_candidate(t)
    if cand is None:
        return ""
    t = cand.replace(",", "")
    if not t.lstrip("+-") or not _CANON_NUMERIC_RE.fullmatch(t):
        return ""
    try:
        d = Decimal(t)
    except InvalidOperation:
        return ""
    if not d.is_finite():
        return ""
    if d == 0:
        return "0"
    return format(d.normalize(), "f")


def _unwrapped(raw: str) -> str:
    return _WRAP.sub("", _WS.sub("", raw.strip()))


def _numeric_candidate(t: str) -> str | None:
    """Return the cleaned string if it is a single numeric literal, else None."""
    stripped = _PREFIX.sub("", t).strip()
    for cand in (stripped, _unwrapped(stripped)):
        cand = cand[:-1] if cand.endswith(".") and cand[:-1].isdigit() else cand
        if cand and _RAW_NUMERIC_RE.fullmatch(cand):
            return cand
    return None


def classify_answer(raw: str | None) -> AnswerType | None:
    """Infer the supported question type of a raw answer, or None if unsupported."""
    if raw is None:
        return None
    t = clean_text(str(raw)).strip()
    if not t:
        return None
    if _numeric_candidate(t):
        return "numeric"
    if normalize_mc(t):
        return "multiple_choice"
    return None


def answer_matches_type(canonical: str, qtype: AnswerType) -> bool:
    """Validate an already-canonical answer against its declared question type."""
    if qtype == "numeric":
        return bool(_CANON_NUMERIC_RE.fullmatch(canonical))
    if qtype == "multiple_choice":
        return all(_is_mc_token(tok) for tok in canonical.split(","))
    return False


def normalize_answer(raw: str | None) -> str:
    """Canonical entry point: numeric first (thousands commas), then MC set."""
    if raw is None:
        return ""
    t = clean_text(str(raw)).strip()
    if not t:
        return ""
    cand = _numeric_candidate(t)
    if cand is not None:
        canon = normalize_numeric(cand)
        if canon:
            return canon
    canon_mc = normalize_mc(t)
    if canon_mc:
        return canon_mc
    return ""


def canonical_type(canonical: str) -> AnswerType:
    """Infer the question type of an already-canonical answer key."""
    return "numeric" if _CANON_NUMERIC_RE.fullmatch(canonical) else "multiple_choice"


def answers_equal(expected_canonical: str, given_raw: str) -> bool:
    """Grade strictly within the two supported formats."""
    given = normalize_answer(given_raw)
    if not given or not expected_canonical:
        return False
    if _CANON_NUMERIC_RE.fullmatch(expected_canonical) and _CANON_NUMERIC_RE.fullmatch(
        given
    ):
        try:
            return Decimal(expected_canonical) == Decimal(given)
        except InvalidOperation:
            return False
    if "," in expected_canonical or "," in given:
        return set(expected_canonical.split(",")) == set(given.split(","))
    return expected_canonical == given
