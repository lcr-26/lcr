"""Exact optimistic rank bounds for fixed-source member reduction.

The global violation score is (capacity, deadline sum). A reduction changes
one domain only. If the candidate is still capacity rejected, its score is
known from loads. Otherwise its deadline sum is at least the sum on unchanged
domains, since every changed-domain contribution is nonnegative. The existing
rank rewards larger reductions of the parent's active violation component;
using this minimum score therefore gives a lexicographic lower rank bound.

No monotonic-response claim, candidate budget or approximate tolerance is
needed. An analysis UNKNOWN supplies no incumbent. A candidate whose lower
rank bound cannot beat an exactly evaluated incumbent cannot win that round.
"""
from collections import Counter
from copy import deepcopy
from fractions import Fraction
from time import monotonic
from unittest.mock import patch

from lcr.network_model import model
from . import grouped_can, refinement_count_groups, search

VERSION = 'repair-exact-rank-bound-pruning-v1'


def reduction_rank(before, score, proxy, action):
    """Exactly the rank used by the frozen repair loop."""
    improvement = score < before
    component = 0 if before[0] > 0 else 1
    actual = max(Fraction(), before[component] - score[component])
    benefit = actual if improvement and actual > 0 else proxy
    delta = action['delta_kEUR']
    return (not improvement, benefit <= 0,
        Fraction(max(0, delta)) / benefit if benefit > 0 else Fraction(max(0, delta)),
        delta, -benefit, action['demand'], action['after'])


def lower_rank(scorer, choices, base, changed, proxy, action):
    """Return a valid lower rank bound, or None outside the local contract."""
    scorer.prepare(base)
    old = [base['members'][f'{changed}:{slot}'] for slot in range(4)
           if f'{changed}:{slot}' in base['members']]
    if not old:
        return None
    source = old[0]['source']
    ch = choices[changed]
    if len(ch['sources']) != len(model.SCHEMES[ch['scheme']]):
        raise ValueError('invalid reduction member count')
    if (any(m['source'] != source for m in old)
            or any(s != source for s in ch['sources'])
            or len(ch['sources']) >= len(old)):
        return None
    loads = list(base.loads)
    loads[source] -= (len(old)-len(ch['sources'])) * Fraction(old[0]['c_ns'], old[0]['period_ns'])
    cap = sum((u for u in loads if u >= 1), Fraction())
    if cap:
        score = (cap, Fraction())
    elif base['score'][0] > 0:
        # Entering the non-capacity stage always improves the active capacity
        # component. Its unknown deadline value cannot change that rank.
        score = (Fraction(), Fraction())
    else:
        unchanged = sum((d['severity'] for s,d in enumerate(scorer.domains)
                         if s != source), Fraction())
        score = (Fraction(), unchanged)
    return reduction_rank(base['score'], score, proxy, action)


class Runner(refinement_count_groups.Runner):
    def __init__(self, ds, watchdog_s, skip_internal):
        super().__init__(ds, watchdog_s, skip_internal)
        self.rank_parent = None
        self.rank_incumbent = None
        self.active_reduction = None
        self.rank_stats = Counter()

    def repair(self, free):
        original = search.reductions
        def track_proxy(*args, **kwargs):
            try:
                for trial, action, proxy in original(*args, **kwargs):
                    self.active_reduction = (trial, action, proxy)
                    yield trial, action, proxy
            finally:
                self.active_reduction = None
        with patch.object(search, 'reductions', track_proxy):
            return super().repair(free)

    def candidate_screen(self, choice, bound, changed, phase, action):
        if phase != 'reduction':
            return super().candidate_screen(choice, bound, changed, phase, action)
        self.guard()
        trial, original_action, proxy = self.active_reduction
        if trial is not choice or original_action is not action or action['demand'] != changed:
            raise AssertionError('reduction proxy must belong to this exact candidate')
        if self.rank_parent is not bound:
            self.rank_parent, self.rank_incumbent = bound, None
            self.rank_stats['rounds'] += 1
        began = monotonic()
        optimistic = lower_rank(self.reduction_scorer, choice, bound, changed, proxy, action)
        self.rank_stats['logical_candidates'] += 1
        prune = (optimistic is not None and self.rank_incumbent is not None
                 and optimistic >= self.rank_incumbent)
        self.times['rank_bound:reduction'] += monotonic()-began
        if prune:
            # The frozen loop ignores None. An exactly evaluated incumbent is
            # already in its ranked list, so this cannot empty the round. No
            # UNKNOWN or infeasibility verdict is attached to a skipped edit.
            self.trial_count += 1
            self.counts['screen:reduction'] += 1
            self.rank_stats['pruned_candidates'] += 1
            self.rank_stats['pruned_from:'+bound['stage']] += 1
            return None
        self.rank_stats['evaluated_candidates'] += 1
        result = super().candidate_screen(choice, bound, changed, phase, action)
        if result is None:
            self.rank_stats['unknown_without_incumbent_update'] += 1
            return None
        exact = reduction_rank(bound['score'], result['score'], proxy, action)
        if optimistic is not None:
            assert optimistic <= exact, 'invalid optimistic reduction rank'
        self.rank_incumbent = min(self.rank_incumbent, exact) if self.rank_incumbent else exact
        return result


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
        evaluation_counter_scope='logical candidate order retained; first phase excludes only edits '
            'whose optimistic rank cannot beat an exactly scored incumbent; second-phase CAN '
            'count groups retained; analyzer cache warmth and actual score/check counts may change')
    return result
