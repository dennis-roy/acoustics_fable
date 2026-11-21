# protocol: the Pico to CM5 wire format

This is the contract between the Pico firmware (`firmware/`), the CM5 receiver (`link/`) and the Pico simulator (`simulator/`). If you change anything here, change all three and bump the version.

Protocol version: **1.0** (`PROTO_MAJOR = 1`, `PROTO_MINOR = 0`).

## Two serial ports on one USB cable

The Pico shows up as one USB device with two serial ports (USB CDC).

| Port | USB interface | Used by | Carries |
|---|---|---|---|
| DATA | 0 (Linux name ends `-if00`) | the application | commands to the Pico; INFO, ACK, EVENT, STATS and the PDM/TACH stream from it |
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
| 0x10 | HB_STATUS | Pico to host | HB | `u32 counter, u8 outputs, u8 state, u16 timeout_ms` |
| 0x81 | HELLO | host to Pico | DATA | `u8 proto_major, u8 proto_minor, u16 zero` |
| 0x82 | STREAM_START | host to Pico | DATA | `u8 source, u8 mode, u16 zero, u32 seed, u32 duration_ms` |
| 0x83 | STREAM_STOP | host to Pico | DATA | empty |
| 0x84 | SET_OUTPUTS | host to Pico | DATA | `u8 mask, u8[3] zero` |
| 0x85 | GET_STATS | host to Pico | DATA | empty |
| 0x90 | HEARTBEAT | host to Pico | HB | `u32 counter, u8 flags, u8[3] zero`; flags bit 0 = APP_ALIVE |

Bit order in PDM and TACH payloads: bit 7 of byte 0 is the earliest sample.

Every command gets exactly one ACK. The Pico ignores frame types it does not know.

### ACK status

| Value | Name | Meaning |
|---|---|---|
| 0 | OK | done |
| 1 | BAD_VERSION | no HELLO yet on this connection, or its major version did not match |
| 2 | BAD_ARG | a field was out of range |
| 3 | NOT_IMPLEMENTED | for example `source = MIC` before build-plan step 3 |
| 4 | BUSY | a stream is already running |
| 5 | SAFE_STATE | outputs refused because the heartbeat is not healthy |

### EVENT codes

| Code | Name | arg |
|---|---|---|
| 1 | STREAM_STARTED | seed |
| 2 | STREAM_STOPPED | number of PDM frames generated in the stream |
| 3 | TX_OVERFLOW | frames dropped since the last TX_OVERFLOW event |
| 4 | WATCHDOG_TRIP | reason: 1 heartbeat timeout, 2 application not alive |
| 5 | WATCHDOG_OK | 0 |

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

- `source`: 0 = synthetic, 1 = microphone (step 3).
- `mode`: 0 = real rate (one PDM and one TACH frame every 10 ms, 430.6 kB/s with headers), 1 = maximum rate (PDM frames back to back, as fast as the host takes them).
- `seed`: seed for the synthetic data.
- `duration_ms`: the Pico stops by itself after this long. 0 = until STREAM_STOP.

PDM and TACH `seq` restart at 0 on every STREAM_START. When a stream ends the Pico sends EVENT STREAM_STOPPED with the number of PDM frames it generated, so the host can prove it received the tail.

If the host reads too slowly, the Pico's 64 kB transmit buffer fills. In real-rate mode the Pico then drops whole stream frames, counts them, sets GAP on the next frame of that type and sends TX_OVERFLOW. In maximum-rate mode it waits and drops nothing.

### Synthetic data

Used for the link proof. Both ends can generate it, so the receiver checks every byte.

- Pattern: 65 536 bytes. Start `x = seed` (use 1 if the seed is 0). Repeat 16 384 times: `x ^= x << 13; x ^= x >> 17; x ^= x << 5` (32-bit), then append `x` as 4 little-endian bytes.
- PDM frame `n` carries 4000 pattern bytes starting at offset `(n * 4000) mod 65536`, wrapping round.
- TACH frame `n` carries 250 pattern bytes starting at offset `(n * 250 + 32768) mod 65536`.

4000 does not divide 65 536, so each frame starts at a different place in the pattern. A repeated, skipped or shifted buffer in the firmware is caught even when its CRC is correct.

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
