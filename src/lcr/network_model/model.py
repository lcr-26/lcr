"""Analytical pilot, NOT the frozen dual-CAN LCR or a physical simulator.

Four FD-capable buses; demand format is fixed; source choice changes FD C.
Zero external jitter, fixed-priority nonpreemptive CAN, periodic TSN templates.
FD occupation follows Luo Eq.(17), not a new ISO bit-level timing proof.
TSN encapsulation follows the existing Yan-style L+8 model, not full AVTP.
All times are integer ns. Successful templates repeat indefinitely; no LCM
expansion or relaxation of the shared sink link is used by the constructor.
"""
from collections import defaultdict
from fractions import Fraction
from math import gcd
from time import monotonic

SCHEMES = {'D': ('D',), 'CA': ('C', 'A'), 'BB': ('B', 'B'),
           'BAA': ('B', 'A', 'A'), 'AAAA': ('A',) * 4}
DLC = (*range(9), 12, 16, 20, 24, 32, 48, 64)
ARBITRATION_BPS = 500_000
DATA_BPS = (500_000, 2_000_000, 2_000_000, 5_000_000)
SWITCH_NS = 10_000
BIT_NS = 2_000


class AnalysisLimit(Exception):
    pass


def wire_payload(payload, fmt):
    if type(payload) is not int or payload < 0:
        raise ValueError('integer nonnegative payload required')
    if fmt == 'classic' and payload <= 8:
        return payload
    if fmt == 'fd' and payload <= 64:
        return next(n for n in DLC if n >= payload)
    raise ValueError('unsupported format/payload')


def can_ns(payload, fmt, domain):
    if type(domain) is not int or not 0 <= domain < 4:
        raise ValueError('invalid source domain')
    length = wire_payload(payload, fmt)
    if fmt == 'classic':
        return (55 + 10 * length) * BIT_NS  # Davis classic bound incl. IFS
    return 32 * BIT_NS + ((28 if length <= 16 else 33) + 10 * length) * (
        1_000_000_000 // DATA_BPS[domain])


def tsn_ns(payloads):
    payload = sum(length + 8 for length in payloads)
    if not payloads or payload > 1500:
        raise ValueError('nonempty TSN frame of at most 1500 B payload required')
    wire = (max(64, 22 + payload) + 8) * 80
    return wire + 12 * 80, wire


def cost(demand, scheme):
    return sum(demand['costs_kEUR'][a] for a in SCHEMES[scheme])


def cheapest(demand):
    return min(SCHEMES, key=lambda k: (cost(demand, k), len(SCHEMES[k]), k))


def configuration(demands, scheme_policy, placement):
    """Two diagnostic placements only; not an implementation of LCR.

    'balanced' uses heterogeneous projected source utilization, not raw bytes.
    A fixed demand format never changes to FD to exploit a faster data rate.
    """
    if scheme_policy not in ('D', 'cheapest') or placement not in ('original', 'balanced'):
        raise ValueError('unsupported diagnostic policy')
    result = {}
    jobs = []
    for d in demands:
        scheme = 'D' if scheme_policy == 'D' else cheapest(d)
        result[d['id']] = dict(scheme=scheme, sources=[d['original_source']] * len(SCHEMES[scheme]))
        for slot in range(len(SCHEMES[scheme])):
            jobs.append((d, slot))
    if placement == 'balanced':
        load = [Fraction(0) for _ in DATA_BPS]
        jobs.sort(key=lambda x: (-Fraction(max(can_ns(x[0]['payload'], x[0]['format'], s)
                                               for s in range(4)), x[0]['period_ns']),
                                 x[0]['priority_rank'], x[1]))
        for d, slot in jobs:
            contributions = [Fraction(can_ns(d['payload'], d['format'], s), d['period_ns'])
                             for s in range(4)]
            src = min(range(4), key=lambda s: (load[s] + contributions[s], s))
            result[d['id']]['sources'][slot] = src
            load[src] += contributions[src]
    return result


def materialize(demands, choices):
    if len({d['id'] for d in demands}) != len(demands) or set(choices) != {d['id'] for d in demands}:
        raise ValueError('all demands must appear exactly once')
    if len({d['priority_rank'] for d in demands}) != len(demands):
        raise ValueError('distinct fixed demand priority ranks required')
    members = {}
    for d in demands:
        if not 0 < d['deadline_ns'] <= d['period_ns']:
            raise ValueError('pilot supports zero-phase constrained deadlines only')
        choice = choices[d['id']]
        grades = SCHEMES[choice['scheme']]
        if len(choice['sources']) != len(grades):
            raise ValueError('member count mismatch')
        for slot, (grade, src) in enumerate(zip(grades, choice['sources'])):
            key = f"{d['id']}:{slot}"
            members[key] = dict(id=key, demand=d['id'], slot=slot, source=src,
                period_ns=d['period_ns'], deadline_ns=d['deadline_ns'],
                payload=wire_payload(d['payload'], d['format']), format=d['format'],
                c_ns=can_ns(d['payload'], d['format'], src),
                priority=d['priority_rank'] * 4 + slot, grade=grade,
                cost_kEUR=d['costs_kEUR'][grade])
    return members


def can_analysis(members, guard=lambda: None, limit=10000):
    """Davis multi-instance recurrence, zero jitter, nominal bit time 2 us.

    Same recurrence as the existing reference checker; independently replayed
    by audit.py. Iteration exhaustion is UNKNOWN, not unschedulability.
    """
    responses = {}; loads = {}; detail = {}
    for source in range(4):
        fs = sorted((m for m in members.values() if m['source'] == source), key=lambda m: m['priority'])
        if len(fs) > 2048:
            return dict(status='CAN_identifier_capacity', source=source)
        util = sum((Fraction(m['c_ns'], m['period_ns']) for m in fs), Fraction(0))
        loads[source] = float(util)
        if util >= 1:
            return dict(status='CAN_capacity_screen', source=source, utilization=loads)
        for index, m in enumerate(fs):
            guard()
            hp, hep = fs[:index], fs[:index+1]
            blocking = max((x['c_ns'] for x in fs[index+1:]), default=0)
            busy = m['c_ns']
            for _ in range(limit):
                guard()
                nxt = blocking + sum(((busy + x['period_ns']-1)//x['period_ns']) * x['c_ns'] for x in hep)
                if nxt == busy:
                    break
                busy = nxt
            else:
                raise AnalysisLimit('CAN_busy_iteration_limit')
            count = (busy + m['period_ns']-1)//m['period_ns']
            if count > limit:
                raise AnalysisLimit('CAN_job_limit')
            values = []
            for q in range(count):
                w = blocking + q*m['c_ns']
                for _ in range(limit):
                    guard()
                    nxt = blocking + q*m['c_ns'] + sum(
                        ((w + BIT_NS + x['period_ns']-1)//x['period_ns'])*x['c_ns'] for x in hp)
                    if nxt == w:
                        break
                    w = nxt
                else:
                    raise AnalysisLimit('CAN_wait_iteration_limit')
                values.append(w + m['c_ns'] - q*m['period_ns'])
            responses[m['id']] = max(values)
            detail[m['id']] = dict(blocking_ns=blocking, jobs=count, busy_ns=busy)
    return dict(status='ok', responses=responses, utilization=loads, details=detail)


def frames_for(members, responses, aggregate):
    groups = []; pools = defaultdict(list)
    ordered = sorted(members.values(), key=lambda m: (m['source'], m['period_ns'], m['priority']))

    def frame(keys):
        ms = [members[k] for k in keys]
        reserve, wire = tsn_ns([m['payload'] for m in ms])
        # Keep the existing conservative hop convention: upstream IFG precedes
        # the switch delay in the chosen fixed no-wait forwarding offset.
        offset = reserve + SWITCH_NS
        return dict(records=list(keys), source=ms[0]['source'], period_ns=ms[0]['period_ns'],
            ready_ns=max(responses[m['id']] for m in ms),
            latest_ns=min(m['deadline_ns'] for m in ms) - offset - wire,
            reserve_ns=reserve, wire_ns=wire, offset_ns=offset)

    for m in ordered:
        candidates = pools[m['source'], m['period_ns']] if aggregate else []
        chosen = None
        for i in candidates:
            keys = groups[i]['records']
            if any(members[k]['demand'] == m['demand'] for k in keys):
                continue
            if sum(members[k]['payload']+8 for k in keys) + m['payload']+8 > 1500:
                continue
            trial = frame(keys + [m['id']])
            if trial['ready_ns'] <= trial['latest_ns']:
                chosen = i; groups[i] = trial; break
        if chosen is None:
            pools[m['source'], m['period_ns']].append(len(groups))
            groups.append(frame([m['id']]))
    return groups


def next_pair_start(start, length, period, old_start, old_length, old_period):
    """Earliest start >= start avoiding one indefinitely periodic interval."""
    g = gcd(period, old_period)
    if length + old_length > g:
        return None
    delta = (start - old_start) % g
    if delta < old_length:
        return start + old_length - delta
    if delta > g - length:
        return start + g - delta + old_length
    return start


def schedule(frames, guard=lambda: None):
    placed = []; jumps = 0
    for f in sorted(frames, key=lambda f: (f['latest_ns'], f['ready_ns'], f['records'])):
        if f['reserve_ns'] > f['period_ns']:
            return dict(status='TSN_self_overlap', failed_frame=f)
        start = f['ready_ns']
        if start > f['latest_ns']:
            return dict(status='TSN_empty_window', failed_frame=f)
        while start <= f['latest_ns']:
            guard(); nxt = start
            for old in placed:
                pairs = [(f['offset_ns'], old['offset_ns'], 'down')]
                if f['source'] == old['source']:
                    pairs.append((0, 0, 'up'))
                for shift, old_shift, link in pairs:
                    value = next_pair_start(start + shift, f['reserve_ns'], f['period_ns'],
                        old['start_ns'] + old_shift, old['reserve_ns'], old['period_ns'])
                    if value is None:
                        return dict(status='TSN_periodic_pair_conflict', failed_frame=f,
                                    conflict=old['records'], link=link, jumps=jumps)
                    nxt = max(nxt, value-shift)
            if nxt == start:
                placed.append(dict(f, start_ns=start)); break
            start = nxt; jumps += 1
        else:
            return dict(status='TSN_window_exhausted', failed_frame=f, placed=len(placed), jumps=jumps)
    return dict(status='certified', frames=placed, jumps=jumps)


def evaluate(demands, choices, aggregate, seconds=30.):
    began = monotonic()

    def guard():
        if monotonic()-began > seconds:
            raise AnalysisLimit('diagnostic_wall_limit')

    members = materialize(demands, choices)
    base = dict(cost_kEUR=sum(m['cost_kEUR'] for m in members.values()), members=len(members),
                aggregate=aggregate)
    try:
        can = can_analysis(members, guard)
        base['CAN'] = can
        if can['status'] != 'ok':
            return dict(base, status=can['status'], elapsed_s=monotonic()-began)
        reserve, wire = tsn_ns([0])
        failures = [k for k, m in members.items()
                    if can['responses'][k] + reserve + SWITCH_NS + wire > m['deadline_ns']]
        if failures:
            return dict(base, status='CAN_deadline_screen', failed_members=failures,
                        elapsed_s=monotonic()-began)
        frames = frames_for(members, can['responses'], aggregate)
        base.update(frame_count=len(frames),
            output_utilization=sum(f['reserve_ns']/f['period_ns'] for f in frames),
            min_frame_window_ns=min(f['latest_ns']-f['ready_ns'] for f in frames))
        value = schedule(frames, guard)
        return dict(base, **value, elapsed_s=monotonic()-began)
    except AnalysisLimit as exc:
        return dict(base, status='unknown', limit=str(exc), elapsed_s=monotonic()-began)
