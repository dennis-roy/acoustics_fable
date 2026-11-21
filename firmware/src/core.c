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
    uint8_t mode;
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

    uint32_t seq_info, seq_event, seq_stats, seq_ack, seq_hb;
    uint32_t ring_high;
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
    eol_put_u32(p + 28, 0x1u | 0x8u); /* synthetic source, outputs. Mic and tach come in step 3 */
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

static void stream_stop(void)
{
    if (!st.streaming) return;
    st.streaming = false;
    send_event(EOL_EV_STREAM_STOPPED, st.gen[PDM]);
}

static void stream_poll(void)
{
    if (!st.streaming) return;
    bool timed_out = st.duration_us && (st.now - st.t0) >= st.duration_us;

    if (st.mode == MODE_REAL) {
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
    if (p[0] == SRC_MIC) return EOL_ST_NOT_IMPLEMENTED;
    if (p[0] != SRC_SYNTH) return EOL_ST_BAD_ARG;
    if (st.streaming) return EOL_ST_BUSY;

    st.mode = p[1];
    st.seed = eol_get_u32(p + 4);
    uint32_t duration_ms = eol_get_u32(p + 8);
    if (duration_ms > 3600000u) return EOL_ST_BAD_ARG; /* 1 hour; keeps the microsecond count in 32 bits */
    st.duration_us = duration_ms * 1000u;
    build_pattern(st.seed);
    st.gen[PDM] = st.gen[TACH] = 0;
    st.gap[PDM] = st.gap[TACH] = false;
    st.t0 = st.next_due = st.now;
    st.streaming = true;
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
    default:
        return; /* unknown types are ignored, with no ACK */
    }
    send_ack(type, seq, status);
    if (type == EOL_T_STREAM_START && status == EOL_ST_OK) send_event(EOL_EV_STREAM_STARTED, st.seed);
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
