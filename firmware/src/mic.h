/* Microphone clock and capture on the Pico (mic.c, mic.pio). The functions the core
   uses are declared in hal.h. */
#ifndef EOL_MIC_H
#define EOL_MIC_H

/* Call once, after the system clock is set. Leaves the microphone clock held low. */
void mic_init(void);

#endif
