#pragma once
#include <cstddef>
#include <cstdint>
#include <cstring>

struct BridgeEndpoint {
  char host[254] = {};
  uint16_t port = 443;
};

// Old firmware could retain volume=100/auto=true. First secure-v1 load is quiet.
inline int safeStartupVolume(int stored, bool reviewed) {
  const int ceiling = reviewed ? 100 : 20;
  return stored < 0 ? 0 : (stored > ceiling ? ceiling : stored);
}

// Migrate the legacy button default once; retain later deliberate mode choices.
inline bool startupHandsFree(bool hasChoice, bool autoMode) {
  return !hasChoice || autoMode;
}

// NVS strings allow 4000 bytes including the terminating NUL.
constexpr size_t kMaxRootCaBytes = 3999;
// Native USB must hold one whole command while the loop is sleeping.
constexpr size_t kSerialCommandLimit = 6000;
constexpr size_t kUsbRxCapacity = 8192;
static_assert(kUsbRxCapacity >= kSerialCommandLimit + 1, "USB RX must include newline");

template <typename UsbSerial>
inline bool reserveUsbCommandBuffer(UsbSerial& serial) {
  return serial.setRxBufferSize(kUsbRxCapacity) >= kSerialCommandLimit + 1;
}

// Empty putString returns zero on success AND failure; delete an existing key instead.
template <typename Store>
inline bool storeSettingString(Store& store, const char* key, const char* value) {
  if (!*value) return !store.isKey(key) || store.remove(key);
  return store.putString(key, value) == std::strlen(value);
}

// The old bearer must be durably absent before an origin can change, even on power loss.
// Caller clears its in-memory bearer before invoking this with an already-open store.
template <typename Store>
inline bool storeChangedBridgeOrigin(Store& store, const char* origin) {
  return storeSettingString(store, "token", "") &&
         storeSettingString(store, "bridge", origin);
}

inline void accumulateMicEnergy(const int32_t* stereo, size_t samples, double (&energy)[2]) {
  for (size_t i = 0; i < samples; ++i) {
    for (int channel = 0; channel < 2; ++channel) {
      double value = static_cast<double>(stereo[i * 2 + channel] >> 8);
      energy[channel] += value * value;
    }
  }
}

inline int selectMicChannel(double leftEnergy, double rightEnergy) {
  if (leftEnergy <= 0 && rightEnergy <= 0) return -1;
  return rightEnergy > leftEnergy ? 1 : 0;
}

// Exact bounded health schema: one object, three unique string fields, no extensions.
// Bridge mode names are nonempty ASCII identifiers; escaped/control text is not a mode.
inline bool validHealthBody(const uint8_t* body, size_t received, size_t declared) {
  if (!declared || declared > 512 || received != declared) return false;
  size_t pos = 0;
  auto whitespace = [&]() {
    while (pos < received && (body[pos] == ' ' || body[pos] == '\t' ||
                             body[pos] == '\r' || body[pos] == '\n')) ++pos;
  };
  auto take = [&](uint8_t ch) {
    whitespace();
    if (pos >= received || body[pos] != ch) return false;
    ++pos;
    return true;
  };
  auto identifier = [&](char* output, size_t capacity) {
    if (!take('"')) return false;
    size_t length = 0;
    while (pos < received && body[pos] != '"') {
      uint8_t ch = body[pos++];
      if (!((ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') ||
            (ch >= '0' && ch <= '9') || ch == '-' || ch == '_') ||
          length + 1 >= capacity) return false;
      output[length++] = ch;
    }
    if (!length || pos >= received) return false;
    output[length] = 0;
    ++pos;
    return true;
  };
  if (!take('{')) return false;
  unsigned fields = 0;
  for (int i = 0; i < 3; ++i) {
    char key[8], value[65];
    if ((i && !take(',')) || !identifier(key, sizeof(key)) || !take(':') ||
        !identifier(value, sizeof(value))) return false;
    unsigned field = !std::strcmp(key, "status") ? 1 :
                     !std::strcmp(key, "speech") ? 2 :
                     !std::strcmp(key, "reply") ? 4 : 0;
    if (!field || (fields & field) || (field == 1 && std::strcmp(value, "ok"))) return false;
    fields |= field;
  }
  if (fields != 7 || !take('}')) return false;
  whitespace();
  return pos == received;
}


// PCM16 is little-endian, including negative full-scale samples; no alignment assumed.
inline bool scalePcm16(uint8_t* pcm, size_t bytes, int volume) {
  if ((bytes & 1) || volume < 0 || volume > 100) return false;
  if (volume == 100) return true;
  for (size_t i = 0; i < bytes; i += 2) {
    int32_t sample = pcm[i] | (uint32_t(pcm[i + 1]) << 8);
    if (sample >= 32768) sample -= 65536;
    uint16_t scaled = static_cast<uint16_t>(sample * volume / 100);
    pcm[i] = scaled & 0xff;
    pcm[i + 1] = scaled >> 8;
  }
  return true;
}


inline bool decimalBounded(const char* text, uint32_t maximum, uint32_t& value) {
  value = 0;
  if (!text || !*text) return false;
  for (; *text; ++text) {
    if (*text < '0' || *text > '9') return false;
    uint32_t digit = *text - '0';
    if (value > maximum / 10 || (value == maximum / 10 && digit > maximum % 10)) return false;
    value = value * 10 + digit;
  }
  return true;
}

inline bool validDeviceToken(const char* token) {
  if (std::strlen(token) != 43) return false;
  for (size_t i = 0; i < 43; ++i) {
    char c = token[i];
    if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
          (c >= '0' && c <= '9') || c == '-' || c == '_')) return false;
  }
  return true;
}

inline bool parseBridgeUrl(const char* url, BridgeEndpoint& endpoint) {
  if (std::strncmp(url, "https://", 8) != 0) return false;
  const char* host = url + 8;
  const char* end = host;
  while (*end && *end != ':' && *end != '/') ++end;
  size_t length = end - host;
  if (!length || length > 253) return false;
  size_t label = 0;
  bool hasLetter = false;
  for (size_t i = 0; i < length; ++i) {
    char c = host[i];
    bool letter = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z');
    bool digit = c >= '0' && c <= '9';
    hasLetter = hasLetter || letter;
    if (c == '.') {
      if (!label || host[i - 1] == '-') return false;
      label = 0;
    } else {
      if ((!letter && !digit && c != '-') || (!label && c == '-') || ++label > 63) return false;
    }
  }
  if (!label || host[length - 1] == '-' || !hasLetter) return false;
  uint32_t port = 443;
  if (*end == ':') {
    const char* start = ++end;
    while (*end && *end != '/') ++end;
    char digits[6] = {};
    size_t n = end - start;
    if (!n || n > 5) return false;
    std::memcpy(digits, start, n);
    if (!decimalBounded(digits, 65535, port) || !port) return false;
  }
  if (*end && !(*end == '/' && !end[1])) return false;
  std::memcpy(endpoint.host, host, length);
  endpoint.host[length] = 0;
  endpoint.port = static_cast<uint16_t>(port);
  return true;
}

