#include "edge_power.h"
#include <Arduino.h>
#include "config.h"
#include "edge_out.h"
#include "log.h"

// Bits: 0 E1 5V present, 1 E1 power good, 2 E2 5V present, 3 E2 power good.
static uint8_t power;
static uint32_t lastPollMs;

static uint8_t readPower() {
  return (digitalRead(PIN_E1_5V_PRESENT) == LOW ? 1 : 0) |
         (digitalRead(PIN_E1_PWR_GOOD) == LOW ? 2 : 0) |
         (digitalRead(PIN_E2_5V_PRESENT) == LOW ? 4 : 0) |
         (digitalRead(PIN_E2_PWR_GOOD) == LOW ? 8 : 0);
}

static void logPower() {
  logLine("edge E1 %s, E2 %s", edgePowerState(0), edgePowerState(1));
}

void edgePowerInit() {
  pinMode(PIN_E1_5V_PRESENT, INPUT);
  pinMode(PIN_E2_5V_PRESENT, INPUT);
  pinMode(PIN_E1_PWR_GOOD, INPUT);
  pinMode(PIN_E2_PWR_GOOD, INPUT);
  power = readPower();
  lastPollMs = millis();
  logPower();
}

bool edgePowerPoll() {
  if (millis() - lastPollMs < EDGE_POWER_POLL_MS) return false;
  lastPollMs = millis();
  const uint8_t now = readPower();
  if (now == power) return false;
  power = now;
  logPower();
  return true;
}

uint8_t edgePowerChains() {
#if EDGE_POWER_CHECK
  return ((power & 1) ? EDGE_E1_CHAINS : 0) | ((power & 4) ? EDGE_E2_CHAINS : 0);
#else
  return EDGE_E1_CHAINS | EDGE_E2_CHAINS;
#endif
}

// Power good means nothing without 5V: the pull-up makes it read "not
// good" when the edge node is unplugged.
const char *edgePowerState(int edge) {
  const uint8_t b = power >> (2 * edge);
  if (!(b & 1)) return "off";
  return (b & 2) ? "on" : "on, 12V FAULT";
}
