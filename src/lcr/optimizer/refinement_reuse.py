"""Exact prefix-response reuse in BOTH phases of the frozen two-phase LCR.

Only the kernel binding in refine() is added. Search, candidate generation,
scores, acceptance, loop guards, UNKNOWN handling and independent audits are
inherited unchanged. No cross-demand combined repair is introduced.

The same latest-success-per-domain analyzer is retained from repair. A cached
domain answer in Evaluator may leave that analyzer's latest record stale, but
the Analyzer compares full timing prefixes and blocking before reuse, so a
stale entry can only miss reuse, not produce an unchecked answer.
"""
from copy import deepcopy
from unittest.mock import patch

from lcr.network_model import model
from . import grouped_can, recurrence_reuse

VERSION = 'two-phase-exact-prefix-reuse-v1'


class Runner(recurrence_reuse.Runner):
    def __init__(self, ds, watchdog_s, skip_internal, *, refine_reuse=True):
        super().__init__(ds, watchdog_s, skip_internal)
        self.refine_reuse = refine_reuse
        self.refine_stats = {}
        self.repair_stats = {}

    def refine(self):
        self.repair_stats = dict(self.analyzer.stats)
        before = self.analyzer.stats.copy()
        old_reuse = self.analyzer.reuse
        self.analyzer.reuse = self.refine_reuse
        try:
            with patch.object(model, 'can_analysis', self.analyzer):
                return super().refine()
        finally:
            self.analyzer.reuse = old_reuse
            self.refine_stats = {k: self.analyzer.stats[k]-before[k]
                                 for k in self.analyzer.stats if k != 'peak_stored_members'}


def run(ds, *, mode='JOINT', watchdog_s=None, skip_internal=True, refine_reuse=True):
    if mode not in ('MRR', 'JOINT'):
        raise ValueError(mode)
    saved = deepcopy(ds)
    with patch.object(model, 'can_analysis', grouped_can.can_analysis):
        runner = Runner(ds, watchdog_s, skip_internal, refine_reuse=refine_reuse)
        result = runner.execute(mode)
    assert ds == saved
    result.update(can_backend=VERSION, repair_can_backend=recurrence_reuse.VERSION,
        refine_can_backend=recurrence_reuse.VERSION if refine_reuse else 'suffix-only-control-v1',
        repair_backend='domain-local-reuse-v1', recurrence_reuse=dict(runner.analyzer.stats),
        repair_recurrence_reuse=runner.repair_stats or dict(runner.analyzer.stats),
        refine_recurrence_reuse=runner.refine_stats,
        domain_reuse=dict(runner.reduction_scorer.stats),
        reduction_score_stages=dict(runner.reduction_scorer.stages),
        evaluation_counter_scope='all original candidates and evaluator calls retained; '
                                 'only response recurrence computation may be reused')
    return result
