#include "receiver.h"
#include <Arduino.h>
#include <cstring>
#include "config.h"
#include "frames.h"
#include "stats.h"

static bool haveFrame = false;
static uint32_t currentFrame = 0;     // newest frame number seen
static uint32_t framePixels = 0;      // pixels written for currentFrame

static inline uint16_t rd16(const uint8_t *p) { return p[0] | (p[1] << 8); }
static inline uint32_t rd32(const uint8_t *p) {
  return p[0] | (p[1] << 8) | (p[2] << 16) | ((uint32_t)p[3] << 24);
}

static void latch(uint32_t frame) {
  if (frame != currentFrame || framePixels < BUF_PIXELS) stats.incomplete++;
  framesLatch();
  stats.latches++;
}

// 1..LATE_WINDOW frames behind the newest seen (mod 2^32).
static bool isLate(uint32_t frame) {
  if (!haveFrame) return false;
  const uint32_t age = currentFrame - frame;
  return age >= 1 && age <= LATE_WINDOW;
}

static bool handleSync(size_t len, uint8_t flags, uint32_t frame) {
  if (flags != 0 || len != HEADER_SIZE) return false;
  latch(frame);
  return true;
}

static bool handlePixels(const uint8_t *d, size_t len, uint8_t flags, uint32_t frame) {
  if ((flags & ~FLAG_SHOW_NOW) || len < PIXELS_HEADER_SIZE) return false;
  const uint32_t offset = rd16(d + 8), count = rd16(d + 10);
  if (count == 0 || count > MAX_PIXELS_PER_PACKET) return false;
  if (len != PIXELS_HEADER_SIZE + 3 * count) return false;

  if (isLate(frame)) {
    stats.late++;
    return true;
  }
  if (!haveFrame || frame != currentFrame) {
    haveFrame = true;
    currentFrame = frame;   // a new frame, or a host restart
    framePixels = 0;
  }
  uint32_t n = 0;
  if (offset < BUF_PIXELS) n = min(count, (uint32_t)BUF_PIXELS - offset);
  stats.clipped += count - n;
  if (n) std::memcpy(framesBack() + offset * 3, d + PIXELS_HEADER_SIZE, n * 3);   // wire R,G,B
  framePixels += n;

  if (flags & FLAG_SHOW_NOW) latch(frame);
  return true;
}

bool handlePacket(const uint8_t *d, size_t len) {
  if (len < HEADER_SIZE || d[0] != PROTO_MAGIC || d[1] != PROTO_VERSION) return false;
  const uint8_t type = d[2], flags = d[3];
  const uint32_t frame = rd32(d + 4);
  switch (type) {
    case TYPE_SYNC: return handleSync(len, flags, frame);
    case TYPE_PIXELS: return handlePixels(d, len, flags, frame);
    default:
      stats.ignored++;   // unknown or reserved type: ignored, still a valid packet
      return true;
  }
}

void receiverReset() { haveFrame = false; }

uint32_t receiverFrame() { return currentFrame; }
