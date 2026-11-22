/* Wire format shared with the CM5. The contract is protocol/README.md. */
#ifndef EOL_PROTO_H
#define EOL_PROTO_H

#include <stddef.h>
#include <stdint.h>

#define EOL_PROTO_MAJOR 1
#define EOL_PROTO_MINOR 1

#define EOL_HDR_LEN 24u
#define EOL_CRC_LEN 4u
#define EOL_MAX_PAYLOAD 4096u
#define EOL_PDM_PAYLOAD 4000u /* 10 ms at 3.2 Mbit/s */
#define EOL_TACH_PAYLOAD 250u /* 10 ms at 200 kbit/s */
#define EOL_HB_TIMEOUT_MS 500u
#define EOL_MIC_STATUS_LEN 40u
#define EOL_REBOOT_MAGIC 0x544F4F42u /* the bytes "BOOT" */

/* Microphone clocks, review section 5.2. STREAM_START picks 0 or 1; the wake-up clock
   always runs first. Each must divide the system clock by an even whole number. */
#define EOL_MIC_WAKE_HZ 2000000u
#define EOL_MIC_CLOCK_0_HZ 3200000u
#define EOL_MIC_CLOCK_1_HZ 4000000u

enum {
    EOL_T_INFO = 0x01,
    EOL_T_PDM = 0x02,
    EOL_T_TACH = 0x03,
    EOL_T_EVENT = 0x04,
    EOL_T_STATS = 0x05,
    EOL_T_ACK = 0x06,
    EOL_T_MIC_STATUS = 0x07,
    EOL_T_HB_STATUS = 0x10,
    EOL_T_HELLO = 0x81,
    EOL_T_STREAM_START = 0x82,
    EOL_T_STREAM_STOP = 0x83,
    EOL_T_SET_OUTPUTS = 0x84,
    EOL_T_GET_STATS = 0x85,
    EOL_T_REBOOT = 0x86,
    EOL_T_HEARTBEAT = 0x90,
};

enum { EOL_F_GAP = 0x01, EOL_F_SYNTH = 0x02 };

enum {
    EOL_ST_OK = 0,
    EOL_ST_BAD_VERSION = 1,
    EOL_ST_BAD_ARG = 2,
    EOL_ST_NOT_IMPLEMENTED = 3,
    EOL_ST_BUSY = 4,
    EOL_ST_SAFE_STATE = 5,
};

enum {
    EOL_EV_STREAM_STARTED = 1,
    EOL_EV_STREAM_STOPPED = 2,
    EOL_EV_TX_OVERFLOW = 3,
    EOL_EV_WATCHDOG_TRIP = 4,
    EOL_EV_WATCHDOG_OK = 5,
    EOL_EV_MIC_OVERRUN = 6,
};

enum { EOL_WD_HB_TIMEOUT = 1, EOL_WD_APP_DEAD = 2 };

/* What the SELECT probe found (MIC_STATUS), and which half of the clock is forwarded. */
enum {
    EOL_MIC_NOT_PROBED = 0,
    EOL_MIC_SELECT_GND = 1, /* data valid in the low half: read at the rising edge */
    EOL_MIC_SELECT_VDD = 2, /* data valid in the high half: read at the falling edge */
    EOL_MIC_STUCK = 3,      /* the line never changes: no microphone, no power or no clock */
    EOL_MIC_AMBIGUOUS = 4,  /* changes, but fits neither setting */
};
enum { EOL_MIC_USE_NONE = 0, EOL_MIC_USE_L = 1, EOL_MIC_USE_H = 2 };

/* Output mask bits. Bit 5 is driven by the firmware only. */
enum {
    EOL_OUT_OK = 0x01,
    EOL_OUT_NOK = 0x02,
    EOL_OUT_UNDEFINED = 0x04,
    EOL_OUT_READY = 0x08,
    EOL_OUT_BUSY = 0x10,
    EOL_OUT_WATCHDOG = 0x20,
    EOL_OUT_HOST_MASK = 0x1F,
    EOL_OUT_RESULT_MASK = 0x07,
};

enum { EOL_STATE_HELLO = 0x01, EOL_STATE_STREAMING = 0x02, EOL_STATE_HEALTHY = 0x04 };

/* CRC-32 (IEEE 802.3), chainable like zlib: start with crc = 0. */
uint32_t eol_crc32(uint32_t crc, const uint8_t *p, size_t n);

void eol_put_u16(uint8_t *p, uint16_t v);
void eol_put_u32(uint8_t *p, uint32_t v);
uint16_t eol_get_u16(const uint8_t *p);
uint32_t eol_get_u32(const uint8_t *p);

/* Fills the 24-byte header, including its own CRC. */
void eol_put_header(uint8_t hdr[EOL_HDR_LEN], uint8_t type, uint8_t flags, uint16_t len,
                    uint32_t seq, uint64_t t_us);

/* Incoming frames (commands and heartbeats are small). */
#define EOL_RX_MAX_PAYLOAD 64u
#define EOL_RX_BUF (EOL_HDR_LEN + EOL_RX_MAX_PAYLOAD + EOL_CRC_LEN)

typedef struct {
    uint8_t buf[EOL_RX_BUF];
    size_t n;
    uint32_t junk; /* bytes discarded while resynchronising */
} eol_parser_t;

typedef void (*eol_frame_cb)(void *ctx, uint8_t type, uint32_t seq, const uint8_t *payload,
                             uint16_t len);

void eol_parser_feed(eol_parser_t *ps, const uint8_t *p, size_t n, eol_frame_cb cb, void *ctx);

#endif
