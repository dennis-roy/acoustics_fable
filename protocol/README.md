# protocol: the Pico to CM5 wire format

This is the contract between the Pico firmware (`firmware/`), the CM5 receiver (`link/`) and the Pico simulator (`simulator/`). If you change anything here, change all three and bump the version.

Protocol version: **1.1** (`PROTO_MAJOR = 1`, `PROTO_MINOR = 1`).

| Version | Added |
|---|---|
| 1.0 | everything up to and including the synthetic stream, outputs and watchdog |
| 1.1 | the microphone stream (MIC_STATUS, EVENT MIC_OVERRUN, two option bytes in STREAM_START) and REBOOT. A 1.0 host works unchanged with 1.1 firmware |

## Two serial ports on one USB cable

The Pico shows up as one USB device with two serial ports (USB CDC).

| Port | USB interface | Used by | Carries |
|---|---|---|---|
| DATA | 0 (Linux name ends `-if00`) | the application | commands to the Pico; INFO, ACK, EVENT, STATS, MIC_STATUS and the PDM/TACH stream from it |
| HB | 2 (Linux name ends `-if02`) | the heartbeat process only | HEARTBEAT to the Pico, HB_STATUS back |

The heartbeat has its own port so a small separate process can own it. Analysis load in the application then cannot delay it. See `link/README.md`.

## Frame

Every message in both directions on both ports is one frame. All numbers are little-endian.

| Offset | Size | Field | Meaning |
|---|---|---|---|
| 0 | 4 | magic | `45 4F 4C 31` ("EOL1") |
| 4 | 1 | type | see below |
| 5 | 1 | flags | see below |
| 6 | 2 | length | payload bytes, 0 to 4096 |
| 8 | 4 | seq | per type and per direction, +1 for every frame the sender generates |
| 12 | 8 | t_us | sender's clock in microseconds (Pico: since boot). For PDM/TACH: time of the first sample in the payload |
| 20 | 4 | hdr_crc | CRC-32 of bytes 0 to 19 |
| 24 | length | payload | |
| 24 + length | 4 | crc | CRC-32 of everything before it (header, hdr_crc and payload) |

CRC-32 is the common one (IEEE 802.3, the same as Python's `zlib.crc32`).

Why two CRCs: the header CRC lets the receiver trust `length` before it waits for the payload. The final CRC covers the data.

How loss shows: the sender increments `seq` even for a frame it had to drop, so the receiver sees a gap. Loss is never silent.

Resync: a receiver that sees a bad magic, a bad header CRC or a bad final CRC discards one byte and searches again.

### Flags

| Bit | Name | Meaning |
|---|---|---|
| 0 | GAP | the sender dropped one or more frames of this type just before this one |
| 1 | SYNTH | payload is synthetic test data, not a sensor |

### Types

| Type | Name | Direction | Port | Payload |
|---|---|---|---|---|
| 0x01 | INFO | Pico to host | DATA | 48 bytes, below |
| 0x02 | PDM | Pico to host | DATA | 4000 bytes = 10 ms of the 1-bit microphone stream at 3.2 MHz |
| 0x03 | TACH | Pico to host | DATA | 250 bytes = 10 ms of the tach line sampled at 200 kHz |
| 0x04 | EVENT | Pico to host | DATA | `u8 code, u8[3] zero, u32 arg` |
| 0x05 | STATS | Pico to host | DATA | 40 bytes, below. Sent once a second (held back while more than 4096 bytes are waiting to be sent) and on GET_STATS |
| 0x06 | ACK | Pico to host | DATA | `u8 cmd_type, u8 status, u16 zero, u32 cmd_seq` |
| 0x07 | MIC_STATUS | Pico to host | DATA | 40 bytes, below. Sent once in every microphone stream, before its first PDM frame |
| 0x10 | HB_STATUS | Pico to host | HB | `u32 counter, u8 outputs, u8 state, u16 timeout_ms` |
| 0x81 | HELLO | host to Pico | DATA | `u8 proto_major, u8 proto_minor, u16 zero` |
| 0x82 | STREAM_START | host to Pico | DATA | `u8 source, u8 mode, u8 clock, u8 select, u32 seed, u32 duration_ms` |
| 0x83 | STREAM_STOP | host to Pico | DATA | empty |
| 0x84 | SET_OUTPUTS | host to Pico | DATA | `u8 mask, u8[3] zero` |
| 0x85 | GET_STATS | host to Pico | DATA | empty |
| 0x86 | REBOOT | host to Pico | DATA | `u32 magic` = `0x544F4F42`, the bytes "BOOT" |
| 0x90 | HEARTBEAT | host to Pico | HB | `u32 counter, u8 flags, u8[3] zero`; flags bit 0 = APP_ALIVE |

Bit order in PDM and TACH payloads: bit 7 of byte 0 is the earliest sample.

Every command gets exactly one ACK. Both ends ignore frame types they do not know.

### ACK status

| Value | Name | Meaning |
|---|---|---|
| 0 | OK | done |
| 1 | BAD_VERSION | no HELLO yet on this connection, or its major version did not match |
| 2 | BAD_ARG | a field was out of range |
| 3 | NOT_IMPLEMENTED | for example `source = MIC` on firmware built without microphone capture |
| 4 | BUSY | a stream is already running |
| 5 | SAFE_STATE | outputs refused because the heartbeat is not healthy |

### EVENT codes

| Code | Name | arg |
|---|---|---|
| 1 | STREAM_STARTED | synthetic: the seed. Microphone: the clock in hertz |
| 2 | STREAM_STOPPED | number of PDM frames generated in the stream |
| 3 | TX_OVERFLOW | frames dropped since the last TX_OVERFLOW event |
| 4 | WATCHDOG_TRIP | reason: 1 heartbeat timeout, 2 application not alive |
| 5 | WATCHDOG_OK | 0 |
| 6 | MIC_OVERRUN | raw capture words lost because the firmware's main loop fell behind |

### INFO (48 bytes)

| Offset | Field |
|---|---|
| 0 | `u8 proto_major` |
| 1 | `u8 proto_minor` |
| 2 | `u8 fw_major, fw_minor, fw_patch` |
| 5 | `u8 board`: 1 = Pico 2 W, 0xFE = firmware core built for a PC, 0xFF = Python simulator |
| 6 | `u16 hb_timeout_ms` |
| 8 | `u32 sys_clk_hz`: the system clock the firmware configured |
| 12 | `u32 fc_sys_khz`: the same clock measured by the chip's own frequency counter against the crystal, 0 if not available |
| 16 | `u8[8] unique_id`: board serial number |
| 24 | `u16 pdm_payload` (4000), `u16 tach_payload` (250) |
| 28 | `u32 features`: bit 0 synthetic source, bit 1 microphone, bit 2 tach, bit 3 outputs |
| 32 | `char[16] build`: build tag, zero padded |

### STATS (40 bytes)

`u32 uptime_ms, u32 pdm_generated, u32 pdm_dropped, u32 tach_generated, u32 tach_dropped, u32 tx_high_water, u32 rx_junk_bytes, u32 hb_received, u32 wd_trips, u8 outputs, u8 state, u16 zero`

`state` bits (also in HB_STATUS): bit 0 HELLO accepted, bit 1 streaming, bit 2 watchdog healthy.

## Version handshake

1. The host sends HELLO with its protocol version.
2. The Pico always replies INFO, then ACK. The ACK status is BAD_VERSION if the major versions differ.
3. The host refuses to continue if the major versions differ or the Pico's minor version is older than the host needs.
4. Until a HELLO with a matching major version arrives, the Pico answers STREAM_START and SET_OUTPUTS with BAD_VERSION.

So both ends refuse a mismatch.

## Streams

STREAM_START fields:

- `source`: 0 = synthetic, 1 = microphone.
- `mode`: 0 = real rate (one PDM and one TACH frame every 10 ms, 430.6 kB/s with headers), 1 = maximum rate (PDM frames back to back, as fast as the host takes them). The microphone takes mode 0 only.
- `clock`, `select`: microphone only, see "Microphone stream". Zero for the synthetic source.
- `seed`: seed for the synthetic data.
- `duration_ms`: the Pico stops by itself after this long. 0 = until STREAM_STOP. For the microphone it is the length of the capture, counted in whole frames from the first PDM frame.

PDM and TACH `seq` restart at 0 on every STREAM_START. When a stream ends the Pico sends EVENT STREAM_STOPPED with the number of PDM frames it generated, so the host can prove it received the tail.

If the host reads too slowly, the Pico's 64 kB transmit buffer fills. In real-rate mode the Pico then drops whole stream frames, counts them, sets GAP on the next frame of that type and sends TX_OVERFLOW. In maximum-rate mode it waits and drops nothing.

### Synthetic data

Used for the link proof. Both ends can generate it, so the receiver checks every byte.

- Pattern: 65 536 bytes. Start `x = seed` (use 1 if the seed is 0). Repeat 16 384 times: `x ^= x << 13; x ^= x >> 17; x ^= x << 5` (32-bit), then append `x` as 4 little-endian bytes.
- PDM frame `n` carries 4000 pattern bytes starting at offset `(n * 4000) mod 65536`, wrapping round.
- TACH frame `n` carries 250 pattern bytes starting at offset `(n * 250 + 32768) mod 65536`.

4000 does not divide 65 536, so each frame starts at a different place in the pattern. A repeated, skipped or shifted buffer in the firmware is caught even when its CRC is correct.

## Microphone stream

`source = 1`. The reasons for every step are in sections 3 and 5 of `docs/phase0/PHASE0_HARDWARE_DESIGN_REVIEW.md`.

STREAM_START options:

- `clock`: 0 = 3.2 MHz, 1 = 4.0 MHz.
- `select`: 0 = the Pico finds out which half of the clock carries the data (normal). 1 = read "L", 2 = read "H", whatever the probe says (bench use).

What the Pico does, in order:

| Step | Length | Clock | Sent to the host |
|---|---|---|---|
| Wake | 100 ms | 2.0 MHz | ACK, then EVENT STREAM_STARTED |
| Settle | 20 ms | the chosen clock | nothing |
| Probe | 100 000 clock periods, about 31 ms | the chosen clock | MIC_STATUS at the end |
| Run | `duration_ms`, or until STREAM_STOP | the chosen clock | PDM frames, `seq` from 0, no SYNTH flag |
| Stop | | held low | EVENT STREAM_STOPPED with the number of PDM frames |

The 2.0 MHz step is never skipped: the microphone must not go from sleep straight to the ultrasonic clock.

A PDM frame is always 4000 bytes, 32 000 clock periods: 10 ms at 3.2 MHz, 8 ms at 4.0 MHz. `t_us` of frame `n` is the start of the run plus `n` frame lengths. There are no TACH frames in a microphone stream yet.

If the probe finds no microphone and `select` is 0, the Pico sends MIC_STATUS, stops the clock and sends STREAM_STOPPED with 0. Nothing is streamed from a dead line.

If the firmware's main loop falls behind and capture data is overwritten, the Pico sends EVENT MIC_OVERRUN, counts the frames that went with it as dropped, skips their `seq` numbers and sets GAP on the next frame. As with every other loss, the host sees a gap.

### The two reads per clock period

The Pico reads the data line twice in every period:

- **L**, at the clock's rising edge: the line as it was at the end of the low half.
- **H**, at the falling edge: the line as it was at the end of the high half.

The microphone drives the line in one half and lets go of it in the other. Which half depends on how its SELECT pin is tied. With SELECT at ground, L is the data; with SELECT at the supply, H is.

### The SELECT probe

The probe looks at pairs of neighbouring reads over 100 000 periods and counts:

| Counter | Counts periods where |
|---|---|
| `ones_l`, `ones_h` | L was 1; H was 1 |
| `lh_10`, `lh_01` | L was 1 and the H after it 0; L was 0 and the H after it 1 |
| `hl_10`, `hl_01` | H was 1 and the next period's L 0; H was 0 and the next period's L 1 |

Two reads of the same bit, one while the microphone drives and one after it lets go, can differ in one direction only: the direction the released line drifts in. Two reads on either side of a bit boundary differ in both directions. So with `same_if_gnd = min(lh_10, lh_01)`, `same_if_vdd = min(hl_10, hl_01)`, `small = periods / 64` and `large = periods / 16`:

| Condition | Verdict |
|---|---|
| `same_if_gnd < small` and `same_if_vdd >= large` | 1, SELECT = GND: forward L |
| `same_if_vdd < small` and `same_if_gnd >= large` | 2, SELECT = VDD: forward H |
| both under `small`, and only L varies (`ones_l` between 1/8 and 7/8 of the periods, `ones_h` not) | 1 |
| both under `small`, and only H varies | 2 |
| both under `small` otherwise | 3, the line is stuck: no microphone, no power or no clock |
| anything else | 4, ambiguous |

The third and fourth rows cover a released line that always falls back to the same level. The rule does not care whether the released line holds its level, falls or rises, so it needs no particular pull resistor.

### MIC_STATUS (40 bytes)

| Offset | Field |
|---|---|
| 0 | `u8 verdict`: 0 not probed, 1 SELECT = GND, 2 SELECT = VDD, 3 stuck, 4 ambiguous |
| 1 | `u8 use`: which read is forwarded. 0 none, 1 L, 2 H |
| 2 | `u8 forced`: 1 if `use` came from the command's `select`, not from the probe |
| 3 | `u8 clock` code |
| 4 | `u32 clock_hz` |
| 8 | `u32 periods` |
| 12 | `u32 ones_l, ones_h` |
| 20 | `u32 lh_10, lh_01` |
| 28 | `u32 hl_10, hl_01` |
| 36 | `u32 zero` |

### Simulated microphone signal

The two stand-ins for the Pico (the Python simulator and the firmware core built for a PC) have no microphone, so they make a 1-bit stream by rule. It uses whole numbers only, so both make exactly the same bits and a test can check every one.

- Count clock periods `n` from 0. After 320 000 periods start again from `n = 0` with `v1 = v2 = 0`.
- Input: `x = 16384 * sq(n, 3200) + 4096 * sq(n, 100)`, where `sq(n, P)` is +1 if `n mod P < P / 2`, otherwise -1. At 3.2 MHz these are square waves at 1 kHz and 32 kHz.
- For each period, in this order: the bit is 1 if `v2 >= 0`; `y = 65536` if the bit is 1, otherwise `-65536`; `v1 += x - y`; `v2 += v1 - y`.

The Python simulator starts its first PDM frame at `n = 0`. The PC build of the firmware core runs the signal from the moment the clock starts, so its first frame begins somewhere else in the signal, always a multiple of 16 periods in.

## Restart into the bootloader

REBOOT with the right magic word makes the Pico answer ACK and, 100 ms later, restart as a USB drive, so new firmware can be copied to it without pressing its button. Reset opens every output contact. It needs a HELLO first, like every command that changes anything. A wrong magic word gets BAD_ARG.

This is a development convenience. Anyone who can open the DATA port can use it, so decide before shipping whether it stays.

## Outputs and the watchdog

`mask` bits: 0 OK, 1 NOK, 2 UNDEFINED, 3 READY, 4 BUSY. Bit 5 (WATCHDOG) is driven by the Pico and cannot be set.

Rules the Pico enforces:

- "Healthy" means: a HEARTBEAT with APP_ALIVE set arrived on the HB port within the last 500 ms.
- While not healthy, every output is off, and SET_OUTPUTS is refused with SAFE_STATE.
- On losing health, the Pico forgets the requested mask. Outputs stay off after health returns until a new SET_OUTPUTS arrives. A stale OK can never reappear.
- At most one of OK, NOK, UNDEFINED may be set. Otherwise BAD_ARG.
- Closing the DATA port also clears the requested mask.
- While healthy, WATCHDOG is a 1 Hz square wave.

"Off" means the output contact is open. See section 7 of `docs/phase0/PHASE0_HARDWARE_DESIGN_REVIEW.md`.
