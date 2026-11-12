"""LCR adapter and paired member-reduction control; not frozen final methods.

Same original-source cheapest start for both methods. All legal one-member
migrations off failed buses are considered in deterministic order until first
strict improvement (no fixed candidate/step cap). Revisit prevention plus strict
CAN-score descent and decreasing member count ensure finite state progression.
After CAN passes, alternative aggregation/ordering and failure-local source moves
are accepted only if fully certified. MRR shares the base constructor and all
reduction rules but disables these communication adjustments.
"""
from collections import Counter
from copy import deepcopy
from fractions import Fraction
from time import monotonic

from lcr.network_model import model
from .protocol import audit,id_map


def key(choices):
    return tuple((k,v['scheme'],tuple(v['sources'])) for k,v in sorted(choices.items()))


def score_dict(value):
    return [str(x) for x in value]


class ScreenResult(dict):
    """Public screen fields plus exact loads for a one-demand delta.

    Loads are kept out of the serialized/public fields. Parent results and
    their member records are immutable after construction.
    """
    def __init__(self, *, loads, **fields):
        super().__init__(fields)
        self.loads=tuple(loads)


def replace_choice(choices,demand,scheme,sources):
    """Copy the outer mapping and own the changed demand's source list."""
    trial=choices.copy()
    trial[demand]=dict(scheme=scheme,sources=list(sources))
    return trial


class Evaluator:
    def __init__(self,ds,guard=lambda:None):
        self.ds=ds;self.guard=guard;self.bus_cache={};self.screens=0;self.bus_checks=0
        self.cache_hits=0;self.constructs=0;self.statuses=Counter()
        self.lookup={d['id']:d for d in ds}
        self.full_screens=0;self.delta_screens=0;self.members_materialized=0

    def screen(self,choices,*,base=None,changed=None):
        """Same full checker, with an exact delta for a one-demand edit.

        When base/changed are supplied, choices must differ from that parent
        only at changed. Search callers construct that edit with replace_choice.
        All CAN response and TSN decisions retain the original implementation.
        """
        self.guard();self.screens+=1
        if (base is None)!=(changed is None):
            raise ValueError('base and changed must be supplied together')
        if base is None:
            self.full_screens+=1
            members=model.materialize(self.ds,choices)
            ids=id_map(self.ds,choices)
            loads=[Fraction() for _ in range(4)]
            for m in members.values():
                loads[m['source']]+=Fraction(m['c_ns'],m['period_ns'])
            total_cost=sum(m['cost_kEUR'] for m in members.values())
            self.members_materialized+=len(members)
        else:
            self.delta_screens+=1
            # Shallow member copies are safe: member records are never mutated.
            members=base['members'].copy();ids=base['can_ids'].copy()
            loads=list(base.loads);total_cost=base['cost_kEUR']
            old_keys=[]
            for slot in range(4):
                k=f'{changed}:{slot}'
                old=members.get(k)
                if old is not None:
                    old_keys.append(k)
                    loads[old['source']]-=Fraction(old['c_ns'],old['period_ns'])
                    total_cost-=old['cost_kEUR']
            d=self.lookup[changed]
            added=model.materialize([d],{changed:choices[changed]})
            for k in old_keys:
                if k not in added:
                    members.pop(k);ids.pop(k)
            members.update(added);ids.update(id_map([d],{changed:choices[changed]}))
            for m in added.values():
                loads[m['source']]+=Fraction(m['c_ns'],m['period_ns'])
                total_cost+=m['cost_kEUR']
            self.members_materialized+=len(added)
        util=dict(enumerate(loads))
        # Sum the utilizations of overloaded buses; equality at 1 is rejected
        # by the inherited sufficient checker. No epsilon/empirical threshold.
        cap=sum((u for u in util.values() if u>=1),Fraction())
        result=ScreenResult(loads=loads,members=members,can_ids=ids,cost_kEUR=total_cost,
                    utilization={s:float(u) for s,u in util.items()},hot=set(),responses={},details={})
        if cap:
            result.update(stage='CAN_capacity',score=(cap,Fraction()),hot={s for s,u in util.items() if u>=1})
            self.statuses[result['stage']]+=1
            return result
        for s in range(4):
            flows={k:m for k,m in members.items() if m['source']==s}
            signature=(s,tuple(sorted((k,m['c_ns'],m['period_ns'],m['priority']) for k,m in flows.items())))
            if signature in self.bus_cache:
                self.cache_hits+=1;value=self.bus_cache[signature]
            else:
                self.guard();self.bus_checks+=1
                value=model.can_analysis(flows,self.guard)
                if value['status']!='ok':raise AssertionError(value)
                self.bus_cache[signature]=value
            result['responses'].update(value['responses']);result['details'].update(value['details'])
        severity=Fraction();violations=[]
        for k,m in members.items():
            reserve,wire=model.tsn_ns([m['payload']])
            deficit=result['responses'][k]+reserve+model.SWITCH_NS+wire-m['deadline_ns']
            if deficit>0:
                severity+=Fraction(deficit,m['deadline_ns']);result['hot'].add(m['source'])
                violations.append(dict(member=k,deficit_ns=deficit,source=m['source']))
        result.update(stage='CAN_deadline' if severity else 'CAN_pass',score=(Fraction(),severity),violations=violations)
        self.statuses[result['stage']]+=1
        return result

    def construct(self,bound,aggregate=True,order='latest'):
        self.guard();self.constructs+=1
        ms=bound['members'];frames=model.frames_for(ms,bound['responses'],aggregate)
        if order=='latest':
            value=model.schedule(frames,self.guard)
        else:
            value=ordered_schedule(frames,self.guard,order)
        if value['status']=='certified':
            value.update(CAN=dict(responses=bound['responses']),can_ids=bound['can_ids'],
                members=len(ms),cost_kEUR=bound['cost_kEUR'],aggregate=aggregate,order=order)
        return value


def ordered_schedule(frames,guard,order):
    """Same exact periodic placement primitive, two fixed alternative orders."""
    if order=='ready':sorter=lambda f:(f['ready_ns'],f['latest_ns'],f['records'])
    elif order=='slack':sorter=lambda f:(f['latest_ns']-f['ready_ns'],f['latest_ns'],f['records'])
    else:raise ValueError(order)
    placed=[]
    for f in sorted(frames,key=sorter):
        start=f['ready_ns']
        if f['reserve_ns']>f['period_ns']:return dict(status='TSN_self_overlap',failed_frame=f)
        while start<=f['latest_ns']:
            guard();nxt=start
            for old in placed:
                shifts=[(f['offset_ns'],old['offset_ns'])]
                if f['source']==old['source']:shifts.append((0,0))
                for shift,oshift in shifts:
                    val=model.next_pair_start(start+shift,f['reserve_ns'],f['period_ns'],
                        old['start_ns']+oshift,old['reserve_ns'],old['period_ns'])
                    if val is None:return dict(status='TSN_periodic_pair_conflict',failed_frame=f)
                    nxt=max(nxt,val-shift)
            if nxt==start:
                placed.append(dict(f,start_ns=start));break
            start=nxt
        else:return dict(status='TSN_window_exhausted',failed_frame=f)
    return dict(status='certified',frames=placed)


def migrations(ds,choices,bound,hot):
    # First try the most urgent violated member; next strongest occupation
    # relief. Ordering is a proxy only; acceptance uses recalculated responses.
    deficits={v['member']:v['deficit_ns'] for v in bound.get('violations',[])}
    jobs=[]
    lookup={d['id']:d for d in ds}
    for k,m in bound['members'].items():
        if m['source'] not in hot:continue
        d=lookup[m['demand']]
        for dest in range(4):
            if dest==m['source']:continue
            old_util=Fraction(m['c_ns'],m['period_ns'])
            new_util=Fraction(model.can_ns(d['payload'],d['format'],dest),d['period_ns'])
            jobs.append(((-deficits.get(k,0),new_util-old_util,m['priority'],dest),m,dest))
    for _,m,dest in sorted(jobs,key=lambda x:x[0]):
        before=choices[m['demand']];sources=list(before['sources']);sources[m['slot']]=dest
        trial=replace_choice(choices,m['demand'],before['scheme'],sources)
        yield trial,dict(member=m['id'],from_source=m['source'],to_source=dest)


def reductions(ds,choices,hot):
    for d in sorted(ds,key=lambda d:d['merged_id']):
        before=choices[d['id']];n=len(before['sources'])
        # Equivalent member-count choices have identical timing: keep cheapest.
        best={}
        for s,grades in model.SCHEMES.items():
            if len(grades)>=n or model.cost(d,s)>d['costs_kEUR']['D']:continue
            count=len(grades)
            if count not in best or (model.cost(d,s),s)<(model.cost(d,best[count]),best[count]):best[count]=s
        for count,s in sorted(best.items()):
            trial=replace_choice(choices,d['id'],s,before['sources'][:count])
            benefit=sum((Fraction(model.can_ns(d['payload'],d['format'],source),d['period_ns'])
                         for source in before['sources'][count:] if source in hot),Fraction())
            yield trial,dict(demand=d['id'],before=before['scheme'],after=s,
                delta_kEUR=model.cost(d,s)-model.cost(d,before['scheme'])),benefit


def run(ds,*,free=True,watchdog_s=None):
    """Natural termination; optional external diagnostic watchdog is disclosed.

    No certified baseline -> no optimization run, never an unchecked fallback.
    Every returned witness is independently replayed. Original All-D remains
    intact even if a migrated D-only working configuration is infeasible.
    """
    began=monotonic()
    def guard():
        if watchdog_s is not None and monotonic()-began>watchdog_s:
            raise model.AnalysisLimit('external_diagnostic_watchdog')
    ev=Evaluator(ds,guard);events=[];moves=[];seen=set();scans=[];fallback=None;choice=None
    initial=None;termination='unknown';trial_count=0
    def publish(ch,w):
        a=audit(ds,ch,w)
        return dict(choices=deepcopy(ch),witness=w,audit=a)
    try:
        base_ch=model.configuration(ds,'D','original');base_b=ev.screen(base_ch)
        if base_b['stage']!='CAN_pass':
            return dict(status='unqualified',reason=base_b['stage'],elapsed_s=monotonic()-began)
        base_w=ev.construct(base_b)
        if base_w['status']!='certified':
            return dict(status='unqualified',reason=base_w['status'],elapsed_s=monotonic()-began)
        fallback=publish(base_ch,base_w)
        choice=model.configuration(ds,'cheapest','original');bound=ev.screen(choice);seen.add(key(choice))
        initial=dict(cost_kEUR=bound['cost_kEUR'],members=len(bound['members']),stage=bound['stage'],score=score_dict(bound['score']))
        while True:
            guard();failure=None
            if bound['stage']=='CAN_pass':
                failure=ev.construct(bound)
                if failure['status']=='certified':
                    candidate=publish(choice,failure)
                    if failure['cost_kEUR']<fallback['witness']['cost_kEUR']:fallback=candidate
                    termination='first_repaired_feasible';break
            hot=set(bound['hot'])
            if not hot:
                records=(failure or {}).get('failed_frame',{}).get('records',[])
                hot={bound['members'][k]['source'] for k in records}
                if not hot:hot=set(range(4))
            moved=False
            if free:
                scan=dict(kind='free',generated=0,evaluated=0,exhausted=False,stage=bound['stage'])
                scans.append(scan)
                if bound['stage']=='CAN_pass':
                    for aggregate,order in ((False,'latest'),(True,'ready'),(True,'slack'),(False,'ready'),(False,'slack')):
                        w=ev.construct(bound,aggregate,order)
                        if w['status']=='certified':
                            fallback=publish(choice,w)
                            moves.append(dict(kind='free_communication',aggregate=aggregate,order=order))
                            termination='first_repaired_feasible';moved=True;break
                    if moved:break
                for trial,action in migrations(ds,choice,bound,hot):
                    guard();scan['generated']+=1
                    if key(trial) in seen:continue
                    scan['evaluated']+=1;trial_count+=1
                    b=ev.screen(trial,base=bound,changed=action['member'].rsplit(':',1)[0])
                    accepted=(bound['stage']!='CAN_pass' and b['score']<bound['score'])
                    if bound['stage']=='CAN_pass' and b['stage']=='CAN_pass':
                        w=ev.construct(b)
                        if w['status']=='certified':
                            fallback=publish(trial,w);termination='first_repaired_feasible'
                            moves.append(dict(kind='free_source',action=action,certified=True));moved=True;break
                    if accepted:
                        moves.append(dict(kind='free_source',action=action,cost_kEUR=b['cost_kEUR'],
                            before=score_dict(bound['score']),after=score_dict(b['score'])))
                        choice=trial;bound=b;seen.add(key(trial));moved=True;break
                else:scan['exhausted']=True
                if termination=='first_repaired_feasible':break
                if moved:continue
            # Enumerate all smaller-count representatives; unchanged sources.
            ranked=[]
            for trial,action,proxy in reductions(ds,choice,hot):
                guard();trial_count+=1;b=ev.screen(trial,base=bound,changed=action['demand'])
                improvement=b['score']<bound['score']
                component=0 if bound['score'][0]>0 else 1
                actual=max(Fraction(),bound['score'][component]-b['score'][component])
                benefit=actual if improvement and actual>0 else proxy
                delta=action['delta_kEUR']
                rank=(not improvement,benefit<=0,
                    Fraction(max(0,delta))/benefit if benefit>0 else Fraction(max(0,delta)),
                    delta,-benefit,action['demand'],action['after'])
                ranked.append((rank,trial,action,b))
            if not ranked:
                termination='no_smaller_scheme_fallback';break
            _,trial,action,b=min(ranked,key=lambda x:x[0])
            moves.append(dict(kind='member_reduction',action=action,before=score_dict(bound['score']),
                after=score_dict(b['score']),before_cost_kEUR=bound['cost_kEUR'],after_cost_kEUR=b['cost_kEUR']))
            choice=trial;bound=b;seen.add(key(trial))
    except model.AnalysisLimit as exc:
        termination=str(exc)
    elapsed=monotonic()-began
    if fallback is not None:
        audit(ds,fallback['choices'],fallback['witness'])
    return dict(status='feasible' if fallback else 'unknown',method='LCR-adapter-v2' if free else 'MRR-adapter-v2',
        result=fallback,initial=initial,termination=termination,moves=moves,scans=scans,
        elapsed_s=elapsed,evaluations=ev.screens,bus_checks=ev.bus_checks,bus_cache_hits=ev.cache_hits,
        constructions=ev.constructs,trial_count=trial_count,stage_counts=dict(ev.statuses),
        full_screens=ev.full_screens,delta_screens=ev.delta_screens,members_materialized=ev.members_materialized,
        watchdog_s=watchdog_s,scope='development adapter, no optimality/physical guarantee; no silent global priority changes')
