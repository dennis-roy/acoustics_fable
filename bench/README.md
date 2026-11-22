# bench: bench items with the second Pico

Python package `eol_bench`. Drives the logic analyser (`../firmware_la`) from the CM5 and turns its counts into nanoseconds, MHz and percentages, with the pass limits of section 10 of the review.

**State: written and tested against a fake analyser, 2026-11-22. Never run with the real second Pico.**

Wiring and flashing: `../firmware_la/README.md`. Stop the acquisition service first (`wakeup` and `clock` start a microphone stream on the front-end Pico themselves; or give `--manual` and start one yourself).

```
export PYTHONPATH=$PWD/link:$PWD/bench
python3 -m eol_bench info
python3 -m eol_bench levels                 # wiring check: the eight inputs
python3 -m eol_bench clock                  # bench items 3 and 4: 3.2 MHz, duty 48.5 to 51.5 %
python3 -m eol_bench wakeup                 # items 8 and 9: change after 100 ms ± 1 ms, no phase under 150 ns
python3 -m eol_bench delay --clk 0 --data 1 # item 11: data changes 10 to 50 ns after its clock edge
python3 -m eol_bench burst --trigger 0      # all inputs, list of changes
python3 -m eol_bench gen --pin 15 --hz 100  # item 30 (needs the interface board for 5 to 24 V)
```

Each check ends with `RESULT PASS` or `FAIL` and the numbers. Frequencies are relative to the analyser's crystal.

Tests: `PYTHONPATH=link:bench python3 -m unittest discover -s bench/tests` (8 tests).
