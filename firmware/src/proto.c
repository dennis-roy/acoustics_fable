#include "proto.h"

#include <string.h>

static const uint8_t MAGIC[4] = {0x45, 0x4F, 0x4C, 0x31}; /* "EOL1" */

static uint32_t crc_table[256];
static int crc_ready;

static void crc_init(void)
{
    for (uint32_t i = 0; i < 256; i++) {
        uint32_t c = i;
        for (int k = 0; k < 8; k++) c = (c & 1u) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1);
        crc_table[i] = c;
    }
    crc_ready = 1;
}

uint32_t eol_crc32(uint32_t crc, const uint8_t *p, size_t n)
{
    if (!crc_ready) crc_init();
    uint32_t c = crc ^ 0xFFFFFFFFu;
    while (n--) c = crc_table[(c ^ *p++) & 0xFFu] ^ (c >> 8);
    return c ^ 0xFFFFFFFFu;
}

void eol_put_u16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
}

void eol_put_u32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16);
    p[3] = (uint8_t)(v >> 24);
}

uint16_t eol_get_u16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }

uint32_t eol_get_u32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

void eol_put_header(uint8_t hdr[EOL_HDR_LEN], uint8_t type, uint8_t flags, uint16_t len,
                    uint32_t seq, uint64_t t_us)
{
    memcpy(hdr, MAGIC, 4);
    hdr[4] = type;
    hdr[5] = flags;
    eol_put_u16(hdr + 6, len);
    eol_put_u32(hdr + 8, seq);
    eol_put_u32(hdr + 12, (uint32_t)t_us);
    eol_put_u32(hdr + 16, (uint32_t)(t_us >> 32));
    eol_put_u32(hdr + 20, eol_crc32(0, hdr, 20));
}

static void drop_one(eol_parser_t *ps)
{
    memmove(ps->buf, ps->buf + 1, --ps->n);
    ps->junk++;
}

/* Returns 1 if the buffer changed and should be looked at again. */
static int parser_step(eol_parser_t *ps, eol_frame_cb cb, void *ctx)
{
    if (ps->n == 0) return 0;
    size_t m = ps->n < 4 ? ps->n : 4;
    if (memcmp(ps->buf, MAGIC, m) != 0) {
        drop_one(ps);
        return 1;
    }
    if (ps->n < EOL_HDR_LEN) return 0;
    uint16_t len = eol_get_u16(ps->buf + 6);
    if (eol_get_u32(ps->buf + 20) != eol_crc32(0, ps->buf, 20) || len > EOL_RX_MAX_PAYLOAD) {
        drop_one(ps);
        return 1;
    }
    size_t total = EOL_HDR_LEN + len + EOL_CRC_LEN;
    if (ps->n < total) return 0;
    if (eol_get_u32(ps->buf + total - EOL_CRC_LEN) != eol_crc32(0, ps->buf, total - EOL_CRC_LEN)) {
        drop_one(ps);
        return 1;
    }
    cb(ctx, ps->buf[4], eol_get_u32(ps->buf + 8), ps->buf + EOL_HDR_LEN, len);
    ps->n -= total;
    memmove(ps->buf, ps->buf + total, ps->n);
    return 1;
}

void eol_parser_feed(eol_parser_t *ps, const uint8_t *p, size_t n, eol_frame_cb cb, void *ctx)
{
    while (n--) {
        /* The buffer can never be full here: a full buffer always holds a complete frame
           or a byte that parser_step discards. */
        ps->buf[ps->n++] = *p++;
        while (parser_step(ps, cb, ctx)) {
        }
    }
}
