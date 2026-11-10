// WT32-ETH01 drive node: wire protocol v2 receiver, edge node LED output
// Spec: docs/protocol.md. Modules (docs/decisions/0015-drive-node-firmware-modules.md):
//   receiver    protocol v2 packets -> frame buffers
//   frames      back/pending/front buffers, show task
//   edge_out    I2S1 -> 74HCT595s (decision 0014)
//   edge_power  edge node power status inputs
//   stats, log  periodic stats line; serial, then UDP text
// This file: setup order, the UDP socket, the data timeout, the loop schedule.

#include <Arduino.h>
#include <ETH.h>
#include <WiFiUdp.h>
#include "config.h"
#include "edge_out.h"
#include "edge_power.h"
#include "frames.h"
#include "log.h"
#include "receiver.h"
#include "stats.h"
#if OTA_ENABLED
#include <ArduinoOTA.h>
#endif

static WiFiUDP udp;
static uint8_t rxBuf[MAX_PACKET_SIZE];
static uint32_t lastPacketMs = 0;
static bool blanked = true;   // no data since the last blank

// ---------------------------------------------------------------------------
//  Setup
// ---------------------------------------------------------------------------
static void halt(const char *msg) {
  while (true) {
    logLine("%s", msg);
    delay(2000);
  }
}

// UART0's pins (IO1, IO3) carry E2 Q4/Q3 data: serial must stop before
// edgeAttachPins() routes them to I2S.
static void releaseSerial() {
#if !KEEP_SERIAL
  logLine("serial off: IO1/IO3 now carry LED data; logs go to UDP port %d", STATS_UDP_PORT);
  logSerialOff();
#endif
}

static void initEthernet() {
  ETH.begin(ETH_PHY_TYPE_CFG, ETH_PHY_ADDR_CFG, ETH_PHY_MDC_CFG,
            ETH_PHY_MDIO_CFG, ETH_PHY_POWER_CFG, ETH_CLK_MODE_CFG);
  ETH.config(IPAddress(NET_IP), IPAddress(NET_GATEWAY), IPAddress(NET_MASK));
  while (!ETH.hasIP()) delay(100);
  logLine("IP: %s", ETH.localIP().toString().c_str());
}

static void initOta() {
#if OTA_ENABLED
  ArduinoOTA.setHostname(HOSTNAME);
  ArduinoOTA.setPassword(OTA_PASSWORD);
  ArduinoOTA.onStart([]() { framesBlank(); });
  ArduinoOTA.begin();
#endif
}

void setup() {
  logBegin();
  logLine("%s: protocol v%d, %d strips x %d pixels, SRCLK %u Hz, show %u us", HOSTNAME,
          PROTO_VERSION, NUM_STRIPS, LEDS_PER_STRIP, edgeClockHz(), edgeShowUs());

  if (!framesInit()) halt("ERROR: out of memory for LED buffers");
  if (!edgeInit()) halt("ERROR: LED output init failed (out of memory?)");
  edgePowerInit();
  framesSetChains(edgePowerChains());

  // The LEDs come up before Ethernet, so the 595s' random power-up
  // outputs are cleared as soon as possible.
  releaseSerial();
  edgeAttachPins();
  framesStart();   // starts the show task, then blanks

  initEthernet();
  initOta();
  udp.begin(UDP_PORT);
  logUseUdp(udp);
  logLine("Listening on UDP %u", UDP_PORT);
}

// ---------------------------------------------------------------------------
//  Loop
// ---------------------------------------------------------------------------
// Drains up to MAX_PACKETS_PER_LOOP queued packets.
static void receivePackets() {
  for (uint8_t i = 0; i < MAX_PACKETS_PER_LOOP; i++) {
    int size = udp.parsePacket();
    if (size <= 0) break;
    stats.packets++;
    if (size > (int)sizeof(rxBuf)) {
      udp.clear();
      stats.bad++;
      continue;
    }
    int len = udp.read(rxBuf, sizeof(rxBuf));
    if (len > 0 && handlePacket(rxBuf, len)) {
      lastPacketMs = millis();
      blanked = false;
      logSetHost(udp.remoteIP());
    } else {
      stats.bad++;
    }
  }
}

static void checkDataTimeout() {
#if DATA_TIMEOUT_MS > 0
  if (!blanked && millis() - lastPacketMs > DATA_TIMEOUT_MS) {
    framesBlank();
    receiverReset();
    blanked = true;
  }
#endif
}

// An edge node that just came up has random 595 outputs, so clear them if
// no frames are coming.
static void pollEdgePower() {
  if (!edgePowerPoll()) return;
  framesSetChains(edgePowerChains());
  if (blanked) framesBlank();
}

void loop() {
#if OTA_ENABLED
  ArduinoOTA.handle();
#endif
  receivePackets();
  checkDataTimeout();
  pollEdgePower();
  statsPoll();
}
