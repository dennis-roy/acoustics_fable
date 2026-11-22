/* Pin map for the Pico 2 W. Source: docs/phase0/PHASE0_HARDWARE_DESIGN_REVIEW.md section 6.1.
   Pins marked "step 7" are reserved here so nothing else takes them; the firmware does
   not drive or read them yet. */
#ifndef EOL_BOARD_H
#define EOL_BOARD_H

#define PIN_MIC_CLK 2       /* PIO side-set, microphone clock (mic.c) */
#define PIN_MIC_DATA 3      /* PIO input, 1-bit microphone data (mic.c) */
#define PIN_TACH 6          /* step 7: tach input from the isolator */
#define PIN_START 7         /* step 7: Start input from the isolator */
#define PIN_START_DEMO_N 8  /* step 7: bench push button to GND */
#define PIN_OUT_BASE 10     /* GP10..GP15: OK, NOK, UNDEFINED, READY, BUSY, WATCHDOG */
#define OUT_PIN_MASK (0x3Fu << PIN_OUT_BASE)
#define PIN_CLK_LOOPBACK 20 /* bench, not used yet: jumper from GP2, CLOCK GPIN0, to measure the mic clock */

/* Pico 2 W only: GP23 powers the radio chip. Held low, the radio is off. */
#define PIN_WL_ON 23

#define USB_ITF_DATA 0
#define USB_ITF_HB 1

#endif
