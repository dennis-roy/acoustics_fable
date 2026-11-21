/* The firmware's logic, free of any hardware call except hal.h.
   The platform loop feeds it received bytes and the time, and drains its transmit buffers. */
#ifndef EOL_CORE_H
#define EOL_CORE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define EOL_FW_MAJOR 0
#define EOL_FW_MINOR 2
#define EOL_FW_PATCH 0

#ifndef EOL_BUILD_TAG
#define EOL_BUILD_TAG "dev"
#endif

void core_init(void);

/* Call often (at least every millisecond). */
void core_poll(uint64_t now_us);

void core_rx_data(uint64_t now_us, const uint8_t *p, size_t n); /* bytes from the DATA port */
void core_rx_hb(uint64_t now_us, const uint8_t *p, size_t n);   /* bytes from the HB port */

/* The DATA port was opened or closed by the host. */
void core_data_port_open(uint64_t now_us, bool open);

/* Transmit side: look at the next contiguous bytes, then say how many were sent. */
size_t core_tx_data_peek(const uint8_t **p);
void core_tx_data_consume(size_t n);
size_t core_tx_hb_peek(const uint8_t **p);
void core_tx_hb_consume(size_t n);

#endif
