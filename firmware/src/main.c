/* Pico 2 W platform layer: clocks, pins, USB. All behaviour lives in core.c.
   NOT TESTED ON HARDWARE. See firmware/README.md. */
#include <stdio.h>
#include <string.h>

#include "board.h"
#include "core.h"
#include "hal.h"
#include "hardware/clocks.h"
#include "hardware/gpio.h"
#include "hardware/watchdog.h"
#include "mic.h"
#include "pico/bootrom.h"
#include "pico/stdlib.h"
#include "pico/unique_id.h"
#include "tusb.h"

#define HW_WATCHDOG_MS 1000u

static uint32_t fc_sys_khz;

void hal_set_outputs(uint8_t mask)
{
    gpio_put_masked(OUT_PIN_MASK, ((uint32_t)mask << PIN_OUT_BASE) & OUT_PIN_MASK);
}

void hal_unique_id(uint8_t id[8])
{
    pico_unique_board_id_t uid;
    pico_get_unique_board_id(&uid);
    memcpy(id, uid.id, 8);
}

uint32_t hal_sys_clk_hz(void) { return clock_get_hz(clk_sys); }
uint32_t hal_fc_sys_khz(void) { return fc_sys_khz; }
uint8_t hal_board_id(void) { return EOL_BOARD_PICO2_W; }

void hal_reboot_to_bootloader(void)
{
    reset_usb_boot(0, 0); /* does not return: the chip restarts as a USB drive */
}

static void board_init(void)
{
    /* Outputs first: low = every contact open. Pads are isolated until now. */
    gpio_init_mask(OUT_PIN_MASK);
    gpio_put_masked(OUT_PIN_MASK, 0);
    gpio_set_dir_out_masked(OUT_PIN_MASK);

    /* Radio off: never power the wireless chip. */
    gpio_init(PIN_WL_ON);
    gpio_put(PIN_WL_ON, 0);
    gpio_set_dir(PIN_WL_ON, GPIO_OUT);

    /* 12 MHz x 128 = 1536 MHz VCO, / 6 / 2 = 128 MHz (review section 5.1). */
    set_sys_clock_pll(1536000000u, 6, 2);
    stdio_init_all(); /* debug UART on GP0/GP1, after the clock change */
    fc_sys_khz = frequency_count_khz(CLOCKS_FC0_SRC_VALUE_CLK_SYS);
    printf("eol_pico %d.%d.%d (%s) clk_sys=%lu Hz, counted %lu kHz\n", EOL_FW_MAJOR, EOL_FW_MINOR,
           EOL_FW_PATCH, EOL_BUILD_TAG, (unsigned long)clock_get_hz(clk_sys),
           (unsigned long)fc_sys_khz);
    mic_init();
}

static void pump_rx(uint64_t now)
{
    uint8_t buf[64];
    while (tud_cdc_n_available(USB_ITF_DATA)) {
        uint32_t n = tud_cdc_n_read(USB_ITF_DATA, buf, sizeof buf);
        if (!n) break;
        core_rx_data(now, buf, n);
    }
    while (tud_cdc_n_available(USB_ITF_HB)) {
        uint32_t n = tud_cdc_n_read(USB_ITF_HB, buf, sizeof buf);
        if (!n) break;
        core_rx_hb(now, buf, n);
    }
}

static void pump_tx(void)
{
    const uint8_t *p;
    size_t n;

    while ((n = core_tx_data_peek(&p)) > 0) {
        uint32_t space = tud_cdc_n_write_available(USB_ITF_DATA);
        if (!space) break;
        uint32_t w = tud_cdc_n_write(USB_ITF_DATA, p, n < space ? (uint32_t)n : space);
        if (!w) break;
        core_tx_data_consume(w);
    }
    tud_cdc_n_write_flush(USB_ITF_DATA);

    if ((n = core_tx_hb_peek(&p)) > 0) {
        if (tud_cdc_n_connected(USB_ITF_HB)) {
            uint32_t space = tud_cdc_n_write_available(USB_ITF_HB);
            uint32_t w = tud_cdc_n_write(USB_ITF_HB, p, n < space ? (uint32_t)n : space);
            core_tx_hb_consume(w);
            tud_cdc_n_write_flush(USB_ITF_HB);
        } else {
            core_tx_hb_consume(n); /* nobody listening */
        }
    }
}

int main(void)
{
    board_init();
    core_init();
    tusb_init();

    /* If this loop ever stops, the chip resets and reset opens every output contact. */
    watchdog_enable(HW_WATCHDOG_MS, true);

    while (true) {
        tud_task();
        uint64_t now = time_us_64();
        core_data_port_open(now, tud_cdc_n_connected(USB_ITF_DATA));
        pump_rx(now);
        core_poll(now);
        pump_tx();
        watchdog_update();
    }
}
