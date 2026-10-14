"""Split section text into overlapping chunks, preferring paragraph and sentence boundaries."""

SEPARATORS = ["\n\n", "\n", ". ", "; ", ", ", " "]


def split_text(text: str, size: int, overlap: int) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    return _merge(_split(text, size, SEPARATORS), size, overlap)


def _split(text: str, size: int, separators: list[str]) -> list[str]:
    """Break text into pieces no longer than `size`, cutting at the coarsest separator possible."""
    if len(text) <= size:
        return [text]
    if not separators:
        return [text[i : i + size] for i in range(0, len(text), size)]

    sep, finer = separators[0], separators[1:]
    parts = text.split(sep)
    if len(parts) == 1:
        return _split(text, size, finer)

    pieces = []
    for i, part in enumerate(parts):
        piece = part + sep if i < len(parts) - 1 else part
        if len(piece) <= size:
            pieces.append(piece)
        else:
            pieces.extend(_split(piece, size, finer))
    return pieces


def _merge(pieces: list[str], size: int, overlap: int) -> list[str]:
    """Greedily pack pieces into chunks; each new chunk starts with the tail of the previous one."""
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if current.strip() and len(current) + len(piece) > size:
            chunks.append(current.strip())
            current = _tail(current, overlap)
        current += piece
    if current.strip():
        chunks.append(current.strip())
    return chunks


def _tail(text: str, overlap: int) -> str:
    if overlap <= 0:
        return ""
    tail = text[-overlap:]
    # Start the overlap on a word boundary so chunks don't open mid-word.
    space = tail.find(" ")
    return tail[space + 1 :] if 0 <= space < len(tail) - 1 else tail
