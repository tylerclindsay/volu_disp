#pragma once
#include <stdint.h>
#include <atomic>
// =============================================================================
//  stats.h - counters for the periodic stats line, reset each interval
// =============================================================================

struct Stats {
  // Loop task only.
  uint32_t packets;      // UDP packets received
  uint32_t bad;          // dropped as invalid
  uint32_t ignored;      // valid, unknown or reserved type
  uint32_t late;         // PIXELS for a frame 1..LATE_WINDOW behind
  uint32_t clipped;      // pixels past the buffer end
  uint32_t latches;      // SYNC or SHOW_NOW
  uint32_t incomplete;   // latches before the whole frame arrived
  uint32_t overrun;      // latches that never reached the LEDs
  // Written by the show task.
  std::atomic<uint32_t> shows{0}, showUsMax{0}, unpowered{0}, timeouts{0};
};

extern Stats stats;

// Logs and resets the counters every STATS_INTERVAL_MS (loop task).
void statsPoll();
