#pragma once
#include <stdint.h>
#include "config.h"
// =============================================================================
//  frames.h - the node's frame buffers and the show task
// =============================================================================
//
// Buffers (each NUM_STRIPS * LEDS_PER_STRIP RGB pixels, flat, protocol order,
// padded with black strips to EDGE_STRIPS for the output):
//   back     PIXELS write here (loop task only)
//   pending  the latest latched frame, waiting for the LEDs (mutex)
//   front    being clocked out (show task only)
// A latch (SYNC or SHOW_NOW) copies back -> pending at once, so the next
// frame's PIXELS can arrive while the LEDs are still busy; when the LEDs
// are free the show task swaps pending and front and shows front. If
// several latches come during one show, only the newest is shown
// (protocol.md -> Frames).

#define BUF_PIXELS (NUM_STRIPS * LEDS_PER_STRIP)
#define BUF_BYTES  (BUF_PIXELS * 3)
static_assert(NUM_STRIPS >= 1 && NUM_STRIPS <= 64, "a drive node has 1..64 strips");
static_assert(BUF_PIXELS <= 65536, "PIXELS offset is a u16");

// Allocates the buffers. Returns false if out of memory.
bool framesInit();

// Starts the show task and blanks the LEDs (595 outputs are random at
// power-up). After edgeInit() and edgeAttachPins().
void framesStart();

// The back buffer (BUF_BYTES, R,G,B per pixel); loop task only.
uint8_t *framesBack();

// Copies back -> pending and wakes the show task (loop task).
void framesLatch();

// Clears the back buffer and shows black (loop task).
void framesBlank();

// Chains to drive (edgeShow() mask), from the next show on.
void framesSetChains(uint8_t chains);
