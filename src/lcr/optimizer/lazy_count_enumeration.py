"""Generate refinement placements lazily by CAN-equivalent count groups.

For k labelled members in d domains, M=d**k placements have only
G=binomial(k+d-1,d-1) source-count vectors. All vectors in one count group
have the same (maximum load, total load) sort key. A heap merges each group's
lexicographic permutation iterator, preserving the original global order.
Once the existing CAN screen rejects a count group, do not expand its suffix.

CAN passes, UNKNOWN and TSN failures never remove a group. All actual screens
and TSN attempts remain in their original order. Logical candidate counters
now count emitted vectors, not all omitted rejected permutations. Worst-case
time still expands M placements if all groups pass. Placement storage changes
from O(M*k) to O(G*(k+d)); at fixed d this is O(G*k). At fixed d,k neither
changes the asymptotic N order.
"""
from collections import Counter
from copy import deepcopy
from fractions import Fraction
import heapq
from itertools import combinations_with_replacement
from math import comb
from unittest.mock import patch

from lcr.network_model import model
from . import grouped_can, repair_rank_bounds, repair_refine, search

VERSION = 'lazy-CAN-count-group-enumeration-v1'


def permutations(counts):
    """Distinct lexicographic permutations, O(k) work/state per emitted tuple."""
    values = [source for source,count in enumerate(counts) for _ in range(count)]
    while True:
        yield tuple(values)
        index = len(values)-2
        while index >= 0 and values[index] >= values[index+1]:
            index -= 1
        if index < 0:
            return
        right = len(values)-1
        while values[right] <= values[index]:
            right -= 1
        values[index], values[right] = values[right], values[index]
        values[index+1:] = reversed(values[index+1:])


def placements(residual, contribution, count, rejected, stats):
    """Heap merge in exactly the frozen max-load/total-load/source order."""
    domains = len(residual)
    heap = []
    for sources in combinations_with_replacement(range(domains), count):
        counts = tuple(sources.count(s) for s in range(domains))
        stats['count_vectors_constructed'] += 1
        if rejected(counts):
            stats['groups_excluded_before_expansion'] += 1
            continue
        loads = [r+n*c for r,n,c in zip(residual, counts, contribution)]
        key = (max(loads), sum(loads))
        iterator = permutations(counts)
        first = next(iterator)
        heap.append((key[0], key[1], first, counts, iterator))
    assert stats is not None and len(heap) <= comb(count+domains-1, domains-1)
    heapq.heapify(heap)
    stats['peak_pending_groups'] = max(stats['peak_pending_groups'], len(heap))
    while heap:
        maximum, total, sources, counts, iterator = heapq.heappop(heap)
        # Recheck after a yield: the caller may have learned this group's CAN
        # rejection. It may also have learned it in a previous scheme option.
        if rejected(counts):
            stats['groups_excluded_during_merge'] += 1
            continue
        stats['source_vectors_yielded'] += 1
        yield sources
        if rejected(counts):
            stats['groups_excluded_after_screen'] += 1
            continue
        following = next(iterator, None)
        if following is not None:
            heapq.heappush(heap, (maximum, total, following, counts, iterator))


def cheaper_assignments(ds, choices, bound, count_groups, stats):
    options = []
    for d in ds:
        old_cost = model.cost(d, choices[d['id']]['scheme'])
        for scheme, grades in model.SCHEMES.items():
            saving = old_cost-model.cost(d, scheme)
            if saving > 0:
                options.append((-saving, d['merged_id'], scheme, d, len(grades)))
    for neg_saving, _, scheme, d, count in sorted(options, key=lambda x:x[:3]):
        count_groups.prepare(bound)
        stats['scheme_options'] += 1
        residual = list(bound.loads)
        for source in choices[d['id']]['sources']:
            residual[source] -= Fraction(model.can_ns(d['payload'], d['format'], source), d['period_ns'])
        contribution = [Fraction(model.can_ns(d['payload'], d['format'], source), d['period_ns'])
                        for source in range(4)]
        def rejected(counts):
            return (d['id'], counts) in count_groups.rejections
        for sources in placements(residual, contribution, count, rejected, stats):
            yield search.replace_choice(choices, d['id'], scheme, sources), dict(
                demand=d['id'], before=deepcopy(choices[d['id']]),
                after=dict(scheme=scheme, sources=list(sources)), saving_kEUR=-neg_saving)


class Runner(repair_rank_bounds.Runner):
    def __init__(self, ds, watchdog_s, skip_internal):
        super().__init__(ds, watchdog_s, skip_internal)
        self.enumeration_stats = Counter()

    def refine(self):
        def generate(ds, choices, bound):
            return cheaper_assignments(ds, choices, bound, self.count_groups, self.enumeration_stats)
        with patch.object(repair_refine, 'cheaper_assignments', generate):
            return super().refine()


def run(ds, *, mode='JOINT', watchdog_s=None, skip_internal=True):
    if mode not in ('MRR', 'JOINT'):
        raise ValueError(mode)
    saved = deepcopy(ds)
    with patch.object(model, 'can_analysis', grouped_can.can_analysis):
        runner = Runner(ds, watchdog_s, skip_internal)
        result = runner.execute(mode)
    assert ds == saved
    result.update(can_backend=VERSION, recurrence_reuse=dict(runner.analyzer.stats),
        repair_recurrence_reuse=runner.repair_stats or dict(runner.analyzer.stats),
        refine_recurrence_reuse=runner.refine_stats,
        domain_reuse=dict(runner.reduction_scorer.stats),
        reduction_score_stages=dict(runner.reduction_scorer.stages),
        feasibility_screen=runner.feasibility_stats,
        count_group_pruning=dict(runner.count_groups.stats),
        repair_rank_pruning=dict(runner.rank_stats),
        lazy_enumeration=dict(runner.enumeration_stats),
        evaluation_counter_scope='refinement trial/count-group counters count emitted vectors; '
            'CAN-rejected permutations are not expanded; original actual-screen and TSN order retained; '
            'no inference that worst-case total time is polynomial in variable member count')
    return result
