"""Search rate-1/2 convolutional generator pairs for maximum free distance.

    PYTHONPATH=. python3 tools/conv_generators.py --constraint 14 --samples 200

A rate-1/2 code with constraint length K is two K-bit generator polynomials;
for K=9 the pair (0o561, 0o753) is the standard NASA code with free distance 12,
and that pair is printed first as a check.  No equally standard pair exists for
K=14, so rather than trust a remembered table this searches candidate pairs and
reports the largest free distance it finds, along with the pair, so the result
can be reproduced.

Free distance is the weight of the lightest non-zero codeword.  By linearity
and time invariance that codeword can be taken to start with an input 1, so it
is the shortest path from the state after one input 1 back to the zero state,
with each edge weighted by the number of 1s it emits.
"""

from __future__ import annotations

import argparse
import heapq
import random


def _popcount(x: int) -> int:
    return bin(x).count("1")


def _poly_mod(a: int, b: int) -> int:
    while a and a.bit_length() >= b.bit_length():
        a ^= b << (a.bit_length() - b.bit_length())
    return a


def _poly_gcd(a: int, b: int) -> int:
    while b:
        a, b = b, _poly_mod(a, b)
    return a


def free_distance(p1: int, p2: int, k: int) -> int:
    """Weight of the lightest non-zero codeword of the rate-1/2 code."""
    mask = (1 << (k - 1)) - 1
    dist = {1 & mask: _popcount(p1 & 1) + _popcount(p2 & 1)}
    heap = [(dist[1 & mask], 1 & mask)]
    while heap:
        d, state = heapq.heappop(heap)
        if d != dist.get(state):
            continue
        if state == 0:
            return d
        for b in (0, 1):
            reg = (state << 1) | b
            nxt = reg & mask
            weight = _popcount(reg & p1) + _popcount(reg & p2)
            nd = d + weight
            if nd < dist.get(nxt, 1 << 30):
                dist[nxt] = nd
                heapq.heappush(heap, (nd, nxt))
    return 1 << 30


def search(k: int, samples: int, seed: int = 1) -> list[tuple[int, int, int]]:
    rng = random.Random(seed)
    high = 1 << (k - 1)
    best: list[tuple[int, int, int]] = []
    for _ in range(samples):
        p1 = rng.randrange(high, 1 << k) | 1
        p2 = rng.randrange(high, 1 << k) | 1
        if p1 == p2 or _poly_gcd(p1, p2) != 1:
            continue          # a shared factor would be catastrophic
        if p1 > p2:
            p1, p2 = p2, p1
        best.append((free_distance(p1, p2, k), p1, p2))
    best.sort(reverse=True)
    # Keep pairs that are not duplicates after taking the best distance first.
    seen: set[tuple[int, int]] = set()
    out = []
    for d, p1, p2 in best:
        if (p1, p2) in seen:
            continue
        seen.add((p1, p2))
        out.append((d, p1, p2))
        if len(out) == 5:
            break
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--constraint", type=int, default=14)
    ap.add_argument("--samples", type=int, default=200)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    if args.constraint == 9:
        print(f"standard K=9 pair: free distance {free_distance(0o561, 0o753, 9)}")
    print(f"K={args.constraint}, {args.samples} candidate pairs")
    for d, p1, p2 in search(args.constraint, args.samples, args.seed):
        print(f"  d_free {d:2d}  polynomials {p1:o} {p2:o}  (0o{p1:o}, 0o{p2:o})")


if __name__ == "__main__":
    main()
