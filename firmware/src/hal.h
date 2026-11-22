/* What the firmware core needs from the board. Implemented in main.c for the Pico 2 W
   and in host/core_host.c for a PC build of the same core. */
#ifndef EOL_HAL_H
#define EOL_HAL_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

enum { EOL_BOARD_PICO2_W = 1, EOL_BOARD_HOST_BUILD = 0xFE, EOL_BOARD_PY_SIM = 0xFF };

/* Bit 0 OK ... bit 5 WATCHDOG. 0 = every output contact open. */
void hal_set_outputs(uint8_t mask);
void hal_unique_id(uint8_t id[8]);
uint32_t hal_sys_clk_hz(void);
/* System clock measured by the chip's frequency counter, in kHz. 0 if not available. */
uint32_t hal_fc_sys_khz(void);
uint8_t hal_board_id(void);

/* Microphone clock and capture.

   The line is read twice in every clock period: "L" at the rising edge (the line as it
   was at the end of the low half) and "H" at the falling edge (the end of the high
   half). A raw word holds 16 periods: bit 31 is the earliest sample, and the samples
   alternate L, H, L, H. Which of the two carries the data depends on the microphone's
   SELECT pin; the core works that out (review section 3.4). */
bool hal_mic_available(void);
void hal_mic_start(uint32_t clock_hz);     /* clock on, capture from a clean start */
void hal_mic_set_clock(uint32_t clock_hz); /* change the clock without stopping it */
void hal_mic_stop(void);                   /* clock held low: the microphone sleeps */
/* Copies up to max_words raw words. If the caller came too late and words were
   overwritten, returns 0 and says how many were lost. */
size_t hal_mic_read(uint32_t *dst, size_t max_words, uint32_t *lost_words);

/* Restart into the USB bootloader, so new firmware can be copied without the button. */
void hal_reboot_to_bootloader(void);

#endif
