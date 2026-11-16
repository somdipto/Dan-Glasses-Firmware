// ==========================================
// ESP32-S3 VOICE ASSISTANT
// XIAO ESP32-S3 + INMP441 mic + MAX98357A amp
//
// Listens all the time, records while you speak (stops after a pause),
// sends the WAV to the laptop server, plays the spoken reply, repeats.
//
// Wi-Fi and server settings are stored on the board, not in this file:
//   - USB:   python tools/device.py setup
//   - Phone: join the "VoiceAssistant-XXXX" Wi-Fi and fill in the form
//            (opens automatically when no Wi-Fi works; or hold BOOT 3 s)
// Type "help" in the Serial Monitor (115200) for all commands.
// ==========================================

#include <Arduino.h>
#include <ESP_I2S.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <Preferences.h>
#include <algorithm>
#include <cstring>

#define FW_VERSION "2.5"

I2SClass I2S;

// ==========================================
// PINS (XIAO labels in brackets)
// ==========================================
// INMP441 MICROPHONE
// Clock pins SWAPPED to match this build's wiring: SCK/BCLK on D2, WS/LRC on D1.
#define MIC_BCLK 3   // SCK  [D2]
#define MIC_WS   2   // WS   [D1]
#define MIC_SD   1   // SD   [D0]

// MAX98357A SPEAKER (shares BCLK/WS with the mic)
#define SPK_BCLK 3   // BCLK [D2]
#define SPK_WS   2   // LRC  [D1]
#define SPK_DIN  4   // DIN  [D3]

#define BOOT_BUTTON 0
#ifdef LED_BUILTIN
#define STATUS_LED LED_BUILTIN   // XIAO ESP32-S3: GPIO21, active LOW
#else
#define STATUS_LED 21
#endif

// ==========================================
// AUDIO / SPEECH DETECTION
// ==========================================
#define SAMPLE_RATE    16000
#define FRAME_SAMPLES  320                        // 20 ms
#define FRAME_MS       20
#define PREROLL_FRAMES 15                         // 300 ms kept before speech
#define START_FRAMES   4                          // 80 ms loud = speech started
#define MIN_SPEECH_MS  300                        // shorter blips are ignored
#define TAIL_KEEP_MS   300                        // silence kept at the end
#define MAX_RECORD_MS  7000    // was 10 s; long clips are almost always background chatter
#define WAV_HEADER     44

#define DISCOVERY_PORT 5001
#define PORTAL_TIMEOUT_MS (10UL * 60UL * 1000UL)

// ==========================================
// SETTINGS (saved in flash)
// ==========================================
Preferences prefs;

struct Settings {
  String ssid;
  String pass;
  String server;        // "" = find the laptop automatically
  int volume = 80;      // 0-100
  // Defaults tuned on real hardware (INMP441 next to the face): gain 16 clipped,
  // a 2 s pause made replies feel slow, sensitivity 5 caught too much chatter.
  int silenceMs = 800;  // pause that ends a question
  int sensitivity = 4;  // 1 (needs loud voice) .. 10 (very sensitive)
  int micGain = 6;      // digital mic gain
  bool handsFree = true; // false = only the BOOT button starts recording
  float voiceRef = 0;    // your speaking level from 'calibrate' (0 = none)
  bool listen = true;
  bool compress = true;  // send IMA ADPCM (4x smaller) instead of 16-bit WAV
} cfg;

void loadSettings() {
  prefs.begin("va", true);
  cfg.ssid = prefs.getString("ssid", "");
  cfg.pass = prefs.getString("pass", "");
  cfg.server = prefs.getString("server", "");
  cfg.volume = prefs.getInt("volume", 80);
  cfg.silenceMs = prefs.getInt("silence", 800);
  cfg.sensitivity = prefs.getInt("sens", 4);
  cfg.micGain = prefs.getInt("gain", 6);
  cfg.handsFree = prefs.getBool("auto", true);
  cfg.voiceRef = prefs.getFloat("voice", 0);
  cfg.compress = prefs.getBool("adpcm", true);
  prefs.end();
}

void saveSettings() {
  prefs.begin("va", false);
  prefs.putString("ssid", cfg.ssid);
  prefs.putString("pass", cfg.pass);
  prefs.putString("server", cfg.server);
  prefs.putInt("volume", cfg.volume);
  prefs.putInt("silence", cfg.silenceMs);
  prefs.putInt("sens", cfg.sensitivity);
  prefs.putInt("gain", cfg.micGain);
  prefs.putBool("auto", cfg.handsFree);
  prefs.putFloat("voice", cfg.voiceRef);
  prefs.putBool("adpcm", cfg.compress);
  prefs.end();
}

// ==========================================
// STATE
// ==========================================
IPAddress serverIp;
uint16_t serverPort = 5000;
bool serverFound = false;
unsigned long lastServerSearch = 0;
unsigned long lastWifiReport = 0;
bool wifiWasConnected = false;

uint8_t* recording = nullptr;   // WAV header + PCM, in PSRAM
size_t recordCapacity = 0;      // samples

int32_t micRaw[FRAME_SAMPLES * 2];
int16_t frame[FRAME_SAMPLES];
int16_t preroll[PREROLL_FRAMES][FRAME_SAMPLES];
int prerollHead = 0;
int prerollCount = 0;

int micChannel = -1;            // 0 = left, 1 = right, -1 = not detected
float noiseFloor = 0;
int loudFrames = 0;
bool talkRequested = false;     // BOOT button short press
uint32_t clippedSamples = 0;    // samples that hit full scale
bool quietMicCheck = false;     // background re-checks don't print warnings

WebServer web(80);
DNSServer dns;
bool portalActive = false;
unsigned long portalStarted = 0;
String portalNetworks;

String serialLine;

void handleSerial();

// ==========================================
// LED (active LOW on the XIAO)
// ==========================================
void led(bool on) {
  digitalWrite(STATUS_LED, on ? LOW : HIGH);
}

// ==========================================
// WAV HELPERS
// ==========================================
uint16_t rd16(const uint8_t* p) {
  return (uint16_t)p[0] |
         ((uint16_t)p[1] << 8);
}

uint32_t rd32(const uint8_t* p) {
  return (uint32_t)p[0] |
         ((uint32_t)p[1] << 8) |
         ((uint32_t)p[2] << 16) |
         ((uint32_t)p[3] << 24);
}

void wr16(uint8_t* p, uint16_t v) {
  p[0] = v & 0xff;
  p[1] = v >> 8;
}

void wr32(uint8_t* p, uint32_t v) {
  for (int i = 0; i < 4; i++) p[i] = (v >> (8 * i)) & 0xff;
}

bool isWav(const uint8_t* p, size_t n) {
  return p && n >= 12 &&
         memcmp(p, "RIFF", 4) == 0 &&
         memcmp(p + 8, "WAVE", 4) == 0;
}

void writeWavHeader(uint8_t* h, uint32_t pcmBytes) {
  memcpy(h, "RIFF", 4);
  wr32(h + 4, 36 + pcmBytes);
  memcpy(h + 8, "WAVEfmt ", 8);
  wr32(h + 16, 16);
  wr16(h + 20, 1);                  // PCM
  wr16(h + 22, 1);                  // mono
  wr32(h + 24, SAMPLE_RATE);
  wr32(h + 28, SAMPLE_RATE * 2);
  wr16(h + 32, 2);
  wr16(h + 34, 16);
  memcpy(h + 36, "data", 4);
  wr32(h + 40, pcmBytes);
}

// ==========================================
// READ EXACT BYTES
// ==========================================
bool readExact(WiFiClient* s, uint8_t* dst, size_t n) {
  size_t done = 0;
  unsigned long lastData = millis();

  while (done < n) {
    int avail = s->available();

    if (avail > 0) {
      size_t want = min(n - done, (size_t)avail);
      int got = s->read(dst + done, want);

      if (got > 0) {
        done += got;
        lastData = millis();
      }
    } else {
      if (!s->connected() && s->available() == 0) {
        Serial.println("Stream ended early");
        return false;
      }

      if (millis() - lastData > 45000) {
        Serial.println("Read timeout");
        return false;
      }

      delay(2);
    }
  }

  return true;
}

// ==========================================
// SKIP BYTES
// ==========================================
bool skipBytes(WiFiClient* s, uint32_t n) {
  uint8_t temp[256];

  while (n > 0) {
    size_t amount = min(n, (uint32_t)sizeof(temp));

    if (!readExact(s, temp, amount)) {
      return false;
    }

    n -= amount;
  }

  return true;
}

// ==========================================
// SPEECH-BAND FILTER
// Two cascaded 2nd-order high-pass sections at 200 Hz. Removes DC,
// table/fan rumble and hum below the voice band, which otherwise
// keeps the speech detector triggered.
// ==========================================
#define HP_CUTOFF_HZ 200.0f

struct Biquad {
  float b0, b1, b2, a1, a2;
  float x1 = 0, x2 = 0, y1 = 0, y2 = 0;

  void highPass(float fc, float q) {
    float w = 2 * PI * fc / SAMPLE_RATE;
    float alpha = sinf(w) / (2 * q);
    float c = cosf(w);
    float a0 = 1 + alpha;
    b0 = (1 + c) / 2 / a0;
    b1 = -(1 + c) / a0;
    b2 = (1 + c) / 2 / a0;
    a1 = -2 * c / a0;
    a2 = (1 - alpha) / a0;
  }

  void reset() { x1 = x2 = y1 = y2 = 0; }

  float process(float x) {
    float y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2;
    x2 = x1; x1 = x;
    y2 = y1; y1 = y;
    return y;
  }
};

Biquad hp1, hp2;

void setupFilter() {
  // Butterworth 4th order: Q values of the two sections.
  hp1.highPass(HP_CUTOFF_HZ, 0.5412f);
  hp2.highPass(HP_CUTOFF_HZ, 1.3066f);
}

// ==========================================
// I2S: MICROPHONE / SPEAKER
// They share BCLK and WS, so only one runs at a time.
// ==========================================
bool startMic() {
  I2S.end();

  I2S.setPins(MIC_BCLK, MIC_WS, -1, MIC_SD);

  // INMP441 sends 24-bit samples in 32-bit slots (64 clocks per frame).
  bool ok = I2S.begin(
    I2S_MODE_STD,
    SAMPLE_RATE,
    I2S_DATA_BIT_WIDTH_32BIT,
    I2S_SLOT_MODE_STEREO
  );

  // Hold the amp's data line low so the speaker stays silent
  // while the shared clocks run.
  pinMode(SPK_DIN, OUTPUT);
  digitalWrite(SPK_DIN, LOW);

  hp1.reset();
  hp2.reset();
  return ok;
}

bool startSpeaker(uint32_t sampleRate, uint16_t channels) {
  I2S.end();

  I2S.setPins(SPK_BCLK, SPK_WS, SPK_DIN, -1);

  return I2S.begin(
    I2S_MODE_STD,
    sampleRate,
    I2S_DATA_BIT_WIDTH_16BIT,
    channels == 1
      ? I2S_SLOT_MODE_MONO
      : I2S_SLOT_MODE_STEREO
  );
}

// Push a little silence through so the amp doesn't click or repeat
// the last buffer when I2S stops.
void speakerFlush(uint16_t channels) {
  uint8_t zeros[512] = {0};
  for (int i = 0; i < 6 * channels; i++) I2S.write(zeros, sizeof(zeros));
}

void applyVolume(uint8_t* pcm, size_t bytes) {
  if (cfg.volume >= 100) return;
  int16_t* s = (int16_t*)pcm;
  size_t n = bytes / 2;
  int32_t v = cfg.volume;
  for (size_t i = 0; i < n; i++) s[i] = (int16_t)((int32_t)s[i] * v / 100);
}

// Read one 20 ms frame from the mic into `frame` (16-bit, filtered,
// gain applied). Returns the frame's RMS level.
float readMicFrame() {
  size_t want = sizeof(micRaw);
  size_t got = 0;
  while (got < want) {
    size_t n = I2S.readBytes((char*)micRaw + got, want - got);
    if (n == 0) break;
    got += n;
  }

  int ch = micChannel < 0 ? 0 : micChannel;
  float gain = cfg.micGain / 256.0f;  // 24-bit -> 16-bit, then gain
  double sum = 0;

  for (int i = 0; i < FRAME_SAMPLES; i++) {
    float x = (float)(micRaw[i * 2 + ch] >> 8);  // 24-bit sample
    float y = hp2.process(hp1.process(x)) * gain;
    if (y > 32767) { y = 32767; clippedSamples++; }
    if (y < -32768) { y = -32768; clippedSamples++; }
    frame[i] = (int16_t)y;
    sum += (double)y * y;
  }

  return sqrtf(sum / FRAME_SAMPLES);
}

// The INMP441's L/R pin decides which half of the frame it talks in.
// Measure both so either wiring works.
void detectMicChannel() {
  double energy[2] = {0, 0};
  for (int f = 0; f < 15; f++) {
    size_t got = 0;
    while (got < sizeof(micRaw)) {
      size_t n = I2S.readBytes((char*)micRaw + got, sizeof(micRaw) - got);
      if (n == 0) break;
      got += n;
    }
    if (f < 5) continue;  // mic wake-up time
    for (int i = 0; i < FRAME_SAMPLES; i++) {
      for (int c = 0; c < 2; c++) {
        double v = (double)(micRaw[i * 2 + c] >> 8);
        energy[c] += v * v;
      }
    }
  }

  if (energy[0] == 0 && energy[1] == 0) {
    micChannel = -1;
    if (!quietMicCheck) {
      Serial.println("[mic] NO SIGNAL - check wiring: SD->D0, SCK->D2, "
                     "WS->D1, VDD->3V3, GND->GND, L/R->GND. "
                     "Type 'micdiag' for details.");
    }
    return;
  }

  micChannel = energy[1] > energy[0] ? 1 : 0;
  Serial.printf("[mic] ok, using %s channel\n",
                micChannel ? "right" : "left");
}

// Let the mic settle and learn the room's background noise level.
// After its clock starts the INMP441 needs about a second before its
// output is clean.
#define MIC_SETTLE_FRAMES 50

void calibrateNoise() {
  for (int i = 0; i < MIC_SETTLE_FRAMES; i++) readMicFrame();

  // Average the quieter 60% of frames so a cough or click
  // during calibration doesn't raise the threshold.
  const int n = 25;
  float levels[n];
  for (int i = 0; i < n; i++) levels[i] = readMicFrame();
  std::sort(levels, levels + n);
  float sum = 0;
  int keep = n * 6 / 10;
  for (int i = 0; i < keep; i++) sum += levels[i];
  noiseFloor = max(sum / keep, 2.0f);
  loudFrames = 0;
  prerollCount = 0;
}

// After 'calibrate', speech must reach this fraction of your own level.
// People further away are quieter, so they stay below it.
float voiceFraction() {
  return 0.15f + (10 - cfg.sensitivity) * 0.05f;        // 0.60 .. 0.15
}

float startThreshold() {
  if (cfg.voiceRef > 0) {
    return max(noiseFloor * 2.0f, cfg.voiceRef * voiceFraction());
  }
  float ratio = 1.5f + (10 - cfg.sensitivity) * 0.5f;   // 6.0 .. 1.5
  float minimum = 3000.0f / (cfg.sensitivity + 1);      // 1500 .. 270
  return max(noiseFloor * ratio, minimum);
}

float keepThreshold() {
  return max(noiseFloor * 2.0f, startThreshold() * 0.5f);
}

// ==========================================
// WAV STREAM PLAYBACK
// PCM 16-bit mono/stereo
// ==========================================
bool playWavStream(WiFiClient* stream, size_t totalBytes) {
  if (totalBytes < 12) {
    Serial.println("Response too small");
    return false;
  }

  uint8_t header[12];

  if (!readExact(stream, header, 12)) {
    return false;
  }

  if (!isWav(header, 12)) {
    Serial.println("Invalid RIFF/WAVE");
    return false;
  }

  size_t consumed = 12;

  bool haveFmt = false;

  uint16_t format = 0;
  uint16_t channels = 0;
  uint16_t bits = 0;
  uint16_t align = 0;
  uint32_t sampleRate = 0;

  uint8_t fmt[16];
  uint8_t audioBuffer[1024];

  while (consumed + 8 <= totalBytes) {
    uint8_t chunk[8];

    if (!readExact(stream, chunk, 8)) {
      return false;
    }

    consumed += 8;

    uint32_t chunkSize = rd32(chunk + 4);
    size_t remainingBody = totalBytes - consumed;

    bool isData =
      memcmp(chunk, "data", 4) == 0;

    // --------------------------------------
    // FORMAT CHUNK
    // --------------------------------------
    if (memcmp(chunk, "fmt ", 4) == 0) {
      if (chunkSize < 16 ||
          chunkSize > remainingBody) {
        Serial.println("Invalid fmt chunk");
        return false;
      }

      if (!readExact(stream, fmt, 16)) {
        return false;
      }

      consumed += 16;

      format = rd16(fmt);
      channels = rd16(fmt + 2);
      sampleRate = rd32(fmt + 4);
      align = rd16(fmt + 12);
      bits = rd16(fmt + 14);

      uint32_t extra = chunkSize - 16;

      if (extra > 0) {
        if (!skipBytes(stream, extra)) {
          return false;
        }
        consumed += extra;
      }

      // RIFF padding
      if (chunkSize & 1) {
        if (consumed >= totalBytes ||
            !skipBytes(stream, 1)) {
          Serial.println("Missing fmt padding");
          return false;
        }
        consumed++;
      }

      if (format != 1 || bits != 16) {
        Serial.println("Need PCM 16-bit WAV");
        return false;
      }

      if (channels != 1 && channels != 2) {
        Serial.println("Unsupported channels");
        return false;
      }

      if (sampleRate < 8000 || sampleRate > 48000) {
        Serial.println("Unsupported sample rate");
        return false;
      }

      if (align != channels * 2) {
        Serial.println("Invalid PCM alignment");
        return false;
      }

      haveFmt = true;
    }

    // --------------------------------------
    // AUDIO DATA
    // --------------------------------------
    else if (isData) {
      if (!haveFmt) {
        Serial.println("fmt chunk missing");
        return false;
      }

      // If declared WAV size is bigger than
      // actual HTTP body, clamp to body.
      size_t playable = min(
        (size_t)chunkSize,
        remainingBody
      );

      playable -= playable % align;

      if (playable == 0) {
        Serial.println("No PCM data");
        return false;
      }

      if (!startSpeaker(sampleRate, channels)) {
        Serial.println("Speaker I2S failed");
        return false;
      }

      Serial.printf("[speaking] %.1f s at %lu Hz\n",
                    playable / (float)(sampleRate * align),
                    (unsigned long)sampleRate);

      size_t left = playable;

      while (left > 0) {
        size_t amount = min(
          left,
          sizeof(audioBuffer)
        );

        amount -= amount % align;

        if (amount == 0) {
          I2S.end();
          return false;
        }

        if (!readExact(stream, audioBuffer, amount)) {
          Serial.println("PCM read failed");
          I2S.end();
          return false;
        }

        applyVolume(audioBuffer, amount);

        size_t sent = 0;

        while (sent < amount) {
          size_t written = I2S.write(
            audioBuffer + sent,
            amount - sent
          );

          if (written == 0) {
            Serial.println("Speaker write failed");
            I2S.end();
            return false;
          }

          sent += written;
        }

        left -= amount;
        yield();
      }

      speakerFlush(channels);
      I2S.end();

      return true;
    }

    // --------------------------------------
    // OTHER CHUNKS: LIST / JUNK
    // --------------------------------------
    else {
      if (chunkSize > remainingBody) {
        Serial.println("Invalid chunk length");
        return false;
      }

      if (!skipBytes(stream, chunkSize)) {
        return false;
      }

      consumed += chunkSize;

      if (chunkSize & 1) {
        if (consumed >= totalBytes ||
            !skipBytes(stream, 1)) {
          Serial.println("Missing chunk padding");
          return false;
        }
        consumed++;
      }
    }
  }

  Serial.println("No audio data chunk found");
  return false;
}

// ==========================================
// FIND THE LAPTOP SERVER
// ==========================================
bool parseServer(const String& s, IPAddress& ip, uint16_t& port) {
  String host = s;
  port = 5000;
  int colon = s.indexOf(':');
  if (colon >= 0) {
    host = s.substring(0, colon);
    port = s.substring(colon + 1).toInt();
  }
  if (port == 0) port = 5000;
  if (ip.fromString(host)) return true;
  return WiFi.hostByName(host.c_str(), ip) == 1;
}

bool serverReachable(IPAddress ip, uint16_t port) {
  WiFiClient c;
  if (!c.connect(ip, port, 3000)) return false;
  c.stop();
  return true;
}

// Broadcast "VA_DISCOVER" on the local network; the server answers
// "VA_SERVER <port>" from its own address.
bool discoverServer(IPAddress& ip, uint16_t& port) {
  WiFiUDP udp;
  if (!udp.begin(DISCOVERY_PORT + 1)) return false;

  IPAddress targets[2] = {WiFi.broadcastIP(), IPAddress(255, 255, 255, 255)};
  bool found = false;

  for (int attempt = 0; attempt < 3 && !found; attempt++) {
    for (IPAddress t : targets) {
      udp.beginPacket(t, DISCOVERY_PORT);
      udp.print("VA_DISCOVER");
      udp.endPacket();
    }

    unsigned long start = millis();
    while (millis() - start < 800) {
      if (udp.parsePacket() > 0) {
        char buf[32] = {0};
        udp.read(buf, sizeof(buf) - 1);
        if (strncmp(buf, "VA_SERVER", 9) == 0) {
          ip = udp.remoteIP();
          port = atoi(buf + 10);
          if (port == 0) port = 5000;
          found = true;
          break;
        }
      }
      delay(10);
    }
  }

  udp.stop();
  return found;
}

bool findServer() {
  IPAddress ip;
  uint16_t port;
  bool ok;

  if (cfg.server.length()) {
    ok = parseServer(cfg.server, ip, port) && serverReachable(ip, port);
    if (!ok) {
      Serial.printf("[server] cannot reach %s - is server.py running?\n",
                    cfg.server.c_str());
    }
  } else {
    ok = discoverServer(ip, port) && serverReachable(ip, port);
    if (!ok) {
      Serial.println("[server] not found - start server.py on the laptop "
                     "and keep both on the same Wi-Fi");
    }
  }

  serverFound = ok;
  if (ok) {
    serverIp = ip;
    serverPort = port;
    Serial.printf("[server] found %s:%u\n",
                  ip.toString().c_str(), port);
  }
  return ok;
}

// ==========================================
// HTTP EXCHANGE WITH THE SERVER
// Sends `body` (or nothing) and plays the WAV reply.
// ==========================================
bool readLine(WiFiClient& c, String& line, unsigned long timeoutMs) {
  line = "";
  unsigned long start = millis();
  while (millis() - start < timeoutMs) {
    while (c.available()) {
      char ch = c.read();
      if (ch == '\n') {
        line.trim();
        return true;
      }
      if (line.length() < 400) line += ch;
    }
    if (!c.connected() && !c.available()) return false;
    delay(5);
  }
  return false;
}

// ==========================================
// UPLOAD COMPRESSION: IMA/DVI ADPCM, 4 bits per sample (4x smaller than PCM16).
// Matches Python's audioop.adpcm2lin: start state (0, 0), first sample in the
// HIGH nibble. The Wi-Fi uplink is the slowest step, so this cuts most of the wait.
// Body layout: "IMA1", uint32 LE sample count, uint32 LE sample rate, nibbles.
// ==========================================
#define ADPCM_HEADER 12
static const int8_t kImaIndex[16] = {-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8};
static const int16_t kImaStep[89] = {
  7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45, 50, 55, 60,
  66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371,
  408, 449, 494, 544, 598, 658, 724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878,
  2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845,
  8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086,
  29794, 32767};

// Encodes in place: `out` may point into the same buffer as `in` as long as it
// starts at or before it (writes trail reads: byte i/2 vs bytes 2i..2i+1).
size_t adpcmEncode(const int16_t* in, size_t n, uint8_t* out) {
  int pred = 0, index = 0;
  uint8_t pending = 0;
  size_t bytes = 0;
  for (size_t i = 0; i < n; i++) {
    int step = kImaStep[index];
    int diff = in[i] - pred;
    int code = 0;
    if (diff < 0) { code = 8; diff = -diff; }
    int delta = step >> 3;
    if (diff >= step) { code |= 4; diff -= step; delta += step; }
    step >>= 1;
    if (diff >= step) { code |= 2; diff -= step; delta += step; }
    step >>= 1;
    if (diff >= step) { code |= 1; delta += step; }
    pred += (code & 8) ? -delta : delta;
    if (pred > 32767) pred = 32767; else if (pred < -32768) pred = -32768;
    index += kImaIndex[code];
    if (index < 0) index = 0; else if (index > 88) index = 88;
    if ((i & 1) == 0) pending = (uint8_t)(code << 4);
    else out[bytes++] = pending | (uint8_t)code;
  }
  if (n & 1) out[bytes++] = pending;
  return bytes;
}

// The laptop/hotspot Wi-Fi dozes when idle and drops the first burst after a
// quiet spell, costing a 1.5 s TCP retransmission (measured: first upload ~2.1 s,
// following ones ~35 ms). A tiny UDP packet when speech STARTS wakes the path,
// so it's awake by the time the question is uploaded. The server ignores it.
void wakeLink() {
  if (!serverFound || WiFi.status() != WL_CONNECTED) return;
  static WiFiUDP wakeUdp;
  wakeUdp.beginPacket(serverIp, DISCOVERY_PORT);
  wakeUdp.print("VA_WAKE");
  wakeUdp.endPacket();
}

// Returns HTTP status, or -1 on connection problems.
int exchange(const char* method, const char* path,
             const uint8_t* body, size_t bodyLen, const char* contentType = "audio/wav") {
  WiFiClient c;
  uint32_t t0 = millis();

  if (!c.connect(serverIp, serverPort, 5000)) {
    Serial.println("[server] connection failed");
    serverFound = false;
    return -1;
  }
  // Small header writes + Nagle + Windows delayed ACK can stall ~200 ms:
  // send the whole header in one write and disable Nagle.
  c.setNoDelay(true);
  uint32_t tConnected = millis();

  char header[256];
  int hlen = snprintf(header, sizeof(header),
                      "%s %s HTTP/1.1\r\nHost: %s:%u\r\nX-Device: %s\r\nConnection: close\r\n",
                      method, path, serverIp.toString().c_str(), serverPort,
                      WiFi.macAddress().c_str());
  if (body) {
    hlen += snprintf(header + hlen, sizeof(header) - hlen,
                     "Content-Type: %s\r\nContent-Length: %u\r\n", contentType, (unsigned)bodyLen);
  }
  hlen += snprintf(header + hlen, sizeof(header) - hlen, "\r\n");
  c.write((const uint8_t*)header, hlen);

  size_t sent = 0;
  while (body && sent < bodyLen) {
    size_t n = c.write(body + sent, min((size_t)4096, bodyLen - sent));
    if (n == 0) {
      Serial.println("[server] upload failed");
      c.stop();
      return -1;
    }
    sent += n;
  }
  uint32_t tUploaded = millis();

  String line;
  if (!readLine(c, line, 90000)) {
    Serial.println("[server] no answer (timeout)");
    c.stop();
    return -1;
  }
  uint32_t tAnswer = millis();
  Serial.printf("[timing] connect %lu ms, upload %lu ms (%u KB), server %lu ms\n",
                (unsigned long)(tConnected - t0), (unsigned long)(tUploaded - tConnected),
                (unsigned)(bodyLen / 1024), (unsigned long)(tAnswer - tUploaded));

  int status = 0;
  int sp = line.indexOf(' ');
  if (sp > 0) status = line.substring(sp + 1).toInt();

  long length = -1;
  while (readLine(c, line, 10000) && line.length() > 0) {
    int colon = line.indexOf(':');
    if (colon < 0) continue;
    String name = line.substring(0, colon);
    String value = line.substring(colon + 1);
    name.toLowerCase();
    value.trim();
    if (name == "content-length") length = value.toInt();
    else if (name == "x-transcript" && value.length())
      Serial.println("[heard] " + value);
    else if (name == "x-reply" && value.length())
      Serial.println("[reply] " + value);
  }

  if (status == 200) {
    if (length <= 0 || length > 2 * 1024 * 1024) {
      Serial.println("[server] bad reply length");
    } else if (!playWavStream(&c, (size_t)length)) {
      Serial.println("[speaking] playback failed");
    }
  } else if (status != 204) {
    String err;
    while (readLine(c, line, 2000)) err += line;
    Serial.printf("[server] error %d %s\n", status, err.c_str());
  }

  c.stop();
  return status;
}

// ==========================================
// ONE QUESTION -> ANSWER ROUND
// ==========================================
void askServer(size_t samples) {
  uint32_t pcmBytes = samples * 2;
  Serial.printf("[thinking] sending %.1f s of audio\n",
                samples / (float)SAMPLE_RATE);

  int status;
  if (cfg.compress) {
    // PCM sits after the 44-byte WAV header; encode it in place to just after a
    // 12-byte ADPCM header (output always trails input, so this is safe).
    const int16_t* pcm = (const int16_t*)(recording + WAV_HEADER);
    size_t packed = adpcmEncode(pcm, samples, recording + ADPCM_HEADER);
    uint32_t n = samples, rate = SAMPLE_RATE;
    memcpy(recording, "IMA1", 4);
    memcpy(recording + 4, &n, 4);      // little-endian on the ESP32
    memcpy(recording + 8, &rate, 4);
    status = exchange("POST", "/ask", recording, ADPCM_HEADER + packed, "audio/x-ima-adpcm");
  } else {
    writeWavHeader(recording, pcmBytes);
    status = exchange("POST", "/ask", recording, WAV_HEADER + pcmBytes);
  }

  if (status == 204) {
    Serial.println("[ignored] no words recognised");
  }
}

void resumeListening() {
  if (!startMic()) {
    Serial.println("[mic] restart failed");
    return;
  }
  // Skip the amp's echo / room tail, then re-learn the noise level.
  calibrateNoise();
  talkRequested = false;
  led(false);
  Serial.println(cfg.listen ? "[listening]" : "[paused] type 'resume'");
}

// Record until the speaker pauses for `silenceMs`, or 10 s.
// Returns the number of samples captured (0 = discarded).
size_t recordUtterance() {
  int16_t* pcm = (int16_t*)(recording + WAV_HEADER);
  size_t count = 0;

  // Start with the pre-roll so the first syllable isn't cut.
  int first = (prerollHead - prerollCount + PREROLL_FRAMES) % PREROLL_FRAMES;
  for (int i = 0; i < prerollCount; i++) {
    int idx = (first + i) % PREROLL_FRAMES;
    memcpy(pcm + count, preroll[idx], FRAME_SAMPLES * 2);
    count += FRAME_SAMPLES;
  }

  int speechMs = prerollCount ? START_FRAMES * FRAME_MS : 0;
  int silentMs = 0;
  size_t maxSamples = min(recordCapacity,
                          (size_t)SAMPLE_RATE * MAX_RECORD_MS / 1000);
  float baseKeep = keepThreshold();
  float voiceLevel = startThreshold();
  float voicedSum = 0;
  int voicedFrames = 0;

  while (count + FRAME_SAMPLES <= maxSamples) {
    float level = readMicFrame();
    memcpy(pcm + count, frame, FRAME_SAMPLES * 2);
    count += FRAME_SAMPLES;

    // Anything well below the speaker's own loudness counts as
    // silence, so quieter talk further away doesn't keep it going.
    float keep = max(baseKeep, voiceLevel * 0.25f);

    if (level > keep) {
      speechMs += FRAME_MS;
      silentMs = 0;
      voiceLevel += (level - voiceLevel) * 0.1f;
      voicedSum += level;
      voicedFrames++;
    } else {
      silentMs += FRAME_MS;
      if (silentMs >= cfg.silenceMs) break;
    }
  }

  if (count + FRAME_SAMPLES > maxSamples) {
    Serial.printf("[recording] %d s limit reached\n", MAX_RECORD_MS / 1000);
  }

  if (speechMs < MIN_SPEECH_MS) {
    Serial.println("[ignored] too short");
    return 0;
  }

  // Calibrated: drop speech that is clearly quieter than you.
  if (cfg.voiceRef > 0 && voicedFrames > 0) {
    float average = voicedSum / voicedFrames;
    if (average < cfg.voiceRef * voiceFraction()) {
      Serial.printf("[ignored] quieter than your voice (%.0f < %.0f) - "
                    "someone further away?\n",
                    average, cfg.voiceRef * voiceFraction());
      return 0;
    }
  }

  // Drop most of the trailing silence.
  int trimMs = max(0, silentMs - TAIL_KEEP_MS);
  size_t trim = (size_t)SAMPLE_RATE * trimMs / 1000;
  if (trim < count) count -= trim;

  return count;
}

void listenStep() {
  float level = readMicFrame();

  memcpy(preroll[prerollHead], frame, sizeof(frame));
  prerollHead = (prerollHead + 1) % PREROLL_FRAMES;
  if (prerollCount < PREROLL_FRAMES) prerollCount++;

  if (level > startThreshold()) {
    loudFrames++;
  } else {
    loudFrames = 0;
    // Follow the background noise: fall quickly, rise slowly.
    float rate = level < noiseFloor ? 0.05f : 0.002f;
    noiseFloor += (level - noiseFloor) * rate;
    if (noiseFloor < 2.0f) noiseFloor = 2.0f;
  }

  if (talkRequested) {
    talkRequested = false;
    prerollCount = 0;
    led(true);
    wakeLink();
    Serial.println("[recording] button pressed - speak now");
  } else if (cfg.handsFree && loudFrames >= START_FRAMES) {
    led(true);
    wakeLink();
    Serial.println("[recording] speech detected");
  } else {
    return;
  }

  size_t samples = recordUtterance();
  loudFrames = 0;

  if (samples == 0) {
    prerollCount = 0;
    led(false);
    return;
  }

  Serial.printf("[recording] done, %.1f s\n",
                samples / (float)SAMPLE_RATE);

  askServer(samples);
  resumeListening();
}

// ==========================================
// WI-FI
// ==========================================
bool connectWiFi(unsigned long timeoutMs) {
  if (cfg.ssid.length() == 0) return false;

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  WiFi.disconnect();
  delay(100);
  WiFi.begin(cfg.ssid.c_str(), cfg.pass.c_str());

  Serial.printf("[wifi] connecting to \"%s\"", cfg.ssid.c_str());
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < timeoutMs) {
    delay(500);
    Serial.print(".");
    handleSerial();
  }
  Serial.println();

  if (WiFi.status() != WL_CONNECTED) {
    wl_status_t st = WiFi.status();
    Serial.printf("[wifi] FAILED to join \"%s\". The ESP32 needs a 2.4 GHz "
                  "network - check the name, password and band.\n",
                  cfg.ssid.c_str());
    Serial.printf("[wifi] reason: %s (status %d). Type 'scan' to see signal strengths.\n",
                  st == WL_NO_SSID_AVAIL ? "network not seen (out of range / no antenna / 5 GHz)"
                  : st == WL_CONNECT_FAILED ? "rejected (wrong password or security type)"
                  : "no answer from the network (weak signal?)", (int)st);
    return false;
  }

  wifiWasConnected = true;
  Serial.printf("[wifi] connected ip=%s rssi=%d\n",
                WiFi.localIP().toString().c_str(), WiFi.RSSI());
  return true;
}

// ==========================================
// SETUP PORTAL (phone / browser)
// ==========================================
String htmlEscape(const String& s) {
  String o;
  for (char c : s) {
    if (c == '&') o += "&amp;";
    else if (c == '<') o += "&lt;";
    else if (c == '>') o += "&gt;";
    else if (c == '"') o += "&quot;";
    else if (c == '\'') o += "&#39;";
    else o += c;
  }
  return o;
}

void portalRoot() {
  String page =
    "<!doctype html><html><head><meta charset=utf-8>"
    "<meta name=viewport content='width=device-width,initial-scale=1'>"
    "<title>Voice Assistant setup</title><style>"
    "body{font-family:sans-serif;max-width:420px;margin:24px auto;padding:0 16px}"
    "input,select,button{width:100%;padding:10px;margin:6px 0 14px;"
    "box-sizing:border-box;font-size:16px}"
    "button{background:#1a73e8;color:#fff;border:0;border-radius:6px}"
    "small{color:#666}</style></head><body>"
    "<h2>Voice Assistant setup</h2>"
    "<form method=post action=/save>"
    "<label>Wi-Fi network (2.4 GHz)</label>"
    "<input name=ssid list=nets required value='" + htmlEscape(cfg.ssid) + "'>"
    "<datalist id=nets>" + portalNetworks + "</datalist>"
    "<label>Wi-Fi password</label>"
    "<input name=pass type=password>"
    "<label>Laptop server address <small>(leave empty = automatic)</small></label>"
    "<input name=server placeholder='e.g. 192.168.1.20:5000' value='" +
    htmlEscape(cfg.server) + "'>"
    "<button type=submit>Save and connect</button></form>"
    "<small>Use the same Wi-Fi as the laptop running server.py. "
    "Phone hotspots must be set to 2.4 GHz.</small></body></html>";
  web.send(200, "text/html", page);
}

void portalSave() {
  cfg.ssid = web.arg("ssid");
  cfg.pass = web.arg("pass");
  cfg.server = web.arg("server");
  cfg.ssid.trim();
  cfg.server.trim();
  saveSettings();
  web.send(200, "text/html",
           "<meta name=viewport content='width=device-width'>"
           "<h3>Saved. The assistant is restarting and joining "
           "your Wi-Fi.</h3>");
  Serial.printf("[portal] saved network \"%s\", restarting\n",
                cfg.ssid.c_str());
  delay(1500);
  ESP.restart();
}

void startPortal() {
  I2S.end();
  led(true);

  // Scan first (station mode), then become an access point.
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  int n = WiFi.scanNetworks();
  portalNetworks = "";
  for (int i = 0; i < n; i++) {
    portalNetworks += "<option value='" + htmlEscape(WiFi.SSID(i)) + "'>";
  }
  WiFi.scanDelete();

  uint8_t mac[6];
  WiFi.macAddress(mac);
  char apName[32];
  snprintf(apName, sizeof(apName), "VoiceAssistant-%02X%02X", mac[4], mac[5]);

  WiFi.mode(WIFI_AP);
  WiFi.softAP(apName);
  delay(200);
  dns.start(53, "*", WiFi.softAPIP());

  web.on("/", HTTP_GET, portalRoot);
  web.on("/save", HTTP_POST, portalSave);
  web.onNotFound([]() {
    web.sendHeader("Location", "http://192.168.4.1/", true);
    web.send(302, "text/plain", "");
  });
  web.begin();

  portalActive = true;
  portalStarted = millis();
  Serial.printf("[portal] SETUP MODE: join Wi-Fi \"%s\" on your phone, "
                "then open http://192.168.4.1\n", apName);
}

// ==========================================
// SERIAL COMMANDS
// ==========================================
void printHelp() {
  Serial.println(
    "\nCommands:\n"
    "  status              show settings and connection\n"
    "  ssid <name>         set Wi-Fi name (spaces allowed)\n"
    "  pass <password>     set Wi-Fi password (empty = open network)\n"
    "  connect             join Wi-Fi with the saved name/password\n"
    "  server <ip[:port]>  fixed laptop address, or 'server auto'\n"
    "  volume <0-100>      speaker volume\n"
    "  silence <ms>        pause that ends a question (500-5000)\n"
    "  sensitivity <1-10>  speech detection (higher = more sensitive)\n"
    "  gain <1-64>         microphone gain\n"
    "  mode auto|button    auto = start on speech, button = press BOOT\n"
    "  calibrate           learn your voice level (ignore people further away)\n"
    "  calibrate off       forget the calibration\n"
    "  mic                 show mic level for 5 s\n"
    "  micdiag             find out why the mic gives no signal\n"
    "  tone                play a test beep\n"
    "  hello               play a test reply from the server\n"
    "  pause / resume      stop / start listening\n"
    "  portal              open the phone setup hotspot\n"
    "  forget              erase Wi-Fi settings\n"
    "  restart");
}

void printStatus() {
  Serial.printf("[status] firmware=%s ssid=\"%s\" wifi=%s ip=%s rssi=%d\n",
                FW_VERSION, cfg.ssid.c_str(),
                WiFi.status() == WL_CONNECTED ? "connected" : "disconnected",
                WiFi.localIP().toString().c_str(), WiFi.RSSI());
  Serial.printf("[status] server=%s (%s) found=%s\n",
                serverFound ? (serverIp.toString() + ":" + serverPort).c_str()
                            : "-",
                cfg.server.length() ? cfg.server.c_str() : "auto",
                serverFound ? "yes" : "no");
  Serial.printf("[status] mode=%s voice=%.0f volume=%d silence=%dms "
                "sensitivity=%d gain=%d listening=%s mic=%s noise=%.0f "
                "threshold=%.0f psram=%u\n",
                cfg.handsFree ? "auto" : "button", cfg.voiceRef,
                cfg.volume, cfg.silenceMs, cfg.sensitivity, cfg.micGain,
                cfg.listen ? "yes" : "no",
                micChannel < 0 ? "none" : (micChannel ? "right" : "left"),
                noiseFloor, startThreshold(), (unsigned)ESP.getPsramSize());
  Serial.printf("[status] upload=%s\n", cfg.compress ? "ADPCM (4x smaller)" : "WAV");
}

// Work out why the mic gives no signal: look at the raw data, then test
// the SD line with the internal pull-up / pull-down resistors.
void micDiagnostics() {
  if (portalActive) return;
  startMic();
  for (int i = 0; i < MIC_SETTLE_FRAMES; i++) readMicFrame();

  int32_t lo[2] = {INT32_MAX, INT32_MAX}, hi[2] = {INT32_MIN, INT32_MIN};
  for (int f = 0; f < 10; f++) {
    size_t got = 0;
    while (got < sizeof(micRaw)) {
      size_t n = I2S.readBytes((char*)micRaw + got, sizeof(micRaw) - got);
      if (n == 0) break;
      got += n;
    }
    for (int i = 0; i < FRAME_SAMPLES; i++) {
      for (int c = 0; c < 2; c++) {
        int32_t v = micRaw[i * 2 + c] >> 8;
        lo[c] = min(lo[c], v);
        hi[c] = max(hi[c], v);
      }
    }
  }
  Serial.printf("[micdiag] raw left: %ld..%ld  right: %ld..%ld\n",
                (long)lo[0], (long)hi[0], (long)lo[1], (long)hi[1]);
  bool data = (hi[0] - lo[0] > 100) || (hi[1] - lo[1] > 100);

  I2S.end();
  int up = 0, down = 0;
  pinMode(MIC_SD, INPUT_PULLUP);
  delay(5);
  for (int i = 0; i < 50; i++) { up += digitalRead(MIC_SD); delayMicroseconds(200); }
  pinMode(MIC_SD, INPUT_PULLDOWN);
  delay(5);
  for (int i = 0; i < 50; i++) { down += digitalRead(MIC_SD); delayMicroseconds(200); }
  pinMode(MIC_SD, INPUT);
  Serial.printf("[micdiag] SD line (D0): high %d/50 with pull-up, %d/50 with "
                "pull-down\n", up, down);

  if (data) {
    Serial.println("[micdiag] RESULT: mic is sending data - wiring OK");
  } else if (up == 50 && down == 0) {
    Serial.println("[micdiag] RESULT: the mic is not driving SD (line is "
                   "floating). Check: mic VDD->3V3 and GND->GND (power), "
                   "SCK->D2 and WS->D1 (clock), and the SD->D0 wire itself.");
  } else if (up == 0) {
    Serial.println("[micdiag] RESULT: SD is stuck LOW even with pull-up - "
                   "the SD wire is touching GND or sits in a wrong row.");
  } else if (down == 50) {
    Serial.println("[micdiag] RESULT: SD is stuck HIGH - the SD wire is "
                   "touching 3V3/5V or sits in a wrong row.");
  } else {
    Serial.println("[micdiag] RESULT: SD line is unstable - loose contact? "
                   "Re-seat the SD->D0 wire.");
  }

  if (startMic()) detectMicChannel();
}

void micMeter() {
  if (portalActive) return;
  startMic();
  for (int i = 0; i < MIC_SETTLE_FRAMES; i++) readMicFrame();
  Serial.println("[mic] speak now - level for 5 s (# = loudness)");
  unsigned long start = millis();
  float peak = 0;
  while (millis() - start < 5000) {
    float level = 0;
    for (int i = 0; i < 5; i++) level = max(level, readMicFrame());
    peak = max(peak, level);
    int bars = min(50, (int)(level / 40));
    String bar;
    for (int i = 0; i < bars; i++) bar += '#';
    Serial.printf("[mic] %5.0f %s\n", level, bar.c_str());
  }
  Serial.printf("[mic] peak=%.0f noise=%.0f start-threshold=%.0f\n",
                peak, noiseFloor, startThreshold());
  resumeListening();
}

// Learn how loud *you* are at your usual distance, so speech from
// people further away (quieter) can be ignored.
void calibrateVoice() {
  if (portalActive) return;
  const int quietFrames = 100;   // 2 s
  const int speakFrames = 200;   // 4 s
  static float levels[speakFrames];

  startMic();
  for (int i = 0; i < MIC_SETTLE_FRAMES; i++) readMicFrame();

  Serial.println("[calibrate] step 1/2: stay QUIET for 2 seconds...");
  for (int i = 0; i < quietFrames; i++) levels[i] = readMicFrame();
  std::sort(levels, levels + quietFrames);
  float room = max(levels[quietFrames / 2], 2.0f);

  Serial.println("[calibrate] step 2/2: get ready to speak...");
  for (int n = 3; n > 0; n--) {
    Serial.printf("[calibrate] %d\n", n);
    for (int i = 0; i < 40; i++) readMicFrame();   // 0.8 s
  }
  Serial.println("[calibrate] SPEAK NOW at your normal distance and volume "
                 "(e.g. count from one to ten) - 4 seconds, LED is on");
  led(true);
  clippedSamples = 0;
  for (int i = 0; i < speakFrames; i++) levels[i] = readMicFrame();
  led(false);
  uint32_t clipped = clippedSamples;

  // Keep only the frames where you were actually speaking.
  int voiced = 0;
  for (int i = 0; i < speakFrames; i++) {
    if (levels[i] > room * 3.0f) levels[voiced++] = levels[i];
  }
  if (voiced < 25) {
    Serial.printf("[calibrate] FAILED: didn't hear you clearly (room=%.0f). "
                  "Speak louder or closer, then try again.\n", room);
    return;
  }
  std::sort(levels, levels + voiced);
  float voice = levels[voiced / 2];

  if (clipped > (uint32_t)(speakFrames * FRAME_SAMPLES) / 500 &&
      cfg.micGain > 2) {
    cfg.micGain = max(1, cfg.micGain / 2);
    cfg.voiceRef = 0;
    saveSettings();
    Serial.printf("[calibrate] RETRY: your voice was too loud for the mic "
                  "setting (clipping). Gain lowered to %d - run calibrate "
                  "again.\n", cfg.micGain);
    return;
  }

  if (voice < room * 4.0f) {
    Serial.printf("[calibrate] FAILED: your voice (%.0f) is only %.1fx louder "
                  "than the room (%.0f). Move closer to the mic or reduce "
                  "noise, then try again.\n", voice, voice / room, room);
    return;
  }

  cfg.voiceRef = voice;
  saveSettings();
  Serial.printf("[calibrate] done: room=%.0f voice=%.0f (%.1fx louder). "
                "Speech below %.0f will be ignored.\n",
                room, voice, voice / room, voice * voiceFraction());
}

void playTone() {
  if (!startSpeaker(16000, 1)) {
    Serial.println("[tone] speaker I2S failed");
    return;
  }
  int16_t buf[320];
  int amplitude = 8000 * cfg.volume / 100;
  for (int block = 0; block < 25; block++) {          // 0.5 s, 1 kHz
    for (int i = 0; i < 320; i++) {
      buf[i] = amplitude * sinf(2 * PI * 1000 * (block * 320 + i) / 16000.0f);
    }
    I2S.write((uint8_t*)buf, sizeof(buf));
  }
  speakerFlush(1);
  Serial.println("[tone] done");
  if (!portalActive) resumeListening();
}

void runCommand(String line) {
  // Strip only the line ending and leading blanks: Wi-Fi names and passwords may
  // legitimately END in a space (some phone hotspots are named like "My Phone ").
  while (line.length() && (line.endsWith("\r") || line.endsWith("\n")))
    line.remove(line.length() - 1);
  int lead = 0;
  while (lead < (int)line.length() && (line[lead] == ' ' || line[lead] == '\t')) lead++;
  line = line.substring(lead);
  String probe = line;
  probe.trim();
  if (probe.length() == 0) return;

  int sp = line.indexOf(' ');
  String cmd = sp < 0 ? line : line.substring(0, sp);
  String arg = sp < 0 ? "" : line.substring(sp + 1);
  cmd.toLowerCase();
  if (cmd != "pass" && cmd != "ssid") arg.trim();

  if (cmd == "help" || cmd == "?") {
    printHelp();
  } else if (cmd == "status") {
    printStatus();
  } else if (cmd == "ssid") {
    cfg.ssid = arg;
    saveSettings();
    Serial.printf("[ok] ssid=\"%s\"\n", cfg.ssid.c_str());
  } else if (cmd == "pass") {
    cfg.pass = arg;
    saveSettings();
    Serial.println("[ok] password saved");
  } else if (cmd == "connect") {
    if (portalActive) {
      ESP.restart();          // leave setup mode cleanly
    }
    I2S.end();
    serverFound = false;
    if (connectWiFi(20000)) findServer();
    resumeListening();
  } else if (cmd == "server") {
    cfg.server = (arg == "auto") ? "" : arg;
    saveSettings();
    Serial.printf("[ok] server=%s\n",
                  cfg.server.length() ? cfg.server.c_str() : "auto");
    serverFound = false;
    lastServerSearch = 0;
  } else if (cmd == "volume") {
    cfg.volume = constrain(arg.toInt(), 0, 100);
    saveSettings();
    Serial.printf("[ok] volume=%d\n", cfg.volume);
  } else if (cmd == "silence") {
    cfg.silenceMs = constrain(arg.toInt(), 500, 5000);
    saveSettings();
    Serial.printf("[ok] silence=%dms\n", cfg.silenceMs);
  } else if (cmd == "sensitivity") {
    cfg.sensitivity = constrain(arg.toInt(), 1, 10);
    saveSettings();
    Serial.printf("[ok] sensitivity=%d threshold=%.0f\n",
                  cfg.sensitivity, startThreshold());
  } else if (cmd == "gain") {
    cfg.micGain = constrain(arg.toInt(), 1, 64);
    bool hadCalibration = cfg.voiceRef > 0;
    cfg.voiceRef = 0;   // levels change with gain
    saveSettings();
    Serial.printf("[ok] gain=%d\n", cfg.micGain);
    if (hadCalibration) {
      Serial.println("[ok] calibration cleared - run 'calibrate' again");
    }
    if (!portalActive) resumeListening();
  } else if (cmd == "mode") {
    arg.toLowerCase();
    cfg.handsFree = arg != "button";
    saveSettings();
    Serial.printf("[ok] mode=%s\n", cfg.handsFree ? "auto" : "button");
  } else if (cmd == "calibrate") {
    if (arg == "off") {
      cfg.voiceRef = 0;
      saveSettings();
      Serial.println("[ok] calibration cleared");
    } else {
      calibrateVoice();
    }
    resumeListening();
  } else if (cmd == "micdiag") {
    micDiagnostics();
    resumeListening();
  } else if (cmd == "mic") {
    micMeter();
  } else if (cmd == "tone") {
    playTone();
  } else if (cmd == "hello") {
    if (!serverFound) {
      Serial.println("[server] not connected yet");
    } else {
      I2S.end();
      exchange("GET", "/hello", nullptr, 0);
      resumeListening();
    }
  } else if (cmd == "pause") {
    cfg.listen = false;
    Serial.println("[ok] listening paused");
  } else if (cmd == "resume") {
    cfg.listen = true;
    resumeListening();
  } else if (cmd == "portal") {
    startPortal();
  } else if (cmd == "forget") {
    cfg.ssid = "";
    cfg.pass = "";
    saveSettings();
    Serial.println("[ok] Wi-Fi settings erased");
  } else if (cmd == "restart") {
    ESP.restart();
  } else if (cmd == "nettest") {
    // Upload a question-sized block (24 KB, like ~3 s of compressed speech)
    // 10 times to the server's /sink and print each [timing] line.
    // "nettest wake": send the speech-start wake packet first and wait 1 s
    // (the shortest a question takes), like a real question would.
    if (!serverFound) {
      Serial.println("[nettest] server not found yet");
    } else {
      if (arg == "wake") {
        wakeLink();
        delay(1000);
      }
      for (int i = 0; i < 10; i++) {
        Serial.printf("[nettest] %d/10: ", i + 1);
        exchange("POST", "/sink", recording, 24 * 1024, "application/octet-stream");
        delay(300);
      }
      Serial.println("[nettest] done");
    }
  } else if (cmd == "compress") {
    // compress on  = 4x smaller uploads (IMA ADPCM, default)
    // compress off = plain 16-bit WAV (if recognition ever seems worse)
    if (arg == "on" || arg == "1") cfg.compress = true;
    else if (arg == "off" || arg == "0") cfg.compress = false;
    saveSettings();
    Serial.printf("[ok] compress=%s\n", cfg.compress ? "on (ADPCM, 4x smaller)" : "off (WAV)");
  } else if (cmd == "scan") {
    // Wi-Fi diagnosis: what the glasses can hear, and how strongly.
    // Around -40..-65 dBm is good; below -80 dBm (or nothing at all right next to
    // the router/phone) usually means the XIAO's external antenna isn't attached.
    if (WiFi.getMode() == WIFI_OFF) WiFi.mode(WIFI_STA);
    int n = WiFi.scanNetworks();
    Serial.printf("[scan] %d networks\n", n);
    for (int i = 0; i < n; i++) {
      wifi_auth_mode_t a = WiFi.encryptionType(i);
      Serial.printf("[scan] %4d dBm  ch%-2d  %-8s  %s\n", (int)WiFi.RSSI(i), (int)WiFi.channel(i),
                    a == WIFI_AUTH_OPEN ? "open" : a == WIFI_AUTH_WPA3_PSK ? "WPA3"
                    : a == WIFI_AUTH_WPA2_WPA3_PSK ? "WPA2/3" : a == WIFI_AUTH_WPA2_PSK ? "WPA2"
                    : "WPA/other", WiFi.SSID(i).c_str());
    }
    WiFi.scanDelete();
  } else {
    Serial.println("[error] unknown command - type help");
  }
}

void handleSerial() {
  static bool busy = false;   // commands may call back into here
  if (busy) return;
  busy = true;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (serialLine.length()) {
        String line = serialLine;
        serialLine = "";
        runCommand(line);
      }
    } else if (serialLine.length() < 200) {
      serialLine += c;
    }
  }
  busy = false;
}

// BOOT button: short press = talk now (for noisy rooms),
// hold 3 s = open the phone setup portal.
void checkBootButton() {
  static unsigned long pressedAt = 0;
  static bool held = false;
  if (digitalRead(BOOT_BUTTON) == LOW) {
    if (pressedAt == 0) pressedAt = millis();
    if (!held && millis() - pressedAt > 3000 && !portalActive) {
      held = true;
      startPortal();
    }
  } else {
    if (pressedAt && !held && millis() - pressedAt > 40) {
      talkRequested = true;
    }
    pressedAt = 0;
    held = false;
  }
}

// ==========================================
// SETUP
// ==========================================
void setup() {
#if ARDUINO_USB_MODE && ARDUINO_USB_CDC_ON_BOOT
  // Don't stall the audio loop when no serial monitor is open.
  Serial.setTxBufferSize(4096);
  Serial.setTxTimeoutMs(20);
#endif
  Serial.begin(115200);
  delay(1000);

  pinMode(STATUS_LED, OUTPUT);
  led(false);
  pinMode(BOOT_BUTTON, INPUT_PULLUP);

  Serial.printf("\n=== Voice assistant v%s ===\n", FW_VERSION);
  loadSettings();

  // 10 s of 16 kHz 16-bit audio = 320 KB, needs PSRAM.
  recordCapacity = (size_t)SAMPLE_RATE * MAX_RECORD_MS / 1000;
  size_t bytes = WAV_HEADER + recordCapacity * 2;
  recording = (uint8_t*)ps_malloc(bytes);
  if (!recording) {
    recordCapacity = SAMPLE_RATE * 4;    // fall back to 4 s in normal RAM
    recording = (uint8_t*)malloc(WAV_HEADER + recordCapacity * 2);
    Serial.println("[warn] no PSRAM - enable Tools > PSRAM > OPI PSRAM. "
                   "Questions limited to 4 s.");
  }
  if (!recording) {
    Serial.println("[error] out of memory");
    while (true) delay(1000);
  }

  setupFilter();
  if (!startMic()) {
    Serial.println("[mic] I2S start FAILED");
  } else {
    detectMicChannel();
  }

  if (cfg.ssid.length() == 0) {
    Serial.println("[wifi] not configured. Run: python tools/device.py setup");
    Serial.println("       or use the phone setup hotspot below.");
    startPortal();
    return;
  }

  if (!connectWiFi(20000) && !connectWiFi(20000)) {
    startPortal();
    return;
  }

  findServer();
  lastServerSearch = millis();

  if (!startMic()) Serial.println("[mic] I2S start FAILED");
  calibrateNoise();
  Serial.println("Type 'help' for commands.");
  Serial.println("[listening]");
}

// ==========================================
// MAIN LOOP
// ==========================================
void loop() {
  handleSerial();

  if (portalActive) {
    dns.processNextRequest();
    web.handleClient();
    if (millis() - portalStarted > PORTAL_TIMEOUT_MS && cfg.ssid.length()) {
      Serial.println("[portal] timeout, retrying saved Wi-Fi");
      ESP.restart();
    }
    delay(2);
    return;
  }

  checkBootButton();

  if (WiFi.status() != WL_CONNECTED) {
    if (wifiWasConnected) {
      Serial.println("[wifi] lost connection, reconnecting...");
      wifiWasConnected = false;
      serverFound = false;
    }
    if (millis() - lastWifiReport > 15000) {
      lastWifiReport = millis();
      WiFi.reconnect();
    }
    delay(100);
    return;
  }

  if (!wifiWasConnected) {
    wifiWasConnected = true;
    Serial.printf("[wifi] connected ip=%s rssi=%d\n",
                  WiFi.localIP().toString().c_str(), WiFi.RSSI());
  }

  if (!serverFound) {
    if (millis() - lastServerSearch > 5000) {
      lastServerSearch = millis();
      I2S.end();
      if (findServer()) resumeListening();
    }
    delay(20);
    return;
  }

  // No mic found yet (e.g. a loose wire): look again every 5 s, so fixing
  // the wiring works without a restart.
  if (micChannel < 0) {
    static unsigned long lastMicCheck = 0;
    if (millis() - lastMicCheck > 5000) {
      lastMicCheck = millis();
      quietMicCheck = true;
      if (startMic()) detectMicChannel();
      quietMicCheck = false;
      if (micChannel >= 0) resumeListening();
    }
    delay(20);
    return;
  }

  if (!cfg.listen) {
    delay(20);
    return;
  }

  listenStep();
}
