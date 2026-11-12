"""Isolated CAN rate sensitivity, with a parameterized scalar certificate replay.

The original model and audit source files remain frozen. The occupation formula
is the same analytical assumption at new rates, not a hardware timing claim.
Use independent processes: module bindings are temporarily replaced.
"""
from contextlib import contextmanager, ExitStack
from fractions import Fraction
from itertools import combinations
from math import gcd
from unittest.mock import patch

from lcr.network_model import model
from . import grouped_can, repair_refine

PROFILES = (
    ('base', 500_000, (500_000, 2_000_000, 2_000_000, 5_000_000)),
    ('arb1_data1225', 1_000_000, (1_000_000, 2_000_000, 2_000_000, 5_000_000)),
    ('arb1_data1445', 1_000_000, (1_000_000, 4_000_000, 4_000_000, 5_000_000)),
    ('arb1_data1555', 1_000_000, (1_000_000, 5_000_000, 5_000_000, 5_000_000)),
)


def validate(profile):
    _, arbitration, data = profile
    if len(data) != 4 or any(type(rate) is not int or rate <= 0 or 10**9 % rate
                             for rate in (arbitration, *data)):
        raise ValueError('four positive rates with integer-ns bit durations required')
    if any(rate < arbitration for rate in data):
        raise ValueError('data phase must not be slower than arbitration')


def independent_occupation(demand, source, profile):
    """Independent DLC/duration arithmetic; no call to model.can_ns."""
    _, arbitration, data = profile
    payload = demand['payload']
    if demand['format'] == 'classic':
        assert 0 <= payload <= 8
        return payload, (55 + payload * 10) * (10**9 // arbitration)
    assert demand['format'] == 'fd' and 0 <= payload <= 64
    length = min(x for x in [*range(9), 12, 16, 20, 24, 32, 48, 64] if x >= payload)
    return length, 32 * (10**9 // arbitration) + (10 * length + (28 if length <= 16 else 33)) * (10**9 // data[source])


def response_bounds(flows, bit_ns):
    result = {}
    ordered = sorted(flows, key=lambda f: f['rank'])
    for j, flow in enumerate(ordered):
        block = max([f['duration'] for f in ordered[j + 1:]] or [0])
        length = flow['duration']
        for _ in range(10000):
            following = block
            for other in ordered[:j + 1]:
                following += -(-length // other['period']) * other['duration']
            if following == length:
                break
            length = following
        else:
            raise AssertionError('audit busy limit')
        values = []
        for q in range(-(-length // flow['period'])):
            wait = block + q * flow['duration']
            for _ in range(10000):
                following = block + q * flow['duration']
                for other in ordered[:j]:
                    following += -(-(wait + bit_ns) // other['period']) * other['duration']
                if following == wait:
                    break
                wait = following
            else:
                raise AssertionError('audit wait limit')
            values.append(wait + flow['duration'] - q * flow['period'])
        result[flow['id']] = max(values)
    return result


def audit(ds, choices, witness, profile):
    """Scalar arithmetic replay, including unchanged periodic TSN constraints."""
    if not __debug__:
        raise RuntimeError('Certificate auditing requires Python assertions to be enabled')
    validate(profile)
    assert witness['status'] == 'certified'
    assert set(choices) == {d['id'] for d in ds}
    grades = {'D':'D', 'CA':'CA', 'BB':'BB', 'BAA':'BAA', 'AAAA':'AAAA'}
    members, ids = {}, {}
    buses = [[] for _ in range(4)]
    cost = 0
    for d in ds:
        assert d['merged_id'] == (0, 64, 105, 211)[d['original_source']] + d['original_id']
        assert d['priority_rank'] == d['merged_id'] - 1
        choice = choices[d['id']]
        assert len(grades[choice['scheme']]) == len(choice['sources'])
        for j, (grade, source) in enumerate(zip(grades[choice['scheme']], choice['sources'])):
            assert 0 <= source < 4
            payload, duration = independent_occupation(d, source, profile)
            key = f"{d['id']}:{j}"
            m = dict(id=key, demand=d['id'], source=source, payload=payload,
                     period=d['period_ns'], deadline=d['deadline_ns'],
                     rank=4*d['priority_rank']+j, duration=duration)
            members[key] = m
            buses[source].append(m)
            ids[key] = 4*d['merged_id']-3+j
            assert 0 <= ids[key] <= 2047
            cost += d['costs_kEUR'][grade]
    assert len(set(ids.values())) == len(ids)
    assert ids == witness['can_ids']
    response = {}
    for bus in buses:
        assert len(bus) <= 2048
        assert sum((Fraction(f['duration'], f['period']) for f in bus), Fraction()) < 1
        response.update(response_bounds(bus, 10**9 // profile[1]))
    assert response == witness['CAN']['responses']
    assert cost == witness['cost_kEUR'] and len(members) == witness['members']
    seen, links, slack, reservations = [], {}, [], []
    for index, frame in enumerate(witness['frames']):
        assert frame['records'] and len(set(frame['records'])) == len(frame['records'])
        ms = [members[k] for k in frame['records']]
        assert len({m['demand'] for m in ms}) == len(ms)
        assert {m['source'] for m in ms} == {frame['source']}
        assert {m['period'] for m in ms} == {frame['period_ns']}
        byte_count = sum(m['payload']+8 for m in ms)
        assert byte_count <= 1500
        wire = (max(64, byte_count+22)+8)*80
        reservation, shift = wire+960, wire+10960
        assert (frame['wire_ns'],frame['reserve_ns'],frame['offset_ns']) == (wire,reservation,shift)
        assert frame['ready_ns'] == max(response[m['id']] for m in ms)
        assert frame['latest_ns'] == min(m['deadline'] for m in ms)-shift-wire
        assert type(frame['start_ns']) is int and frame['start_ns'] >= frame['ready_ns']
        assert reservation <= frame['period_ns']
        end = frame['start_ns']+shift+wire
        for m in ms:
            assert end <= m['deadline']
            slack.append(m['deadline']-end)
        for link, begin in ((('up',frame['source']),frame['start_ns']),
                            (('down',0),frame['start_ns']+shift)):
            links.setdefault(link,[]).append((begin,reservation,frame['period_ns'],index))
        seen.extend(frame['records'])
        reservations.append(reservation/frame['period_ns'])
    assert sorted(seen) == sorted(members)
    pair_count = 0
    for intervals in links.values():
        for a,b in combinations(intervals,2):
            spacing = gcd(a[2],b[2])
            forward = (b[0]-a[0]) % spacing
            assert forward >= a[1] and spacing-forward >= b[1]
            pair_count += 1
    return dict(status='passed',members=len(members),frames=len(witness['frames']),
                min_slack_ns=min(slack),periodic_pairs=pair_count,
                output_utilization=sum(reservations),cost_kEUR=cost,global_CAN_IDs_checked=len(ids))


@contextmanager
def use_rates(profile):
    validate(profile)
    _, arbitration, data = profile
    bit_ns = 10**9 // arbitration
    def duration(payload, fmt, source):
        if type(source) is not int or not 0 <= source < 4:
            raise ValueError('invalid source domain')
        length = model.wire_payload(payload, fmt)
        if fmt == 'classic':
            return (55+10*length)*bit_ns
        return 32*bit_ns+((28 if length <= 16 else 33)+10*length)*(10**9//data[source])
    with ExitStack() as stack:
        for module, name, value in ((model,'ARBITRATION_BPS',arbitration),
            (model,'DATA_BPS',tuple(data)),(model,'BIT_NS',bit_ns),
            (model,'can_ns',duration),(grouped_can,'BIT_NS',bit_ns),
            (repair_refine,'audit',lambda ds,ch,w: audit(ds,ch,w,profile))):
            stack.enter_context(patch.object(module,name,value))
        yield
