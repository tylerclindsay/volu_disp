#include <ETH.h>
#include <ArduinoOTA.h>

IPAddress ip(192, 168, 50, 101);
IPAddress gw(0, 0, 0, 0);
IPAddress mask(255, 255, 255, 0);

void setup() {
  Serial.begin(115200);
  ETH.begin(ETH_PHY_LAN8720, 1, 23, 18, 16, ETH_CLOCK_GPIO0_IN);
  ETH.config(ip, gw, mask);
  while (!ETH.linkUp()) delay(100);

  ArduinoOTA.setHostname("wt32-01");
  ArduinoOTA.setPassword("change-me");
  ArduinoOTA.begin();
}

void loop() {
  ArduinoOTA.handle();
}