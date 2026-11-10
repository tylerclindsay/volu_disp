#pragma once
#include <stdint.h>
#include "config.h"
// =============================================================================
//  edge_out.h - LED output through the edge nodes' 74HCT595 shift registers
//  Design: docs/decisions/0014-edge-node-output.md
// =============================================================================
//
// I2S1 in LCD (parallel) mode drives 8 serial data lines, one per 595
// ("chain" c = strip / 8: E1 Q1..Q4, then E2 Q1..Q4), plus RCLK; the I2S
// sample clock is SRCLK. Strip s is on output s % 8 (A..H) of chain s / 8,
// i.e. edge node s / 32, connector J(s % 32 + 1).
//
// Use: edgeInit() once, edgeAttachPins() once UART0 is released (IO1/IO3
// carry LED data), then edgeShow() per frame, all from one task.

// Chain mask bits for each edge node.
#define EDGE_E1_CHAINS 0x0F
#define EDGE_E2_CHAINS 0xF0

// Strips in a frame buffer for edgeShow(): NUM_STRIPS rounded up to whole
// chains. Strips past NUM_STRIPS must stay 0 (black).
#define EDGE_STRIPS (((NUM_STRIPS) + 7) / 8 * 8)
#define EDGE_FRAME_BYTES (EDGE_STRIPS * LEDS_PER_STRIP * 3)

// Allocates the DMA ring and sets up I2S1 and its interrupt (on the
// calling core). Returns false if out of memory or the interrupt can't be
// allocated.
bool edgeInit();

// Routes the I2S1 signals to the data, SRCLK and RCLK pins.
void edgeAttachPins();

enum EdgeResult { EDGE_SHOWN, EDGE_NO_CHAINS, EDGE_TIMEOUT };

// Clocks a frame out and blocks until done, including the reset (latch)
// time. rgb: EDGE_FRAME_BYTES, strip-major R,G,B (protocol order); it is
// read during the show, so it must not change until this returns. Chains
// outside chainMask (unpowered edge nodes), without strips, or not routed
// (KEEP_SERIAL) are held low; EDGE_NO_CHAINS if that leaves none.
EdgeResult edgeShow(const uint8_t *rgb, uint8_t chainMask);

// Expected duration of edgeShow(), in µs.
uint32_t edgeShowUs();

// Sample clock (SRCLK) in Hz.
uint32_t edgeClockHz();

// Times since boot the DMA interrupt ran too late to refill a buffer
// (that frame was glitched).
uint32_t edgeLateCount();
