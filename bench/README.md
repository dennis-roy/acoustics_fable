# bench: bench items with the second Pico

Python package `eol_bench`. Drives the logic analyser (`../firmware_la`) from the CM5 and turns its counts into nanoseconds, MHz and percentages, with the pass limits of section 10 of the review.

**State:** firmware 0.1.0 has been exercised on the real second Pico, including
clock and data burst capture on 2026-11-24. Counting firmware 0.2.0 is under host
verification and awaits owner-approved flashing and physical timing tests.

Wiring and flashing: `../firmware_la/README.md`. Stop the acquisition service first (`wakeup` and `clock` start a microphone stream on the front-end Pico themselves; or give `--manual` and start one yourself).

```
export PYTHONPATH=$PWD/link:$PWD/bench
python3 -m eol_bench info
python3 -m eol_bench levels                 # wiring check: the eight inputs
python3 -m eol_bench clock                  # 3.2 MHz, at least 10 settled seconds, counting mode
python3 -m eol_bench wakeup                 # frequency switch after 100 ms ± 1 ms, counting mode
python3 -m eol_bench clock --widths --periods 128  # separate legacy duty/pulse check, may overrun
python3 -m eol_bench delay --clk 0 --data 1 # item 11: data changes 10 to 50 ns after its clock edge
python3 -m eol_bench burst --trigger 0      # all inputs, list of changes
python3 -m eol_bench gen --pin 15 --hz 100  # item 30 (needs the interface board for 5 to 24 V)
```

Each check ends with `RESULT PASS` or `FAIL` and the numbers. Frequencies are relative to the analyser's crystal.

Tests: `PYTHONPATH=link:bench python3 -m unittest discover -s bench/tests`.
Counting mode does not verify duty or short pulses. The original width-based
wake-up check remains available with `wakeup --widths`. That legacy check may
overrun at the mic clock, as it did in the 2026-11-23 bench run.
