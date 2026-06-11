"""Text analysis utilities ported from TG_Dashboard/server.js."""
import re
from typing import Optional


DEFAULT_POSITIVE = [
    'спасибо большое', 'огромное спасибо', 'очень доволен', 'очень довольна',
    'отличный сервис', 'хороший сервис', 'всё работает', 'все работает',
    'заработало', 'решили проблему', 'решили вопрос', 'помогли',
    'быстро решили', 'оперативно', 'профессионально', 'молодцы',
    'отличная работа', 'хорошая работа', 'рекомендую', '5 звезд',
    'всё отлично', 'все отлично', 'супер', 'отлично', 'прекрасно',
]

DEFAULT_NEGATIVE = [
    'жалоба', 'претензия', 'верните', 'возврат', 'возвращайте',
    'отвратительно', 'кошмар', 'ужасно', 'безобразие', 'безобразно',
    'требую', 'обман', 'обманули', 'обманываете', 'мошенники',
    'никогда больше', 'недопустимо', 'неприемлемо', 'позор',
    'роспотребнадзор', 'скандал', 'наглость', 'деньги обратно',
    'компенсацию', 'это что такое', 'что за сервис', 'плохой сервис',
    'ужасный сервис', 'плохое обслуживание', 'это недопустимо',
]

DEFAULT_PENDING = [
    'проверяю', 'уточняю', 'смотрю', 'посмотрю',
    'сейчас проверю', 'сейчас уточню', 'сейчас посмотрю',
    'подождите', 'подожди', 'одну минуту', 'одну секунду',
    'минуту', 'секунду', 'момент', 'один момент',
    'уже смотрю', 'разбираюсь', 'выясняю',
]

DEFAULT_NO_REPLY = [
    'спасибо', 'спс', 'благодарю', 'ок', 'ok', 'окей', 'okay',
    'понял', 'поняла', 'понятно', 'хорошо', 'ладно', 'отлично',
    'супер', 'отлично', 'принял', 'приняла', 'ясно', 'ага', 'угу',
    '👍', '🙏', '✅', '+', 'ок спасибо', 'хорошо спасибо',
]

# Regex for "word-boundary" matching in Cyrillic + Latin text.
# For purely alphabetic phrases (single word, letters only), we require word
# boundaries so e.g. "ок" doesn't match inside "около".
# For phrases containing spaces/punctuation/digits, falls back to substring.
_WORD_RE = re.compile(r'^[a-zа-яё]+$', re.IGNORECASE)


def phrase_matches(norm: str, phrase: str) -> bool:
    """Check if phrase occurs in norm (already-lowered text).

    For purely-alphabetic phrases, requires word boundaries so short words
    like "ок" don't match inside "около". For phrases with spaces,
    punctuation or digits, falls back to plain substring match.
    """
    p = phrase.lower().strip()
    if not p:
        return False
    if _WORD_RE.match(p):
        # Word-boundary match for alphabetic-only phrases
        escaped = re.escape(p)
        pattern = rf'(?:^|[^a-zа-яё0-9]){escaped}(?:[^a-zа-яё0-9]|$)'
        return bool(re.search(pattern, norm, re.IGNORECASE))
    return p in norm


def is_positive(text: Optional[str], keywords: list[str]) -> bool:
    if not text or not keywords:
        return False
    norm = text.lower()
    return any(phrase_matches(norm, k) for k in keywords)


def is_negative(text: Optional[str], keywords: list[str]) -> bool:
    if not text or not keywords:
        return False
    norm = text.lower()
    # Keyword match with word-boundary awareness
    if any(phrase_matches(norm, k) for k in keywords):
        return True
    # Heuristics: 3+ exclamation marks OR mostly uppercase (>60% of letters)
    if text.count('!') >= 3:
        return True
    letters = re.sub(r'[^a-zA-Zа-яА-Я]', '', text)
    if len(letters) > 5:
        upper = len(re.findall(r'[A-ZА-Я]', letters))
        if upper / len(letters) > 0.6:
            return True
    return False


def is_pending_reply(text: Optional[str], phrases: list[str]) -> bool:
    if not text or not phrases:
        return False
    norm = text.lower().strip()
    return any(phrase_matches(norm, p) for p in phrases)


def is_no_reply(text: Optional[str], phrases: list[str]) -> bool:
    if not text or not phrases:
        return False
    norm = re.sub(r'[!?.,…\s]+$', '', text.lower().strip()).strip()
    return any(p.lower().strip() == norm for p in phrases)


def parse_agent_tag(text: Optional[str]) -> Optional[str]:
    """Extract agent name from [Name] tag prefix."""
    if not text:
        return None
    m = re.match(r'^\[([^\]]{1,30})\]', text)
    return m.group(1) if m else None


def gen_missed_id() -> str:
    """Generate a unique ID for missed events (for hide/unhide feature)."""
    import time
    import random
    import string
    return f"{int(time.time() * 1000)}_{''.join(random.choices(string.ascii_lowercase + string.digits, k=6))}"
