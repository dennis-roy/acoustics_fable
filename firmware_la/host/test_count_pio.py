"""Cycle interpreter for the actual la_counts source, independent of C statistics.

Models the used PIO instructions, including pre-decrement JMP decisions and
instruction delays. FIFO service is ideal here; hardware RXSTALL checks cover
FIFO backpressure separately. No hardware timing claim follows from this model.
"""
from pathlib import Path
import re
import unittest


def simulate(signal, gates=100):
    source = Path(__file__).parents[1] / "src" / "la.pio"
    body = source.read_text().split(".program la_counts\n")[1]
    labels, program = {}, []
    for line in body.splitlines():
        line = line.split(";")[0].strip()
        if not line or line.startswith("."):
            continue
        if line.endswith(":"):
            labels[line[:-1]] = len(program)
        else:
            program.append(line)
    if len(program) > 32:
        raise AssertionError("PIO instruction memory exceeded")
    regs = dict(x=0, y=0, osr=372, isr=0)
    pc, cycle, results = 0, 0, []
    while len(results) < gates and cycle < (gates + 2) * 1600:
        line = program[pc]
        delay = re.search(r"\[(\d+)\]", line)
        ticks = 1 + (int(delay[1]) if delay else 0)
        words = line.split("[")[0].replace(",", "").split()
        target = pc + 1
        if words[0] == "mov":
            value = words[2]
            invert = value.startswith("~")
            name = value.lstrip("~")
            value = 0 if name == "null" else regs[name]
            regs[words[1]] = (~value if invert else value) & 0xffffffff
        elif words[0] == "jmp":
            condition = True
            if len(words) == 3:
                if words[1] == "pin":
                    condition = bool(signal(cycle))
                else:
                    reg = words[1][:1]
                    condition = regs[reg] != 0
                    regs[reg] = (regs[reg] - 1) & 0xffffffff
            if condition:
                target = labels[words[-1]]
        elif words[0] == "wait":
            if signal(cycle) != int(words[1]):
                target = pc
        elif words[0] == "push":
            results.append((cycle, regs["isr"]))
        elif words[0] not in ("nop", "pull"):
            raise AssertionError(line)
        cycle += ticks
        pc = target
    return results


class CountingPIOTests(unittest.TestCase):
    def test_frequencies_and_phase_offsets(self):
        for hz in (100000, 1000000, 2000000, 3200000):
            for phase in (0, 1, 13, 24):
                def signal(cycle):
                    return int(((cycle + phase) * hz * 2 // 150000000) % 2)
                rows = simulate(signal)
                self.assertEqual(len(rows), 100)
                self.assertLessEqual(abs(sum(count for _, count in rows[1:]) - 99 * hz / 100000), 1)
                for (t0, _), (t1, count) in zip(rows, rows[1:]):
                    self.assertEqual(t1 - t0, 1500)
                    self.assertLessEqual(abs(count - hz / 100000), 1)

    def test_no_first_edge_has_no_result(self):
        self.assertEqual(simulate(lambda _: 0), [])
        self.assertEqual(simulate(lambda _: 1), [])

    def test_stopped_signal_produces_zero_gates(self):
        rows = simulate(lambda t: int((t // 75) % 2) if t < 30000 else 0)
        self.assertEqual(len(rows), 100)
        self.assertTrue(all(count == 0 for _, count in rows[-50:]))

    def test_switch_is_located_within_one_gate(self):
        switch = 75000
        def signal(t):
            hz = 2000000 if t < switch else 3200000
            return int((t * hz * 2 // 150000000) % 2)
        rows = simulate(signal)
        first_changed = next(t for t, count in rows if count > 22)
        # Report the start of the changed gate, not when its PUSH completes.
        self.assertLessEqual(abs(first_changed - 1500 - switch), 1500)


if __name__ == "__main__":
    unittest.main()
