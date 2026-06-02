import re

# Schwelle (Zeichen), ab der ein Transkript für die LLM-Korrektur in Abschnitte
# zerlegt wird (Plan §7). Lokale LLMs haben begrenzten Kontext; lange Diktate
# würden das Fenster sprengen oder am Ende an Qualität verlieren.
DEFAULT_CHUNK_THRESHOLD = 6000


def split_into_chunks(text: str, max_chars: int = DEFAULT_CHUNK_THRESHOLD) -> list[str]:
    """Zerlegt langen Text in Abschnitte <= max_chars für die Korrektur.

    Schnitte erfolgen bevorzugt an Absatz-, dann an Satzgrenzen, niemals
    mitten im Wort. Kurzer Text (<= max_chars) wird unverändert als eine
    einzige Einheit zurückgegeben.
    """
    text = text.strip()
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    buf = ""  # akkumuliert ganze Absätze, intern durch Leerzeile getrennt
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) > max_chars:
            # Zu langer Absatz: erst den Puffer abschließen, dann satzweise splitten.
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.extend(_sentence_chunks(para, max_chars))
            continue
        if not buf:
            buf = para
        elif len(buf) + 2 + len(para) <= max_chars:
            buf += "\n\n" + para
        else:
            chunks.append(buf)
            buf = para
    if buf:
        chunks.append(buf)
    return chunks


def _sentence_chunks(paragraph: str, max_chars: int) -> list[str]:
    """Zerlegt einen überlangen Absatz an Satzgrenzen (intern durch ' ' getrennt)."""
    chunks: list[str] = []
    buf = ""
    for sentence in re.split(r"(?<=[.!?…])\s+", paragraph):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) > max_chars:
            # Einzelner überlanger "Satz" (z. B. ohne Satzzeichen): an Wortgrenzen.
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.extend(_word_chunks(sentence, max_chars))
            continue
        if not buf:
            buf = sentence
        elif len(buf) + 1 + len(sentence) <= max_chars:
            buf += " " + sentence
        else:
            chunks.append(buf)
            buf = sentence
    if buf:
        chunks.append(buf)
    return chunks


def _word_chunks(sentence: str, max_chars: int) -> list[str]:
    """Letzter Ausweg: an Wortgrenzen splitten (nie mitten im Wort)."""
    chunks: list[str] = []
    buf = ""
    for word in sentence.split():
        if not buf:
            buf = word
        elif len(buf) + 1 + len(word) <= max_chars:
            buf += " " + word
        else:
            chunks.append(buf)
            buf = word
    if buf:
        chunks.append(buf)
    return chunks
