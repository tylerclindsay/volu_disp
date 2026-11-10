#pragma once
#include <IPAddress.h>
#include <WiFiUdp.h>
// =============================================================================
//  log.h - status and stats lines: serial until the LED output takes UART0's
//  pins, then UDP text to STATS_UDP_PORT (tools/node_stats.py)
// =============================================================================

// Starts serial (115200).
void logBegin();

// printf-style; one line, no trailing newline needed.
void logLine(const char *fmt, ...);

// Flushes and stops serial: IO1/IO3 are about to carry LED data.
void logSerialOff();

// UDP log lines go out through this socket, to the host set by logSetHost()
// (none until then).
void logUseUdp(WiFiUDP &udp);
void logSetHost(IPAddress host);
