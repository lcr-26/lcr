"""Deadline repair selects a cause-relevant priority prefix, without rescans.

For homogeneous copies in one fixed source, reducing a DIFFERENT lower-priority
demand leaves at least one equal-duration copy on that bus. A violating member's
own C/T, HP set, and maximum LP blocking are therefore unchanged. Such an edit
cannot fix that member's CAN/singleton deadline violation. Prioritize the first
violating member on each bus by querying only demands at or above its priority.

The exclusion is relative to the current witness, not permanent infeasibility
of an edit. Deadline ranking uses removed interference in that witness's already
computed busy interval, with exact ceil(L/T) weights for HP messages. Capacity
and TSN failures retain the utilization queue. A response/blocking change after
editing still needs the original complete checker; the ranking is not an exact
prediction. No new time, candidate, round, or numeric threshold is introduced.
The inherited one-sweep refinement permits cross-domain member deployment.
"""
from bisect import bisect_left, bisect_right
from collections import Counter
from fractions import Fraction
import heapq
from unittest.mock import patch

from lcr.network_model import model
from . import agenda_sweep

VERSION = 'cause-relevant-CAN-critical-window-queue-v3-no-baseline'


def minimum(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


class PrefixMinimum:
    """Point updates and minimum over [0,end); no priority-range enumeration."""
    def __init__(self, size):
        self.base = 1
        while self.base < size:
            self.base *= 2
        self.tree = [None]*(2*self.base)

    def set(self, index, value):
        node = self.base+index
        self.tree[node] = value
        while node > 1:
            node //= 2
            self.tree[node] = minimum(self.tree[2*node], self.tree[2*node+1])

    def prefix(self, end):
        left, right, result = self.base, self.base+end, None
        while left < right:
            if left % 2:
                result = minimum(result, self.tree[left])
                left += 1
            if right % 2:
                right -= 1
                result = minimum(result, self.tree[right])
            left //= 2
            right //= 2
        return result


class Agenda:
    def __init__(self, ds, choices, current, *, causal=True, own_blocking=True, deadline_window=True):
        self.current, self.causal, self.own_blocking = current, causal, own_blocking
        self.deadline_window = deadline_window and causal
        self.stats = Counter()
        self.lookup = {d['id']: d for d in ds}
        self.groups = [[] for _ in range(4)]
        self.entries, self.locations, self.sources = {}, {}, {}
        for d in ds:
            src = choices[d['id']]['sources']
            if not src or len(set(src)) != 1:
                raise ValueError('fixed single-source repair required')
            self.groups[src[0]].append(d['id'])
            self.sources[d['id']] = src[0]
        for group in self.groups:
            group.sort(key=lambda demand: self.lookup[demand]['priority_rank'])
        self.ranks = [[self.lookup[demand]['priority_rank'] for demand in group] for group in self.groups]
        self.period_groups, self.period_trees, self.period_locations, self.period_ranks = {}, {}, {}, {}
        if self.deadline_window:
            for source, group in enumerate(self.groups):
                for demand in group:
                    key = source, self.lookup[demand]['period_ns']
                    self.period_groups.setdefault(key, []).append(demand)
            for key, group in self.period_groups.items():
                self.period_trees[key] = PrefixMinimum(len(group))
                self.period_ranks[key] = [self.lookup[d]['priority_rank'] for d in group]
                for index, demand in enumerate(group):
                    self.period_locations[demand] = (key, index)
        self.member_counts = {}
        # Every other demand retains at least one copy with unchanged C. Thus
        # external LP maximum blocking is constant throughout fixed-source repair.
        self.external_blocking = {}
        for source, group in enumerate(self.groups):
            following = 0
            for demand in reversed(group):
                self.external_blocking[demand] = following
                d = self.lookup[demand]
                following = max(following, model.can_ns(d['payload'], d['format'], source))
        self.trees = [PrefixMinimum(len(group)) for group in self.groups]
        for source, group in enumerate(self.groups):
            for index, demand in enumerate(group):
                self.locations[demand] = (source, index)
                self.add(self.lookup[demand], choices[demand])

    def add(self, d, before):
        source, index = self.locations[d['id']]
        sources = before['sources']
        if not sources or any(s != source for s in sources):
            raise ValueError('repair deployment changed')
        count, best = len(sources), {}
        self.member_counts[d['id']] = count
        for scheme, grades in model.SCHEMES.items():
            target = len(grades)
            if target >= count or model.cost(d, scheme) > model.cost(d, 'D'):
                continue
            if target not in best or (model.cost(d, scheme), scheme) < (model.cost(d, best[target]), best[target]):
                best[target] = scheme
        entries = []
        for target, scheme in best.items():
            delta = model.cost(d, scheme)-model.cost(d, before['scheme'])
            relief = Fraction((count-target)*model.can_ns(d['payload'], d['format'], source), d['period_ns'])
            key = (Fraction(max(0, delta))/relief, delta, d['merged_id'], scheme)
            heapq.heappush(entries, (key, d['id'], scheme))
            self.stats['enqueued'] += 1
        self.entries[d['id']] = entries
        self.trees[source].set(index, entries[0] if entries else None)
        self.set_period(d['id'])

    def set_period(self, demand):
        if not self.deadline_window:
            return
        d = self.lookup[demand]
        source = self.sources[demand]
        duration = model.can_ns(d['payload'], d['format'], source)
        entries = []
        for entry in self.entries[demand]:
            key, _, scheme = entry
            delta = key[1]
            relief = (self.member_counts[demand]-len(model.SCHEMES[scheme]))*duration
            entries.append(((Fraction(max(0, delta))/relief, delta, d['merged_id'], scheme), demand, scheme))
        key, index = self.period_locations[demand]
        self.period_trees[key].set(index, min(entries, default=None))

    def advance(self, d, choice):
        self.stats['obsolete_discarded'] += len(self.entries[d['id']])
        self.add(d, choice)

    def pop(self, hot):
        bound = self.current()
        cutoffs, witnesses = {}, {}
        if self.causal and bound['stage'] == 'CAN_deadline':
            for violation in bound['violations']:
                member = bound['members'][violation['member']]
                source, rank = member['source'], member['priority']//4
                cutoffs[source] = min(cutoffs.get(source, rank), rank)
                if source not in witnesses or member['priority'] < witnesses[source]['priority']:
                    witnesses[source] = member
        def query(source):
            count = len(self.groups[source])
            if source in cutoffs:
                if self.own_blocking:
                    member = witnesses[source]
                    count = bisect_left(self.ranks[source], cutoffs[source])
                    if self.deadline_window:
                        result = None
                        length = bound['details'][member['id']]['busy_ns']
                        for (s, period), group in self.period_groups.items():
                            if s != source:
                                continue
                            # Homogeneous period => one common busy-equation
                            # multiplicity; query one minimum for the HP prefix.
                            end = bisect_left(self.period_ranks[s, period], cutoffs[source])
                            entry = self.period_trees[s, period].prefix(end)
                            self.stats['busy_window_group_queries'] += 1
                            if entry is not None:
                                key, demand, scheme = entry
                                factor = (length+period-1)//period
                                scaled = ((key[0]/factor, key[1], key[2], key[3]), demand, scheme)
                                result = minimum(result, scaled)
                    else:
                        result = self.trees[source].prefix(count)
                    old_blocking = bound['details'][member['id']]['blocking_ns']
                    for entry in self.entries[member['demand']]:
                        target = len(model.SCHEMES[entry[2]])
                        removed = target <= member['slot']
                        if not removed:
                            blocking = max(self.external_blocking[member['demand']],
                                member['c_ns'] if target > member['slot']+1 else 0)
                            assert blocking <= old_blocking
                            if blocking == old_blocking:
                                self.stats['own_unchanged_witness_skips'] += 1
                                continue
                        if self.deadline_window:
                            key, demand, scheme = entry
                            if removed:
                                # Own witness disappears; use removed busy work
                                # as a ranking proxy, not an exact WCRT change.
                                period = member['period_ns']
                                relief = (self.member_counts[demand]-target)*member['c_ns']*((length+period-1)//period)
                            else:
                                relief = old_blocking-blocking
                            delta = key[1]
                            entry = ((Fraction(max(0, delta))/relief, delta,
                                      self.lookup[demand]['merged_id'], scheme), demand, scheme)
                        result = minimum(result, entry)
                    self.stats['prefix_queries'] += 1
                    return result
                count = bisect_right(self.ranks[source], cutoffs[source])
                self.stats['prefix_queries'] += 1
            else:
                self.stats['full_queries'] += 1
            return self.trees[source].prefix(count)
        active = [(entry, source) for source in sorted(hot) if (entry := query(source)) is not None]
        if not active and cutoffs:
            # No relevant evaluated action remains, e.g. earlier UNKNOWN. Do
            # not silently delete unrelated messages or assert infeasibility.
            self.stats['no_relevant_action'] += 1
            return None
        if not active:
            active = [(entry, source) for source in range(4) if (entry := query(source)) is not None]
        if not active:
            return None
        entry, source = min(active)
        _, demand, scheme = entry
        # Own-demand filtering can select a non-head entry of this <=3-item heap.
        actual = next(e for e in self.entries[demand] if e[1:] == entry[1:])
        self.entries[demand].remove(actual)
        heapq.heapify(self.entries[demand])
        _, index = self.locations[demand]
        self.trees[source].set(index, self.entries[demand][0] if self.entries[demand] else None)
        self.set_period(demand)
        self.stats['selected'] += 1
        if source in cutoffs:
            assert self.lookup[demand]['priority_rank'] <= cutoffs[source]
            self.stats['cause_relevant_selections'] += 1
        return demand, scheme


class Runner(agenda_sweep.Runner):
    def __init__(self, ds, watchdog_s, skip_internal, *, causal=True, own_blocking=True, deadline_window=True, **kwargs):
        super().__init__(ds, watchdog_s, skip_internal, **kwargs)
        self.causal, self.own_blocking, self.priority_stats = causal, own_blocking, {}
        self.deadline_window = deadline_window

    def repair(self, free):
        original = self.ev.screen
        current = {}
        holder = {}
        def tracked(choices, **kwargs):
            result = original(choices, **kwargs)
            current['bound'] = result
            return result
        def create(ds, choices):
            holder['agenda'] = Agenda(ds, choices, lambda: current['bound'], causal=self.causal,
                                      own_blocking=self.own_blocking, deadline_window=self.deadline_window)
            return holder['agenda']
        try:
            with patch.object(self.ev, 'screen', tracked), patch.object(agenda_sweep, 'Agenda', create):
                return super().repair(free)
        finally:
            if 'agenda' in holder:
                self.priority_stats = dict(holder['agenda'].stats)


def run(ds, *, causal=True, own_blocking=True, deadline_window=True, **kwargs):
    class Configured(Runner):
        def __init__(self, ds, watchdog_s, skip_internal, **options):
            super().__init__(ds, watchdog_s, skip_internal, causal=causal, own_blocking=own_blocking,
                             deadline_window=deadline_window, **options)
    with patch.object(agenda_sweep, 'Runner', Configured):
        result = agenda_sweep.run(ds, **kwargs)
    result.update(can_backend=VERSION, causal_prefix=causal, own_blocking_filter=own_blocking,
                  deadline_window=deadline_window)
    return result
