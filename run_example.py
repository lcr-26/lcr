#!/usr/bin/env python3
"""Run LCR on the included cross-domain example."""
import gzip
import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
sys.path.insert(0, str(root / 'src'))
from lcr.optimizer.deployment_scenarios import invoke

with gzip.open(root / 'data/example_input.json.gz', 'rt') as source:
    demands = json.load(source)['demands']
result = invoke(demands, 'LCR', 'cross')
with (root / 'example_result.json').open('w') as output:
    json.dump(result, output, indent=2)
print('Result written to example_result.json')
