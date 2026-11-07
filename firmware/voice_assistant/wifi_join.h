#pragma once
#include <cstring>

// USB is pumped during a bounded join; only non-network controls may nest.
class WifiJoinState {
 public:
  bool begin() {
    if (active_) return false;
    active_ = true;
    cancelled_ = false;
    return true;
  }
  bool permits(const char* command) const {
    return !active_ || !std::strcmp(command, "ready") || !std::strcmp(command, "status") ||
           !std::strcmp(command, "pause") || !std::strcmp(command, "help") || !std::strcmp(command, "?");
  }
  void pause() { if (active_) cancelled_ = true; }
  bool cancelled() const { return cancelled_; }
  bool finish(bool connected) {
    const bool joined = connected && !cancelled_;
    active_ = false;
    return joined;
  }

 private:
  bool active_ = false;
  bool cancelled_ = false;
};
