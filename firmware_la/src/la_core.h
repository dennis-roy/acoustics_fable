/* Logic analyser on a second Pico 2: the hardware-free part (commands, statistics, encoding).
   Compiled into the firmware and, for tests, on a PC (host/la_core_test.c). */
#ifndef LA_CORE_H
#define LA_CORE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define LA_FW_VERSION "0.1.0"
#define LA_CHANNELS 8          /* inputs GP0..GP7 */
#define LA_BURST_MAX 262144u   /* samples (one byte each: bit n = channel n) */
#define LA_REF_PERIODS 64u     /* periods averaged as the reference before a change is looked for */
#define LA_AFTER_SKIP 16u      /* periods ignored right after a change */
#define LA_AFTER_PERIODS 4096u /* periods averaged after a change */
#define LA_IDLE_END_MS 20u     /* the signal has ended if no edge comes for this long */

/* Each phase (high or low) measured by la.pio lasts 2*n + LA_PHASE_OVERHEAD cycles, where n is the
   count the PIO pushes. Both phases have the same overhead, so the duty cycle is not biased. */
#define LA_PHASE_OVERHEAD 5u

typedef enum { LA_ID, LA_LEVELS, LA_WIDTHS, LA_BURST, LA_GEN, LA_GEN_OFF } la_kind_t;

typedef struct {
    la_kind_t kind;
    uint32_t ch;         /* widths: input channel; burst: trigger channel (or LA_NO_TRIGGER); gen: output pin */
    uint32_t count;      /* widths: periods; burst: samples */
    uint32_t div;        /* burst: system clock divider (sample rate = clk / div) */
    uint32_t timeout_ms;
    bool rising;         /* burst trigger edge */
    uint32_t hz, duty;   /* gen */
    uint32_t skip;       /* widths: periods ignored at the start (optional, default 0) */
} la_cmd_t;

#define LA_NO_TRIGGER 0xFFu

/* Parses one command line. Returns NULL, or a message saying what is wrong. */
const char *la_parse(const char *line, la_cmd_t *cmd);

typedef struct {
    uint32_t periods;
    uint64_t total_cycles;
    uint32_t min_high, max_high, min_low, max_low;
    uint64_t sum_high, sum_low;
    uint64_t ref_sum;          /* first LA_REF_PERIODS periods */
    int32_t change_at;         /* first period that differs from the reference by more than 1/8, or -1 */
    uint64_t cycles_to_change; /* cycles from the first rising edge to the start of that period */
    uint64_t after_sum;
    uint32_t after_n;
    uint32_t min_high_near_change, min_low_near_change; /* shortest phases within 16 periods of the change */
} la_widths_t;

void la_widths_init(la_widths_t *w);
/* One period: the PIO counts of its high phase and the low phase after it. */
void la_widths_add(la_widths_t *w, uint32_t high_count, uint32_t low_count);
/* JSON line describing the result. Returns its length. */
/* timed_out: no signal at all within the timeout. ended: the signal stopped after it had started (a normal end). */
int la_widths_json(const la_widths_t *w, uint32_t clk_hz, bool overrun, bool timed_out, bool ended, char *buf, size_t n);

/* Base64 (RFC 4648) of n bytes; out must hold 4 * ((n + 2) / 3) + 1 bytes. Returns the length. */
size_t la_base64(const uint8_t *in, size_t n, char *out);

/* PWM settings for a square wave: clock divider (integer 1..255) and wrap. Returns the frequency it gives. */
double la_pwm_config(uint32_t clk_hz, uint32_t hz, uint32_t *div, uint32_t *wrap);

#endif
