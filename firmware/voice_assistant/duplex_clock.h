#pragma once
#include <cstddef>
#include <cstdint>
#if defined(__GNUC__)
#define GLASSES_AUDIO_INLINE inline __attribute__((always_inline))
#else
#define GLASSES_AUDIO_INLINE inline
#endif

namespace glasses_audio {
// Physical EOF continuity, independent of software queue sequence numbers.
// A delayed/lost ISR is a capture failure, never permission to relabel samples.
class DmaTimeline {
 public:
  static constexpr int64_t kBlockUs = 10000;
  static constexpr int64_t kJitterUs = 2500;
  void reset() { count_ = 0; last_ = 0; for (auto& descriptor : descriptors_) descriptor = nullptr; }
  GLASSES_AUDIO_INLINE bool observe(const void* descriptor, int64_t time) {
    if (!descriptor || (count_ && (time - last_ < kBlockUs - kJitterUs ||
                                  time - last_ > kBlockUs + kJitterUs))) return false;
    const size_t slot = count_ & 3;
    if (count_ < 4) {
      for (size_t i = 0; i < count_; ++i) if (descriptors_[i] == descriptor) return false;
      descriptors_[slot] = descriptor;
    } else if (descriptors_[slot] != descriptor) return false;
    ++count_; last_ = time;
    return true;
  }
 private:
  const void* descriptors_[4] = {};
  uint64_t count_ = 0;
  int64_t last_ = 0;
};

// The initial reference lead is measured, not inferred from application writes.
// Validate every pair against that phase: even a one-block slip is terminal.
class ReferencePhase {
 public:
  void reset() { lead_ = 0; }
  bool accept(int64_t lead) {
    if (lead < DmaTimeline::kBlockUs - DmaTimeline::kJitterUs ||
        lead > 2 * DmaTimeline::kBlockUs + DmaTimeline::kJitterUs) return false;
    if (!lead_) { lead_ = lead; return true; }
    const int64_t delta = lead - lead_;
    return delta >= -DmaTimeline::kJitterUs && delta <= DmaTimeline::kJitterUs;
  }
 private:
  int64_t lead_ = 0;
};
}  // namespace glasses_audio
#undef GLASSES_AUDIO_INLINE
