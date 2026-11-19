"""Structural-complexity pilot: local reduction agendas and one cost sweep.

This CHANGES the search rule. It is not an equivalent acceleration of LCR.
Repair ranks edits by saved CAN utilization, without screening every alternative.
Refinement visits each initially cheaper (demand, scheme) once, without restarting
after acceptance. A rejected option can become feasible later and be missed.
All accepted cost decreases still need the unchanged CAN and TSN certificates.
No candidate-count, round-count, or elapsed-time tuning parameter is introduced.
"""
from collections import Counter
from copy import deepcopy
from fractions import Fraction
import heapq
from time import monotonic
from unittest.mock import patch

from lcr.network_model import model
from . import grouped_can, lazy_count_enumeration as lazy, refinement_early_exit as early
from . import repair_refine, search

VERSION = 'reduction-agenda-single-cost-sweep-v3-no-baseline'


def cost_options(ds, choices):
    """One immutable option order, tied by the existing demand/scheme order."""
    options = []
    for d in ds:
        old = model.cost(d, choices[d['id']]['scheme'])
        for scheme in model.SCHEMES:
            saving = old - model.cost(d, scheme)
            if saving > 0:
                options.append((-saving, d['merged_id'], scheme, d))
    return sorted(options, key=lambda x: x[:3])


class Agenda:
    """Only an edited demand invalidates its own reduction agenda entries."""
    def __init__(self, ds, choices):
        self.queues = [[] for _ in range(4)]
        self.versions = {d['id']: 0 for d in ds}
        self.stats = Counter()
        for d in ds:
            self.add(d, choices[d['id']])

    def add(self, d, before):
        sources = before['sources']
        if len(set(sources)) != 1:
            raise ValueError('repair agendas require a fixed single source per demand')
        source, count = sources[0], len(sources)
        best = {}
        for scheme, grades in model.SCHEMES.items():
            target = len(grades)
            if target >= count or model.cost(d, scheme) > model.cost(d, 'D'):
                continue
            if target not in best or (model.cost(d, scheme), scheme) < (model.cost(d, best[target]), best[target]):
                best[target] = scheme
        for target, scheme in best.items():
            delta = model.cost(d, scheme) - model.cost(d, before['scheme'])
            relief = Fraction((count-target)*model.can_ns(d['payload'], d['format'], source), d['period_ns'])
            rank = (Fraction(max(0, delta))/relief, delta, d['merged_id'], scheme)
            heapq.heappush(self.queues[source], (rank, d['id'], self.versions[d['id']], scheme))
            self.stats['enqueued'] += 1

    def pop(self, hot):
        for queue in self.queues:
            while queue and queue[0][2] != self.versions[queue[0][1]]:
                heapq.heappop(queue)
                self.stats['stale_discarded'] += 1
        active = [s for s in sorted(hot) if self.queues[s]]
        if not active:
            active = [s for s, queue in enumerate(self.queues) if queue]
        if not active:
            return None
        source = min(active, key=lambda s: self.queues[s][0])
        _, demand, _, scheme = heapq.heappop(self.queues[source])
        self.stats['selected'] += 1
        return demand, scheme

    def advance(self, d, choice):
        self.versions[d['id']] += 1
        self.add(d, choice)


class Runner(lazy.Runner):
    def __init__(self, ds, watchdog_s, skip_internal, *, queue_repair=True, one_sweep=True):
        super().__init__(ds, watchdog_s, skip_internal)
        self.queue_repair, self.one_sweep = queue_repair, one_sweep
        self.structure_stats = Counter()

    def repair(self, free):
        if not self.queue_repair:
            return super().repair(free)
        if free:
            raise ValueError('fixed-source agenda repair only')
        with patch.object(model, 'can_analysis', self.analyzer):
            return self._agenda_repair()

    def _agenda_repair(self):
        choices = model.configuration(self.ds, 'cheapest', 'original')
        bound = self.ev.screen(choices)
        self.initial = dict(cost_kEUR=bound['cost_kEUR'], members=len(bound['members']),
                            stage=bound['stage'], score=search.score_dict(bound['score']))
        agenda = Agenda(self.ds, choices)
        lookup = {d['id']: d for d in self.ds}
        try:
            while True:
                self.guard()
                failed = None
                if bound['stage'] == 'CAN_pass':
                    failed = self.construct(bound)
                    if failed['status'] == 'certified':
                        self.publish(choices, failed, 'repair')
                        self.termination = 'first_repaired_feasible'
                        return
                hot = set(bound['hot'])
                if not hot:
                    records = (failed or {}).get('failed_frame', {}).get('records', [])
                    hot = {bound['members'][k]['source'] for k in records} or set(range(4))
                selected = agenda.pop(hot)
                if selected is None:
                    self.termination = 'reduction_agendas_exhausted_no_certificate'
                    return
                demand, scheme = selected
                before = choices[demand]
                sources = before['sources'][:len(model.SCHEMES[scheme])]
                trial = search.replace_choice(choices, demand, scheme, sources)
                action = dict(demand=demand, before=before['scheme'], after=scheme,
                              delta_kEUR=model.cost(lookup[demand], scheme)-model.cost(lookup[demand], before['scheme']))
                # Bypass the reference reduction-score rank; this is one chosen
                # working edit, not an exact score for all competing edits.
                result = repair_refine.Runner.candidate_screen(
                    self, trial, bound, demand, 'queue_reduction', action)
                if result is None:
                    self.structure_stats['repair_unknown_skipped'] += 1
                    continue
                assert len(result['members']) < len(bound['members'])
                self.moves.append(dict(kind='agenda_member_reduction', action=action,
                    before_cost_kEUR=bound['cost_kEUR'], after_cost_kEUR=result['cost_kEUR']))
                choices, bound = trial, result
                self.structure_stats['repair_commits'] += 1
                agenda.advance(lookup[demand], choices[demand])
        finally:
            self.structure_stats.update({'agenda_' + k: v for k, v in agenda.stats.items()})

    def refine(self):
        if not self.one_sweep:
            return super().refine()
        self.repair_stats = dict(self.analyzer.stats)
        before = self.analyzer.stats.copy()
        self.analyzer = early.Analyzer(self.analyzer)
        checker = early.FeasibilityScreen(self.ev, self.analyzer)
        try:
            with patch.object(model, 'can_analysis', self.analyzer), patch.object(self.ev, 'screen', checker):
                return self._cost_sweep()
        finally:
            self.refine_stats = {k: self.analyzer.stats[k]-before[k]
                                 for k in self.analyzer.stats if k != 'peak_stored_members'}
            self.feasibility_stats = dict(checker.stats, rejection_cache_entries=len(checker.rejections),
                                          kernel=dict(self.analyzer.early_stats))

    def _cost_sweep(self):
        choices = deepcopy(self.fallback['choices'])
        bound = self.ev.screen(choices)
        assert bound['stage'] == 'CAN_pass'
        options = cost_options(self.ds, choices)
        self.structure_stats['refine_options_registered'] = len(options)
        self.refinement_rounds = 1 if options else 0
        lower = sum(model.cost(d, model.cheapest(d)) for d in self.ds)
        for _, _, scheme, d in options:
            self.guard()
            saving = model.cost(d, choices[d['id']]['scheme']) - model.cost(d, scheme)
            if saving <= 0:
                self.structure_stats['refine_options_no_longer_cheaper'] += 1
                continue
            self.structure_stats['refine_options_visited'] += 1
            self.count_groups.prepare(bound)
            residual = list(bound.loads)
            for source in choices[d['id']]['sources']:
                residual[source] -= Fraction(model.can_ns(d['payload'], d['format'], source), d['period_ns'])
            contribution = [Fraction(model.can_ns(d['payload'], d['format'], s), d['period_ns']) for s in range(4)]
            rejected = lambda counts: (d['id'], counts) in self.count_groups.rejections
            for sources in lazy.placements(residual, contribution, len(model.SCHEMES[scheme]),
                                           rejected, self.enumeration_stats):
                trial = search.replace_choice(choices, d['id'], scheme, sources)
                action = dict(demand=d['id'], before=deepcopy(choices[d['id']]),
                              after=dict(scheme=scheme, sources=list(sources)), saving_kEUR=saving)
                candidate = self.candidate_screen(trial, bound, d['id'], 'refine', action)
                if candidate is None or candidate['stage'] != 'CAN_pass':
                    continue
                witness = self.construct(candidate, phase='refine')
                if witness['status'] != 'certified':
                    continue
                assert witness['cost_kEUR'] < self.fallback['witness']['cost_kEUR']
                self.publish(trial, witness, 'refine')
                self.moves.append(dict(kind='sweep_refinement', action=action,
                    before_cost_kEUR=bound['cost_kEUR'], after_cost_kEUR=candidate['cost_kEUR']))
                self.structure_stats['refine_commits'] += 1
                choices, bound = trial, candidate
                break
        self.termination = ('independent_cost_lower_attained' if bound['cost_kEUR'] == lower
                            else 'single_cost_sweep_complete_no_local_optimum_claim')


def run(ds, *, mode='JOINT', watchdog_s=None, skip_internal=True, queue_repair=True, one_sweep=True):
    if mode not in ('MRR', 'JOINT'):
        raise ValueError(mode)
    saved = deepcopy(ds)
    with patch.object(model, 'can_analysis', grouped_can.can_analysis):
        runner = Runner(ds, watchdog_s, skip_internal, queue_repair=queue_repair, one_sweep=one_sweep)
        result = runner.execute(mode)
    assert ds == saved
    result.update(can_backend=VERSION, queue_repair=queue_repair, one_sweep=one_sweep,
                  structure_stats=dict(runner.structure_stats),
                  recurrence_reuse=dict(runner.analyzer.stats),
                  feasibility_screen=runner.feasibility_stats, lazy_enumeration=dict(runner.enumeration_stats),
                  scope='one-pass search with independent verification of each feasible return')
    return result
