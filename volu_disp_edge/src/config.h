#pragma once
#include <Arduino.h>
#include <stdint.h>
// =============================================================================
//  config.h - WT32-ETH01 UDP LED receiver
//  (LED strip layout and pins are declared at the top of main.cpp)
// =============================================================================
//
//  UDP PACKET FORMAT (all multi-byte fields little-endian)
//
//    offset  size  field
//    0       1     magic        always 0xA7 (PACKET_MAGIC)
//    1       1     flags        bit 0 = SHOW: push all strips to the LEDs
//                               after this packet. Set it on the last packet
//                               of a frame so all strips update together.
//    2       1     strip        strip index, 0 .. NUM_STRIPS-1
//    3       2     start        first pixel index this packet writes to
//    5       2     count        number of pixels in this packet
//    7       3*n   pixels       R,G,B for each pixel (n = count)
//
//  A packet can hold at most (MAX_PACKET_SIZE - 7) / 3 = 488 pixels. Longer
//  strips are sent as several packets using 'start'.
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
//  LED behavior
// ---------------------------------------------------------------------------
// Global brightness, 0-255 (255 = unchanged). Applied by FastLED at show time.
#define GLOBAL_BRIGHTNESS 255

// Blank all strips if no valid packet arrives for this many ms. 0 = never.
#define DATA_TIMEOUT_MS 3000

// 1 = push to LEDs after every packet, ignoring the SHOW flag.
// 0 = only when a packet has the SHOW flag set (frame-synced across strips).
#define SHOW_ON_EVERY_PACKET 0

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
#define OTA_PASSWORD "R3ykato"

// ---------------------------------------------------------------------------
//  Protocol / debug
// ---------------------------------------------------------------------------
#define PACKET_MAGIC    0xA7
#define FLAG_SHOW       0x01
#define HEADER_SIZE     7
#define MAX_PACKET_SIZE 1472   // fits a standard 1500 byte MTU

// Max packets processed per loop pass, so a flood can't starve OTA.
#define MAX_PACKETS_PER_LOOP 64

// Print packet/frame stats over serial every N ms. 0 = off.
#define STATS_INTERVAL_MS 5000