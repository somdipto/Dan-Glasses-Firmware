// XIAO ESP32-S3 full-duplex PCM16 over authenticated WSS with bundled FD AEC.
// Reference pin map below is NOT confirmation of the attached board/wiring.
// USB setup: first pairing, then device-token-authenticated maintenance (or BOOT).
// No discovery, AP, web portal, plaintext transport, or embedded credentials.
// ==========================================

#include <Arduino.h>
#include "duplex_audio.h"
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <time.h>
#include <mbedtls/base64.h>
extern "C" {
#include <mbedtls/constant_time.h>
}
#include <mbedtls/x509_crt.h>
#include "bridge_protocol.h"
#include "startup_greeting.h"
#include "stream_controller.h"
#include "stream_transport.h"
#include "wifi_join.h"
#include <Preferences.h>
#include <algorithm>
#include <cstring>
#include <cmath>
#include <utility>
#include <atomic>
#include <strings.h>
SET_LOOP_TASK_STACK_SIZE(16384); // Retain TLS headroom while measuring the stack-canary failure.
#define FW_VERSION "5.2-duplex"

glasses_audio::DuplexAudio audio;

// ==========================================
// PINS (XIAO labels in brackets)
// ==========================================
// INMP441 MICROPHONE
#define MIC_BCLK 2   // SCK  [D1]
#define MIC_WS   3   // WS   [D2]
#define MIC_SD   1   // SD   [D0]

// MAX98357A SPEAKER (shares BCLK/WS with the mic)
#define SPK_BCLK 2   // BCLK [D1]
#define SPK_WS   3   // LRC  [D2]
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
#define PREROLL_FRAMES 15                         // 300 ms kept before speech
#define START_FRAMES   4                          // 80 ms loud = speech started
#define PROVISION_TIMEOUT_MS 120000UL
#define EXCHANGE_TIMEOUT_MS 30000UL  // HTTPS health only, never live audio.

// ==========================================
// SETTINGS (saved in flash)
// ==========================================
Preferences prefs;

struct Settings {
  String ssid;
  String pass;
  String bridgeUrl;     // Explicit HTTPS origin; never discovered over the network.
  String deviceToken;   // Extractable NVS credential: narrow and revocable at the bridge.
  String rootCa;        // Provisioned public trust anchor, not a private key.
  int volume = 20;      // Start quietly; actual ear-level SPL still needs measurement.
  int sensitivity = 5;  // 1 (needs loud voice) .. 10 (very sensitive)
  int micGain = 16;     // digital mic gain
  bool handsFree = true; // Speech-triggered by default; no wake word.
  float voiceRef = 0;    // your speaking level from 'calibrate' (0 = none)
  bool listen = true; // Intent; persistent capture starts after authenticated readiness.
} cfg;

bool settingsDurable = true;
void stopSpeaker();
bool cancelStreaming();

bool settingsSaved(bool success) {
  settingsDurable = success;
  if (!success) {
    cfg.listen = false;
    cancelStreaming();
    audio.end();
    stopSpeaker();
    Serial.println("[error] settings write failed; listening paused, bridge disabled");
  }
  return success;
}

void loadSettings() {
  prefs.begin("va", true);
  cfg.ssid = prefs.getString("ssid", "");
  cfg.pass = prefs.getString("pass", "");
  cfg.bridgeUrl = prefs.getString("bridge", "");
  cfg.deviceToken = prefs.getString("token", "");
  cfg.rootCa = prefs.getString("ca", "");
  bool reviewed = prefs.getBool("controls_v1", false);
  cfg.volume = safeStartupVolume(prefs.getInt("volume", 20), reviewed);
  cfg.sensitivity = constrain(prefs.getInt("sens", 5), 1, 10);
  cfg.micGain = constrain(prefs.getInt("gain", 16), 1, 64);
  cfg.handsFree = startupHandsFree(prefs.getBool("handsfree_v1", false), prefs.getBool("auto", true));
  cfg.voiceRef = prefs.getFloat("voice", 0);
  if (!std::isfinite(cfg.voiceRef) || cfg.voiceRef < 0) cfg.voiceRef = 0;
  prefs.end();
}

bool saveSettings() {
  if (!prefs.begin("va", false)) return settingsSaved(false);
  bool ok = storeSettingString(prefs, "ssid", cfg.ssid.c_str()) &&
            storeSettingString(prefs, "pass", cfg.pass.c_str()) &&
            storeSettingString(prefs, "bridge", cfg.bridgeUrl.c_str()) &&
            storeSettingString(prefs, "token", cfg.deviceToken.c_str()) &&
            storeSettingString(prefs, "ca", cfg.rootCa.c_str()) &&
            (!prefs.isKey("server") || prefs.remove("server")) &&
            (!prefs.isKey("silence") || prefs.remove("silence")) &&
            prefs.putInt("volume", cfg.volume) == sizeof(int32_t) &&
            prefs.putInt("sens", cfg.sensitivity) == sizeof(int32_t) &&
            prefs.putInt("gain", cfg.micGain) == sizeof(int32_t) &&
            prefs.putBool("auto", cfg.handsFree) == sizeof(uint8_t) &&
            prefs.putBool("handsfree_v1", true) == sizeof(uint8_t) &&
            prefs.putFloat("voice", cfg.voiceRef) == sizeof(float) &&
            prefs.putBool("controls_v1", true) == sizeof(uint8_t);
  prefs.end();
  return settingsSaved(ok);
}

// ==========================================
// STATE
// ==========================================
BridgeEndpoint bridge;
String bridgeSession;
unsigned long exchangeStarted = 0;
bool serverFound = false;
unsigned long lastServerSearch = 0;
bool wifiWasConnected = false;
bool wifiJoinRequired = true;    // Saved credential changes require a fresh explicit join.
std::atomic<uint32_t> lastWifiDisconnectReason{0};
WifiJoinState wifiJoin;

glasses_stream::Transport voiceTransport;
glasses_stream::Controller voiceController;
uint8_t playback[FRAME_SAMPLES * 2];
uint32_t playbackGeneration = 0, observedAudioFill = 0;
bool liveSpeaker = false;
bool replacingExchange = false;
uint32_t bargeIns = 0;
glasses_audio::DuplexAudio::Capture capturedAudio;

int32_t micRaw[FRAME_SAMPLES * 2];
int16_t frame[FRAME_SAMPLES];
int16_t preroll[PREROLL_FRAMES][FRAME_SAMPLES];
int prerollHead = 0;
int prerollCount = 0;

int micChannel = -1;            // 0 = left, 1 = right, -1 = not detected
float noiseFloor = 0;
bool noiseCalibrated = false;
int loudFrames = 0;
bool talkRequested = false;     // BOOT button short press
uint32_t clippedSamples = 0;    // samples that hit full scale
bool quietMicCheck = false;     // background re-checks don't print warnings

unsigned long provisioningStarted = 0;
bool provisioningOpen = false;
bool usbReceiveReady = false;
bool setupComplete = false;
String pendingReadiness;

String serialLine;

void handleSerial();
void resumeListening(bool reuseNoise = false);

// ==========================================
// LED (active LOW on the XIAO)
// ==========================================
void led(bool on) {
  digitalWrite(STATUS_LED, on ? LOW : HIGH);
}


// ==========================================
// READ EXACT BYTES
// ==========================================
bool readExact(WiFiClientSecure* s, uint8_t* dst, size_t n) {
  size_t done = 0;
  unsigned long lastData = millis();

  while (done < n) {
    if (millis() - exchangeStarted > EXCHANGE_TIMEOUT_MS) return false;
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

      if (millis() - lastData > 5000) {
        Serial.println("Read timeout");
        return false;
      }

      delay(2);
    }
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


Biquad hp1, hp2;

void setupFilter() {
  // Butterworth 4th order: Q values of the two sections.
  hp1.highPass(HP_CUTOFF_HZ, 0.5412f);
  hp2.highPass(HP_CUTOFF_HZ, 1.3066f);
}

// Persistent stereo32 shared-clock TX/RX. Only the audio task touches I2S.
bool startMic() {
  audio.controls(cfg.micGain, startThreshold());
  if (!audio.begin()) { micChannel = -1; return false; }
  hp1.reset(); hp2.reset();
  return true;
}

void stopSpeaker() {
  audio.mute(); // Flush DIN/output without stopping microphone clocks.
}

bool startSpeaker(uint32_t sampleRate, uint16_t channels) {
  return sampleRate == SAMPLE_RATE && channels == 1 && audio.begin();
}

bool writeLocalPcm(const uint8_t* pcm, size_t bytes, uint32_t started, uint32_t budget) {
  while (millis() - started < budget && audio.healthy()) {
    if (audio.play(pcm, bytes)) return true;
    delay(1);
  }
  return false;
}

bool drainLocalAudio(uint32_t started, uint32_t budget) {
  while (millis() - started < budget && audio.healthy()) {
    if (!audio.pending() && audio.eofs() - audio.lastFill() >= glasses_stream::Controller::kDrainEofs) {
      audio.mute(); return true;
    }
    audio.discardCapture();
    delay(1);
  }
  audio.mute(); return false;
}

// Local boot cue before Wi-Fi readiness; digital DMA completion is not audibility.
void playStartupGreeting() {
  static_assert(kStartupGreetingSampleRate == SAMPLE_RATE, "Startup PCM must be 16000 Hz");
  static_assert(kStartupGreetingBytes == sizeof(kStartupGreetingPcm) &&
                kStartupGreetingBytes > 0 && !(kStartupGreetingBytes & 1),
                "Startup PCM must be nonempty mono PCM16");
  const int volume = safeStartupVolume(cfg.volume, false);
  if (!volume) { Serial.println("[startup] greeting muted"); return; }
  if (!startSpeaker(SAMPLE_RATE, 1)) { Serial.println("[startup] duplex audio failed"); return; }
  uint8_t buffer[640];
  const uint32_t started = millis();
  bool ok = true;
  for (size_t offset = 0; offset < kStartupGreetingBytes && ok; offset += sizeof(buffer)) {
    const size_t bytes = min(sizeof(buffer), kStartupGreetingBytes - offset);
    memcpy(buffer, kStartupGreetingPcm + offset, bytes);
    ok = scalePcm16(buffer, bytes, volume) && writeLocalPcm(buffer, bytes, started, 10000);
  }
  if (ok) ok = drainLocalAudio(started, 10000);
  audio.mute();
  audio.end(); // Offline startup must not leave unconsumed capture running.
  Serial.println(ok ? "[startup] greeting DMA drained; conversation awaits bridge readiness" :
                      "[startup] greeting playback failed");
}

bool readCaptureFrame() {
  if (audio.pop(capturedAudio)) {
    micChannel = audio.channel();
    return true;
  }
  const auto fault = audio.fault();
  audio.end();
  micChannel = -1; noiseCalibrated = false; talkRequested = false;
  prerollCount = loudFrames = 0; led(false);
  if (!quietMicCheck) Serial.printf("[mic] incomplete duplex frame; capture discarded cause=%s\n",
                                   glasses_audio::DuplexAudio::faultName(fault));
  return false;
}

bool readRawMicFrame() {
  if (!readCaptureFrame()) return false;
  audio.rawSnapshot(micRaw);
  return true;
}

// Read one 20 ms frame from the mic into `frame` (16-bit, filtered,
// gain applied). Returns RMS, or -1 for a failed capture (not silent audio).
float readMicFrame() {
  if (micChannel < 0 || micChannel > 1 || !readCaptureFrame()) return -1;

  float gain = cfg.micGain;
  double sum = 0;

  for (int i = 0; i < FRAME_SAMPLES; i++) {
    float x = capturedAudio.pcm[i]; // Actual AEC near-end PCM before filter/gain.
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
  // Owner discards 260 ms, then measures both raw slots before running AEC.
  if (!readRawMicFrame()) return;
  micChannel = audio.channel();
  if (micChannel < 0) {
    if (!quietMicCheck) Serial.println("[mic] no signal on either known I2S slot");
    return;
  }
  noiseCalibrated = false;
  Serial.printf("[mic] ok, using %s channel; AEC full duplex\n", micChannel ? "right" : "left");
}

// Persistent clocks remove playback/rearm switching. Initial settling and slot
// selection live in the audio owner; diagnostics may discard additional PCM.
#define MIC_SETTLE_FRAMES 13

void calibrateNoise() {

  // Average the quieter 60% of frames so a cough or click
  // during calibration doesn't raise the threshold.
  const int n = 25;
  float levels[n];
  for (int i = 0; i < n; i++) {
    levels[i] = readMicFrame();
    if (levels[i] < 0) return;
  }
  std::sort(levels, levels + n);
  float sum = 0;
  int keep = n * 6 / 10;
  for (int i = 0; i < keep; i++) sum += levels[i];
  noiseFloor = max(sum / keep, 2.0f);
  loudFrames = 0;
  prerollCount = 0;
  noiseCalibrated = true;
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


bool bridgeConfigured() {
  return settingsDurable && parseBridgeUrl(cfg.bridgeUrl.c_str(), bridge) &&
         validDeviceToken(cfg.deviceToken.c_str()) &&
         cfg.rootCa.length() >= 100 && cfg.rootCa.length() <= kMaxRootCaBytes;
}

bool ensureTlsClock() {
  if (time(nullptr) >= 1735689600) return true;
  configTime(0, 0, "time.cloudflare.com", "time.google.com");
  unsigned long start = millis();
  while (millis() - start < 20000) {
    if (time(nullptr) >= 1735689600) return true;
    delay(100);
  }
  Serial.println("[tls] clock unavailable; refusing connection");
  return false;
}

bool readLine(WiFiClientSecure& c, String& line, unsigned long timeoutMs) {
  line = "";
  unsigned long start = millis();
  while (millis() - start < timeoutMs && millis() - exchangeStarted < EXCHANGE_TIMEOUT_MS) {
    if (c.available()) {
      char ch = c.read();
      if (ch == '\n') { line.trim(); return true; }
      if (line.length() >= 512) return false;
      line += ch;
    } else {
      if (!c.connected()) return false;
      delay(2);
    }
  }
  return false;
}

int healthExchange() {
  serverFound = false; // Transport failures require a fresh authenticated health check.
  if (!bridgeConfigured() || !ensureTlsClock()) return -1;
  WiFiClientSecure c;
  c.setCACert(cfg.rootCa.c_str());
  c.setHandshakeTimeout(10);
  c.setTimeout(10);
  exchangeStarted = millis();
  // Pass the DNS hostname, not a resolved IP: mbedTLS checks name and chain.
  if (!c.connect(bridge.host, bridge.port, 10000)) {
    serverFound = false;
    Serial.println("[tls] verified connection failed; no fallback");
    return -1;
  }
  c.printf("GET /health HTTP/1.1\r\nHost: %s:%u\r\n", bridge.host, bridge.port);
  c.printf("Authorization: Bearer %s\r\nX-Session: %s\r\n", cfg.deviceToken.c_str(), bridgeSession.c_str());
  c.print("Accept-Encoding: identity\r\nConnection: close\r\n");
  c.print("\r\n");
  String line;
  uint32_t status = 0;
  if (!readLine(c, line, 10000) || line.length() < 12 ||
      (!line.startsWith("HTTP/1.1 ") && !line.startsWith("HTTP/1.0 ")) ||
      !decimalBounded(line.substring(9, 12).c_str(), 599, status) || status < 100) return -1;
  uint32_t length = 0;
  bool haveLength = false, ended = false;
  String contentType;
  size_t headerBytes = 0;
  for (int count = 0; count < 32; ++count) {
    if (!readLine(c, line, 10000)) return -1;
    headerBytes += line.length() + 2;
    if (headerBytes > 8192) return -1;
    if (!line.length()) { ended = true; break; }
    int colon = line.indexOf(':');
    if (colon <= 0) return -1;
    String name = line.substring(0, colon), value = line.substring(colon + 1);
    name.toLowerCase();
    value.trim();
    if (name == "content-length") {
      if (haveLength || !decimalBounded(value.c_str(), 1440044, length)) return -1;
      haveLength = true;
    } else if (name == "content-type") {
      if (contentType.length()) return -1;
      contentType = value;
      int semicolon = contentType.indexOf(';');
      if (semicolon >= 0) contentType = contentType.substring(0, semicolon);
      contentType.toLowerCase();
    } else if (name == "transfer-encoding" || (name == "content-encoding" && value != "identity")) {
      return -1;
    }
  }
  if (!ended) return -1;
  if (status == 200) {
    uint8_t health[512];
    if (!haveLength || !length || length > sizeof(health) || contentType != "application/json" ||
        !readExact(&c, health, length) || !validHealthBody(health, length, length)) return -1;
  } else {
    // Never print remote error bodies, transcripts, headers or credentials.
    Serial.printf("[bridge] request rejected (%u)\n", (unsigned)status);
    if (status == 401 || status == 403) serverFound = false;
  }
  c.stop();
  serverFound = status == 200;
  return status;
}

bool findServer() {
  if (!bridgeConfigured()) return serverFound = false;
  serverFound = healthExchange() == 200;
  if (serverFound) Serial.println("[bridge] authenticated TLS health check passed");
  return serverFound;
}


void flushLivePlayback() {
  audio.mute();
  liveSpeaker = false;
}

bool cancelStreaming() {
  flushLivePlayback(); // Mute BEFORE a potentially stalled TLS join.
  const bool stopped = voiceTransport.cancel();
  voiceController.reset();
  replacingExchange = false;
  led(false);
  if (!stopped) {
    cfg.listen = false; serverFound = false;
    Serial.println("[stream] cancellation still pending; listening paused");
  }
  return stopped;
}

void streamFailed(const char* boundary) {
  const auto failure = voiceTransport.failure();
  Serial.printf("[stream] failure boundary=%s audio=%s transport=%s detail=%ld heap=%u stack_low=%u\n",
                boundary, glasses_audio::DuplexAudio::faultName(audio.fault()),
                glasses_stream::failureReasonName(failure.reason), (long)failure.detail,
                (unsigned)ESP.getFreeHeap(), (unsigned)uxTaskGetStackHighWaterMark(nullptr));
  cancelStreaming();
  audio.end();
  micChannel = -1; noiseCalibrated = false;
  serverFound = false; lastServerSearch = millis();
  Serial.println("[stream] ended without commit; awaiting authenticated recovery");
}

bool queueLiveInput(const uint8_t* pcm, size_t bytes) {
  if (!voiceController.acceptInput(bytes)) { streamFailed("input_limit"); return false; }
  if (voiceTransport.pushInput(pcm, bytes) != glasses_stream::Status::Ok) {
    streamFailed("input_queue"); return false;
  }
  if (voiceController.atInputLimit()) {
    voiceController.stopInput();
    if (voiceTransport.stopInput() != glasses_stream::Status::Ok) { streamFailed("input_close"); return false; }
    Serial.println("[stream] input limit; AEC capture remains live");
  }
  return true;
}

void rememberFrame() {
  memcpy(preroll[prerollHead], frame, sizeof(frame));
  prerollHead = (prerollHead + 1) % PREROLL_FRAMES;
  if (prerollCount < PREROLL_FRAMES) ++prerollCount;
}

bool beginExchange() {
  glasses_stream::Config config{bridge.host, bridge.port, "/voice", cfg.rootCa.c_str(),
                                cfg.deviceToken.c_str(), bridgeSession.c_str()};
  if (voiceTransport.begin(config) != glasses_stream::Status::Ok) { streamFailed("begin"); return false; }
  voiceController.begin(millis());
  led(true);
  const int first = (prerollHead - prerollCount + PREROLL_FRAMES) % PREROLL_FRAMES;
  for (int i = 0; i < prerollCount; ++i) {
    if (!queueLiveInput(reinterpret_cast<uint8_t*>(preroll[(first + i) % PREROLL_FRAMES]),
                        sizeof(frame))) return false;
  }
  prerollCount = loudFrames = 0;
  return true;
}

bool replaceExchange() {
  audio.takeInterruption();
  ++bargeIns;
  flushLivePlayback();
  replacingExchange = true;
  // Eight seconds of retained capture cover the bounded six-second join.
  // cancelled excludes history; complete preserves an already-played pair.
  const bool released = voiceTransport.interruptAndJoin();
  voiceController.reset(); replacingExchange = false;
  if (!released || !audio.healthy()) {
    cfg.listen = false; streamFailed("interruption_join");
    Serial.println("[stream] interruption handoff failed; listening paused");
    return false;
  }
  if (!beginExchange()) return false;
  Serial.println("[stream] replacement capturing preserved near-end speech");
  return true;
}

void streamStep() {
  using namespace glasses_stream;
  if (!cfg.listen) { streamFailed("paused"); return; }
  if (provisioningAllowed()) { streamFailed("provisioning"); return; }
  if (WiFi.status() != WL_CONNECTED) { streamFailed("wifi_lost"); return; }
  if (!audio.healthy()) { streamFailed("audio_unhealthy"); return; }
  if (voiceController.expired(millis())) { streamFailed("turn_deadline"); return; }
  audio.controls(cfg.micGain, startThreshold());
  if (audio.interruptionPending() && !replaceExchange()) return;
  Event event;
  while (voiceTransport.pollEvent(event)) {
    switch (event.type) {
      case EventType::Ready: voiceController.onReady(); break;
      case EventType::Speaking:
        if (!voiceController.onSpeaking(event.revision) ||
            voiceTransport.stopInput() != Status::Ok) { streamFailed("speaking_order"); return; }
        flushLivePlayback();
        liveSpeaker = true;
        observedAudioFill = audio.lastFill();
        playbackGeneration = event.generation;
        break;
      case EventType::Clear:
        if (!voiceController.onClear(event.revision)) { streamFailed("clear_order"); return; }
        flushLivePlayback(); break;
      case EventType::Done:
        if (!voiceController.onDone(event.revision, audio.eofs(), millis())) { streamFailed("done_order"); return; }
        break;
      case EventType::Complete:
        if (!voiceController.onComplete(event.revision)) { streamFailed("complete_order"); return; }
        cancelStreaming();
        // Capture/calibration and pre-roll remain live; no reinitialization gap.
        Serial.println("[listening]");
        return;
      case EventType::Cancelled: streamFailed("cancelled_event"); return;
      case EventType::Error: streamFailed("error_event"); return;
      case EventType::Closed: streamFailed("closed_event"); return;
    }
  }
  if (liveSpeaker) {
    if (!voiceTransport.outputStillCurrent(playbackGeneration)) { flushLivePlayback(); return; }
    // Keep the owner queue short (<=60 ms); DMA/reference never waits for TLS.
    if (audio.pending() <= sizeof(playback) * 2 && !voiceController.acknowledged()) {
      const size_t bytes = voiceTransport.popOutput(playback, sizeof(playback),
                                  voiceController.revision(), playbackGeneration);
      if (bytes) {
        if (!scalePcm16(playback, bytes, cfg.volume)) { streamFailed("output_pcm"); return; }
        if (!audio.play(playback, bytes)) {
          if (audio.interruptionPending()) replaceExchange(); else streamFailed("output_queue");
          return;
        }
        audio.armInterruption(true);
        if (!voiceTransport.outputStillCurrent(playbackGeneration)) { flushLivePlayback(); return; }
      }
    }
    const auto drain = audio.drain(); // One snapshot: final refill cannot race emptiness.
    if (drain.fill != observedAudioFill) {
      observedAudioFill = drain.fill;
      voiceController.noteWrite(drain.fill, millis());
    }
    const bool queueEmpty = voiceTransport.outputPending() == 0;
    const bool localEmpty = drain.empty;
    if (voiceController.drained(drain.eof, queueEmpty, localEmpty)) {
      if (!audio.finishPlayback()) { replaceExchange(); return; }
      liveSpeaker = false;
      const Status acknowledged = voiceTransport.played(voiceController.revision(), playbackGeneration);
      if (acknowledged == Status::Stale) return;
      if (acknowledged != Status::Ok) { streamFailed("played_ack"); return; }
      voiceController.onPlayed();
      audio.armInterruption(true); // Near-end speech during commit must survive too.
      Serial.println("[stream] audio DMA drained; awaiting history commit");
    } else if (voiceController.drainExpired(millis(), queueEmpty, localEmpty)) {
      streamFailed("drain_deadline"); return;
    }
  }
  // Physical capture is independent of the transport input half-close.
  if (readMicFrame() < 0) { streamFailed("capture_frame"); return; }
  rememberFrame();
  if (voiceController.acceptingInput() &&
      !queueLiveInput(reinterpret_cast<uint8_t*>(frame), sizeof(frame))) return;
}

void resumeListening(bool reuseNoise) {
  if (!cancelStreaming()) return;
  prerollCount = loudFrames = 0; talkRequested = false; led(false);
  if (!cfg.listen || !serverFound || WiFi.status() != WL_CONNECTED || provisioningAllowed()) {
    audio.end(); micChannel = -1; return;
  }
  const bool reuseCalibration = reuseNoise && noiseCalibrated && audio.healthy();
  if (!startMic()) {
    micChannel = -1;
    Serial.println("[mic] duplex startup failed; retrying only while armed"); return;
  }
  if (!reuseCalibration) {
    detectMicChannel();
    if (micChannel < 0) { audio.end(); return; }
    calibrateNoise();
  }
  if (micChannel < 0 || !noiseCalibrated) return;
  audio.controls(cfg.micGain, startThreshold());
  Serial.println("[listening]");
}

void listenStep() {
  const float level = readMicFrame();
  if (level < 0) return;
  rememberFrame();
  audio.controls(cfg.micGain, startThreshold());
  if (level > startThreshold()) ++loudFrames;
  else {
    loudFrames = 0;
    const float rate = level < noiseFloor ? 0.05f : 0.002f;
    noiseFloor = max(2.0f, noiseFloor + (level - noiseFloor) * rate);
  }
  if (!talkRequested && !(cfg.handsFree && loudFrames >= START_FRAMES)) return;
  talkRequested = false;
  if (beginExchange()) Serial.println("[stream] connecting; capturing AEC near-end PCM");
}

// ==========================================
// WI-FI
// ==========================================
void wifiDiagnostics() {
  const uint32_t reason = lastWifiDisconnectReason.load(std::memory_order_relaxed);
  Serial.printf("[wifi] status=%d reason=%u name=%s ssid_saved=%s\n",
                (int)WiFi.status(), (unsigned)reason,
                WiFi.disconnectReasonName((wifi_err_reason_t)reason),
                cfg.ssid.length() ? "yes" : "no");
  const bool reconnect = WiFi.getAutoReconnect();
  WiFi.setAutoReconnect(false);
  if (WiFi.status() != WL_CONNECTED) {
    WiFi.disconnect(false, false); // Preserve saved credentials; stop failed association during scan.
    delay(100);
  }
  const int count = WiFi.scanNetworks(false, true, false, 200);
  int exact = 0, variants = 0;
  for (int i = 0; i < count; ++i) {
    const auto* ap = static_cast<const wifi_ap_record_t*>(WiFi.getScanInfoByIndex(i));
    if (!ap || !cfg.ssid.length() || strcasecmp((const char*)ap->ssid, cfg.ssid.c_str())) continue;
    const bool same = !strcmp((const char*)ap->ssid, cfg.ssid.c_str());
    same ? ++exact : ++variants;
    Serial.printf("[wifi] target=%s ssid=%s rssi=%d channel=%u auth=%u\n",
                  same ? "exact" : "case_variant", (const char*)ap->ssid,
                  ap->rssi, (unsigned)ap->primary, (unsigned)ap->authmode);
  }
  Serial.printf("[wifi] scan_count=%d exact=%d case_variants=%d band=2.4GHz\n", count, exact, variants);
  WiFi.scanDelete();
  WiFi.setAutoReconnect(reconnect);
}

bool connectWiFi(unsigned long timeoutMs) {
  if (cfg.ssid.length() == 0 || !wifiJoin.begin()) return false;

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  // Stop the SDK's retry owner before replacing its association/configuration.
  WiFi.setAutoReconnect(false);
  WiFi.disconnect(false, false); // Never erase saved AP credentials.
  delay(100);
  WiFi.begin(cfg.ssid.c_str(), cfg.pass.c_str());
  WiFi.setAutoReconnect(true); // Subsequent recovery belongs to Arduino's Wi-Fi event handler.

  Serial.print("[wifi] connecting");
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && !wifiJoin.cancelled() && millis() - start < timeoutMs) {
    delay(500);
    Serial.print(".");
    handleSerial();
  }
  Serial.println();
  const bool cancelled = wifiJoin.cancelled();
  const bool joined = wifiJoin.finish(WiFi.status() == WL_CONNECTED);
  if (cancelled) return false; // A USB pause during resume must not arm the microphone afterward.

  if (!joined) {
    const uint32_t reason = lastWifiDisconnectReason.load(std::memory_order_relaxed);
    Serial.printf("[wifi] join failed status=%d reason=%u (%s)\n", (int)WiFi.status(),
                  (unsigned)reason, WiFi.disconnectReasonName((wifi_err_reason_t)reason));
    return false;
  }

  wifiWasConnected = true;
  wifiJoinRequired = false;
  Serial.printf("[wifi] connected ip=%s rssi=%d\n",
                WiFi.localIP().toString().c_str(), WiFi.RSSI());
  return true;
}

// No AP or web portal: provisioning is available only over physical USB.
bool provisioningAllowed() {
  if (usbReceiveReady && provisioningOpen && millis() - provisioningStarted < PROVISION_TIMEOUT_MS) return true;
  provisioningOpen = false;
  return false;
}

void openProvisioning() {
  if (!usbReceiveReady) {
    provisioningOpen = false;
    Serial.println("[error] USB receive capacity unavailable; credential writes disabled");
    return;
  }
  provisioningOpen = true;
  provisioningStarted = millis();
  cfg.listen = false;
  talkRequested = false;
  resumeListening();
  Serial.println("[provision] USB credential writes unlocked for 120 seconds; listening paused");
}

// ==========================================
// SERIAL COMMANDS
// ==========================================
void printHelp() {
  Serial.println(
    "\nCommands:\n"
    "  status              connection, live stream and memory headroom\n"
    "  pair [device token] first setup, or authenticated USB maintenance\n"
    "  wifi                paused-only 2.4 GHz target scan and failure reason\n"
    "  ssid/pass           Wi-Fi provisioning (USB unlock required)\n"
    "  bridge <https URL>  explicit bridge origin (USB unlock required)\n"
    "  token/ca            device token / base64 PEM CA (USB unlock required)\n"
    "  connect             join saved Wi-Fi; no AP fallback\n"
    "  volume <0-100>      speaker volume\n"
    "  sensitivity <1-10>  speech detection (higher = more sensitive)\n"
    "  gain <1-64>         microphone gain\n"
    "  mode auto|button    auto = start on speech, button = press BOOT\n"
    "  calibrate           learn your voice level (ignore people further away)\n"
    "  calibrate off       forget the calibration\n"
    "  mic                 show mic level for 5 s\n"
    "  micdiag             find out why the mic gives no signal\n"
    "  tone                play a test beep\n"
    "  pause / resume      stop / start listening\n"
    "  forget              clear credentials (USB unlock required)\n"
    "  restart");
}

void printStatus() {
  Serial.printf("[status] firmware=%s wifi=%s rssi=%d bridge_configured=%s authenticated=%s usb_unlocked=%s usb_rx=%s\n",
                FW_VERSION, WiFi.status() == WL_CONNECTED ? "connected" : "disconnected",
                WiFi.RSSI(), bridgeConfigured() ? "yes" : "no", serverFound && bridgeConfigured() ? "yes" : "no",
                provisioningAllowed() ? "yes" : "no", usbReceiveReady ? "ready" : "failed");
  Serial.printf("[status] mode=%s voice=%.0f volume=%d "
                "sensitivity=%d gain=%d listening=%s mic=%s noise=%.0f "
                "threshold=%.0f psram=%u stream=%s input=%u heap_free=%u psram_free=%u\n",
                cfg.handsFree ? "auto" : "button", cfg.voiceRef,
                cfg.volume, cfg.sensitivity, cfg.micGain,
                cfg.listen ? "yes" : "no",
                micChannel < 0 ? "none" : (micChannel ? "right" : "left"),
                noiseFloor, startThreshold(), (unsigned)ESP.getPsramSize(),
                replacingExchange ? "cancelling" : voiceController.status(), (unsigned)voiceController.inputBytes(),
                (unsigned)ESP.getFreeHeap(), (unsigned)ESP.getFreePsram());
  Serial.printf("[status] duplex=full aec=%s capture=%s aec_chunk=%u reference_lead_ms=10 dma_eofs=%u barge_ins=%u\n",
                audio.healthy() ? "fd" : "off", audio.healthy() ? "live" : "stopped",
                (unsigned)audio.chunkSize(), (unsigned)audio.eofs(), (unsigned)bargeIns);
}

// Work out why the mic gives no signal: look at the raw data, then test
// the SD line with the internal pull-up / pull-down resistors.
void micDiagnostics() {
  if (!startMic()) { micChannel = -1; Serial.println("[micdiag] I2S failed"); return; }
  for (int i = 0; i < MIC_SETTLE_FRAMES; i++) if (!readRawMicFrame()) return;

  int32_t lo[2] = {INT32_MAX, INT32_MAX}, hi[2] = {INT32_MIN, INT32_MIN};
  for (int f = 0; f < 10; f++) {
    if (!readRawMicFrame()) return;
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

  audio.end(); micChannel = -1;
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
    Serial.println("[micdiag] signal detected; this does not prove wiring or electrical safety");
  } else if (up == 50 && down == 0) {
    Serial.println("[micdiag] RESULT: the mic is not driving SD (line is "
                   "floating). Check: mic VDD->3V3 and GND->GND (power), "
                   "SCK->D1 and WS->D2 (clock), and the SD->D0 wire itself.");
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
  if (!startMic()) { micChannel = -1; Serial.println("[mic] I2S failed"); return; }
  detectMicChannel();
  if (micChannel < 0) return;
  for (int i = 0; i < MIC_SETTLE_FRAMES; i++) if (readMicFrame() < 0) return;
  Serial.println("[mic] speak now - level for 5 s (# = loudness)");
  unsigned long start = millis();
  float peak = 0;
  while (millis() - start < 5000) {
    float level = 0;
    for (int i = 0; i < 5; i++) {
      float sample = readMicFrame();
      if (sample < 0) return;
      level = max(level, sample);
    }
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
  const int quietFrames = 100;   // 2 s
  const int speakFrames = 200;   // 4 s
  static float levels[speakFrames];

  if (!startMic()) { micChannel = -1; Serial.println("[calibrate] I2S failed"); return; }
  detectMicChannel();
  if (micChannel < 0) return;
  for (int i = 0; i < MIC_SETTLE_FRAMES; i++) if (readMicFrame() < 0) return;

  Serial.println("[calibrate] step 1/2: stay QUIET for 2 seconds...");
  for (int i = 0; i < quietFrames; i++) {
    levels[i] = readMicFrame();
    if (levels[i] < 0) return;
  }
  std::sort(levels, levels + quietFrames);
  float room = max(levels[quietFrames / 2], 2.0f);

  Serial.println("[calibrate] step 2/2: get ready to speak...");
  for (int n = 3; n > 0; n--) {
    Serial.printf("[calibrate] %d\n", n);
    for (int i = 0; i < 40; i++) if (readMicFrame() < 0) return; // 0.8 s
  }
  Serial.println("[calibrate] SPEAK NOW at your normal distance and volume "
                 "(e.g. count from one to ten) - 4 seconds, LED is on");
  led(true);
  clippedSamples = 0;
  for (int i = 0; i < speakFrames; i++) {
    levels[i] = readMicFrame();
    if (levels[i] < 0) return;
  }
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
    if (!saveSettings()) return;
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
  if (!saveSettings()) return;
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
  const uint32_t started = millis();
  for (int block = 0; block < 25; block++) {          // 0.5 s, 1 kHz
    for (int i = 0; i < 320; i++) {
      buf[i] = amplitude * sinf(2 * PI * 1000 * (block * 320 + i) / 16000.0f);
    }
    if (!writeLocalPcm(reinterpret_cast<uint8_t*>(buf), sizeof(buf), started, 3000)) {
      audio.mute(); Serial.println("[tone] failed"); resumeListening(); return;
    }
  }
  const bool drained = drainLocalAudio(started, 3000);
  Serial.println(drained ? "[tone] DMA drained" : "[tone] failed");
  resumeListening();
}

void runCommand(String line) {
  if (line.length() == 0) return;

  int sp = line.indexOf(' ');
  String cmd = sp < 0 ? line : line.substring(0, sp);
  String arg = sp < 0 ? "" : line.substring(sp + 1);
  cmd.toLowerCase();
  if (cmd != "pass" && cmd != "ssid") arg.trim();
  if (!wifiJoin.permits(cmd.c_str())) {
    Serial.println("[error] Wi-Fi join in progress; use pause, status or ready");
    return;
  }
  if ((cmd == "ssid" || cmd == "pass" || cmd == "bridge" || cmd == "token" || cmd == "ca" ||
       cmd == "forget" || cmd == "connect") && !provisioningAllowed()) {
    Serial.println(usbReceiveReady ? "[error] hold BOOT for 3 seconds to unlock USB provisioning" :
                                    "[error] USB receive capacity unavailable; credential writes disabled");
    return;
  }
  const bool credentialWrite = cmd == "ssid" || cmd == "pass" || cmd == "bridge" ||
                               cmd == "token" || cmd == "ca" || cmd == "forget" || cmd == "connect";
  const bool interruptsAudio = credentialWrite || cmd == "gain" || cmd == "calibrate" ||
                               cmd == "micdiag" || cmd == "mic" || cmd == "tone" ||
                               cmd == "mode" || cmd == "volume" || cmd == "sensitivity" ||
                               cmd == "resume" || cmd == "restart";
  const bool interruptedStream = interruptsAudio && voiceController.active();
  if ((credentialWrite || interruptedStream) && !cancelStreaming()) return;

  if (cmd == "help" || cmd == "?") {
    printHelp();
  } else if (cmd == "ready") {
    if (arg.length() != 32) return;
    for (size_t i = 0; i < arg.length(); ++i)
      if (!((arg[i] >= '0' && arg[i] <= '9') || (arg[i] >= 'a' && arg[i] <= 'f'))) return;
    if (setupComplete) Serial.printf("[ready] firmware=%s nonce=%s\n", FW_VERSION, arg.c_str());
    else pendingReadiness = arg; // Setup may service USB while joining Wi-Fi.
  } else if (cmd == "pair") {
    if (bridgeConfigured()) {
      if (!validDeviceToken(arg.c_str()) ||
          mbedtls_ct_memcmp(arg.c_str(), cfg.deviceToken.c_str(), cfg.deviceToken.length())) {
        Serial.println("[error] current device credential required for USB pairing");
        return;
      }
    } else if (arg.length()) {
      Serial.println("[error] first USB pairing takes no argument");
      return;
    }
    openProvisioning();
  } else if (cmd == "status") {
    printStatus();
  } else if (cmd == "wifi") {
    if (cfg.listen || voiceController.active()) {
      Serial.println("[error] pause listening before Wi-Fi diagnostics");
      return;
    }
    wifiDiagnostics();
  } else if (cmd == "ssid") {
    if (!arg.length() || arg.length() > 32) { Serial.println("[error] invalid network name"); return; }
    cfg.ssid = arg;
    wifiJoinRequired = true;
    if (!saveSettings()) return;
    Serial.println("[ok] network name saved");
  } else if (cmd == "pass") {
    if (arg.length() < 8 || arg.length() > 63) { Serial.println("[error] WPA password must be 8-63 bytes"); return; }
    cfg.pass = arg;
    wifiJoinRequired = true;
    if (!saveSettings()) return;
    Serial.println("[ok] password saved");
  } else if (cmd == "connect") {
    if (!bridgeConfigured()) { Serial.println("[error] configure bridge, token and CA first"); return; }
    audio.end(); micChannel = -1;
    serverFound = false;
    if (connectWiFi(20000)) findServer();
    resumeListening();
  } else if (cmd == "bridge") {
    BridgeEndpoint parsed;
    if (!parseBridgeUrl(arg.c_str(), parsed)) { Serial.println("[error] HTTPS DNS origin required"); return; }
    serverFound = false;
    if (cfg.bridgeUrl != arg) {
      cfg.deviceToken = ""; // Never retain the old bearer in RAM after an origin-change attempt.
      bool ok = prefs.begin("va", false);
      if (ok) {
        ok = storeChangedBridgeOrigin(prefs, arg.c_str());
        prefs.end();
      }
      if (!settingsSaved(ok)) return;
      cfg.bridgeUrl = arg;
    }
    if (!saveSettings()) return;
    serverFound = false;
    Serial.println("[ok] bridge origin saved");
  } else if (cmd == "token") {
    if (!validDeviceToken(arg.c_str())) { Serial.println("[error] invalid device credential"); return; }
    cfg.deviceToken = arg;
    if (!saveSettings()) return;
    serverFound = false;
    Serial.println("[ok] device credential saved");
  } else if (cmd == "ca") {
    // Provisioning has cancelled/joined the transport before any credential mutation.
    uint8_t pem[kMaxRootCaBytes + 1];
    size_t decoded = 0;
    if (mbedtls_base64_decode(pem, kMaxRootCaBytes, &decoded,
                                    (const uint8_t*)arg.c_str(), arg.length()) ||
        decoded < 100 || decoded > kMaxRootCaBytes) { Serial.println("[error] invalid CA encoding"); return; }
    pem[decoded] = 0;
    mbedtls_x509_crt certificate;
    mbedtls_x509_crt_init(&certificate);
    int result = mbedtls_x509_crt_parse(&certificate, pem, decoded + 1);
    mbedtls_x509_crt_free(&certificate);
    if (result) { Serial.println("[error] invalid CA certificate"); return; }
    cfg.rootCa = (const char*)pem;
    if (!saveSettings()) return;
    serverFound = false;
    Serial.println("[ok] trust anchor saved");
  } else if (cmd == "volume") {
    cfg.volume = constrain(arg.toInt(), 0, 100);
    if (!saveSettings()) return;
    Serial.printf("[ok] volume=%d\n", cfg.volume);
  } else if (cmd == "sensitivity") {
    cfg.sensitivity = constrain(arg.toInt(), 1, 10);
    if (!saveSettings()) return;
    Serial.printf("[ok] sensitivity=%d threshold=%.0f\n",
                  cfg.sensitivity, startThreshold());
  } else if (cmd == "gain") {
    cfg.micGain = constrain(arg.toInt(), 1, 64);
    noiseCalibrated = false;
    bool hadCalibration = cfg.voiceRef > 0;
    cfg.voiceRef = 0;   // levels change with gain
    if (!saveSettings()) return;
    Serial.printf("[ok] gain=%d\n", cfg.micGain);
    if (hadCalibration) {
      Serial.println("[ok] calibration cleared - run 'calibrate' again");
    }
    resumeListening();
  } else if (cmd == "mode") {
    arg.toLowerCase();
    if (arg != "auto" && arg != "button") { Serial.println("[error] mode must be auto or button"); return; }
    cfg.handsFree = arg != "button";
    if (!saveSettings()) return;
    Serial.printf("[ok] mode=%s\n", cfg.handsFree ? "auto" : "button");
  } else if (cmd == "calibrate") {
    if (arg == "off") {
      cfg.voiceRef = 0;
      if (!saveSettings()) return;
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
  } else if (cmd == "pause") {
    wifiJoin.pause();
    cfg.listen = false;
    resumeListening();
    Serial.println("[ok] listening paused");
  } else if (cmd == "resume") {
    if (provisioningAllowed()) {
      cfg.listen = false;
      Serial.println("[error] USB provisioning is unlocked; resume after it expires");
      return;
    }
    cfg.listen = false; // Join and authenticate while still paused; only explicit resume may arm.
    audio.end(); micChannel = -1;
    if (!cfg.ssid.length() || !bridgeConfigured()) {
      Serial.println("[error] configure Wi-Fi, bridge, token and CA first");
      return;
    }
    if ((wifiJoinRequired || WiFi.status() != WL_CONNECTED) && !connectWiFi(20000)) return;
    if (provisioningAllowed() || !findServer() || provisioningAllowed()) return;
    cfg.listen = true;
    resumeListening();
  } else if (cmd == "forget") {
    cfg.ssid = "";
    cfg.pass = "";
    cfg.bridgeUrl = "";
    cfg.deviceToken = "";
    cfg.rootCa = "";
    serverFound = false;
    cfg.listen = false;
    audio.end(); micChannel = -1;
    if (!saveSettings()) return;
    Serial.println("[ok] device credentials cleared");
  } else if (cmd == "restart") {
    ESP.restart();
  } else {
    Serial.println("[error] unknown command - type help");
  }
  if (interruptedStream && cfg.listen && micChannel < 0 && !voiceController.active()) {
    resumeListening(true);
  }
}

void handleSerial() {
  static bool overflow = false;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      const bool discarded = overflow;
      String command = std::move(serialLine);
      overflow = false;
      // Clear parser state before dispatch: connect/resume pump USB while joining.
      if (discarded) {
        Serial.println("[error] command too long; discarded");
      } else if (command.length()) {
        runCommand(std::move(command));
      }
    } else if (!overflow && serialLine.length() < kSerialCommandLimit) {
      serialLine += c;
    } else {
      serialLine = "";
      overflow = true;
    }
  }
}

// BOOT short press: talk. Hold 3 s: unlock USB provisioning; never opens an AP.
void checkBootButton() {
  static unsigned long pressedAt = 0;
  static bool held = false;
  if (digitalRead(BOOT_BUTTON) == LOW) {
    if (pressedAt == 0) pressedAt = millis();
    if (!held && millis() - pressedAt > 3000) {
      held = true;
      openProvisioning();
    }
  } else {
    if (pressedAt && !held && millis() - pressedAt > 40) {
      if (serverFound && WiFi.status() == WL_CONNECTED && !provisioningAllowed()) {
        if (!cfg.listen) {
          cfg.listen = true;
          resumeListening();
        }
        if (micChannel >= 0) talkRequested = true;
      }
    }
    pressedAt = 0;
    held = false;
  }
}

// ==========================================
// SETUP
// ==========================================
void setupDevice() {
  digitalWrite(SPK_DIN, LOW);
  pinMode(SPK_DIN, OUTPUT);
#if ARDUINO_USB_MODE && ARDUINO_USB_CDC_ON_BOOT
  // HWCDC 3.3.10 returns the actual allocated queue length (zero on failure).
  // Its 256-byte default drops overflow in the ISR. Size RX, not just TX,
  // before begin(), for a complete 6000-byte command plus its terminator.
  usbReceiveReady = reserveUsbCommandBuffer(Serial);
  // Don't stall the audio loop when no serial monitor is open.
  Serial.setTxBufferSize(4096);
  Serial.setTxTimeoutMs(20);
#endif
  Serial.begin(115200);
  delay(1000);
  if (!usbReceiveReady)
    Serial.println("[error] USB receive capacity unavailable; credential writes disabled");

  pinMode(STATUS_LED, OUTPUT);
  led(false);
  pinMode(BOOT_BUTTON, INPUT_PULLUP);

  Serial.printf("\n=== Voice assistant v%s ===\n", FW_VERSION);
  loadSettings();
  char session[33];
  snprintf(session, sizeof(session), "%08lx%08lx%08lx%08lx",
           (unsigned long)esp_random(), (unsigned long)esp_random(),
           (unsigned long)esp_random(), (unsigned long)esp_random());
  bridgeSession = session;
  Serial.println("[reference] pin map and power must be confirmed before flashing");


  setupFilter();
  WiFi.onEvent([](arduino_event_t* event) {
    lastWifiDisconnectReason.store(event->event_info.wifi_sta_disconnected.reason,
                                   std::memory_order_relaxed);
  }, ARDUINO_EVENT_WIFI_STA_DISCONNECTED);
  playStartupGreeting(); // Local power-on speech must not wait for Wi-Fi or mic readiness.
  if (cfg.ssid.length() == 0 || !bridgeConfigured()) {
    Serial.println("[setup] unconfigured; use USB 'pair' for first setup. No radio portal is available.");
    return;
  }
  if (!connectWiFi(20000)) return;

  findServer();
  lastServerSearch = millis();

  resumeListening();
}

void setup() {
  setupDevice();
  setupComplete = true;
  if (pendingReadiness.length()) {
    Serial.printf("[ready] firmware=%s nonce=%s\n", FW_VERSION, pendingReadiness.c_str());
    pendingReadiness = "";
  }
}

// ==========================================
// MAIN LOOP
// ==========================================
void loop() {
  handleSerial();

  checkBootButton();
  if (provisioningAllowed()) { delay(20); return; }
  if (cfg.ssid.length() == 0 || !bridgeConfigured()) { delay(100); return; }


  if (WiFi.status() != WL_CONNECTED) {
    if (wifiWasConnected) {
      Serial.println("[wifi] lost connection, reconnecting...");
      wifiWasConnected = false;
      serverFound = false;
      resumeListening();
    }
    // Arduino's disconnect-event handler owns retries; status has no "connecting" state.
    delay(100);
    return;
  }

  if (!wifiWasConnected) {
    wifiWasConnected = true;
    Serial.printf("[wifi] connected ip=%s rssi=%d\n",
                  WiFi.localIP().toString().c_str(), WiFi.RSSI());
  }

  if (!serverFound) {
    if (millis() - lastServerSearch > 30000) {
      lastServerSearch = millis();
      audio.end(); micChannel = -1;
      if (findServer()) resumeListening();
    }
    delay(20);
    return;
  }

  if (!cfg.listen) { delay(20); return; } // No mic restart/calibration while paused.
  if (voiceController.active()) {
    streamStep();
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
      else audio.end();
    }
    delay(20);
    return;
  }


  listenStep();
}
