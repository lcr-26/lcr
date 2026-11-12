"""Exact CAN response reuse from an unchanged priority prefix.

For zero jitter a member's recurrence depends only on (C, T), its higher
priority (C,T) sequence, the maximum lower-priority C, and the iteration limit.
No other member's RESPONSE enters that recurrence. An unchanged timing prefix
with the same blocking therefore has the same busy period, q count, waits,
response, and guard count. A changed blocking forces that member's computation
but does not by itself invalidate later prefixes. After a timing-prefix change,
all remaining members are recomputed (even if aggregated interference matches).

Storage: only the latest successful analysis per domain, O(number of members),
not a cache of every visited prefix. UNKNOWN does not enter the cache. Cached
members replay the identical number of guard calls, including external aborts.
Suffix maxima compute EXACT blocking in O(n) rather than repeated O(n^2) scans.
The integer recurrences, initial values, all jobs, and limit tests are unchanged.
"""
from collections import Counter
from copy import deepcopy
from fractions import Fraction
from unittest.mock import patch

from lcr.network_model import model
from . import domain_reuse, grouped_can

VERSION = 'exact-prefix-response-reuse-v1'


class Analyzer:
    def __init__(self, *, reuse=True):
        self.reuse = reuse
        self.previous = {}
        self.stats = Counter()

    def __call__(self, members, guard=lambda: None, limit=10000):
        responses, loads, details = {}, {}, {}
        pending = {}
        self.stats['analysis_calls'] += 1
        for source in range(4):
            fs = sorted((m for m in members.values() if m['source'] == source),
                        key=lambda m: m['priority'])
            if len(fs) > 2048:
                return dict(status='CAN_identifier_capacity', source=source)
            util = sum((Fraction(m['c_ns'], m['period_ns']) for m in fs), Fraction())
            loads[source] = float(util)
            if util >= 1:
                return dict(status='CAN_capacity_screen', source=source, utilization=loads)
            # Empty peer domains are common in a domain-local query; retaining
            # their last nonempty analysis cannot affect the empty answer.
            if not fs:
                continue
            suffix = [0] * (len(fs)+1)
            for i in range(len(fs)-1, -1, -1):
                suffix[i] = max(fs[i]['c_ns'], suffix[i+1])
            self.stats['suffix_members'] += len(fs)
            signature = tuple((m['id'], m['priority'], m['c_ns'], m['period_ns']) for m in fs)
            prior = self.previous.get(source)
            common = (self.reuse and prior is not None and prior['limit'] == limit
                      and prior['bit_ns'] == grouped_can.BIT_NS)
            prefix, records = {}, []
            for index, m in enumerate(fs):
                # Prefix timing must match up to and including the current m.
                common = (common and index < len(prior['signature'])
                          and signature[index] == prior['signature'][index])
                hp = tuple(prefix.items())
                period, duration = m['period_ns'], m['c_ns']
                prefix[period] = prefix.get(period, 0) + duration
                hep = tuple(prefix.items())
                blocking = suffix[index+1]
                old = prior['records'][index] if common else None
                if old is not None and old[2] == blocking:
                    # Replay guards instead of skipping them. For a guard that
                    # interrupts on call k, the returned/UNKNOWN outcome stays
                    # exactly the same as recomputing each recurrence step.
                    for _ in range(old[3]):
                        guard()
                    response, detail = old[0], old[1]
                    records.append(old)
                    self.stats['responses_reused'] += 1
                    self.stats['guard_calls_replayed'] += old[3]
                else:
                    steps = 1
                    guard()
                    busy = duration
                    for _ in range(limit):
                        guard()
                        steps += 1
                        nxt = blocking + sum(((busy+t-1)//t)*c for t, c in hep)
                        if nxt == busy:
                            break
                        busy = nxt
                    else:
                        raise model.AnalysisLimit('CAN_busy_iteration_limit')
                    count = (busy+period-1)//period
                    if count > limit:
                        raise model.AnalysisLimit('CAN_job_limit')
                    values = []
                    for q in range(count):
                        wait = blocking + q*duration
                        for _ in range(limit):
                            guard()
                            steps += 1
                            nxt = blocking + q*duration + sum(
                                ((wait+grouped_can.BIT_NS+t-1)//t)*c for t, c in hp)
                            if nxt == wait:
                                break
                            wait = nxt
                        else:
                            raise model.AnalysisLimit('CAN_wait_iteration_limit')
                        values.append(wait+duration-q*period)
                    response = max(values)
                    detail = dict(blocking_ns=blocking, jobs=count, busy_ns=busy)
                    records.append((response, detail, blocking, steps))
                    self.stats['responses_computed'] += 1
                responses[m['id']] = response
                # Return owned details; callers cannot mutate cached evidence.
                details[m['id']] = detail.copy()
            pending[source] = dict(signature=signature, records=records, limit=limit,
                                   bit_ns=grouped_can.BIT_NS)
        # Do not commit a partially successful/UNKNOWN multi-domain query.
        self.previous.update(pending)
        self.stats['peak_stored_members'] = max(self.stats['peak_stored_members'],
            sum(len(value['signature']) for value in self.previous.values()))
        return dict(status='ok', responses=responses, utilization=loads, details=details)


class Runner(domain_reuse.Runner):
    def __init__(self, ds, watchdog_s, skip_internal):
        super().__init__(ds, watchdog_s, skip_internal)
        self.analyzer = Analyzer()

    def repair(self, free):
        # Restrict the new kernel to the first phase in this validation. Joint
        # refinement still uses the previously tested grouped kernel unchanged.
        with patch.object(model, 'can_analysis', self.analyzer):
            return super().repair(free)


def run(ds, *, mode='JOINT', watchdog_s=None, skip_internal=True):
    if mode not in ('MRR', 'JOINT'):
        raise ValueError(mode)
    saved = deepcopy(ds)
    with patch.object(model, 'can_analysis', grouped_can.can_analysis):
        runner = Runner(ds, watchdog_s, skip_internal)
        result = runner.execute(mode)
    assert ds == saved
    result.update(can_backend='exact-period-grouping-v1', repair_can_backend=VERSION,
        repair_backend='domain-local-reuse-v1', recurrence_reuse=dict(runner.analyzer.stats),
        domain_reuse=dict(runner.reduction_scorer.stats),
        reduction_score_stages=dict(runner.reduction_scorer.stages),
        evaluation_counter_scope='evaluations counts materialized reference screens; '
                                 'trial_count counts all logical candidate evaluations')
    return result
