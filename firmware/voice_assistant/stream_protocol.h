#pragma once
#include <cstddef>
#include <cstdint>
#include <cstring>

namespace glasses_stream {
constexpr size_t kInputLimit = 320000;
constexpr size_t kOutputLimit = 960000;  // Includes audio discarded by clear.
constexpr size_t kFrameLimit = 3200;
constexpr size_t kTextLimit = 64;
constexpr uint32_t kTurnTimeoutMs = 60000;
constexpr size_t kEventCapacity = 32;

enum class Status : uint8_t { Ok, Busy, Invalid, NotActive, NotReady, InputStopped,
                              Overflow, Limit, NoMemory, Stale };
enum class Error : uint8_t { None, Unauthorized, InvalidAudio, Protocol, Silence,
                            Timeout, Upstream, Busy, Overflow, Limit, Network, Cancelled };
enum class EventType : uint8_t { Ready, Speaking, Clear, Done, Complete, Cancelled, Error, Closed };
struct Event {
  EventType type = EventType::Closed;
  uint32_t revision = 0;
  uint32_t generation = 0;
  Error error = Error::None;
};

// Local, secret-free failure provenance. detail is only a numeric TLS error,
// HTTP status, WebSocket opcode/length, or queue/control identifier; never text.
enum class FailureReason : uint8_t {
  None, Config, NoMemory, TaskStart, Dns, TlsConnect, TlsRead, TlsWrite,
  SocketLost, OperationTimeout, TurnTimeout, LocalCancel, HeaderWrite,
  HeaderTimeout, HeaderRead, HeaderLimit, HeaderProtocol, HeaderRejected,
  SocketState, FrameTimeout, FrameRead, FrameFlags, FrameLimit, FrameLength,
  FrameOpcode, PeerClose, LibraryDisconnect, SendState, SendHeader, SendPayload,
  InputLimit, InputOverflow, OutputLimit, OutputFrameLimit, OutputOverflow, BinaryOrder,
  ControlLimit, ControlSyntax, ControlOrder, RemoteError, EventOverflow
};
struct Failure {
  FailureReason reason = FailureReason::None;
  int32_t detail = 0;
};
inline const char* failureReasonName(FailureReason reason) {
  switch (reason) {
#define GLASSES_FAILURE_NAME(name) case FailureReason::name: return #name
    GLASSES_FAILURE_NAME(None); GLASSES_FAILURE_NAME(Config); GLASSES_FAILURE_NAME(NoMemory);
    GLASSES_FAILURE_NAME(TaskStart); GLASSES_FAILURE_NAME(Dns); GLASSES_FAILURE_NAME(TlsConnect);
    GLASSES_FAILURE_NAME(TlsRead); GLASSES_FAILURE_NAME(TlsWrite); GLASSES_FAILURE_NAME(SocketLost);
    GLASSES_FAILURE_NAME(OperationTimeout); GLASSES_FAILURE_NAME(TurnTimeout); GLASSES_FAILURE_NAME(LocalCancel);
    GLASSES_FAILURE_NAME(HeaderWrite); GLASSES_FAILURE_NAME(HeaderTimeout); GLASSES_FAILURE_NAME(HeaderRead);
    GLASSES_FAILURE_NAME(HeaderLimit); GLASSES_FAILURE_NAME(HeaderProtocol); GLASSES_FAILURE_NAME(HeaderRejected);
    GLASSES_FAILURE_NAME(SocketState); GLASSES_FAILURE_NAME(FrameTimeout); GLASSES_FAILURE_NAME(FrameRead);
    GLASSES_FAILURE_NAME(FrameFlags); GLASSES_FAILURE_NAME(FrameLimit); GLASSES_FAILURE_NAME(FrameLength);
    GLASSES_FAILURE_NAME(FrameOpcode); GLASSES_FAILURE_NAME(PeerClose); GLASSES_FAILURE_NAME(LibraryDisconnect);
    GLASSES_FAILURE_NAME(SendState); GLASSES_FAILURE_NAME(SendHeader); GLASSES_FAILURE_NAME(SendPayload);
    GLASSES_FAILURE_NAME(InputLimit); GLASSES_FAILURE_NAME(InputOverflow); GLASSES_FAILURE_NAME(OutputLimit);
    GLASSES_FAILURE_NAME(OutputFrameLimit);
    GLASSES_FAILURE_NAME(OutputOverflow); GLASSES_FAILURE_NAME(BinaryOrder); GLASSES_FAILURE_NAME(ControlLimit);
    GLASSES_FAILURE_NAME(ControlSyntax); GLASSES_FAILURE_NAME(ControlOrder); GLASSES_FAILURE_NAME(RemoteError);
    GLASSES_FAILURE_NAME(EventOverflow);
#undef GLASSES_FAILURE_NAME
  }
  return "Unknown";
}

// The socket and TLS client share this latch so library-owned TLS deletion
// cannot discard the causal error or replace it with cleanup/disconnect noise.
class FailureLatch {
 public:
  void reset() { failure_ = Failure{}; }
  void record(FailureReason reason, int32_t detail = 0) {
    if (failure_.reason == FailureReason::None) failure_ = Failure{reason, detail};
  }
  Failure get() const { return failure_; }
 private:
  Failure failure_;
};

// Actual virtual-I/O gate used by SecureClient. An idle poll refreshes its
// operation budget; an in-progress frame restores its original absolute start.
// Closing permits only its own 200ms window, not an unbounded session bypass.
class IoBudget {
 public:
  explicit IoBudget(uint32_t turnStart) : turnStart_(turnStart) {}
  void budget(uint32_t now, uint32_t duration) { operationStart_ = now; operationMs_ = duration; }
  void closing(uint32_t now) { closing_ = true; budget(now, 200); }
  FailureReason blocked(uint32_t now, bool cancelled) const {
    if (!closing_) {
      if (cancelled) return FailureReason::LocalCancel;
      if (uint32_t(now - turnStart_) >= kTurnTimeoutMs) return FailureReason::TurnTimeout;
    }
    return uint32_t(now - operationStart_) >= operationMs_
        ? FailureReason::OperationTimeout : FailureReason::None;
  }
 private:
  uint32_t turnStart_, operationStart_ = 0, operationMs_ = 1000;
  bool closing_ = false;
};

inline bool validPcmSize(size_t size) {
  return size && size <= kFrameLimit && !(size & 1);
}
inline bool parseRevision(const uint8_t* data, size_t size, uint32_t& revision) {
  if (!data || !size || size > 10 || data[0] < '1' || data[0] > '9') return false;
  uint32_t value = 0;
  for (size_t i = 0; i < size; ++i) {
    if (data[i] < '0' || data[i] > '9') return false;
    const uint32_t digit = data[i] - '0';
    if (value > (2147483647u - digit) / 10) return false;
    value = value * 10 + digit;
  }
  revision = value;
  return true;
}
inline bool equals(const uint8_t* data, size_t size, const char* literal) {
  return size == std::strlen(literal) && !std::memcmp(data, literal, size);
}
inline bool parseControl(const uint8_t* data, size_t size, Event& event) {
  if (!data || !size || size > kTextLimit) return false;
  event = Event{};
  if (equals(data, size, "ready")) { event.type = EventType::Ready; return true; }
  if (equals(data, size, "cancelled")) { event.type = EventType::Cancelled; return true; }
  struct Prefix { const char* text; EventType type; };
  const Prefix prefixes[] = {{"speaking ", EventType::Speaking}, {"clear ", EventType::Clear},
                            {"done ", EventType::Done}, {"complete ", EventType::Complete}};
  for (const Prefix& prefix : prefixes) {
    const size_t length = std::strlen(prefix.text);
    if (size > length && !std::memcmp(data, prefix.text, length)) {
      event.type = prefix.type;
      return parseRevision(data + length, size - length, event.revision);
    }
  }
  if (size > 6 && !std::memcmp(data, "error ", 6)) {
    struct Code { const char* text; Error error; };
    const Code codes[] = {{"unauthorized", Error::Unauthorized}, {"invalid_audio", Error::InvalidAudio},
                          {"protocol", Error::Protocol}, {"silence", Error::Silence},
                          {"timeout", Error::Timeout}, {"upstream", Error::Upstream}, {"busy", Error::Busy}};
    for (const Code& code : codes) {
      if (equals(data + 6, size - 6, code.text)) {
        event.type = EventType::Error; event.error = code.error; return true;
      }
    }
  }
  return false;
}

// No allocation and no synchronization here: the Arduino adapter serializes every
// access. Payloads cross each ring once, with at most two memcpy calls on wrap.
class ByteRing {
 public:
  void bind(uint8_t* storage, size_t capacity) { data_ = storage; capacity_ = capacity; clear(); }
  void clear() { head_ = 0; size_ = 0; }
  size_t size() const { return size_; }
  bool push(const uint8_t* data, size_t size) {
    if (!data || !data_ || size > capacity_ - size_) return false;
    const size_t tail = (head_ + size_) % capacity_;
    const size_t first = size < capacity_ - tail ? size : capacity_ - tail;
    std::memcpy(data_ + tail, data, first);
    std::memcpy(data_, data + first, size - first);
    size_ += size;
    return true;
  }
  size_t pop(uint8_t* output, size_t capacity) {
    const size_t count = size_ < capacity ? size_ : capacity;
    if (!output || !count) return 0;
    const size_t first = count < capacity_ - head_ ? count : capacity_ - head_;
    std::memcpy(output, data_ + head_, first);
    std::memcpy(output + first, data_, count - first);
    head_ = (head_ + count) % capacity_;
    size_ -= count;
    return count;
  }
 private:
  uint8_t* data_ = nullptr;
  size_t capacity_ = 0, head_ = 0, size_ = 0;
};

// Production protocol state, shared by native tests and the network adapter.
// clear and terminal failure bump generation even when PCM was already popped;
// the I2S owner must check the generation before AND after a bounded write.
class TurnState {
 public:
  void begin(uint8_t* input, size_t inputCapacity, uint8_t* output, size_t outputCapacity) {
    input_.bind(input, inputCapacity); output_.bind(output, outputCapacity);
    ++generation_; revision_ = 0; inputBytes_ = outputBytes_ = 0;
    frames_ = controls_ = 0; eventHead_ = eventSize_ = 0;
    failure_.reset();
    active_ = true; ready_ = stopped_ = speaking_ = done_ = played_ = playedPending_ = false;
    cancelling_ = cancellationAcknowledged_ = completed_ = false;
  }
  bool active() const { return active_; }
  bool ready() const { return ready_; }
  bool inputStopped() const { return stopped_; }
  size_t inputPending() const { return input_.size(); }
  size_t outputPending() const { return output_.size(); }
  size_t inputBytes() const { return inputBytes_; }
  size_t outputBytes() const { return outputBytes_; }
  uint32_t generation() const { return generation_; }
  bool current(uint32_t generation) const { return active_ && generation == generation_; }
  bool cancelling() const { return cancelling_; }
  bool cancellationAcknowledged() const { return cancellationAcknowledged_; }
  bool completed() const { return completed_; }
  bool playedSent() const { return played_; }
  Failure failure() const { return failure_.get(); }
  Status requestCancellation() {
    if (!active_ || played_) return Status::NotActive;
    cancelling_ = true; stopped_ = true; playedPending_ = false;
    input_.clear(); output_.clear(); ++generation_;
    eventHead_ = eventSize_ = 0;
    return Status::Ok;
  }
  Status pushInput(const uint8_t* data, size_t size) {
    if (!active_) return Status::NotActive;
    if (cancelling_) return Status::InputStopped;
    if (stopped_) return Status::InputStopped;
    if (!data || !validPcmSize(size)) return Status::Invalid;
    if (size > kInputLimit - inputBytes_) { reject(Error::Limit, FailureReason::InputLimit); return Status::Limit; }
    if (!input_.push(data, size)) { reject(Error::Overflow, FailureReason::InputOverflow); return Status::Overflow; }
    inputBytes_ += size;
    return Status::Ok;
  }
  size_t popInput(uint8_t* data, size_t capacity) {
    return active_ && ready_ ? input_.pop(data, capacity & ~size_t(1)) : 0;
  }
  size_t popOutput(uint8_t* data, size_t capacity, uint32_t revision, uint32_t generation) {
    // The adapter holds one mutex across this check and dequeue. A stale loop
    // snapshot must never consume replacement PCM.
    return !cancelling_ && current(generation) && revision == revision_
        ? output_.pop(data, capacity & ~size_t(1)) : 0;
  }
  Status stopInput() {
    if (!active_) return Status::NotActive;
    stopped_ = true;  // Queued pre-limit input is still sent before stop_input.
    return Status::Ok;
  }
  Status played(uint32_t revision, uint32_t generation) {
    if (generation != generation_) return Status::Stale;
    if (!active_) return Status::NotActive;
    if (cancelling_) return Status::Stale;
    if (!done_ || played_ || playedPending_ || revision != revision_ || output_.size()) return Status::Invalid;
    playedPending_ = true;
    return Status::Ok;
  }
  bool takePlayed(uint32_t& revision) {
    if (!active_ || !playedPending_) return false;
    revision = revision_;
    playedPending_ = false;
    played_ = true;  // Complete is legal only after the network owner dequeues.
    return true;
  }
  bool receiveBinary(const uint8_t* data, size_t size) {
    if (!active_) return false;
    if (!data || !validPcmSize(size)) return reject(Error::Protocol, FailureReason::FrameLength, size);
    if (++frames_ > 8192) return reject(Error::Limit, FailureReason::OutputFrameLimit);
    if (size > kOutputLimit - outputBytes_) return reject(Error::Limit, FailureReason::OutputLimit);
    if (cancelling_) { outputBytes_ += size; return true; } // Discard bounded in-flight output.
    if (!ready_ || !speaking_ || done_) return reject(Error::Protocol, FailureReason::BinaryOrder);
    if (!output_.push(data, size)) return reject(Error::Overflow, FailureReason::OutputOverflow);
    outputBytes_ += size;
    return true;
  }
  bool receiveText(const uint8_t* data, size_t size) {
    if (!active_) return false;
    Event event;
    if (++controls_ > 128) return reject(Error::Limit, FailureReason::ControlLimit);
    if (!parseControl(data, size, event)) return reject(Error::Protocol, FailureReason::ControlSyntax);
    if (event.type == EventType::Error) return reject(event.error, FailureReason::RemoteError, static_cast<int32_t>(event.error));
    if (cancelling_) {
      if (event.type != EventType::Cancelled) return true; // Drain old in-flight controls.
      cancellationAcknowledged_ = true; active_ = false; ++generation_;
      return enqueue(Event{EventType::Cancelled, revision_, generation_, Error::None});
    }
    switch (event.type) {
      case EventType::Ready:
        if (ready_) return reject(Error::Protocol, FailureReason::ControlOrder, static_cast<int32_t>(event.type));
        ready_ = true; break;
      case EventType::Speaking:
        if (!ready_ || speaking_ || event.revision <= revision_) return reject(Error::Protocol, FailureReason::ControlOrder, static_cast<int32_t>(event.type));
        revision_ = event.revision; speaking_ = true; done_ = played_ = playedPending_ = false; break;
      case EventType::Clear:
        if (!ready_ || !speaking_ || event.revision != revision_) return reject(Error::Protocol, FailureReason::ControlOrder, static_cast<int32_t>(event.type));
        output_.clear(); ++generation_; speaking_ = done_ = played_ = playedPending_ = false; break;
      case EventType::Done:
        if (!speaking_ || done_ || event.revision != revision_) return reject(Error::Protocol, FailureReason::ControlOrder, static_cast<int32_t>(event.type));
        done_ = true; break;
      case EventType::Complete:
        if (!played_ || event.revision != revision_) return reject(Error::Protocol, FailureReason::ControlOrder, static_cast<int32_t>(event.type));
        speaking_ = false;
        break;
      default: return reject(Error::Protocol, FailureReason::ControlOrder, static_cast<int32_t>(event.type));
    }
    event.generation = generation_;
    if (!enqueue(event)) return reject(Error::Overflow, FailureReason::EventOverflow);
    if (event.type == EventType::Complete) {
      // Preserve Complete until the controller observes it, but reject new data.
      active_ = false; completed_ = true; input_.clear(); output_.clear(); ++generation_;
    }
    return true;
  }
  bool pollEvent(Event& event) {
    if (!eventSize_) return false;
    event = events_[eventHead_]; eventHead_ = (eventHead_ + 1) % kEventCapacity; --eventSize_;
    return true;
  }
  bool fail(Error error, EventType type = EventType::Error) {
    active_ = false; ready_ = false; input_.clear(); output_.clear(); ++generation_;
    played_ = playedPending_ = false;
    eventHead_ = eventSize_ = 0;
    enqueue(Event{type, revision_, generation_, error});
    return false;
  }
 private:
  bool reject(Error error, FailureReason reason, int32_t detail = 0) {
    failure_.record(reason, detail);
    return fail(error);
  }
  bool enqueue(const Event& event) {
    if (eventSize_ == kEventCapacity) return false;
    events_[(eventHead_ + eventSize_) % kEventCapacity] = event; ++eventSize_; return true;
  }
  ByteRing input_, output_;
  Event events_[kEventCapacity];
  FailureLatch failure_;
  size_t eventHead_ = 0, eventSize_ = 0, inputBytes_ = 0, outputBytes_ = 0;
  uint32_t generation_ = 0, revision_ = 0, frames_ = 0, controls_ = 0;
  bool active_ = false, ready_ = false, stopped_ = false, speaking_ = false, done_ = false;
  bool played_ = false, playedPending_ = false;
  bool cancelling_ = false, cancellationAcknowledged_ = false, completed_ = false;
};
}  // namespace glasses_stream
