#pragma once
#include <stdint.h>
// =============================================================================
//  edge_power.h - edge node power status inputs (5V_PRESENT, PWR_GOOD)
//  Design: docs/decisions/0014-edge-node-output.md -> Edge node power
// =============================================================================

// Sets up the input pins, takes the first reading and logs it.
void edgePowerInit();

// Re-reads the inputs every EDGE_POWER_POLL_MS (loop task). Returns true
// when the reading changed; the change is logged.
bool edgePowerPoll();

// Chains (edgeShow() mask) of the edge nodes whose 5V is present, as of
// the last reading; all chains if EDGE_POWER_CHECK is 0.
uint8_t edgePowerChains();

// "on" / "on, 12V FAULT" / "off" for edge node 0 (E1) or 1 (E2), as of the
// last reading.
const char *edgePowerState(int edge);
