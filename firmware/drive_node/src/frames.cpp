#include "frames.h"
#include <Arduino.h>
#include <atomic>
#include <cstring>
#include <utility>
#include "edge_out.h"
#include "stats.h"

static uint8_t *back, *pending, *front;   // R,G,B per pixel
static SemaphoreHandle_t pendingLock;
static TaskHandle_t showTask;
static bool pendingValid = false;               // under pendingLock
static std::atomic<bool> blankRequest{false};
static std::atomic<uint8_t> chainMask{0};       // chains of powered edge nodes

// Takes the newest latched frame (or a blank request) into front. Returns
// false if there is nothing to show.
static bool takeFrame() {
  xSemaphoreTake(pendingLock, portMAX_DELAY);
  const bool blank = blankRequest, have = pendingValid;
  blankRequest = false;
  pendingValid = false;
  if (!blank && have) std::swap(pending, front);
  xSemaphoreGive(pendingLock);
  if (blank) std::memset(front, 0, BUF_BYTES);
  return blank || have;
}

static void showFront() {
  const uint32_t t0 = micros();
  const EdgeResult r = edgeShow(front, chainMask);
  if (r == EDGE_NO_CHAINS) {   // no powered edge node: lines stay idle
    stats.unpowered++;
    return;
  }
  if (r == EDGE_TIMEOUT) stats.timeouts++;
  const uint32_t us = micros() - t0;
  if (us > stats.showUsMax) stats.showUsMax = us;
  stats.shows++;
}

// Show task: pending <-> front -> LEDs.
static void showLoop(void *) {
  for (;;) {
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    if (takeFrame()) showFront();
  }
}

bool framesInit() {
  // Only the first BUF_BYTES are ever written: the padding strips stay black.
  back = (uint8_t *)calloc(EDGE_FRAME_BYTES, 1);
  pending = (uint8_t *)calloc(EDGE_FRAME_BYTES, 1);
  front = (uint8_t *)calloc(EDGE_FRAME_BYTES, 1);
  pendingLock = xSemaphoreCreateMutex();
  return back && pending && front && pendingLock;
}

void framesStart() {
  xTaskCreatePinnedToCore(showLoop, "show", SHOW_TASK_STACK, nullptr, SHOW_TASK_PRIORITY,
                          &showTask, SHOW_TASK_CORE);
  framesBlank();
}

uint8_t *framesBack() { return back; }

void framesLatch() {
  xSemaphoreTake(pendingLock, portMAX_DELAY);
  if (pendingValid) stats.overrun++;   // the previous latch never reached the LEDs
  std::memcpy(pending, back, BUF_BYTES);
  pendingValid = true;
  xSemaphoreGive(pendingLock);
  xTaskNotifyGive(showTask);
}

void framesBlank() {
  std::memset(back, 0, BUF_BYTES);  // regions without new data stay dark
  blankRequest = true;
  xTaskNotifyGive(showTask);
}

void framesSetChains(uint8_t chains) { chainMask = chains; }
