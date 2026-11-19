"""Exact domain-local reuse for the fixed-source reduction phase only.

The reference repair loop, complete candidate enumeration and tie-breaking are
inherited unchanged. A reduction changes one CAN domain: reuse its peers' local
deadline sums and that domain's previously scored edits until it changes. Always
recompute the GLOBAL capacity/deadline score. Only the selected edit needs the
full network dictionaries; materialize it with the original evaluator and assert
agreement before using it. Joint refinement is deliberately unchanged.

No new numerical tolerance, candidate limit, early stop or infeasibility claim.
UNKNOWN analyses are not cached. Domain caches belong to one immutable input
and timing context, and contain only the current domain configuration's edits.
"""
from collections import Counter
from copy import deepcopy
from fractions import Fraction
from time import monotonic
from unittest.mock import patch

from lcr.network_model import model
from . import grouped_can, repair_refine, search


class DeferredBound(search.ScreenResult):
    """Exact ranking fields; the selected candidate is reified before use."""
    def __init__(self, evaluator, choices, base, changed, stats, **fields):
        super().__init__(**fields)
        self._pending = (evaluator, choices, base, changed, stats)

    def full(self):
        if self._pending is not None:
            evaluator, choices, base, changed, stats = self._pending
            began = monotonic()
            result = evaluator.screen(choices, base=base, changed=changed)
            assert self.loads == result.loads
            for name in ('stage', 'score', 'hot', 'cost_kEUR', 'utilization'):
                assert dict.__getitem__(self, name) == result[name], name
            self.update(result)
            self._pending = None
            stats['materializations'] += 1
            stats['materialization_s'] += monotonic() - began
        return self

    def __getitem__(self, name):
        if name not in self:
            self.full()
        return super().__getitem__(name)

    def get(self, name, default=None):
        if name not in self:
            self.full()
        return super().get(name, default)


class DomainReductionScorer:
    def __init__(self, evaluator):
        self.ev = evaluator
        self.parent = None
        self.domains = [None] * 4
        self.stats = Counter()
        self.stages = Counter()

    @staticmethod
    def signature(flows):
        # Include the deadline and singleton transit inputs as well as the RTA
        # inputs: cached deadline sums depend on more than CAN responses.
        return tuple(sorted((k, m['c_ns'], m['period_ns'], m['priority'],
                             m['deadline_ns'], m['payload']) for k, m in flows.items()))

    @staticmethod
    def severity(flows, responses):
        value = Fraction()
        for k, m in flows.items():
            reserve, wire = model.tsn_ns([m['payload']])
            deficit = responses[k] + reserve + model.SWITCH_NS + wire - m['deadline_ns']
            if deficit > 0:
                value += Fraction(deficit, m['deadline_ns'])
        return value

    def prepare(self, base):
        if base is self.parent:
            return
        if isinstance(base, DeferredBound):
            base.full()
        groups = [{} for _ in range(4)]
        for k, member in base['members'].items():
            groups[member['source']][k] = member
        for source, flows in enumerate(groups):
            signature = self.signature(flows)
            old = self.domains[source]
            if old is not None and old['signature'] == signature:
                self.stats['unchanged_domain_reuses'] += 1
                continue
            if old is not None:
                self.stats['domain_invalidations'] += 1
            self.domains[source] = dict(signature=signature, flows=flows,
                severity=(self.severity(flows, base['responses'])
                          if base['stage'] != 'CAN_capacity' else None), edits={})
        self.parent = base
        self.stats['parent_preparations'] += 1

    def analyze(self, source, flows):
        # Share the reference response cache so selected-candidate reification
        # and subsequent refinement do not recompute the same domain RTA.
        signature = (source, tuple(sorted((k, m['c_ns'], m['period_ns'], m['priority'])
                                          for k, m in flows.items())))
        if signature in self.ev.bus_cache:
            self.ev.cache_hits += 1
            value = self.ev.bus_cache[signature]
        else:
            self.ev.guard()
            self.ev.bus_checks += 1
            value = model.can_analysis(flows, self.ev.guard)
            if value['status'] != 'ok':
                raise AssertionError(value)
            self.ev.bus_cache[signature] = value
        self.stats['local_severity_computations'] += 1
        return self.severity(flows, value['responses'])

    def screen(self, choices, *, base, changed):
        self.ev.guard()
        self.stats['score_requests'] += 1
        self.prepare(base)
        old = [base['members'][f'{changed}:{slot}'] for slot in range(4)
               if f'{changed}:{slot}' in base['members']]
        source = old[0]['source']
        ch = choices[changed]
        if (any(m['source'] != source for m in old)
                or any(s != source for s in ch['sources'])
                or len(ch['sources']) >= len(old)):
            # Outside the proven one-domain reduction contract: use reference.
            self.stats['reference_fallbacks'] += 1
            return self.ev.screen(choices, base=base, changed=changed)
        d = self.ev.lookup[changed]
        count = len(ch['sources'])
        assert count == len(model.SCHEMES[ch['scheme']])
        loads = list(base.loads)
        loads[source] -= (len(old) - count) * Fraction(old[0]['c_ns'], old[0]['period_ns'])
        cap = sum((u for u in loads if u >= 1), Fraction())
        cost = base['cost_kEUR'] - sum(m['cost_kEUR'] for m in old) + model.cost(d, ch['scheme'])
        if cap:
            stage, score = 'CAN_capacity', (cap, Fraction())
            hot = {s for s, load in enumerate(loads) if load >= 1}
        else:
            values = []
            # Preserve domain order and propagate the FIRST analysis limit.
            for s, domain in enumerate(self.domains):
                if s != source:
                    if domain['severity'] is None:
                        domain['severity'] = self.analyze(s, domain['flows'])
                    else:
                        self.stats['base_severity_hits'] += 1
                    values.append(domain['severity'])
                    continue
                edit = (changed, ch['scheme'], tuple(ch['sources']))
                if edit in domain['edits']:
                    self.stats['candidate_local_hits'] += 1
                    value = domain['edits'][edit]
                else:
                    flows = domain['flows'].copy()
                    for m in old:
                        flows.pop(m['id'])
                    flows.update(model.materialize([d], {changed: ch}))
                    # Exceptions (including UNKNOWN) never enter this cache.
                    value = self.analyze(s, flows)
                    domain['edits'][edit] = value
                    self.stats['candidate_local_misses'] += 1
                values.append(value)
            severity = sum(values, Fraction())
            stage = 'CAN_deadline' if severity else 'CAN_pass'
            score = (Fraction(), severity)
            hot = {s for s, value in enumerate(values) if value}
        self.stages[stage] += 1
        self.stats['peak_local_edits'] = max(self.stats['peak_local_edits'],
            sum(len(domain['edits']) for domain in self.domains))
        return DeferredBound(self.ev, choices, base, changed, self.stats,
            loads=loads, stage=stage, score=score, hot=hot, cost_kEUR=cost,
            utilization=dict(enumerate(map(float, loads))))


class Runner(repair_refine.Runner):
    def __init__(self, ds, watchdog_s, skip_internal):
        super().__init__(ds, watchdog_s, skip_internal)
        self.reduction_scorer = DomainReductionScorer(self.ev)

    def repair(self, free):
        if free:
            raise ValueError('domain reuse is restricted to the fixed-source repair phase')
        return super().repair(free=False)

    def candidate_screen(self, choice, bound, changed, phase, action):
        if phase != 'reduction':
            return super().candidate_screen(choice, bound, changed, phase, action)
        self.trial_count += 1
        self.counts['screen:' + phase] += 1
        began = monotonic()
        try:
            return self.reduction_scorer.screen(choice, base=bound, changed=changed)
        except model.AnalysisLimit as exc:
            reason = str(exc)
            if not self.skip_internal or reason not in repair_refine.INTERNAL_LIMITS:
                raise
            label = phase + ':' + reason
            self.unknown[label] += 1
            if label not in self.unknown_examples:
                self.unknown_examples[label] = deepcopy(action)
            return None
        finally:
            self.times['screen:' + phase] += monotonic() - began


def run(ds, *, mode='JOINT', watchdog_s=None, skip_internal=True):
    if mode not in ('MRR', 'JOINT'):
        raise ValueError(mode)
    saved = deepcopy(ds)
    with patch.object(model, 'can_analysis', grouped_can.can_analysis):
        runner = Runner(ds, watchdog_s, skip_internal)
        result = runner.execute(mode)
    assert ds == saved
    result.update(can_backend='exact-period-grouping-v1',
        repair_backend='domain-local-reuse-v1',
        domain_reuse=dict(runner.reduction_scorer.stats),
        reduction_score_stages=dict(runner.reduction_scorer.stages),
        evaluation_counter_scope='evaluations counts materialized reference screens; '
                                 'trial_count counts all logical candidate evaluations')
    return result
