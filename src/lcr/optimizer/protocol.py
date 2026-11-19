"""CAN priority blocks and identifier checks for the vehicle dataset."""
from lcr.network_model import model
from lcr.network_model.audit import replay as replay_timing

OFFSETS = (0,64,105,211)


def id_map(ds,choices):
    result={}
    for d in ds:
        if type(d['merged_id']) is not int or d['merged_id']<1 or d['priority_rank']!=d['merged_id']-1:
            raise ValueError('priority must equal merged-table rank')
        for slot in range(len(model.SCHEMES[choices[d['id']]['scheme']])):
            value=4*(d['merged_id']-1)+slot+1
            if not 0<=value<2048:
                raise ValueError('11-bit global CAN ID exhausted')
            result[f"{d['id']}:{slot}"]=value
    if len(set(result.values()))!=len(result):
        raise ValueError('duplicate global CAN ID')
    return result


def audit(ds,choices,witness):
    # Independent explicit integer ID construction, not via id_map().
    expected={}
    for d in ds:
        g=d['merged_id']
        assert d['priority_rank']==g-1
        assert g==OFFSETS[d['original_source']]+d['original_id']
        for j in range(1,len(model.SCHEMES[choices[d['id']]['scheme']])+1):
            identifier=4*g-4+j
            assert 0<=identifier<=2047
            expected[f"{d['id']}:{j-1}"]=identifier
    assert witness['can_ids']==expected
    assert len(set(expected.values()))==len(expected)
    return dict(replay_timing(ds,choices,witness),global_CAN_IDs_checked=len(expected))
