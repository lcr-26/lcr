"""Isolated candidate-UNKNOWN repair and certified one-demand joint descent.

Original search.py stays frozen. repair() transcribes its decisions, changing
only internal candidate-limit handling. The optional second phase starts from
the actual certified MRR return, not from an unverified low-cost working state.
All assignments of one demand's members are considered; no top-k or step cap.
The grouped binding is process-local: do not run concurrent threads.
"""
from collections import Counter
from copy import deepcopy
from fractions import Fraction
from itertools import product
from time import monotonic
from unittest.mock import patch

from lcr.network_model import model
from . import grouped_can, search
from .protocol import audit

INTERNAL_LIMITS = frozenset(('CAN_busy_iteration_limit', 'CAN_job_limit',
                             'CAN_wait_iteration_limit'))


def cheaper_assignments(ds, choices, bound):
    """Full one-demand, strictly cheaper scheme/deployment neighborhood.

    Largest saving first; placements ordered by projected maximum utilization,
    then total occupation and source tuple. This is an ordering heuristic, not
    a feasibility test. Member slots retain their fixed global priorities.
    """
    options = []
    for d in ds:
        before = choices[d['id']]
        old_cost = model.cost(d, before['scheme'])
        for scheme, grades in model.SCHEMES.items():
            saving = old_cost - model.cost(d, scheme)
            if saving > 0:
                options.append((-saving, d['merged_id'], scheme, d, len(grades)))
    for neg_saving, _, scheme, d, count in sorted(options, key=lambda x: x[:3]):
        residual = list(bound.loads)
        for source in choices[d['id']]['sources']:
            residual[source] -= Fraction(model.can_ns(d['payload'], d['format'], source),
                                         d['period_ns'])
        contribution = [Fraction(model.can_ns(d['payload'], d['format'], source),
                                 d['period_ns']) for source in range(4)]
        placements = []
        for sources in product(range(4), repeat=count):
            loads = residual.copy()
            for source in sources:
                loads[source] += contribution[source]
            placements.append(((max(loads), sum(loads), sources), sources))
        for _, sources in sorted(placements):
            yield search.replace_choice(choices, d['id'], scheme, sources), dict(
                demand=d['id'], before=deepcopy(choices[d['id']]),
                after=dict(scheme=scheme, sources=list(sources)), saving_kEUR=-neg_saving)


class Runner:
    def __init__(self, ds, watchdog_s, skip_internal):
        self.ds = ds
        self.started = monotonic()
        self.watchdog_s = watchdog_s
        self.skip_internal = skip_internal
        self.ev = search.Evaluator(ds, self.guard)
        self.counts, self.times, self.unknown = Counter(), Counter(), Counter()
        self.unknown_examples = {}
        self.moves, self.scans, self.timeline = [], [], []
        self.fallback = None
        self.initial = None
        self.repair_result = None
        self.repair_end_s = None
        self.trial_count = 0
        self.termination = 'unknown'
        self.refinement_rounds = 0

    def guard(self):
        if self.watchdog_s is not None and monotonic() - self.started > self.watchdog_s:
            raise model.AnalysisLimit('external_diagnostic_watchdog')

    def candidate_screen(self, choice, bound, changed, phase, action):
        self.trial_count += 1
        self.counts['screen:' + phase] += 1
        began = monotonic()
        try:
            return self.ev.screen(choice, base=bound, changed=changed)
        except model.AnalysisLimit as exc:
            reason = str(exc)
            if not self.skip_internal or reason not in INTERNAL_LIMITS:
                raise
            label = phase + ':' + reason
            self.unknown[label] += 1
            # Diagnostic samples only; this never restricts evaluated candidates.
            if label not in self.unknown_examples:
                self.unknown_examples[label] = deepcopy(action)
            return None
        finally:
            self.times['screen:' + phase] += monotonic() - began

    def construct(self, bound, aggregate=True, order='latest', phase='repair'):
        self.counts['construct:' + phase] += 1
        began = monotonic()
        try:
            result = self.ev.construct(bound, aggregate, order)
            self.counts['construct_status:' + phase + ':' + result['status']] += 1
            return result
        finally:
            self.times['construct:' + phase] += monotonic() - began

    def publish(self, choices, witness, phase):
        began = monotonic()
        checked = audit(self.ds, choices, witness)
        self.times['audit'] += monotonic() - began
        self.counts['audit'] += 1
        value = dict(choices=deepcopy(choices), witness=witness, audit=checked)
        if self.fallback is None or witness['cost_kEUR'] < self.fallback['witness']['cost_kEUR']:
            self.fallback = value
            self.timeline.append(dict(elapsed_s=monotonic() - self.started,
                                      cost_kEUR=checked['cost_kEUR'], phase=phase))
        return value

    def repair(self, free):
        """Old decisions exactly, except UNKNOWN candidates may be bypassed."""
        base_ch = model.configuration(self.ds, 'D', 'original')
        base_b = self.ev.screen(base_ch)
        if base_b['stage'] != 'CAN_pass':
            self.termination = 'unqualified:' + base_b['stage']
            return
        base_w = self.construct(base_b, phase='baseline')
        if base_w['status'] != 'certified':
            self.termination = 'unqualified:' + base_w['status']
            return
        self.publish(base_ch, base_w, 'baseline')
        choice = model.configuration(self.ds, 'cheapest', 'original')
        # Initial / baseline UNKNOWN is not a candidate and remains a run stop.
        bound = self.ev.screen(choice)
        seen = {search.key(choice)}
        self.initial = dict(cost_kEUR=bound['cost_kEUR'], members=len(bound['members']),
                            stage=bound['stage'], score=search.score_dict(bound['score']))
        while True:
            self.guard()
            failure = None
            if bound['stage'] == 'CAN_pass':
                failure = self.construct(bound)
                if failure['status'] == 'certified':
                    self.publish(choice, failure, 'repair')
                    self.termination = 'first_repaired_feasible'
                    return
            hot = set(bound['hot'])
            if not hot:
                records = (failure or {}).get('failed_frame', {}).get('records', [])
                hot = {bound['members'][k]['source'] for k in records} or set(range(4))
            moved = False
            if free:
                scan = dict(kind='free', generated=0, evaluated=0, exhausted=False, stage=bound['stage'])
                self.scans.append(scan)
                if bound['stage'] == 'CAN_pass':
                    for aggregate, order in ((False, 'latest'), (True, 'ready'), (True, 'slack'),
                                             (False, 'ready'), (False, 'slack')):
                        w = self.construct(bound, aggregate, order)
                        if w['status'] == 'certified':
                            self.publish(choice, w, 'repair')
                            self.moves.append(dict(kind='free_communication', aggregate=aggregate, order=order))
                            self.termination = 'first_repaired_feasible'
                            return
                for trial, action in search.migrations(self.ds, choice, bound, hot):
                    self.guard()
                    scan['generated'] += 1
                    if search.key(trial) in seen:
                        continue
                    scan['evaluated'] += 1
                    b = self.candidate_screen(trial, bound, action['member'].rsplit(':', 1)[0],
                                              'source', action)
                    if b is None:
                        continue
                    accepted = bound['stage'] != 'CAN_pass' and b['score'] < bound['score']
                    if bound['stage'] == 'CAN_pass' and b['stage'] == 'CAN_pass':
                        w = self.construct(b)
                        if w['status'] == 'certified':
                            self.publish(trial, w, 'repair')
                            self.moves.append(dict(kind='free_source', action=action, certified=True))
                            self.termination = 'first_repaired_feasible'
                            return
                    if accepted:
                        self.moves.append(dict(kind='free_source', action=action, cost_kEUR=b['cost_kEUR'],
                            before=search.score_dict(bound['score']), after=search.score_dict(b['score'])))
                        choice, bound = trial, b
                        seen.add(search.key(trial))
                        moved = True
                        break
                else:
                    scan['exhausted'] = True
                if moved:
                    continue
            ranked = []
            unknown_before = sum(self.unknown.values())
            for trial, action, proxy in search.reductions(self.ds, choice, hot):
                self.guard()
                b = self.candidate_screen(trial, bound, action['demand'], 'reduction', action)
                if b is None:
                    continue
                improvement = b['score'] < bound['score']
                component = 0 if bound['score'][0] > 0 else 1
                actual = max(Fraction(), bound['score'][component] - b['score'][component])
                benefit = actual if improvement and actual > 0 else proxy
                delta = action['delta_kEUR']
                rank = (not improvement, benefit <= 0,
                        Fraction(max(0, delta)) / benefit if benefit > 0 else Fraction(max(0, delta)),
                        delta, -benefit, action['demand'], action['after'])
                ranked.append((rank, trial, action, b))
            if not ranked:
                self.termination = ('no_evaluable_reduction_unknown' if sum(self.unknown.values()) > unknown_before
                                    else 'no_smaller_scheme_fallback')
                return
            _, trial, action, b = min(ranked, key=lambda x: x[0])
            self.moves.append(dict(kind='member_reduction', action=action,
                before=search.score_dict(bound['score']), after=search.score_dict(b['score']),
                before_cost_kEUR=bound['cost_kEUR'], after_cost_kEUR=b['cost_kEUR']))
            choice, bound = trial, b
            seen.add(search.key(trial))

    def refine(self):
        """First certified strict cost decrease; exhaustive terminal sweep."""
        choices = deepcopy(self.fallback['choices'])
        bound = self.ev.screen(choices)
        assert bound['stage'] == 'CAN_pass'
        lower = sum(model.cost(d, model.cheapest(d)) for d in self.ds)
        while bound['cost_kEUR'] > lower:
            self.guard()
            self.refinement_rounds += 1
            unknown_before = sum(self.unknown.values())
            for trial, action in cheaper_assignments(self.ds, choices, bound):
                self.guard()
                b = self.candidate_screen(trial, bound, action['demand'], 'refine', action)
                if b is None or b['stage'] != 'CAN_pass':
                    continue
                w = self.construct(b, phase='refine')
                if w['status'] != 'certified':
                    continue
                assert w['cost_kEUR'] < self.fallback['witness']['cost_kEUR']
                self.publish(trial, w, 'refine')
                self.moves.append(dict(kind='joint_refinement', action=action,
                    before_cost_kEUR=bound['cost_kEUR'], after_cost_kEUR=b['cost_kEUR']))
                choices, bound = trial, b
                break
            else:
                self.termination = ('no_certified_joint_improvement_with_unknown'
                    if sum(self.unknown.values()) > unknown_before else 'no_certified_joint_improvement')
                return
        self.termination = 'independent_cost_lower_attained'

    def execute(self, mode):
        try:
            self.repair(free=mode == 'LCR')
            self.repair_end_s = monotonic() - self.started
            self.repair_result = deepcopy(self.fallback)
            repair_termination = self.termination
            if mode == 'JOINT' and self.termination == 'first_repaired_feasible':
                self.refine()
        except model.AnalysisLimit as exc:
            self.termination = str(exc)
            repair_termination = locals().get('repair_termination', self.termination)
        if self.fallback:
            audit(self.ds, self.fallback['choices'], self.fallback['witness'])
        ev = self.ev
        status = 'feasible' if self.fallback else ('unqualified' if self.termination.startswith('unqualified:') else 'unknown')
        return dict(status=status, method=mode, result=self.fallback, initial=self.initial,
            termination=self.termination, repair_termination=repair_termination,
            moves=self.moves, scans=self.scans, elapsed_s=monotonic() - self.started,
            evaluations=ev.screens, bus_checks=ev.bus_checks, bus_cache_hits=ev.cache_hits,
            constructions=ev.constructs, trial_count=self.trial_count, stage_counts=dict(ev.statuses),
            full_screens=ev.full_screens, delta_screens=ev.delta_screens,
            members_materialized=ev.members_materialized, watchdog_s=self.watchdog_s,
            unknown_counts=dict(self.unknown), unknown_examples=self.unknown_examples,
            phase_counts=dict(self.counts), phase_times=dict(self.times), timeline=self.timeline,
            repair_end_s=self.repair_end_s, repair_result=self.repair_result,
            refinement_rounds=self.refinement_rounds, skip_internal=self.skip_internal,
            scope='development; candidate UNKNOWN is not infeasible; no global/local optimum claim')


def run(ds, *, mode='JOINT', watchdog_s=None, skip_internal=True):
    if mode not in ('MRR', 'LCR', 'JOINT'):
        raise ValueError(mode)
    saved = deepcopy(ds)
    with patch.object(model, 'can_analysis', grouped_can.can_analysis):
        result = Runner(ds, watchdog_s, skip_internal).execute(mode)
    assert ds == saved
    result['can_backend'] = 'exact-period-grouping-v1'
    return result
