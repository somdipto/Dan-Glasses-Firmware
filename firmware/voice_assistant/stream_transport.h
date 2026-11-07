#pragma once
#include "stream_protocol.h"
#include <Arduino.h>
#ifndef NODEBUG_WEBSOCKETS
#error "Build every translation unit with -DNODEBUG_WEBSOCKETS; dependency debug logs include authentication headers"
#endif
#include <WebSocketsClient.h>
#if WEBSOCKETS_VERSION_INT != 2007001
#error "The stream adapter requires audited WebSockets 2.7.1"
#endif
#include <atomic>
#include <new>
#include <esp_heap_caps.h>
#include <esp_system.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <freertos/task.h>
#include <lwip/dns.h>
#include <lwip/tcpip.h>

// Dependency: Links2004 WebSockets 2.7.1, tag commit
// 3af6593a00bd4dc8b18be5082575618cf2acc44d; ESP32 core 3.3.10.
// Compile ALL translation units (including the library) with NODEBUG_WEBSOCKETS.
// Its stock loop reconnects and its TLS/header defaults are not bounded enough.
// This adapter uses the maintained handshake/frame implementation, not loop().
namespace glasses_stream {
struct Config {
  const char* host;
  uint16_t port;
  const char* path;
  const char* ca;
  const char* bearer;
  const char* session;
};
namespace detail {
inline bool elapsed(uint32_t start, uint32_t duration) { return uint32_t(millis() - start) >= duration; }
inline void erase(void* pointer, size_t size) {
  volatile uint8_t* bytes = static_cast<volatile uint8_t*>(pointer);
  while (size--) *bytes++ = 0;
}

// lwIP's synchronous DNS API has no caller timeout. Keep a single process-life
// callback slot, containing no credentials. A cancelled lookup may finish later,
// but cannot touch a task, transport, configuration, or newly started lookup.
struct DnsSlot {
  portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;
  char host[254] = {};
  ip_addr_t address = {};
  bool busy = false, finished = false, abandoned = false, success = false;
};
inline DnsSlot& dnsSlot() { static DnsSlot slot; return slot; }
inline void dnsResult(const char*, const ip_addr_t* address, void* argument) {
  auto* slot = static_cast<DnsSlot*>(argument);
  portENTER_CRITICAL(&slot->mux);
  if (address) slot->address = *address;
  slot->success = address != nullptr; slot->finished = true;
  if (slot->abandoned) slot->busy = false;
  portEXIT_CRITICAL(&slot->mux);
}
inline void dnsStart(void* argument) {
  auto* slot = static_cast<DnsSlot*>(argument);
  ip_addr_t address;
  const err_t result = dns_gethostbyname(slot->host, &address, dnsResult, slot);
  if (result != ERR_INPROGRESS) dnsResult(nullptr, result == ERR_OK ? &address : nullptr, slot);
}
inline bool resolve(const char* host, IPAddress& address, const std::atomic<bool>& cancel) {
  DnsSlot& slot = dnsSlot();
  portENTER_CRITICAL(&slot.mux);
  if (slot.busy) { portEXIT_CRITICAL(&slot.mux); return false; }
  slot.busy = true; slot.finished = slot.abandoned = slot.success = false;
  std::memcpy(slot.host, host, std::strlen(host) + 1);
  portEXIT_CRITICAL(&slot.mux);
  if (tcpip_try_callback(dnsStart, &slot) != ERR_OK) {
    portENTER_CRITICAL(&slot.mux); slot.busy = false; portEXIT_CRITICAL(&slot.mux);
    return false;
  }
  const uint32_t start = millis();
  while (!cancel.load() && !elapsed(start, 1500)) {
    portENTER_CRITICAL(&slot.mux);
    if (slot.finished) {
      const bool success = slot.success && IP_IS_V4(&slot.address);
      if (success) address = IPAddress(ip4_addr_get_u32(ip_2_ip4(&slot.address)));
      slot.busy = false;
      portEXIT_CRITICAL(&slot.mux);
      return success;
    }
    portEXIT_CRITICAL(&slot.mux);
    vTaskDelay(pdMS_TO_TICKS(2));
  }
  portENTER_CRITICAL(&slot.mux);
  slot.abandoned = true;
  if (slot.finished) slot.busy = false;
  portEXIT_CRITICAL(&slot.mux);
  return false;
}

// The stock library restarts its read timeout on every received byte. Virtual
// connected/read/write guards impose an ABSOLUTE operation budget even on a
// malicious trickle peer. Only the networking owner ever touches this object.
class SecureClient final : public WiFiClientSecure {
 public:
  SecureClient(const std::atomic<bool>& cancel, uint32_t turnStart, FailureLatch& failure)
      : cancel_(cancel), io_(turnStart), failure_(failure) {}
  using WiFiClientSecure::read;
  void budget(uint32_t duration = 1000) { budgetFrom(millis(), duration); }
  void budgetFrom(uint32_t start, uint32_t duration, FailureReason timeout = FailureReason::OperationTimeout) {
    io_.budget(start, duration); timeoutReason_ = timeout;
  }
  void closing() { io_.closing(millis()); timeoutReason_ = FailureReason::OperationTimeout; }
  bool allowed() {
    FailureReason blocked = io_.blocked(millis(), cancel_.load());
    if (blocked == FailureReason::OperationTimeout) blocked = timeoutReason_;
    if (blocked != FailureReason::None) failure_.record(blocked);
    return blocked == FailureReason::None;
  }
  int32_t tlsError() const { return sslclient->last_error; }
  uint8_t connected() override {
    if (!allowed()) return 0;
    const uint8_t value = WiFiClientSecure::connected();
    if (!value) failure_.record(FailureReason::SocketLost);
    return value;
  }
  int available() override {
    if (!allowed()) return 0;
    // Core 3.3.10's available() discards data_to_read's numeric TLS failure
    // when stop() runs. Preserve its exact logic but latch that error first.
    const int peeked = sslclient->peek_buf >= 0;
    if (!_connected) return peeked;
    const int value = data_to_read(sslclient.get());
    if (value < 0) {
      failure_.record(FailureReason::TlsRead, value);
      WiFiClientSecure::stop();
      return peeked;
    }
    return value + peeked;
  }
  int read(uint8_t* data, size_t size) override {
    if (!allowed()) return -1;
    const int value = WiFiClientSecure::read(data, size);
    if (size && value < 0) failure_.record(FailureReason::TlsRead, value);
    return value;
  }
  size_t write(const uint8_t* data, size_t size) override {
    size_t sent = 0;
    // A quiet peer is not a dead socket. Bound one TLS write, not the whole reply wait.
    while (sent < size && allowed()) {
      const int result = mbedtls_ssl_write(&sslclient->ssl_ctx, data + sent, size - sent);
      if (result > 0) sent += static_cast<size_t>(result);
      else if (result != MBEDTLS_ERR_SSL_WANT_READ && result != MBEDTLS_ERR_SSL_WANT_WRITE) {
        failure_.record(FailureReason::TlsWrite, result); break;
      }
      else vTaskDelay(pdMS_TO_TICKS(1));
    }
    return sent;
  }
 private:
  const std::atomic<bool>& cancel_;
  IoBudget io_;
  FailureLatch& failure_;
  FailureReason timeoutReason_ = FailureReason::OperationTimeout;
};

class Socket final : public WebSocketsClient {
 public:
  bool open(const Config& config, const std::atomic<bool>& cancel, uint32_t turnStart) {
    beginSslWithCA(config.host, config.port, config.path, config.ca, "glasses.v1");
    disableHeartbeat();
    char headers[384];
    const int count = snprintf(headers, sizeof(headers), "Authorization: Bearer %s\r\nX-Session: %s", config.bearer, config.session);
    if (count <= 0 || static_cast<size_t>(count) >= sizeof(headers)) {
      erase(headers, sizeof(headers)); return fail(FailureReason::Config);
    }
    setExtraHeaders(headers); erase(headers, sizeof(headers));
    IPAddress address;
    if (!resolve(config.host, address, cancel)) return fail(cancel.load() ? FailureReason::LocalCancel : FailureReason::Dns);
    auto* secure = new (std::nothrow) SecureClient(cancel, turnStart, failure_);
    if (!secure) return fail(FailureReason::NoMemory);
    _client.ssl = secure; _client.tcp = secure; secure_ = secure;
    secure->setCACert(config.ca);
    secure->setHandshakeTimeout(2);  // Seconds; core default is 120 seconds.
    secure->setTimeout(1000);
    secure->setConnectionTimeout(1500);
    secure->budget(4000);
    // Explicit IP + original DNS host overload preserves SNI and certificate
    // hostname verification without entering an unbounded synchronous resolver.
    if (!secure->connect(address, config.port, config.host, config.ca, nullptr, nullptr))
      return fail(cancel.load() ? FailureReason::LocalCancel : FailureReason::TlsConnect, secure->tlsError());
    if (!secure->allowed()) return false;
    secure->budget();
    _client.status = WSC_HEADER;
    _client.cIsUpgrade = false; _client.cIsWebsocket = false;
    sendHeader(&_client);
    // Library replaces cProtocol on receipt but otherwise leaves the requested
    // value intact. Blank it now so an omitted response protocol is rejected.
    _client.cProtocol = "";
    headerStart_ = millis();
    return secure->allowed() && failure_.get().reason == FailureReason::None;
  }
  bool poll() {
    if (!secure_) return fail(FailureReason::SocketState);
    // Header/frame deadlines never renew on received bytes or interleaved sends.
    if (_client.status == WSC_HEADER) {
      if (elapsed(headerStart_, 3000)) return fail(FailureReason::HeaderTimeout);
      secure_->budgetFrom(headerStart_, 3000, FailureReason::HeaderTimeout);
      size_t budget = 512;
      while (budget-- && secure_->available() > 0) {
        const int value = secure_->read();
        if (value < 0) return fail(FailureReason::HeaderRead);
        if (++headerBytes_ > 4096) return fail(FailureReason::HeaderLimit, headerBytes_);
        if (value == '\n') {
          line_[lineSize_] = 0;
          String line(line_);
          lineSize_ = 0;
          const bool final = line == "\r" || line.length() == 0;
          if (final && (_client.cProtocol != "glasses.v1" || _client.cExtensions.length()))
            return fail(FailureReason::HeaderProtocol, _client.cCode);
          handleHeader(&_client, &line);
          if (_client.status == WSC_CONNECTED) return true;
          if (_client.status != WSC_HEADER || !_client.tcp) return fail(FailureReason::HeaderRejected);
        } else {
          if (lineSize_ + 1 >= sizeof(line_)) return fail(FailureReason::HeaderLimit, lineSize_ + 1);
          line_[lineSize_++] = static_cast<char>(value);
        }
      }
      return secure_->connected();
    }
    if (_client.status != WSC_CONNECTED) return fail(FailureReason::SocketState);
    if (frameStarted_ && elapsed(frameStart_, 15000)) return fail(FailureReason::FrameTimeout);
    secure_->budgetFrom(frameStarted_ ? frameStart_ : millis(), 15000, FailureReason::FrameTimeout);
    if (!secure_->connected()) return false;
    if (!secure_->available()) return failure_.get().reason == FailureReason::None;
    if (!frameStarted_) {
      frameStarted_ = true; frameStart_ = millis();
      secure_->budgetFrom(frameStart_, 15000, FailureReason::FrameTimeout);
    }
    // Preflight ONLY the library header buffer, then delegate framing and payload
    // handling to WebSockets. Reject large/fragmented/masked/extension frames
    // before its per-frame malloc, not after a potentially large allocation.
    size_t needed = _client.cWsRXsize >= 2 && (_client.cWsHeader[1] & 127) == 126 ? 4 : 2;
    while (_client.cWsRXsize < needed && secure_->available()) {
      const int value = secure_->read();
      if (value < 0) return fail(FailureReason::FrameRead);
      _client.cWsHeader[_client.cWsRXsize++] = static_cast<uint8_t>(value);
      if (_client.cWsRXsize == 2) needed = (_client.cWsHeader[1] & 127) == 126 ? 4 : 2;
    }
    if (_client.cWsRXsize < needed) return failure_.get().reason == FailureReason::None;
    const uint8_t first = _client.cWsHeader[0], second = _client.cWsHeader[1];
    if (!(first & 128) || (first & 112) || (second & 128) || (second & 127) == 127)
      return fail(FailureReason::FrameFlags, (int32_t(first) << 8) | second);
    const unsigned opcode = first & 15;
    const size_t size = needed == 4 ? (size_t(_client.cWsHeader[2]) << 8) | _client.cWsHeader[3] : second & 127;
    if (++incomingFrames_ > 8400) return fail(FailureReason::FrameLimit);
    if (needed == 4 && size < 126) return fail(FailureReason::FrameLength, size);
    if (opcode == WSop_binary) { if (!validPcmSize(size)) return fail(FailureReason::FrameLength, size); }
    else if (opcode == WSop_text) { if (!size || size > kTextLimit) return fail(FailureReason::FrameLength, size); }
    else if (opcode != WSop_close && opcode != WSop_ping && opcode != WSop_pong)
      return fail(FailureReason::FrameOpcode, opcode);
    else if (size > 125 || (opcode == WSop_close && size == 1))
      return fail(FailureReason::FrameLength, size);
    if (opcode == WSop_close) {
      failure_.record(FailureReason::PeerClose, size); // Payload length, not an unobserved close code.
      // Invalidate playback BEFORE attempting a close acknowledgement; a peer
      // must not keep queued PCM audible by refusing to read that acknowledgement.
      WebSocketsClient::runCbEvent(WStype_DISCONNECTED, nullptr, 0);
      return false;
    }
    handleWebsocketCb(&_client);
    frameStarted_ = false;
    return _client.status == WSC_CONNECTED && _client.tcp && failure_.get().reason == FailureReason::None;
  }
  bool upgraded() const { return _client.status == WSC_CONNECTED; }
  Failure failure() const { return failure_.get(); }
  bool send(uint8_t* scratch, size_t size, bool binary) {
    if (!_client.tcp || _client.status != WSC_CONNECTED || !secure_) return fail(FailureReason::SendState);
    secure_->budget(5000);
    uint8_t mask[4]; esp_fill_random(mask, sizeof(mask));
    // Library's headerToPayload path uses an all-zero mask. Use its header helper
    // with a genuine mask and the caller's 14-byte reserved scratch instead.
    const uint8_t length = createHeader(scratch, binary ? WSop_binary : WSop_text, size, true, mask, true);
    for (size_t i = 0; i < size; ++i) scratch[14 + i] ^= mask[i & 3];
    if (secure_->write(scratch, length) != length) return fail(FailureReason::SendHeader);
    if (secure_->write(scratch + 14, size) != size) return fail(FailureReason::SendPayload);
    return true;
  }
  void close(bool graceful) {
    if (_client.tcp && secure_) {
      secure_->closing();
      if (graceful && _client.status == WSC_CONNECTED) {
        uint8_t scratch[16] = {}; scratch[14] = 3; scratch[15] = 232;
        uint8_t mask[4]; esp_fill_random(mask, 4);
        const uint8_t size = createHeader(scratch, WSop_close, 2, true, mask, true);
        scratch[14] ^= mask[0]; scratch[15] ^= mask[1];
        secure_->write(scratch, size); secure_->write(scratch + 14, 2);
      }
      secure_->stop();
    }
    // Never call library disconnect(): it emits another potentially blocked
    // frame. The owner closes TLS and invokes the library's cleanup directly.
    clientDisconnect(&_client); secure_ = nullptr;
    wipe(_client.extraHeaders); wipe(_client.plainAuthorization); wipe(_client.base64Authorization);
    _port = 0;
  }
 private:
  bool fail(FailureReason reason, int32_t detail = 0) { failure_.record(reason, detail); return false; }
  size_t write(WSclient_t* client, uint8_t* data, size_t size) override {
    const size_t sent = WebSockets::write(client, data, size);
    if (sent != size) failure_.record(client->status == WSC_HEADER
        ? FailureReason::HeaderWrite : FailureReason::SendPayload);
    return sent;
  }
  void clientDisconnect(WSclient_t* client) override {
    failure_.record(client->status == WSC_HEADER ? FailureReason::HeaderRejected
        : FailureReason::LibraryDisconnect, client->status == WSC_HEADER
            ? client->cCode : static_cast<int32_t>(client->cWsHeaderDecode.opCode));
    WebSocketsClient::clientDisconnect(client);
  }
  void runCbEvent(WStype_t type, uint8_t* payload, size_t size) override {
    if (type == WStype_DISCONNECTED) secure_ = nullptr;
    WebSocketsClient::runCbEvent(type, payload, size);
  }
  static void wipe(String& value) { for (size_t i = 0; i < value.length(); ++i) value[i] = 0; value = ""; }
  SecureClient* secure_ = nullptr;
  FailureLatch failure_;
  char line_[256] = {};
  size_t lineSize_ = 0, headerBytes_ = 0;
  uint32_t headerStart_ = 0, frameStart_ = 0;
  uint32_t incomingFrames_ = 0;
  bool frameStarted_ = false;
};
}  // namespace detail

// One loop/controller owner calls this API; one FreeRTOS task owns all TLS/WS.
// begin captures configuration once. No configuration pointers are borrowed.
// popOutput atomically matches revision/generation; check generation around I2S.
// played is a controller assertion of ACTUAL DMA drain, not merely ring drain.
class Transport {
 public:
  Transport() = default;
  Transport(const Transport&) = delete;
  Transport& operator=(const Transport&) = delete;
  ~Transport() {
    // A failed timed join never frees live task state. Destruction waits for the
    // bounded owner exit instead of force-deleting a task inside TLS/mbedTLS.
    if (!cancel()) cancel(portMAX_DELAY);
    if (storage_) { detail::erase(storage_, kInputLimit + kOutputLimit); heap_caps_free(storage_); }
    if (mutex_) vSemaphoreDelete(mutex_);
    if (finished_) vSemaphoreDelete(finished_);
  }
  Status begin(const Config& config) {
    if (running_.load()) return Status::Busy;
    if (!cancel()) return Status::Busy;
    failure_.reset();
    if (!validConfig(config)) { failure_.record(FailureReason::Config); return Status::Invalid; }
    if (!mutex_) mutex_ = xSemaphoreCreateMutex();
    if (!finished_) finished_ = xSemaphoreCreateBinary();
    if (!mutex_ || !finished_) { failure_.record(FailureReason::NoMemory); return Status::NoMemory; }
    // Retain fixed-capacity PCM rings across joined turns; only the small
    // credential snapshot has turn lifetime. No live task can reach idle reuse.
    if (!storage_) storage_ = static_cast<uint8_t*>(heap_caps_malloc(
        kInputLimit + kOutputLimit, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (!storage_) { failure_.record(FailureReason::NoMemory); return Status::NoMemory; }
    owned_ = static_cast<OwnedConfig*>(heap_caps_calloc(1, sizeof(OwnedConfig), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (!owned_) { failure_.record(FailureReason::NoMemory); return Status::NoMemory; }
    std::strcpy(owned_->host, config.host); std::strcpy(owned_->path, config.path);
    std::strcpy(owned_->ca, config.ca); std::strcpy(owned_->bearer, config.bearer);
    std::strcpy(owned_->session, config.session); owned_->port = config.port;
    state_.begin(storage_, kInputLimit, storage_ + kInputLimit, kOutputLimit);
    cancel_.store(false); running_.store(true); stopSent_ = false;
    turnStart_ = millis();
    if (xTaskCreate(taskEntry, "glasses-wss", 12288, this, 1, &task_) != pdPASS) {
      running_.store(false); task_ = nullptr;
      failure_.record(FailureReason::TaskStart);
      state_.fail(Error::Cancelled, EventType::Closed); disposeOwned(); return Status::NoMemory;
    }
    return Status::Ok;
  }
  Status pushInput(const uint8_t* data, size_t size) {
    if (!mutex_) return Status::NotActive;
    Lock lock(mutex_);
    const Status status = state_.pushInput(data, size);
    record(state_.failure());
    return status;
  }
  size_t popOutput(uint8_t* data, size_t capacity, uint32_t revision, uint32_t generation) {
    if (!mutex_) return 0;
    Lock lock(mutex_); return state_.popOutput(data, capacity, revision, generation);
  }
  bool outputStillCurrent(uint32_t generation) {
    if (!mutex_) return false;
    Lock lock(mutex_); return state_.current(generation);
  }
  size_t outputPending() {
    if (!mutex_) return 0;
    Lock lock(mutex_); return state_.outputPending();
  }
  bool pollEvent(Event& event) {
    if (!mutex_) return false;
    Lock lock(mutex_); return state_.pollEvent(event);
  }
  Status played(uint32_t revision, uint32_t generation) {
    if (!mutex_) return Status::NotActive;
    Lock lock(mutex_); return state_.played(revision, generation);
  }
  Status stopInput() {
    if (!mutex_) return Status::NotActive;
    Lock lock(mutex_); return state_.stopInput();
  }
  bool taskRunning() const { return running_.load(); }
  // Sticky until the next non-Busy begin(), including after event drain/join.
  Failure failure() {
    if (!mutex_) return failure_.get();
    Lock lock(mutex_); return failure_.get();
  }
  bool active() {
    if (!mutex_) return false;
    Lock lock(mutex_); return state_.active();
  }
  bool interruptAndJoin(uint32_t budgetMs = 6000) {
    if (!task_) return false;
    {
      Lock lock(mutex_);
      // If played already left the network owner, complete is the legitimate
      // successful cleanup barrier. Never cancel or erase that committed pair.
      if (!state_.completed() && !state_.playedSent() &&
          state_.requestCancellation() != Status::Ok) return false;
    }
    // Unlike abortive maintenance cancellation, keep TLS live for the remote
    // joined-cleanup acknowledgement. Never reconnect based on TCP close alone.
    if (xSemaphoreTake(finished_, pdMS_TO_TICKS(budgetMs)) != pdTRUE) {
      cancel_.store(true);
      return false;
    }
    task_ = nullptr;
    bool acknowledged;
    { Lock lock(mutex_); acknowledged = state_.cancellationAcknowledged() || state_.completed(); }
    disposeOwned();
    return acknowledged;
  }
  bool cancel(uint32_t joinMs = 6000) {
    if (!task_) return true;
    cancel_.store(true);
    {
      Lock lock(mutex_);
      if (state_.active()) {
        failure_.record(FailureReason::LocalCancel);
        state_.fail(Error::Cancelled, EventType::Closed);
      }
    }
    const TickType_t ticks = joinMs == portMAX_DELAY ? portMAX_DELAY : pdMS_TO_TICKS(joinMs);
    if (xSemaphoreTake(finished_, ticks) != pdTRUE) return false;
    task_ = nullptr; disposeOwned(); return true;
  }
 private:
  void record(Failure failure) { failure_.record(failure.reason, failure.detail); }
  struct OwnedConfig { char host[254], path[257], ca[4000], bearer[257], session[65]; uint16_t port; };
  struct Lock {
    explicit Lock(SemaphoreHandle_t mutex) : mutex(mutex) { xSemaphoreTake(mutex, portMAX_DELAY); }
    ~Lock() { xSemaphoreGive(mutex); }
    SemaphoreHandle_t mutex;
  };
  static bool field(const char* value, size_t maximum, bool header = false) {
    if (!value) return false;
    const size_t size = strnlen(value, maximum + 1);
    if (!size || size > maximum) return false;
    if (header) for (size_t i = 0; i < size; ++i) if (value[i] < 33 || value[i] > 126) return false;
    return true;
  }
  static bool validConfig(const Config& config) {
    if (!config.port || !field(config.host, 253, true) || !field(config.path, 256, true) ||
        !field(config.ca, 3999) || !field(config.bearer, 256, true) || !field(config.session, 64, true)) return false;
    // Existing origin validation remains the authority; reject injected path/query
    // or numeric/IP hosts here as a defense at the actual socket boundary.
    if (std::strcmp(config.path, "/voice")) return false;
    bool letter = false;
    for (const char* ch = config.host; *ch; ++ch) {
      if ((*ch >= 'a' && *ch <= 'z') || (*ch >= 'A' && *ch <= 'Z')) letter = true;
      else if (!((*ch >= '0' && *ch <= '9') || *ch == '.' || *ch == '-')) return false;
    }
    return letter;
  }
  void disposeOwned() {
    if (owned_) { detail::erase(owned_, sizeof(*owned_)); heap_caps_free(owned_); owned_ = nullptr; }
  }
  static void taskEntry(void* argument) { static_cast<Transport*>(argument)->run(); }
  void run() {
    {
      detail::Socket socket;
      socket.onEvent([this, &socket](WStype_t type, uint8_t* data, size_t size) {
        Lock lock(mutex_);
        if (!state_.active()) return;
        if (type == WStype_TEXT) state_.receiveText(data, size);
        else if (type == WStype_BIN) state_.receiveBinary(data, size);
        else if (type == WStype_DISCONNECTED) {
          record(socket.failure());
          failure_.record(FailureReason::LibraryDisconnect);
          state_.fail(Error::Network, EventType::Closed);
        } else if (type != WStype_CONNECTED && type != WStype_PING && type != WStype_PONG) {
          failure_.record(FailureReason::SocketState, static_cast<int32_t>(type));
          state_.fail(Error::Protocol);
        }
        record(state_.failure());
      });
      Config config{owned_->host, owned_->port, owned_->path, owned_->ca, owned_->bearer, owned_->session};
      bool healthy = socket.open(config, cancel_, turnStart_);
      uint8_t scratch[14 + 640] = {};
      bool cancellationSent = false;
      while (healthy && !cancel_.load() && !detail::elapsed(turnStart_, kTurnTimeoutMs)) {
        { Lock lock(mutex_); if (!state_.active()) break; }
        healthy = socket.poll();
        if (!healthy) break;
        size_t count = 0; bool stop = false, requestCancel = false; uint32_t played = 0;
        {
          Lock lock(mutex_);
          if (!state_.active()) break;
          requestCancel = state_.cancelling() && !cancellationSent && socket.upgraded();
          if (!state_.cancelling()) {
            count = state_.popInput(scratch + 14, 640);
            if (state_.ready() && !state_.inputPending()) {
              if (state_.inputStopped() && !stopSent_) { stop = true; stopSent_ = true; }
              state_.takePlayed(played);
            }
          }
        }
        if (requestCancel) {
          std::memcpy(scratch + 14, "cancel", 6);
          healthy = socket.send(scratch, 6, false);
          cancellationSent = healthy;
        }
        if (count) healthy = socket.send(scratch, count, true);
        if (healthy && stop) {
          std::memcpy(scratch + 14, "stop_input", 10); healthy = socket.send(scratch, 10, false);
        }
        if (healthy && played) {
          const int size = snprintf(reinterpret_cast<char*>(scratch + 14), 640, "played %lu", static_cast<unsigned long>(played));
          healthy = size > 0 && socket.send(scratch, static_cast<size_t>(size), false);
        }
        vTaskDelay(pdMS_TO_TICKS(1));
      }
      {
        Lock lock(mutex_);
        record(state_.failure());
        if (state_.active()) {
          record(socket.failure());
          failure_.record(cancel_.load() ? FailureReason::LocalCancel :
              detail::elapsed(turnStart_, kTurnTimeoutMs) ? FailureReason::TurnTimeout : FailureReason::SocketLost);
          state_.fail(cancel_.load() ? Error::Cancelled :
              detail::elapsed(turnStart_, kTurnTimeoutMs) ? Error::Timeout : Error::Network,
              cancel_.load() ? EventType::Closed : EventType::Error);
        }
      }
      // cancellation is already observable to virtual I/O guards. For an explicit
      // normal close allow a short final close frame, never any reconnect.
      socket.close(true);
      detail::erase(scratch, sizeof(scratch));
    }  // Destroy callback/socket copies of credentials before signalling join.
    running_.store(false);
    xSemaphoreGive(finished_);
    vTaskDelete(nullptr);
  }
  SemaphoreHandle_t mutex_ = nullptr, finished_ = nullptr;
  TaskHandle_t task_ = nullptr;
  std::atomic<bool> cancel_{false}, running_{false};
  TurnState state_;
  FailureLatch failure_;
  OwnedConfig* owned_ = nullptr;
  uint8_t* storage_ = nullptr;
  uint32_t turnStart_ = 0;
  bool stopSent_ = false;
};
}  // namespace glasses_stream
