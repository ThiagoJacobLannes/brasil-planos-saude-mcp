"""Small helpers: text normalisation, number parsing, clamping."""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable

USER_AGENT = "brasil-planos-saude-mcp/0.1 (+open-source MCP server; read-only)"


def norm(s: str | None) -> str:
    """Lowercase, accent-free, single-spaced: 'Saúde S/A ' -> 'saude s a'."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def digits(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


def reg(s: str | int | None) -> str:
    """ANS registry number as the 6-digit string the datasets use ('5711' -> '005711')."""
    d = digits(str(s or ""))
    return d.zfill(6) if d else ""


def num(s: str | None) -> float | None:
    """Brazilian number ('1.234,56', '  0,20', 'ND') -> float, or None."""
    s = (s or "").strip()
    if not s or s.upper() in {"ND", "NA", "-"}:
        return None
    s = s.replace(".", "").replace(",", ".") if "," in s else s
    try:
        return float(s)
    except ValueError:
        return None


def clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(n)))


def median(xs: Iterable[float]) -> float | None:
    v = sorted(xs)
    if not v:
        return None
    m = len(v) // 2
    return round(v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2, 2)


def decode(b: bytes) -> str:
    """ANS files mix UTF-8 (with or without BOM) and Windows-1252."""
    b = b.removeprefix(b"\xef\xbb\xbf")
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("cp1252", errors="replace")
