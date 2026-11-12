"""Instrument the frozen SA, and isolate start/energy changes on development data.

The legacy policy reproduces metaheuristics.run(SA)'s proposal/acceptance stream
under a fixed proposal budget. Diagnostic aggregation consumes no random draws.
No LCR repair, constructive migration or extra TSN search is introduced.
"""
from collections import Counter
from copy import deepcopy
from math import exp, isfinite
import random
from time import monotonic, process_time
from unittest.mock import patch

from lcr.network_model import model
from . import can_rate_context, grouped_can, metaheuristics as frozen, search
from .iso_fd_timing import VERSION, use_iso
from .repair_refine import INTERNAL_LIMITS


def state_energy(cost, cmax, status, score, policy):
    if policy == 'legacy':
        violation = 0. if status == 'certified' else 1. + sum(map(float, score))
        return frozen.energy(cost, cmax, violation)
    if policy != 'staged':
        raise ValueError('unknown energy policy')
    if status == 'certified':
        return cost / (cmax + 1)
    if status == 'CAN_capacity':
        value = float(score[0])
        return 3. + value / (1. + value)
    if status == 'CAN_deadline':
        value = float(score[1])
        return 2. + value / (1. + value)
    if status.startswith('TSN_'):
        # The unchanged constructor exposes no continuous TSN deficit score.
        return 1.5
    raise ValueError('unexpected evaluated state: ' + status)


def brief(state):
    return {k: state[k] for k in ('status', 'energy', 'cost_kEUR', 'score', 'utilization')}


class Trace:
    """Counts and extremal boundary witnesses, not a full proposal archive."""
    def __init__(self):
        self.active = Counter()
        self.edges = {}
        self.inverted_exit = None
        self.first_new_certified = None

    def observe(self, before, child, accepted, delta, temperature, iteration, elapsed):
        self.active[before['status']] += 1
        if child is None:
            return
        edge = before['status'] + '->' + child['status']
        record = self.edges.setdefault(edge, dict(proposed=0, accepted=0, uphill=0,
            min_delta=float('inf'), max_delta=-float('inf'), first_iteration=iteration,
            first_temperature=temperature, last_temperature=temperature))
        record['proposed'] += 1
        record['accepted'] += int(accepted)
        record['uphill'] += int(delta > 0)
        record['min_delta'] = min(record['min_delta'], delta)
        record['max_delta'] = max(record['max_delta'], delta)
        record['last_temperature'] = temperature
        if child['status'] == 'certified' and self.first_new_certified is None:
            self.first_new_certified = dict(iteration=iteration, elapsed_s=elapsed,
                                           cost_kEUR=child['cost_kEUR'])
        if before['status'] == 'CAN_capacity' and child['status'] != 'CAN_capacity' and delta > 0:
            if self.inverted_exit is None or delta > self.inverted_exit['delta']:
                self.inverted_exit = dict(iteration=iteration, elapsed_s=elapsed,
                    temperature=temperature, delta=delta,
                    acceptance_probability=exp(-delta/max(temperature, 1e-12)),
                    accepted=accepted, before=brief(before), after=brief(child),
                    before_genes=before['genes'], after_genes=child['genes'])

    def output(self, current, temperature):
        return dict(active_stage_counts=dict(self.active), transitions=self.edges,
                    inverted_capacity_exit=self.inverted_exit,
                    first_proposed_new_certified=self.first_new_certified,
                    final_active=brief(current), final_genes=current['genes'],
                    final_temperature=temperature)


def run(ds, *, seed=0, seconds=180., max_steps=5000, energy_policy='legacy',
        start_policy='lowest', temperature=.1, cooling=.9995):
    if energy_policy not in ('legacy', 'staged') or start_policy not in ('lowest', 'All-D'):
        raise ValueError('invalid diagnostic policy')
    if not ds or type(seed) is not int or not isfinite(seconds) or seconds <= 0 or \
            not isfinite(temperature) or temperature <= 0 or not 0 < cooling < 1 or \
            (max_steps is not None and (type(max_steps) is not int or max_steps < 1)):
        raise ValueError('invalid search parameters')
    original = deepcopy(ds)
    ds = sorted(ds, key=lambda d: d['merged_id'])
    rng = random.Random(seed)
    started, cpu_started = monotonic(), process_time()
    stats, unknown = Counter(), Counter()
    memo, timeline = {}, []
    trace = Trace()
    lower = sum(model.cost(d, model.cheapest(d)) for d in ds)
    cmax = sum(4*d['costs_kEUR']['D'] for d in ds)
    best = current = None
    iterations = 0
    termination = 'unknown'
    start_summary = None
    t = temperature

    def guard():
        if monotonic() - started >= seconds:
            raise frozen.SearchBudget('time_limit')

    ev = search.Evaluator(ds, guard)

    def publish(choices, witness, origin):
        nonlocal best
        began = monotonic()
        checked = can_rate_context.audit(ds, choices, witness, frozen.PROFILE)
        stats['audit_s'] += monotonic() - began
        stats['audits'] += 1
        guard()
        if best is None or checked['cost_kEUR'] < best['witness']['cost_kEUR']:
            best = dict(choices=deepcopy(choices), witness=witness, audit=checked)
            timeline.append(dict(elapsed_s=monotonic()-started,
                                 cost_kEUR=checked['cost_kEUR'], phase=origin))

    def attempt(genes, origin):
        guard()
        stats['fitness_requests'] += 1
        key = frozen.physical_key(genes)
        if key in memo:
            stats['fitness_cache_hits'] += 1
            return dict(memo[key], genes=genes)
        choices = frozen.decode(ds, genes)
        try:
            bound = ev.screen(choices)
        except model.AnalysisLimit as exc:
            if str(exc) not in INTERNAL_LIMITS:
                raise
            unknown[str(exc)] += 1
            return None
        witness = ev.construct(bound) if bound['stage'] == 'CAN_pass' else None
        guard()
        feasible = witness is not None and witness['status'] == 'certified'
        status = witness['status'] if witness else bound['stage']
        state = dict(cost_kEUR=bound['cost_kEUR'],
            energy=state_energy(bound['cost_kEUR'], cmax, status, bound['score'], energy_policy),
            certified=feasible, status=status, score=search.score_dict(bound['score']),
            utilization=bound['utilization'])
        if feasible and (best is None or bound['cost_kEUR'] < best['witness']['cost_kEUR']):
            publish(choices, witness, origin)
        stats['new_fitness_evaluations'] += 1
        stats['state:' + status] += 1
        memo[key] = state
        return dict(state, genes=genes)

    with use_iso(), patch.object(model, 'can_analysis', grouped_can.can_analysis):
        try:
            base = frozen.encode(ds, model.configuration(ds, 'D', 'original'))
            base_state = attempt(base, 'All-D')
            if base_state is None or not base_state['certified']:
                raise ValueError('original-source All-D is not qualified')
            cheapest = frozen.encode(ds, model.configuration(ds, 'cheapest', 'original'))
            current = attempt(cheapest, 'lowest_cost_start')
            start_summary = dict(cost_kEUR=lower, status=current['status'] if current else 'unknown')
            if current is None:
                current = base_state
                stats['unknown_start_uses_All-D'] += 1
            if start_policy == 'All-D':
                current = base_state
            while best['witness']['cost_kEUR'] > lower:
                guard()
                if max_steps is not None and iterations >= max_steps:
                    termination = 'diagnostic_step_limit'
                    break
                child = attempt(frozen.proposal(current['genes'], rng), 'SA_proposal')
                accepted = False
                delta = None
                if child is not None:
                    delta = child['energy'] - current['energy']
                    if delta <= 0 or rng.random() < exp(-delta/max(t, 1e-12)):
                        accepted = True
                        stats['accepted'] += 1
                        stats['accepted_uphill'] += int(delta > 0)
                trace.observe(current, child, accepted, delta, t, iterations+1, monotonic()-started)
                if accepted:
                    current = child
                t *= cooling
                iterations += 1
            else:
                termination = 'independent_cost_lower_attained'
        except frozen.SearchBudget:
            termination = 'time_limit'
        elapsed, cpu_s = monotonic()-started, process_time()-cpu_started
        final_audit_started = monotonic()
        if best is not None:
            best['audit'] = can_rate_context.audit(ds, best['choices'], best['witness'], frozen.PROFILE)
        final_audit_s = monotonic()-final_audit_started
    assert sorted(original, key=lambda d: d['merged_id']) == ds
    return dict(method='SA-diagnostic', model_version=VERSION,
        status='feasible' if best else 'unknown', result=best, initial=start_summary,
        termination=termination, elapsed_s=elapsed, cpu_s=cpu_s, final_audit_s=final_audit_s,
        timeline=timeline, statistics=dict(stats), evaluations=ev.screens,
        bus_checks=ev.bus_checks, bus_cache_hits=ev.cache_hits, constructions=ev.constructs,
        unknown_counts=dict(unknown), iterations=iterations, generations=0,
        lower_kEUR=lower, search_seed=seed,
        parameters=dict(seconds=seconds, max_steps=max_steps, energy_policy=energy_policy,
                        start_policy=start_policy, temperature=temperature, cooling=cooling),
        diagnostics=trace.output(current, t) if current is not None else None,
        scope='development diagnosis; no LCR repair; not a formal baseline selection')
