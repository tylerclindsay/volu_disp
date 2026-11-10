// Edge node LED output: I2S1 parallel DMA -> 74HCT245 -> 74HCT595s.
// Design: docs/decisions/0014-edge-node-output.md
//
// Waveform. Each WS2811 bit is 3 slots: high, data, low. Each slot is 8
// samples; every sample clocks one bit into all 8 shift registers at once
// (the first sample of a slot ends up on QH, the last on QA). RCLK is high
// during sample 0 of every slot, latching the slot shifted in before it,
// so all 64 outputs change together once per slot, one slot late. At a
// 20 MHz sample clock a slot is 400 ns and a bit 1.2 µs (833 kHz):
// T0H 0.4 µs, T1H 0.8 µs. After the last pixel, latch-only slots hold the
// lines low for the reset time.
//
// Sample format (I2S1, LCD mode, 32-bit samples, mono): parallel line n is
// sample bit 16 + n (as bench-verified in FastLED's I2S1 driver):
//   lines 0..7  serial data of chains 0..7     line 8  RCLK
// SRCLK is the I2S WS (LCD write) clock: one cycle per sample.
//
// Pipeline. The caller's frame buffer is read in place during the show. A
// ring of DMA buffers (PX_PER_BUF pixel rows each) is refilled by the
// end-of-buffer interrupt, which bit-transposes each pixel row into the
// data slots: 24 8x8 transposes and 192 samples per pixel. The high and
// low slots are constant and only rewritten when a pixel area changes
// between data and idle. So nothing is copied or encoded between shows.

#include "edge_out.h"
#include <Arduino.h>
#include <cstring>

extern "C" {
#include "driver/gpio.h"
#include "esp_heap_caps.h"
#include "esp_intr_alloc.h"
#include "esp_private/periph_ctrl.h"
#include "esp_rom_gpio.h"
#include "rom/lldesc.h"
#include "soc/gpio_sig_map.h"
#include "soc/i2s_reg.h"
#include "soc/i2s_struct.h"
}

namespace {

constexpr int SLOT_SAMPLES = 8;                     // one per 595 output
constexpr int BIT_SAMPLES = 3 * SLOT_SAMPLES;       // high, data, low slots
constexpr int PX_SAMPLES = 24 * BIT_SAMPLES;        // 576 per pixel row
constexpr int PX_PER_BUF = 2;                       // pixel rows per DMA buffer
constexpr int NUM_BUFS = 3;                         // DMA ring length
constexpr int NUM_DESCS = NUM_BUFS * PX_PER_BUF;    // one descriptor per pixel row
static_assert(PX_SAMPLES * 4 <= 4092, "a DMA descriptor holds at most 4092 bytes");

constexpr uint32_t DATA_SHIFT = 16;                 // line n = sample bit 16 + n
constexpr uint32_t LATCH = 1u << (DATA_SHIFT + 8);  // line 8 = RCLK

constexpr int CHAINS = EDGE_STRIPS / 8;             // chains with strips
constexpr uint8_t USED_CHAINS = (uint8_t)((1u << CHAINS) - 1);
constexpr int STRIP_BYTES = LEDS_PER_STRIP * 3;
constexpr uint8_t ORDER[3] = {LED_COLOR_ORDER};     // R,G,B index of each wire byte

// Timing, from the sample clock: 80 MHz / I2S_CLK_DIV.
constexpr uint32_t SAMPLE_PS = 12500u * I2S_CLK_DIV;
constexpr uint32_t BUF_NS = PX_PER_BUF * PX_SAMPLES * SAMPLE_PS / 1000;
constexpr int PIXEL_BUFS = (LEDS_PER_STRIP + PX_PER_BUF - 1) / PX_PER_BUF;
// Latch-only buffers after the pixels: the reset time, plus up to 64
// samples (the TX FIFO) cut off when the interrupt stops the output.
constexpr int TAIL_BUFS = (LED_RESET_US * 1000 + 64 * SAMPLE_PS / 1000 + BUF_NS - 1) / BUF_NS;
constexpr uint32_t TOTAL_BUFS = PIXEL_BUFS + TAIL_BUFS;
constexpr uint32_t SHOW_US = TOTAL_BUFS * BUF_NS / 1000;

// What a pixel area of a DMA buffer currently holds.
enum : uint8_t { AREA_UNSET, AREA_IDLE, AREA_DATA };

// All in DRAM (heap or .bss), so the IRAM interrupt can use them.
const uint8_t *frame;                         // being shown (caller's buffer)
uint32_t *bufs[NUM_BUFS];                     // PX_PER_BUF * PX_SAMPLES each
lldesc_t *descs;                              // NUM_DESCS, circular
uint8_t area[NUM_BUFS][PX_PER_BUF];
uint8_t routedChains;                         // chains whose pin carries I2S data
uint8_t highChains;                           // chains driven this frame
volatile uint32_t buffersDone;                // buffers fetched this show
volatile uint32_t lateCount;                  // written by the interrupt only
SemaphoreHandle_t doneSem;
intr_handle_t intrHandle;

// ---------------------------------------------------------------------------
//  Interrupt side (IRAM: keeps running while flash is busy, e.g. OTA)
// ---------------------------------------------------------------------------
// 8x8 bit transpose: out[n] bit c = bit (7 - n) of v[c].
inline __attribute__((always_inline)) void transpose8(const uint8_t v[8], uint8_t out[8]) {
  uint32_t x = (v[7] << 24) | (v[6] << 16) | (v[5] << 8) | v[4];
  uint32_t y = (v[3] << 24) | (v[2] << 16) | (v[1] << 8) | v[0];
  uint32_t t;
  t = (x ^ (x >> 7)) & 0x00AA00AA;  x = x ^ t ^ (t << 7);
  t = (y ^ (y >> 7)) & 0x00AA00AA;  y = y ^ t ^ (t << 7);
  t = (x ^ (x >> 14)) & 0x0000CCCC; x = x ^ t ^ (t << 14);
  t = (y ^ (y >> 14)) & 0x0000CCCC; y = y ^ t ^ (t << 14);
  t = (x & 0xF0F0F0F0) | ((y >> 4) & 0x0F0F0F0F);
  y = ((x << 4) & 0xF0F0F0F0) | (y & 0x0F0F0F0F);
  x = t;
  out[0] = x >> 24; out[1] = x >> 16; out[2] = x >> 8; out[3] = x;
  out[4] = y >> 24; out[5] = y >> 16; out[6] = y >> 8; out[7] = y;
}

inline __attribute__((always_inline)) uint8_t scale(uint8_t v) {
#if GLOBAL_BRIGHTNESS < 255
  return (v * (GLOBAL_BRIGHTNESS + 1)) >> 8;
#else
  return v;
#endif
}

// One wire byte of output j of every chain (strips c * 8 + j) into sample
// k = 7 - j of the 8 bits' data slots (the slot's first sample ends up on
// QH). px: the pixel's R,G,B in strip 0; w: data slot of the byte's MSB.
inline __attribute__((always_inline)) void dataByte(const uint8_t *px, int j, int rgb,
                                                    uint32_t *w, uint32_t latch,
                                                    uint32_t chains) {
  uint8_t v[8], t[8];
  for (int c = 0; c < 8; c++) v[c] = c < CHAINS ? scale(px[(c * 8 + j) * STRIP_BYTES + rgb]) : 0;
  transpose8(v, t);
  for (int n = 0; n < 8; n++, w += BIT_SAMPLES) *w = ((t[n] & chains) << DATA_SHIFT) | latch;
}

// A pixel area past the strip end: all lines low, latch-only slots.
inline __attribute__((always_inline)) void writeIdle(uint32_t *s) {
  for (int n = 0; n < PX_SAMPLES; n++) s[n] = (n % SLOT_SAMPLES == 0) ? LATCH : 0;
}

// The constant slots of a pixel area: each bit's high slot (driven chains
// high) and low slot.
inline __attribute__((always_inline)) void writeFixedSlots(uint32_t *s) {
  const uint32_t high = (uint32_t)highChains << DATA_SHIFT;
  for (int bit = 0; bit < 24; bit++) {
    uint32_t *w = s + bit * BIT_SAMPLES;
    for (int k = 0; k < SLOT_SAMPLES; k++) {
      w[k] = high | (k == 0 ? LATCH : 0);
      w[2 * SLOT_SAMPLES + k] = (k == 0 ? LATCH : 0);
    }
  }
}

// The data slots of pixel row p.
inline __attribute__((always_inline)) void writeData(uint32_t *s, uint32_t p) {
  const uint8_t *px = frame + p * 3;
  const uint32_t chains = highChains;
  for (int j = 0; j < 8; j++) {
    const int k = 7 - j;
    const uint32_t latch = k == 0 ? LATCH : 0;
    uint32_t *w = s + SLOT_SAMPLES + k;
    dataByte(px, j, ORDER[0], w, latch, chains);
    dataByte(px, j, ORDER[1], w + 8 * BIT_SAMPLES, latch, chains);
    dataByte(px, j, ORDER[2], w + 16 * BIT_SAMPLES, latch, chains);
  }
}

// Pixel area s (holding `kind`) for pixel row p: the constant parts are
// only rewritten when the kind changes.
void IRAM_ATTR fillPixel(uint32_t *s, uint8_t &kind, uint32_t p) {
  if (p >= LEDS_PER_STRIP) {
    if (kind != AREA_IDLE) writeIdle(s);
    kind = AREA_IDLE;
    return;
  }
  if (kind != AREA_DATA) writeFixedSlots(s);
  kind = AREA_DATA;
  writeData(s, p);
}

// Fills ring buffer b with the frame's n-th buffer (pixel rows, then idle).
void IRAM_ATTR fillBuf(uint32_t b, uint32_t n) {
  for (int i = 0; i < PX_PER_BUF; i++)
    fillPixel(bufs[b] + i * PX_SAMPLES, area[b][i], n * PX_PER_BUF + i);
}

void IRAM_ATTR stopTx() {
  I2S1.int_ena.val = 0;
  I2S1.conf.tx_start = 0;
  I2S1.out_link.stop = 1;
}

// The whole frame has been fetched: stop and wake edgeShow().
inline __attribute__((always_inline)) void finishShow() {
  stopTx();
  BaseType_t woken = pdFALSE;
  xSemaphoreGiveFromISR(doneSem, &woken);
  portYIELD_FROM_ISR(woken);
}

// End of a DMA buffer: it has moved into the TX FIFO, and the DMA is on the
// next one. Refill it with the buffer NUM_BUFS ahead, or stop when the
// whole frame (with its reset tail) has been fetched.
void IRAM_ATTR onDmaEof(void *) {
  const uint32_t st = I2S1.int_st.val;
  I2S1.int_clr.val = st;
  if (!(st & I2S_OUT_EOF_INT_ST_M)) return;
  const uint32_t d = (I2S1.out_eof_des_addr - (uint32_t)descs) / sizeof(lldesc_t);
  if (d >= NUM_DESCS) return;
  const uint32_t finished = d / PX_PER_BUF;
  // Normally `finished` is the next buffer in order; if the interrupt was
  // late and EOFs merged, catch up (those buffers replayed stale data).
  for (int i = 0; i < NUM_BUFS; i++) {
    const uint32_t b = buffersDone % NUM_BUFS;
    const uint32_t n = buffersDone + 1;
    buffersDone = n;
    if (n >= TOTAL_BUFS) {
      finishShow();
      return;
    }
    fillBuf(b, n - 1 + NUM_BUFS);
    if (b == finished) return;
    lateCount = lateCount + 1;
  }
}

// ---------------------------------------------------------------------------
//  Setup
// ---------------------------------------------------------------------------
void routeOut(int pin, int signal, bool invert) {
  esp_rom_gpio_pad_select_gpio(pin);
  gpio_set_direction((gpio_num_t)pin, GPIO_MODE_OUTPUT);
  esp_rom_gpio_connect_out_signal(pin, signal, invert, false);
}

void resetI2s() {
  const uint32_t lcRst = I2S_IN_RST_M | I2S_OUT_RST_M | I2S_AHBM_RST_M | I2S_AHBM_FIFO_RST_M;
  I2S1.lc_conf.val |= lcRst;
  I2S1.lc_conf.val &= ~lcRst;
  const uint32_t confRst = I2S_RX_RESET_M | I2S_RX_FIFO_RESET_M | I2S_TX_RESET_M | I2S_TX_FIFO_RESET_M;
  I2S1.conf.val |= confRst;
  I2S1.conf.val &= ~confRst;
}

// The DMA buffers and their circular descriptor list (one per pixel row,
// an EOF interrupt per buffer). Returns false if out of memory.
bool allocRing() {
  descs = (lldesc_t *)heap_caps_calloc(NUM_DESCS, sizeof(lldesc_t), MALLOC_CAP_DMA);
  if (!descs) return false;
  for (int b = 0; b < NUM_BUFS; b++) {
    bufs[b] = (uint32_t *)heap_caps_malloc(PX_PER_BUF * PX_SAMPLES * 4, MALLOC_CAP_DMA);
    if (!bufs[b]) return false;
    for (int i = 0; i < PX_PER_BUF; i++) area[b][i] = AREA_UNSET;
  }
  for (int d = 0; d < NUM_DESCS; d++) {
    lldesc_t &l = descs[d];
    l.buf = (uint8_t *)(bufs[d / PX_PER_BUF] + (d % PX_PER_BUF) * PX_SAMPLES);
    l.size = PX_SAMPLES * 4;
    l.length = PX_SAMPLES * 4;
    l.owner = 1;
    l.eof = (d % PX_PER_BUF == PX_PER_BUF - 1);   // one interrupt per buffer
    l.qe.stqe_next = &descs[(d + 1) % NUM_DESCS];
  }
  return true;
}

// I2S1, LCD master TX, 32-bit mono samples (FastLED's bench-tested setup).
void configI2s() {
  periph_module_enable(PERIPH_I2S1_MODULE);
  resetI2s();
  I2S1.conf.val = 0;
  I2S1.conf.tx_msb_right = 1;
  I2S1.conf.tx_right_first = 1;
  I2S1.conf2.val = 0;
  I2S1.conf2.lcd_en = 1;
  I2S1.sample_rate_conf.val = 0;
  I2S1.sample_rate_conf.tx_bits_mod = 32;
  I2S1.sample_rate_conf.tx_bck_div_num = 1;
  I2S1.clkm_conf.val = 0;   // clka_en = 0: PLL_D2 clock; sample rate 80 MHz / div
  I2S1.clkm_conf.clkm_div_a = 1;
  I2S1.clkm_conf.clkm_div_b = 0;
  I2S1.clkm_conf.clkm_div_num = I2S_CLK_DIV;
  I2S1.clkm_conf.clk_en = 1;
  I2S1.fifo_conf.val = 0;
  I2S1.fifo_conf.tx_fifo_mod_force_en = 1;
  I2S1.fifo_conf.tx_fifo_mod = 3;
  I2S1.fifo_conf.tx_data_num = 32;
  I2S1.fifo_conf.dscr_en = 1;
  I2S1.conf1.val = 0;
  I2S1.conf1.tx_pcm_bypass = 1;
  I2S1.conf_chan.val = 0;
  I2S1.conf_chan.tx_chan_mod = 1;
  I2S1.timing.val = 0;
  I2S1.int_ena.val = 0;
  I2S1.int_clr.val = I2S1.int_raw.val;
}

// Starts DMA from the top of the ring, with the EOF interrupt on.
void startTx() {
  resetI2s();
  I2S1.lc_conf.val = I2S_OUT_DATA_BURST_EN | I2S_OUTDSCR_BURST_EN;
  I2S1.int_clr.val = I2S1.int_raw.val;
  I2S1.int_ena.val = 0;
  I2S1.int_ena.out_eof = 1;
  I2S1.out_link.addr = (uint32_t)&descs[0];
  I2S1.out_link.start = 1;
  I2S1.conf.tx_start = 1;
}

}  // namespace

// ---------------------------------------------------------------------------
//  Public API
// ---------------------------------------------------------------------------
bool edgeInit() {
  doneSem = xSemaphoreCreateBinary();
  if (!doneSem || !allocRing()) return false;
  configI2s();
  return esp_intr_alloc(ETS_I2S1_INTR_SOURCE, ESP_INTR_FLAG_IRAM | ESP_INTR_FLAG_LEVEL3,
                        onDmaEof, nullptr, &intrHandle) == ESP_OK;
}

void edgeAttachPins() {
  static const int dataPins[8] = {
      PIN_E1_Q1SER, PIN_E1_Q2SER, PIN_E1_Q3SER, PIN_E1_Q4SER,
      PIN_E2_Q1SER, PIN_E2_Q2SER, PIN_E2_Q3SER, PIN_E2_Q4SER,
  };
  for (int c = 0; c < 8; c++) {
#if KEEP_SERIAL
    if (dataPins[c] == 1 || dataPins[c] == 3) continue;   // UART0 TX/RX stay serial
#endif
    routeOut(dataPins[c], I2S1O_DATA_OUT0_IDX + c, false);
    routedChains |= 1 << c;
  }
  routeOut(PIN_RCLK, I2S1O_DATA_OUT0_IDX + 8, false);
  routeOut(PIN_SRCLK, I2S1O_WS_OUT_IDX, SRCLK_INVERT);
}

EdgeResult edgeShow(const uint8_t *rgb, uint8_t chainMask) {
  chainMask &= USED_CHAINS & routedChains;
  if (!chainMask) return EDGE_NO_CHAINS;
  if (chainMask != highChains) {   // the constant high slots must be rewritten
    highChains = chainMask;
    memset(area, AREA_UNSET, sizeof(area));
  }
  frame = rgb;
  buffersDone = 0;
  for (int b = 0; b < NUM_BUFS; b++) fillBuf(b, b);
  xSemaphoreTake(doneSem, 0);   // clear a stale give
  startTx();
  if (xSemaphoreTake(doneSem, pdMS_TO_TICKS(SHOW_US / 1000 + 20)) != pdTRUE) {
    stopTx();
    return EDGE_TIMEOUT;
  }
  return EDGE_SHOWN;
}

uint32_t edgeShowUs() { return SHOW_US; }

uint32_t edgeClockHz() { return 80000000u / I2S_CLK_DIV; }

uint32_t edgeLateCount() { return lateCount; }
