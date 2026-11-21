/* USB descriptors: composite device, two CDC serial ports.
   Interface 0/1 = DATA port, interface 2/3 = HB port.
   NOT TESTED ON HARDWARE. */
#include <string.h>

#include "pico/unique_id.h"
#include "tusb.h"

/* Development identity only. 0x2E8A is Raspberry Pi's vendor ID and 0x000A the Pico SDK's
   generic serial product ID. A shipped product needs its own product ID. */
#define USB_VID 0x2E8A
#define USB_PID 0x000A

static const tusb_desc_device_t desc_device = {
    .bLength = sizeof(tusb_desc_device_t),
    .bDescriptorType = TUSB_DESC_DEVICE,
    .bcdUSB = 0x0200,
    .bDeviceClass = TUSB_CLASS_MISC,
    .bDeviceSubClass = MISC_SUBCLASS_COMMON,
    .bDeviceProtocol = MISC_PROTOCOL_IAD,
    .bMaxPacketSize0 = CFG_TUD_ENDPOINT0_SIZE,
    .idVendor = USB_VID,
    .idProduct = USB_PID,
    .bcdDevice = 0x0020,
    .iManufacturer = 1,
    .iProduct = 2,
    .iSerialNumber = 3,
    .bNumConfigurations = 1,
};

uint8_t const *tud_descriptor_device_cb(void) { return (uint8_t const *)&desc_device; }

enum { ITF_DATA_COMM = 0, ITF_DATA_DATA, ITF_HB_COMM, ITF_HB_DATA, ITF_TOTAL };

#define EP_DATA_NOTIF 0x81
#define EP_DATA_OUT 0x02
#define EP_DATA_IN 0x82
#define EP_HB_NOTIF 0x83
#define EP_HB_OUT 0x04
#define EP_HB_IN 0x84

#define CONFIG_TOTAL_LEN (TUD_CONFIG_DESC_LEN + 2 * TUD_CDC_DESC_LEN)

static const uint8_t desc_configuration[] = {
    TUD_CONFIG_DESCRIPTOR(1, ITF_TOTAL, 0, CONFIG_TOTAL_LEN, 0x00, 250),
    TUD_CDC_DESCRIPTOR(ITF_DATA_COMM, 4, EP_DATA_NOTIF, 8, EP_DATA_OUT, EP_DATA_IN, 64),
    TUD_CDC_DESCRIPTOR(ITF_HB_COMM, 5, EP_HB_NOTIF, 8, EP_HB_OUT, EP_HB_IN, 64),
};

uint8_t const *tud_descriptor_configuration_cb(uint8_t index)
{
    (void)index;
    return desc_configuration;
}

/* The CM5 finds the ports by these strings: /dev/serial/by-id/usb-Zynith_EOL_Pico_front_end_* */
static const char *const strings[] = {
    NULL,                 /* 0: language, handled below */
    "Zynith",             /* 1: manufacturer */
    "EOL Pico front end", /* 2: product */
    NULL,                 /* 3: serial, from the board's unique ID */
    "EOL data",           /* 4: DATA port */
    "EOL heartbeat",      /* 5: HB port */
};

uint16_t const *tud_descriptor_string_cb(uint8_t index, uint16_t langid)
{
    (void)langid;
    static uint16_t desc[1 + 32];
    char serial[2 * PICO_UNIQUE_BOARD_ID_SIZE_BYTES + 1];
    const char *s;
    size_t n;

    if (index == 0) {
        desc[1] = 0x0409; /* English */
        n = 1;
    } else {
        if (index >= sizeof strings / sizeof strings[0]) return NULL;
        if (index == 3) {
            pico_get_unique_board_id_string(serial, sizeof serial);
            s = serial;
        } else {
            s = strings[index];
        }
        n = strlen(s);
        if (n > 32) n = 32;
        for (size_t i = 0; i < n; i++) desc[1 + i] = (uint16_t)s[i];
    }
    desc[0] = (uint16_t)((TUSB_DESC_STRING << 8) | (2 * n + 2));
    return desc;
}
