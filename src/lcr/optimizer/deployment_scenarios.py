"""Deployment-scenario adapters; frozen cross-domain algorithms are untouched.

In the fixed scenario all active and dormant source slots equal the demand's
original source. SA proposes scheme changes only. GA crosses scheme loci and
mutates schemes; it does not spend evaluations changing constant source loci.
The same starts, energy, selection, acceptance, cooling and budgets are retained.
"""
from contextlib import ExitStack
from copy import deepcopy
from time import monotonic, process_time
from unittest.mock import patch

from lcr.network_model import model
from . import agenda_sweep, formal_methods, metaheuristics, sa_diagnostics
from .iso_fd_timing import use_iso

VERSION = "paired-fixed-cross-deployment-v1"
SCENARIOS = ("cross", "fixed")
METHODS = ("All-D", "SA", "GA", "LCR-util", "LCR")
NEW_METHODS = ("SA", "GA", "LCR-util")


def fixed_random_genome(ds, rng):
    return tuple((rng.randrange(len(metaheuristics.SCHEMES)), *(d["original_source"],)*4)
                 for d in ds)


def fixed_change_gene(gene, rng, dormant=False):
    scheme = rng.choice([i for i in range(len(metaheuristics.SCHEMES)) if i != gene[0]])
    return (scheme, *gene[1:])


def fixed_crossover(a, b, rng):
    if not a or len(a) != len(b):
        raise ValueError("equal nonempty parents required")
    lo, hi = sorted(rng.sample(range(len(a)+1), 2))
    return a[:lo]+b[lo:hi]+a[hi:]


def assert_fixed(ds, choices):
    assert set(choices) == {d["id"] for d in ds}
    for d in ds:
        c = choices[d["id"]]
        assert len(c["sources"]) == len(model.SCHEMES[c["scheme"]])
        assert all(s == d["original_source"] for s in c["sources"]), d["id"]


class FixedUtilRunner(agenda_sweep.Runner):
    _cost_sweep = formal_methods.FixedSourceRunner._cost_sweep


def fixed_util(ds):
    measurements = {}
    class Measured(FixedUtilRunner):
        def refine(self):
            wall, cpu = monotonic(), process_time()
            try:
                return super().refine()
            finally:
                measurements.update(refine_s=monotonic()-wall, refine_cpu_s=process_time()-cpu)
    with use_iso(), patch.object(agenda_sweep, "Runner", Measured):
        result = agenda_sweep.run(ds, mode="JOINT", watchdog_s=None)
    result["measurements"] = measurements
    result["registered_method"] = "LCR-util"
    return result


def invoke(ds, method, scenario, *, seed=None, meta_parameters=None):
    if scenario not in SCENARIOS or method not in METHODS[1:]:
        raise ValueError("unknown deployment scenario or optimizer")
    if scenario == "cross":
        return formal_methods.invoke(ds, method, seed=seed, meta_parameters=meta_parameters)
    saved = deepcopy(ds)
    budget = formal_methods.META_PARAMETERS if meta_parameters is None else meta_parameters
    if method == "LCR":
        result = formal_methods.rule_run(ds, "LCR-fixed")
    elif method == "LCR-util":
        result = fixed_util(ds)
    else:
        decode = metaheuristics.decode
        def checked_decode(demands, genes):
            choices = decode(demands, genes)
            assert_fixed(demands, choices)
            return choices
        with ExitStack() as context:
            for name, value in dict(random_genome=fixed_random_genome,
                                    change_gene=fixed_change_gene,
                                    crossover=fixed_crossover,
                                    physical_key=lambda genes: bytes(g[0] for g in genes),
                                    decode=checked_decode).items():
                context.enter_context(patch.object(metaheuristics, name, value))
            if method == "SA":
                result = sa_diagnostics.run(ds, seed=seed, **budget, **formal_methods.SA_PARAMETERS)
            else:
                result = metaheuristics.run(ds, "GA", seed=seed, **budget, **formal_methods.GA_PARAMETERS)
        result["parameters"].update(source_policy="original_only",
            genotype="one mutable scheme locus per demand; four constant source slots",
            crossover="two-point over scheme loci" if method == "GA" else None)
    assert ds == saved
    for field in ("result", "repair_result"):
        if result.get(field) is not None:
            assert_fixed(ds, result[field]["choices"])
    result.update(deployment_scenario="fixed", deployment_adapter=VERSION)
    return result
