# Accounting and anomaly algorithms

The shared pure functions are in `src/opengrid/domain.py`; transaction-level orchestration is in `services.py`. Unit tests exercise numerical and temporal edge cases.

Register energy is `(end − start) × multiplier` in kWh. Both boundaries must be present, aligned and share an epoch. PRIMARY input uses multiplier 1. Missing pairs are never interpolated. Negative deltas require configured modulus, explicit rollover confirmation and a maximum plausible energy bound; otherwise they are reset/invalid observations. Derived average power equals interval kWh × 4 for a 15-minute interval.

Net transformer import minus summed downstream net import gives accounting difference. Import and export remain separate nonnegative channels. An absent export register is zero only under an explicit import-only configuration. Missing readings, empty topology, nonpositive/near-zero net input and reverse flow suppress the percentage. Negative accounting differences remain visible.

MVP reporting requires all expected meters to have valid energy. The production design's configurable partial-coverage eligibility is intentionally stricter here: observed partial energy can be displayed, but it does not create an imbalance percentage or investigation.

The baseline is the median of the same UTC quarter-hour slot in the previous 28 days, with at least 14 eligible observations and matching expected-meter membership. Mean, standard deviation and MAD accompany it. A candidate must exceed both 5 percentage points and 3 × 1.4826 × MAD, plus minimum kWh impact. These thresholds are configurable.

Persistence requires the current slot to be abnormal and at least four of six consecutive eligible slots to qualify. Gaps break the window. Scoring uses the requested 35/25/20/10/10 weights for deviation, persistence, completeness, meter deviations and event correlation. Confidence is separately derived from valid population and baseline sample coverage; it is an evidence-quality index, never a probability.

Meter comparisons require at least seven comparable observations and a meaningful baseline. Import below 30% of baseline or above 250% is a drop/spike. Six adjacent valid zero intervals produce ZERO_CONSUMPTION; six almost-equal intervals produce FLATLINE. These are observations, not causal conclusions. Independent meter evaluation and transformer correlation use the same current interval data.

Late or revised boundary readings recompute both neighbors. Changed balances re-evaluate the later same-slot baseline dependencies and following persistence windows. Superseded evidence is retained, and human case decisions are never erased by a recalculation.
