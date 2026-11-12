"""Independent arithmetic replay of supplied periodic witness certificates.

No calls to model materialization, timing, RTA, framing, or scheduling code.
The CAN recurrence is the same analytical model, not an independent theorem.
Pair collision checks are exact for indefinitely repeating periodic templates.
"""
from fractions import Fraction
from itertools import combinations
from math import gcd

GRADES = {'D':'D', 'CA':'CA', 'BB':'BB', 'BAA':'BAA', 'AAAA':'AAAA'}


def response_bounds(flows):
    result = {}
    ordered = sorted(flows, key=lambda f: f['rank'])
    for j, f in enumerate(ordered):
        block = max([x['duration'] for x in ordered[j+1:]] or [0])
        length = f['duration']
        for _ in range(10000):
            following = block
            for other in ordered[:j+1]:
                following += -(-length//other['period']) * other['duration']
            if following == length:
                break
            length = following
        else:
            raise AssertionError('audit busy limit')
        values = []
        for q in range(-(-length//f['period'])):
            wait = block + q*f['duration']
            for _ in range(10000):
                following = block + q*f['duration']
                for other in ordered[:j]:
                    following += -(-(wait+2000)//other['period']) * other['duration']
                if following == wait:
                    break
                wait = following
            else:
                raise AssertionError('audit wait limit')
            values.append(wait + f['duration'] - q*f['period'])
        result[f['id']] = max(values)
    return result


def replay(demands, choices, witness):
    assert witness['status'] == 'certified'
    assert set(choices) == {d['id'] for d in demands}
    members = {}; source_flows = [[] for _ in range(4)]; total_cost = 0
    for d in demands:
        c = choices[d['id']]; grades = GRADES[c['scheme']]
        assert len(grades) == len(c['sources'])
        for slot, (grade, source) in enumerate(zip(grades, c['sources'])):
            assert 0 <= source < 4
            payload = d['payload']
            if d['format'] == 'classic':
                assert 0 <= payload <= 8
                duration = 110000 + payload * 20000
            else:
                assert d['format'] == 'fd' and 0 <= payload <= 64
                payload = min(x for x in [*range(9),12,16,20,24,32,48,64] if x >= payload)
                duration = 64000 + (10*payload + (28 if payload <= 16 else 33)) * [2000,500,500,200][source]
            key = f"{d['id']}:{slot}"
            m = dict(id=key, demand=d['id'], source=source, payload=payload,
                     period=d['period_ns'], deadline=d['deadline_ns'],
                     rank=4*d['priority_rank']+slot, duration=duration)
            members[key] = m; source_flows[source].append(m)
            total_cost += d['costs_kEUR'][grade]
    response = {}
    for flows in source_flows:
        assert len(flows) <= 2048
        assert sum((Fraction(f['duration'],f['period']) for f in flows),Fraction(0)) < 1
        response.update(response_bounds(flows))
    assert response == witness['CAN']['responses']
    assert total_cost == witness['cost_kEUR']
    assert len(members) == witness['members']
    seen = []; links = {}; slack = []; reservations = []
    for index, f in enumerate(witness['frames']):
        assert f['records'] and len(set(f['records'])) == len(f['records'])
        ms = [members[k] for k in f['records']]
        assert len({m['demand'] for m in ms}) == len(ms), 'same-demand aggregation'
        assert {m['source'] for m in ms} == {f['source']}
        assert {m['period'] for m in ms} == {f['period_ns']}
        byte_count = sum(m['payload'] + 8 for m in ms)
        assert byte_count <= 1500
        wire = (max(64, byte_count + 22) + 8) * 80
        reservation = wire + 960
        shift = reservation + 10000
        assert (f['wire_ns'], f['reserve_ns'], f['offset_ns']) == (wire,reservation,shift)
        assert f['ready_ns'] == max(response[m['id']] for m in ms)
        assert f['latest_ns'] == min(m['deadline'] for m in ms)-shift-wire
        assert type(f['start_ns']) is int and f['start_ns'] >= f['ready_ns']
        assert reservation <= f['period_ns']
        end = f['start_ns'] + shift + wire
        for m in ms:
            assert end <= m['deadline'], 'deadline violated'
            slack.append(m['deadline']-end)
        for link, begin in [(('up',f['source']), f['start_ns']), (('down',0),f['start_ns']+shift)]:
            links.setdefault(link,[]).append((begin,reservation,f['period_ns'],index))
        seen.extend(f['records']); reservations.append(reservation/f['period_ns'])
    assert sorted(seen) == sorted(members), 'missing or repeated delivery'
    pair_count = 0
    for intervals in links.values():
        for a,b in combinations(intervals,2):
            spacing = gcd(a[2],b[2])
            forward = (b[0]-a[0]) % spacing
            assert forward >= a[1] and spacing-forward >= b[1], 'periodic collision'
            pair_count += 1
    return dict(status='passed', members=len(members), frames=len(witness['frames']),
                min_slack_ns=min(slack), periodic_pairs=pair_count,
                output_utilization=sum(reservations), cost_kEUR=total_cost)
