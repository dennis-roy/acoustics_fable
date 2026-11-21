/* What the firmware core needs from the board. Implemented in main.c for the Pico 2 W
   and in host/core_host.c for a PC build of the same core. */
#ifndef EOL_HAL_H
#define EOL_HAL_H

#include <stdint.h>

enum { EOL_BOARD_PICO2_W = 1, EOL_BOARD_HOST_BUILD = 0xFE, EOL_BOARD_PY_SIM = 0xFF };

/* Bit 0 OK ... bit 5 WATCHDOG. 0 = every output contact open. */
void hal_set_outputs(uint8_t mask);
void hal_unique_id(uint8_t id[8]);
uint32_t hal_sys_clk_hz(void);
/* System clock measured by the chip's frequency counter, in kHz. 0 if not available. */
uint32_t hal_fc_sys_khz(void);
uint8_t hal_board_id(void);

#endif
