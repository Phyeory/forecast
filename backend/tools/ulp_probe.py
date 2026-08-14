"""Probe whether numba's scalar libm matches CPython's bit-for-bit.

This decides HOW the V2 hot path may be optimised.  Vectorised numpy
transcendentals (np.exp/np.log) and pairwise summation do NOT reproduce
CPython scalar results bit-for-bit, so any "just vectorise it" rewrite would
silently perturb the RBPF weights and invalidate the benchmark history.
A numba scalar loop that keeps the SAME operation order is the safe path --
provided numba's libm agrees with CPython's, which is what this measures.
"""
import math

import numpy as np
from numba import njit


@njit(cache=True)
def k_log(w, out):
    for i in range(w.shape[0]):
        wi = w[i]
        if wi < 1e-300:
            wi = 1e-300
        out[i] = math.log(wi)


@njit(cache=True)
def k_exp(x, out):
    for i in range(x.shape[0]):
        out[i] = math.exp(x[i])


@njit(cache=True)
def k_seqsum(w):
    s = 0.0
    for i in range(w.shape[0]):
        s += w[i]
    return s


@njit(cache=True)
def k_sqrt(x, out):
    for i in range(x.shape[0]):
        out[i] = math.sqrt(x[i])


def main():
    rng = np.random.default_rng(0)
    n = 200
    trials = 5000
    bad_log = bad_exp = bad_sum = bad_sqrt = 0

    for _ in range(trials):
        lw = rng.normal(0, 8, n)
        lw -= lw.max()
        w = np.exp(lw)
        w /= w.sum()
        w = np.maximum(w, 1e-300)

        out = np.empty(n)
        k_log(w, out)
        ref = np.array([math.log(max(float(x), 1e-300)) for x in w])
        bad_log += int((out.view("int64") != ref.view("int64")).sum())

        out2 = np.empty(n)
        k_exp(lw, out2)
        ref2 = np.array([math.exp(float(x)) for x in lw])
        bad_exp += int((out2.view("int64") != ref2.view("int64")).sum())

        pys = 0.0
        for i in range(n):
            pys += float(w[i])
        if np.float64(k_seqsum(w)).view("int64") != np.float64(pys).view("int64"):
            bad_sum += 1

        av = np.abs(lw)
        out3 = np.empty(n)
        k_sqrt(av, out3)
        ref3 = np.array([math.sqrt(float(x)) for x in av])
        bad_sqrt += int((out3.view("int64") != ref3.view("int64")).sum())

    tot = trials * n
    print(f"numba math.log  vs CPython: {bad_log:>8} / {tot} mismatches")
    print(f"numba math.exp  vs CPython: {bad_exp:>8} / {tot} mismatches")
    print(f"numba math.sqrt vs CPython: {bad_sqrt:>8} / {tot} mismatches")
    print(f"numba seq-sum   vs CPython: {bad_sum:>8} / {trials} mismatches")
    ok = (bad_log == 0 and bad_exp == 0 and bad_sqrt == 0 and bad_sum == 0)
    print("\nVERDICT:", "numba scalar loops ARE bit-identical -> safe to port"
          if ok else "numba differs -> must keep CPython scalar order")


if __name__ == "__main__":
    main()
