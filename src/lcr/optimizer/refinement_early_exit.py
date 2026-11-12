"""Feasibility-only CAN screening for the frozen second-phase neighborhood.

An exact converged job response larger than D - singleton TSN transit already
fails the inherited sufficient screen: max_q R(q) >= R(q). Stop that candidate,
without computing other jobs/members or a full violation score. This is a
checker rejection, NOT a proof that every physical schedule is infeasible.

Partial responses never enter the complete-domain/prefix caches. A separate
cache stores only rejection witnesses under a full domain timing signature;
the current deadline/transit threshold is checked again on every reuse.
UNKNOWN is still raised, not cached or called a deadline violation. A witness
found before a later analysis limit can replace a formerly UNKNOWN outcome.
Candidate order, first phase, acceptance and complete certification are intact.
"""
from collections import Counter
from copy import deepcopy
from fractions import Fraction
from unittest.mock import patch

from lcr.network_model import model
from . import grouped_can, recurrence_reuse, refinement_reuse, search
from .protocol import id_map

VERSION = 'refinement-feasibility-early-exit-v1'


def response_budget(member):
    reserve, wire = model.tsn_ns([member['payload']])
    return member['deadline_ns'] - reserve - model.SWITCH_NS - wire


class DeadlineWitness(Exception):
    def __init__(self, member, response, *, job=None):
        self.member, self.response, self.job = member, response, job
        super().__init__(f'{member}: certified response contribution {response}')


class Analyzer(recurrence_reuse.Analyzer):
    """Original full kernel plus a deadline-aware, domain-local entry point."""
    def __init__(self, original):
        super().__init__(reuse=original.reuse)
        self.previous, self.stats = original.previous, original.stats
        self.early_stats = Counter()

    def feasible_domain(self, members, guard=lambda: None, limit=10000):
        # Same arithmetic/order/limits as recurrence_reuse.Analyzer. Only the
        # checks marked WITNESS and diagnostics are added. Full success commits
        # the same complete prefix records; an exception commits nothing.
        sources = {m['source'] for m in members.values()}
        if len(sources) > 1:
            raise ValueError('domain-local query required')
        if not members:
            return self(members, guard, limit)
        source = next(iter(sources))
        fs = sorted(members.values(), key=lambda m: m['priority'])
        self.stats['analysis_calls'] += 1
        if len(fs) > 2048:
            return dict(status='CAN_identifier_capacity', source=source)
        util = sum((Fraction(m['c_ns'], m['period_ns']) for m in fs), Fraction())
        loads = {s: float(util) if s == source else 0.0 for s in range(4)}
        if util >= 1:
            return dict(status='CAN_capacity_screen', source=source,
                        utilization={s:loads[s] for s in range(source+1)})
        suffix = [0] * (len(fs)+1)
        for i in range(len(fs)-1, -1, -1):
            suffix[i] = max(fs[i]['c_ns'], suffix[i+1])
        self.stats['suffix_members'] += len(fs)
        signature = tuple((m['id'], m['priority'], m['c_ns'], m['period_ns']) for m in fs)
        prior = self.previous.get(source)
        common = (self.reuse and prior is not None and prior['limit'] == limit
                  and prior['bit_ns'] == grouped_can.BIT_NS)
        prefix, records, responses, details = {}, [], {}, {}
        for index, m in enumerate(fs):
            common = (common and index < len(prior['signature'])
                      and signature[index] == prior['signature'][index])
            hp = tuple(prefix.items())
            period, duration = m['period_ns'], m['c_ns']
            prefix[period] = prefix.get(period, 0) + duration
            hep = tuple(prefix.items())
            blocking = suffix[index+1]
            old = prior['records'][index] if common else None
            budget = response_budget(m)
            if old is not None and old[2] == blocking:
                for _ in range(old[3]):
                    guard()
                response, detail = old[0], old[1]
                records.append(old)
                self.stats['responses_reused'] += 1
                self.stats['guard_calls_replayed'] += old[3]
                if response > budget:  # WITNESS: exact previously computed max.
                    self.early_stats['prefix_witnesses'] += 1
                    self.early_stats['members_skipped'] += len(fs)-index-1
                    raise DeadlineWitness(m['id'], response)
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
                    response = wait+duration-q*period
                    self.early_stats['jobs_converged'] += 1
                    if response > budget:  # WITNESS: max_q R(q) >= this R(q).
                        self.early_stats['job_witnesses'] += 1
                        self.early_stats['jobs_skipped'] += count-q-1
                        self.early_stats['members_skipped'] += len(fs)-index-1
                        raise DeadlineWitness(m['id'], response, job=q)
                    values.append(response)
                response = max(values)
                detail = dict(blocking_ns=blocking, jobs=count, busy_ns=busy)
                records.append((response, detail, blocking, steps))
                self.stats['responses_computed'] += 1
            responses[m['id']] = response
            details[m['id']] = detail.copy()
        self.previous[source] = dict(signature=signature, records=records, limit=limit,
                                     bit_ns=grouped_can.BIT_NS)
        self.stats['peak_stored_members'] = max(self.stats['peak_stored_members'],
            sum(len(value['signature']) for value in self.previous.values()))
        return dict(status='ok', responses=responses, utilization=loads, details=details)


class FeasibilityScreen:
    """Bound only in refine; no partial result can reach TSN construction."""
    def __init__(self, evaluator, analyzer):
        self.ev, self.analyzer = evaluator, analyzer
        self.full_screen = evaluator.screen
        self.rejections, self.stats = {}, Counter()

    def reject(self, result, witness):
        member = result['members'][witness.member]
        deficit = witness.response-response_budget(member)
        assert deficit > 0
        result.update(stage='CAN_deadline', partial=True, score=None,
            hot={member['source']}, violations=[dict(member=witness.member,
                deficit_ns=deficit, source=member['source'])],
            rejection_witness=dict(member=witness.member, response_contribution_ns=witness.response,
                                   job=witness.job))
        self.ev.statuses['CAN_deadline'] += 1
        self.stats['rejected_candidates'] += 1
        return result

    def __call__(self, choices, *, base=None, changed=None):
        if base is None or changed is None:
            return self.full_screen(choices, base=base, changed=changed)
        ev = self.ev
        ev.guard(); ev.screens += 1; ev.delta_screens += 1
        # Keep the original delta materialization, capacity gate and domain
        # traversal. This experiment isolates feasibility-only early rejection.
        members = base['members'].copy(); ids = base['can_ids'].copy()
        loads = list(base.loads); total_cost = base['cost_kEUR']
        old_keys = []
        for slot in range(4):
            k = f'{changed}:{slot}'
            old = members.get(k)
            if old is not None:
                old_keys.append(k)
                loads[old['source']] -= Fraction(old['c_ns'], old['period_ns'])
                total_cost -= old['cost_kEUR']
        d = ev.lookup[changed]
        added = model.materialize([d], {changed: choices[changed]})
        for k in old_keys:
            if k not in added:
                members.pop(k); ids.pop(k)
        members.update(added); ids.update(id_map([d], {changed: choices[changed]}))
        for m in added.values():
            loads[m['source']] += Fraction(m['c_ns'], m['period_ns'])
            total_cost += m['cost_kEUR']
        ev.members_materialized += len(added)
        cap = sum((u for u in loads if u >= 1), Fraction())
        result = search.ScreenResult(loads=loads, members=members, can_ids=ids,
            cost_kEUR=total_cost, utilization={s:float(u) for s,u in enumerate(loads)},
            hot=set(), responses={}, details={})
        if cap:
            result.update(stage='CAN_capacity', score=(cap,Fraction()),
                          hot={s for s,u in enumerate(loads) if u >= 1})
            ev.statuses['CAN_capacity'] += 1
            return result
        for s in range(4):
            flows = {k:m for k,m in members.items() if m['source'] == s}
            signature = (s, tuple(sorted((k,m['c_ns'],m['period_ns'],m['priority'])
                                        for k,m in flows.items())))
            # Do not reuse a rejection after bit timing/threshold changes.
            fail_key = (grouped_can.BIT_NS, signature)
            witness = self.rejections.get(fail_key)
            if witness is not None and witness.response > response_budget(flows[witness.member]):
                self.stats['rejection_cache_hits'] += 1
                return self.reject(result, witness)
            if signature in ev.bus_cache:
                ev.cache_hits += 1
                value = ev.bus_cache[signature]
                for k,m in flows.items():
                    if value['responses'][k] > response_budget(m):
                        witness = DeadlineWitness(k, value['responses'][k])
                        self.rejections[fail_key] = witness
                        self.stats['complete_cache_witnesses'] += 1
                        return self.reject(result, witness)
            else:
                ev.guard(); ev.bus_checks += 1
                try:
                    value = self.analyzer.feasible_domain(flows, ev.guard)
                except DeadlineWitness as witness:
                    # Store a fresh exception without traceback/local frames.
                    saved = DeadlineWitness(witness.member, witness.response, job=witness.job)
                    self.rejections[fail_key] = saved
                    self.stats['new_domain_witnesses'] += 1
                    return self.reject(result, saved)
                if value['status'] != 'ok':
                    raise AssertionError(value)
                ev.bus_cache[signature] = value
            result['responses'].update(value['responses'])
            result['details'].update(value['details'])
        result.update(stage='CAN_pass', score=(Fraction(),Fraction()), violations=[])
        ev.statuses['CAN_pass'] += 1
        self.stats['passed_candidates'] += 1
        return result


class Runner(refinement_reuse.Runner):
    def __init__(self, ds, watchdog_s, skip_internal):
        super().__init__(ds, watchdog_s, skip_internal)
        self.feasibility_stats = {}

    def refine(self):
        self.analyzer = Analyzer(self.analyzer)
        checker = FeasibilityScreen(self.ev, self.analyzer)
        try:
            with patch.object(self.ev, 'screen', checker):
                return super().refine()
        finally:
            self.feasibility_stats = dict(checker.stats,
                rejection_cache_entries=len(checker.rejections),
                kernel=dict(self.analyzer.early_stats))


def run(ds, *, mode='JOINT', watchdog_s=None, skip_internal=True):
    if mode not in ('MRR','JOINT'):
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
        evaluation_counter_scope='same logical candidate sequence; refinement stops on a '
            'definite inherited-checker rejection; full and witness caches are distinct')
    return result
