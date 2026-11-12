# Low-Cost Repair for CAN–TSN Networks

This repository contains the Low-Cost Repair (LCR) implementation, vehicle message parameters, and results reported in the paper.

## Run the example

Python 3.10 or later is required. From the repository root, run:

```sh
PYTHONHASHSEED=0 python3 run_example.py
```

The script runs LCR with cross-domain deployment on the included 250-demand input and writes `example_result.json`. The included input uses seed 2026112001 and deadline factor 1.0. The example includes generated A–D development costs. The `src/lcr` modules implement LCR and its schedulability checks.

## Data and results

- `data/vehicle_messages.csv` contains the 250 vehicle message parameters used as the basis for the study. Payload is in bytes; periods, deadlines and published timing quantities are in nanoseconds. The source is [CAN-TSN](https://github.com/typer668/CAN-TSN), associated with F. Luo et al., DOI: 10.1109/TVT.2026.3661074. The upstream MIT licence is included in `data/UPSTREAM-LICENSE.txt`.
- `data/example_input.json.gz` contains the example demands and baseline. Costs are generated for the experiment and are not measured vehicle development costs.
- `results/INSTANCE_VALUES.csv` records the original four-method experimental results. `results/stage_results.csv` records the subsequent no-baseline LCR and LCR-util runs, including stage costs, times and action counts. The paper uses the appropriate results from both batches. Runtime depends on the execution environment.
- `results/fpga/stream_measurements.csv` and `results/fpga/summary.json` contain measured and analytical results for the FPGA validation test. The test used the example configuration, 505 member streams and 100 seconds of traffic release.

