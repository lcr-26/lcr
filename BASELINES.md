# SA and GA settings

The main experiments use 30 input seeds, 2026112001 through 2026112030. For an input seed \(s\), SA uses \(s+310000\) and GA uses \(s+410000\) under both placement policies. Each method has a 1800 s wall-clock search budget and can stop early when it finds a timing-verified design at the ideal bound. This bound sums the least expensive ASIL decomposition option for each demand. The fixed-source search keeps all tasks in their original domains. The cross-domain search can assign a task to any of the four domains.

## Parameter choice and scope

The temperature, cooling, proposal-size, population, crossover and mutation settings were retained from the development pilot. The same rules were used across demand counts, deadline factors and placement policies, without selecting settings separately for the reported conditions. The mutation rule 1/N selects one demand per offspring in expectation. These are implementation settings; the pilot did not establish that they are optimal for the communication model. For SA, the formal protocol uses a verified All-D active start if the cheapest design is not schedulable, so annealing can start from a feasible design.

[BASELINE_SEEDS.csv](BASELINE_SEEDS.csv) lists the input and search seeds of all 840 main baseline runs. The N=200, deadline-factor=1 result is shared between the demand-count and deadline comparisons. Each record in the list agrees with the run plan and the native solver output.

The 1,800 s search budget includes initialization and checks performed during search. There is no iteration, generation or stagnation cutoff. The final check is included in total runtime, which may exceed 1,800 s. Fixed seeds reproduce random choices for the same execution path; a wall-clock cutoff may allow a different number of proposals on other hardware.

## Simulated annealing

SA checks All-D and the cheapest original-source option combination. If the latter is verified, it attains the lower bound and search stops. Otherwise the active annealing state is All-D. The initial temperature is 0.1 and the temperature is multiplied by 0.9995 after every iteration. A proposal makes one edit with probability 0.75 or two edits with probability 0.25, drawing the demand uniformly for each edit. under the fixed-source policy, each edit chooses a different option uniformly. under the cross-domain policy, it chooses with equal probability between a option edit and moving a uniformly chosen active task to a different uniformly chosen domain. SA accepts an energy decrease and accepts an increase \(\Delta E\) with probability \(\exp[-\Delta E/\max(T,10^{-12})]\).

## Genetic algorithm

The target population size is 32. Initialization includes All-D and the cheapest original-source option combination. Random individuals fill the population unless the cost lower bound has already been reached. An incomplete check of the cheapest initial combination falls back to All-D. Selection uses rank-based roulette-wheel sampling with replacement, averaging the rank weights of tied individuals. The best individual is carried forward. Two-point crossover is applied with probability 0.9 to the option loci under the fixed-source policy or to the flattened option and source loci under the cross-domain policy. Mutation is applied independently with probability \(1/N\) per demand. A mutation chooses a different option under the fixed-source policy. under the cross-domain policy it chooses with equal probability between a option change and a source change in one of the four source slots, including slots that may become active after a option change.

Both baselines use the shared timing checker. For a candidate of cost \(C\) that passes the timing check, the search energy is \(C/(C_{\max}+1)\), where \(C_{\max}=\sum_g4c_g(D)\) for the sorted costs of the main experiment. A candidate that fails a completed timing check has energy \(1+v/(1+v)\), where \(v\) is one plus the sum of the timing check's violation scores. Candidates with incomplete checks are skipped.

## Paired results

The paired summary groups the main results by experiment, demand count, deadline factor and placement policy. Each row compares LCR with one baseline using the same 30 inputs. The difference is LCR's cost savings over All-D minus the comparator's savings over the same All-D. Positive values favor LCR. The sample standard deviation is calculated over the seed-level differences. The nominal 95% percentile bootstrap intervals use 20,000 resamples of those paired differences with seed 20261113. The intervals describe variability across the evaluated input seeds. At \(N=250\), those seeds vary the synthetic costs on a single communication workload.

- [Paired summaries](results/PAIRED_COMPARISONS.csv)
- [Seed-level paired differences](results/PAIRED_DIFFERENCES.csv)
- [Main per-instance results](results/INSTANCE_VALUES.csv)
- [SA and GA checkpoint comparisons at 250 demands](results/SA_GA_CHECKPOINTS.csv)
