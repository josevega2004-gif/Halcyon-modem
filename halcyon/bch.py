"""Binary BCH codes over GF(2^5), enough for a (31, 16, 3) block code.

The product-code experiment needs an algebraic hard-decision decoder: syndrome
evaluation, Berlekamp-Massey, Chien search, then the corrected word checked
against the syndromes again.  The generator polynomial is built from the
minimal polynomials of alpha^1 .. alpha^(2t), so the code is the textbook
BCH(31, 16) with distance 7 and no table is copied.
"""

from __future__ import annotations

import numpy as np

from .bits import bits_to_int, int_to_bits

# x^5 + x^2 + 1, primitive over GF(2)
PRIMITIVE = 0x25


def _build_tables(primitive: int, m: int) -> tuple[list[int], list[int]]:
    n = (1 << m) - 1
    exp = [0] * (2 * n)
    log = [0] * (1 << m)
    x = 1
    for i in range(n):
        exp[i] = x
        log[x] = i
        x <<= 1
        if x & (1 << m):
            x ^= primitive
    for i in range(n, 2 * n):
        exp[i] = exp[i - n]
    return exp, log


def _gf2_mul(a: int, b: int) -> int:
    out = 0
    while b:
        if b & 1:
            out ^= a
        a <<= 1
        b >>= 1
    return out


def _gf2_mod(a: int, g: int) -> int:
    while a and a.bit_length() >= g.bit_length():
        a ^= g << (a.bit_length() - g.bit_length())
    return a


class BCHCode:
    """A binary BCH code with a primitive narrow-sense construction."""

    def __init__(self, m: int = 5, t: int = 3, primitive: int = PRIMITIVE):
        self.m = int(m)
        self.t = int(t)
        self.n = (1 << self.m) - 1
        self.exp, self.log = _build_tables(primitive, self.m)

        # generator = product of the distinct minimal polynomials of
        # alpha^1 .. alpha^(2t), multiplied over GF(2)
        generator = 1
        seen: set[int] = set()
        for i in range(1, 2 * self.t + 1):
            coset = []
            c = i % self.n
            while c not in coset:
                coset.append(c)
                c = (c * 2) % self.n
            key = min(coset)
            if key in seen:
                continue
            seen.add(key)
            generator = _gf2_mul(generator, self._minimal_polynomial(coset))
        self.g = generator
        self.k = self.n - (generator.bit_length() - 1)
        if self.k <= 0:
            raise ValueError("constraint length and t do not define a code")

    # -------------------------------------------------------------- GF helpers
    def _mul(self, a: int, b: int) -> int:
        if a == 0 or b == 0:
            return 0
        return self.exp[(self.log[a] + self.log[b]) % self.n]

    def _inv(self, a: int) -> int:
        if a == 0:
            raise ZeroDivisionError("zero has no inverse")
        return self.exp[(-self.log[a]) % self.n]

    def _minimal_polynomial(self, coset: list[int]) -> int:
        poly = [1]
        for c in coset:
            root = self.exp[c]
            nxt = [0] * (len(poly) + 1)
            for j, coef in enumerate(poly):
                nxt[j] ^= self._mul(coef, root)
                nxt[j + 1] ^= coef
            poly = nxt
        if any(v not in (0, 1) for v in poly):   # pragma: no cover
            raise ValueError("minimal polynomial did not land in GF(2)")
        out = 0
        for j, v in enumerate(poly):
            if v:
                out |= 1 << j
        return out

    # ------------------------------------------------------------------ encode
    def encode(self, message: np.ndarray) -> np.ndarray:
        msg = np.asarray(message, dtype=np.uint8).ravel()
        if msg.size != self.k:
            raise ValueError(f"expected {self.k} message bits, got {msg.size}")
        value = bits_to_int(msg) << (self.n - self.k)
        return int_to_bits(value ^ _gf2_mod(value, self.g), self.n)

    # ------------------------------------------------------------------ decode
    def decode(self, word: np.ndarray) -> tuple[np.ndarray, bool]:
        bits = np.asarray(word, dtype=np.uint8).ravel()
        if bits.size != self.n:
            raise ValueError(f"expected {self.n} bits, got {bits.size}")
        value = bits_to_int(bits)
        syndromes = [self._evaluate(value, j) for j in range(1, 2 * self.t + 1)]
        if not any(syndromes):
            return bits.copy(), True

        locator = self._berlekamp_massey(syndromes)
        if not locator:
            return bits.copy(), False

        roots = []
        for exponent in range(self.n):
            x = self.exp[(self.n - exponent) % self.n]
            if self._evaluate_poly(locator, x) == 0:
                roots.append(exponent)
        if len(roots) != len(locator) - 1:
            return bits.copy(), False

        fixed = bits.copy()
        for exponent in roots:
            fixed[self.n - 1 - exponent] ^= 1
        check = bits_to_int(fixed)
        if any(self._evaluate(check, j) for j in range(1, 2 * self.t + 1)):
            return bits.copy(), False
        return fixed, True

    def _evaluate(self, value: int, j: int) -> int:
        """value(alpha^j) for a code word held as a bit vector."""
        out = 0
        exponent = 0
        while value:
            if value & 1:
                out ^= self.exp[(j * exponent) % self.n]
            value >>= 1
            exponent += 1
        return out

    def _evaluate_poly(self, poly: list[int], x: int) -> int:
        out = 0
        for coef in reversed(poly):
            out = self._mul(out, x) ^ int(coef)
        return out

    def _berlekamp_massey(self, syndromes: list[int]) -> list[int] | None:
        c = [1]
        b = [1]
        length = 0
        shift = 1
        prev = 1
        for step in range(len(syndromes)):
            discrepancy = syndromes[step]
            for i in range(1, length + 1):
                if i < len(c):
                    discrepancy ^= self._mul(c[i], syndromes[step - i])
            if discrepancy == 0:
                shift += 1
                continue
            coef = self._mul(discrepancy, self._inv(prev))
            old = c[:]
            need = len(b) + shift
            if len(c) < need:
                c += [0] * (need - len(c))
            for j in range(len(b)):
                c[j + shift] ^= self._mul(coef, b[j])
            if 2 * length <= step:
                length = step + 1 - length
                b = old
                prev = discrepancy
                shift = 1
            else:
                shift += 1
        return c[: length + 1]
