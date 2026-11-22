#include "core.h"

#include <string.h>

#include "hal.h"
#include "proto.h"

#define RING_SIZE 65536u   /* power of two */
#define RING_RESERVE 512u  /* stream frames never use this last part, so replies still fit */
#define PATTERN_SIZE 65536u
#define FRAME_PERIOD_US 10000u
#define STATS_PERIOD_US 1000000u
#define HB_TIMEOUT_US (EOL_HB_TIMEOUT_MS * 1000u)
#define MAX_FRAMES_PER_POLL 8
#define STATS_BACKLOG_LIMIT 4096u /* periodic STATS waits while more than this is queued */

enum { SRC_SYNTH = 0, SRC_MIC = 1 };
enum { MODE_REAL = 0, MODE_MAX = 1 };
enum { PDM = 0, TACH = 1 };

static uint8_t ring[RING_SIZE];
static uint32_t ring_head, ring_tail; /* free-running byte counts */
static uint8_t pattern[PATTERN_SIZE];
static uint8_t hbtx[128];
static size_t hbtx_len;
static eol_parser_t data_parser, hb_parser;

static struct {
    uint64_t now;
    bool port_open, hello_ok;

    bool streaming;
    uint8_t src, mode;
    uint64_t t0, next_due;
    uint32_t duration_us, seed;
    uint32_t gen[2], dropped[2];
    bool gap[2];

    uint32_t ovf_unreported;
    uint64_t last_ovf_event, last_stats;

    bool hb_seen, hb_app_alive, healthy;
    uint64_t hb_last;
    uint32_t hb_count, wd_trips;
    uint8_t requested, actual;

    uint32_t seq_info, seq_event, seq_stats, seq_ack, seq_hb, seq_mic;
    uint32_t ring_high;
    uint64_t reboot_at;
} st;

/* ---- transmit ring ---- */

static uint32_t ring_free(void) { return RING_SIZE - (ring_head - ring_tail); }

static void ring_put(const uint8_t *p, size_t n)
{
    while (n) {
        uint32_t idx = ring_head & (RING_SIZE - 1);
        size_t chunk = RING_SIZE - idx;
        if (chunk > n) chunk = n;
        memcpy(&ring[idx], p, chunk);
        ring_head += (uint32_t)chunk;
        p += chunk;
        n -= chunk;
    }
}

/* Queues one frame on the DATA port. The payload may be in two pieces (a, b).
   Returns false, and queues nothing, if it does not fit. */
static bool emit(uint8_t type, uint8_t flags, uint32_t seq, uint64_t t_us, const uint8_t *a,
                 size_t na, const uint8_t *b, size_t nb, bool is_stream)
{
    size_t total = EOL_HDR_LEN + na + nb + EOL_CRC_LEN;
    if (!st.port_open || ring_free() < total + (is_stream ? RING_RESERVE : 0)) return false;

    uint8_t hdr[EOL_HDR_LEN], tail[EOL_CRC_LEN];
    eol_put_header(hdr, type, flags, (uint16_t)(na + nb), seq, t_us);
    uint32_t crc = eol_crc32(0, hdr, EOL_HDR_LEN);
    crc = eol_crc32(crc, a, na);
    crc = eol_crc32(crc, b, nb);
    eol_put_u32(tail, crc);

    ring_put(hdr, EOL_HDR_LEN);
    ring_put(a, na);
    ring_put(b, nb);
    ring_put(tail, EOL_CRC_LEN);

    uint32_t used = ring_head - ring_tail;
    if (used > st.ring_high) st.ring_high = used;
    return true;
}

static void emit_small(uint8_t type, uint32_t *seq, const uint8_t *payload, size_t n)
{
    emit(type, 0, (*seq)++, st.now, payload, n, NULL, 0, false);
}

static void send_event(uint8_t code, uint32_t arg)
{
    uint8_t p[8] = {code, 0, 0, 0};
    eol_put_u32(p + 4, arg);
    emit_small(EOL_T_EVENT, &st.seq_event, p, sizeof p);
}

static void send_ack(uint8_t cmd, uint32_t cmd_seq, uint8_t status)
{
    uint8_t p[8] = {cmd, status, 0, 0};
    eol_put_u32(p + 4, cmd_seq);
    emit_small(EOL_T_ACK, &st.seq_ack, p, sizeof p);
}

static uint8_t state_bits(void)
{
    return (uint8_t)((st.hello_ok ? EOL_STATE_HELLO : 0) | (st.streaming ? EOL_STATE_STREAMING : 0) |
                     (st.healthy ? EOL_STATE_HEALTHY : 0));
}

static void send_info(void)
{
    uint8_t p[48] = {0};
    p[0] = EOL_PROTO_MAJOR;
    p[1] = EOL_PROTO_MINOR;
    p[2] = EOL_FW_MAJOR;
    p[3] = EOL_FW_MINOR;
    p[4] = EOL_FW_PATCH;
    p[5] = hal_board_id();
    eol_put_u16(p + 6, EOL_HB_TIMEOUT_MS);
    eol_put_u32(p + 8, hal_sys_clk_hz());
    eol_put_u32(p + 12, hal_fc_sys_khz());
    hal_unique_id(p + 16);
    eol_put_u16(p + 24, EOL_PDM_PAYLOAD);
    eol_put_u16(p + 26, EOL_TACH_PAYLOAD);
    /* bit 0 synthetic source, bit 1 microphone, bit 3 outputs. Tach (bit 2) is not built yet. */
    eol_put_u32(p + 28, 0x1u | (hal_mic_available() ? 0x2u : 0u) | 0x8u);
    strncpy((char *)p + 32, EOL_BUILD_TAG, 15);
    emit_small(EOL_T_INFO, &st.seq_info, p, sizeof p);
}

static void send_stats(void)
{
    uint8_t p[40] = {0};
    eol_put_u32(p + 0, (uint32_t)(st.now / 1000u));
    eol_put_u32(p + 4, st.gen[PDM]);
    eol_put_u32(p + 8, st.dropped[PDM]);
    eol_put_u32(p + 12, st.gen[TACH]);
    eol_put_u32(p + 16, st.dropped[TACH]);
    eol_put_u32(p + 20, st.ring_high);
    eol_put_u32(p + 24, data_parser.junk + hb_parser.junk);
    eol_put_u32(p + 28, st.hb_count);
    eol_put_u32(p + 32, st.wd_trips);
    p[36] = st.actual;
    p[37] = state_bits();
    emit_small(EOL_T_STATS, &st.seq_stats, p, sizeof p);
    st.last_stats = st.now;
}

/* ---- outputs and the heartbeat watchdog ---- */

static void safety_update(void)
{
    bool fresh = st.hb_seen && (st.now - st.hb_last) <= HB_TIMEOUT_US;
    bool healthy = fresh && st.hb_app_alive;

    if (st.healthy && !healthy) {
        st.requested = 0; /* forgotten: a stale result must never come back */
        st.wd_trips++;
        send_event(EOL_EV_WATCHDOG_TRIP, fresh ? EOL_WD_APP_DEAD : EOL_WD_HB_TIMEOUT);
    } else if (!st.healthy && healthy) {
        send_event(EOL_EV_WATCHDOG_OK, 0);
    }
    st.healthy = healthy;

    uint8_t actual = 0;
    if (healthy) {
        actual = st.requested & EOL_OUT_HOST_MASK;
        if ((st.now / 500000u) & 1u) actual |= EOL_OUT_WATCHDOG; /* 1 Hz square wave */
    }
    if (actual != st.actual) {
        st.actual = actual;
        hal_set_outputs(actual);
    }
}

/* ---- synthetic stream ---- */

static void build_pattern(uint32_t seed)
{
    uint32_t x = seed ? seed : 1u;
    for (uint32_t i = 0; i < PATTERN_SIZE; i += 4) {
        x ^= x << 13;
        x ^= x >> 17;
        x ^= x << 5;
        eol_put_u32(&pattern[i], x);
    }
}

static void stream_frame(int kind, uint64_t t_us)
{
    uint32_t len = kind == PDM ? EOL_PDM_PAYLOAD : EOL_TACH_PAYLOAD;
    uint32_t n = st.gen[kind]++;
    uint32_t off = (n * len + (kind == TACH ? PATTERN_SIZE / 2 : 0)) & (PATTERN_SIZE - 1);
    uint32_t na = PATTERN_SIZE - off;
    if (na > len) na = len;
    uint8_t flags = (uint8_t)(EOL_F_SYNTH | (st.gap[kind] ? EOL_F_GAP : 0));

    if (emit(kind == PDM ? EOL_T_PDM : EOL_T_TACH, flags, n, t_us, &pattern[off], na, pattern,
             len - na, true)) {
        st.gap[kind] = false;
    } else {
        st.gap[kind] = true;
        st.dropped[kind]++;
        st.ovf_unreported++;
    }
}

/* ---- microphone stream ---- */

#define MIC_WAKE_US 100000u       /* standard-mode clock first, review section 3.2 */
#define MIC_SETTLE_US 20000u      /* a mode change takes 10 ms at most [MIC s3] */
#define MIC_PROBE_PERIODS 100000u /* about 31 ms at 3.2 MHz */
#define MIC_CHUNK_WORDS 256u
#define MIC_CHUNKS_PER_POLL 32
#define MIC_WORDS_PER_FRAME (EOL_PDM_PAYLOAD / 2u)

enum { MIC_WAKE, MIC_SETTLE, MIC_PROBE, MIC_RUN };

static uint8_t micbuf[EOL_PDM_PAYLOAD];

static struct {
    uint8_t phase, clock_code, forced, use, verdict;
    uint32_t clock_hz, frame_us, frames_target, fill;
    uint64_t t_phase, t_run;
    uint32_t periods, ones_l, ones_h, lh_10, lh_01, hl_10, hl_01;
} mic;

/* Bits 0, 2, 4 ... 30 of x, packed into 16 bits in the same order. */
static uint32_t even_bits(uint32_t x)
{
    x &= 0x55555555u;
    x = (x | (x >> 1)) & 0x33333333u;
    x = (x | (x >> 2)) & 0x0F0F0F0Fu;
    x = (x | (x >> 4)) & 0x00FF00FFu;
    x = (x | (x >> 8)) & 0x0000FFFFu;
    return x;
}

static uint32_t pop(uint32_t x) { return (uint32_t)__builtin_popcount(x); }

/* One raw word into the probe's counters. In l and h, bit 15 is the earliest period. */
static void probe_word(uint32_t w)
{
    uint32_t l = even_bits(w >> 1), h = even_bits(w);
    uint32_t l_next = (l << 1) & 0xFFFEu; /* L of the following period, lined up with H */
    mic.periods += 16;
    mic.ones_l += pop(l);
    mic.ones_h += pop(h);
    mic.lh_10 += pop(l & ~h & 0xFFFFu);
    mic.lh_01 += pop(~l & h & 0xFFFFu);
    mic.hl_10 += pop(h & ~l_next & 0xFFFEu);
    mic.hl_01 += pop(~h & l_next & 0xFFFEu);
}

static uint32_t min_u32(uint32_t a, uint32_t b) { return a < b ? a : b; }

/* Which half of the clock carries the data? (protocol/README.md, "The SELECT probe")

   The microphone drives the line in one half of the period and lets go in the other.
   Two samples of the SAME bit (driven, then released) can differ in one direction only:
   the direction the released line drifts in. Two samples either side of a bit boundary
   differ in both directions. So the pairing whose smaller count is about zero is the
   same-bit pairing. */
static uint8_t probe_verdict(void)
{
    uint32_t n = mic.periods, small = n / 64u, large = n / 16u;
    uint32_t same_if_gnd = min_u32(mic.lh_10, mic.lh_01); /* L then H is one bit if SELECT = GND */
    uint32_t same_if_vdd = min_u32(mic.hl_10, mic.hl_01); /* H then next L is one bit if VDD */

    if (same_if_gnd < small && same_if_vdd >= large) return EOL_MIC_SELECT_GND;
    if (same_if_vdd < small && same_if_gnd >= large) return EOL_MIC_SELECT_VDD;
    if (same_if_gnd < small && same_if_vdd < small) {
        /* Either nothing moves at all, or the released half always falls back to one
           level. In the second case the half that still varies is the data. */
        bool l_alive = mic.ones_l > n / 8u && mic.ones_l < n - n / 8u;
        bool h_alive = mic.ones_h > n / 8u && mic.ones_h < n - n / 8u;
        if (l_alive && !h_alive) return EOL_MIC_SELECT_GND;
        if (h_alive && !l_alive) return EOL_MIC_SELECT_VDD;
        return EOL_MIC_STUCK;
    }
    return EOL_MIC_AMBIGUOUS;
}

static void send_mic_status(void)
{
    uint8_t p[EOL_MIC_STATUS_LEN] = {mic.verdict, mic.use, mic.forced ? 1 : 0, mic.clock_code};
    eol_put_u32(p + 4, mic.clock_hz);
    eol_put_u32(p + 8, mic.periods);
    eol_put_u32(p + 12, mic.ones_l);
    eol_put_u32(p + 16, mic.ones_h);
    eol_put_u32(p + 20, mic.lh_10);
    eol_put_u32(p + 24, mic.lh_01);
    eol_put_u32(p + 28, mic.hl_10);
    eol_put_u32(p + 32, mic.hl_01);
    emit_small(EOL_T_MIC_STATUS, &st.seq_mic, p, sizeof p);
}

static void stream_stop(void);

static void mic_frame(void)
{
    uint32_t n = st.gen[PDM]++;
    uint8_t flags = st.gap[PDM] ? EOL_F_GAP : 0;
    if (emit(EOL_T_PDM, flags, n, mic.t_run + (uint64_t)n * mic.frame_us, micbuf, EOL_PDM_PAYLOAD,
             NULL, 0, true)) {
        st.gap[PDM] = false;
    } else {
        st.gap[PDM] = true;
        st.dropped[PDM]++;
        st.ovf_unreported++;
    }
    mic.fill = 0;
}

/* The capture fell behind and raw words were overwritten. Count the frames that went
   with them, so the frame numbers show the hole. */
static void mic_lost(uint32_t lost_words)
{
    uint32_t frames = (mic.fill / 2u + lost_words + MIC_WORDS_PER_FRAME - 1u) / MIC_WORDS_PER_FRAME;
    if (mic.frames_target && frames > mic.frames_target - st.gen[PDM])
        frames = mic.frames_target - st.gen[PDM]; /* never count past the end of the capture */
    st.gen[PDM] += frames;
    st.dropped[PDM] += frames;
    st.ovf_unreported += frames;
    st.gap[PDM] = true;
    mic.fill = 0;
    send_event(EOL_EV_MIC_OVERRUN, lost_words);
}

static void mic_start(const uint8_t *p, uint32_t duration_ms)
{
    memset(&mic, 0, sizeof mic);
    mic.clock_code = p[2];
    mic.forced = p[3];
    mic.clock_hz = p[2] ? EOL_MIC_CLOCK_1_HZ : EOL_MIC_CLOCK_0_HZ;
    mic.frame_us = (uint32_t)((uint64_t)EOL_PDM_PAYLOAD * 8u * 1000000u / mic.clock_hz);
    mic.frames_target = (uint32_t)((uint64_t)duration_ms * 1000u / mic.frame_us);
    mic.phase = MIC_WAKE;
    mic.t_phase = st.now;
    hal_mic_start(EOL_MIC_WAKE_HZ); /* never straight into the ultrasonic clock [MIC s5] */
}

static void mic_poll(void)
{
    uint32_t words[MIC_CHUNK_WORDS], lost;

    for (int chunk = 0; chunk < MIC_CHUNKS_PER_POLL && st.streaming; chunk++) {
        size_t n = hal_mic_read(words, MIC_CHUNK_WORDS, &lost);
        if (lost && mic.phase == MIC_RUN) {
            mic_lost(lost);
            if (mic.frames_target && st.gen[PDM] >= mic.frames_target) stream_stop();
        }
        if (n == 0) break;

        if (mic.phase == MIC_PROBE) {
            for (size_t i = 0; i < n; i++) probe_word(words[i]);
        } else if (mic.phase == MIC_RUN) {
            for (size_t i = 0; i < n && st.streaming; i++) {
                uint32_t v = mic.use == EOL_MIC_USE_H ? even_bits(words[i]) : even_bits(words[i] >> 1);
                micbuf[mic.fill++] = (uint8_t)(v >> 8);
                micbuf[mic.fill++] = (uint8_t)v;
                if (mic.fill < EOL_PDM_PAYLOAD) continue;
                mic_frame();
                if (mic.frames_target && st.gen[PDM] >= mic.frames_target) stream_stop();
            }
        } /* MIC_WAKE and MIC_SETTLE: read and thrown away */
    }
    if (!st.streaming) return;

    if (mic.phase == MIC_WAKE && st.now - mic.t_phase >= MIC_WAKE_US) {
        hal_mic_set_clock(mic.clock_hz);
        mic.phase = MIC_SETTLE;
        mic.t_phase = st.now;
    } else if (mic.phase == MIC_SETTLE && st.now - mic.t_phase >= MIC_SETTLE_US) {
        mic.phase = MIC_PROBE;
    } else if (mic.phase == MIC_PROBE && mic.periods >= MIC_PROBE_PERIODS) {
        mic.verdict = probe_verdict();
        if (mic.forced)
            mic.use = mic.forced; /* bench override: stream that half whatever the probe says */
        else if (mic.verdict == EOL_MIC_SELECT_GND)
            mic.use = EOL_MIC_USE_L;
        else if (mic.verdict == EOL_MIC_SELECT_VDD)
            mic.use = EOL_MIC_USE_H;
        send_mic_status();
        if (mic.use == EOL_MIC_USE_NONE) {
            stream_stop(); /* no microphone to listen to */
            return;
        }
        mic.phase = MIC_RUN;
        mic.t_run = st.now;
    }
}

/* ---- both kinds of stream ---- */

static void stream_stop(void)
{
    if (!st.streaming) return;
    if (st.src == SRC_MIC) hal_mic_stop();
    st.streaming = false;
    send_event(EOL_EV_STREAM_STOPPED, st.gen[PDM]);
}

static void stream_poll(void)
{
    if (!st.streaming) return;
    bool timed_out = st.duration_us && (st.now - st.t0) >= st.duration_us;

    if (st.src == SRC_MIC) {
        mic_poll();
    } else if (st.mode == MODE_REAL) {
        uint64_t limit = timed_out ? st.t0 + st.duration_us : st.now;
        for (int i = 0; i < MAX_FRAMES_PER_POLL && st.next_due < limit; i++) {
            stream_frame(PDM, st.next_due);
            stream_frame(TACH, st.next_due);
            st.next_due += FRAME_PERIOD_US;
        }
        if (timed_out && st.next_due >= limit) stream_stop();
    } else {
        size_t frame = EOL_HDR_LEN + EOL_PDM_PAYLOAD + EOL_CRC_LEN;
        for (int i = 0; i < MAX_FRAMES_PER_POLL && !timed_out && ring_free() >= frame + RING_RESERVE; i++)
            stream_frame(PDM, st.now);
        if (timed_out) stream_stop();
    }

    if (st.ovf_unreported && (st.now - st.last_ovf_event) >= STATS_PERIOD_US) {
        send_event(EOL_EV_TX_OVERFLOW, st.ovf_unreported);
        st.ovf_unreported = 0;
        st.last_ovf_event = st.now;
    }
}

/* ---- commands ---- */

static uint8_t cmd_stream_start(const uint8_t *p, uint16_t len)
{
    if (!st.hello_ok) return EOL_ST_BAD_VERSION;
    if (len < 12 || p[1] > MODE_MAX) return EOL_ST_BAD_ARG;
    if (p[0] == SRC_MIC) {
        if (!hal_mic_available()) return EOL_ST_NOT_IMPLEMENTED;
        /* p[2] clock: 0 or 1. p[3] which half to read: 0 = find out, 1 = L, 2 = H. */
        if (p[1] != MODE_REAL || p[2] > 1 || p[3] > EOL_MIC_USE_H) return EOL_ST_BAD_ARG;
    } else if (p[0] != SRC_SYNTH) {
        return EOL_ST_BAD_ARG;
    }
    if (st.streaming) return EOL_ST_BUSY;

    st.mode = p[1];
    st.seed = eol_get_u32(p + 4);
    uint32_t duration_ms = eol_get_u32(p + 8);
    if (duration_ms > 3600000u) return EOL_ST_BAD_ARG; /* 1 hour; keeps the microsecond count in 32 bits */
    st.src = p[0];
    st.duration_us = st.src == SRC_MIC ? 0 : duration_ms * 1000u; /* the microphone counts frames */
    st.gen[PDM] = st.gen[TACH] = 0;
    st.gap[PDM] = st.gap[TACH] = false;
    st.t0 = st.next_due = st.now;
    st.streaming = true;
    if (st.src == SRC_MIC)
        mic_start(p, duration_ms);
    else
        build_pattern(st.seed);
    return EOL_ST_OK;
}

static uint8_t cmd_set_outputs(const uint8_t *p, uint16_t len)
{
    if (!st.hello_ok) return EOL_ST_BAD_VERSION;
    if (len < 4 || (p[0] & ~EOL_OUT_HOST_MASK)) return EOL_ST_BAD_ARG;
    uint8_t results = p[0] & EOL_OUT_RESULT_MASK;
    if (results & (results - 1)) return EOL_ST_BAD_ARG; /* more than one result line */
    if (!st.healthy) return EOL_ST_SAFE_STATE;
    st.requested = p[0];
    safety_update();
    return EOL_ST_OK;
}

static void on_data_frame(void *ctx, uint8_t type, uint32_t seq, const uint8_t *p, uint16_t len)
{
    (void)ctx;
    uint8_t status = EOL_ST_OK;
    switch (type) {
    case EOL_T_HELLO:
        st.hello_ok = len >= 4 && p[0] == EOL_PROTO_MAJOR;
        send_info();
        status = st.hello_ok ? EOL_ST_OK : EOL_ST_BAD_VERSION;
        break;
    case EOL_T_STREAM_START:
        status = cmd_stream_start(p, len);
        break;
    case EOL_T_STREAM_STOP:
        stream_stop();
        break;
    case EOL_T_SET_OUTPUTS:
        status = cmd_set_outputs(p, len);
        break;
    case EOL_T_GET_STATS:
        send_stats();
        break;
    case EOL_T_REBOOT:
        if (!st.hello_ok)
            status = EOL_ST_BAD_VERSION;
        else if (len < 4 || eol_get_u32(p) != EOL_REBOOT_MAGIC)
            status = EOL_ST_BAD_ARG;
        else
            st.reboot_at = st.now + 100000u; /* after the ACK has gone out */
        break;
    default:
        return; /* unknown types are ignored, with no ACK */
    }
    send_ack(type, seq, status);
    if (type == EOL_T_STREAM_START && status == EOL_ST_OK)
        send_event(EOL_EV_STREAM_STARTED, st.src == SRC_MIC ? mic.clock_hz : st.seed);
}

static void on_hb_frame(void *ctx, uint8_t type, uint32_t seq, const uint8_t *p, uint16_t len)
{
    (void)ctx;
    (void)seq;
    if (type != EOL_T_HEARTBEAT || len < 8) return;
    st.hb_seen = true;
    st.hb_last = st.now;
    st.hb_app_alive = (p[4] & 1u) != 0;
    st.hb_count++;
    safety_update();

    uint8_t reply[8];
    memcpy(reply, p, 4); /* counter echo */
    reply[4] = st.actual;
    reply[5] = state_bits();
    eol_put_u16(reply + 6, EOL_HB_TIMEOUT_MS);

    size_t total = EOL_HDR_LEN + sizeof reply + EOL_CRC_LEN;
    if (hbtx_len + total > sizeof hbtx) return; /* host is not reading; skip this reply */
    uint8_t *f = hbtx + hbtx_len;
    eol_put_header(f, EOL_T_HB_STATUS, 0, sizeof reply, st.seq_hb++, st.now);
    memcpy(f + EOL_HDR_LEN, reply, sizeof reply);
    eol_put_u32(f + EOL_HDR_LEN + sizeof reply, eol_crc32(0, f, EOL_HDR_LEN + sizeof reply));
    hbtx_len += total;
}

/* ---- public ---- */

void core_init(void)
{
    memset(&st, 0, sizeof st);
    memset(&data_parser, 0, sizeof data_parser);
    memset(&hb_parser, 0, sizeof hb_parser);
    ring_head = ring_tail = 0;
    hbtx_len = 0;
    hal_set_outputs(0);
}

void core_poll(uint64_t now_us)
{
    st.now = now_us;
    safety_update();
    stream_poll();
    if (st.reboot_at && st.now >= st.reboot_at) {
        st.reboot_at = 0;
        if (st.streaming && st.src == SRC_MIC) hal_mic_stop();
        st.streaming = false;
        hal_set_outputs(0);
        hal_reboot_to_bootloader();
    }
    if (st.port_open && (st.now - st.last_stats) >= STATS_PERIOD_US &&
        (ring_head - ring_tail) <= STATS_BACKLOG_LIMIT)
        send_stats();
}

void core_rx_data(uint64_t now_us, const uint8_t *p, size_t n)
{
    st.now = now_us;
    eol_parser_feed(&data_parser, p, n, on_data_frame, NULL);
}

void core_rx_hb(uint64_t now_us, const uint8_t *p, size_t n)
{
    st.now = now_us;
    eol_parser_feed(&hb_parser, p, n, on_hb_frame, NULL);
}

void core_data_port_open(uint64_t now_us, bool open)
{
    st.now = now_us;
    if (open == st.port_open) return;
    st.port_open = open;
    st.hello_ok = false;
    if (st.streaming && st.src == SRC_MIC) hal_mic_stop();
    st.streaming = false;
    st.requested = 0;
    ring_tail = ring_head; /* discard anything queued for the old connection */
    data_parser.n = 0;
    if (open) st.last_stats = now_us;
    safety_update();
}

size_t core_tx_data_peek(const uint8_t **p)
{
    uint32_t idx = ring_tail & (RING_SIZE - 1);
    uint32_t used = ring_head - ring_tail;
    uint32_t contiguous = RING_SIZE - idx;
    *p = &ring[idx];
    return used < contiguous ? used : contiguous;
}

void core_tx_data_consume(size_t n) { ring_tail += (uint32_t)n; }

size_t core_tx_hb_peek(const uint8_t **p)
{
    *p = hbtx;
    return hbtx_len;
}

void core_tx_hb_consume(size_t n)
{
    hbtx_len -= n;
    memmove(hbtx, hbtx + n, hbtx_len);
}
