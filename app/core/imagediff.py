"""Tiny, dependency-light visual change detection (Qt-free).

We compare two screenshots with a *difference hash* (dHash): each image is
reduced to a small greyscale grid and turned into a bit-string of horizontal
gradients. The Hamming distance between the two hashes, normalised to ``0..1``,
is a robust measure of "did the page visibly change" that ignores trivial
re-rendering jitter (anti-aliasing, sub-pixel shifts, small brightness drift).

Pillow is imported lazily so the module stays importable without it; when the
library is unavailable the caller treats the pair as "changed".
"""

from __future__ import annotations

import io
from collections.abc import Sequence

_HASH_SIZE = 8  # -> 8*8 = 64 bits


def _dhash_bits(data: bytes, hash_size: int = _HASH_SIZE) -> list[int] | None:
    """Return the 64-bit dHash of ``data`` or ``None`` when Pillow is missing."""
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        return None

    with Image.open(io.BytesIO(data)) as image:
        image = image.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)
        pixels = list(image.getdata())

    width = hash_size + 1
    bits: list[int] = []
    for row in range(hash_size):
        base = row * width
        for col in range(hash_size):
            bits.append(1 if pixels[base + col] > pixels[base + col + 1] else 0)
    return bits


def hamming(a: Sequence[int], b: Sequence[int]) -> int:
    """Count the differing bits between two equal-length bit strings."""
    return sum(1 for x, y in zip(a, b, strict=False) if x != y)


def diff_ratio(previous: bytes, current: bytes, hash_size: int = _HASH_SIZE) -> float | None:
    """Normalised visual difference in ``0..1``; ``None`` when uncomputable.

    ``0`` means the two captures look identical, ``1`` means every gradient bit
    flipped (a completely different page).
    """
    bits_a = _dhash_bits(previous, hash_size)
    bits_b = _dhash_bits(current, hash_size)
    if bits_a is None or bits_b is None:
        return None
    return hamming(bits_a, bits_b) / float(hash_size * hash_size)
