#include "log.h"
#include <Arduino.h>
#include <cstdarg>
#include "config.h"

static bool serialOn = false;
static WiFiUDP *logUdp = nullptr;
static IPAddress logHost;   // sender of the last valid packet

void logBegin() {
  Serial.begin(115200);
  Serial.println();
  serialOn = true;
}

void logLine(const char *fmt, ...) {
  char line[256];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(line, sizeof(line), fmt, ap);
  va_end(ap);
  if (serialOn) Serial.println(line);
#if STATS_UDP_PORT > 0
  if (logUdp && logHost != IPAddress()) {
    logUdp->beginPacket(logHost, STATS_UDP_PORT);
    logUdp->printf("%s: %s\n", HOSTNAME, line);
    logUdp->endPacket();
  }
#endif
}

void logSerialOff() {
  Serial.flush();
  Serial.end();
  serialOn = false;
}

void logUseUdp(WiFiUDP &udp) { logUdp = &udp; }

void logSetHost(IPAddress host) { logHost = host; }
