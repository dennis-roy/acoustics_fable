/* PC build of the firmware core.

   This compiles the SAME core.c and proto.c that go into the Pico, and gives them two
   pseudo-terminals in place of the two USB serial ports. The CM5 receiver can then be
   tested against the real firmware logic without a Pico.

   It prints:
     PORTS <data port path> <heartbeat port path>     once, at start
     OUT <microseconds> <mask>                        each time the outputs change
     MIC <hertz>                                      each time the microphone clock changes; 0 = stopped
     REBOOT                                           on the restart-to-bootloader command, then it exits

   It also stands in for the microphone: see "a simulated microphone" below.

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

/* ---- a simulated microphone ----

   The signal is the one defined in protocol/README.md ("Simulated microphone signal"),
   so a test can check every bit that comes out of the core.

   Environment:
     EOL_SIM_MIC      gnd (default) | vdd | stuck | absent     how SELECT is tied, or a fault
     EOL_SIM_RELEASE  low (default) | hold | high              what the line does when the
                                                                microphone lets go of it */
enum { SIM_GND, SIM_VDD, SIM_STUCK, SIM_ABSENT };
enum { REL_LOW, REL_HOLD, REL_HIGH };
#define SIM_SIGNAL_PERIODS 320000u
#define SIM_RING_WORDS 8192u

static struct {
    int kind, release;
    bool running;
    uint32_t clock_hz, n;
    uint64_t t0, produced;
    int64_t v1, v2;
    int prev_released;
} sim;

static int sim_bit(void)
{
    if (sim.n == 0) sim.v1 = sim.v2 = 0;
    int64_t x = 16384 * ((sim.n % 3200u) < 1600u ? 1 : -1) + 4096 * ((sim.n % 100u) < 50u ? 1 : -1);
    int y = sim.v2 >= 0 ? 1 : -1;
    sim.v1 += x - 65536 * y;
    sim.v2 += sim.v1 - 65536 * y;
    sim.n = (sim.n + 1u) % SIM_SIGNAL_PERIODS;
    return y > 0;
}

static int sim_released(int bit) { return sim.release == REL_HOLD ? bit : sim.release == REL_HIGH; }

static uint32_t sim_word(void)
{
    uint32_t w = 0;
    for (int i = 0; i < 16; i++) {
        int l = 0, h = 0;
        if (sim.kind == SIM_GND) { /* driven in the low half, released in the high half */
            l = sim_bit();
            h = sim_released(l);
        } else if (sim.kind == SIM_VDD) { /* driven in the high half, released in the next low half */
            l = sim.prev_released;
            h = sim_bit();
            sim.prev_released = sim_released(h);
        }
        w = (w << 2) | (uint32_t)(l << 1 | h);
    }
    return w;
}

bool hal_mic_available(void) { return sim.kind != SIM_ABSENT; }

void hal_mic_set_clock(uint32_t clock_hz)
{
    printf("MIC %u\n", clock_hz);
    sim.clock_hz = clock_hz;
    sim.t0 = now_us();
    sim.produced = 0;
}

void hal_mic_start(uint32_t clock_hz)
{
    hal_mic_set_clock(clock_hz);
    sim.running = true;
}

void hal_mic_stop(void)
{
    if (sim.running) printf("MIC 0\n");
    sim.running = false;
}

size_t hal_mic_read(uint32_t *dst, size_t max_words, uint32_t *lost_words)
{
    *lost_words = 0;
    if (!sim.running) return 0;
    uint64_t due = (now_us() - sim.t0) * (sim.clock_hz / 16u) / 1000000u;
    uint64_t waiting = due - sim.produced;
    if (waiting > SIM_RING_WORDS * 3u / 4u) { /* the reader came too late, as on the Pico */
        *lost_words = (uint32_t)waiting;
        sim.n = (uint32_t)((sim.n + waiting * 16u) % SIM_SIGNAL_PERIODS);
        sim.produced = due;
        return 0;
    }
    size_t n = waiting < max_words ? (size_t)waiting : max_words;
    for (size_t i = 0; i < n; i++) dst[i] = sim_word();
    sim.produced += n;
    return n;
}

void hal_reboot_to_bootloader(void)
{
    printf("REBOOT\n");
    exit(0);
}

static void sim_configure(void)
{
    const char *kind = getenv("EOL_SIM_MIC"), *release = getenv("EOL_SIM_RELEASE");
    sim.kind = !kind || !strcmp(kind, "gnd") ? SIM_GND
               : !strcmp(kind, "vdd") ? SIM_VDD
               : !strcmp(kind, "stuck") ? SIM_STUCK
               : SIM_ABSENT;
    sim.release = !release || !strcmp(release, "low") ? REL_LOW : !strcmp(release, "hold") ? REL_HOLD : REL_HIGH;
}

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

    sim_configure();
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
