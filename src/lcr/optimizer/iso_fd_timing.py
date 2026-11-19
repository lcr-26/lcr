"""CANASTA (TVT 2023), Section IV, Eqs.12-17, standard ISO FD occupation.

Scope: successful 11-bit data frames, ideal configured bit times, BRS=1
(equal nominal/data rates are permitted), no retransmission or error-passive
suspend transmission. Occupation includes the three-bit intermission once.
This is a field-count bound, NOT a hardware/oscillator-tolerance certificate.
"""
from contextlib import contextmanager
from unittest.mock import patch

from lcr.network_model import model
from . import can_rate_context

VERSION = 'iso-fd-canasta-v1'


def field_counts(payload):
    """BRS and CRC delimiter together occupy one nominal and one data bit.

    Use CANASTA Table IV and Eqs.12-16, not a fitted overhead constant.
    Nominal: 17 header bits, 3 stuff bits, up to 2 ACK slots, 1 ACK
    delimiter, 7 EOF and 3 intermission bits. Data stuffing uses the paper's
    conservative cross-boundary residual count, rather than ID-specific bits.
    BRS is accounted as nominal and CRC delimiter as data only in this sum;
    neither physical hybrid bit individually has that full bit duration.
    """
    length = model.wire_payload(payload, 'fd')
    crc = 17 if length <= 16 else 21
    nb, nd, nf, nu = 17, 5+8*length, 4+crc+1, 13
    sb = (nb-2)//4
    rb = nb-1-4*sb
    sd = (rb+nd-1)//4
    fixed_stuff = (nf+3)//4
    crc_field = 4 + crc + fixed_stuff + 1
    return dict(wire_payload=length, nominal_bits=nb+sb+nu,
                data_bits=nd+sd+nf+fixed_stuff,
                nb=nb,nd=nd,nf=nf,nu=nu,sb=sb,rb=rb,sd=sd,
                crc_sequence_bits=crc, stuff_count_and_parity_bits=4,
                fixed_stuff_bits=fixed_stuff, crc_field_bits=crc_field,
                intermission_bits=3)


def occupation_ns(payload, fmt, source, profile=can_rate_context.PROFILES[0]):
    can_rate_context.validate(profile)
    if type(source) is not int or not 0 <= source < 4:
        raise ValueError('invalid source domain')
    length = model.wire_payload(payload, fmt)
    nominal_ns, data_ns = 10**9 // profile[1], 10**9 // profile[2][source]
    if fmt == 'classic':
        return (55 + 10*length) * nominal_ns
    fields = field_counts(length)
    return fields['nominal_bits']*nominal_ns + fields['data_bits']*data_ns


def independent_occupation(demand, source, profile):
    """Audit arithmetic independent of field_counts/occupation_ns."""
    payload = demand['payload']
    nominal_ns = 10**9 // profile[1]
    if demand['format'] == 'classic':
        assert type(payload) is int and 0 <= payload <= 8
        return payload, (55 + 10*payload)*nominal_ns
    assert demand['format'] == 'fd' and type(payload) is int and 0 <= payload <= 64
    length = min(x for x in [0,1,2,3,4,5,6,7,8,12,16,20,24,32,48,64] if x >= payload)
    return length, 33*nominal_ns + (10*length + (35 if length <= 16 else 40))*(10**9 // profile[2][source])


@contextmanager
def use_iso(profile=can_rate_context.PROFILES[0]):
    """Apply the paper’s ISO CAN FD timing model and independent checker."""
    with can_rate_context.use_rates(profile), \
         patch.object(model, 'can_ns', lambda p,f,s: occupation_ns(p,f,s,profile)), \
         patch.object(can_rate_context, 'independent_occupation', independent_occupation):
        yield
