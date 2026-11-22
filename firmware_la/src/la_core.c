#include "la_core.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static bool word(const char **p, char *out, size_t n)
{
    while (**p == ' ' || **p == '\t') (*p)++;
    size_t i = 0;
    while (**p && **p != ' ' && **p != '\t' && **p != '\r' && **p != '\n') {
        if (i + 1 < n) out[i++] = **p;
        (*p)++;
    }
    out[i] = 0;
    return i > 0;
}

static bool number(const char **p, uint32_t lo, uint32_t hi, uint32_t *v)
{
    char w[16];
    if (!word(p, w, sizeof w)) return false;
    char *end;
    unsigned long x = strtoul(w, &end, 10);
    if (*end || x < lo || x > hi) return false;
    *v = (uint32_t)x;
    return true;
}

const char *la_parse(const char *line, la_cmd_t *c)
{
    char w[16];
    const char *p = line;
    memset(c, 0, sizeof *c);
    if (!word(&p, w, sizeof w)) return "empty command";
    if (!strcmp(w, "id")) { c->kind = LA_ID; return NULL; }
    if (!strcmp(w, "levels")) { c->kind = LA_LEVELS; return NULL; }
    if (!strcmp(w, "widths")) {
        c->kind = LA_WIDTHS;
        if (!number(&p, 0, LA_CHANNELS - 1, &c->ch)) return "widths <channel 0-7> <periods 1-50000000> <timeout ms 1-600000> [skip periods]";
        if (!number(&p, 1, 50000000u, &c->count)) return "widths: periods must be 1 to 50000000";
        if (!number(&p, 1, 600000u, &c->timeout_ms)) return "widths: timeout must be 1 to 600000 ms";
        const char *rest = p;
        char extra[16];
        if (word(&rest, extra, sizeof extra) && !number(&p, 0, 50000000u, &c->skip)) return "widths: skip must be 0 to 50000000";
        return NULL;
    }
    if (!strcmp(w, "burst")) {
        c->kind = LA_BURST;
        if (!number(&p, 4, LA_BURST_MAX, &c->count) || c->count % 4) return "burst <samples 4-262144, a multiple of 4> <divider 1-65535> <trigger channel 0-7 or -> <r|f> <timeout ms>";
        if (!number(&p, 1, 65535, &c->div)) return "burst: divider must be 1 to 65535";
        if (!word(&p, w, sizeof w)) return "burst: trigger channel missing (0-7, or - for none)";
        if (!strcmp(w, "-")) c->ch = LA_NO_TRIGGER;
        else {
            const char *q = w;
            if (!number(&q, 0, LA_CHANNELS - 1, &c->ch)) return "burst: trigger channel must be 0-7 or -";
        }
        if (!word(&p, w, sizeof w) || (strcmp(w, "r") && strcmp(w, "f"))) return "burst: edge must be r (rising) or f (falling)";
        c->rising = w[0] == 'r';
        if (!number(&p, 1, 600000u, &c->timeout_ms)) return "burst: timeout must be 1 to 600000 ms";
        return NULL;
    }
    if (!strcmp(w, "gen")) {
        if (!word(&p, w, sizeof w)) return "gen <pin 8-22> <hz 1-1000000> <duty 1-99> | gen off";
        if (!strcmp(w, "off")) { c->kind = LA_GEN_OFF; return NULL; }
        c->kind = LA_GEN;
        const char *q = w;
        if (!number(&q, 8, 22, &c->ch)) return "gen: pin must be 8 to 22 (GP0-GP7 are the inputs)";
        if (!number(&p, 1, 1000000u, &c->hz)) return "gen: frequency must be 1 to 1000000 Hz";
        if (!number(&p, 1, 99, &c->duty)) return "gen: duty must be 1 to 99 %";
        return NULL;
    }
    return "unknown command (id, levels, widths, burst, gen)";
}

void la_widths_init(la_widths_t *w)
{
    memset(w, 0, sizeof *w);
    w->min_high = w->min_low = w->min_high_near_change = w->min_low_near_change = UINT32_MAX;
    w->change_at = -1;
}

void la_widths_add(la_widths_t *w, uint32_t high_count, uint32_t low_count)
{
    uint32_t hi = 2u * high_count + LA_PHASE_OVERHEAD, lo = 2u * low_count + LA_PHASE_OVERHEAD;
    uint32_t period = hi + lo;
    if (hi < w->min_high) w->min_high = hi;
    if (hi > w->max_high) w->max_high = hi;
    if (lo < w->min_low) w->min_low = lo;
    if (lo > w->max_low) w->max_low = lo;
    w->sum_high += hi;
    w->sum_low += lo;

    if (w->periods < LA_REF_PERIODS) {
        w->ref_sum += period;
    } else if (w->change_at < 0) {
        uint64_t ref = w->ref_sum, p = (uint64_t)period * LA_REF_PERIODS; /* compare without dividing */
        uint64_t diff = p > ref ? p - ref : ref - p;
        if (diff * 8u > ref) {
            w->change_at = (int32_t)w->periods;
            w->cycles_to_change = w->total_cycles;
        }
    }
    if (w->change_at >= 0) {
        uint32_t since = w->periods - (uint32_t)w->change_at;
        if (since < 16u) {
            if (hi < w->min_high_near_change) w->min_high_near_change = hi;
            if (lo < w->min_low_near_change) w->min_low_near_change = lo;
        }
        if (since >= LA_AFTER_SKIP && w->after_n < LA_AFTER_PERIODS) {
            w->after_sum += period;
            w->after_n++;
        }
    }
    w->total_cycles += period;
    w->periods++;
}

int la_widths_json(const la_widths_t *w, uint32_t clk_hz, bool overrun, bool timed_out, bool ended, char *buf, size_t n)
{
    unsigned ref_n = w->periods < LA_REF_PERIODS ? w->periods : LA_REF_PERIODS;
    return snprintf(buf, n,
                    "{\"mode\":\"widths\",\"clk_hz\":%lu,\"periods\":%lu,\"total_cycles\":%llu,"
                    "\"high\":{\"min\":%lu,\"max\":%lu,\"sum\":%llu},\"low\":{\"min\":%lu,\"max\":%lu,\"sum\":%llu},"
                    "\"ref_periods\":%u,\"ref_sum\":%llu,\"change_at\":%ld,\"cycles_to_change\":%llu,"
                    "\"after_periods\":%lu,\"after_sum\":%llu,\"min_high_near_change\":%lu,\"min_low_near_change\":%lu,"
                    "\"overrun\":%s,\"timed_out\":%s,\"ended\":%s}",
                    (unsigned long)clk_hz, (unsigned long)w->periods, (unsigned long long)w->total_cycles,
                    (unsigned long)(w->periods ? w->min_high : 0), (unsigned long)w->max_high, (unsigned long long)w->sum_high,
                    (unsigned long)(w->periods ? w->min_low : 0), (unsigned long)w->max_low, (unsigned long long)w->sum_low,
                    ref_n, (unsigned long long)w->ref_sum, (long)w->change_at, (unsigned long long)w->cycles_to_change,
                    (unsigned long)w->after_n, (unsigned long long)w->after_sum,
                    (unsigned long)(w->change_at >= 0 ? w->min_high_near_change : 0),
                    (unsigned long)(w->change_at >= 0 ? w->min_low_near_change : 0),
                    overrun ? "true" : "false", timed_out ? "true" : "false", ended ? "true" : "false");
}

size_t la_base64(const uint8_t *in, size_t n, char *out)
{
    static const char t[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    size_t o = 0;
    for (size_t i = 0; i < n; i += 3) {
        uint32_t v = (uint32_t)in[i] << 16;
        if (i + 1 < n) v |= (uint32_t)in[i + 1] << 8;
        if (i + 2 < n) v |= in[i + 2];
        out[o++] = t[(v >> 18) & 63];
        out[o++] = t[(v >> 12) & 63];
        out[o++] = i + 1 < n ? t[(v >> 6) & 63] : '=';
        out[o++] = i + 2 < n ? t[v & 63] : '=';
    }
    out[o] = 0;
    return o;
}

double la_pwm_config(uint32_t clk_hz, uint32_t hz, uint32_t *div, uint32_t *wrap)
{
    double top = (double)clk_hz / hz; /* counts per period at divider 1 */
    uint32_t d = (uint32_t)(top / 65536.0) + 1u;
    if (d > 255u) d = 255u;
    double w = top / d - 1.0;
    if (w > 65535.0) w = 65535.0;
    if (w < 1.0) w = 1.0;
    *div = d;
    *wrap = (uint32_t)(w + 0.5);
    return (double)clk_hz / ((double)d * (*wrap + 1u));
}
