"""Systematic LDPC codes with a staircase (dual-diagonal) parity part.

Structure
---------
``H = [A | D]`` where ``D`` is the dual-diagonal matrix (ones on the main
diagonal and the super-diagonal, last column having a single one).  That makes
``D`` invertible by construction, so every block length / rate is encodable
without Gaussian elimination, and encoding reduces to

    s = A u        (over GF(2))
    p[i] = XOR of s[i:]

which is a suffix XOR.  This is the same idea as the 802.11n / 5G NR base
graphs: no singular-parity-matrix retries, no per-code construction cost worth
worrying about, linear-time encoding.

``A`` is grown column by column with a progressive-edge-growth style rule:
always attach to the currently least-loaded check nodes and reject edges that
would close a 4-cycle.  That gives irregular codes with good thresholds for
short blocks (500-3000 bits) while staying cheap to build.

Decoding is log-domain sum-product with a syndrome-based early exit.
"""

from __future__ import annotations

import heapq
import itertools

import numpy as np

_CODES: dict[tuple[int, int, int], "LDPCCode"] = {}


def _build_A(k: int, m: int, dv: int, seed: int) -> np.ndarray:
    """Build the m x k information part, balancing check degrees, no 4-cycles."""
    rng = np.random.default_rng(seed)
    deg = np.zeros(m, np.int32)
    neigh: list[set[int]] = [set() for _ in range(m)]
    # (degree, random tie-break, check index): the random key is what keeps the
    # graph from degenerating into a banded, block-diagonal structure.
    heap = [(0, float(rng.random()), i) for i in range(m)]
    heapq.heapify(heap)
    A = np.zeros((m, k), np.uint8)

    seen = np.zeros(m, dtype=bool)
    for c in range(k):
        chosen: list[int] = []
        for _ in range(dv):
            popped: list[int] = []
            pick = -1
            # Walk candidate checks in order of increasing current degree and
            # take the first one that closes no 4-cycle (PEG rule).
            budget = 96
            while heap and pick < 0 and budget > 0:
                budget -= 1
                d, _key, r = heapq.heappop(heap)
                if d != deg[r]:  # stale entry
                    continue
                popped.append(r)
                if r in chosen:
                    continue
                # A check adjacent to an already-chosen one would close a
                # 4-cycle through that shared staircase parity bit.
                ok = True
                for r2 in chosen:
                    if abs(r - r2) == 1 or (neigh[r] & neigh[r2]):
                        ok = False
                        break
                if ok:
                    pick = r
                    break
            for r in popped:
                if r != pick:
                    heapq.heappush(heap, (deg[r], float(rng.random()), r))
            if pick < 0:
                # Over budget: take the least loaded unused candidate even if
                # it closes a 4-cycle (rare, and bounded on purpose).
                cands = [r for r in popped if r not in chosen]
                if cands:
                    pick = min(cands, key=lambda r: deg[r])
                else:
                    free = np.nonzero(~seen)[0]
                    if free.size == 0:  # pragma: no cover
                        break
                    pick = int(free[np.argmin(deg[free])])
                    seen[pick] = True
            chosen.append(pick)
            A[pick, c] = 1
            neigh[pick].add(c)
            deg[pick] += 1
            heapq.heappush(heap, (deg[pick], float(rng.random()), pick))
    return A


class LDPCCode:
    """A systematic (n, k) LDPC code with staircase parity bits."""

    def __init__(self, n: int, k: int, dv: int = 3, seed: int = 0):
        if not 0 < k < n:
            raise ValueError("need 0 < k < n")
        self.n = int(n)
        self.k = int(k)
        self.m = self.n - self.k
        self.A = _build_A(self.k, self.m, dv, seed)
        H = np.zeros((self.m, self.n), np.uint8)
        H[:, : self.k] = self.A
        idx = np.arange(self.m)
        H[idx, self.k + idx] = 1
        if self.m > 1:
            H[idx[:-1], self.k + idx[:-1] + 1] = 1
        self.H = H
        self.Af = self.A.astype(np.float32)

        rows, cols = np.nonzero(H)
        self.rows = rows.astype(np.int64)
        self.cols = cols.astype(np.int64)
        self.E = rows.size
        deg = np.bincount(rows, minlength=self.m)
        if deg.min() == 0:  # pragma: no cover - construction guarantees this
            raise RuntimeError("empty check node")
        self.offsets = np.concatenate(([0], np.cumsum(deg)[:-1])).astype(np.int64)
        self.seg = np.repeat(np.arange(self.m), deg)
        self.max_deg = int(deg.max())

    # ------------------------------------------------------------------ encode
    def encode(self, info: np.ndarray) -> np.ndarray:
        """Encode one or more info blocks of shape (..., k) -> (..., n)."""
        u = np.atleast_2d(np.asarray(info, dtype=np.uint8))
        if u.shape[1] != self.k:
            raise ValueError(f"expected {self.k} info bits, got {u.shape[1]}")
        s = (u.astype(np.float32) @ self.Af.T).astype(np.int64) & 1
        parity = np.bitwise_xor.accumulate(s[:, ::-1], axis=1)[:, ::-1]
        return np.concatenate([u, parity.astype(np.uint8)], axis=1)

    # ------------------------------------------------------------------ decode
    def decode(
        self, llr: np.ndarray, max_iter: int = 50, stop_on_valid: bool = True
    ) -> tuple[np.ndarray, bool, int]:
        """Sum-product decode. ``llr`` > 0 means bit 0.

        Returns (hard decisions (n,), converged, iterations_used).
        """
        llr = np.asarray(llr, dtype=np.float64).ravel()
        if llr.size != self.n:
            raise ValueError(f"expected {self.n} LLRs, got {llr.size}")
        msg = np.zeros(self.E)
        post = llr.copy()
        for it in range(1, max_iter + 1):
            var2chk = post[self.cols] - msg
            # ---- check node update (tanh product rule, vectorised per segment)
            clipped = np.clip(var2chk, -60.0, 60.0)
            logabs = np.log(np.clip(np.abs(np.tanh(clipped * 0.5)), 1e-12, 1.0))
            seg_sum = np.add.reduceat(logabs, self.offsets)
            excl = seg_sum[self.seg] - logabs
            neg = (clipped < 0).astype(np.int64)
            seg_neg = np.add.reduceat(neg, self.offsets)
            sign = 1.0 - 2.0 * ((seg_neg[self.seg] & 1) ^ neg)
            mag = np.exp(np.minimum(excl, 0.0))
            msg = sign * 2.0 * np.arctanh(np.minimum(mag, 1.0 - 1e-12))
            post = llr + np.bincount(self.cols, weights=msg, minlength=self.n)
            bits = (post < 0).astype(np.uint8)
            if stop_on_valid and self.syndrome_ok(bits):
                return bits, True, it
        return (post < 0).astype(np.uint8), False, max_iter

    def decode_osd(
        self,
        llr: np.ndarray,
        max_iter: int = 50,
        window: int = 10,
        max_flips: int = 2,
        check=None,
    ) -> tuple[np.ndarray, bool, int]:
        """Sum-product, then ordered-statistics reprocessing if it fails.

        Ordering the bits by reliability and re-encoding from the k most
        reliable columns is the order-1 OSD solution; flipping a few of the
        least reliable information bits and re-encoding gives order-2.  It
        costs a GF(2) elimination per block and only runs when sum-product
        gave up, which is where the short-block floors live.

        ``check`` accepts a candidate codeword (the caller's CRC test).
        """
        bits, converged, iters = self.decode(llr, max_iter=max_iter)
        if converged and (check is None or check(bits)):
            return bits, True, iters

        llr = np.asarray(llr, dtype=np.float64).ravel()
        order = np.argsort(-np.abs(llr), kind="stable")
        k = self.k
        A = self.H[:, order[:k]]
        B = self.H[:, order[k:]]
        inv_b = _gf2_inverse(B)
        if inv_b is None:
            return bits, False, iters
        G = (inv_b.astype(np.uint16) @ A.astype(np.uint16)) & 1

        def candidate(u: np.ndarray) -> np.ndarray:
            parity = ((G @ u.astype(np.uint16)) & 1).astype(np.uint8)
            cw = np.zeros(self.n, dtype=np.uint8)
            cw[order[:k]] = u
            cw[order[k:]] = parity
            return cw

        def accept(cw: np.ndarray) -> bool:
            return self.syndrome_ok(cw) and (check is None or check(cw))

        base = (llr[order[:k]] < 0).astype(np.uint8)
        cw = candidate(base)
        if accept(cw):
            return cw, True, iters

        pool = np.arange(max(0, k - window), k)
        for flips in range(1, max_flips + 1):
            for combo in itertools.combinations(pool, flips):
                u = base.copy()
                u[list(combo)] ^= 1
                cw = candidate(u)
                if accept(cw):
                    return cw, True, iters
        return bits, False, iters

    def syndrome_ok(self, bits: np.ndarray) -> bool:
        b = np.asarray(bits, dtype=np.uint8).ravel()
        if b.size != self.n:
            return False
        return not np.any(
            np.add.reduceat(b[self.cols].astype(np.int64), self.offsets) & 1
        )


def _gf2_inverse(matrix: np.ndarray) -> np.ndarray | None:
    """Invert a square GF(2) matrix, or return None if it is singular."""
    m = matrix.shape[0]
    aug = np.concatenate([matrix.astype(np.uint8) & 1, np.eye(m, dtype=np.uint8)], axis=1)
    for col in range(m):
        rows = np.nonzero(aug[col:, col])[0]
        if rows.size == 0:
            return None
        pivot = col + int(rows[0])
        if pivot != col:
            aug[[col, pivot]] = aug[[pivot, col]]
        row = aug[col].copy()
        mask = aug[:, col].astype(bool)
        mask[col] = False
        aug[mask] ^= row
    return aug[:, m:]


def code_for(n: int, k: int, dv: int | None = None) -> LDPCCode:
    """Cached code construction; encoding/decoding reuse the same matrices.

    Low-rate codes need a heavier information column weight, otherwise the
    degree-2 parity chain dominates the graph and the threshold sags.
    """
    rate = k / n
    if dv is None:
        # Every check needs at least one *information* edge.  A check fed only by
        # the staircase parity columns codes nothing -- it merely forces adjacent
        # parity bits to be equal, which collapses the code's effective rate and
        # leaves a block-error floor that does not move with S/N.
        #
        # That is exactly what a fixed dv = 6 produced at rate 1/8, where there are
        # ~7 checks per information bit: it is the cause of the 0.25-0.33 floors
        # measured on the narrow low-rate modes, not a property of the modulation.
        # So scale the column weight with the check-to-information ratio.
        checks_per_info = (n - k) / max(k, 1)
        dv = max(3, int(np.ceil(checks_per_info)) + 1)
    key = (int(n), int(k), int(dv))
    code = _CODES.get(key)
    if code is None:
        code = LDPCCode(n, k, dv=dv, seed=(n * 7919 + k * 104729) & 0xFFFF)
        _CODES[key] = code
    return code
