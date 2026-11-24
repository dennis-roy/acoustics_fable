/* Tests of la_core.c on a PC: cc -std=c11 -Wall -Wextra -Werror -Isrc host/la_core_test.c src/la_core.c */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "la_core.h"

static int failures;
#define CHECK(cond) do { if (!(cond)) { printf("FAIL line %d: %s\n", __LINE__, #cond); failures++; } } while (0)

/* PIO count for a phase of `cycles` (inverse of 2n + overhead). */
static uint32_t count_for(uint32_t cycles) { return (cycles - LA_PHASE_OVERHEAD) / 2u; }

static void test_parse(void)
{
    la_cmd_t c;
    CHECK(!la_parse("id", &c) && c.kind == LA_ID);
    CHECK(!la_parse("levels\r", &c) && c.kind == LA_LEVELS);
    CHECK(!la_parse("widths 3 1000000 500", &c) && c.kind == LA_WIDTHS && c.ch == 3 && c.count == 1000000 && c.timeout_ms == 500 && c.skip == 0);
    CHECK(!la_parse("widths 0 10 10 300000", &c) && c.skip == 300000);
    CHECK(la_parse("widths 0 10 10 x", &c) != NULL);
    CHECK(la_parse("widths 8 10 10", &c) != NULL);
    CHECK(la_parse("widths 0 0 10", &c) != NULL);
    CHECK(la_parse("widths 0 10", &c) != NULL);
    CHECK(!la_parse("burst 4096 1 0 r 1000", &c) && c.kind == LA_BURST && c.count == 4096 && c.div == 1 && c.ch == 0 && c.rising);
    CHECK(!la_parse("burst 8 150 - f 10", &c) && c.ch == LA_NO_TRIGGER && !c.rising);
    CHECK(la_parse("burst 4097 1 0 r 10", &c) != NULL);   /* not a multiple of 4 */
    CHECK(la_parse("burst 262148 1 0 r 10", &c) != NULL); /* too many */
    CHECK(la_parse("burst 8 0 0 r 10", &c) != NULL);
    CHECK(la_parse("burst 8 1 0 x 10", &c) != NULL);
    CHECK(!la_parse("gen 15 100 50", &c) && c.kind == LA_GEN && c.ch == 15 && c.hz == 100 && c.duty == 50);
    CHECK(la_parse("gen 3 100 50", &c) != NULL);  /* an input pin */
    CHECK(la_parse("gen 15 100 100", &c) != NULL);
    CHECK(!la_parse("gen off", &c) && c.kind == LA_GEN_OFF);
    CHECK(la_parse("explode", &c) != NULL);
    CHECK(la_parse("", &c) != NULL);
    CHECK(la_parse("widths 1 10x 10", &c) != NULL);
}

static void test_widths_wakeup(void)
{
    /* The microphone wake-up seen by a 150 MHz analyser: 2.000 MHz (75 cycles) for 100 ms, then
       3.200 MHz (46.875 cycles). Measured phases are always odd (2n + 5), so the analyser sees 37 then
       37 or 39 alternately for 2 MHz (75 on average), and 23 then 23 or 25 for 3.2 MHz (47 on average). */
    la_widths_t w;
    la_widths_init(&w);
    uint64_t cycles = 0;
    uint32_t n_slow = 200000; /* 100 ms of 2 MHz */
    for (uint32_t i = 0; i < n_slow; i++) {
        la_widths_add(&w, count_for(37), count_for(i & 1 ? 39 : 37));
        cycles += i & 1 ? 76 : 74;
    }
    for (uint32_t i = 0; i < 100000; i++) la_widths_add(&w, count_for(23), count_for(i & 1 ? 25 : 23));
    CHECK(w.periods == 300000);
    CHECK(w.change_at == (int32_t)n_slow);
    CHECK(w.cycles_to_change == cycles);
    double ms = w.cycles_to_change / 150e3;
    CHECK(fabs(ms - 100.0) < 1.0);
    CHECK(w.after_n == LA_AFTER_PERIODS);
    double after = (double)w.after_sum / w.after_n;
    CHECK(fabs(after - 47.0) < 1.5);
    CHECK(w.min_high_near_change >= 22 && w.min_high_near_change <= 24);
    char buf[1024];
    int n = la_widths_json(&w, 150000000u, false, false, true, buf, sizeof buf);
    CHECK(n > 0 && n < (int)sizeof buf);
    CHECK(strstr(buf, "\"change_at\":200000") != NULL);
    CHECK(strstr(buf, "\"overrun\":false") != NULL && strstr(buf, "\"ended\":true") != NULL);
}

static void test_widths_glitch_and_steady(void)
{
    la_widths_t w;
    la_widths_init(&w);
    for (int i = 0; i < 1000; i++) la_widths_add(&w, count_for(23), count_for(i & 1 ? 25 : 23));
    CHECK(w.change_at == -1); /* steady: no change */
    CHECK(w.min_high == 23 && w.max_low == 25);
    /* A glitch that keeps the period (7 + 39 = 46): not a change of frequency, but the shortest phase shows it.
       Bench item 9 uses the shortest phase over the whole capture for exactly this reason. */
    la_widths_add(&w, count_for(7), count_for(39));
    CHECK(w.change_at == -1 && w.min_high == 7);
    /* A glitch that shortens the period (7 + 25) is also a change, and is caught near it. */
    la_widths_add(&w, count_for(7), count_for(25));
    CHECK(w.change_at == 1001);
    CHECK(w.min_high_near_change == 7);
    la_widths_t e;
    la_widths_init(&e);
    char buf[1024];
    la_widths_json(&e, 150000000u, true, true, false, buf, sizeof buf);
    CHECK(strstr(buf, "\"periods\":0") && strstr(buf, "\"timed_out\":true") && strstr(buf, "\"overrun\":true"));
}

static void test_base64(void)
{
    char out[16];
    CHECK(la_base64((const uint8_t *)"", 0, out) == 0 && out[0] == 0);
    la_base64((const uint8_t *)"f", 1, out); CHECK(!strcmp(out, "Zg=="));
    la_base64((const uint8_t *)"fo", 2, out); CHECK(!strcmp(out, "Zm8="));
    la_base64((const uint8_t *)"foo", 3, out); CHECK(!strcmp(out, "Zm9v"));
    la_base64((const uint8_t *)"foobar", 6, out); CHECK(!strcmp(out, "Zm9vYmFy"));
    uint8_t bin[3] = {0xFF, 0x00, 0x80};
    la_base64(bin, 3, out); CHECK(!strcmp(out, "/wCA"));
}

static void test_pwm(void)
{
    uint32_t div, wrap;
    double f = la_pwm_config(150000000u, 100, &div, &wrap);
    CHECK(wrap <= 65535 && div >= 1 && div <= 255);
    CHECK(fabs(f - 100.0) / 100.0 < 1e-4);
    f = la_pwm_config(150000000u, 25000, &div, &wrap);
    CHECK(div == 1 && fabs(f - 25000.0) / 25000.0 < 1e-3);
    f = la_pwm_config(150000000u, 1, &div, &wrap); /* below what 16 bits and /255 can reach: lowest possible */
    CHECK(div == 255 && wrap == 65535 && f > 1.0);
}

static void test_counts(void)
{
    la_cmd_t c;
    CHECK(!la_parse("counts 0 1000000 12000", &c) && c.kind == LA_COUNTS && c.count == 1000000);
    CHECK(la_parse("counts 8 1 10", &c) != NULL);
    CHECK(la_parse("counts 0 0 10", &c) != NULL);
    CHECK(la_parse("counts 0 6000001 10", &c) != NULL);
    la_counts_t s;
    la_counts_init(&s);
    for (unsigned i = 0; i < 10000; i++) CHECK(la_counts_add(&s, 20));
    for (unsigned i = 0; i < 100; i++) CHECK(la_counts_add(&s, 32));
    CHECK(s.gates == 10100 && s.change_at == 10000);
    CHECK(s.min_count == 20 && s.max_count == 32);
    CHECK(s.ref_sum == 20 * LA_REF_PERIODS);
    CHECK(s.after_n > 0 && s.after_sum == 32 * s.after_n);
    CHECK(!la_counts_add(&s, LA_COUNT_MAX + 1));
    CHECK(s.after_min == 32 && s.after_max == 32);
    char out[1024];
    CHECK(la_counts_json(&s, 150000000, false, false, out, sizeof out) < (int)sizeof out);
    CHECK(strstr(out, "\"gate_cycles\":1500") != NULL);
    CHECK(strstr(out, "\"change_at\":10000") != NULL);
    la_counts_init(&s);
    for (unsigned i = 0; i < 100; i++) CHECK(la_counts_add(&s, 0));
    CHECK(s.change_at == -1 && s.zero_gates == 100);
    la_counts_init(&s);
    for (unsigned i = 0; i < 10000; i++) la_counts_add(&s, 20);
    la_counts_add(&s, 16); /* transient before the real startup switch */
    for (unsigned i = 0; i < 59; i++) la_counts_add(&s, 20);
    for (unsigned i = 0; i < 100; i++) la_counts_add(&s, 32);
    CHECK(s.change_at == 10060);
    CHECK(s.after_min == 32 && s.after_max == 32);
}

int main(void)
{
    test_parse();
    test_widths_wakeup();
    test_widths_glitch_and_steady();
    test_base64();
    test_pwm();
    test_counts();
    printf(failures ? "%d FAILED\n" : "all la_core tests passed\n", failures);
    return failures != 0;
}
