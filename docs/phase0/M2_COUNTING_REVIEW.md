# Codex to Fable review — M2 analyser counting

Review is NON-BLOCKING on the PIO design. Owner approval is required before flashing.
Source baseline is rev-2 at 5b000d5d2c29e98f4bb7f42ac0200b7e9799378b.
Implementation is isolated in branch codex/m2-counting at /home/dennis/acoustics_fable_m2.

## Design and review decisions

- PIO uses 32 instructions, with 373 four-cycle sample iterations and eight boundary cycles per 1500-cycle gate. Both input paths have equal timing. Gate length is 10 us at 150 MHz.
- X is a 32-bit down counter. At most 187 rising edges fit in one gate. Larger decoded counts invalidate capture. No arithmetic wrap is possible within a gate.
- Preserve the input level across gate boundaries. Maximum sampling gap is 12 cycles (80 ns). Both input phases must exceed this interval. The model covers the intended 50 percent duty bench clocks.
- Finite DMA writes to an 8192-word ring. Lag beyond 8128 words or RXSTALL invalidates capture. A chained one-word DMA disables the SM after the final recorded word, independent of CPU scheduling.
- Counting sends an armed acknowledgement before the host starts the microphone. Unsupported firmware and transport errors prevent mic startup. Legacy widths and burst protocols are retained.
- A frequency change needs three agreeing changed gates within one count, then is backdated to the candidate gate. This rejects a single outlier before the true switch. Sixteen gates after the candidate are excluded from settled statistics.
- Clock captures 1020000 gates and requires at least 1000000 settled gates, settled counts 31–33 in every gate, zero integrity faults and a 3.2 MHz mean within 0.5 percent. Duty and shortest pulses require separate width/burst measurements.

Independent code review checked the actual PIO paths. Its important findings about
DMA tail stalls, arming order, insufficient settled duration, missing settled
range checks and transient switch misdating were addressed. Regression coverage
includes command rejection, missing armed ACK, incomplete counts, out-of-range
settled gates and an outlier 60 gates before the real transition.

## Verification recorded 2026-11-24

- CM5 baseline selftest, original source: PASS 19, FAIL 0, NOT RUN 3.
- Updated full selftest: PASS 20, FAIL 0, NOT RUN 3. No hardware options were used.
- C host tests compiled with C11, Wall, Wextra, Werror: all la_core tests passed.
- Focused Python bench tests: 14 passed.
- Cycle interpreter of the actual PIO source: four tests passed. Cases include 100 kHz, 1 MHz, 2 MHz, 3.2 MHz, phase offsets, stuck inputs, stopping input and a startup switch.
- Deliberately weakened C count bound: detected by failing assertions. Deliberately lengthened PIO sample path: three timing tests failed. Mutated copies were separate from production source.
- Pico 2 firmware cross-compiled using SDK 2.1.1 and the installed SDK 2.3.1 on the CM5. This was local cross-compilation, not a GitHub CI run.
- git diff --check passed.

SDK 2.1.1 UF2 SHA-256 is
`f27260f6aa3e14782d3ccba51d5e2a4ea503f8e2d091a608edff9fb9bb3b1e33`.
SDK 2.3.1 UF2 SHA-256 is
`8462390864419c5d1e4e03e5b2cb7101c65b6649cc718559157fa41c5376157a`.

The original flashed 0.1.0 UF2 was unavailable on the CM5. A fallback was rebuilt
from the unchanged baseline analyser source using SDK 2.1.1, SHA-256
`f66dfcd734c0caf6ec32476616ac2192aa4c25e85dae7c6a9b414388e9575c98`.
This fallback is not the original CI binary and has not been physically verified.
The original source matches the analyser sources at 335b3ca.

## Bench evidence retained

Both physical configurations passed microphone capture with firmware 0.3.0.
Test A (GP1 disconnected) is captures/testA_GP1_disconnected_20261124T083027Z.
Test B quiet pair is captures/testB_quiet_GP1_connected_20261124T083444Z.
The first directory named testA_20261124T080341Z was mislabelled: owner confirmed
GP1 was connected, so those recordings are Test B evidence. Preserve its filenames.
With GP1 connected a burst recorded 2796 clock and 964 data transitions.
Earlier quiet repeatability failures are retained in testB_GP1_connected_20261124T083201Z.
The subsequent owner-confirmed quiet pair differed by 0.5, 0.2 and 0.0 dB.

## Pending physical verification and decision

No new firmware has been flashed. Hardware claims are limited to the baseline
0.1.0 analyser and 0.3.0 front end. The new counting timing is model-verified,
not measured on the Pico. Approval to push or run CI has not been obtained.

Fable should assess the 1500-cycle gate, 32-bit counter, 12-cycle sampling gap,
DMA completion stop and three-gate transition confirmation. Owner should approve
flashing the SDK 2.1.1 eol_la.uf2 onto the analyser Pico 2 only, then generator
self-tests and the real-clock tests. The front-end Pico 2 W stays on 0.3.0.

After approved flashing, owner-approved generator loopback wiring is needed for
100 kHz and 1 MHz self-tests. Restore the current mic clock/data wiring for at
least ten settled seconds without overrun and five wake-up runs agreeing within
one gate. Preserve every result, including failures.

Deferred minor issue from review: the legacy widths report can fail while printing
an incomplete post-change result, and generic counting marks isolated zero gates
invalid even for a source near or below 100 kHz. These do not affect the intended
shared-crystal 100 kHz/1 MHz generator checks or the 2/3.2 MHz microphone clock.
