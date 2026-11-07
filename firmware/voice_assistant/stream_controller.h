#pragma once
#include <cstddef>
#include <cstdint>

namespace glasses_stream {

// Exchange lifecycle, not physical capture ownership: AEC capture remains live
// while transport input is closed, assistant PCM drains, and cancellation joins.
class Controller {
 public:
  static constexpr size_t kInputLimit = 16000u * 2u * 10u;
  static constexpr uint32_t kSessionMs = 60000;
  // Dedicated owner refills four 160-frame DMA descriptors in on_sent.
  // Five subsequent real EOFs include the final refilled descriptor's TX.
  static constexpr uint32_t kDrainEofs = 5;
  static constexpr uint32_t kDrainTimeoutMs = 1000;

  void begin(uint32_t now) {
    reset();
    active_ = true;
    started_ = now;
  }
  void reset() {
    active_ = ready_ = inputClosed_ = speaking_ = done_ = acknowledged_ = false;
    inputBytes_ = 0;
    revision_ = drainEof_ = drainStarted_ = started_ = 0;
  }
  bool active() const { return active_; }
  bool ready() const { return ready_; }
  bool acceptingInput() const { return active_ && !inputClosed_; }
  bool speaking() const { return speaking_; }
  bool done() const { return done_; }
  bool acknowledged() const { return acknowledged_; }
  size_t inputBytes() const { return inputBytes_; }
  uint32_t revision() const { return revision_; }
  bool expired(uint32_t now) const { return active_ && now - started_ >= kSessionMs; }
  bool acceptInput(size_t bytes) {
    if (!acceptingInput() || !bytes || (bytes & 1) || bytes > kInputLimit - inputBytes_) return false;
    inputBytes_ += bytes;
    return true;
  }
  bool atInputLimit() const { return inputBytes_ == kInputLimit; }
  void stopInput() { inputClosed_ = true; }
  void onReady() { ready_ = true; }
  bool onSpeaking(uint32_t revision) {
    if (!active_ || !ready_ || !revision || revision <= revision_) return false;
    revision_ = revision;
    inputClosed_ = speaking_ = true;
    done_ = acknowledged_ = false;
    return true;
  }
  bool onClear(uint32_t revision) {
    if (!active_ || revision != revision_) return false;
    speaking_ = done_ = acknowledged_ = false;
    return true;
  }
  bool onDone(uint32_t revision, uint32_t eof, uint32_t now) {
    if (!active_ || !speaking_ || done_ || revision != revision_) return false;
    done_ = true;
    noteWrite(eof, now);
    return true;
  }
  void noteWrite(uint32_t eof, uint32_t now) {
    drainEof_ = eof;
    drainStarted_ = now;
  }
  bool drained(uint32_t eof, bool queueEmpty, bool localEmpty) const {
    return done_ && !acknowledged_ && queueEmpty && localEmpty &&
           eof - drainEof_ >= kDrainEofs;
  }
  bool drainExpired(uint32_t now, bool queueEmpty, bool localEmpty) const {
    return done_ && !acknowledged_ && queueEmpty && localEmpty &&
           now - drainStarted_ >= kDrainTimeoutMs;
  }
  void onPlayed() { acknowledged_ = true; speaking_ = false; }
  bool onComplete(uint32_t revision) {
    if (!active_ || !acknowledged_ || revision != revision_) return false;
    active_ = false;
    return true;
  }
  const char* status() const {
    if (!active_) return "idle";
    if (acknowledged_) return "committing";
    if (done_) return "draining";
    if (speaking_) return "speaking";
    if (inputClosed_) return "waiting";
    return ready_ ? "capturing" : "connecting";
  }

 private:
  bool active_ = false, ready_ = false, inputClosed_ = false;
  bool speaking_ = false, done_ = false, acknowledged_ = false;
  size_t inputBytes_ = 0;
  uint32_t revision_ = 0, started_ = 0, drainEof_ = 0, drainStarted_ = 0;
};

}  // namespace glasses_stream
