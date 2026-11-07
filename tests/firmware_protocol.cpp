#include "../firmware/voice_assistant/bridge_protocol.h"
#include "../firmware/voice_assistant/wifi_join.h"
#include <array>
#include <cassert>
#include <iostream>
#include <string>
#include <map>
#include <vector>



struct SettingsStore {
  std::map<std::string, std::string> values{{"bridge", "https://old.test"}, {"token", "old-bearer"}};
  std::vector<std::map<std::string, std::string>> durableStates;
  std::string failKey;
  bool failRemove = false;
  size_t writes = 0;

  bool isKey(const char* key) { return values.count(key) != 0; }
  bool remove(const char* key) {
    if (failRemove) return false;
    values.erase(key);
    durableStates.push_back(values);
    return true;
  }
  size_t putString(const char* key, const char* value) {
    ++writes;
    if (failKey == key) return 0;
    values[key] = value;
    durableStates.push_back(values);
    return std::strlen(value);
  }
};

int main() {
  {
    WifiJoinState join;
    assert(join.permits("resume"));
    assert(join.begin());
    // setup -> join -> USB resume/connect must not start a second join or TLS.
    for (const char* command : {"resume", "connect", "wifi", "pair", "ssid", "pass", "forget", "tone"})
      assert(!join.permits(command));
    assert(!join.begin()); // Defensive guard even if a caller bypasses dispatch.
    for (const char* command : {"ready", "status", "pause", "help", "?"})
      assert(join.permits(command));
    join.pause(); // A pause read during resume's join must win even if Wi-Fi gets IP.
    assert(join.cancelled());
    assert(!join.finish(true));
    assert(join.permits("resume"));
    assert(join.begin() && !join.cancelled());
    assert(join.finish(true));
    assert(join.begin());
    assert(!join.finish(false)); // Timeout does not leave the command gate locked.
    assert(join.begin());
    assert(join.finish(true));
  }
  struct UsbRx {
    size_t capacity, requested = 0;
    size_t setRxBufferSize(size_t size) { requested = size; return capacity; }
  };
  for (size_t capacity : {size_t(0), size_t(256), size_t(6000), size_t(6001), size_t(8192)}) {
    UsbRx serial{capacity};
    assert(reserveUsbCommandBuffer(serial) == (capacity >= 6001));
    assert(serial.requested == 8192);
  }
  constexpr size_t caCommandBytes = 3 + ((kMaxRootCaBytes + 2) / 3) * 4 + 1;
  static_assert(caCommandBytes == 5336 && caCommandBytes <= kUsbRxCapacity,
                "Maximum CA burst must fit native USB RX");
  BridgeEndpoint endpoint;
  assert(parseBridgeUrl("https://voice.example.test:8443/", endpoint));
  assert(!std::strcmp(endpoint.host, "voice.example.test") && endpoint.port == 8443);
  assert(parseBridgeUrl("https://localhost", endpoint) && endpoint.port == 443);
  for (const char* bad : {"http://voice.test", "https://user:token@voice.test", "https://127.0.0.1",
                          "https://voice.test:0", "https://voice.test:65536", "https://voice.test:443x",
                          "https://-bad.test", "https://bad-.test", "https://bad..test", "https://voice.test/path",
                          "https://voice.test?key=x", "https://voice.test/#bad", "https://voice.test\r\nInjected: x"}) {
    assert(!parseBridgeUrl(bad, endpoint));
  }
  assert(validDeviceToken(std::string(43, 'a').c_str()));
  assert(!validDeviceToken(std::string(42, 'a').c_str()));
  assert(!validDeviceToken((std::string(42, 'a') + "\n").c_str()));
  uint32_t number;
  assert(decimalBounded("1440044", 1440044, number) && number == 1440044);
  for (const char* bad : {"", "-1", "+1", "1440045", "99999999999999999999", "4x"})
    assert(!decimalBounded(bad, 1440044, number));
  assert(safeStartupVolume(100, false) == 20);
  assert(safeStartupVolume(7, false) == 7);
  assert(safeStartupVolume(-1, true) == 0);
  assert(safeStartupVolume(200, true) == 100);
  assert(safeStartupVolume(80, true) == 80);
  assert(startupHandsFree(false, false)); // Legacy button default must not block hands-free startup.
  assert(!startupHandsFree(true, false)); // A later explicit button-mode choice still wins.
  assert(startupHandsFree(true, true));
  {
    // No power-loss prefix of the real origin-save seam pairs new host and old bearer.
    SettingsStore store;
    assert(storeChangedBridgeOrigin(store, "https://new.test"));
    assert(store.durableStates.size() == 2);
    for (const auto& state : store.durableStates) assert(state.count("token") == 0);
    assert(store.values.at("bridge") == "https://new.test");
    SettingsStore eraseFailure;
    eraseFailure.failRemove = true;
    assert(!storeChangedBridgeOrigin(eraseFailure, "https://new.test"));
    assert(eraseFailure.values.at("bridge") == "https://old.test");
    assert(eraseFailure.values.at("token") == "old-bearer" && eraseFailure.writes == 0);
    SettingsStore originFailure;
    originFailure.failKey = "bridge";
    assert(!storeChangedBridgeOrigin(originFailure, "https://new.test"));
    assert(originFailure.values.at("bridge") == "https://old.test");
    assert(!originFailure.isKey("token"));
    SettingsStore empty;
    assert(storeSettingString(empty, "unset", ""));
    assert(empty.writes == 0);
    assert(storeSettingString(empty, "token", ""));
    assert(!empty.isKey("token"));
    empty.failKey = "token";
    assert(!storeSettingString(empty, "token", "new-bearer"));
    empty.values["token"] = "existing";
    empty.failRemove = true;
    assert(!storeSettingString(empty, "token", ""));
    assert(empty.values.at("token") == "existing");
    assert(kMaxRootCaBytes == 3999);
  }
  {
    const std::string body = R"({"status":"ok","speech":"cartesia-managed-agent","reply":"cartesia-managed-llm"})";
    auto health = [](const std::string& text, size_t declared) {
      return validHealthBody(reinterpret_cast<const uint8_t*>(text.data()), text.size(), declared);
    };
    assert(health(body, body.size()));
    const std::string reordered = " \n{\"reply\":\"direct\", \"speech\":\"local\", \"status\":\"ok\"}\r\n";
    assert(health(reordered, reordered.size()));
    for (size_t n = 0; n < body.size(); ++n) {
      assert(!health(body.substr(0, n), body.size())); // Complete Content-Length is mandatory.
      assert(!health(body.substr(0, n), n)); // Truncated JSON is never readiness.
    }
    assert(!health(body, body.size() - 1));
    assert(!health(body, body.size() + 1));
    assert(health(body + std::string(512 - body.size(), ' '), 512));
    assert(!health(body + std::string(513 - body.size(), ' '), 513));
    const std::string mode64 = "{\"status\":\"ok\",\"speech\":\"" +
                               std::string(64, 'a') + "\",\"reply\":\"direct\"}";
    const std::string mode65 = "{\"status\":\"ok\",\"speech\":\"" +
                               std::string(65, 'a') + "\",\"reply\":\"direct\"}";
    assert(health(mode64, mode64.size()));
    assert(!health(mode65, mode65.size()));
    for (const std::string bad : {
        "", "{}", R"({"status":"not-ok","speech":"local","reply":"direct"})",
        R"({"status":"ok","speech":"","reply":"direct"})",
        R"({"status":"ok","speech":true,"reply":"direct"})",
        R"({"status":"ok","speech":{"status":"ok"},"reply":"direct"})",
        R"({"status":"ok","status":"ok","reply":"direct"})",
        R"({"status":"ok","unknown":"local","reply":"direct"})",
        R"({"status":"ok","speech":"local","reply":"direct","extra":"ok"})",
        R"({"status":"ok","speech":"local","reply":"direct",})",
        R"({"status":"ok","speech":"local","reply":"direct"}garbage)",
        R"({"status":"ok","speech":"local","reply":"direct"}{})",
        R"({"status":"\u006fk","speech":"local","reply":"direct"})"}) {
      assert(!health(bad, bad.size()));
    }
    assert(!health(body + std::string(1, '\0'), body.size() + 1));
    assert(selectMicChannel(0, 0) == -1);
    assert(selectMicChannel(0, 1000000) == 1); // Right-only INMP441 is not silently read as left.
    assert(selectMicChannel(1000000, 0) == 0);
    assert(selectMicChannel(1, 1000000) == 1);
    std::array<int32_t, 8> stereo = {0, 25600, 0, -51200, 0, 76800, 0, -102400};
    double energy[2] = {0, 0};
    accumulateMicEnergy(stereo.data(), stereo.size() / 2, energy);
    assert(energy[0] == 0 && energy[1] == 300000);
    assert(selectMicChannel(energy[0], energy[1]) == 1);
    stereo.fill(0);
    energy[0] = energy[1] = 0;
    accumulateMicEnergy(stereo.data(), stereo.size() / 2, energy);
    assert(selectMicChannel(energy[0], energy[1]) == -1);
  }
  {
    // Scaling is signed little-endian, including unaligned and full-scale inputs.
    std::array<uint8_t, 12> pcm = {0xaa, 0x00, 0x80, 0xff, 0x7f, 0x01,
                                  0xff, 0xff, 0x00, 0xff, 0xff, 0xbb};
    assert(scalePcm16(pcm.data() + 1, 10, 20));
    assert(pcm[1] == 0x67 && pcm[2] == 0xe6); // -6553
    assert(pcm[3] == 0x99 && pcm[4] == 0x19); // 6553
    assert(pcm[5] == 0xcd && pcm[6] == 0xff); // -51
    assert(pcm[7] == 0x33 && pcm[8] == 0x00); // 51
    assert(pcm[9] == 0 && pcm[10] == 0);
    assert(pcm.front() == 0xaa && pcm.back() == 0xbb);
    assert(!scalePcm16(pcm.data() + 1, 9, 20));
    assert(scalePcm16(pcm.data() + 1, 10, 0));
    for (size_t i = 1; i < 11; ++i) assert(pcm[i] == 0);
  }
  std::cout << "Firmware health/schema, durable credentials, channel selection and PCM scaling checks passed. Not an on-board TLS/audio test.\n";
}
