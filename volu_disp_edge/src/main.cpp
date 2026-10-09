// WT32-ETH01 UDP LED receiver (FastLED)
// Rename to <sketch>.ino for Arduino IDE (keep config.h next to it),
// or place in src/main.cpp for PlatformIO.
// Requires: FastLED (recent version with ESP32 core 3.x support)

#include <Arduino.h>
#include <ETH.h>
#include <WiFiUdp.h>
#include <FastLED.h>
#include "config.h"
#if OTA_ENABLED
#include <ArduinoOTA.h>
#endif

// =============================================================================
//  LED STRIP LAYOUT - edit this block (pins must be literal constants)
// =============================================================================
// Usable data pins on WT32-ETH01: 4, 14, 32, 33.
// Avoid 2, 12, 15 (boot strapping), the Ethernet PHY pins
// (0, 16, 18, 19, 21, 22, 23, 25, 26, 27), and input-only 35, 36, 39.

#define NUM_STRIPS 2
#define NUM_LEDS   10   // buffer size: the longest strip's pixel count

// Pixels actually on each strip (each <= NUM_LEDS, one entry per strip)
static constexpr uint16_t STRIP_LEDS[] = { 10, 10 };

static CRGB leds[NUM_STRIPS][NUM_LEDS];

// One line per strip. Add/remove lines to match NUM_STRIPS.
static void addStrips() {
  FastLED.addLeds<WS2811,  4, GRB>(leds[0], STRIP_LEDS[0]);
  FastLED.addLeds<WS2811, 14, GRB>(leds[1], STRIP_LEDS[1]);
}
// =============================================================================

static_assert(sizeof(STRIP_LEDS) / sizeof(STRIP_LEDS[0]) == NUM_STRIPS,
              "STRIP_LEDS must have exactly NUM_STRIPS entries");
static constexpr bool stripLensFit() {
  for (size_t i = 0; i < NUM_STRIPS; i++)
    if (STRIP_LEDS[i] > NUM_LEDS) return false;
  return true;
}
static_assert(stripLensFit(), "every STRIP_LEDS entry must be <= NUM_LEDS");

static WiFiUDP udp;
static uint8_t rxBuf[MAX_PACKET_SIZE];

static uint32_t lastPacketMs = 0;
static bool blanked = true;

// stats
static uint32_t statPackets = 0, statFrames = 0, statBad = 0, statLastMs = 0;

static void showFrame() {
  FastLED.show();
  statFrames++;
}

static void blankAll() {
  FastLED.clear(true);   // zero the buffers and push black to the strips
}

// Returns true if the packet was valid. Sets 'show' if the frame should latch.
static bool handlePacket(const uint8_t *d, size_t len, bool &show) {
  if (len < HEADER_SIZE || d[0] != PACKET_MAGIC) return false;

  const uint8_t flags = d[1];
  const uint8_t s = d[2];
  const uint16_t start = d[3] | (d[4] << 8);
  uint16_t count = d[5] | (d[6] << 8);

  if (s >= NUM_STRIPS) return false;
  if (len < HEADER_SIZE + (size_t)count * 3) return false;

  const uint16_t n = STRIP_LEDS[s];
  if (start >= n) return false;
  if (start + count > n) count = n - start;

  const uint8_t *p = d + HEADER_SIZE;
  CRGB *strip = leds[s];
  for (uint16_t i = 0; i < count; i++, p += 3) {
    strip[start + i].setRGB(p[0], p[1], p[2]);
  }

  if (flags & FLAG_SHOW) show = true;
  return true;
}

void setup() {
  Serial.begin(115200);
  Serial.println();
  Serial.println(HOSTNAME);

  // LEDs
  addStrips();
  if (FastLED.count() != NUM_STRIPS) {
    while (true) {
      Serial.printf("CONFIG ERROR: addStrips() added %d strips, NUM_STRIPS is %d\n",
                    FastLED.count(), NUM_STRIPS);
      delay(2000);
    }
  }
  FastLED.setBrightness(GLOBAL_BRIGHTNESS);
  blankAll();

  // Ethernet (static IP)
  ETH.begin(ETH_PHY_TYPE_CFG, ETH_PHY_ADDR_CFG, ETH_PHY_MDC_CFG,
            ETH_PHY_MDIO_CFG, ETH_PHY_POWER_CFG, ETH_CLK_MODE_CFG);
  ETH.config(IPAddress(NET_IP), IPAddress(NET_GATEWAY), IPAddress(NET_MASK));
  while (!ETH.hasIP()) delay(100);
  Serial.print("IP: ");
  Serial.println(ETH.localIP());

#if OTA_ENABLED
  ArduinoOTA.setHostname(HOSTNAME);
  ArduinoOTA.setPassword(OTA_PASSWORD);
  ArduinoOTA.onStart([]() { blankAll(); });
  ArduinoOTA.begin();
#endif

  udp.begin(UDP_PORT);
  Serial.printf("Listening on UDP %u: %u strips\n", UDP_PORT, NUM_STRIPS);
  statLastMs = millis();
}

void loop() {
#if OTA_ENABLED
  ArduinoOTA.handle();
#endif

  // Drain everything queued. If frames arrive faster than the LEDs can
  // refresh, intermediate frames are applied in order and only the latest
  // state is shown, which keeps latency low.
  bool show = false;
  for (uint8_t n = 0; n < MAX_PACKETS_PER_LOOP; n++) {
    int size = udp.parsePacket();
    if (size <= 0) break;

    if (size > (int)sizeof(rxBuf)) {
      udp.clear();
      statBad++;
      continue;
    }
    int len = udp.read(rxBuf, sizeof(rxBuf));
    if (len > 0 && handlePacket(rxBuf, len, show)) {
      statPackets++;
      lastPacketMs = millis();
      blanked = false;
#if SHOW_ON_EVERY_PACKET
      show = true;
#endif
    } else {
      statBad++;
    }
  }

  if (show) showFrame();

#if DATA_TIMEOUT_MS > 0
  if (!blanked && millis() - lastPacketMs > DATA_TIMEOUT_MS) {
    blankAll();
    blanked = true;
  }
#endif

#if STATS_INTERVAL_MS > 0
  uint32_t now = millis();
  if (now - statLastMs >= STATS_INTERVAL_MS) {
    float secs = (now - statLastMs) / 1000.0f;
    Serial.printf("pkts/s: %.1f  fps: %.1f  bad: %u\n",
                  statPackets / secs, statFrames / secs, statBad);
    statPackets = statFrames = statBad = 0;
    statLastMs = now;
  }
#endif
}