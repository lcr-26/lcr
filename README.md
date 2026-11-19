# Low-Cost Repair for CAN–TSN Networks

This repository contains the algorithm implementations, the vehicle message dataset and input data, and the reported per-instance and per-stream results. The scripts used to run the experimental batches and collect results are not included.

## Algorithm implementation

The Python implementation uses the standard library and requires Python 3.10 or later. With `src` on the Python module search path, the entry point is `lcr.optimizer.deployment_scenarios.invoke(demands, method, scenario)`. Use `LCR`, `LCR-util`, `SA`, or `GA` for the method and `fixed` or `cross` for the placement scenario. Run with assertions enabled and use a separate process for each concurrent call. Set `seed` explicitly for SA and GA; the [baseline settings](BASELINES.md) document the parameters used in the paper.

The entry point applies the paper's CAN FD timing and checks the constructed schedule. The implementation uses four source domains, the reserved CAN priority blocks, and the fixed-source and cross-domain placement policies evaluated in the study. `fixed` keeps each task in its original domain; `cross` allows any of the four domains. The code evaluates timing and estimated cost for the modeled ASIL decomposition options and task placements. It does not assess safety independence or support arbitrary externally assigned CAN priorities.

- [Stage I: Cause-guided task reduction](src/lcr/optimizer/causal_priority_queue.py): selects task reductions using the CAN and TSN bottlenecks.
- [Stage II: Joint cost reduction](src/lcr/optimizer/agenda_sweep.py): tests cheaper options with alternative task placements. It uses [CAN screening equivalence](src/lcr/optimizer/refinement_count_groups.py) to skip equivalent placements after a confirmed CAN rejection, with other demands fixed.
- [ISO CAN FD timing](src/lcr/optimizer/iso_fd_timing.py) and [gateway and TSN scheduling](src/lcr/network_model/model.py): implement the transmission bounds, aggregation, and two-hop reservations. The [independent timing checker](src/lcr/optimizer/can_rate_context.py) checks the completed schedule.

## Dataset and results

In the code and result files, `scheme` denotes a decomposition option, `scenario` identifies the placement policy, and `member_id` identifies a message stream.

- [Vehicle messages](data/vehicle_messages.csv): 250 message records from the [CAN-TSN dataset](https://github.com/typer668/CAN-TSN) associated with F. Luo et al., DOI: 10.1109/TVT.2026.3661074. See the [upstream MIT license](data/UPSTREAM-LICENSE.txt). Payloads are in bytes; periods, deadlines, and reported timing bounds are in nanoseconds. Source indices 0–3 correspond to domains $s_1$–$s_4$ in the paper.
- [Frozen solver input](data/reference_input_n250.json.gz): the 250-demand input at deadline factor 1.0 and seed `2026112001`, including its undecomposed design. The `demands` field is the input to the solver. ASIL costs are synthetic and in thousands of euros (kEUR).
- [Main per-instance results](results/INSTANCE_VALUES.csv): the results used in the main table, including All-D, SA, GA, LCR-util, and LCR. [Stage records](results/stage_results.csv) give repair and refinement details for LCR and LCR-util. A blank action count denotes no recorded action. [Compact LCR records](results/lcr_results.json) include timing-check summaries. Recorded runtimes depend on the execution environment.
- [SA and GA checkpoints](results/SA_GA_CHECKPOINTS.csv): per-instance differences between the best verified baseline cost by each reported time and the final LCR cost. Positive differences favor LCR.
- [Stage II CAN counts](results/STAGE2_CAN_SCREEN_COUNTS.csv) and [shared downlink counts](results/STAGE2_DOWNLINK_COUNTS.csv): per-seed counts for the 30 cross-domain runs at $N=250$ and deadline factor 1. The no-reuse column counts candidate placements in the same search order. It is not a runtime measurement. Downlink attribution keeps previously scheduled offsets fixed.
- [Cost sensitivity results](results/COST_SENSITIVITY_PAIRS.csv): per-instance results before and after shuffling the ASIL A–C cost columns. `demands_with_inversions` counts demands where an adjacent lower ASIL costs more than the next higher ASIL.
- [FPGA stream results](results/fpga/stream_measurements.csv) and [summary](results/fpga/summary.json): measurements and bounds for 505 streams in the 100-second experiment. Columns ending in `_tick` use 10 ns per tick.
