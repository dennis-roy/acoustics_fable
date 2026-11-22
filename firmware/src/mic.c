/* Microphone clock and capture: one PIO state machine and one DMA channel.
   NOT TESTED ON HARDWARE. See firmware/README.md.

   The state machine (mic.pio) makes the clock and shifts in two samples per period.
   Every 32 samples it pushes a word; the DMA channel copies each word into a ring in
   memory, for ever, with no interrupt and no work for the processor. hal_mic_read()
   hands the words to the core from the main loop. */
#include "mic.h"

#include "board.h"
#include "hal.h"
#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/gpio.h"
#include "hardware/pio.h"
#include "mic.pio.h"
#include "pico/time.h"

/* 32 kB is the largest ring the DMA can wrap by itself [SDK channel_config_set_ring].
   At 3.2 MHz it holds 41 ms. */
#define RING_BITS 15u
#define RING_BYTES (1u << RING_BITS)
#define RING_WORDS (RING_BYTES / 4u)

static uint32_t ring[RING_WORDS] __attribute__((aligned(RING_BYTES)));
static PIO pio;
static uint sm, offset, dma_ch;
static bool running;
static uint32_t rd;          /* next word to hand out */
static uint32_t words_per_s; /* at the present clock */
static uint64_t caught_up_us; /* when the reader last had everything */

/* Clock low, without running the program. */
static void clock_low(void) { pio_sm_exec(pio, sm, pio_encode_nop() | pio_encode_sideset(1, 0)); }

static void set_divider(uint32_t clock_hz)
{
    uint32_t half_period = clock_get_hz(clk_sys) / (2u * clock_hz);
    hard_assert(half_period * 2u * clock_hz == clock_get_hz(clk_sys)); /* whole number only */
    pio_sm_set_clkdiv_int_frac8(pio, sm, half_period, 0);
    words_per_s = clock_hz / 16u; /* two samples per period, 32 samples per word */
}

void mic_init(void)
{
    bool ok = pio_claim_free_sm_and_add_program(&mic_capture_program, &pio, &sm, &offset);
    hard_assert(ok);
    dma_ch = (uint)dma_claim_unused_channel(true);

    pio_sm_config c = mic_capture_program_get_default_config(offset);
    sm_config_set_sideset_pins(&c, PIN_MIC_CLK);
    sm_config_set_in_pins(&c, PIN_MIC_DATA);
    sm_config_set_in_shift(&c, false, true, 32); /* shift left: the first sample ends in bit 31 */
    sm_config_set_fifo_join(&c, PIO_FIFO_JOIN_RX);
    pio_sm_init(pio, sm, offset, &c);

    /* Drive the clock pin low before the pad is handed to the PIO. */
    pio_sm_set_consecutive_pindirs(pio, sm, PIN_MIC_CLK, 1, true);
    clock_low();
    pio_gpio_init(pio, PIN_MIC_CLK);
    gpio_set_slew_rate(PIN_MIC_CLK, GPIO_SLEW_RATE_FAST); /* the microphone wants edges of 3 ns or less [MIC s4] */

    /* Data pin: input, no internal pull. The microphone drives it for half of every period. */
    pio_gpio_init(pio, PIN_MIC_DATA);
    gpio_disable_pulls(PIN_MIC_DATA);
}

bool hal_mic_available(void) { return true; }

void hal_mic_stop(void)
{
    if (!running) return;
    pio_sm_set_enabled(pio, sm, false);
    clock_low();
    /* RP2350-E5: clear the enable bit before aborting, or the channel can start again. */
    hw_clear_bits(&dma_hw->ch[dma_ch].al1_ctrl, DMA_CH0_CTRL_TRIG_EN_BITS);
    dma_channel_abort(dma_ch);
    running = false;
}

void hal_mic_start(uint32_t clock_hz)
{
    hal_mic_stop();
    set_divider(clock_hz);
    pio_sm_clear_fifos(pio, sm);
    pio_sm_restart(pio, sm); /* empties the shift register, so words line up with periods */
    pio_sm_clkdiv_restart(pio, sm);
    pio_sm_exec(pio, sm, pio_encode_jmp(offset));

    dma_channel_config d = dma_channel_get_default_config(dma_ch);
    channel_config_set_transfer_data_size(&d, DMA_SIZE_32);
    channel_config_set_read_increment(&d, false);
    channel_config_set_write_increment(&d, true);
    channel_config_set_ring(&d, true, RING_BITS);
    channel_config_set_dreq(&d, pio_get_dreq(pio, sm, false));
    /* Endless mode: the count never runs down, so the channel never stops [RP 12.6.2.2]. */
    uint32_t endless = (DMA_CH0_TRANS_COUNT_MODE_VALUE_ENDLESS << DMA_CH0_TRANS_COUNT_MODE_LSB) | 1u;
    dma_channel_configure(dma_ch, &d, ring, &pio->rxf[sm], endless, true);

    rd = 0;
    caught_up_us = time_us_64();
    running = true;
    pio_sm_set_enabled(pio, sm, true);
}

void hal_mic_set_clock(uint32_t clock_hz)
{
    if (running) set_divider(clock_hz);
}

size_t hal_mic_read(uint32_t *dst, size_t max_words, uint32_t *lost_words)
{
    *lost_words = 0;
    if (!running) return 0;

    uint64_t now = time_us_64();
    uint32_t wr = ((dma_hw->ch[dma_ch].write_addr - (uint32_t)(uintptr_t)ring) / 4u) % RING_WORDS;

    /* If the reader has been behind for longer than three quarters of the ring, words may
       have been overwritten. Throw the lot away and say so. */
    uint64_t behind_words = (now - caught_up_us) * words_per_s / 1000000u;
    if (behind_words > RING_WORDS * 3u / 4u) {
        *lost_words = (uint32_t)behind_words;
        rd = wr;
        caught_up_us = now;
        return 0;
    }

    size_t n = 0;
    while (rd != wr && n < max_words) {
        dst[n++] = ring[rd];
        rd = (rd + 1u) % RING_WORDS;
    }
    if (rd == wr) caught_up_us = now;
    return n;
}
