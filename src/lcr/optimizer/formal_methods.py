"""Frozen-rule adapters for the prospective six-method scale experiment.

Historical search modules are left intact. The fixed-source control changes
only the placement domain of the current one-pass cost sweep. In particular,
it does not enumerate and discard the other 4**k placements.
"""
from copy import deepcopy
from time import monotonic, process_time
from unittest.mock import patch

from lcr.network_model import model
from . import agenda_sweep, causal_priority_queue as causal, metaheuristics, sa_diagnostics, search
from .iso_fd_timing import use_iso

VERSION = 'formal-six-methods-single-sweep-v1'
RULE_METHODS = ('LCR-util', 'LCR-fixed', 'LCR')
OPT_METHODS = ('SA', 'GA', *RULE_METHODS)
METHODS = ('All-D', *OPT_METHODS)
META_PARAMETERS = dict(seconds=1800., max_steps=None)
SA_PARAMETERS = dict(energy_policy='legacy', start_policy='All-D', temperature=.1, cooling=.9995)
GA_PARAMETERS = dict(population=32, crossover_probability=.9)


class FixedSourceRunner(causal.Runner):
    """Same repair, cheaper options, checks and acceptance; original sources."""
    def _cost_sweep(self):
        choices = deepcopy(self.fallback['choices'])
        bound = self.ev.screen(choices)
        assert bound['stage'] == 'CAN_pass'
        options = agenda_sweep.cost_options(self.ds, choices)
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
            sources = (d['original_source'],) * len(model.SCHEMES[scheme])
            self.enumeration_stats['fixed_placements_yielded'] += 1
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
        self.termination = ('independent_cost_lower_attained' if bound['cost_kEUR'] == lower
                            else 'single_cost_sweep_complete_no_local_optimum_claim')


def rule_run(ds, method):
    if method not in RULE_METHODS:
        raise ValueError(method)
    module = agenda_sweep if method == 'LCR-util' else causal
    parent = FixedSourceRunner if method == 'LCR-fixed' else module.Runner
    measurements = {}
    class Measured(parent):
        def refine(self):
            began, cpu = monotonic(), process_time()
            try:
                return super().refine()
            finally:
                measurements.update(refine_s=monotonic()-began, refine_cpu_s=process_time()-cpu)
    with use_iso(), patch.object(module, 'Runner', Measured):
        result = module.run(ds, mode='JOINT', watchdog_s=None)
    result['measurements'] = measurements
    result['registered_method'] = method
    return result


def invoke(ds, method, *, seed=None, meta_parameters=None):
    if method in RULE_METHODS:
        return rule_run(ds, method)
    budget = META_PARAMETERS if meta_parameters is None else meta_parameters
    if method == 'SA':
        return sa_diagnostics.run(ds, seed=seed, **budget, **SA_PARAMETERS)
    if method == 'GA':
        return metaheuristics.run(ds, 'GA', seed=seed, **budget, **GA_PARAMETERS)
    raise ValueError(method)
