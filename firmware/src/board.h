/* Pin map for the Pico 2 W. Source: docs/phase0/PHASE0_HARDWARE_DESIGN_REVIEW.md section 6.1.
   Pins marked "step 3" or "step 7" are reserved here so nothing else takes them; this
   firmware step does not drive or read them. */
#ifndef EOL_BOARD_H
#define EOL_BOARD_H

#define PIN_MIC_CLK 2       /* step 3: PIO side-set, microphone clock */
#define PIN_MIC_DATA 3      /* step 3: PIO input, 1-bit microphone data */
#define PIN_TACH 6          /* step 3: tach input from the isolator */
#define PIN_START 7         /* step 7: Start input from the isolator */
#define PIN_START_DEMO_N 8  /* step 7: bench push button to GND */
#define PIN_OUT_BASE 10     /* GP10..GP15: OK, NOK, UNDEFINED, READY, BUSY, WATCHDOG */
#define OUT_PIN_MASK (0x3Fu << PIN_OUT_BASE)
#define PIN_CLK_LOOPBACK 20 /* step 3: jumper from GP2, CLOCK GPIN0, to measure the mic clock */

/* Pico 2 W only: GP23 powers the radio chip. Held low, the radio is off. */
#define PIN_WL_ON 23

#define USB_ITF_DATA 0
#define USB_ITF_HB 1

#endif
