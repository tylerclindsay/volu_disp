#pragma once
#include <Arduino.h>
#include <stdint.h>
// =============================================================================
//  config.h - WT32-ETH01 drive node, wire protocol v2, edge node output
// =============================================================================
//
//  Spec: docs/protocol.md. Summary (all multi-byte fields little-endian):
//
//  Common header (8 bytes)
//    0  1  magic    0xA7
//    1  1  version  2
//    2  1  type     1 PIXELS, 2 SYNC, 3 reserved (status); others ignored
//    3  1  flags    bit0 SHOW_NOW (PIXELS only); other bits must be 0
//    4  4  frame    u32 host frame sequence number
//
//  PIXELS (type 1): + offset u16, count u16 (1..486), then R,G,B x count.
//    Writes into the node's flat back buffer: pixel p of strip s is at
//    s * LEDS_PER_STRIP + p (pixel 0 = top of the strip). Packets may span
//    strips; pixels past the buffer end are ignored.
//  SYNC (type 2): header only, broadcast. Latch: back -> front, show.
//
//  PIXELS 1..8 frames older than the newest frame seen are late and dropped.
//
// =============================================================================

// ---------------------------------------------------------------------------
//  Node identity (override per node with -DNODE_ID=2 in platformio.ini)
// ---------------------------------------------------------------------------
#ifndef NODE_ID
#define NODE_ID 1
#endif

#define STR_(x) #x
#define STR(x)  STR_(x)
#define HOSTNAME "volu-disp-node-" STR(NODE_ID)

// ---------------------------------------------------------------------------
//  Strip layout (override per node with -DNUM_STRIPS=40 etc. in platformio.ini)
// ---------------------------------------------------------------------------
// Must match this node's section in config/displays/*.yaml:
//   NUM_STRIPS = section width * depth, LEDS_PER_STRIP = section height.
// Strip s is (z - z0) * width + (x - x0) (docs/hardware.md -> Drive node
// mapping) and plugs into edge node s / 32 (E1, E2), connector J(s % 32 + 1).
// Defaults: 40 x 12, matching config/displays/bench.yaml and each node of
// config/displays/prototype.yaml.
#ifndef NUM_STRIPS
#define NUM_STRIPS 40
#endif
#ifndef LEDS_PER_STRIP
#define LEDS_PER_STRIP 12
#endif

// ---------------------------------------------------------------------------
//  LED output: I2S1 -> 74HCT245 -> IDC -> edge node 74HCT595s
//  (src/edge_out.cpp, docs/decisions/0014-edge-node-output.md)
// ---------------------------------------------------------------------------
// Serial data line of each shift register: E<edge>_Q<register>SER.
// Register n of an edge node drives connectors J(8n-7)..J(8n) (outputs A..H).
#define PIN_E1_Q1SER 5    // strapping pin (SDIO timing only)
#define PIN_E1_Q2SER 12
#define PIN_E1_Q3SER 14
#define PIN_E1_Q4SER 15
#define PIN_E2_Q1SER 33
#define PIN_E2_Q2SER 32
#define PIN_E2_Q3SER 3    // UART0 RX: serial is off once the output starts
#define PIN_E2_Q4SER 1    // UART0 TX
#define PIN_SRCLK    4    // shift clock, shared by all 8 registers
#define PIN_RCLK     2    // latch clock, shared (strapping pin)

// Edge node power status inputs (pulled up on the drive node). 39, 36 and
// 35 are input-only pins; 17 is a normal GPIO.
#define PIN_E1_5V_PRESENT 39   // low = the edge node's 5V (595 supply) is up
#define PIN_E2_5V_PRESENT 36
#define PIN_E1_PWR_GOOD   17   // low = the edge node's 12V hot-swap is good
#define PIN_E2_PWR_GOOD   35   //   (reads high, not good, when unplugged)

// Only drive the data lines of edge nodes whose 5V is present (keeps logic
// levels off an unpowered 595). 0 = always drive both.
#define EDGE_POWER_CHECK 1
// How often the power inputs are read, ms.
#define EDGE_POWER_POLL_MS 50

// Sample clock = SRCLK = 80 MHz / I2S_CLK_DIV. 4 -> 20 MHz: a WS2811 bit is
// 3 slots x 8 samples = 1.2 us (T0H 0.4 us, T1H 0.8 us), 28.8 us per pixel.
#define I2S_CLK_DIV 4
// SRCLK polarity: 1 = rising edge mid-sample, as esp_lcd drives the I2S
// LCD write clock. Flip if a scope shows SER changing on the rising edge.
#define SRCLK_INVERT 1
// Lines held low (latch-only slots) after each frame, so the WS2811s latch:
// >= 50 us (datasheet; decision 0013's show-time model assumes 50). Rounds
// up to whole DMA buffers of 57.6 us.
#define LED_RESET_US 50
// LED byte order on the wire, as indices into the protocol's R,G,B. WS2811
// strips here are GRB.
#define LED_COLOR_ORDER 1, 0, 2

// Global brightness, 0-255 (255 = unchanged). The host applies brightness;
// keep this at 255 except for bench tests.
#define GLOBAL_BRIGHTNESS 255

// Blank all strips if no valid packet arrives for this many ms. 0 = never.
#define DATA_TIMEOUT_MS 3000

// ---------------------------------------------------------------------------
//  Network
// ---------------------------------------------------------------------------
#define UDP_PORT 7777

// Static IP: 192.168.1.(100 + NODE_ID)
#define NET_IP      192, 168, 1, (100 + NODE_ID)
#define NET_GATEWAY 192, 168, 1, 1
#define NET_MASK    255, 255, 255, 0

// WT32-ETH01 Ethernet PHY (LAN8720) - don't change for this board
#define ETH_PHY_TYPE_CFG  ETH_PHY_LAN8720
#define ETH_PHY_ADDR_CFG  1
#define ETH_PHY_MDC_CFG   23
#define ETH_PHY_MDIO_CFG  18
#define ETH_PHY_POWER_CFG 16
#define ETH_CLK_MODE_CFG  ETH_CLOCK_GPIO0_IN

// ---------------------------------------------------------------------------
//  OTA
// ---------------------------------------------------------------------------
#define OTA_ENABLED  1
// OTA_PASSWORD is injected via build_flags from secrets.ini (gitignored)
#ifndef OTA_PASSWORD
#error "OTA_PASSWORD not defined - copy secrets.ini.example to secrets.ini"
#endif

// ---------------------------------------------------------------------------
//  Protocol v2 (must match docs/protocol.md and volu_gen/output/protocol.py)
// ---------------------------------------------------------------------------
#define PROTO_MAGIC        0xA7
#define PROTO_VERSION      2
#define TYPE_PIXELS        1
#define TYPE_SYNC          2
#define FLAG_SHOW_NOW      0x01
#define HEADER_SIZE        8
#define PIXELS_HEADER_SIZE 12
#define MAX_PACKET_SIZE    1472   // fits a standard 1500 byte MTU
#define MAX_PIXELS_PER_PACKET ((MAX_PACKET_SIZE - PIXELS_HEADER_SIZE) / 3)  // 486
#define LATE_WINDOW        8      // PIXELS 1..8 frames old are dropped

// ---------------------------------------------------------------------------
//  Tasks / debug
// ---------------------------------------------------------------------------
// Max packets processed per loop pass, so a flood can't starve OTA.
#define MAX_PACKETS_PER_LOOP 64

// The show task (front buffer -> LEDs) runs on the same core as loop(), at
// a higher priority: it copies and starts each show, then blocks while the
// LEDs clock out, so receiving continues during the show.
#define SHOW_TASK_CORE     1
#define SHOW_TASK_PRIORITY 2
#define SHOW_TASK_STACK    4096

// Report packet/frame stats every N ms. 0 = off.
#define STATS_INTERVAL_MS 5000

// UART0's pins (IO1, IO3) carry E2 Q4/Q3 data, so serial output stops when
// the LED output starts (after the boot banner). Stats and status messages
// go as text over UDP to this port on the host that last sent a valid
// packet (listen with tools/node_stats.py). 0 = off.
#define STATS_UDP_PORT 7778

// Bench/debug only: 1 keeps serial on IO1/IO3 and leaves E2 Q3/Q4 (strips
// 48-63) undriven; stats also go to serial. Can be set with -DKEEP_SERIAL=1.
#ifndef KEEP_SERIAL
#define KEEP_SERIAL 0
#endif
