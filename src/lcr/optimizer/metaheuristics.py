"""Development SA/GA adapters for the convergent ISO FD timing model.

Operators follow the project's timing-only baselines: SA uses Metropolis and
geometric cooling; GA uses linear-rank roulette, two-point crossover and one
elite. This is an adaptation, not a reproduction of Hu/Luo or a calibrated
claim. Both search decomposition plus four fixed source slots per demand;
all methods share the existing deterministic packing/scheduling constructor.
No LCR repair or postprocessing is injected into these baselines.
"""
from collections import Counter
from copy import deepcopy
from math import exp, isfinite
import random
from time import monotonic, process_time
from unittest.mock import patch

from lcr.network_model import model
from . import can_rate_context, grouped_can, search
from .iso_fd_timing import VERSION, use_iso
from .repair_refine import INTERNAL_LIMITS

SCHEMES = tuple(model.SCHEMES)
PROFILE = can_rate_context.PROFILES[0]


class SearchBudget(Exception):
    pass


def encode(ds, choices):
    return tuple((SCHEMES.index(choices[d['id']]['scheme']),
                  *(choices[d['id']]['sources'] + [d['original_source']] *
                    (4-len(choices[d['id']]['sources'])))) for d in ds)


def decode(ds, genes):
    if len(ds) != len(genes):
        raise ValueError('one gene per mandatory demand required')
    result = {}
    for d, gene in zip(ds, genes):
        if len(gene) != 5 or not 0 <= gene[0] < len(SCHEMES) or any(
                not 0 <= s < 4 for s in gene[1:]):
            raise ValueError('illegal scheme or source slot')
        scheme = SCHEMES[gene[0]]
        result[d['id']] = dict(scheme=scheme,
                              sources=list(gene[1:1+len(model.SCHEMES[scheme])]))
    return result


def physical_key(genes):
    # Dormant slots do not change fitness; retain them in the working genome.
    return bytes(v for gene in genes for v in
                 (gene[0], *gene[1:1+len(model.SCHEMES[SCHEMES[gene[0]]])],
                  *([4] * (4-len(model.SCHEMES[SCHEMES[gene[0]]])))))


def random_genome(ds, rng):
    return tuple((rng.randrange(len(SCHEMES)), *(rng.randrange(4) for _ in range(4)))
                 for _ in ds)


def change_gene(gene, rng, dormant=False):
    out = list(gene)
    if rng.randrange(2) == 0:
        out[0] = rng.choice([s for s in range(len(SCHEMES)) if s != gene[0]])
    else:
        slots = 4 if dormant else len(model.SCHEMES[SCHEMES[gene[0]]])
        j = 1+rng.randrange(slots)
        out[j] = rng.choice([s for s in range(4) if s != out[j]])
    return tuple(out)


def proposal(genes, rng):
    out = list(genes)
    for _ in range(2 if rng.random() < .25 else 1):
        j = rng.randrange(len(out))
        out[j] = change_gene(out[j], rng)
    return tuple(out)


def crossover(a, b, rng):
    if not a or len(a) != len(b):
        raise ValueError('equal nonempty parents required')
    aa = tuple(v for gene in a for v in gene)
    bb = tuple(v for gene in b for v in gene)
    lo, hi = sorted(rng.sample(range(len(aa)+1), 2))
    child = aa[:lo]+bb[lo:hi]+aa[hi:]
    return tuple(child[i:i+5] for i in range(0, len(child), 5))


def mutate(genes, rng):
    return tuple(change_gene(g, rng, dormant=True) if rng.random() < 1/len(genes)
                 else g for g in genes)


def rank_weights(population):
    indices = sorted(range(len(population)), key=lambda i: population[i]['energy'], reverse=True)
    weights = [0.] * len(indices)
    lo = 0
    while lo < len(indices):
        hi = lo+1
        while hi < len(indices) and population[indices[hi]]['energy'] == population[indices[lo]]['energy']:
            hi += 1
        for j in range(lo, hi):
            weights[indices[j]] = (lo+1+hi)/2
        lo = hi
    return weights


def energy(cost, cmax, violation):
    return cost/(cmax+1) if violation == 0 else 1+violation/(1+violation)


def run(ds, method, *, seed=0, seconds=600., population=32,
        temperature=.1, cooling=.9995, crossover_probability=.9, max_steps=None):
    """600 s applies to the whole search, including baseline and initial state.

    max_steps is a deterministic testing hook; the server trial leaves it None.
    UNKNOWN candidates are skipped, never classified as infeasible. Only final
    witnesses and compact improvement events are retained, not every genome.
    """
    if method not in ('SA', 'GA') or not ds or type(seed) is not int:
        raise ValueError('SA/GA, nonempty demands and integer seed required')
    if not isfinite(seconds) or seconds <= 0 or population < 2 or \
            not isfinite(temperature) or temperature <= 0 or not 0 < cooling < 1 or \
            not 0 <= crossover_probability <= 1 or \
            (max_steps is not None and (type(max_steps) is not int or max_steps < 1)):
        raise ValueError('invalid search parameters')
    original = deepcopy(ds)
    ds = sorted(ds, key=lambda d: d['merged_id'])
    rng = random.Random(seed)
    started, cpu_started = monotonic(), process_time()
    stats, unknown = Counter(), Counter()
    memo, timeline = {}, []
    best = None
    lower = sum(model.cost(d, model.cheapest(d)) for d in ds)
    cmax = sum(4*d['costs_kEUR']['D'] for d in ds)
    termination = 'unknown'
    iterations = generations = 0
    start_summary = None

    def guard():
        if monotonic()-started >= seconds:
            raise SearchBudget('time_limit')

    ev = search.Evaluator(ds, guard)

    def publish(choices, witness, origin):
        nonlocal best
        began = monotonic()
        checked = can_rate_context.audit(ds, choices, witness, PROFILE)
        stats['audit_s'] += monotonic()-began
        stats['audits'] += 1
        guard()  # Never claim an improvement obtained after the time limit.
        if best is None or checked['cost_kEUR'] < best['witness']['cost_kEUR']:
            best = dict(choices=deepcopy(choices), witness=witness, audit=checked)
            timeline.append(dict(elapsed_s=monotonic()-started,
                                 cost_kEUR=checked['cost_kEUR'], phase=origin))

    def attempt(genes, origin):
        guard()
        stats['fitness_requests'] += 1
        key = physical_key(genes)
        if key in memo:
            stats['fitness_cache_hits'] += 1
            return dict(memo[key], genes=genes)
        choices = decode(ds, genes)
        try:
            bound = ev.screen(choices)
        except model.AnalysisLimit as exc:
            if str(exc) not in INTERNAL_LIMITS:
                raise
            unknown[str(exc)] += 1
            return None
        witness = None
        if bound['stage'] == 'CAN_pass':
            witness = ev.construct(bound)
        guard()
        feasible = witness is not None and witness['status'] == 'certified'
        value = 0. if feasible else 1.+sum(map(float, bound['score']))
        state = dict(cost_kEUR=bound['cost_kEUR'], energy=energy(bound['cost_kEUR'], cmax, value),
                     certified=feasible, status=witness['status'] if witness else bound['stage'])
        if feasible and (best is None or bound['cost_kEUR'] < best['witness']['cost_kEUR']):
            publish(choices, witness, origin)
        stats['new_fitness_evaluations'] += 1
        stats['state:'+state['status']] += 1
        memo[key] = state
        return dict(state, genes=genes)

    with use_iso(), patch.object(model, 'can_analysis', grouped_can.can_analysis):
        try:
            base = encode(ds, model.configuration(ds, 'D', 'original'))
            base_state = attempt(base, 'All-D')
            if base_state is None or not base_state['certified']:
                raise ValueError('original-source All-D is not qualified')
            cheapest = encode(ds, model.configuration(ds, 'cheapest', 'original'))
            current = attempt(cheapest, 'lowest_cost_start')
            start_summary = dict(cost_kEUR=sum(model.cost(d, model.cheapest(d)) for d in ds),
                                 status=current['status'] if current else 'unknown')
            if current is None:
                current = base_state
                stats['unknown_start_uses_All-D'] += 1
            members = [base_state, current]
            t = temperature
            if method == 'GA':
                while len(members) < population and best['witness']['cost_kEUR'] > lower:
                    item = attempt(random_genome(ds, rng), 'random_initial_member')
                    if item is not None:
                        members.append(item)
                        stats['initial_random_members'] += 1
            while best['witness']['cost_kEUR'] > lower:
                guard()
                if max_steps is not None and iterations >= max_steps:
                    termination = 'test_step_limit'
                    break
                if method == 'SA':
                    child = attempt(proposal(current['genes'], rng), 'SA_proposal')
                    if child is not None:
                        delta = child['energy']-current['energy']
                        if delta <= 0 or rng.random() < exp(-delta/max(t, 1e-12)):
                            current = child
                            stats['accepted'] += 1
                            stats['accepted_uphill'] += int(delta > 0)
                    t *= cooling
                    iterations += 1
                else:
                    weights = rank_weights(members)
                    following = [min(members, key=lambda s: s['energy'])]
                    while len(following) < population and best['witness']['cost_kEUR'] > lower:
                        guard()
                        if max_steps is not None and iterations >= max_steps:
                            break
                        a, b = rng.choices(members, weights=weights, k=2)
                        child = a['genes']
                        if rng.random() < crossover_probability:
                            child = crossover(child, b['genes'], rng)
                            stats['crossovers'] += 1
                        item = attempt(mutate(child, rng), 'GA_offspring')
                        iterations += 1
                        if item is not None:
                            following.append(item)
                    members = following
                    generations += 1
            else:
                termination = 'independent_cost_lower_attained'
        except SearchBudget:
            termination = 'time_limit'
        elapsed, cpu_s = monotonic()-started, process_time()-cpu_started
        final_audit_started = monotonic()
        if best is not None:
            best['audit'] = can_rate_context.audit(ds, best['choices'], best['witness'], PROFILE)
        final_audit_s = monotonic()-final_audit_started
    assert sorted(original, key=lambda d: d['merged_id']) == ds
    return dict(method=method, model_version=VERSION, status='feasible' if best else 'unknown',
                result=best, initial=start_summary, termination=termination, elapsed_s=elapsed,
                cpu_s=cpu_s, final_audit_s=final_audit_s, timeline=timeline, statistics=dict(stats),
                evaluations=ev.screens, bus_checks=ev.bus_checks, bus_cache_hits=ev.cache_hits,
                constructions=ev.constructs, unknown_counts=dict(unknown), iterations=iterations,
                generations=generations, lower_kEUR=lower, search_seed=seed,
                parameters=dict(seconds=seconds, population=population if method == 'GA' else None,
                    temperature=temperature if method == 'SA' else None,
                    cooling=cooling if method == 'SA' else None,
                    crossover_probability=crossover_probability if method == 'GA' else None,
                    mutation_per_demand=1/len(ds) if method == 'GA' else None,
                    max_steps=max_steps, stagnation_stop=False),
                scope='development adaptation; parameters not calibrated for this new model')
