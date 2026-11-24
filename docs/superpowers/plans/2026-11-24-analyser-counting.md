# Analyser counting implementation plan

The approved scope is handover M2. Preserve the front-end wire protocol and firmware.

- [x] Add failing C tests for the count command, statistics, switch detection, incomplete captures and invalid counts.
- [x] Add a 32-instruction PIO counter. Each sample path takes four cycles. There are 373 samples and eight boundary cycles per 1500-cycle gate. Preserve the previous input level across gate boundaries. X is a 32-bit down counter. No blocking wait occurs after the first rising edge. Detect FIFO stalls and DMA consumer overrun.
- [x] Add bounded count capture using finite DMA into the existing ring. Report timing, count range, mean, reference and post-switch frequency, first transition gate, timeout and overrun.
- [x] Add host count reports and CLI. Use counting for clock frequency and wake-up. Keep widths and burst available for separate duty and short-pulse checks.
- [x] Run cycle-level tests of the actual PIO instructions against 100 kHz, 1 MHz, 2 MHz, 3.2 MHz, phase offsets, no transitions and a 2-to-3.2 MHz switch. Verify fixed gate timing and edge count error at most one per gate.
- [x] Run the host C and Python tests, mutation check, full selftest and SDK cross compilation. Retain UF2 hash, patch and logs. Prepare the non-blocking Fable review with gate timing, counter width, overflow handling and limitations. No flash or push is authorized by this plan.

Review focus includes constant-high/low inputs, shutdown during capture, ring overwrite, FIFO stall, old analyser firmware without counting support, short pulses and boundary transitions. A first detected transition has gate quantization plus the input polling uncertainty. The counter measures frequency relative to the analyser crystal and does not establish shortest pulse or duty.
