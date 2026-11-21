/* PC build of the firmware core.

   This compiles the SAME core.c and proto.c that go into the Pico, and gives them two
   pseudo-terminals in place of the two USB serial ports. The CM5 receiver can then be
   tested against the real firmware logic without a Pico.

   It prints:
     PORTS <data port path> <heartbeat port path>     once, at start
     OUT <microseconds> <mask>                        each time the outputs change

   What it does NOT cover: main.c, usb_descriptors.c, the clocks, the pins and USB itself. */
#define _GNU_SOURCE
#define _DARWIN_C_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

#include "core.h"
#include "hal.h"

static uint64_t now_us(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000u + (uint64_t)ts.tv_nsec / 1000u;
}

void hal_set_outputs(uint8_t mask) { printf("OUT %llu %u\n", (unsigned long long)now_us(), mask); }
void hal_unique_id(uint8_t id[8]) { memcpy(id, "HOSTCORE", 8); }
uint32_t hal_sys_clk_hz(void) { return 128000000u; }
uint32_t hal_fc_sys_khz(void) { return 0; }
uint8_t hal_board_id(void) { return EOL_BOARD_HOST_BUILD; }

/* Opens a pseudo-terminal in raw mode. Returns the master side; the path of the other
   side goes to `name`. */
static int open_pty(char *name, size_t size)
{
    int master = posix_openpt(O_RDWR | O_NOCTTY);
    if (master < 0 || grantpt(master) || unlockpt(master)) {
        perror("pty");
        exit(1);
    }
    snprintf(name, size, "%s", ptsname(master));

    /* Keep our own handle on the other side, so the master never sees a hang-up when a
       client closes, and set raw mode there (no echo, no line editing). */
    int slave = open(name, O_RDWR | O_NOCTTY);
    struct termios t;
    if (slave < 0 || tcgetattr(slave, &t)) {
        perror("pty slave");
        exit(1);
    }
    cfmakeraw(&t);
    tcsetattr(slave, TCSANOW, &t);
    fcntl(master, F_SETFL, O_NONBLOCK);
    return master;
}

int main(void)
{
    char data_name[128], hb_name[128];
    uint8_t buf[4096];

    setvbuf(stdout, NULL, _IOLBF, 0);
    int data = open_pty(data_name, sizeof data_name);
    int hb = open_pty(hb_name, sizeof hb_name);

    printf("PORTS %s %s\n", data_name, hb_name);
    core_init();
    core_data_port_open(now_us(), true);

    while (getppid() != 1) { /* stop if the test that started us has gone */
        struct pollfd fds[2] = {{data, POLLIN, 0}, {hb, POLLIN, 0}};
        poll(fds, 2, 1);
        uint64_t now = now_us();
        ssize_t n;

        while ((n = read(data, buf, sizeof buf)) > 0) core_rx_data(now, buf, (size_t)n);
        while ((n = read(hb, buf, sizeof buf)) > 0) core_rx_hb(now, buf, (size_t)n);
        core_poll(now);

        const uint8_t *p;
        size_t len;
        while ((len = core_tx_data_peek(&p)) > 0 && (n = write(data, p, len)) > 0)
            core_tx_data_consume((size_t)n);
        if ((len = core_tx_hb_peek(&p)) > 0 && (n = write(hb, p, len)) > 0)
            core_tx_hb_consume((size_t)n);
    }
    return 0;
}
