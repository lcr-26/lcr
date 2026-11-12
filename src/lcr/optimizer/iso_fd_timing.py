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
LENGTHS = (*range(9), 12, 16, 20, 24, 32, 48, 64)


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
    """Process-local adapter; keeps old model and old certificates reproducible."""
    with can_rate_context.use_rates(profile), \
         patch.object(model, 'can_ns', lambda p,f,s: occupation_ns(p,f,s,profile)), \
         patch.object(can_rate_context, 'independent_occupation', independent_occupation):
        yield


def stuffing_maximum(length, *, identifier=None, esi=0, nominal_ns=2000, data_ns=500, ack_slots=2):
    """Exact finite-state maximum over unspecified ID and all payload bitstrings.

    Only dynamic stuffing is optimized. CRC contents do not affect length
    because ISO CRC stuffing is fixed. This is an arithmetic protocol check,
    not a network simulator. A bitstring attaining the maximum is retained.
    BRS=1; no dynamic stuff bit immediately before the first fixed CRC bit.
    """
    if length not in LENGTHS or esi not in (0, 1) or ack_slots not in (1,2):
        raise ValueError('legal wire length and ESI required')
    if identifier is not None and not 0 <= identifier < 2048:
        raise ValueError('11-bit identifier required')
    id_bits = [None]*11 if identifier is None else [int(b) for b in f'{identifier:011b}']
    dlc = LENGTHS.index(length)
    bits = [0, *id_bits, 0, 0, 1, 0, 1, esi, *map(int, f'{dlc:04b}'), *([None]*(8*length))]
    # State(last bit, run length) -> (weighted stuffing, nominal count, data count, input bits).
    states = {(None, 0): (0, 0, 0, '')}
    for index, known in enumerate(bits):
        following = {}
        for (last, run), (score, nominal, data, witness) in states.items():
            for value in (0, 1) if known is None else (known,):
                count = run+1 if last == value else 1
                extra = count == 5 and index < len(bits)-1
                n, d = int(extra and index < 17), int(extra and index >= 17)
                key = (1-value, 1) if extra else (value, count)
                item = (score+n*nominal_ns+d*data_ns, nominal+n, data+d, witness+str(value))
                if key not in following or item[0] > following[key][0]:
                    following[key] = item
        states = following
    score, nominal, data, bits = max(states.values(), key=lambda item:item[0])
    crc = 17 if length <= 16 else 21
    crc_field = 4 + crc + 1 + (4+crc)//4 + 1
    base_nominal, base_data = 17+ack_slots+1+7+3, 5+8*length+crc_field
    return dict(length=length,identifier=int(bits[1:12],2),esi=esi,ack_slots=ack_slots,
                payload_bits=bits[22:],nominal_stuff=nominal,data_stuff=data,
                nominal_bits=base_nominal+nominal,data_bits=base_data+data,
                occupation_ns=base_nominal*nominal_ns+base_data*data_ns+score)


def explicit_stuffing(witness):
    """Independent streaming replay for the maximizing bitstring (no DP)."""
    length = witness['length']
    raw = ('0'+f"{witness['identifier']:011b}"+'00101'+str(witness['esi'])+
           f'{LENGTHS.index(length):04b}'+witness['payload_bits'])
    last, run = -1, 0
    counts = [0, 0]
    for i, char in enumerate(raw):
        bit = int(char)
        run = run+1 if bit == last else 1
        last = bit
        if run == 5 and i+1 < len(raw):
            counts[int(i >= 17)] += 1
            last, run = 1-bit, 1
    return tuple(counts)
