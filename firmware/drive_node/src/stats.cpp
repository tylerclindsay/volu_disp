#include "stats.h"
#include <Arduino.h>
#include "config.h"
#include "edge_out.h"
#include "edge_power.h"
#include "log.h"
#include "receiver.h"

Stats stats;

void statsPoll() {
#if STATS_INTERVAL_MS > 0
  static uint32_t lastMs = millis();
  static uint32_t lateLast = 0;
  const uint32_t now = millis();
  if (now - lastMs < STATS_INTERVAL_MS) return;
  const float secs = (now - lastMs) / 1000.0f;
  const uint32_t late = edgeLateCount();
  Stats &s = stats;
  logLine("pkt/s %.0f  latch/s %.1f  show/s %.1f  show max %u us  "
          "frame %u  incomplete %u  late %u  overrun %u  bad %u  clipped %u  ignored %u  "
          "dma late %u  timeouts %u  unpowered %u  E1 %s  E2 %s",
          s.packets / secs, s.latches / secs, s.shows.load() / secs, s.showUsMax.load(),
          receiverFrame(), s.incomplete, s.late, s.overrun, s.bad, s.clipped, s.ignored,
          late - lateLast, s.timeouts.load(), s.unpowered.load(),
          edgePowerState(0), edgePowerState(1));
  s.packets = s.latches = s.incomplete = s.late = s.bad = s.ignored = s.clipped = s.overrun = 0;
  s.shows = 0;   // racy with the show task; fine for stats
  s.showUsMax = 0;
  s.timeouts = 0;
  s.unpowered = 0;
  lateLast = late;
  lastMs = now;
#endif
}
