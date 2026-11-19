"""Skip CAN-rejected count-equivalent placements in one refinement round.

Each demand has an exclusive four-priority block. All of its members share
format, payload, T and D; C depends on source, not grade/slot. With other
demands fixed, placements with the same counts per source therefore have the
same CAN ordered timing sequence up to renaming that demand's members. Every
busy/job/wait recurrence and singleton deadline test corresponds exactly.

Only a recorded CAN capacity/deadline rejection prunes a group. Neither a CAN
pass, a TSN failure nor UNKNOWN prunes anything. Records are cleared whenever
the certified parent changes. All CAN-passing slot permutations still reach
the original frame constructor in the original order.
"""
from collections import Counter
from copy import deepcopy
from time import monotonic
from unittest.mock import patch

from lcr.network_model import model
from . import grouped_can, refinement_early_exit as early

VERSION = 'refinement-CAN-count-group-rejection-v1'


class CountGroups:
    """Round-local rejection evidence; immutable input/timing context required."""
    def __init__(self, ds):
        self.lookup = {d['id']:d for d in ds}
        self.parent = None
        self.rejections = {}
        self.stats = Counter()

    def prepare(self, parent):
        if parent is self.parent:
            return
        # Assert the actual members obey the priority-block and identical-copy
        # contract. Grouping is intentionally restricted to this model.
        for m in parent['members'].values():
            d = self.lookup[m['demand']]
            if (m['priority'] != 4*d['priority_rank']+m['slot']
                    or not 0 <= m['slot'] < 4
                    or m['period_ns'] != d['period_ns']
                    or m['deadline_ns'] != d['deadline_ns']
                    or m['format'] != d['format']
                    or m['payload'] != model.wire_payload(d['payload'],d['format'])
                    or m['c_ns'] != model.can_ns(d['payload'],d['format'],m['source'])):
                raise ValueError('count grouping requires homogeneous copies in exclusive priority blocks')
        ranks = [d['priority_rank'] for d in self.lookup.values()]
        if any(type(r) is not int for r in ranks) or len(set(ranks)) != len(ranks):
            raise ValueError('count grouping requires distinct integer priority blocks')
        self.parent = parent
        self.rejections.clear()
        self.stats['parent_resets'] += 1

    def lookup_rejection(self, parent, changed, choice):
        self.prepare(parent)
        sources = choice['sources']
        if (len(sources) != len(model.SCHEMES[choice['scheme']])
                or any(type(s) is not int or s not in range(4) for s in sources)):
            raise ValueError('invalid group placement')
        key = (changed,tuple(sources.count(s) for s in range(4)))
        self.stats['logical_candidates'] += 1
        evidence = self.rejections.get(key)
        if evidence is None:
            self.stats['evaluated_candidates'] += 1
            return key,None
        self.stats['pruned_candidates'] += 1
        self.stats['pruned:'+evidence['stage']] += 1
        return key,dict(stage='CAN_count_group_rejected',count_group=key,
                        representative_stage=evidence['stage'])

    def remember(self, key, choice, result):
        if result is None:
            self.stats['unknown_not_cached'] += 1
            return
        stage = result['stage']
        if stage not in ('CAN_capacity','CAN_deadline'):
            self.stats['passes_not_pruned'] += 1
            return
        # Store only rejection evidence, never responses that could be mistaken
        # for a complete certificate of another slot arrangement.
        evidence = dict(stage=stage,scheme=choice['scheme'],sources=tuple(choice['sources']))
        if stage == 'CAN_deadline':
            w = result['rejection_witness']
            m = result['members'][w['member']]
            evidence['witness'] = dict(w,source=m['source'],demand=m['demand'],
                deadline_ns=m['deadline_ns'],singleton_budget_ns=early.response_budget(m))
        else:
            evidence['loads'] = result.loads
        self.rejections[key] = evidence
        self.stats['recorded_groups'] += 1
        self.stats['recorded:'+stage] += 1
        self.stats['peak_groups'] = max(self.stats['peak_groups'],len(self.rejections))


class Runner(early.Runner):
    def __init__(self, ds, watchdog_s, skip_internal):
        super().__init__(ds,watchdog_s,skip_internal)
        self.count_groups = CountGroups(ds)

    def candidate_screen(self, choice, bound, changed, phase, action):
        if phase != 'refine':
            return super().candidate_screen(choice,bound,changed,phase,action)
        self.guard()
        began = monotonic()
        key, rejected = self.count_groups.lookup_rejection(bound,changed,choice[changed])
        self.times['count_group_lookup:refine'] += monotonic()-began
        if rejected is not None:
            # Retain logical candidate counts/order for comparison. Evaluator
            # counters count only the checks that actually execute.
            self.trial_count += 1
            self.counts['screen:refine'] += 1
            return rejected
        result = super().candidate_screen(choice,bound,changed,phase,action)
        began = monotonic()
        self.count_groups.remember(key,choice[changed],result)
        self.times['count_group_record:refine'] += monotonic()-began
        return result


def run(ds, *, mode='JOINT', watchdog_s=None, skip_internal=True):
    if mode not in ('MRR','JOINT'):
        raise ValueError(mode)
    saved = deepcopy(ds)
    with patch.object(model,'can_analysis',grouped_can.can_analysis):
        runner = Runner(ds,watchdog_s,skip_internal)
        result = runner.execute(mode)
    assert ds == saved
    result.update(can_backend=VERSION,recurrence_reuse=dict(runner.analyzer.stats),
        repair_recurrence_reuse=runner.repair_stats or dict(runner.analyzer.stats),
        refine_recurrence_reuse=runner.refine_stats,
        domain_reuse=dict(runner.reduction_scorer.stats),
        reduction_score_stages=dict(runner.reduction_scorer.stages),
        feasibility_screen=runner.feasibility_stats,
        count_group_pruning=dict(runner.count_groups.stats),
        evaluation_counter_scope='trial_count and phase screen counts retain all logical candidates; '
            'evaluations/domain checks exclude CAN-rejected count-group permutations; '
            'all CAN passes, TSN trials, first-phase rules and accepted order retained')
    return result
