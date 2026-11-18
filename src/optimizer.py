"""Vote allocation maths: the chunked marginal allocator and the continuous
Lagrange solver used by the weekly procedure.

A legacy ``VoteOptimizer`` class (greedy and SLSQP "quadratic" strategies) was
removed on 2026-11-18. Nothing outside this module referenced it, and it was the
only code that pulled in numpy and scipy; the functions below use the standard
library only. The wired-in entry points are ``solve_marginal_allocation`` (the
auto-voter and the pre-boundary review) and ``solve_alloc_for_set`` (the weekly
allocation review).
"""

import math
from dataclasses import dataclass
from typing import List, Tuple


# ---------------------------------------------------------------------------
# Canonical module-level voting math — single source of truth
# ---------------------------------------------------------------------------


def expected_return_usd(
    total_usd: float, base_votes: float, your_votes: float
) -> float:
    if your_votes <= 0:
        return 0.0
    denom = float(base_votes) + float(your_votes)
    if denom <= 0:
        return 0.0
    return float(total_usd) * (float(your_votes) / denom)


def marginal_gain_usd(
    total_usd: float, base_votes: float, current_votes: float, delta_votes: float
) -> float:
    """Expected return gain from adding delta votes at current allocation level."""
    if delta_votes <= 0:
        return 0.0
    current = expected_return_usd(total_usd, base_votes, current_votes)
    after = expected_return_usd(total_usd, base_votes, current_votes + delta_votes)
    return max(0.0, after - current)


def marginal_loss_usd(
    total_usd: float, base_votes: float, current_votes: float, delta_votes: float
) -> float:
    """Expected return loss from removing delta votes at current allocation level."""
    if delta_votes <= 0 or current_votes <= 0:
        return 0.0
    before = expected_return_usd(total_usd, base_votes, current_votes)
    after = expected_return_usd(
        total_usd, base_votes, max(0.0, current_votes - delta_votes)
    )
    return max(0.0, before - after)


@dataclass
class GaugeBoundaryState:
    gauge: str
    pool: str
    votes_raw: float
    total_usd: float


def solve_marginal_allocation(
    states: List[Tuple[str, str, float, float]],
    total_votes: int,
    min_per_pool: int,
    max_selected_pools: int,
    chunk_size: int = 1000,
) -> List[int]:
    """Discrete marginal allocator using vote chunks with dynamic pool entry/swap.

    - Seeds top pools with minimum allocation floor.
    - Allocates remaining votes in chunked marginal-return steps.
    - Allows inactive candidates to replace active pools when beneficial.
    - Uses exact budget by assigning final remainder to best active candidate.
    """
    n = len(states)
    if n == 0:
        return []

    total_votes_i = int(total_votes)
    if total_votes_i <= 0:
        return [0] * n

    max_selected = max(1, min(int(max_selected_pools), n))
    step = max(1, int(chunk_size))
    min_per_pool_i = int(max(0, min_per_pool))
    if max_selected * min_per_pool_i > total_votes_i:
        raise ValueError("Infeasible allocation: k * min_per_pool exceeds voting power")

    rewards = [max(float(s[3]), 0.0) for s in states]
    base_votes_list = [max(float(s[2]), 0.0) for s in states]

    allocations = [0] * n
    floors = [0] * n

    seed_count = min(max_selected, n)
    for idx in range(seed_count):
        floors[idx] = min_per_pool_i
        allocations[idx] = min_per_pool_i

    used = sum(allocations)
    if used > total_votes_i:
        raise ValueError("Infeasible seeded allocation")

    remaining = total_votes_i - used

    def active_indices() -> List[int]:
        return [idx for idx, votes in enumerate(allocations) if votes > 0]

    def best_add_candidate(delta_votes: int) -> Tuple[int, float]:
        best_idx = -1
        best_gain = -1.0
        active = set(active_indices())
        active_count = len(active)
        for idx in range(n):
            if idx not in active and active_count >= max_selected:
                continue
            gain = marginal_gain_usd(
                rewards[idx],
                base_votes_list[idx],
                float(allocations[idx]),
                float(delta_votes),
            )
            if gain > best_gain:
                best_gain = gain
                best_idx = idx
        return best_idx, max(0.0, best_gain)

    def best_active_add(delta_votes: int) -> Tuple[int, float]:
        best_idx = -1
        best_gain = -1.0
        for idx in active_indices():
            gain = marginal_gain_usd(
                rewards[idx],
                base_votes_list[idx],
                float(allocations[idx]),
                float(delta_votes),
            )
            if gain > best_gain:
                best_gain = gain
                best_idx = idx
        return best_idx, max(0.0, best_gain)

    def worst_removable_active(delta_votes: int) -> Tuple[int, float]:
        worst_idx = -1
        worst_loss = float("inf")
        for idx in active_indices():
            if allocations[idx] - floors[idx] < delta_votes:
                continue
            loss = marginal_loss_usd(
                rewards[idx],
                base_votes_list[idx],
                float(allocations[idx]),
                float(delta_votes),
            )
            if loss < worst_loss:
                worst_loss = loss
                worst_idx = idx
        if worst_idx < 0:
            return -1, 0.0
        return worst_idx, max(0.0, worst_loss)

    while remaining >= step:
        active = set(active_indices())
        active_count = len(active)
        candidate_idx, candidate_gain = best_add_candidate(step)
        if candidate_idx < 0:
            break

        if candidate_idx in active or active_count < max_selected:
            allocations[candidate_idx] += step
            remaining -= step
            continue

        removable_idx, removable_loss = worst_removable_active(step)
        if removable_idx >= 0 and candidate_gain > removable_loss:
            allocations[removable_idx] -= step
            allocations[candidate_idx] += step
            continue

        fallback_idx, _fallback_gain = best_active_add(step)
        if fallback_idx < 0:
            break
        allocations[fallback_idx] += step
        remaining -= step

    if remaining > 0:
        active = active_indices()
        if len(active) < max_selected:
            idx, _gain = best_add_candidate(remaining)
        else:
            idx, _gain = best_active_add(remaining)
        if idx < 0:
            idx = 0
        allocations[idx] += remaining
        remaining = 0

    total_alloc = sum(allocations)
    if total_alloc != total_votes_i:
        drift = total_votes_i - total_alloc
        target_idx = active_indices()[0] if active_indices() else 0
        allocations[target_idx] += drift

    return [int(v) for v in allocations]


def solve_alloc_for_set(
    states: List[GaugeBoundaryState], total_votes: int, min_per_pool: int
) -> List[float]:
    """Solve optimal allocation for K pools using Lagrange multiplier method."""
    k = len(states)
    if k * min_per_pool > total_votes:
        raise ValueError("Infeasible: k * min_per_pool > voting power")

    floors = [float(min_per_pool)] * k
    if k == 0:
        return []

    remaining = float(total_votes - k * min_per_pool)
    if remaining <= 0:
        return floors

    B = [max(s.total_usd, 0.0) for s in states]
    V = [max(float(s.votes_raw), 0.0) for s in states]

    def alloc_for_lambda(lmbd: float) -> List[float]:
        out = []
        for i in range(k):
            if B[i] <= 0 or V[i] <= 0:
                out.append(floors[i])
                continue
            x = math.sqrt((B[i] * V[i]) / lmbd) - V[i]
            out.append(max(x, floors[i]))
        return out

    lo = 1e-18
    hi = 1.0
    for _ in range(120):
        if sum(alloc_for_lambda(hi)) <= total_votes:
            break
        hi *= 2.0

    for _ in range(160):
        mid = (lo + hi) / 2.0
        if sum(alloc_for_lambda(mid)) > total_votes:
            lo = mid
        else:
            hi = mid

    alloc = alloc_for_lambda(hi)
    s = sum(alloc)
    if s <= 0:
        return floors

    if abs(s - total_votes) > 1e-8:
        scale = total_votes / s
        alloc = [max(f, a * scale) for a, f in zip(alloc, floors)]

    return alloc
