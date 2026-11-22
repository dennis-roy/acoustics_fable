/* Logic analyser on a second Pico 2 (bench item list: docs/phase0/PHASE0_HARDWARE_DESIGN_REVIEW.md, 10.1).
   Commands arrive as text lines on USB serial; every answer is one JSON line (burst data follows as
   base64 lines and END). The core logic is in la_core.c. NOT TESTED ON HARDWARE.

   Inputs: GP0..GP7 (3.3 V logic only). Generator output: any of GP8..GP22. LED (GP25): on while
   measuring. */
#include <stdio.h>
#include <string.h>

#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/gpio.h"
#include "hardware/irq.h"
#include "hardware/pio.h"
#include "hardware/pwm.h"
#include "la.pio.h"
#include "la_core.h"
#include "pico/stdlib.h"

#define LED 25
#define RING_BITS 15u
#define RING_WORDS ((1u << RING_BITS) / 4u)

static uint32_t buf[LA_BURST_MAX / 4] __attribute__((aligned(1u << RING_BITS)));
static PIO pio = pio0;
static const uint sm = 0;
static uint dma_ch;
static volatile uint32_t wraps; /* times the widths ring has wrapped */
static int gen_pin = -1;
static char line[96], out[1024];

static void dma_irq(void)
{
    if (dma_channel_get_irq0_status(dma_ch)) {
        dma_channel_acknowledge_irq0(dma_ch);
        wraps++;
    }
}

static void stop_capture(void)
{
    pio_sm_set_enabled(pio, sm, false);
    dma_channel_set_irq0_enabled(dma_ch, false);
    hw_clear_bits(&dma_hw->ch[dma_ch].al1_ctrl, DMA_CH0_CTRL_TRIG_EN_BITS); /* RP2350-E5 */
    dma_channel_abort(dma_ch);
    pio_clear_instruction_memory(pio);
    gpio_put(LED, 0);
}

static void reply(const char *s) { printf("%s\n", s); }

static void cmd_widths(const la_cmd_t *c)
{
    uint offset = pio_add_program(pio, &la_widths_program);
    pio_sm_config cfg = la_widths_program_get_default_config(offset);
    sm_config_set_in_pins(&cfg, c->ch);
    sm_config_set_jmp_pin(&cfg, c->ch);
    sm_config_set_fifo_join(&cfg, PIO_FIFO_JOIN_RX);
    pio_sm_init(pio, sm, offset, &cfg);

    /* DMA: the PIO's words into a 32 kB ring, re-triggering itself at every wrap and counting wraps,
       so the reader can tell when it has fallen a whole ring behind. */
    dma_channel_config d = dma_channel_get_default_config(dma_ch);
    channel_config_set_transfer_data_size(&d, DMA_SIZE_32);
    channel_config_set_read_increment(&d, false);
    channel_config_set_write_increment(&d, true);
    channel_config_set_ring(&d, true, RING_BITS);
    channel_config_set_dreq(&d, pio_get_dreq(pio, sm, false));
    wraps = 0;
    dma_channel_acknowledge_irq0(dma_ch);
    dma_channel_set_irq0_enabled(dma_ch, true);
    uint32_t count = (DMA_CH0_TRANS_COUNT_MODE_VALUE_TRIGGER_SELF << DMA_CH0_TRANS_COUNT_MODE_LSB) | RING_WORDS;
    dma_channel_configure(dma_ch, &d, buf, &pio->rxf[sm], count, true);

    la_widths_t w;
    la_widths_init(&w);
    uint64_t done = 0; /* words consumed */
    bool overrun = false, timed_out = false, ended = false;
    uint32_t pending_high = 0, pairs = 0;
    absolute_time_t deadline = make_timeout_time_ms(c->timeout_ms), last_edge = nil_time;
    gpio_put(LED, 1);
    pio_sm_set_enabled(pio, sm, true);
    while (w.periods < c->count) {
        uint32_t n1, a, n2;
        do { /* a consistent (wraps, write position) pair */
            n1 = wraps;
            a = (dma_hw->ch[dma_ch].write_addr - (uint32_t)(uintptr_t)buf) / 4u % RING_WORDS;
            n2 = wraps;
        } while (n1 != n2);
        uint64_t produced = (uint64_t)n1 * RING_WORDS + a;
        if (produced < done) continue; /* the ring just wrapped and its interrupt has not counted it yet */
        if (produced - done > RING_WORDS - 64u) { overrun = true; break; }
        if (produced == done) {
            if (is_nil_time(last_edge)) {
                if (time_reached(deadline)) { timed_out = true; break; } /* never any signal */
            } else if (absolute_time_diff_us(last_edge, get_absolute_time()) > LA_IDLE_END_MS * 1000) {
                ended = true; /* the signal stopped: a normal end */
                break;
            }
            continue;
        }
        last_edge = get_absolute_time();
        while (done < produced && w.periods < c->count) {
            uint32_t v = buf[done % RING_WORDS]; /* the PIO pushes ~x, which is the count itself */
            if ((done & 1u) == 0) pending_high = v; /* words alternate: high first (la.pio), then low */
            else if (pairs++ >= c->skip) la_widths_add(&w, pending_high, v);
            done++;
        }
    }
    stop_capture();
    la_widths_json(&w, clock_get_hz(clk_sys), overrun, timed_out, ended, out, sizeof out);
    reply(out);
}

static void cmd_burst(const la_cmd_t *c)
{
    uint16_t prog[3];
    pio_program_t p = {.instructions = prog, .origin = -1};
    uint n = 0;
    if (c->ch != LA_NO_TRIGGER) { /* wait for the trigger edge, then sample */
        prog[n++] = pio_encode_wait_gpio(!c->rising, c->ch);
        prog[n++] = pio_encode_wait_gpio(c->rising, c->ch);
    }
    prog[n++] = la_burst_program_instructions[0];
    p.length = (uint8_t)n;
    uint offset = pio_add_program(pio, &p);
    pio_sm_config cfg = la_burst_program_get_default_config(offset);
    sm_config_set_wrap(&cfg, offset + n - 1, offset + n - 1);
    sm_config_set_in_pins(&cfg, 0);
    sm_config_set_in_shift(&cfg, true, true, 32); /* first sample in the lowest byte: bytes are in time order */
    sm_config_set_fifo_join(&cfg, PIO_FIFO_JOIN_RX);
    sm_config_set_clkdiv_int_frac8(&cfg, c->div, 0);
    pio_sm_init(pio, sm, offset, &cfg);

    dma_channel_config d = dma_channel_get_default_config(dma_ch);
    channel_config_set_transfer_data_size(&d, DMA_SIZE_32);
    channel_config_set_read_increment(&d, false);
    channel_config_set_write_increment(&d, true);
    channel_config_set_dreq(&d, pio_get_dreq(pio, sm, false));
    dma_channel_configure(dma_ch, &d, buf, &pio->rxf[sm], c->count / 4u, true);
    gpio_put(LED, 1);
    pio_sm_set_enabled(pio, sm, true);
    absolute_time_t deadline = make_timeout_time_ms(c->timeout_ms);
    while (dma_channel_is_busy(dma_ch) && !time_reached(deadline)) tight_loop_contents();
    bool complete = !dma_channel_is_busy(dma_ch);
    stop_capture();
    if (!complete) {
        reply(c->ch == LA_NO_TRIGGER ? "{\"error\":\"burst did not finish in time\"}" : "{\"error\":\"trigger not seen before the timeout\"}");
        return;
    }
    snprintf(out, sizeof out, "{\"mode\":\"burst\",\"clk_hz\":%lu,\"div\":%lu,\"samples\":%lu,\"trigger\":%d,\"rising\":%s}",
             (unsigned long)clock_get_hz(clk_sys), (unsigned long)c->div, (unsigned long)c->count,
             c->ch == LA_NO_TRIGGER ? -1 : (int)c->ch, c->rising ? "true" : "false");
    reply(out);
    const uint8_t *bytes = (const uint8_t *)buf;
    char chunk[4 * 57 / 3 + 4];
    for (size_t i = 0; i < c->count; i += 57) { /* 57 bytes -> 76 characters per line */
        size_t m = c->count - i < 57 ? c->count - i : 57;
        la_base64(bytes + i, m, chunk);
        reply(chunk);
    }
    reply("END");
}

static void cmd_gen(const la_cmd_t *c)
{
    if (gen_pin >= 0) {
        pwm_set_enabled(pwm_gpio_to_slice_num((uint)gen_pin), false);
        gpio_deinit((uint)gen_pin);
        gen_pin = -1;
    }
    if (c->kind == LA_GEN_OFF) {
        reply("{\"gen\":\"off\"}");
        return;
    }
    uint32_t div, wrap;
    double actual = la_pwm_config(clock_get_hz(clk_sys), c->hz, &div, &wrap);
    uint slice = pwm_gpio_to_slice_num(c->ch);
    gpio_set_function(c->ch, GPIO_FUNC_PWM);
    pwm_set_clkdiv_int_frac4(slice, (uint8_t)div, 0);
    pwm_set_wrap(slice, (uint16_t)wrap);
    pwm_set_chan_level(slice, pwm_gpio_to_channel(c->ch), (uint16_t)((wrap + 1u) * c->duty / 100u));
    pwm_set_enabled(slice, true);
    gen_pin = (int)c->ch;
    snprintf(out, sizeof out, "{\"gen\":\"on\",\"pin\":%lu,\"hz\":%.4f,\"duty\":%lu}", (unsigned long)c->ch, actual, (unsigned long)c->duty);
    reply(out);
}

int main(void)
{
    stdio_init_all();
    gpio_init(LED);
    gpio_set_dir(LED, GPIO_OUT);
    for (uint i = 0; i < LA_CHANNELS; i++) {
        pio_gpio_init(pio, i); /* inputs, read by the PIO (its pin directions start as inputs) */
        gpio_disable_pulls(i);
    }
    dma_ch = (uint)dma_claim_unused_channel(true);
    irq_set_exclusive_handler(DMA_IRQ_0, dma_irq);
    irq_set_enabled(DMA_IRQ_0, true);

    size_t len = 0;
    while (true) {
        int ch = getchar_timeout_us(100000);
        if (ch == PICO_ERROR_TIMEOUT) continue;
        if (ch != '\n' && ch != '\r') {
            if (len + 1 < sizeof line) line[len++] = (char)ch;
            continue;
        }
        if (!len) continue;
        line[len] = 0;
        len = 0;
        la_cmd_t c;
        const char *err = la_parse(line, &c);
        if (err) {
            snprintf(out, sizeof out, "{\"error\":\"%s\"}", err);
            reply(out);
            continue;
        }
        switch (c.kind) {
        case LA_ID:
            snprintf(out, sizeof out, "{\"fw\":\"eol-la %s\",\"board\":\"pico2\",\"clk_hz\":%lu,\"channels\":%u}",
                     LA_FW_VERSION, (unsigned long)clock_get_hz(clk_sys), LA_CHANNELS);
            reply(out);
            break;
        case LA_LEVELS:
            snprintf(out, sizeof out, "{\"levels\":%lu}", (unsigned long)(gpio_get_all() & 0xFFu));
            reply(out);
            break;
        case LA_WIDTHS: cmd_widths(&c); break;
        case LA_BURST: cmd_burst(&c); break;
        case LA_GEN:
        case LA_GEN_OFF: cmd_gen(&c); break;
        }
    }
}
