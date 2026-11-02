/*
 * The touch screen of the POCO X5 Pro (redwood): either a Goodix GT9916 ("Berlin-D") or a
 * FocalTech FT3680 on the SPI bus of the SoC's serial engine 13, as a source of touches for
 * /dev/redwood-touch (sys/redwood_input.h). Only with boot-arg touchscreen=1: every address
 * below belongs to this phone.
 *
 * There is no clock, pin, regulator or SPI framework in this kernel, so the driver does all of
 * it itself, in the order the vendor's Linux drivers do:
 *   power    the chip's I/O supply is LDO2 of the PM8350C, switched through RPMh (one request
 *            written into the "always on" processor's command slots); the analog supply is
 *            behind a GPIO
 *   clocks   the serial engine's three clocks, by vote in the global clock controller
 *   pins     four GPIOs to the serial engine, plus reset, interrupt and the analog supply switch
 *   SPI      the serial engine ("GENI") in FIFO mode, polled; or, if the engine cannot be used,
 *            the same four pins driven by hand
 *   panel    GPIO96=1 and GPIO68=0 identify the FocalTech variant; everything else is Goodix,
 *            the same discriminator the vendor sm8350-kernel-OKX uses
 *   chip     Goodix: find where it keeps the touch report, then read it when the IRQ line signals;
 *            FocalTech: read the report at register 0x01 with FTS 5.x SPI framing (cmd 0xC1 for
 *            read, 0xC0 for write, two-byte CRC-16/Kermit)
 * No interrupt is used: a thread looks at the interrupt line 125 times a second (boot-arg
 * touch_poll_us=N: every N microseconds instead of 8000). With touch_lat=1 a line for every report
 * read ("tl: c before US seen US read US": the look before, the look that found the line low, the
 * report read), for measuring how long a touch takes to show (tools/latency.py).
 *
 * Register layouts: Linux drivers/spi/spi-geni-qcom.c, drivers/soc/qcom/{qcom-geni-se,rpmh-rsc,
 * cmd-db}.c, drivers/clk/qcom/gcc-sc7280.c, drivers/pinctrl/qcom/pinctrl-sc7280.c,
 * drivers/input/touchscreen/{goodix_berlin_*,focaltech_touch/focaltech_core}.c, and the phone's
 * own device tree.
 */
#include <kern/clock.h>
#include <kern/thread.h>
#include <kern/sched_prim.h>
#include <libkern/libkern.h>
#include <mach/mach_types.h>
#include <pexpert/pexpert.h>
#include <sys/redwood_input.h>
#include <sys/systm.h>

extern void redwood_tl(const char *format, ...) __printflike(1, 2);

extern vm_offset_t ml_io_map(vm_offset_t phys_addr, vm_size_t size);
extern vm_offset_t ml_io_map_wcomb(vm_offset_t phys_addr, vm_size_t size);
extern kern_return_t thread_terminate(thread_t thread);

static vm_offset_t tlmm, gcc, se, rsc, cmd_db;

static inline uint32_t
rd(vm_offset_t address)
{
	return *(volatile uint32_t *)address;
}

static inline void
wr(vm_offset_t address, uint32_t value)
{
	*(volatile uint32_t *)address = value;
}

static void
sleep_us(uint32_t us)
{
	delay_for_interval(us, NSEC_PER_USEC);
}

static void
sleep_ms(uint32_t ms)
{
	delay_for_interval(ms, NSEC_PER_MSEC);
}

/* ---- pins (TLMM) ------------------------------------------------------------------------------- */

#define TLMM_PHYS               0x0f100000UL
#define TLMM_SIZE               0x100000UL
#define GPIO_CFG(n)             (tlmm + 0x1000UL * (n))         /* [1:0] pull, [5:2] function, [8:6] drive, [9] output */
#define GPIO_IO(n)              (tlmm + 0x1000UL * (n) + 4)     /* bit 0 in, bit 1 out */
#define PULL_NONE               0
#define PULL_UP                 3

#define PIN_MISO                52
#define PIN_MOSI                53
#define PIN_CLK                 54
#define PIN_CS                  55
#define PIN_INT                 81      /* low = the chip has a report */
#define PIN_RESET               105     /* low = in reset */
#define PIN_AVDD                107     /* high = analog supply on */
#define PIN_FOCALTECH_ID        68      /* FocalTech if this reads 0 ... */
#define PIN_GOODIX_ID           96      /* ... and this reads 1 (sm8350-kernel-OKX convention) */
#define PIN_FUNCTION_SPI        1       /* "qup15" */

static void
gpio_configure(unsigned pin, unsigned function, unsigned pull, unsigned ma, bool output)
{
	wr(GPIO_CFG(pin), pull | function << 2 | (ma / 2 - 1) << 6 | (output ? 1u << 9 : 0));
}

static void
gpio_set(unsigned pin, bool high)
{
	wr(GPIO_IO(pin), high ? 2 : 0);
}

static bool
gpio_get(unsigned pin)
{
	return rd(GPIO_IO(pin)) & 1;
}

static void
gpio_output(unsigned pin, bool high, unsigned pull, unsigned ma)
{
	gpio_set(pin, high);            /* the level first, so that the pin never drives the wrong one */
	gpio_configure(pin, 0, pull, ma, true);
}

/* ---- the I/O supply (RPMh) ----------------------------------------------------------------------- */

#define CMD_DB_PHYS             0x80860000UL
#define CMD_DB_SIZE             0x20000UL
#define RSC_PHYS                0x18220000UL    /* the application processor's "DRV 2" */
#define RSC_SIZE                0x10000UL
#define TCS(reg)                (rsc + 0xd00 + (reg))           /* command slot set 0, an "active" one */
#define TCS_IRQ_ENABLE          0x00
#define TCS_IRQ_STATUS          0x04
#define TCS_IRQ_CLEAR           0x08
#define TCS_WAIT_FOR_COMPLETION 0x10
#define TCS_CONTROL             0x14
#define TCS_CONTROL_ENABLE      (1u << 16)
#define TCS_CONTROL_TRIGGER     (1u << 24)
#define TCS_STATUS              0x18            /* 0 while busy */
#define TCS_CMD_ENABLE          0x1c
#define TCS_CMD_MSGID(n)        (0x30 + 20 * (n))
#define TCS_CMD_ADDR(n)         (0x34 + 20 * (n))
#define TCS_CMD_DATA(n)         (0x38 + 20 * (n))
#define MSGID_WRITE_4_BYTES     0x10008
#define MSGID_RESPONSE          0x100

#define VRM_VOLTAGE             0               /* a regulator's registers, from its address: millivolts */
#define VRM_ENABLE              4
#define VRM_MODE                8
#define VRM_MODE_LDO_HPM        7

struct cmd_db_slave {
	uint16_t id, header_offset, data_offset, count, version, reserved[3];
};
struct cmd_db_header {
	uint32_t version;
	uint8_t magic[4];
	struct cmd_db_slave slave[8];
	uint32_t checksum, reserved;
	uint8_t data[];
};
struct cmd_db_entry {
	char id[8];
	uint32_t priority[2];
	uint32_t address;
	uint16_t length, offset;
};

/* The RPMh address of a resource by its name in the command database the boot loader left; 0 = none. */
static uint32_t
rpmh_address(const char *name)
{
	const struct cmd_db_header *db = (const struct cmd_db_header *)cmd_db;
	char id[8] = { 0 };

	if (db->magic[0] != 0xdb || db->magic[1] != 0x30 || db->magic[2] != 0x03 || db->magic[3] != 0x0c) {
		printf("goodix: no command database (magic %02x %02x %02x %02x)\n", db->magic[0], db->magic[1], db->magic[2],
		    db->magic[3]);
		return 0;
	}
	strlcpy(id, name, sizeof(id));
	for (unsigned s = 0; s < 8; s++) {
		const struct cmd_db_slave *slave = &db->slave[s];
		const struct cmd_db_entry *entry = (const struct cmd_db_entry *)(db->data + slave->header_offset);

		if (slave->id == 0 || slave->header_offset >= CMD_DB_SIZE - 0x1000 || slave->count > 512) {
			continue;
		}
		for (unsigned i = 0; i < slave->count; i++) {
			if (memcmp(entry[i].id, id, sizeof(id)) == 0) {
				return entry[i].address;
			}
		}
	}
	return 0;
}

static bool
rpmh_wait(uint32_t reg, uint32_t mask, uint32_t value, const char *what)
{
	for (int i = 0; i < 2000; i++) {
		if ((rd(TCS(reg)) & mask) == value) {
			return true;
		}
		sleep_us(100);
	}
	printf("goodix: RPMh: timeout waiting for %s (register %#x = %#x)\n", what, reg, rd(TCS(reg)));
	return false;
}

/* Send up to three register writes to RPMh and wait until they are done. */
static bool
rpmh_write(const uint32_t *address, const uint32_t *data, unsigned count)
{
	uint32_t irq_enable = rd(TCS(TCS_IRQ_ENABLE));
	bool done;

	/* the slot set must be idle, and switched off the way the hardware wants it: trigger, then enable */
	for (int i = 0; rd(TCS(TCS_STATUS)) == 0; i++) {
		if (i > 2000) {
			printf("goodix: RPMh: the command slots stay busy\n");
			return false;
		}
		sleep_us(100);
	}
	wr(TCS(TCS_CONTROL), rd(TCS(TCS_CONTROL)) & ~TCS_CONTROL_TRIGGER);
	wr(TCS(TCS_CONTROL), rd(TCS(TCS_CONTROL)) & ~TCS_CONTROL_ENABLE);
	wr(TCS(TCS_IRQ_CLEAR), 1);
	for (unsigned i = 0; i < count; i++) {
		wr(TCS(TCS_CMD_MSGID(i)), MSGID_WRITE_4_BYTES | MSGID_RESPONSE);
		wr(TCS(TCS_CMD_ADDR(i)), address[i]);
		wr(TCS(TCS_CMD_DATA(i)), data[i]);
	}
	wr(TCS(TCS_WAIT_FOR_COMPLETION), (1u << count) - 1);
	wr(TCS(TCS_CMD_ENABLE), (1u << count) - 1);
	wr(TCS(TCS_IRQ_ENABLE), irq_enable | 1);        /* the status bit follows the enable; the interrupt itself stays masked */
	wr(TCS(TCS_CONTROL), TCS_CONTROL_ENABLE);
	wr(TCS(TCS_CONTROL), TCS_CONTROL_ENABLE | TCS_CONTROL_TRIGGER);

	done = rpmh_wait(TCS_IRQ_STATUS, 1, 1, "the request");
	wr(TCS(TCS_CONTROL), rd(TCS(TCS_CONTROL)) & ~TCS_CONTROL_TRIGGER);
	wr(TCS(TCS_CONTROL), rd(TCS(TCS_CONTROL)) & ~TCS_CONTROL_ENABLE);
	wr(TCS(TCS_CMD_ENABLE), 0);
	wr(TCS(TCS_WAIT_FOR_COMPLETION), 0);
	wr(TCS(TCS_IRQ_CLEAR), 1);
	wr(TCS(TCS_IRQ_ENABLE), irq_enable);
	return done;
}

/* The chip's I/O supply: 1.8 V from LDO2 of the PM8350C ("ldoc2"). Nothing else is connected to it. */
static bool
io_supply_on(void)
{
	uint32_t base = rpmh_address("ldoc2");
	uint32_t address[3] = { base + VRM_VOLTAGE, base + VRM_ENABLE, base + VRM_MODE };
	uint32_t data[3] = { 1800, 1, VRM_MODE_LDO_HPM };

	/* addresses of regulators ("VRM") have 4 in bits 19:16; anything else is not what we are looking for */
	if (base == 0 || (base >> 16 & 0xf) != 4) {
		printf("goodix: RPMh: no regulator \"ldoc2\" (address %#x)\n", base);
		return false;
	}
	printf("goodix: RPMh: ldoc2 is at %#x; slot status %#x, control %#x, irq enable %#x status %#x\n", base,
	    rd(TCS(TCS_STATUS)), rd(TCS(TCS_CONTROL)), rd(TCS(TCS_IRQ_ENABLE)), rd(TCS(TCS_IRQ_STATUS)));
	if (!rpmh_write(address, data, 3)) {
		return false;
	}
	printf("goodix: I/O supply on (1.8 V)\n");
	return true;
}

/* ---- clocks (GCC) -------------------------------------------------------------------------------- */

#define GCC_PHYS                0x00100000UL
#define GCC_SIZE                0x60000UL
#define GCC_VOTE                (gcc + 0x52008)         /* the application processor's votes */
#define VOTE_QUP1_CORE_2X       (1u << 18)
#define VOTE_QUP1_CORE          (1u << 19)
#define VOTE_QUP1_M_AHB         (1u << 20)
#define VOTE_QUP1_S_AHB         (1u << 21)
#define VOTE_QUP1_SE5           (1u << 27)             /* serial engine 13 is the 6th of the second group */
#define GCC_QUP1_M_AHB_CBCR     (gcc + 0x18004)         /* bit 31: the clock is off */
#define GCC_QUP1_S_AHB_CBCR     (gcc + 0x18008)
#define GCC_QUP1_SE5_CBCR       (gcc + 0x185fc)
#define GCC_QUP1_SE5_CMD        (gcc + 0x18600)         /* bit 0: take the new configuration */
#define GCC_QUP1_SE5_CFG        (gcc + 0x18604)         /* [4:0] 2*divider-1, [10:8] source, [13:12] mode */
#define GCC_QUP1_SE5_DFSR       (gcc + 0x18614)         /* bit 0: the engine chooses among preset rates itself */
#define GCC_QUP1_SE5_PERF_CFG(l) (gcc + 0x1861c + 4 * (l))
#define GCC_QUP1_SE5_PERF_M(l)  (gcc + 0x1865c + 4 * (l))
#define GCC_QUP1_SE5_PERF_N(l)  (gcc + 0x1869c + 4 * (l))
#define CLOCK_OFF               (1u << 31)
#define XO_HZ                   19200000

static uint32_t se_source_hz, se_clock_select;

static uint32_t
rcg_rate(uint32_t cfg, uint32_t m, uint32_t not_n_minus_m)
{
	static const uint32_t parent[8] = { XO_HZ, 600000000, 0, 0, 0, 0, 300000000, 0 };
	uint64_t hz = parent[cfg >> 8 & 7];
	uint32_t divider = cfg & 0x1f, mode = cfg >> 12 & 3;

	if (divider) {
		hz = hz * 2 / (divider + 1);
	}
	if (mode) {
		uint32_t n = (~not_n_minus_m & 0xffff) + (m & 0xffff);

		if (n) {
			hz = hz * (m & 0xffff) / n;
		}
	}
	return (uint32_t)hz;
}

static bool
clocks_on(void)
{
	wr(GCC_VOTE, rd(GCC_VOTE) | VOTE_QUP1_CORE_2X | VOTE_QUP1_CORE | VOTE_QUP1_M_AHB | VOTE_QUP1_S_AHB);
	if (rd(GCC_QUP1_SE5_DFSR) & 1) {
		/* preset rates: the lowest that is at least 4 MHz */
		for (uint32_t level = 0; level < 8; level++) {
			uint32_t hz = rcg_rate(rd(GCC_QUP1_SE5_PERF_CFG(level)), rd(GCC_QUP1_SE5_PERF_M(level)),
			    rd(GCC_QUP1_SE5_PERF_N(level)));

			if (hz >= 4000000 && (se_source_hz == 0 || hz < se_source_hz)) {
				se_source_hz = hz;
				se_clock_select = level;
			}
		}
		if (se_source_hz == 0) {
			printf("goodix: no usable preset clock rate\n");
			return false;
		}
	} else {
		/* the crystal, undivided: nothing else has to be running */
		wr(GCC_QUP1_SE5_CFG, (rd(GCC_QUP1_SE5_CFG) & ~0x371fu) | 1);
		wr(GCC_QUP1_SE5_CMD, rd(GCC_QUP1_SE5_CMD) | 1);
		for (int i = 0; i < 500 && (rd(GCC_QUP1_SE5_CMD) & 1); i++) {
			sleep_us(10);
		}
		if (rd(GCC_QUP1_SE5_CMD) & 1) {
			printf("goodix: the serial engine's clock does not take its configuration (%#x)\n", rd(GCC_QUP1_SE5_CMD));
			return false;
		}
		se_source_hz = XO_HZ;
		se_clock_select = 0;
	}
	wr(GCC_VOTE, rd(GCC_VOTE) | VOTE_QUP1_SE5);
	for (int i = 0; i < 1000; i++) {
		if (!((rd(GCC_QUP1_M_AHB_CBCR) | rd(GCC_QUP1_S_AHB_CBCR) | rd(GCC_QUP1_SE5_CBCR)) & CLOCK_OFF)) {
			return true;
		}
		sleep_us(10);
	}
	printf("goodix: clocks stay off: %#x %#x %#x\n", rd(GCC_QUP1_M_AHB_CBCR), rd(GCC_QUP1_S_AHB_CBCR),
	    rd(GCC_QUP1_SE5_CBCR));
	return false;
}

/* ---- SPI through the serial engine (GENI) ---------------------------------------------------------- */

#define SE_PHYS                 0x00a94000UL
#define SE_SIZE                 0x4000UL
#define GENI_FORCE_DEFAULT      (se + 0x020)
#define GENI_OUTPUT_CTRL        (se + 0x024)
#define GENI_CGC_CTRL           (se + 0x028)
#define GENI_STATUS             (se + 0x040)    /* bit 0: a command is running */
#define GENI_SER_M_CLK_CFG      (se + 0x048)    /* bit 0 enable, [15:4] divider */
#define GENI_IF_DISABLE_RO      (se + 0x064)    /* bit 0: no FIFO interface */
#define GENI_FW_REVISION_RO     (se + 0x068)    /* [15:8] protocol: 1 = SPI */
#define GENI_CLK_SEL            (se + 0x07c)
#define SPI_CPHA                (se + 0x224)
#define SPI_LOOPBACK            (se + 0x22c)
#define SPI_CPOL                (se + 0x230)
#define SPI_DEMUX_OUTPUT_INV    (se + 0x24c)
#define SPI_DEMUX_SEL           (se + 0x250)
#define GENI_BYTE_GRAN          (se + 0x254)
#define GENI_DMA_MODE_EN        (se + 0x258)
#define SPI_TRANS_CFG           (se + 0x25c)
#define GENI_TX_PACKING_CFG0    (se + 0x260)
#define GENI_TX_PACKING_CFG1    (se + 0x264)
#define SPI_WORD_LEN            (se + 0x268)
#define SPI_TX_TRANS_LEN        (se + 0x26c)
#define SPI_RX_TRANS_LEN        (se + 0x270)
#define SPI_DELAY_COUNTERS      (se + 0x278)
#define GENI_RX_PACKING_CFG0    (se + 0x284)
#define GENI_RX_PACKING_CFG1    (se + 0x288)
#define GENI_M_CMD0             (se + 0x600)    /* [31:27] operation */
#define GENI_M_CMD_CTRL         (se + 0x604)    /* bit 2 cancel, bit 1 abort */
#define GENI_M_IRQ_STATUS       (se + 0x610)
#define GENI_M_IRQ_EN           (se + 0x614)
#define GENI_M_IRQ_CLEAR        (se + 0x618)
#define GENI_S_IRQ_CLEAR        (se + 0x648)
#define GENI_TX_FIFO            (se + 0x700)
#define GENI_RX_FIFO            (se + 0x780)
#define GENI_TX_FIFO_STATUS     (se + 0x800)    /* [27:0] words */
#define GENI_RX_FIFO_STATUS     (se + 0x804)    /* bit 31 last, [30:28] bytes of the last word, [24:0] words */
#define GENI_TX_WATERMARK       (se + 0x80c)
#define GENI_RX_WATERMARK       (se + 0x810)
#define GENI_RX_RFR_WATERMARK   (se + 0x814)
#define SE_DMA_TX_IRQ_CLR       (se + 0xc44)
#define SE_DMA_RX_IRQ_CLR       (se + 0xd44)
#define SE_GSI_EVENT_EN         (se + 0xe18)
#define SE_IRQ_EN               (se + 0xe1c)
#define SE_HW_PARAM_0           (se + 0xe24)    /* [21:16] FIFO depth in words */
#define SE_DMA_GENERAL_CFG      (se + 0xe30)

#define M_CMD_DONE              (1u << 0)
#define M_CMD_CANCEL            (1u << 4)
#define M_CMD_ABORT             (1u << 5)
#define M_ERRORS                ((1u << 1) | (1u << 2) | (1u << 3) | (1u << 24) | (1u << 25) | (1u << 28) | (1u << 29))
#define SPI_FULL_DUPLEX         (3u << 27)

static uint32_t fifo_depth;

/* 0 = ready; otherwise why the engine cannot be used */
static const char *
geni_spi_init(uint32_t hz)
{
	uint32_t divider;

	if ((rd(GENI_FW_REVISION_RO) >> 8 & 0xff) != 1) {
		return "the serial engine has no SPI firmware";
	}
	if (rd(GENI_IF_DISABLE_RO) & 1) {
		return "the serial engine's FIFO interface is disabled";
	}
	fifo_depth = rd(SE_HW_PARAM_0) >> 16 & 0x3f;
	if (fifo_depth < 4) {
		return "the serial engine reports no FIFO";
	}
	wr(SE_GSI_EVENT_EN, 0);
	wr(GENI_M_IRQ_CLEAR, 0xffffffff);
	wr(GENI_S_IRQ_CLEAR, 0xffffffff);
	wr(SE_DMA_TX_IRQ_CLR, 0xffffffff);
	wr(SE_DMA_RX_IRQ_CLR, 0xffffffff);
	wr(GENI_CGC_CTRL, rd(GENI_CGC_CTRL) | 0x7f);
	wr(SE_DMA_GENERAL_CFG, rd(SE_DMA_GENERAL_CFG) | 0xf);
	wr(GENI_OUTPUT_CTRL, 0x7f);
	wr(GENI_FORCE_DEFAULT, 1);
	wr(GENI_DMA_MODE_EN, rd(GENI_DMA_MODE_EN) & ~1u);
	wr(GENI_RX_WATERMARK, fifo_depth - 3);
	wr(GENI_RX_RFR_WATERMARK, fifo_depth - 2);
	wr(GENI_TX_WATERMARK, 0);
	/* the status bits we poll follow these enables; the interrupt itself stays masked at the GIC */
	wr(SE_IRQ_EN, rd(SE_IRQ_EN) | 0xf);
	wr(GENI_M_IRQ_EN, rd(GENI_M_IRQ_EN) | 0x7fc0007f);

	/* mode 0, chip select 0, 8 bits, most significant first, four bytes in a FIFO word */
	wr(SPI_LOOPBACK, 0);
	wr(SPI_DEMUX_SEL, 0);
	wr(SPI_CPHA, 0);
	wr(SPI_CPOL, 0);
	wr(SPI_DEMUX_OUTPUT_INV, 0);
	wr(SPI_TRANS_CFG, rd(SPI_TRANS_CFG) & ~2u);
	wr(SPI_DELAY_COUNTERS, 0);
	wr(GENI_TX_PACKING_CFG0, 0x0007f8fe);
	wr(GENI_TX_PACKING_CFG1, 0x000ffefe);
	wr(GENI_RX_PACKING_CFG0, 0x0007f8fe);
	wr(GENI_RX_PACKING_CFG1, 0x000ffefe);
	wr(GENI_BYTE_GRAN, 0);
	wr(SPI_WORD_LEN, 8 - 4);

	divider = (se_source_hz + hz - 1) / hz;
	wr(GENI_CLK_SEL, se_clock_select & 7);
	wr(GENI_SER_M_CLK_CFG, (divider ? divider : 1) << 4 | 1);
	return NULL;
}

static void
geni_drain(uint8_t *rx, uint32_t length, uint32_t *received)
{
	uint32_t status = rd(GENI_RX_FIFO_STATUS), words = status & 0x01ffffff, bytes = words * 4;

	if (status & 0x80000000u) {
		uint32_t valid = status >> 28 & 7;

		if (valid && valid < 4) {
			bytes -= 4 - valid;
		}
	}
	for (uint32_t i = 0; i < words; i++) {
		uint32_t word = rd(GENI_RX_FIFO);

		for (int j = 0; j < 4 && bytes; j++, bytes--) {
			if (*received < length) {
				if (rx) {
					rx[*received] = (uint8_t)(word >> (8 * j));
				}
				(*received)++;
			}
		}
	}
}

static void
geni_recover(void)
{
	wr(GENI_M_CMD_CTRL, 1u << 2);
	for (int i = 0; i < 2000 && !(rd(GENI_M_IRQ_STATUS) & M_CMD_CANCEL); i++) {
		sleep_us(100);
	}
	if (!(rd(GENI_M_IRQ_STATUS) & M_CMD_CANCEL)) {
		wr(GENI_M_CMD_CTRL, 1u << 1);
		for (int i = 0; i < 2000 && !(rd(GENI_M_IRQ_STATUS) & M_CMD_ABORT); i++) {
			sleep_us(100);
		}
	}
	for (int i = 0; i < 64 && (rd(GENI_RX_FIFO_STATUS) & 0x01ffffff); i++) {
		(void)rd(GENI_RX_FIFO);
	}
	wr(GENI_M_IRQ_CLEAR, 0xffffffff);
}

/* One transfer with the chip selected throughout: `length` bytes out (zeros without tx) and as many in. */
static bool
geni_transfer(const uint8_t *tx, uint8_t *rx, uint32_t length)
{
	static unsigned complaints;
	uint32_t sent = 0, received = 0, status = 0;
	uint64_t deadline;

	for (int i = 0; rd(GENI_STATUS) & 1; i++) {
		if (i > 2000) {
			geni_recover();
			break;
		}
		sleep_us(100);
	}
	wr(GENI_M_IRQ_CLEAR, 0xffffffff);
	wr(SPI_TX_TRANS_LEN, length);
	wr(SPI_RX_TRANS_LEN, length);
	wr(GENI_M_CMD0, SPI_FULL_DUPLEX);
	clock_interval_to_deadline(200, NSEC_PER_MSEC, &deadline);
	while (mach_absolute_time() < deadline) {
		while (sent < length && (rd(GENI_TX_FIFO_STATUS) & 0x0fffffff) + 1 < fifo_depth) {
			uint32_t word = 0;

			for (int j = 0; j < 4 && sent < length; j++, sent++) {
				word |= (uint32_t)(tx ? tx[sent] : 0) << (8 * j);
			}
			wr(GENI_TX_FIFO, word);
		}
		status = rd(GENI_M_IRQ_STATUS);
		geni_drain(rx, length, &received);
		if (status & M_ERRORS) {
			break;
		}
		if (status & M_CMD_DONE) {
			geni_drain(rx, length, &received);
			wr(GENI_M_IRQ_CLEAR, status);
			if (received == length) {
				return true;
			}
			break;
		}
	}
	if (complaints++ < 8) {
		printf("goodix: SPI transfer of %u bytes failed: status %#x, sent %u, received %u\n", length, status, sent,
		    received);
	}
	geni_recover();
	return false;
}

/* ---- I2C through the same serial engine ------------------------------------------------------------ */

#define SE_I2C_TX_TRANS_LEN     (se + 0x26c)
#define SE_I2C_RX_TRANS_LEN     (se + 0x270)
#define SE_I2C_SCL_COUNTERS     (se + 0x278)

#define I2C_OP_WRITE            1
#define I2C_OP_READ             2
#define I2C_OP_WRITE_READ       3
#define I2C_OP_ADDR_ONLY        4
#define I2C_ADDR_SHIFT          9
#define I2C_STOP_STRETCH        (1u << 2)

/* 0 = ready; otherwise why the engine cannot be used. hz is the wanted SCL frequency. */
static const char *
geni_i2c_init(uint32_t hz)
{
	uint32_t div = 1, high = 5, low = 11, cycle = 22;

	if ((rd(GENI_FW_REVISION_RO) >> 8 & 0xff) != 3) {
		return "the serial engine has no I2C firmware";
	}
	if (rd(GENI_IF_DISABLE_RO) & 1) {
		return "the serial engine's FIFO interface is disabled";
	}
	fifo_depth = rd(SE_HW_PARAM_0) >> 16 & 0x3f;
	if (fifo_depth < 4) {
		return "the serial engine reports no FIFO";
	}
	wr(GENI_M_IRQ_CLEAR, 0xffffffff);
	wr(GENI_S_IRQ_CLEAR, 0xffffffff);
	wr(GENI_CGC_CTRL, rd(GENI_CGC_CTRL) | 0x7f);
	wr(GENI_OUTPUT_CTRL, 0x7f);
	wr(GENI_FORCE_DEFAULT, 1);
	wr(GENI_DMA_MODE_EN, rd(GENI_DMA_MODE_EN) & ~1u);
	wr(GENI_RX_WATERMARK, fifo_depth - 3);
	wr(GENI_RX_RFR_WATERMARK, fifo_depth - 2);
	wr(GENI_TX_WATERMARK, 0);
	wr(SE_IRQ_EN, rd(SE_IRQ_EN) | 0xf);
	wr(GENI_M_IRQ_EN, rd(GENI_M_IRQ_EN) | 0x7fc0007f);

	/* eight bits to a word, four bytes to a FIFO word - the same packing the SPI path uses */
	wr(GENI_TX_PACKING_CFG0, 0x0007f8fe);
	wr(GENI_TX_PACKING_CFG1, 0x000ffefe);
	wr(GENI_RX_PACKING_CFG0, 0x0007f8fe);
	wr(GENI_RX_PACKING_CFG1, 0x000ffefe);
	wr(GENI_BYTE_GRAN, 0);

	/* SCL = source / (cycle * div): twenty-two counts of the divider per bit period */
	if (hz == 0) {
		hz = 400000;
	}
	if (se_source_hz != 0) {
		uint32_t want = (se_source_hz + (cycle * hz) / 2) / (cycle * hz);

		div = want ? want : 1;
	}
	wr(GENI_CLK_SEL, se_clock_select & 7);
	wr(GENI_SER_M_CLK_CFG, div << 4 | 1);
	wr(SE_I2C_SCL_COUNTERS, high << 20 | low << 10 | cycle);
	return NULL;
}

static const char *
geni_i2c_transfer(uint32_t opcode, uint32_t address, const uint8_t *tx, uint32_t tx_len,
    uint8_t *rx, uint32_t rx_len)
{
	uint32_t sent = 0, received = 0, status = 0;
	uint64_t deadline;

	for (int i = 0; rd(GENI_STATUS) & 1; i++) {
		if (i > 2000) {
			return "the serial engine stays busy";
		}
		sleep_us(100);
	}
	wr(GENI_M_IRQ_CLEAR, 0xffffffff);
	if (tx_len != 0) {
		wr(SE_I2C_TX_TRANS_LEN, tx_len);
	}
	if (rx_len != 0) {
		wr(SE_I2C_RX_TRANS_LEN, rx_len);
	}
	wr(GENI_M_CMD0, (opcode << 27) | (address << I2C_ADDR_SHIFT));
	/* let the engine pull from the FIFO, then fill it */
	wr(GENI_TX_WATERMARK, tx_len != 0 ? 1 : 0);
	while (sent < tx_len && (rd(GENI_TX_FIFO_STATUS) & 0x0fffffff) + 1 < fifo_depth) {
		uint32_t word = 0;

		for (int j = 0; j < 4 && sent < tx_len; j++, sent++) {
			word |= (uint32_t)tx[sent] << (8 * j);
		}
		wr(GENI_TX_FIFO, word);
	}
	clock_interval_to_deadline(100, NSEC_PER_MSEC, &deadline);
	while (mach_absolute_time() < deadline) {
		status = rd(GENI_M_IRQ_STATUS);
		if (status & M_ERRORS) {
			break;
		}
		if (status & M_CMD_DONE) {
			if (rx_len != 0) {
				geni_drain(rx, rx_len, &received);
			}
			wr(GENI_M_IRQ_CLEAR, status);
			wr(GENI_TX_WATERMARK, 0);
			if (rx_len == 0 || received == rx_len) {
				return NULL;
			}
			return "the read returned fewer bytes than asked";
		}
		if (rx_len != 0) {
			geni_drain(rx, rx_len, &received);
		}
		sleep_us(20);
	}
	wr(GENI_TX_WATERMARK, 0);
	wr(GENI_M_IRQ_CLEAR, status ? status : 0xffffffff);
	geni_recover();
	{
		static char why[96];

		snprintf(why, sizeof(why), "no completion (irq status %#x, sent %u, received %u)", status, sent, received);
		return why;
	}
}

/* ---- SPI by hand, on the same pins ----------------------------------------------------------------- */

static unsigned bitbang_in = PIN_MISO, bitbang_out = PIN_MOSI;

static void
bitbang_init(void)
{
	gpio_output(PIN_CS, true, PULL_NONE, 6);
	gpio_output(PIN_CLK, false, PULL_NONE, 6);
	gpio_output(bitbang_out, false, PULL_NONE, 6);
	gpio_configure(bitbang_in, 0, PULL_NONE, 2, false);
}

static bool
bitbang_transfer(const uint8_t *tx, uint8_t *rx, uint32_t length)
{
	gpio_set(PIN_CS, false);
	for (uint32_t i = 0; i < length; i++) {
		uint8_t out = tx ? tx[i] : 0, in = 0;

		for (int bit = 7; bit >= 0; bit--) {
			gpio_set(bitbang_out, out >> bit & 1);
			gpio_set(PIN_CLK, true);
			in = (uint8_t)(in << 1 | gpio_get(bitbang_in));
			gpio_set(PIN_CLK, false);
		}
		if (rx) {
			rx[i] = in;
		}
	}
	gpio_set(PIN_CS, true);
	return true;
}

static bool (*spi_transfer)(const uint8_t *tx, uint8_t *rx, uint32_t length);

/* ---- the Goodix chip ------------------------------------------------------------------------------- */

#define GOODIX_FW_VERSION       0x10014         /* 28 bytes */
#define GOODIX_IC_INFO          0x10070         /* a 16-bit length, then that many bytes */
#define GOODIX_MAX_READ         1024
#define GOODIX_MAX_TOUCHES      10
#define GOODIX_STATUS_TOUCH     0x80
#define GOODIX_STATUS_REQUEST   0x40
#define GOODIX_REQUEST_RESET    3

static uint8_t spi_tx[8 + GOODIX_MAX_READ], spi_rx[8 + GOODIX_MAX_READ];
static uint32_t touch_address, command_address;
static const char *report_rate = "240";        /* what the status line says (touch_rate=480) */
static uint32_t max_x = 10800, max_y = 24000;   /* the device tree's; the chip may say otherwise */

static bool
goodix_read(uint32_t address, uint8_t *data, uint32_t length)
{
	if (length > GOODIX_MAX_READ) {
		return false;
	}
	spi_tx[0] = 0xf1;
	spi_tx[1] = (uint8_t)(address >> 24);
	spi_tx[2] = (uint8_t)(address >> 16);
	spi_tx[3] = (uint8_t)(address >> 8);
	spi_tx[4] = (uint8_t)address;
	spi_tx[5] = spi_tx[6] = spi_tx[7] = 0xff;
	memset(spi_tx + 8, 0, length);
	if (!spi_transfer(spi_tx, spi_rx, 8 + length)) {
		return false;
	}
	memcpy(data, spi_rx + 8, length);
	return true;
}

static bool
goodix_write(uint32_t address, const uint8_t *data, uint32_t length)
{
	if (length > GOODIX_MAX_READ) {
		return false;
	}
	spi_tx[0] = 0xf0;
	spi_tx[1] = (uint8_t)(address >> 24);
	spi_tx[2] = (uint8_t)(address >> 16);
	spi_tx[3] = (uint8_t)(address >> 8);
	spi_tx[4] = (uint8_t)address;
	memcpy(spi_tx + 5, data, length);
	return spi_transfer(spi_tx, NULL, 5 + length);
}

/* The chip's blocks end in the 16-bit sum of their bytes; all zeros or all ones is no answer at all. */
static bool
goodix_checksum_ok(const uint8_t *data, uint32_t length)
{
	uint32_t sum = 0;
	bool blank = true;

	if (length < 2) {
		return false;
	}
	for (uint32_t i = 0; i < length - 2; i++) {
		sum += data[i];
		blank &= data[i] == 0 || data[i] == 0xff;
	}
	return !blank && (uint16_t)sum == (uint16_t)(data[length - 2] | data[length - 1] << 8);
}

static uint16_t
le16(const uint8_t *p)
{
	return (uint16_t)(p[0] | p[1] << 8);
}

static uint32_t
le32(const uint8_t *p)
{
	return p[0] | p[1] << 8 | p[2] << 16 | (uint32_t)p[3] << 24;
}

static void
touch_reset(void)
{
	gpio_set(PIN_RESET, false);
	sleep_ms(2);
	gpio_set(PIN_RESET, true);
	sleep_ms(100);
}

/* Is the chip there and running its firmware? Finds where it keeps the touch report. */
static bool
goodix_probe(bool verbose)
{
	static uint8_t info[GOODIX_MAX_READ];
	uint8_t version[28];
	uint32_t length, offset;

	if (!goodix_read(GOODIX_FW_VERSION, version, sizeof(version))) {
		return false;
	}
	if (!goodix_checksum_ok(version, sizeof(version))) {
		if (verbose) {
			printf("goodix: no valid firmware version: %02x %02x %02x %02x %02x %02x %02x %02x ... %02x %02x\n",
			    version[0], version[1], version[2], version[3], version[4], version[5], version[6], version[7],
			    version[26], version[27]);
		}
		return false;
	}
	printf("goodix: chip \"%.6s\", firmware \"%.8s\" %02x.%02x.%02x.%02x, sensor %u\n", (const char *)version,
	    (const char *)version + 10, version[18], version[19], version[20], version[21], version[23]);

	if (!goodix_read(GOODIX_IC_INFO, info, 2)) {
		return false;
	}
	length = le16(info);
	if (length < 40 || length >= GOODIX_MAX_READ || !goodix_read(GOODIX_IC_INFO, info, length) ||
	    !goodix_checksum_ok(info, length)) {
		printf("goodix: bad chip description (length %u)\n", length);
		return false;
	}
	/* the length, the version (16 bytes), the features (10), four counts, then five lists of 16-bit numbers */
	offset = 2 + 16 + 10 + 4;
	for (int i = 0; i < 5; i++) {
		if (offset >= length) {
			return false;
		}
		offset += 1 + 2u * info[offset];
	}
	if (offset + 48 + 2 > length) {
		printf("goodix: chip description too short (%u, lists end at %u)\n", length, offset);
		return false;
	}
	touch_address = le32(info + offset + 44);
	command_address = le32(info + offset + 0);      /* (the "misc" part: the command buffer comes first) */
	if (offset + 122 + 20 + 2 <= length) {
		uint16_t x = le16(info + offset + 122 + 16), y = le16(info + offset + 122 + 18);

		if (x && y) {
			max_x = x;
			max_y = y;
		}
	}
	printf("goodix: touch report at %#x, coordinates up to %u x %u\n", touch_address, max_x, max_y);
	return touch_address != 0;
}

/*
 * A command to the chip (Android's brl_send_cmd(): goodix_brl_hw.c of the phone's kernel): sixteen
 * bytes at the command address -- state, acknowledgement, length, command, its data, and the sum of
 * the bytes from the length on -- then the acknowledgement looked for, 0x80 when done.
 */
#define GOODIX_CMD_HIGH_RATE    0xc0            /* data 1: report 480 times a second instead of 240 (Xiaomi's game mode) */
#define GOODIX_ACK_OK           0x80
#define GOODIX_ACK_BUSY         0x02

static bool
goodix_command(uint8_t command, uint8_t data)
{
	uint8_t buffer[16], answer[16];
	uint32_t sum;

	if (command_address == 0) {
		return false;
	}
	memset(buffer, 0, sizeof(buffer));
	buffer[2] = 5;
	buffer[3] = command;
	buffer[4] = data;
	sum = buffer[2] + buffer[3] + buffer[4];
	buffer[5] = (uint8_t)sum;
	buffer[6] = (uint8_t)(sum >> 8);
	for (int attempt = 0; attempt < 6; attempt++) {
		if (!goodix_write(command_address, buffer, sizeof(buffer))) {
			return false;
		}
		for (int look = 0; look < 6; look++) {
			if (!goodix_read(command_address, answer, sizeof(answer))) {
				return false;
			}
			if (answer[1] == GOODIX_ACK_OK) {
				return true;
			}
			sleep_ms(1);
			if (answer[1] != GOODIX_ACK_BUSY && answer[1] != 0) {
				break;
			}
		}
	}
	return false;
}

/* What has been seen so far: said once a minute, so that the end of the log always tells. */
static struct {
	uint32_t polls, reports, touches, bad, last_x, last_y;
} seen;

/* Which fingers were down in the last report, and where. */
static struct {
	bool down, seen;
	uint32_t x, y;
} contact[GOODIX_MAX_TOUCHES];
static uint32_t flip;           /* boot-arg touch_flip: 1 = mirror left-right, 2 = mirror top-bottom */

static void
goodix_report(const uint8_t *touches, uint32_t count)
{
	static unsigned shown;

	for (int i = 0; i < GOODIX_MAX_TOUCHES; i++) {
		contact[i].seen = false;
	}
	for (uint32_t i = 0; i < count; i++) {
		const uint8_t *t = touches + 8 * i;
		uint32_t id = t[0] >> 4, kind = t[0] & 0xf;
		uint32_t x = le16(t + 2), y = le16(t + 4);

		if (id >= GOODIX_MAX_TOUCHES || kind == 1 || kind == 3) {       /* 1 and 3: a stylus */
			continue;
		}
		if (shown < 40) {
			shown++;
			printf("goodix: finger %u at %u, %u (of %u x %u)\n", id, x, y, max_x, max_y);
		}
		seen.touches++;
		seen.last_x = x;
		seen.last_y = y;
		x = x >= max_x ? 65535 : x * 65535 / max_x;
		y = y >= max_y ? 65535 : y * 65535 / max_y;
		if (flip & 1) {
			x = 65535 - x;
		}
		if (flip & 2) {
			y = 65535 - y;
		}
		contact[id].seen = true;
		if (!contact[id].down) {
			redwood_touch_post(REDWOOD_TOUCH_DOWN, id, x, y);
		} else if (contact[id].x != x || contact[id].y != y) {
			redwood_touch_post(REDWOOD_TOUCH_MOVE, id, x, y);
		}
		contact[id].down = true;
		contact[id].x = x;
		contact[id].y = y;
	}
	/* a finger that is no longer in the report has lifted */
	for (uint32_t id = 0; id < GOODIX_MAX_TOUCHES; id++) {
		if (contact[id].down && !contact[id].seen) {
			contact[id].down = false;
			redwood_touch_post(REDWOOD_TOUCH_UP, id, contact[id].x, contact[id].y);
		}
	}
}

/* Read the report if there is one, and tell the chip it has been read. */
static void
goodix_poll(void)
{
	static uint8_t report[8 + 8 * GOODIX_MAX_TOUCHES + 2];
	static unsigned bad;
	const uint8_t zero = 0;
	const uint32_t first = 8 + 8 * 2 + 2;           /* the header and two fingers: all there is, most of the time */
	uint32_t count;

	seen.polls++;
	if (!goodix_read(touch_address, report, first) || report[0] == 0) {
		return;
	}
	seen.reports++;
	count = report[2] & 0xf;
	if ((report[0] & GOODIX_STATUS_TOUCH) && count > 2 && count <= GOODIX_MAX_TOUCHES &&
	    !goodix_read(touch_address + 8, report + 8, 8 * count + 2)) {
		return;
	}
	goodix_write(touch_address, &zero, 1);
	if (!goodix_checksum_ok(report, 8)) {
		seen.bad++;
		if (bad++ < 8) {
			printf("goodix: bad report header: %02x %02x %02x %02x %02x %02x %02x %02x\n", report[0], report[1],
			    report[2], report[3], report[4], report[5], report[6], report[7]);
		}
		return;
	}
	if (report[0] & GOODIX_STATUS_REQUEST) {
		if ((report[2] & 0xf) == GOODIX_REQUEST_RESET || report[2] == GOODIX_REQUEST_RESET) {
			printf("goodix: the chip asks to be reset\n");
			touch_reset();
		}
		return;
	}
	if (!(report[0] & GOODIX_STATUS_TOUCH)) {
		return;
	}
	if (count > GOODIX_MAX_TOUCHES || (count && !goodix_checksum_ok(report + 8, count * 8 + 2))) {
		seen.bad++;
		if (bad++ < 8) {
			printf("goodix: bad report (%u touches)\n", count);
		}
		return;
	}
	goodix_report(report + 8, count);
}

/* ---- the FocalTech FT3680 chip --------------------------------------------------------------------- */

/*
 * FTS 5.x SPI framing from sm8350-kernel-OKX drivers/input/touchscreen/focaltech_3680/
 * focaltech_spi.c (verbatim):
 *   TX[0] = register address (writebuf[0] / cmd[0])
 *   TX[1] = FOCALTECH_READ_CTRL (0xA0 = READ_CMD, CRC_EN set) or FOCALTECH_WRITE_CTRL (0x00)
 *   TX[2..3] = data length BE (for read: datalen; for write: writelen - 1)
 *   TX[4..6] = three dummy bytes (SPI_DUMMY_BYTE)
 *   TX[7..7+len-1] = write data (empty on read; chip drives MISO here)
 *   TX[last-1..last] = CRC-16/Kermit of the data bytes, little-endian (only when READ_CMD or
 *                     when a vendor WRITE op needs CRC; vendor WRITE_CMD = 0x00 has no CRC)
 * SPI_HEADER_LENGTH = 4 + SPI_DUMMY_BYTE = 7.
 */
#define FOCALTECH_READ_CTRL         0xa0		/* READ_CMD = 0x80 | DATA_CRC_EN(0x20) */
#define FOCALTECH_WRITE_CTRL        0x00		/* WRITE_CMD, no CRC on TX */
#define FOCALTECH_CHIP_ID_H         0xa3
#define FOCALTECH_CHIP_ID_L         0x9f
#define FOCALTECH_TOUCH_DATA        0x01		/* the touch report lives at this address */
#define FOCALTECH_MAX_TOUCHES       10
#define FOCALTECH_TOUCH_REPORT_LEN  (2 + 6 * FOCALTECH_MAX_TOUCHES)
#define FOCALTECH_MAX_TRANSFER      (7 + FOCALTECH_TOUCH_REPORT_LEN + 2)

static uint8_t focaltech_tx[FOCALTECH_MAX_TRANSFER], focaltech_rx[FOCALTECH_MAX_TRANSFER];

/* CRC-16/Kermit (polynomial 0x8408 reflected, initial 0xffff) -- same as vendor rckit_crc16(). */
static uint16_t
focaltech_crc(const uint8_t *data, uint32_t length)
{
	uint16_t crc = 0xffff;

	for (uint32_t i = 0; i < length; i++) {
		crc ^= data[i];
		for (int bit = 0; bit < 8; bit++) {
			crc = (crc & 1) ? (uint16_t)((crc >> 1) ^ 0x8408) : (uint16_t)(crc >> 1);
		}
	}
	return crc;
}

static bool
focaltech_read(uint8_t address, uint8_t *data, uint32_t length)
{
	const uint32_t data_offset = 7;
	uint32_t transfer_length = data_offset + length + 2;

	if (length == 0 || transfer_length > sizeof(focaltech_tx)) {
		return false;
	}
	for (int attempt = 0; attempt < 3; attempt++) {
		memset(focaltech_tx, 0, transfer_length);
		memset(focaltech_rx, 0, transfer_length);
		focaltech_tx[0] = address;		/* vendor: TX[0] = cmd[0] = reg address */
		focaltech_tx[1] = FOCALTECH_READ_CTRL;	/* vendor: TX[1] = ctrl = 0xA0 */
		focaltech_tx[2] = (uint8_t)(length >> 8);
		focaltech_tx[3] = (uint8_t)length;
		if (!spi_transfer(focaltech_tx, focaltech_rx, transfer_length)) {
			sleep_us(150);
			continue;
		}
		/* vendor: bit 7 of status (rx[3]) is BUSY, bit 5 is CRC error */
		if (!(focaltech_rx[3] & 0xa0)) {
			uint16_t expected = focaltech_crc(focaltech_rx + data_offset, length);
			uint16_t received = (uint16_t)(focaltech_rx[data_offset + length] |
			    focaltech_rx[data_offset + length + 1] << 8);
			if (expected == received) {
				memcpy(data, focaltech_rx + data_offset, length);
				sleep_us(150);		/* minimum CS-high time in the vendor driver */
				return true;
			}
		}
		sleep_us(150);
	}
	return false;
}

/*
 * Vendor fts_write: no CRC on TX. For a 1-byte "command" (length == 0) the frame is just the
 * four header bytes { addr, 0x00, 0x00, 0x00 } with no dummy and no data. For length > 0 the
 * three dummy bytes precede the data: { addr, 0x00, lenH, lenL, 0, 0, 0, data... }. The chip
 * drives the "status" byte on rx[3] -- bits 7 and 5 set mean busy or CRC error (vendor retries
 * SPI_RETRY_NUMBER = 3 times on failure before giving up).
 */
static bool
focaltech_write(uint8_t address, const uint8_t *data, uint32_t length)
{
	uint32_t transfer_length;

	if (length == 0) {
		transfer_length = 4;
	} else {
		transfer_length = 7 + length;
	}
	if (transfer_length > sizeof(focaltech_tx)) {
		return false;
	}
	memset(focaltech_tx, 0, transfer_length);
	focaltech_tx[0] = address;
	focaltech_tx[1] = FOCALTECH_WRITE_CTRL;
	focaltech_tx[2] = (uint8_t)(length >> 8);
	focaltech_tx[3] = (uint8_t)length;
	if (data && length > 0) {
		memcpy(focaltech_tx + 7, data, length);
	}
	for (int attempt = 0; attempt < 3; attempt++) {
		if (spi_transfer(focaltech_tx, focaltech_rx, transfer_length) &&
		    !(focaltech_rx[3] & 0xa0)) {
			sleep_us(150);
			return true;
		}
		sleep_us(150);
	}
	return false;
}

/*
 * Vendor fts_fwupg_get_boot_state() reads the chip's "boot ID" via the FT boot-ROM protocol:
 *   TX = { 0x55, 0xAA }                 (FTS_CMD_START1, FTS_CMD_START2 -- enter boot)
 *   wait FTS_CMD_START_DELAY = 12 ms
 *   TX = { 0x90 }, RX = 2 bytes         (FTS_CMD_READ_ID, incell cmd length)
 * If the two RX bytes are the chip's boot ID (0x56 0x62 for FT3680), firmware is present and
 * valid in flash but has not yet been started; the host must issue FTS_CMD_FIRMWARE_START
 * (0x08) to make the boot ROM jump to it.
 */
#define FTS_CMD_START1              0x55
#define FTS_CMD_START2              0xaa
#define FTS_CMD_START_DELAY         12		/* ms, between enter-boot and first command */
#define FTS_CMD_READ_BOOT_ID        0x90

/* Boot-ROM commands for firmware (pramboot) upload, from focaltech_3658u/focaltech_flash.h. */
#define FTS_ROMBOOT_CMD_SET_PRAM_ADDR   0xad
#define FTS_ROMBOOT_CMD_WRITE           0xae
#define FTS_ROMBOOT_CMD_START_APP       0x08
#define FTS_FLASH_PACKET_LENGTH         32		/* bytes per PRAM write */

#include "redwood_focaltech_fw.h"

/*
 * Vendor fts_read_bootid(): send { 0x55, 0xAA } as a 2-byte framed write (treated as a write of
 * one data byte 0xAA to register 0x55), wait 12 ms, then do a framed read of 2 bytes at
 * register 0x90. The chip answers with its ROM ID (0x56 0x62 for FT3680) if it is sitting in
 * the boot ROM waiting for a firmware download.
 */
static bool
focaltech_read_boot_id(uint8_t *high, uint8_t *low)
{
	uint8_t aa = FTS_CMD_START2;
	uint8_t out[2] = { 0 };
	bool ok;

	if (!focaltech_write(FTS_CMD_START1, &aa, 1)) {
		return false;
	}
	sleep_ms(12);			/* FTS_CMD_START_DELAY */
	ok = focaltech_read(FTS_CMD_READ_BOOT_ID, out, 2);
	*high = out[0];
	*low  = out[1];
	return ok;
}

/* Vendor fts_wait_tp_to_valid: poll chip ID for up to TIMEOUT_READ_REG (1 s) at 200 ms steps. */
static bool
focaltech_wait_valid(uint8_t *high, uint8_t *low)
{
	for (int attempt = 0; attempt < 5; attempt++) {
		if (focaltech_read(FOCALTECH_CHIP_ID_H, high, 1) &&
		    focaltech_read(FOCALTECH_CHIP_ID_L, low, 1) &&
		    *high == 0x56 && *low == 0x62) {
			printf("focaltech: FT3680 runtime ID valid after %d attempt(s)\n", attempt + 1);
			return true;
		}
		sleep_ms(200);			/* vendor INTERVAL_READ_REG */
	}
	return false;
}

/*
 * Reset the chip and send 0x55/0xAA to put it in boot ROM, as vendor fts_get_ic_information
 * does. Try up to 3 times with escalating settle delays (12, 20, 28 ms). Returns true if the
 * chip is sitting in its boot ROM, with { 0x56, 0x62 } read back from the boot-ID register.
 */
static bool
focaltech_enter_boot_rom(void)
{
	uint8_t boot_high = 0, boot_low = 0;

	for (int cnt = 0; cnt < 3; cnt++) {
		gpio_set(PIN_RESET, false);
		sleep_ms(1);
		gpio_set(PIN_RESET, true);
		sleep_ms(FTS_CMD_START_DELAY + cnt * 8);
		if (focaltech_read_boot_id(&boot_high, &boot_low) &&
		    boot_high == 0x56 && boot_low == 0x62) {
			printf("focaltech: in boot ROM after reset %d\n", cnt);
			return true;
		}
		printf("focaltech: boot ID attempt %d: %02x %02x (not 56 62)\n", cnt, boot_high, boot_low);
	}
	return false;
}

/*
 * Download the embedded firmware (pramboot + application) into the chip's PRAM via the boot
 * ROM's SPI_V2 upload protocol. Each 32-byte packet is sent as two framed writes: first set
 * the PRAM destination address (SET_PRAM_ADDR + 3 BE address bytes), then WRITE + data bytes.
 */
static bool
focaltech_write_pram(const uint8_t *buf, uint32_t len)
{
	uint8_t addr_bytes[3];

	for (uint32_t offset = 0; offset < len; offset += FTS_FLASH_PACKET_LENGTH) {
		uint32_t chunk = FTS_FLASH_PACKET_LENGTH;
		if (offset + chunk > len) {
			chunk = len - offset;
		}
		addr_bytes[0] = (uint8_t)(offset >> 16);
		addr_bytes[1] = (uint8_t)(offset >> 8);
		addr_bytes[2] = (uint8_t)offset;
		if (!focaltech_write(FTS_ROMBOOT_CMD_SET_PRAM_ADDR, addr_bytes, 3)) {
			printf("focaltech: PRAM address write failed at offset %u\n", offset);
			return false;
		}
		if (!focaltech_write(FTS_ROMBOOT_CMD_WRITE, buf + offset, chunk)) {
			printf("focaltech: PRAM data write failed at offset %u\n", offset);
			return false;
		}
	}
	return true;
}

static bool
focaltech_start_app(void)
{
	uint8_t cmd = FTS_ROMBOOT_CMD_START_APP;

	return focaltech_write(cmd, NULL, 0);
}

/*
 * Full chip bring-up: look for an already-running firmware; if none, reset to boot ROM and
 * download the embedded firmware into PRAM; then start it and wait for runtime to come up.
 */
static bool
focaltech_probe(bool verbose)
{
	uint8_t high = 0, low = 0;

	/* Maybe firmware is already running (previous session, warm boot). */
	if (focaltech_wait_valid(&high, &low)) {
		return true;
	}

	if (!focaltech_enter_boot_rom()) {
		if (verbose) {
			printf("focaltech: chip not in boot ROM; last read runtime %02x %02x\n", high, low);
		}
		return false;
	}

	printf("focaltech: downloading %u bytes of firmware to PRAM...\n", FOCALTECH_FW_LENGTH);
	if (!focaltech_write_pram(focaltech_fw, FOCALTECH_FW_LENGTH)) {
		printf("focaltech: PRAM download failed\n");
		return false;
	}
	printf("focaltech: PRAM download complete; starting application\n");
	if (!focaltech_start_app()) {
		printf("focaltech: start-app command failed\n");
		return false;
	}
	sleep_ms(250);			/* firmware needs time to initialize all peripherals */

	if (focaltech_wait_valid(&high, &low)) {
		return true;
	}
	if (verbose) {
		printf("focaltech: firmware did not come up (last runtime read %02x %02x)\n", high, low);
	}
	return false;
}

/* ---- FocalTech touch reports --------------------------------------------------------------------- */

/*
 * Per-chip report layout for FT3680 (vendor focaltech_core.c, FTS_TOUCH_OFF_* offsets):
 *   report[0] = gesture (0 outside gesture mode)
 *   report[1] = (chip_type << 4) | finger_count
 *   report[2 + 6*i + 0] = E_XH:  bits 7..6 = event (0 = down, 1 = up, 2 = contact)
 *                                bits 3..0 = x bits 14..11
 *   report[2 + 6*i + 1] = XL:    x bits 10..3
 *   report[2 + 6*i + 2] = ID_YH: bits 7..4 = finger id, bits 3..0 = y bits 14..11
 *   report[2 + 6*i + 3] = YL:    y bits 10..3
 *   report[2 + 6*i + 4] = PRE:   bits 7..5 = x bits 2..0, bits 4..2 = y bits 2..0
 *                                (also the pressure byte -- the lowest bits of each 15-bit axis
 *                                 are packed in with it)
 *   report[2 + 6*i + 5] = AREA:  touch area / major
 * Coordinates are 15-bit "super-high resolution" sub-pixel values; the touch panel's actual
 * extent is reported through the device tree as x_max/y_max (10800 x 24000 for redwood).
 */
#define FOCALTECH_EVENT_DOWN        0
#define FOCALTECH_EVENT_UP          1
#define FOCALTECH_EVENT_CONTACT     2

static struct {
	bool down, seen;
	uint32_t x, y;
} focaltech_contact[FOCALTECH_MAX_TOUCHES];

static void
focaltech_contact_update(uint32_t id, uint32_t event, uint32_t x, uint32_t y,
    uint32_t max_report_x, uint32_t max_report_y)
{
	static unsigned shown;

	if (id >= FOCALTECH_MAX_TOUCHES || max_report_x == 0 || max_report_y == 0) {
		return;
	}
	if (event == FOCALTECH_EVENT_UP) {
		focaltech_contact[id].seen = true;
		if (focaltech_contact[id].down) {
			focaltech_contact[id].down = false;
			redwood_touch_post(REDWOOD_TOUCH_UP, id, focaltech_contact[id].x, focaltech_contact[id].y);
		}
		return;
	}
	seen.touches++;
	switch (flip) {
	case 1: x = max_report_x - 1 - x; break;
	case 2: y = max_report_y - 1 - y; break;
	case 3: x = max_report_x - 1 - x; y = max_report_y - 1 - y; break;
	}
	x = (uint32_t)(((uint64_t)x * 65535) / (max_report_x - 1));
	y = (uint32_t)(((uint64_t)y * 65535) / (max_report_y - 1));
	seen.last_x = x;
	seen.last_y = y;
	focaltech_contact[id].seen = true;
	if (!focaltech_contact[id].down) {
		focaltech_contact[id].down = true;
		redwood_touch_post(REDWOOD_TOUCH_DOWN, id, x, y);
	} else if (focaltech_contact[id].x != x || focaltech_contact[id].y != y) {
		redwood_touch_post(REDWOOD_TOUCH_MOVE, id, x, y);
	}
	focaltech_contact[id].x = x;
	focaltech_contact[id].y = y;
	if (shown++ < 8) {
		printf("focaltech: finger %u %s at %u, %u\n", id,
		    event == FOCALTECH_EVENT_DOWN ? "down" : "move", x, y);
	}
}

/* Panel size in the FT3680 super-high-resolution reports (device tree x_max/y_max on redwood). */
static uint32_t focaltech_max_x = 10800, focaltech_max_y = 24000;

static void
focaltech_report(const uint8_t *report)
{
	uint32_t count = report[1] & 0x0f;

	for (uint32_t i = 0; i < FOCALTECH_MAX_TOUCHES; i++) {
		focaltech_contact[i].seen = false;
	}
	for (uint32_t i = 0; i < FOCALTECH_MAX_TOUCHES; i++) {
		const uint8_t *touch = report + 2 + 6 * i;
		uint32_t id = (touch[2] >> 4) & 0x0f;
		uint32_t event, x, y;

		if (id >= FOCALTECH_MAX_TOUCHES) {
			break;			/* no more valid slots in this report */
		}
		event = (touch[0] >> 6) & 0x3;
		x = ((uint32_t)(touch[0] & 0x0f) << 11) | ((uint32_t)touch[1] << 3) |
		    ((uint32_t)touch[4] >> 5);
		y = ((uint32_t)(touch[2] & 0x0f) << 11) | ((uint32_t)touch[3] << 3) |
		    (((uint32_t)touch[4] & 0x1c) >> 2);
		focaltech_contact_update(id, event, x, y, focaltech_max_x, focaltech_max_y);
	}
	(void)count;
	/* Fingers not in this report are up (vendor driver uses a bitmap; we remember explicitly). */
	for (uint32_t i = 0; i < FOCALTECH_MAX_TOUCHES; i++) {
		if (!focaltech_contact[i].seen && focaltech_contact[i].down) {
			focaltech_contact[i].down = false;
			redwood_touch_post(REDWOOD_TOUCH_UP, i, focaltech_contact[i].x, focaltech_contact[i].y);
		}
	}
}

static void
focaltech_poll(void)
{
	static uint8_t report[FOCALTECH_TOUCH_REPORT_LEN];
	static unsigned bad;

	seen.polls++;
	if (!focaltech_read(FOCALTECH_TOUCH_DATA, report, sizeof(report))) {
		seen.bad++;
		if (bad++ < 8) {
			printf("focaltech: SPI read failed for touch report\n");
		}
		return;
	}
	seen.reports++;
	focaltech_report(report);
}

/* Which touch chip is on this board. */
enum touch_chip { TOUCH_GOODIX, TOUCH_FOCALTECH };
static enum touch_chip touch_chip;

static enum touch_chip
touch_chip_detect(void)
{
	gpio_configure(PIN_FOCALTECH_ID, 0, PULL_NONE, 2, false);
	gpio_configure(PIN_GOODIX_ID, 0, PULL_NONE, 2, false);
	if (gpio_get(PIN_GOODIX_ID) && !gpio_get(PIN_FOCALTECH_ID)) {
		return TOUCH_FOCALTECH;
	}
	return TOUCH_GOODIX;
}

static bool
chip_probe(bool verbose)
{
	if (touch_chip == TOUCH_FOCALTECH) {
		return focaltech_probe(verbose);
	}
	touch_reset();
	return goodix_probe(verbose);
}

static void
chip_poll(void)
{
	if (touch_chip == TOUCH_FOCALTECH) {
		focaltech_poll();
	} else {
		goodix_poll();
	}
}

/* ---- all of it together ---------------------------------------------------------------------------- */

static void
show_state(const char *when)
{
	printf("goodix: %s: votes %#x, clocks %#x %#x %#x, rate cfg %#x dfs %#x; pins cs/clk/mosi/miso cfg %#x %#x %#x %#x,"
	    " reset %#x/%u, int %#x/%u, avdd %#x/%u, straps %u %u\n", when, rd(GCC_VOTE), rd(GCC_QUP1_M_AHB_CBCR),
	    rd(GCC_QUP1_S_AHB_CBCR), rd(GCC_QUP1_SE5_CBCR), rd(GCC_QUP1_SE5_CFG), rd(GCC_QUP1_SE5_DFSR), rd(GPIO_CFG(PIN_CS)),
	    rd(GPIO_CFG(PIN_CLK)), rd(GPIO_CFG(PIN_MOSI)), rd(GPIO_CFG(PIN_MISO)), rd(GPIO_CFG(PIN_RESET)),
	    rd(GPIO_IO(PIN_RESET)), rd(GPIO_CFG(PIN_INT)), rd(GPIO_IO(PIN_INT)), rd(GPIO_CFG(PIN_AVDD)), rd(GPIO_IO(PIN_AVDD)),
	    gpio_get(68), gpio_get(96));
}

/* Power the chip up from nothing, in the vendor driver's order. */
static void
power_on(void)
{
	gpio_output(PIN_RESET, false, PULL_UP, 8);
	gpio_configure(PIN_INT, 0, PULL_UP, 8, false);
	io_supply_on();
	sleep_ms(3);
	gpio_output(PIN_AVDD, true, PULL_NONE, 2);
	sleep_ms(15);
}

static void
use_engine_pins(void)
{
	gpio_configure(PIN_MISO, PIN_FUNCTION_SPI, PULL_NONE, 6, false);
	gpio_configure(PIN_MOSI, PIN_FUNCTION_SPI, PULL_NONE, 6, false);
	gpio_configure(PIN_CLK, PIN_FUNCTION_SPI, PULL_NONE, 6, false);
	gpio_configure(PIN_CS, PIN_FUNCTION_SPI, PULL_NONE, 6, false);
}

static const char *
chip_name(void)
{
	return touch_chip == TOUCH_FOCALTECH ? "focaltech" : "goodix";
}

static bool
bring_up(void)
{
	const char *problem = "its clocks cannot be turned on";

	show_state("as found");
	touch_chip = touch_chip_detect();
	printf("goodix: ID gpio96=%u gpio68=%u: selected %s\n", gpio_get(PIN_GOODIX_ID),
	    gpio_get(PIN_FOCALTECH_ID), chip_name());
	power_on();

	if (clocks_on()) {
		printf("goodix: serial engine: firmware %#x, interface %#x, parameters %#x, source clock %u Hz (select %u)\n",
		    rd(GENI_FW_REVISION_RO), rd(GENI_IF_DISABLE_RO), rd(SE_HW_PARAM_0), se_source_hz, se_clock_select);
		uint32_t spi_hz = 1000000;

		/* boot-arg touch_spi_hz=N: the bus at N Hz (Android's device tree gives the chip 5 MHz) */
		PE_parse_boot_argn("touch_spi_hz", &spi_hz, sizeof(spi_hz));
		if (spi_hz < 100000 || spi_hz > 10000000) {
			spi_hz = 1000000;
		}
		printf("goodix: the bus at %u Hz\n", spi_hz);
		problem = geni_spi_init(spi_hz);
	}
	if (problem == NULL) {
		use_engine_pins();
		spi_transfer = geni_transfer;
		show_state("powered, on the serial engine");
		if (chip_probe(true)) {
			return true;
		}
		printf("goodix: %s did not answer through the serial engine; trying the pins by hand\n", chip_name());
	} else {
		printf("goodix: %s; driving the pins by hand\n", problem);
	}

	/* by hand; which of the two data pins is the input is not written down anywhere, so try both */
	spi_transfer = bitbang_transfer;
	for (int attempt = 0; attempt < 2; attempt++) {
		bitbang_in = attempt == 0 ? PIN_MISO : PIN_MOSI;
		bitbang_out = attempt == 0 ? PIN_MOSI : PIN_MISO;
		bitbang_init();
		if (chip_probe(true)) {
			printf("goodix: %s answers on pins driven by hand (data in on gpio%u)\n", chip_name(), bitbang_in);
			return true;
		}
	}
	show_state("no answer");
	return false;
}

static uint64_t
micros_now(void)
{
	uint64_t ns;

	absolutetime_to_nanoseconds(mach_absolute_time(), &ns);
	return ns / 1000;
}

static void
goodix_thread(__unused void *parameter, __unused wait_result_t result)
{
	unsigned idle = 0, idle_looks;
	uint32_t poll_us = 8000, latency = 0, reads = 0;
	uint64_t before = 0, reading = 0;

	PE_parse_boot_argn("touch_poll_us", &poll_us, sizeof(poll_us));
	PE_parse_boot_argn("touch_lat", &latency, sizeof(latency));
	if (poll_us < 250 || poll_us > 100000) {
		poll_us = 8000;
	}
	idle_looks = 80000 / poll_us;   /* a report read every 80 ms even with the line high, in case it is missed */

	tlmm = ml_io_map(TLMM_PHYS, TLMM_SIZE);
	gcc = ml_io_map(GCC_PHYS, GCC_SIZE);
	se = ml_io_map(SE_PHYS, SE_SIZE);
	rsc = ml_io_map(RSC_PHYS, RSC_SIZE);
	cmd_db = ml_io_map_wcomb(CMD_DB_PHYS, CMD_DB_SIZE);      /* memory, not registers: no alignment rules */

	if (!bring_up()) {
		printf("goodix: no %s touch screen\n", chip_name());
		thread_terminate(current_thread());
		return;
	}
	printf("goodix: %s touch screen ready; the interrupt line is looked at every %u us\n", chip_name(), poll_us);
	{
		uint32_t rate = 0;

		/* boot-arg touch_rate=480: the chip reports twice as often (Xiaomi's game mode; 240 otherwise) */
		if (PE_parse_boot_argn("touch_rate", &rate, sizeof(rate)) && rate == 480 && touch_chip == TOUCH_GOODIX) {
			bool done = goodix_command(GOODIX_CMD_HIGH_RATE, 1);

			report_rate = done ? "480 (acknowledged)" : "240 (480 NOT ACKNOWLEDGED)";
			printf("goodix: reports 480 times a second (command %#x at %#x): %s\n", GOODIX_CMD_HIGH_RATE, command_address,
			    done ? "acknowledged" : "NOT ACKNOWLEDGED");
		}
	}
	for (unsigned tick = 1;; tick++) {
		uint64_t looked = micros_now(), read_at;

		/* the interrupt line is low while a report waits; look anyway every 80 ms in case it is missed */
		if (!gpio_get(PIN_INT)) {
			idle = 0;
			chip_poll();
			read_at = micros_now();
			reads++;
			reading += read_at - looked;
			if (latency) {
				redwood_tl("tl: c before %llu seen %llu read %llu\n", before, looked, read_at);
			}
		} else if (++idle >= idle_looks) {
			idle = 0;
			chip_poll();
			reads++;
			reading += micros_now() - looked;
		}
		before = looked;
		if (tick % (60 * (1000000 / poll_us)) == 0) {
			printf("%s: so far %u reports in %u looks (%u bad), %u finger positions, the last at %u, %u; "
			    "interrupt line %s; a report takes %llu us to read; the line looked at every %u us, reports %s a second\n",
			    chip_name(), seen.reports, seen.polls, seen.bad, seen.touches, seen.last_x, seen.last_y,
			    gpio_get(PIN_INT) ? "high" : "low", reads ? reading / reads : 0, poll_us, report_rate);
		}
		sleep_us(poll_us);
	}
}

/* ---- ST FTS probe for thyme (Mi 10S): pins, reset, and one I2C transaction ---------------------- */

#define FTS_PIN_SDA             36
#define FTS_PIN_SCL             37
#define FTS_PIN_RESET           38
#define FTS_PIN_INT             39
#define FTS_ADDRESS             0x49

static void
fts_high(unsigned pin)          /* the pin stops driving the line; the pull-up takes it high */
{
	gpio_configure(pin, 0, PULL_UP, 2, false);
}

static void
fts_low(unsigned pin)           /* the pin drives the line low */
{
	gpio_set(pin, false);
	gpio_configure(pin, 0, PULL_UP, 2, true);
}

static bool
fts_read(unsigned pin)
{
	return gpio_get(pin);
}

static void
fts_bit_delay(void)
{
	sleep_us(5);
}

static void
fts_start(void)
{
	fts_high(FTS_PIN_SDA);
	fts_high(FTS_PIN_SCL);
	fts_bit_delay();
	fts_low(FTS_PIN_SDA);
	fts_bit_delay();
	fts_low(FTS_PIN_SCL);
	fts_bit_delay();
}

static void
fts_stop(void)
{
	fts_low(FTS_PIN_SDA);
	fts_low(FTS_PIN_SCL);
	fts_bit_delay();
	fts_high(FTS_PIN_SCL);
	fts_bit_delay();
	fts_high(FTS_PIN_SDA);
	fts_bit_delay();
}

static bool
fts_write_byte(uint8_t byte)
{
	bool acked;

	for (int bit = 7; bit >= 0; bit--) {
		if (byte & (1u << bit)) {
			fts_high(FTS_PIN_SDA);
		} else {
			fts_low(FTS_PIN_SDA);
		}
		fts_bit_delay();
		fts_high(FTS_PIN_SCL);
		fts_bit_delay();
		fts_low(FTS_PIN_SCL);
		fts_bit_delay();
	}
	fts_high(FTS_PIN_SDA);          /* let go, so the chip can acknowledge */
	fts_bit_delay();
	fts_high(FTS_PIN_SCL);
	fts_bit_delay();
	acked = !fts_read(FTS_PIN_SDA);
	fts_low(FTS_PIN_SCL);
	fts_bit_delay();
	return acked;
}

static uint8_t
fts_read_byte(bool ack)
{
	uint8_t byte = 0;

	fts_high(FTS_PIN_SDA);
	for (int bit = 7; bit >= 0; bit--) {
		fts_bit_delay();
		fts_high(FTS_PIN_SCL);
		fts_bit_delay();
		if (fts_read(FTS_PIN_SDA)) {
			byte |= 1u << bit;
		}
		fts_low(FTS_PIN_SCL);
	}
	if (ack) {
		fts_low(FTS_PIN_SDA);
	} else {
		fts_high(FTS_PIN_SDA);
	}
	fts_bit_delay();
	fts_high(FTS_PIN_SCL);
	fts_bit_delay();
	fts_low(FTS_PIN_SCL);
	fts_bit_delay();
	fts_high(FTS_PIN_SDA);
	return byte;
}


extern void loader_log_putc(char c);

static void
fts_say(const char *format, ...) __printflike(1, 2);

static void
fts_say(const char *format, ...)
{
	char line[256];
	va_list list;
	const char *c;

	va_start(list, format);
	vsnprintf(line, sizeof(line), format, list);
	va_end(list);
	for (c = line; *c != 0; c++) {
		loader_log_putc(*c);
	}
	printf("%s", line);
}


/* ---- staged probe: ftsstage=N makes the probe panic at stage N, log and all ---- */

static uint32_t fts_stage_request;
static bool fts_stage_read;

#include <kern/debug.h>   /* panic() is a macro there */

static void
fts_stage(uint32_t stage, const char *what)
{
	if (!fts_stage_read) {
		fts_stage_read = true;
		PE_parse_boot_argn("ftsstage", &fts_stage_request, sizeof(fts_stage_request));
	}
	fts_say("fts: stage %u (%s)\n", stage, what);
	if (fts_stage_request != 0 && stage >= fts_stage_request) {
		panic("fts probe stopped at stage %u (%s)", stage, what);
	}
}

/*
 * Pin bring-up with the numbers the vendor's tables gave (see the comment on bringup_test).
 */
/* Every RPMh resource the loader left in the command database, "ldo" ones first. */
static void
cmd_db_dump(void)
{
	const struct cmd_db_header *db = (const struct cmd_db_header *)cmd_db;
	unsigned shown = 0, total = 0;

	fts_say("cmd_db: magic %02x %02x %02x %02x version %#x\n",
	    db->magic[0], db->magic[1], db->magic[2], db->magic[3], db->version);
	for (unsigned s = 0; s < 8; s++) {
		const struct cmd_db_slave *sl = &db->slave[s];
		const struct cmd_db_entry *e = (const struct cmd_db_entry *)(db->data + sl->header_offset);

		if (sl->id == 0 || sl->header_offset >= CMD_DB_SIZE - 0x1000 || sl->count > 512) {
			continue;
		}
		for (unsigned i = 0; i < sl->count; i++, total++) {
			if (e[i].id[0] == 'l' && e[i].id[1] == 'd' && e[i].id[2] == 'o') {
				fts_say("cmd_db: %.8s = %#x\n", e[i].id, e[i].address);
				shown++;
			} else if (total < 12) {
				fts_say("cmd_db: %.8s = %#x\n", e[i].id, e[i].address);
			}
		}
	}
	fts_say("cmd_db: %u ldo entries shown, %u entries total\n", shown, total);
}

/* ---- open-drain I2C by hand, waiting for the slave to release SCL ---- */

static uint32_t bb_us = 5;      /* boot-arg bbus=<microseconds>: the quarter period */

static void
bb_delay(void)
{
	sleep_us(bb_us);
}

static void
bb_sda_release(void)
{
	gpio_configure(FTS_PIN_SDA, 0, PULL_UP, 2, false);
}

static void
bb_sda_low(void)
{
	gpio_set(FTS_PIN_SDA, false);
	gpio_configure(FTS_PIN_SDA, 0, PULL_UP, 2, true);
}

static void
bb_scl_release(void)
{
	gpio_configure(FTS_PIN_SCL, 0, PULL_UP, 2, false);
	for (int i = 0; i < 1000 && !gpio_get(FTS_PIN_SCL); i++) {      /* clock stretching */
		sleep_us(1);
	}
}

static void
bb_scl_low(void)
{
	gpio_set(FTS_PIN_SCL, false);
	gpio_configure(FTS_PIN_SCL, 0, PULL_UP, 2, true);
}

static void
bb_start(void)
{
	bb_sda_release();
	bb_scl_release();
	bb_delay();
	bb_sda_low();
	bb_delay();
	bb_scl_low();
	bb_delay();
}

static void
bb_stop(void)
{
	bb_sda_low();
	bb_scl_low();
	bb_delay();
	bb_scl_release();
	bb_delay();
	bb_sda_release();
	bb_delay();
}

static bool
bb_write(uint8_t byte)
{
	bool acked;

	for (int bit = 7; bit >= 0; bit--) {
		if (byte & (1u << bit)) {
			bb_sda_release();
		} else {
			bb_sda_low();
		}
		bb_delay();
		bb_scl_release();
		bb_delay();
		bb_scl_low();
		bb_delay();
	}
	bb_sda_release();
	bb_delay();
	bb_scl_release();
	bb_delay();
	acked = !gpio_get(FTS_PIN_SDA);
	bb_scl_low();
	bb_delay();
	return acked;
}

static uint8_t
bb_read(bool ack)
{
	uint8_t byte = 0;

	bb_sda_release();
	for (int bit = 7; bit >= 0; bit--) {
		bb_delay();
		bb_scl_release();
		bb_delay();
		if (gpio_get(FTS_PIN_SDA)) {
			byte |= 1u << bit;
		}
		bb_scl_low();
		bb_delay();
	}
	if (ack) {
		bb_sda_low();
	} else {
		bb_sda_release();
	}
	bb_delay();
	bb_scl_release();
	bb_delay();
	bb_scl_low();
	bb_delay();
	bb_sda_release();
	return byte;
}

/* ---- the ST command protocol, by hand --------------------------------------------------------- */

/* write [cmd][addr hi][addr lo] and read len+1 bytes; the first byte read is a dummy */
/* write a byte sequence exactly as given */
static bool
st_cmd_write_raw(const uint8_t *bytes, unsigned len)
{
	bool ok;

	bb_start();
	ok = bb_write(0x49 << 1);
	for (unsigned i = 0; i < len && ok; i++) {
		ok = bb_write(bytes[i]);
	}
	bb_stop();
	return ok;
}

static bool st_hw_read(uint32_t address, uint8_t *data, unsigned len);

static void
ier_trace(const char *when)
{
	uint8_t v[4] = { 0 };

	if (st_hw_read(0x20000029, v, sizeof(v))) {
		fts_say("ier_trace: %s -> %02x %02x %02x %02x\n", when, v[0], v[1], v[2], v[3]);
	}
}

/* read hardware registers: [0xFA][addr32] then len bytes */
static bool
st_hw_read(uint32_t address, uint8_t *data, unsigned len)
{
	bool ok;

	bb_start();
	ok = bb_write(0x49 << 1) && bb_write(0xFA) &&
	    bb_write((uint8_t)(address >> 24)) && bb_write((uint8_t)(address >> 16)) &&
	    bb_write((uint8_t)(address >> 8)) && bb_write((uint8_t)address);
	if (ok) {
		bb_start();
		ok = bb_write((0x49 << 1) | 1);
	}
	if (!ok) {
		bb_stop();
		return false;
	}
	for (unsigned i = 0; i < len; i++) {
		data[i] = bb_read(i != len - 1);
	}
	bb_stop();
	return true;
}

/* write [cmd][addr hi][addr lo] then read len bytes exactly as the chip sends them */
static bool
st_cmd_read_raw_addr(uint8_t cmd, uint16_t address, uint8_t *data, unsigned len)
{
	bool ok;

	bb_start();
	ok = bb_write(0x49 << 1) && bb_write(cmd) &&
	    bb_write((uint8_t)(address >> 8)) && bb_write((uint8_t)(address & 0xff));
	if (ok) {
		bb_start();
		ok = bb_write((0x49 << 1) | 1);
	}
	if (!ok) {
		bb_stop();
		return false;
	}
	for (unsigned i = 0; i < len; i++) {
		data[i] = bb_read(i != len - 1);
	}
	bb_stop();
	return true;
}

/* write just the command byte and read len bytes exactly as the chip sends them */
static bool
st_cmd_read_raw(uint8_t cmd, uint8_t *data, unsigned len)
{
	bool ok;

	bb_start();
	ok = bb_write(0x49 << 1) && bb_write(cmd);
	if (ok) {
		bb_start();
		ok = bb_write((0x49 << 1) | 1);
	}
	if (!ok) {
		bb_stop();
		return false;
	}
	for (unsigned i = 0; i < len; i++) {
		data[i] = bb_read(i != len - 1);
	}
	bb_stop();
	return true;
}

/* write [cmd][addr hi][addr lo][data...] */
static bool
st_cmd_write(uint8_t cmd, bool with_address, uint16_t address, const uint8_t *data, unsigned len)
{
	bool ok;

	bb_start();
	ok = bb_write(0x49 << 1) && bb_write(cmd);
	if (ok && with_address) {
		ok = bb_write((uint8_t)(address >> 8)) && bb_write((uint8_t)(address & 0xff));
	}
	for (unsigned i = 0; i < len && ok; i++) {
		ok = bb_write(data[i]);
	}
	bb_stop();
	return ok;
}

static bool
st_cmd_read(uint8_t cmd, bool with_address, uint16_t address, uint8_t *data, unsigned len)
{
	bool ok;

	bb_start();
	ok = bb_write(0x49 << 1) && bb_write(cmd);
	if (ok && with_address) {
		ok = bb_write((uint8_t)(address >> 8)) && bb_write((uint8_t)(address & 0xff));
	}
	if (ok) {
		bb_start();
		ok = bb_write((0x49 << 1) | 1);
	}
	if (!ok) {
		bb_stop();
		return false;
	}
	(void)bb_read(true);                    /* the dummy byte */
	for (unsigned i = 0; i < len; i++) {
		data[i] = bb_read(i != len - 1);
	}
	bb_stop();
	return true;
}

/* ---- keep the hypervisor's watchdog fed ------------------------------------------------------ */

#define HV_WDT_PET 0x86000007ULL

static void
hv_wdt_pet_once(void)
{
	register uint64_t x0 __asm__("x0") = HV_WDT_PET;
	register uint64_t x1 __asm__("x1") = 0;
	register uint64_t x2 __asm__("x2") = 0;
	register uint64_t x3 __asm__("x3") = 0;

	__asm__ volatile ("smc #0" : "+r"(x0), "+r"(x1), "+r"(x2), "+r"(x3)
	    : : "x4", "x5", "x6", "x7", "x8", "x9", "x10", "x11", "x12", "x13", "x14", "x15", "x16", "x17");
}


static void
bringup_test(uint32_t mode)
{
	uint64_t c;

#ifndef REDWOOD_FTS_VENDOR_LIBRARY
	if (mode == 29 || mode == 30 || mode == 31 || mode == 33 || mode == 36) {
		fts_say("bringup: mode %u needs the unavailable legacy FTS vendor library\n", mode);
		return;
	}
#endif

	tlmm = ml_io_map(0x0f500000UL, 0x100000UL);            /* gpio block: 0x500000 into the DT window */
	gcc = ml_io_map(GCC_PHYS, GCC_SIZE);
	se = ml_io_map(SE_PHYS, SE_SIZE);
	rsc = ml_io_map(RSC_PHYS, RSC_SIZE);
	cmd_db = ml_io_map_wcomb(CMD_DB_PHYS, CMD_DB_SIZE);
	c = tlmm;
	fts_say("bringup: tlmm %#llx gcc %#llx se %#llx rsc %#llx cmd_db %#llx\n",
	    (unsigned long long)c, (unsigned long long)gcc, (unsigned long long)se,
	    (unsigned long long)rsc, (unsigned long long)cmd_db);

	/* the I/O rail's enable first: plain gpio, high */
	gpio_output(69, true, PULL_NONE, 2);
	fts_say("bringup: gpio69 cfg %#x io %#x\n", rd(GPIO_CFG(69)), rd(GPIO_IO(69)));

	/* SDA/SCL to qup13: position 1 of the pin's function list, field [5:2]; the device tree asks
	 * for 2 mA and no pull */
	wr(GPIO_CFG(FTS_PIN_SDA), 1u << 2);
	wr(GPIO_CFG(FTS_PIN_SCL), 1u << 2);

	/* the interrupt line as an input with a pull-up; the reset line as an output, high */
	gpio_configure(FTS_PIN_INT, 0, PULL_UP, 2, false);
	gpio_output(FTS_PIN_RESET, true, PULL_UP, 8);

	fts_say("bringup: cfg36 %#x cfg37 %#x cfg38 %#x cfg39 %#x cfg69 %#x\n",
	    rd(GPIO_CFG(FTS_PIN_SDA)), rd(GPIO_CFG(FTS_PIN_SCL)), rd(GPIO_CFG(FTS_PIN_RESET)),
	    rd(GPIO_CFG(FTS_PIN_INT)), rd(GPIO_CFG(69)));

	if (mode < 4) {
		if (mode != 0) {
			panic("bringup: pins configured, rails untouched");
		}
		return;   /* mode 0: the bring-up preamble only, for the panel driver to use */
	}

	/* VDD 1.8 V: RPMh regulator ldoa1 (pm8150a_l1 in the device tree) */
	if (mode == 5) {
		static const char *names[] = { "ldoa1", "ldoc1", "ldoc2", "ldob1", "ldoa2", "ldoc3" };

		cmd_db_dump();
		for (unsigned i = 0; i < sizeof(names) / sizeof(names[0]); i++) {
			fts_say("bringup: lookup %.8s -> %#x\n", names[i], rpmh_address(names[i]));
		}
		panic("bringup: command database dumped");
	}
	if (mode != 33) {
		/* the device tree's touch vdd-supply: rpmh-regulator-ldoc1 */
		const char *name = (mode >= 6) ? "ldoc1" : "ldoa1";
		uint32_t base = rpmh_address(name);

		fts_say("bringup: %s address %#x\n", name, base);
		if (base != 0 && ((base >> 16) & 0xf) == 4) {
			uint32_t address[3] = { base + VRM_VOLTAGE, base + VRM_ENABLE, base + VRM_MODE };
			uint32_t data[3] = { 1800, 1, VRM_MODE_LDO_HPM };

			fts_say("bringup: %s 1.8 V: %s\n", name, rpmh_write(address, data, 3) ? "on" : "FAILED");
		}
	}

	/* reset: low for 15 ms, high, then 150 ms for the chip's firmware to come up
	 * (bringup=14 skips it, to see what the chip does untouched; 33 keeps Android's state) */
#ifdef REDWOOD_FTS_VENDOR_LIBRARY
	if (mode == 33) {
		fts_say("warm: leaving rails and reset exactly as the boot chain left them\n");
	} else if (mode == 15) {
		/* the device tree calls the reset gpio active high: low is "running" */
		gpio_output(FTS_PIN_RESET, false, PULL_UP, 8);
		sleep_ms(200);
		fts_say("bringup: reset low (active high), cfg38 %#x io38 %#x\n",
		    rd(GPIO_CFG(FTS_PIN_RESET)), rd(GPIO_IO(FTS_PIN_RESET)));
	} else if (mode != 14) {
		gpio_output(FTS_PIN_RESET, false, PULL_UP, 8);
		sleep_ms(15);
		gpio_output(FTS_PIN_RESET, true, PULL_UP, 8);
		sleep_ms(150);
	} else {
		fts_say("bringup: reset left alone (mode 14)\n");
	}

	fts_say("bringup: after reset cfg38 %#x io38 %#x, sda %u scl %u int %u\n",
	    rd(GPIO_CFG(FTS_PIN_RESET)), rd(GPIO_IO(FTS_PIN_RESET)),
	    gpio_get(FTS_PIN_SDA), gpio_get(FTS_PIN_SCL), gpio_get(FTS_PIN_INT));
	if (mode < 7) {
		panic("bringup: rails on and reset pulsed");
	}

	if (mode == 13) {
		fts_say("pin_release_test: start sda %u scl %u\n", gpio_get(FTS_PIN_SDA), gpio_get(FTS_PIN_SCL));
		bb_sda_low();
		fts_say("pin_release_test: sda driven low -> %u\n", gpio_get(FTS_PIN_SDA));
		bb_sda_release();
		fts_say("pin_release_test: sda released -> %u (1 = the pull-up wins)\n", gpio_get(FTS_PIN_SDA));
		bb_scl_low();
		fts_say("pin_release_test: scl driven low -> %u\n", gpio_get(FTS_PIN_SCL));
		bb_scl_release();
		fts_say("pin_release_test: scl released -> %u\n", gpio_get(FTS_PIN_SCL));

		bb_start();
		(void)bb_write(0x49 << 1);
		fts_say("pin_release_test: after address: sda %u scl %u\n", gpio_get(FTS_PIN_SDA), gpio_get(FTS_PIN_SCL));
		bb_stop();
		sleep_ms(1);
		fts_say("pin_release_test: after stop: sda %u scl %u\n", gpio_get(FTS_PIN_SDA), gpio_get(FTS_PIN_SCL));
		panic("pin_release_test: done");
	}

	{
		uint32_t us = 0;

		if (PE_parse_boot_argn("bbus", &us, sizeof(us)) && us >= 1 && us <= 500) {
			bb_us = us;
		}
		fts_say("bringup: bit delay %u us\n", bb_us);
	}

	if (mode == 33) {
		extern int fts_port_warm_frame(void);

		fts_say("warm: reading a frame with the chip untouched\n");
		(void)fts_port_warm_frame();
		panic("warm: done");
	}
#endif

	if (mode == 32) {
		static uint8_t regs[0x180];

		for (unsigned r = 0; r < 6; r++) {
			uint32_t base = 0x20000200 + r * 0x40;

			if (st_hw_read(base, regs, 0x40)) {
				for (unsigned line = 0; line < 4; line++) {
					fts_say("hwregs200: %#x: %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x\n",
					    base + line * 16,
					    regs[line*16+0], regs[line*16+1], regs[line*16+2], regs[line*16+3],
					    regs[line*16+4], regs[line*16+5], regs[line*16+6], regs[line*16+7],
					    regs[line*16+8], regs[line*16+9], regs[line*16+10], regs[line*16+11],
					    regs[line*16+12], regs[line*16+13], regs[line*16+14], regs[line*16+15]);
				}
			}
		}
		panic("hwregs200: done");
	}

	if (mode == 40) {
		extern void redwood_st_touch_init(void);
		uint64_t deadline;
		unsigned waited = 0;

		fts_say("touch40: starting the ported driver\n");
		redwood_st_touch_init();

		/* yield, so the driver thread runs */
		for (unsigned i = 0; i < 55; i++) {
			clock_interval_to_deadline(1000, NSEC_PER_MSEC, &deadline);
			(void)assert_wait_deadline(&waited, THREAD_UNINT, deadline);
			thread_block(THREAD_CONTINUE_NULL);
		}
		fts_say("touch40: done waiting\n");
		panic("touch40: done");
	}

	if (mode == 39) {
		static const uint8_t sense_on[1] = { 0x93 };
		static const uint8_t sense_off[1] = { 0x92 };
		static uint8_t nodes[2048];
		unsigned frames = 0;

		gpio_output(FTS_PIN_RESET, false, PULL_UP, 8);
		sleep_ms(15);
		gpio_output(FTS_PIN_RESET, true, PULL_UP, 8);
		sleep_ms(150);

		for (unsigned pass = 0; pass < 6; pass++) {
			uint8_t off_buf[3] = { 0 };
			uint8_t hdr[9] = { 0 };
			unsigned offset = 0;
			int force = 17;
			int sense = 37;

			/* the panel's own scan cycle */
			(void)st_cmd_write_raw(sense_on, 1);
			sleep_ms(200);
			(void)st_cmd_write_raw(sense_off, 1);
			sleep_ms(50);

			/* drain the FIFO, as the driver does before reading the frame */
			{
				uint8_t junk[9] = { 0 };

				for (unsigned i = 0; i < 8; i++) {
					(void)st_cmd_read_raw(0x86, junk, sizeof(junk));
				}
			}

			/* where the chip says the NORM_TOUCH frame is */
			if (st_cmd_read_raw_addr(0xD0, 0x0004, off_buf, sizeof(off_buf))) {
				offset = off_buf[1] | (off_buf[2] << 8);
			}
			fts_say("thyme39: %d offset bytes %02x %02x %02x -> %#x  (channels %dx%d)\n",
			    pass, off_buf[0], off_buf[1], off_buf[2], offset, force, sense);

			if (st_cmd_read_raw_addr(0xD0, 0x8000, hdr, sizeof(hdr))) {
				fts_say("thyme39: %d header %02x %02x %02x %02x %02x %02x %02x %02x\n",
				    pass, hdr[0], hdr[1], hdr[2], hdr[3], hdr[4],
				    hdr[5], hdr[6], hdr[7]);
			}

			if (offset != 0 && force > 0 && sense > 0) {
				unsigned len = (unsigned)(force * sense * 2);
				unsigned start = offset + (unsigned)(sense * 2);

				if (len + 1 <= sizeof(nodes) &&
				    st_cmd_read_raw_addr(0xD0, (uint16_t)start, nodes, len + 1)) {
					int sum = 0, first = 0;

					for (unsigned i = 0; i < len; i += 2) {
						int v = (int16_t)(nodes[i + 1] | (nodes[i + 2] << 8));

						sum += v;
						if (i == 0) {
							first = v;
						}
					}
					fts_say("thyme39: %d frame at %#x  sum %d  first %d\n",
					    pass, start, sum, first);
					frames++;
				}
			}
			sleep_ms(80);
		}
		fts_say("thyme39: %u frames\n", frames);
		panic("thyme39: done");
	}

	if (mode == 38) {
		static const uint8_t requ[3] = { 0xB7, 0x00, 0x02 };   /* MS_TOUCH_ACTIVE */
		static uint8_t nodes[2048];
		unsigned frames = 0;

		/* reset, scan on */
		gpio_output(FTS_PIN_RESET, false, PULL_UP, 8);
		sleep_ms(15);
		gpio_output(FTS_PIN_RESET, true, PULL_UP, 8);
		sleep_ms(150);
		/* the thyme driver starts sensing with the single byte command, not the scan mode write */
		{
			static const uint8_t sense_on[1] = { 0x93 };
			static const uint8_t sense_off[1] = { 0x92 };

			fts_say("thyme38: sense on %s\n",
			    st_cmd_write_raw(sense_on, 1) ? "ok" : "NAK");
			sleep_ms(200);
			fts_say("thyme38: sense off %s\n",
			    st_cmd_write_raw(sense_off, 1) ? "ok" : "NAK");
			sleep_ms(50);
		}

		for (unsigned pass = 0; pass < 8; pass++) {
			uint8_t hdr[9] = { 0 };
			unsigned got = 0;

			/* ask the chip to load a fresh frame into its buffer */
			fts_say("thyme38: request %s\n",
			    st_cmd_write_raw(requ, sizeof(requ)) ? "ok" : "NAK");
			for (unsigned i = 0; i < 200; i++) {
				uint8_t ev[9] = { 0 };

				if (st_cmd_read_raw(0x86, ev, sizeof(ev)) && ev[0] == 0x25) {
					got = 1;
					break;
				}
				sleep_ms(1);
			}
			fts_say("thyme38: frame ready event %s\n", got ? "seen" : "TIMEOUT");

			if (!st_cmd_read_raw_addr(0xD0, 0x8000, hdr, sizeof(hdr))) {
				fts_say("thyme38: header NAK\n");
				continue;
			}
			fts_say("thyme38: %d hdr %02x %02x %02x %02x %02x %02x %02x %02x\n",
			    pass, hdr[0], hdr[1], hdr[2], hdr[3], hdr[4], hdr[5], hdr[6], hdr[7]);

			if (hdr[1] == 0xB5) {
				int force = hdr[5];
				int sense = hdr[6];
				unsigned len = (unsigned)(force * sense * 2);

				if (len > 0 && len + 1 <= sizeof(nodes) &&
				    st_cmd_read_raw_addr(0xD0, 0x8008, nodes, len + 1)) {
					int sum = 0, first = 0;

					for (unsigned i = 0; i < len; i += 2) {
						int v = (int16_t)(nodes[i + 1] | (nodes[i + 2] << 8));

						sum += v;
						if (i == 0) {
							first = v;
						}
					}
					fts_say("thyme38: %d  %dx%d  sum %d  first %d\n",
					    pass, force, sense, sum, first);
					frames++;
				}
			}
			sleep_ms(100);
		}
		fts_say("thyme38: %u frames decoded\n", frames);
		panic("thyme38: done");
	}

	if (mode == 37) {
		static const uint8_t scan_on[3] = { 0xA0, 0x00, 0x01 };
		unsigned seen = 0;

		/* reset, then start scanning, exactly as this phone's driver does */
		gpio_output(FTS_PIN_RESET, false, PULL_UP, 8);
		sleep_ms(15);
		gpio_output(FTS_PIN_RESET, true, PULL_UP, 8);
		sleep_ms(150);
		fts_say("thyme frame: scan on %s\n",
		    st_cmd_write_raw(scan_on, sizeof(scan_on)) ? "ok" : "NAK");
		sleep_ms(200);

		for (unsigned pass = 0; pass < 10; pass++) {
			uint8_t hdr[9] = { 0 };

			if (!st_cmd_read_raw_addr(0xD0, 0x8000, hdr, sizeof(hdr))) {
				fts_say("thyme frame: header read NAK\n");
				sleep_ms(150);
				continue;
			}
			fts_say("thyme frame: %d hdr %02x %02x %02x %02x %02x %02x | %02x %02x\n",
			    pass, hdr[0], hdr[1], hdr[2], hdr[3], hdr[4], hdr[5], hdr[6], hdr[7]);

			if (hdr[1] == 0xB5) {
				int force = hdr[5];
				int sense = hdr[6];
				unsigned len = (unsigned)(force * sense * 2);
				static uint8_t nodes[2048];
				int sum = 0, first = 0;

				if (len > 0 && len + 1 <= sizeof(nodes)) {
					if (st_cmd_read_raw_addr(0xD0, 0x8008, nodes, len + 1)) {
						for (unsigned i = 0; i < len; i += 2) {
							int v = (int16_t)(nodes[i + 1] | (nodes[i + 2] << 8));

							sum += v;
							if (i == 0) {
								first = v;
							}
						}
						sum = sum;
						fts_say("thyme frame: %d  %dx%d  sum %d  first %d\n",
						    pass, force, sense, sum, first);
						seen++;
					} else {
						fts_say("thyme frame: data read NAK\n");
					}
				}
			}
			sleep_ms(150);
		}
		fts_say("thyme frame: %u frames decoded\n", seen);
		panic("thyme frame: done");
	}

#ifdef REDWOOD_FTS_VENDOR_LIBRARY
	if (mode == 36) {
		/* upstream FTS5 init, then read the raw frame over and over: is it live now? */
		static const uint8_t cmds[4] = { 0x91, 0xa3, 0xa4, 0xa2 };
		static const uint8_t int_en[4] = { 0xB6, 0x00, 0x2C, 0x01 };
		static const uint8_t scan_all[3] = { 0xA0, 0x00, 0xFF };
		extern int fts_port_frame_sum(void);
		int first = 0;

		for (unsigned i = 0; i < 4; i++) {
			(void)st_cmd_write_raw(&cmds[i], 1);
			sleep_ms(i == 3 ? 50 : 20);
		}
		(void)st_cmd_write_raw(int_en, sizeof(int_en));
		sleep_ms(20);
		(void)st_cmd_write_raw(scan_all, sizeof(scan_all));
		sleep_ms(50);

		for (unsigned i = 0; i < 40; i++) {
			uint8_t junk[9] = { 0 };
			int sum = fts_port_frame_sum();

			if (i == 0) {
				first = sum;
			}
			if (sum != first || (i % 10) == 0) {
				fts_say("frame live: %u sum %d (first %d) int %u\n",
				    i, sum, first, gpio_get(FTS_PIN_INT));
			}
			for (unsigned f = 0; f < 4; f++) {
				(void)st_cmd_read_raw(0x86, junk, sizeof(junk));
			}
			sleep_ms(120);
		}
		fts_say("frame live: done, first %d\n", first);
		panic("frame live: done");
	}
#endif

	if (mode == 35) {
		/* upstream Linux STM FTS5 bring-up */
		static const uint8_t cmds[4] = { 0x91, 0xa3, 0xa4, 0xa2 };
		static const uint8_t int_en[4] = { 0xB6, 0x00, 0x2C, 0x01 };
		uint8_t scan_all[3] = { 0xA0, 0x00, 0xFF };
		{
			uint32_t sc = 0;

			if (PE_parse_boot_argn("ftsscan", &sc, sizeof(sc)) && sc <= 0xff) {
				scan_all[2] = (uint8_t)sc;
			}
		}
		unsigned events = 0;

		fts_say("upstream FTS5: starting the mainline sequence\n");

		for (unsigned i = 0; i < 4; i++) {
			bool ok = st_cmd_write_raw(&cmds[i], 1);

			fts_say("upstream FTS5: cmd %02x %s\n", cmds[i], ok ? "ok" : "NAK");
			sleep_ms(i == 3 ? 50 : 20);
		}

		fts_say("upstream FTS5: interrupts %s\n",
		    st_cmd_write_raw(int_en, sizeof(int_en)) ? "enabled" : "NAK");
		sleep_ms(20);

		fts_say("upstream FTS5: scan mode %02x %s\n", scan_all[2],
		    st_cmd_write_raw(scan_all, sizeof(scan_all)) ? "set" : "NAK");
		sleep_ms(50);

		for (unsigned i = 0; i < 12000; i++) {
			uint8_t r[9] = { 0 };

			if (st_cmd_read_raw(0x86, r, sizeof(r)) && r[0] != 0x00 && r[0] != 0xFF) {
				if (r[0] != 0x43) {   /* anything that is not a status update */
					fts_say("upstream FTS5: EVENT %02x %02x %02x %02x %02x %02x %02x %02x  int %u\n",
					    r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7],
					    gpio_get(FTS_PIN_INT));
				}
				events++;
			}
#ifdef REDWOOD_FTS_VENDOR_LIBRARY
			if ((i % 3000) == 2999) {
				extern int fts_port_frame_sum(void);
				fts_say("upstream FTS5: %u events so far, frame sum %d, int %u\n",
				    events, fts_port_frame_sum(), gpio_get(FTS_PIN_INT));
			}
#endif
			sleep_ms(2);
		}
		fts_say("upstream FTS5: %u events total\n", events);
		panic("upstream FTS5: done");
	}

	if (mode == 34) {
		static const uint8_t hw_pre[7] = { 0xFA, 0x20, 0x00, 0x00, 0x00, 0x00, 0x00 };
		static const uint8_t scan_on[3] = { 0xA0, 0x00, 0x01 };
		unsigned ready = 0, seen = 0;

		for (unsigned i = 0; i < 300; i++) {
			uint8_t r[9] = { 0 };

			if (st_cmd_read_raw(0x86, r, sizeof(r)) && r[0] == 0x03) {
				ready++;
				break;
			}
			sleep_ms(10);
		}
		fts_say("every event: ready %u\n", ready);

		(void)st_cmd_write_raw(hw_pre, sizeof(hw_pre));
		(void)st_cmd_write_raw(scan_on, sizeof(scan_on));
		sleep_ms(20);

		for (unsigned i = 0; i < 3000; i++) {
			uint8_t r[9] = { 0 };

			if (st_cmd_read_raw(0x86, r, sizeof(r))) {
				if (r[0] != 0x00 && r[0] != 0xFF) {
					fts_say("every event: %02x %02x %02x %02x %02x %02x %02x %02x %02x  int %u\n",
					    r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8],
					    gpio_get(FTS_PIN_INT));
					seen++;
				}
			}
			if ((i % 500) == 0) {
				fts_say("every event: %u iterations, int %u\n", i, gpio_get(FTS_PIN_INT));
			}
			sleep_ms(2);
		}
		fts_say("every event: %u events total\n", seen);
		panic("every event: done");
	}

#ifdef REDWOOD_FTS_VENDOR_LIBRARY
	if (mode == 31) {
		extern int fts_port_selftest(void);
		uint32_t base = rpmh_address("ldoc1");
		uint32_t address[3], on[3] = { 1800, 1, VRM_MODE_LDO_HPM };
		uint32_t off[3] = { 1800, 0, VRM_MODE_LDO_HPM };

		address[0] = base + VRM_VOLTAGE;
		address[1] = base + VRM_ENABLE;
		address[2] = base + VRM_MODE;

		/* down: AVDD first, then VDD, reset asserted while both are off */
		gpio_output(69, false, PULL_NONE, 2);
		if (base != 0 && ((base >> 16) & 0xf) == 4) {
			fts_say("order: vdd off %s\n", rpmh_write(address, off, 3) ? "ok" : "FAILED");
		}
		sleep_ms(50);
		gpio_output(FTS_PIN_RESET, false, PULL_UP, 8);
		sleep_ms(20);

		/* up in the driver's order: VDD, 20 ms, AVDD, 20 ms, then release reset */
		if (base != 0 && ((base >> 16) & 0xf) == 4) {
			fts_say("order: vdd on %s\n", rpmh_write(address, on, 3) ? "ok" : "FAILED");
		}
		sleep_ms(20);
		gpio_output(69, true, PULL_NONE, 2);
		fts_say("order: avdd on\n");
		sleep_ms(20);
		gpio_output(FTS_PIN_RESET, true, PULL_UP, 8);
		sleep_ms(150);
		fts_say("order: reset released, running the ported driver\n");

		(void)fts_port_selftest();
		panic("order: done");
	}

	if (mode == 30) {
		/* bring the ported panel driver up and let it poll for a few seconds */
		extern int fts_touch_init(void);

		fts_say("touch_test: starting the ST panel driver\n");
		(void)fts_touch_init();
		fts_say("touch_test: polling for 8 seconds\n");
		sleep_ms(8000);
		{
			extern uint32_t fts_touch_poll_count(void);
			fts_say("touch_test: poll cycles %u\n", fts_touch_poll_count());
		}
		fts_say("touch_test: done\n");
		panic("touch_test: done");
	}

	if (mode == 29) {
		/* let the ported vendor driver run its own init and read a frame */
		extern int fts_port_selftest(void);

		fts_say("port_test: calling the ported driver\n");
		fts_port_selftest();
		fts_say("port_test: returned\n");
		panic("port_test: done");
	}
#endif

	if (mode == 28) {
		static const uint8_t hw_pre[7] = { 0xFA, 0x20, 0x00, 0x00, 0x00, 0x00, 0x00 };
		static const uint8_t scan_on[3] = { 0xA0, 0x00, 0x01 };
		static const uint8_t scan_off[3] = { 0xA0, 0x00, 0x00 };
		static const uint8_t gesture[6] = { 0xA2, 0x03, 0x00, 0x00, 0x00, 0x00 };
		static uint8_t frame[1280];
		uint8_t info[160] = { 0 };
		uint16_t raw_addr = 0;
		unsigned ready = 0;

		{
			/* SYSTEM_RESET_VALUE 0x81 written to ADDR_SYSTEM_RESET: let the firmware restart
			 * itself, which may do more than pulling the reset line */
			static const uint8_t sw_reset[6] = { 0xFA, 0x20, 0x00, 0x00, 0x24, 0x81 };
			int ok = st_cmd_write_raw(sw_reset, sizeof(sw_reset));

			fts_say("capture_frames: software reset %s\n", ok ? "sent" : "NAK");
			sleep_ms(150);
		}

		for (unsigned i = 0; i < 300; i++) {
			uint8_t r[9] = { 0 };

			if (st_cmd_read_raw(0x86, r, sizeof(r)) && r[0] == 0x03) {
				ready++;
				break;
			}
			sleep_ms(10);
		}
		fts_say("capture_frames: ready %u\n", ready);
		sleep_ms(1500);            /* let the analog side settle before the first scan */
		sleep_ms(1500);

		if (!st_cmd_read_raw_addr(0xA6, 0, info, sizeof(info))) {
			panic("capture_frames: sysinfo NAK");
		}
		raw_addr = (uint16_t)(info[136] | ((uint16_t)info[137] << 8));
		fts_say("capture_frames: raw address %#x\n", raw_addr);

		{
			uint8_t regs[128] = { 0 };

			if (st_hw_read(0x20000080, regs, sizeof(regs))) {
				for (unsigned line = 0; line < 8; line++) {
					fts_say("hwregs80: %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x\n",
					    regs[line*16+0], regs[line*16+1], regs[line*16+2], regs[line*16+3],
					    regs[line*16+4], regs[line*16+5], regs[line*16+6], regs[line*16+7],
					    regs[line*16+8], regs[line*16+9], regs[line*16+10], regs[line*16+11],
					    regs[line*16+12], regs[line*16+13], regs[line*16+14], regs[line*16+15]);
				}
			}
			if (st_hw_read(0x20000100, regs, sizeof(regs))) {
				for (unsigned line = 0; line < 4; line++) {
					fts_say("hwregs100: %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x\n",
					    regs[line*16+0], regs[line*16+1], regs[line*16+2], regs[line*16+3],
					    regs[line*16+4], regs[line*16+5], regs[line*16+6], regs[line*16+7],
					    regs[line*16+8], regs[line*16+9], regs[line*16+10], regs[line*16+11],
					    regs[line*16+12], regs[line*16+13], regs[line*16+14], regs[line*16+15]);
				}
			}
			if (st_hw_read(0x20000000, regs, sizeof(regs))) {
				for (unsigned line = 0; line < 8; line++) {
					fts_say("hwregs: %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x\n",
					    regs[line*16+0], regs[line*16+1], regs[line*16+2], regs[line*16+3],
					    regs[line*16+4], regs[line*16+5], regs[line*16+6], regs[line*16+7],
					    regs[line*16+8], regs[line*16+9], regs[line*16+10], regs[line*16+11],
					    regs[line*16+12], regs[line*16+13], regs[line*16+14], regs[line*16+15]);
				}
			}
		}

		(void)st_cmd_write_raw(hw_pre, sizeof(hw_pre));
		(void)st_cmd_write_raw(gesture, sizeof(gesture));

		for (unsigned snap = 0; snap < 20; snap++) {
			uint32_t sum = 0;
			unsigned mn = 255, mx = 0;

				fts_say("capture_frames: int before scan %u\n", gpio_get(FTS_PIN_INT));
			(void)st_cmd_write_raw(scan_on, sizeof(scan_on));
			sleep_ms(200);                          /* WAIT_FOR_FRESH_FRAMES */
			{
				unsigned drained = 0;

				for (unsigned d = 0; d < 24; d++) {
					uint8_t ev[9] = { 0 };

					if (!st_cmd_read_raw(0x86, ev, sizeof(ev))) {
						break;
					}
					if (ev[0] == 0x00 || ev[0] == 0xFF) {
						break;
					}
					drained++;
				}
				fts_say("capture_frames: %u events drained, int %u\n",
				    drained, gpio_get(FTS_PIN_INT));
			}
			(void)st_cmd_write_raw(scan_off, sizeof(scan_off));
			sleep_ms(60);                           /* WAIT_AFTER_SENSEOFF */

			{
				uint8_t junk[9] = { 0 };
				for (unsigned f = 0; f < 4; f++) {   /* flushFIFO */
					(void)st_cmd_read_raw(0x86, junk, sizeof(junk));
				}
			}

			{
				uint8_t small[16] = { 0 };
				uint8_t filt[16] = { 0 };

				if (st_cmd_read_raw_addr(0xA6, raw_addr, small, sizeof(small))) {
					fts_say("short_long: short %u: %02x %02x %02x %02x %02x %02x %02x %02x\n",
					    snap, small[0], small[1], small[2], small[3],
					    small[4], small[5], small[6], small[7]);
				}
				if (st_cmd_read_raw_addr(0xA6, 0x42d8, filt, sizeof(filt))) {
					fts_say("short_long: filter %u: %02x %02x %02x %02x %02x %02x %02x %02x\n",
					    snap, filt[0], filt[1], filt[2], filt[3],
					    filt[4], filt[5], filt[6], filt[7]);
				}
			}

			if (st_cmd_read_raw_addr(0xA6, raw_addr, frame, 1258)) {
				for (unsigned i = 0; i < 1258; i++) {
					sum += frame[i];
					if (frame[i] < mn) {
						mn = frame[i];
					}
					if (frame[i] > mx) {
						mx = frame[i];
					}
				}
				fts_say("capture_frames: %u sum %u min %u max %u head %02x %02x %02x %02x\n",
				    snap, sum, mn, mx, frame[0], frame[1], frame[2], frame[3]);
			} else {
				fts_say("capture_frames: %u NAK\n", snap);
			}
		}
		panic("capture_frames: done");
	}

	if (mode == 27) {
		static const uint8_t sense_on[3] = { 0x00, 0x00, 0x03 };
		bool ok;

		ok = st_cmd_write(0xA2, true, 0x0300, sense_on, sizeof(sense_on));
		fts_say("st_scan_on: sense-on packet %s\n", ok ? "written" : "NAK");
		sleep_ms(100);

		for (unsigned i = 0; i < 12; i++) {
			uint8_t d[10] = { 0 };

			if (st_cmd_read(0x86, false, 0, d, sizeof(d))) {
				fts_say("st_scan_on: event %u: %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x\n",
				    i, d[0], d[1], d[2], d[3], d[4], d[5], d[6], d[7], d[8], d[9]);
			}
			sleep_ms(20);
		}
		{
			uint8_t d[16] = { 0 };

			if (st_cmd_read(0xA6, true, 0, d, sizeof(d))) {
				fts_say("st_scan_on: block again: %02x %02x %02x %02x %02x %02x %02x %02x\n",
				    d[0], d[1], d[2], d[3], d[4], d[5], d[6], d[7]);
			}
		}
		panic("st_scan_on: done");
	}

	if (mode == 26) {
		static const struct { uint8_t cmd; bool addr16; uint16_t addr; } tries[] = {
			{ 0xA8, true, 0x0000 }, { 0xA8, true, 0x0010 }, { 0x01, true, 0x0000 },
			{ 0x01, true, 0x0010 }, { 0xFA, true, 0x0000 }, { 0xA6, true, 0x0100 },
			{ 0xA6, true, 0x1000 }, { 0x86, false, 0x0000 },
		};

		for (unsigned k = 0; k < sizeof(tries) / sizeof(tries[0]); k++) {
			uint8_t d[16] = { 0 };

			if (st_cmd_read(tries[k].cmd, tries[k].addr16, tries[k].addr, d, sizeof(d))) {
				fts_say("st_cmds2: cmd %02x @%#06x:", tries[k].cmd, tries[k].addr);
				for (unsigned i = 0; i < sizeof(d); i++) {
					fts_say(" %02x", d[i]);
				}
				fts_say("\n");
			} else {
				fts_say("st_cmds2: cmd %02x @%#06x: NAK\n", tries[k].cmd, tries[k].addr);
			}
		}
		panic("st_cmds2: done");
	}

	if (mode == 25) {
		uint8_t wide[32] = { 0 };
		unsigned found = 0;

		if (st_cmd_read(0xA6, true, 0, wide, sizeof(wide))) {
			fts_say("st_map: 0xA6 @0, 32 bytes:");
			for (unsigned i = 0; i < sizeof(wide); i++) {
				fts_say(" %02x", wide[i]);
			}
			fts_say("\n");
		}

		for (uint32_t addr = 0; addr < 0x40; addr++) {
			uint8_t d[8] = { 0 };
			bool any = false;

			if (!st_cmd_read(0xA6, true, (uint16_t)addr, d, sizeof(d))) {
				continue;
			}
			for (unsigned i = 0; i < sizeof(d); i++) {
				if (d[i] != 0) {
					any = true;
				}
			}
			if (any) {
				fts_say("st_map: %#06x -> %02x %02x %02x %02x %02x %02x %02x %02x\n",
				    addr, d[0], d[1], d[2], d[3], d[4], d[5], d[6], d[7]);
				found++;
			}
		}
		fts_say("st_map: %u addresses with data in 0x00..0x3F\n", found);
		panic("st_map: done");
	}

	if (mode == 24) {
		for (unsigned attempt = 0; attempt < 8; attempt++) {
			uint8_t d[10] = { 0 };

			if (!st_cmd_read(0x86, false, 0, d, sizeof(d))) {
				fts_say("st_event_poll: %u: NAK\n", attempt);
				continue;
			}
			fts_say("st_event_poll: %u: %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x\n",
			    attempt, d[0], d[1], d[2], d[3], d[4], d[5], d[6], d[7], d[8], d[9]);
			sleep_ms(50);
		}
		{
			uint8_t d[8] = { 0 };

			if (st_cmd_read(0xA6, true, 0, d, sizeof(d))) {
				fts_say("st_event_poll: 0xA6 @0 -> %02x %02x %02x %02x %02x %02x %02x %02x\n",
				    d[0], d[1], d[2], d[3], d[4], d[5], d[6], d[7]);
			} else {
				fts_say("st_event_poll: 0xA6 @0 -> NAK\n");
			}
		}
		panic("st_event_poll: done");
	}

	if (mode == 23) {
		unsigned found = 0;

		/* the four command bytes ST documents, each with address 0 */
		{
			static const uint8_t cmds[] = { 0xA0, 0xA2, 0xA4, 0xA8 };
			static const bool addr16[] = { true, false, true, false };

			for (unsigned k = 0; k < sizeof(cmds) / sizeof(cmds[0]); k++) {
				uint8_t d[8] = { 0 };

				if (st_cmd_read(cmds[k], addr16[k], 0, d, sizeof(d))) {
					fts_say("st_cmd_read: cmd %02x -> %02x %02x %02x %02x %02x %02x %02x %02x\n",
					    cmds[k], d[0], d[1], d[2], d[3], d[4], d[5], d[6], d[7]);
				} else {
					fts_say("st_cmd_read: cmd %02x -> NAK\n", cmds[k]);
				}
			}
		}

		/* every command byte with address 0, reporting anything that is not all zeros */
		for (unsigned c = 0; c < 0x100; c++) {
			uint8_t d[6] = { 0 };
			bool any = false;

			if (!st_cmd_read((uint8_t)c, true, 0, d, sizeof(d))) {
				continue;
			}
			for (unsigned i = 0; i < sizeof(d); i++) {
				if (d[i] != 0) {
					any = true;
				}
			}
			if (any) {
				fts_say("st_scan: cmd %02x -> %02x %02x %02x %02x %02x %02x\n",
				    c, d[0], d[1], d[2], d[3], d[4], d[5]);
				if (++found > 20) {
					break;
				}
			}
		}
		fts_say("st_scan: %u commands answered with data\n", found);
		panic("st_scan: done");
	}

	if (mode == 22) {
		unsigned i;

		fts_say("bus_clear: int %u sda %u scl %u\n",
		    gpio_get(FTS_PIN_INT), gpio_get(FTS_PIN_SDA), gpio_get(FTS_PIN_SCL));

		/* nine clocks on SCL with SDA released, then a stop: brings a slave that is mid-byte
		 * back to an idle boundary */
		bb_sda_release();
		for (i = 0; i < 9; i++) {
			bb_scl_low();
			bb_delay();
			bb_scl_release();
			bb_delay();
		}
		bb_stop();
		sleep_ms(2);
		fts_say("bus_clear: after clearing: sda %u scl %u\n",
		    gpio_get(FTS_PIN_SDA), gpio_get(FTS_PIN_SCL));

		/* a bare read: no register address, the pending report if there is one */
		{
			uint8_t r[8] = { 0 };
			bool ok;

			bb_start();
			ok = bb_write((0x49 << 1) | 1);
			fts_say("bus_clear: read address %s\n", ok ? "ACK" : "NAK");
			if (ok) {
				for (i = 0; i < sizeof(r); i++) {
					r[i] = bb_read(i != sizeof(r) - 1);
				}
			}
			bb_stop();
			fts_say("bus_clear: bare read %02x %02x %02x %02x %02x %02x %02x %02x\n",
			    r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7]);
			sleep_ms(2);
			fts_say("bus_clear: after: sda %u scl %u int %u\n",
			    gpio_get(FTS_PIN_SDA), gpio_get(FTS_PIN_SCL), gpio_get(FTS_PIN_INT));
		}
		panic("bus_clear: done");
	}

	if (mode == 21) {
		unsigned i, us;

		fts_say("sda_watch: idle before anything: sda %u scl %u\n",
		    gpio_get(FTS_PIN_SDA), gpio_get(FTS_PIN_SCL));

		/* time the rise after the master releases the line */
		bb_sda_low();
		bb_sda_release();
		for (i = 0; i < 500; i++) {
			if (gpio_get(FTS_PIN_SDA)) {
				break;
			}
			sleep_us(1);
		}
		fts_say("sda_watch: rose after %u us (500 = still low)\n", i);

		/* a write of just the address, then look at the line again */
		bb_start();
		(void)bb_write(0x49 << 1);
		bb_stop();
		us = 0;
		for (i = 0; i < 500; i++) {
			if (gpio_get(FTS_PIN_SDA)) {
				break;
			}
			sleep_us(1);
			us++;
		}
		fts_say("sda_watch: after a write: sda %u scl %u (rose after %u us)\n",
		    gpio_get(FTS_PIN_SDA), gpio_get(FTS_PIN_SCL), us);

		/* a read of one byte */
		bb_start();
		if (bb_write((0x49 << 1) | 1)) {
			uint8_t v = bb_read(false);

			fts_say("sda_watch: one byte read as %02x\n", v);
		}
		bb_stop();
		us = 0;
		for (i = 0; i < 500; i++) {
			if (gpio_get(FTS_PIN_SDA)) {
				break;
			}
			sleep_us(1);
			us++;
		}
		fts_say("sda_watch: after a read: sda %u scl %u (rose after %u us)\n",
		    gpio_get(FTS_PIN_SDA), gpio_get(FTS_PIN_SCL), us);
		panic("sda_watch: done");
	}

	if (mode == 20) {
		static const uint32_t regs[] = { 0x0000, 0x0001, 0x0010, 0x0020, 0x0040, 0x00A0,
						 0x0100, 0xA000, 0xA001, 0xA003 };
		static const uint8_t cmds[] = { 0xA0, 0x00 };

		for (unsigned c = 0; c < sizeof(cmds) / sizeof(cmds[0]); c++) {
			for (unsigned k = 0; k < sizeof(regs) / sizeof(regs[0]); k++) {
				uint8_t r[4] = { 0 };
				bool ok;

				bb_start();
				ok = bb_write(0x49 << 1) && bb_write(cmds[c]) &&
				    bb_write((uint8_t)(regs[k] >> 8)) && bb_write((uint8_t)(regs[k] & 0xff));
				if (ok) {
					bb_start();
					ok = bb_write((0x49 << 1) | 1);
				}
				if (!ok) {
					bb_stop();
					fts_say("cmd_addr_scan: cmd %02x reg %#06x: NAK\n", cmds[c], regs[k]);
					continue;
				}
				for (unsigned i = 0; i < sizeof(r); i++) {
					r[i] = bb_read(i != sizeof(r) - 1);
				}
				bb_stop();
				fts_say("cmd_addr_scan: cmd %02x reg %#06x = %02x %02x %02x %02x\n",
				    cmds[c], regs[k], r[0], r[1], r[2], r[3]);
			}
		}
		panic("cmd_addr_scan: done");
	}

	if (mode == 19) {
		unsigned nonzero = 0;

		for (uint32_t reg = 0; reg < 0x40; reg++) {
			uint8_t r[4] = { 0 };
			bool ok;

			bb_start();
			ok = bb_write(0x49 << 1);
			if (ok) {
				ok = bb_write((uint8_t)reg);            /* one byte of address */
			}
			if (!ok) {
				bb_stop();
				continue;
			}
			bb_start();                                     /* repeated start, no stop */
			if (!bb_write((0x49 << 1) | 1)) {
				bb_stop();
				continue;
			}
			for (unsigned i = 0; i < sizeof(r); i++) {
				r[i] = bb_read(i != sizeof(r) - 1);
			}
			bb_stop();
			if (r[0] || r[1] || r[2] || r[3]) {
				fts_say("reg8_scan: %#04x = %02x %02x %02x %02x\n", reg, r[0], r[1], r[2], r[3]);
				nonzero++;
			}
			if (reg == 0) {
				fts_say("reg8_scan: register 0 answered %02x %02x %02x %02x\n", r[0], r[1], r[2], r[3]);
			}
		}
		fts_say("reg8_scan: %u of 64 registers nonzero\n", nonzero);
		panic("reg8_scan: done");
	}

	if (mode == 18) {
		static const uint8_t f0[2] = { 0xA0, 0x00 };
		static const uint8_t f1[3] = { 0xA0, 0x00, 0x00 };
		static const uint8_t f2[1] = { 0x00 };
		static const uint8_t f3[1] = { 0xA0 };
		static const uint8_t f4[2] = { 0xA2, 0x03 };
		struct { const uint8_t *p; unsigned n; const char *what; } forms[] = {
			{ f0, 2, "A0 00" }, { f1, 3, "A0 00 00" }, { f2, 1, "00" }, { f3, 1, "A0" },
			{ f4, 2, "A2 03" },
		};

		for (unsigned k = 0; k < sizeof(forms) / sizeof(forms[0]); k++) {
			uint8_t r[4] = { 0 };
			bool ok = true;

			sleep_ms(20);
			bb_start();
			ok = bb_write(0x49 << 1);
			for (unsigned i = 0; i < forms[k].n && ok; i++) {
				if (!bb_write(forms[k].p[i])) {
					ok = false;
				}
			}
			bb_stop();
			if (!ok) {
				fts_say("addr_forms: write %s: NAK\n", forms[k].what);
				continue;
			}
			sleep_ms(2);
			bb_start();
			if (!bb_write((0x49 << 1) | 1)) {
				bb_stop();
				fts_say("addr_forms: write %s then read: read address NAKed\n", forms[k].what);
				continue;
			}
			for (unsigned i = 0; i < sizeof(r); i++) {
				r[i] = bb_read(i != sizeof(r) - 1);
			}
			bb_stop();
			fts_say("addr_forms: %s -> %02x %02x %02x %02x\n", forms[k].what, r[0], r[1], r[2], r[3]);
		}
		panic("addr_forms: done");
	}

	if (mode == 17) {
		static const uint8_t cmd_a0[3] = { 0xA0, 0x00, 0x00 };
		static const uint8_t cmd_a2[6] = { 0xA2, 0x03, 0x00, 0x00, 0x00, 0x03 };
		const uint8_t *packets[2] = { cmd_a0, cmd_a2 };
		const unsigned lengths[2] = { 3, 6 };

		for (unsigned p = 0; p < 2; p++) {
			uint8_t r[8] = { 0 };
			bool ok;

			fts_say("cmd_packets: packet %u, %u bytes: ", p, lengths[p]);
			bb_start();
			ok = bb_write(0x49 << 1);
			for (unsigned i = 0; i < lengths[p] && ok; i++) {
				ok = bb_write(packets[p][i]) && ok;
			}
			bb_stop();
			fts_say("%s\n", ok ? "written" : "not acknowledged");
			if (!ok) {
				continue;
			}
			sleep_ms(5);
			bb_start();
			if (!bb_write((0x49 << 1) | 1)) {
				bb_stop();
				fts_say("cmd_packets: read address not acknowledged\n");
				continue;
			}
			for (unsigned i = 0; i < sizeof(r); i++) {
				r[i] = bb_read(i != sizeof(r) - 1);
			}
			bb_stop();
			fts_say("cmd_packets: read %02x %02x %02x %02x %02x %02x %02x %02x\n",
			    r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7]);
		}
		panic("cmd_packets: done");
	}

	if (mode == 12 || mode == 14 || mode == 15) {
		unsigned nonzero = 0, acked = 0;

		{
			uint8_t r[2] = { 0, 0 };

			bb_start();
			if (bb_write(0x49 << 1)) {
				bb_write(0x00);
				bb_write(0x00);
				sleep_us(200);          /* give the part time to latch the pointer */
				bb_start();
				if (bb_write((0x49 << 1) | 1)) {
					r[0] = bb_read(true);
					r[1] = bb_read(false);
				}
			}
			bb_stop();
			fts_say("bringup: register 0 at %u us/bit: %02x %02x\n", bb_us, r[0], r[1]);
		}

		for (uint32_t reg = 0; reg < 0x100; reg++) {
			uint8_t r[2] = { 0, 0 };
			bool ok = false;

			bb_start();
			if (bb_write(0x49 << 1)) {
				bb_write((uint8_t)(reg >> 8));
				bb_write((uint8_t)(reg & 0xff));
				bb_start();
				if (bb_write((0x49 << 1) | 1)) {
					r[0] = bb_read(true);
					r[1] = bb_read(false);
					ok = true;
				}
			}
			bb_stop();
			if (ok) {
				acked++;
			}
			if (r[0] != 0 || r[1] != 0) {
				fts_say("reg_scan: %#04x = %02x %02x\n", reg, r[0], r[1]);
				nonzero++;
			}
		}
		fts_say("reg_scan: %u registers read, %u nonzero\n", acked, nonzero);
		panic("reg_scan: done");
	}

	if (mode >= 11) {
		uint8_t r[4];
		bool ok;

		fts_say("bb2: idle sda %u scl %u\n", gpio_get(FTS_PIN_SDA), gpio_get(FTS_PIN_SCL));
		bb_start();
		ok = bb_write(0x49 << 1);
		fts_say("bb2: address 0x92: %s\n", ok ? "ACK" : "NO ACK");
		if (ok) {
			fts_say("bb2: register byte 0x00: %s\n", bb_write(0x00) ? "ACK" : "NO ACK");
			fts_say("bb2: register byte 0x00: %s\n", bb_write(0x00) ? "ACK" : "NO ACK");
			bb_start();
			fts_say("bb2: address 0x93: %s\n", bb_write((0x49 << 1) | 1) ? "ACK" : "NO ACK");
			for (unsigned i = 0; i < sizeof(r); i++) {
				r[i] = bb_read(i != sizeof(r) - 1);
			}
			bb_stop();
			fts_say("bb2: read %02x %02x %02x %02x\n", r[0], r[1], r[2], r[3]);
		} else {
			bb_stop();
		}
		panic("bb2: done");
	}

	if (mode >= 8) {
		uint8_t id[2] = { 0xff, 0xff };

		/* back to plain gpio with the pull-ups on: the hand-driven bus */
		fts_high(FTS_PIN_SDA);
		fts_high(FTS_PIN_SCL);
		sleep_ms(1);
		fts_say("bb: idle sda %u scl %u\n", fts_read(FTS_PIN_SDA), fts_read(FTS_PIN_SCL));

		fts_start();
		if (!fts_write_byte(0x49 << 1)) {
			fts_stop();
			fts_say("bb: NO ACKNOWLEDGE at 0x49\n");
			panic("bb: no answer");
		}
		fts_say("bb: the chip acknowledges 0x49\n");
		fts_write_byte(0x00);
		fts_write_byte(0x00);
		fts_stop();

		fts_start();
		fts_write_byte((0x49 << 1) | 1);
		id[0] = fts_read_byte(true);
		id[1] = fts_read_byte(false);
		fts_stop();
		fts_say("bb: chip id (stop between) %02x %02x\n", id[0], id[1]);

		if (mode >= 9) {
			for (unsigned attempt = 0; attempt < 4; attempt++) {
				uint8_t r[6];

				sleep_ms(300);
				fts_start();
				if (!fts_write_byte(0x49 << 1)) {
					fts_stop();
					fts_say("bb: no acknowledge on attempt %u\n", attempt);
					continue;
				}
				fts_write_byte(0x00);           /* register 0x0000: the chip id */
				fts_write_byte(0x00);
				fts_start();                    /* repeated start: the pointer stays */
				fts_write_byte((0x49 << 1) | 1);
				for (unsigned i = 0; i < sizeof(r); i++) {
					r[i] = fts_read_byte(i != sizeof(r) - 1);
				}
				fts_stop();
				fts_say("bb: attempt %u: %02x %02x %02x %02x %02x %02x\n", attempt,
				    r[0], r[1], r[2], r[3], r[4], r[5]);
				if (r[0] != 0 || r[1] != 0) {
					break;
				}
			}
		}
		if (mode >= 10) {
			uint8_t best[2] = { 0, 0 };

			for (unsigned attempt = 0; attempt < 10; attempt++) {
				uint8_t r[2] = { 0, 0 };

				sleep_ms(200);
				fts_start();
				if (!fts_write_byte(0x49 << 1)) {
					fts_stop();
					fts_say("bb: attempt %u: no acknowledge\n", attempt);
					continue;
				}
				fts_write_byte(0x00);
				fts_write_byte(0x00);
				fts_start();
				fts_write_byte((0x49 << 1) | 1);
				r[0] = fts_read_byte(true);
				r[1] = fts_read_byte(false);
				fts_stop();
				fts_say("bb: attempt %u (16-bit address): %02x %02x\n", attempt, r[0], r[1]);
				if (r[0] != 0 || r[1] != 0) {
					best[0] = r[0];
					best[1] = r[1];
					break;
				}
			}
			if (best[0] == 0 && best[1] == 0) {
				/* one byte of register address: some parts use an 8-bit pointer for the id */
				sleep_ms(100);
				fts_start();
				if (fts_write_byte(0x49 << 1)) {
					uint8_t r[2];

					fts_write_byte(0x00);
					fts_start();
					fts_write_byte((0x49 << 1) | 1);
					r[0] = fts_read_byte(true);
					r[1] = fts_read_byte(false);
					fts_stop();
					fts_say("bb: 8-bit address: %02x %02x\n", r[0], r[1]);
				} else {
					fts_stop();
					fts_say("bb: 8-bit attempt: no acknowledge\n");
				}
			}
		}
		panic("bb: transaction done");
	}

	/* ---- I2C: the same engine, in its I2C protocol ---- */
	{
		uint8_t id[4] = { 0, 0, 0, 0 };
		const char *err = NULL;

		fts_say("i2c: gcc before: vote %#x, cbcr %#x %#x %#x, fsr %#x\n",
		    rd(GCC_VOTE), rd(GCC_QUP1_M_AHB_CBCR), rd(GCC_QUP1_S_AHB_CBCR),
		    rd(GCC_QUP1_SE5_CBCR), rd(GCC_QUP1_SE5_CFG));
		wr(GCC_VOTE, rd(GCC_VOTE) | VOTE_QUP1_CORE_2X | VOTE_QUP1_CORE | VOTE_QUP1_M_AHB |
		    VOTE_QUP1_S_AHB | VOTE_QUP1_SE5);
		/* the engine's own rate generator from the crystal: source select 0, divider 1 */
		wr(GCC_QUP1_SE5_CFG, (rd(GCC_QUP1_SE5_CFG) & ~0x371fu) | 1u);
		wr(GCC_QUP1_SE5_CMD, rd(GCC_QUP1_SE5_CMD) | 1u);
		sleep_ms(5);
		se_source_hz = XO_HZ;
		se_clock_select = 0;
		fts_say("i2c: gcc after: vote %#x, cfg %#x, cmd %#x, cbcr %#x %#x %#x\n",
		    rd(GCC_VOTE), rd(GCC_QUP1_SE5_CFG), rd(GCC_QUP1_SE5_CMD),
		    rd(GCC_QUP1_M_AHB_CBCR), rd(GCC_QUP1_S_AHB_CBCR), rd(GCC_QUP1_SE5_CBCR));
		if (!err) {
			err = geni_i2c_init(400000);
			fts_say("i2c: engine init: %s (protocol %u, fifo %u)\n",
			    err ? err : "ready", rd(GENI_FW_REVISION_RO) >> 8 & 0xff, fifo_depth);
		}
		if (!err) {
			static const uint8_t pointer[2] = { 0x00, 0x00 };

			err = geni_i2c_transfer(I2C_OP_WRITE, 0x49, pointer, sizeof(pointer), NULL, 0);
			fts_say("i2c: write 00 00 to 0x49: %s\n", err ? err : "ACK (the chip answered)");
		}
		if (!err) {
			err = geni_i2c_transfer(I2C_OP_READ, 0x49, NULL, 0, id, 2);
			fts_say("i2c: read 2 bytes from register 0: %s, data %02x %02x\n",
			    err ? err : "ok", id[0], id[1]);
		}
	}
	panic("i2c: probe done");
}

/*
 * The pin controller at the base the vendor's own group table implies: 0x0f500000 + 0x1000*gpio.
 * One map of 1 MB covers gpio 0..255. tlmtest=1 only reads; tlmtest=2 also writes and reads back.
 */
static void
tlmm_test(uint32_t mode)
{
	vm_offset_t b = ml_io_map(0x0f500000UL, 0x100000UL);
	uint64_t c36 = b + 0x1000UL * 36, c37 = b + 0x1000UL * 37, c38 = b + 0x1000UL * 38;
	uint32_t v36, v37, v38, io36;

	fts_say("tlmm: window %#llx, cfg36 %#llx, cfg38 %#llx\n", (unsigned long long)b,
	    (unsigned long long)c36, (unsigned long long)c38);
	v36 = rd(c36);
	v37 = rd(c37);
	v38 = rd(c38);
	io36 = rd(c36 + 4);
	fts_say("tlmm: cfg36 %#x cfg37 %#x cfg38 %#x io36 %#x\n", v36, v37, v38, io36);
	if (mode < 2) {
		panic("tlmtest: read-only pass done (cfg36 %#x cfg38 %#x)", v36, v38);
	}
	wr(c36, 0x000000C3UL);          /* gpio function, pull-up, 8 mA, input */
	fts_say("tlmm: wrote C3 to gpio36, read back %#x\n", rd(c36));
	wr(c36, v36);                   /* put back what was there */
	panic("tlmtest: write pass done");
}

void
thyme_fts_probe(void)
{
	uint8_t id[6];
	int i;

	fts_stage(1, "entered the probe");


	/*
	 * Which window is the pin controller in? The device tree says 0x0f000000 (16 MB);
	 * the redwood driver this probe came from says 0x0f100000. Read the reset pin's
	 * configuration from both: Android leaves gpio38 as a plain GPIO with a pull-up and
	 * 8 mA of drive, so the live window answers with a small non-zero word and the other
	 * one does not.
	 */
	vm_offset_t tlmm_dt = ml_io_map(0x0f000000UL, 0x100000UL);   /* 1 MB reaches gpio 255; 16 MB does not fit this early */
	vm_offset_t tlmm_rw = ml_io_map(TLMM_PHYS, TLMM_SIZE);
	uint32_t dt_cfg38 = rd(tlmm_dt + 0x1000UL * FTS_PIN_RESET);
	uint32_t dt_cfg36 = rd(tlmm_dt + 0x1000UL * FTS_PIN_SDA);
	uint32_t rw_cfg38 = rd(tlmm_rw + 0x1000UL * FTS_PIN_RESET);
	uint32_t rw_cfg36 = rd(tlmm_rw + 0x1000UL * FTS_PIN_SDA);

	fts_say("fts: cfg38/cfg36: at 0f000000 %#x/%#x, at 0f100000 %#x/%#x\n",
	    dt_cfg38, dt_cfg36, rw_cfg38, rw_cfg36);
	{
		uint32_t window = 0;

		/* ftstlmm=1: the redwood driver's window; anything else: the device tree's */
		PE_parse_boot_argn("ftstlmm", &window, sizeof(window));
		tlmm = window ? tlmm_rw : tlmm_dt;
	}
	{
		uint32_t window = 0;

		PE_parse_boot_argn("ftstlmm", &window, sizeof(window));
		fts_say("fts: using the window at %s (ftstlmm=%u)\n", window ? "0f100000" : "0f000000", window);
	}

	fts_stage(2, "tlmm mapped");

	/* ---- mmio control: do any device registers answer this kernel at all? ---- */
	{
		vm_offset_t gic = ml_io_map(0x17A00000UL, 0x10000UL);
		vm_offset_t gcc = ml_io_map(0x00100000UL, 0x200000UL);

		fts_say("fts: mmio control: gic iidr %#x, pidr2 %#x\n", rd(gic + 0x8), rd(gic + 0xffe8));
		fts_say("fts: mmio control: gcc vote %#x, se5 cfg %#x\n", rd(gcc + 0x52008), rd(gcc + 0x18604));
		fts_say("fts: mmio control: tlmm cfg36 %#x, io36 %#x\n", rd(tlmm_dt + 0x24000), rd(tlmm_dt + 0x24004));
	}
	fts_stage(32, "mmio control reads");

	{
		vm_offset_t ufs = ml_io_map(0x01D84000UL, 0x10000UL);

		/* reach: ufs - the loader itself drives this controller, so it is in the map for sure */
		fts_say("fts: reach: ufs %#x\n", rd(ufs + 0x0));
	}
	fts_stage(33, "read the ufs controller");
	{
		vm_offset_t rsc = ml_io_map(0x18220000UL, 0x10000UL);

		fts_say("fts: reach: rpmh rsc %#x\n", rd(rsc + 0xd00));
	}
	fts_stage(34, "read the rpmh command set");
	{
		vm_offset_t mdp = ml_io_map(0x0AE00000UL, 0x10000UL);

		fts_say("fts: reach: mdss %#x\n", rd(mdp + 0x0));
	}
	fts_stage(35, "read the display controller");

	/* ---- write/read test: does the pin controller keep what is written to it? ---- */
	{
		vm_offset_t a = tlmm_dt + 0x1000UL * FTS_PIN_SDA;      /* device tree window, gpio36 */

		fts_say("fts: write/read test: %#x write %#x -> ", a, 0x12345678UL);
		wr(a, 0x12345678UL);
		fts_say("read %#x\n", rd(a));
		wr(a, 0);                                              /* put the reset value back */
	}
	fts_stage(36, "write/read test at the device tree window");
	{
		vm_offset_t b = tlmm_rw + 0x1000UL * FTS_PIN_SDA;      /* mainline window, gpio36 */

		fts_say("fts: write/read test: %#x write %#x -> ", b, 0x12345678UL);
		wr(b, 0x12345678UL);
		fts_say("read %#x\n", rd(b));
		wr(b, 0);
	}
	fts_stage(37, "write/read test at the mainline window");

	{
		volatile uint32_t v = *(volatile uint32_t *)(uintptr_t)0x0F124000UL;

		fts_say("fts: probe read 0f124000 = %#x\n", v);
	}
	fts_stage(30, "read 0f124000 (mainline TLMM base + gpio36)");
	{
		volatile uint32_t v = *(volatile uint32_t *)(uintptr_t)0x0F126000UL;

		fts_say("fts: probe read 0f126000 = %#x\n", v);
	}
	fts_stage(31, "read 0f126000 (mainline TLMM base + gpio38)");


	fts_say("fts: probing the touch screen on gpio %u (sda) %u (scl) %u (reset) %u (int), address %#x\n",
	    FTS_PIN_SDA, FTS_PIN_SCL, FTS_PIN_RESET, FTS_PIN_INT, FTS_ADDRESS);

	fts_stage(3, "the probe was announced");

	uint32_t cfg36 = rd(GPIO_CFG(FTS_PIN_SDA));

	fts_say("fts: pin 36's configuration reads %#x, before anything is written\n", cfg36);
	fts_stage(4, "pin 36's configuration read, nothing written yet");

	fts_high(FTS_PIN_SDA);          /* both bus lines idle: inputs, pull-ups on */
	fts_stage(5, "pin 36 (sda) configured as an input");
	fts_high(FTS_PIN_SCL);
	fts_stage(6, "pin 37 (scl) configured as an input");
	gpio_output(FTS_PIN_RESET, true, PULL_UP, 2);   /* out of reset */
	fts_stage(7, "pin 38 (reset) driven high");
	gpio_configure(FTS_PIN_INT, 0, PULL_UP, 2, false);
	fts_stage(8, "pin 39 (int) configured as an input");
	sleep_ms(10);
	fts_stage(9, "slept 10 ms");
	uint32_t sda_in = gpio_get(FTS_PIN_SDA), scl_in = gpio_get(FTS_PIN_SCL), int_in = gpio_get(FTS_PIN_INT);

	fts_stage(10, "the three input levels read");
	uint32_t cfg_sda = rd(GPIO_CFG(FTS_PIN_SDA)), cfg_scl = rd(GPIO_CFG(FTS_PIN_SCL));
	uint32_t cfg_rst = rd(GPIO_CFG(FTS_PIN_RESET)), cfg_int = rd(GPIO_CFG(FTS_PIN_INT));

	fts_stage(11, "the four pin configurations read");
	fts_say("fts: before reset: sda %u scl %u int %u; cfg %#x %#x %#x %#x\n",
	    sda_in, scl_in, int_in, cfg_sda, cfg_scl, cfg_rst, cfg_int);

	fts_stage(12, "about to pulse the reset line");
	gpio_output(FTS_PIN_RESET, false, PULL_UP, 2);
	sleep_ms(15);
	gpio_output(FTS_PIN_RESET, true, PULL_UP, 2);
	sleep_ms(150);

	fts_stage(13, "the reset pulse is over");
	fts_say("fts: after reset: sda %u scl %u int %u\n",
	    fts_read(FTS_PIN_SDA) ? 1 : 0, fts_read(FTS_PIN_SCL) ? 1 : 0, gpio_get(FTS_PIN_INT) ? 1 : 0);
	if (!fts_read(FTS_PIN_SDA) || !fts_read(FTS_PIN_SCL)) {
		fts_say("fts: a bus line stays low: wrong pins, or no pull-up\n");
		return;
	}

	fts_stage(14, "both bus lines are high, about to address the chip");
	fts_start();
	if (!fts_write_byte(FTS_ADDRESS << 1)) {
		fts_stop();
		fts_say("fts: NO ACKNOWLEDGE at %#x: nothing answers (pins, supply or reset)\n", FTS_ADDRESS);
		return;
	}
	fts_say("fts: the chip acknowledges at %#x\n", FTS_ADDRESS);
	fts_stage(15, "the chip acknowledged");


	fts_write_byte(0x00);           /* a 16-bit register address: ST's FTS uses that */
	fts_write_byte(0x00);
	fts_start();
	fts_write_byte((FTS_ADDRESS << 1) | 1);
	for (i = 0; i < 6; i++) {
		id[i] = fts_read_byte(i != 5);
	}
	fts_stop();
	fts_say("fts: registers 0x0000..0x0005: %02x %02x %02x %02x %02x %02x\n", id[0], id[1], id[2], id[3], id[4], id[5]);
	fts_stage(16, "the registers were read");


	fts_start();
	if (fts_write_byte(FTS_ADDRESS << 1)) {
		fts_write_byte(0x00);   /* the same address as one byte, in case the chip wants 8-bit registers */
		fts_start();
		fts_write_byte((FTS_ADDRESS << 1) | 1);
		for (i = 0; i < 6; i++) {
			id[i] = fts_read_byte(i != 5);
		}
		fts_stop();
		fts_say("fts: 8-bit addressing, registers 0xf0..0xf5: %02x %02x %02x %02x %02x %02x\n",
		    id[0], id[1], id[2], id[3], id[4], id[5]);
	} else {
		fts_stop();
	}
	fts_say("fts: done\n");
}

void
redwood_goodix_init(void)
{
	uint32_t enabled = 0, probe = 0;
	thread_t thread;

	fts_say("goodix: init, ftsprobe parsed=%u value=%u\n",
	    PE_parse_boot_argn("ftsprobe", &probe, sizeof(probe)) ? 1 : 0, probe);

	{
		uint32_t tlm = 0;

		if (PE_parse_boot_argn("tlmtest", &tlm, sizeof(tlm)) && tlm) {
			tlmm_test(tlm);
			return;
		}
	}

	{
		uint32_t bp = 0;

		if (PE_parse_boot_argn("bringup", &bp, sizeof(bp)) && bp) {
			bringup_test(bp);
			return;
		}
	}
	{
		uint32_t fts = 0, ts = 0;

		/*
		 * touchscreen=1 (or touch_fts=1) is this phone's ST panel: the driver itself runs from the
		 * input layer, long after this returns, so give it the hardware first - TLMM mapped, pins
		 * muxed, rails on, reset released - exactly as the bringup probe does.
		 */
		/* touchscreen=1 now belongs to the ported driver, which sets the pins itself;
		 * only the old probe path still needs this preamble. */
		if (PE_parse_boot_argn("touch_fts", &fts, sizeof(fts)) && fts) {
			bringup_test(0);
			return;
		}
	}
	if (probe) {
		thyme_fts_probe();
		return;
	}
	if (!PE_parse_boot_argn("goodix", &enabled, sizeof(enabled)) || !enabled) {
		return;
	}
	PE_parse_boot_argn("touch_flip", &flip, sizeof(flip));
	if (kernel_thread_start(goodix_thread, NULL, &thread) == KERN_SUCCESS) {
		thread_deallocate(thread);
	}
}


/* ---- RPMh rails for the ported touch driver ---------------------------------------------- */

/* Vote a rail on: name comes from the command database ("ldoc1"), mv is the voltage in millivolts. */
int
redwood_rpmh_vreg_enable(const char *name, unsigned mv)
{
	uint32_t base;
	uint32_t address[3], data[3];

	/* the command database is mapped by the probe preamble, which a plain boot never runs */
	if (cmd_db == 0) {
		rsc = ml_io_map(RSC_PHYS, RSC_SIZE);
		cmd_db = ml_io_map_wcomb(CMD_DB_PHYS, CMD_DB_SIZE);
	}
	if (cmd_db == 0) {
		return ENXIO;
	}

	base = rpmh_address(name);
	if (base == 0 || ((base >> 16) & 0xf) != 4) {
		return ENXIO;
	}
	address[0] = base + VRM_VOLTAGE;
	address[1] = base + VRM_ENABLE;
	address[2] = base + VRM_MODE;
	data[0] = mv;
	data[1] = 1;
	data[2] = VRM_MODE_LDO_HPM;
	return rpmh_write(address, data, 3) ? 0 : EIO;
}

/* ---- transport entry point for the ported ST driver ------------------------------------------ */

/* Layout-compatible with the port's struct i2c_msg: addr, flags, len, buf. */
struct fts_port_msg {
	unsigned short addr;
	unsigned short flags;
	unsigned short len;
	unsigned char *buf;
};

#define FTS_PORT_MSG_READ 0x0001

int
redwood_fts_transfer(struct fts_port_msg *msgs, int num)
{
	if (num <= 0) {
		return 0;
	}
	for (int m = 0; m < num; m++) {
		struct fts_port_msg *msg = &msgs[m];
		bool read = (msg->flags & FTS_PORT_MSG_READ) != 0;
		bool ok;

		bb_start();
		ok = bb_write((uint8_t)((msg->addr << 1) | (read ? 1 : 0)));
		if (ok) {
			if (read) {
				for (unsigned i = 0; i < msg->len; i++) {
					msg->buf[i] = bb_read(i != msg->len - 1);
				}
			} else {
				for (unsigned i = 0; i < msg->len && ok; i++) {
					ok = bb_write(msg->buf[i]);
				}
			}
		}
		if (!ok) {
			bb_stop();
			return -1;
		}
	}
	bb_stop();
	return num;
}

void
redwood_fts_gpio_set(int gpio, int value)
{
	if (gpio >= 0) {
		gpio_output(gpio, value != 0, PULL_UP, 2);
	}
}


/* Blink a small patch of the framebuffer: a cheap way to keep the display actively refreshing
 * without painting the whole panel (and without burn-in risk). */
void
redwood_fb_blink(int times)
{
	vm_offset_t fb = ml_io_map(0x9C000000ULL, 64 * 4 * 64);

	if (fb == 0) {
		return;
	}
	for (int i = 0; i < times; i++) {
		uint32_t *px = (uint32_t *)fb;

		for (int p = 0; p < 64 * 64; p++) {
			px[p] = (i & 1) ? 0xFFFFFFFFu : 0x00000000u;
		}
		sleep_ms(30);
	}
}
