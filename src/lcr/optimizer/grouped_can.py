"""Exact period grouping for the existing zero-jitter CAN recurrence.

This is an isolated alternative implementation, not a new schedulability test.
For any fixed integer t and priority prefix P:
  sum_{i in P} ceil(t/T_i) C_i = sum_T ceil(t/T) sum_{i in P:T_i=T} C_i.
Apply separately to hp (waiting) and hep (busy period). Blocking, recurrence
initialization, all q instances, and external guards are preserved.
Integer arithmetic makes regrouping exact, without rounding or a new budget.
Per-message jitter would require a different grouping key and is not supported
by this model; this implementation must not be reused as a jitter-aware test.
"""
from fractions import Fraction

from lcr.network_model.model import BIT_NS


def can_analysis(members, guard=lambda: None):
    responses = {}
    loads = {}
    detail = {}
    for source in range(4):
        fs = sorted((m for m in members.values() if m['source'] == source),
                    key=lambda m: m['priority'])
        if len(fs) > 2048:
            return dict(status='CAN_identifier_capacity', source=source)
        util = sum((Fraction(m['c_ns'], m['period_ns']) for m in fs), Fraction(0))
        loads[source] = float(util)
        if util >= 1:
            return dict(status='CAN_capacity_screen', source=source, utilization=loads)
        prefix = {}
        for index, m in enumerate(fs):
            guard()
            hp = tuple(prefix.items())
            period = m['period_ns']
            prefix[period] = prefix.get(period, 0) + m['c_ns']
            hep = tuple(prefix.items())
            blocking = max((x['c_ns'] for x in fs[index + 1:]), default=0)
            busy = m['c_ns']
            while True:
                guard()
                nxt = blocking + sum(((busy + t - 1) // t) * duration for t, duration in hep)
                if nxt == busy:
                    break
                busy = nxt
            count = (busy + period - 1) // period
            worst_response = 0
            for q in range(count):
                w = blocking + q * m['c_ns']
                while True:
                    guard()
                    nxt = blocking + q * m['c_ns'] + sum(
                        ((w + BIT_NS + t - 1) // t) * duration for t, duration in hp)
                    if nxt == w:
                        break
                    w = nxt
                worst_response = max(worst_response, w + m['c_ns'] - q * period)
            responses[m['id']] = worst_response
            detail[m['id']] = dict(blocking_ns=blocking, jobs=count, busy_ns=busy)
    return dict(status='ok', responses=responses, utilization=loads, details=detail)
