#pragma once
#include <stddef.h>
#include <stdint.h>
// =============================================================================
//  receiver.h - wire protocol v2 packets into the frame buffers (loop task)
//  Spec: docs/protocol.md
// =============================================================================

// Applies one packet. Returns false for a packet the spec says to drop.
bool handlePacket(const uint8_t *d, size_t len);

// Forgets the current frame, e.g. after the data timeout, so the next
// PIXELS start a new one whatever its number.
void receiverReset();

// Newest frame number seen.
uint32_t receiverFrame();
