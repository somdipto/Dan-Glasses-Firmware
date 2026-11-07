#pragma once
#include <Arduino.h>
#include "bridge_protocol.h"
#include "speech_filter.h"
#include "duplex_clock.h"
#include <atomic>
#include <cmath>
#include <cstring>
#include <driver/i2s_std.h>
#include <driver/gpio.h>
#include <esp_aec.h>
#include <esp_heap_caps.h>
#include <esp_rom_gpio.h>
#include <esp_timer.h>
#include <soc/gpio_sig_map.h>
#include <freertos/queue.h>
#include <freertos/task.h>

namespace glasses_audio {
// The only I2S owner. Wire slots are always stereo32; network PCM stays mono16.
// DMA callbacks never allocate, touch PSRAM, run AEC, or wait for network work.
class DuplexAudio {
 public:
  static constexpr size_t kBlock = 160, kFrame = 320, kDescriptors = 4;
  static constexpr size_t kCaptureFrames = 400;  // 8 s > bounded transport join (6 s).
  struct Capture { int16_t pcm[kFrame]; };
  enum class Fault : uint8_t {
    None, Setup, TxDma, TxTimeline, RxDma, RxTimeline, RxOverflow,
    RxTimeout, RxSequence, ReferenceMissing, ReferencePhase, CaptureOverflow
  };
  Fault fault() const {
    portENTER_CRITICAL(&mux_); const Fault value = faultReason_; portEXIT_CRITICAL(&mux_);
    return value;
  }
  static const char* faultName(Fault fault) {
    switch (fault) {
      case Fault::None: return "none";
      case Fault::Setup: return "setup";
      case Fault::TxDma: return "tx_dma";
      case Fault::TxTimeline: return "tx_timeline";
      case Fault::RxDma: return "rx_dma";
      case Fault::RxTimeline: return "rx_timeline";
      case Fault::RxOverflow: return "rx_overflow";
      case Fault::RxTimeout: return "rx_timeout";
      case Fault::RxSequence: return "rx_sequence";
      case Fault::ReferenceMissing: return "reference_missing";
      case Fault::ReferencePhase: return "reference_phase";
      case Fault::CaptureOverflow: return "capture_overflow";
    }
    return "unknown";
  }
  DuplexAudio() = default;
  DuplexAudio(const DuplexAudio&) = delete;
  DuplexAudio& operator=(const DuplexAudio&) = delete;
  ~DuplexAudio() { end(); if (captureStorage_) heap_caps_free(captureStorage_); }
  bool begin() {
    if (running_.load()) return healthy();
    task_ = nullptr;
    release();
    stopping_.store(false); channel_.store(-1);
    portENTER_CRITICAL(&mux_);
    fault_ = interrupted_ = bargeArmed_ = false;
    faultReason_ = Fault::None;
    eof_ = lastFill_ = rxSequence_ = 0;
    rxHead_ = rxSize_ = 0;
    txTimeline_.reset(); rxTimeline_.reset();
    portEXIT_CRITICAL(&mux_);
    referenceOffset_ = INT32_MIN;
    phase_.reset();
    framePosition_ = chunkPosition_ = 0;
    energy_[0] = energy_[1] = 0;
    loud_ = 0;
    vadHp1_.highPass(200, 0.5412f); vadHp2_.highPass(200, 1.3066f);
    vadHp1_.reset(); vadHp2_.reset();
    portENTER_CRITICAL(&mux_);
    std::memset(references_, 0, sizeof(references_));
    portEXIT_CRITICAL(&mux_);
    if (!captureStorage_) captureStorage_ = static_cast<uint8_t*>(heap_caps_malloc(
        kCaptureFrames * sizeof(Capture), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (!captureStorage_) return fail();
    capture_ = xQueueCreateStatic(kCaptureFrames, sizeof(Capture), captureStorage_, &captureQueue_);
    aec_ = aec_create(16000, 4, 1, AEC_MODE_FD_LOW_COST);
    if (!aec_) return fail();
    // Preserve near-end double talk; bundled default is aggressive NLP.
    if (aec_set_nlp_level(aec_, AEC_NLP_LEVEL_NORMAL) != AEC_NLP_LEVEL_NORMAL) return fail();
    chunk_ = aec_get_chunksize(aec_);
    if (chunk_ <= 0 || chunk_ > 1024) return fail();
    const size_t stride = (size_t(chunk_) + 7) & ~size_t(7);
    aligned_ = static_cast<int16_t*>(heap_caps_aligned_alloc(16, stride * 6,
                                                           MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    if (!aligned_) return fail();
    mic_ = aligned_; ref_ = mic_ + stride; clean_ = ref_ + stride;
    i2s_chan_config_t channelConfig = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    channelConfig.dma_desc_num = kDescriptors; channelConfig.dma_frame_num = kBlock;
    // We refill completed descriptors ourselves. The driver must not erase them.
    channelConfig.auto_clear_after_cb = false; channelConfig.auto_clear_before_cb = false;
    if (i2s_new_channel(&channelConfig, &tx_, &rx_) != ESP_OK) return fail();
    i2s_std_config_t standard = {};
    standard.clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(16000);
    standard.slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_STEREO);
    standard.gpio_cfg.mclk = I2S_GPIO_UNUSED;
    standard.gpio_cfg.bclk = GPIO_NUM_2; standard.gpio_cfg.ws = GPIO_NUM_3;
    standard.gpio_cfg.dout = GPIO_NUM_4; standard.gpio_cfg.din = GPIO_NUM_1;
    if (i2s_channel_init_std_mode(tx_, &standard) != ESP_OK ||
        i2s_channel_init_std_mode(rx_, &standard) != ESP_OK) return fail();
    i2s_event_callbacks_t txCallbacks = {}, rxCallbacks = {};
    txCallbacks.on_sent = sent; rxCallbacks.on_recv = received;
    if (i2s_channel_register_event_callback(tx_, &txCallbacks, this) != ESP_OK ||
        i2s_channel_register_event_callback(rx_, &rxCallbacks, this) != ESP_OK) return fail();
    mute();
    // RX starts first; startup phase is measured once from actual DMA EOFs below.
    if (i2s_channel_enable(rx_) != ESP_OK || i2s_channel_enable(tx_) != ESP_OK) return fail();
    running_.store(true);
    if (xTaskCreatePinnedToCore(entry, "glasses-aec", 8192, this, 4, &task_, 1) != pdPASS) {
      running_.store(false); return fail();
    }
    return true;
  }
  void end() {
    mute(); stopping_.store(true);
    // No task deletion in the middle of DSP or driver ownership.
    while (running_.load()) vTaskDelay(pdMS_TO_TICKS(1));
    task_ = nullptr; release(); channel_.store(-1);
  }
  bool healthy() const { return !failed() && running_.load(); }
  int channel() const { return channel_.load(); }
  int chunkSize() const { return chunk_; }
  uint32_t eofs() const {
    portENTER_CRITICAL(&mux_); const uint32_t value = eof_; portEXIT_CRITICAL(&mux_); return value;
  }
  uint32_t lastFill() const {
    portENTER_CRITICAL(&mux_); const uint32_t value = lastFill_; portEXIT_CRITICAL(&mux_); return value;
  }
  size_t pending() {
    portENTER_CRITICAL(&mux_); const size_t count = outputSize_; portEXIT_CRITICAL(&mux_); return count * 2;
  }
  struct Drain { uint32_t eof, fill; bool empty; };
  Drain drain() {
    portENTER_CRITICAL(&mux_);
    Drain result{eof_, lastFill_, outputSize_ == 0};
    portEXIT_CRITICAL(&mux_); return result;
  }
  bool pop(Capture& capture, uint32_t waitMs = 800) {
    return healthy() && xQueueReceive(capture_, &capture, pdMS_TO_TICKS(waitMs)) == pdTRUE && healthy();
  }
  void rawSnapshot(int32_t (&raw)[kFrame * 2]) {
    portENTER_CRITICAL(&mux_); std::memcpy(raw, rawLatest_, sizeof(rawLatest_)); portEXIT_CRITICAL(&mux_);
  }
  void discardCapture() { if (capture_) xQueueReset(capture_); }
  void controls(int gain, float threshold) { gain_.store(gain); threshold_.store(threshold); }
  void armInterruption(bool armed) {
    portENTER_CRITICAL(&mux_); bargeArmed_ = armed && !fault_; portEXIT_CRITICAL(&mux_);
  }
  bool takeInterruption() {
    portENTER_CRITICAL(&mux_); const bool value = interrupted_; interrupted_ = false; portEXIT_CRITICAL(&mux_);
    return value;
  }
  bool interruptionPending() const {
    portENTER_CRITICAL(&mux_); const bool value = interrupted_; portEXIT_CRITICAL(&mux_); return value;
  }
  void mute() {
    portENTER_CRITICAL(&mux_); muteLocked(); portEXIT_CRITICAL(&mux_);
  }
  bool finishPlayback() {
    // Serialize the final played decision with the owner's VAD latch.
    portENTER_CRITICAL(&mux_);
    const bool uninterrupted = !interrupted_;
    muteLocked();
    portEXIT_CRITICAL(&mux_); return uninterrupted;
  }
  bool play(const uint8_t* pcm, size_t bytes) {
    if (!healthy() || !pcm || !bytes || (bytes & 1)) return false;
    portENTER_CRITICAL(&mux_);
    if (fault_ || interrupted_) { portEXIT_CRITICAL(&mux_); return false; }
    const size_t samples = bytes / 2;
    if (samples > kOutputSamples - outputSize_) { portEXIT_CRITICAL(&mux_); return false; }
    for (size_t i = 0; i < samples; ++i) {
      output_[(outputHead_ + outputSize_ + i) % kOutputSamples] =
          int16_t(uint16_t(pcm[i * 2]) | (uint16_t(pcm[i * 2 + 1]) << 8));
    }
    outputSize_ += samples; unmuteRequested_ = true;
    portEXIT_CRITICAL(&mux_); return true;
  }
 private:
  bool failed() const {
    portENTER_CRITICAL(&mux_); const bool value = fault_; portEXIT_CRITICAL(&mux_); return value;
  }
  void muteLocked() {
    if (!muted_) { gatePreviousMuted_ = false; gateChangedUs_ = esp_timer_get_time(); }
    muted_ = true; unmuteRequested_ = false; outputHead_ = outputSize_ = 0;
    mutedAt_ = eof_; bargeArmed_ = false;
    gpio_set_level(GPIO_NUM_4, 0);
    esp_rom_gpio_connect_out_signal(4, SIG_GPIO_OUT_IDX, false, false);
  }
  static constexpr size_t kRxBlocks = 12, kReferences = 32, kOutputSamples = 4096;
  struct RxBlock { int32_t raw[kBlock * 2]; uint32_t sequence; int64_t time; };
  struct Reference { int16_t pcm[kBlock]; uint32_t sequence; int64_t time; };
  // Definitions are ordinary (non-inline/non-COMDAT) IRAM functions in .cpp.
  // Xtensa requires their literal pools before use, not header COMDAT literals.
  bool faultFromIsr(Fault reason);
  static bool sent(i2s_chan_handle_t, i2s_event_data_t* event, void* context);
  static bool received(i2s_chan_handle_t, i2s_event_data_t* event, void* context);
  static void entry(void* context) { static_cast<DuplexAudio*>(context)->run(); }
  bool referenceFor(const RxBlock& block, int16_t* output) {
    const uint32_t started = millis();
    while (!stopping_.load() && !failed() && millis() - started < 40) {
      portENTER_CRITICAL(&mux_);
      if (referenceOffset_ == INT32_MIN) {
        // IDF does not promise synchronous TX/RX starts. Delay processing RX
        // until actual TX ends >=10 ms LATER; choosing reference from the past
        // would make a short acoustic path noncausal. Freeze this ordinal offset:
        // reference leads capture by 10..20 ms, and the shared clock cannot drift.
        const uint32_t newest = eof_;
        uint32_t selected = 0;
        for (uint32_t back = 0; back < kReferences && back < newest; ++back) {
          const auto& candidate = references_[(newest - back) % kReferences];
          if (candidate.sequence == newest - back && candidate.time >= block.time + 10000)
            selected = candidate.sequence;
        }
        if (selected) referenceOffset_ = int32_t(block.sequence - selected);
      }
      bool valid = false;
      if (referenceOffset_ != INT32_MIN) {
        const uint32_t expected = block.sequence - uint32_t(referenceOffset_);
        const auto& reference = references_[expected % kReferences];
        valid = reference.sequence == expected;
        if (valid && !phase_.accept(reference.time - block.time)) {
          portEXIT_CRITICAL(&mux_); return fail(Fault::ReferencePhase);
        }
        if (valid) std::memcpy(output, reference.pcm, kBlock * 2);
      }
      portEXIT_CRITICAL(&mux_);
      if (valid) return true;
      vTaskDelay(pdMS_TO_TICKS(1));
    }
    return false; // Never replace a missing physical reference with fake silence.
  }
  bool readRx(RxBlock& block) {
    const uint32_t started = millis();
    while (!stopping_.load() && !failed() && millis() - started < 100) {
      portENTER_CRITICAL(&mux_);
      const bool available = rxSize_ != 0;
      if (available) {
        block = rxBlocks_[rxHead_];
        rxHead_ = (rxHead_ + 1) % kRxBlocks;
        --rxSize_;
      }
      portEXIT_CRITICAL(&mux_);
      if (available) return true;
      vTaskDelay(pdMS_TO_TICKS(1));
    }
    return false;
  }
  void run() {
    RxBlock block; int16_t reference[kBlock]; uint32_t expected = 1;
    while (!stopping_.load() && !failed()) {
      if (!readRx(block)) { fail(Fault::RxTimeout); break; }
      if (block.sequence != expected++) { fail(Fault::RxSequence); break; }
      // Diagnostics need only the most recent physical stereo frame, not an
      // eight-second raw recording duplicated alongside the near-end queue.
      portENTER_CRITICAL(&mux_);
      std::memcpy(rawLatest_ + ((block.sequence - 1) & 1) * kBlock * 2, block.raw, sizeof(block.raw));
      portEXIT_CRITICAL(&mux_);
      if (block.sequence <= 26) continue; // 260 ms initial INMP441 settling.
      if (block.sequence <= 30) {
        accumulateMicEnergy(block.raw, kBlock, energy_);
        if (block.sequence == 30) {
          channel_.store(selectMicChannel(energy_[0], energy_[1]));
          // No mic signal must not suppress the independent local boot cue.
        }
        continue;
      }
      if (!referenceFor(block, reference)) { fail(Fault::ReferenceMissing); break; }
      const int detected = channel_.load();
      const int selected = detected < 0 ? 0 : detected;
      bool processing = true;
      for (size_t i = 0; i < kBlock && processing; ++i) {
        mic_[chunkPosition_] = int16_t(block.raw[i * 2 + selected] >> 16);
        ref_[chunkPosition_] = reference[i];
        if (++chunkPosition_ != size_t(chunk_)) continue;
        aec_process(aec_, mic_, ref_, clean_);
        for (int j = 0; j < chunk_ && processing; ++j) {
          captureFrame_.pcm[framePosition_] = clean_[j];
          if (++framePosition_ == kFrame) {
            double energy = 0;
            const int gain = gain_.load();
            for (size_t k = 0; k < kFrame; ++k) {
              const float filtered = vadHp2_.process(vadHp1_.process(captureFrame_.pcm[k])) * gain;
              energy += double(filtered) * filtered;
            }
            const bool voiced = std::sqrt(energy / kFrame) > threshold_.load();
            portENTER_CRITICAL(&mux_);
            if (bargeArmed_ && voiced) ++loud_; else loud_ = 0;
            if (loud_ >= 4) {
              interrupted_ = true; muteLocked(); loud_ = 0;
            }
            portEXIT_CRITICAL(&mux_);
            if (xQueueSend(capture_, &captureFrame_, 0) != pdTRUE) {
              fail(Fault::CaptureOverflow); processing = false; break;
            }
            framePosition_ = 0;
          }
        }
        chunkPosition_ = 0;
      }
    }
    mute(); running_.store(false); vTaskDelete(nullptr);
  }
  bool fail(Fault reason = Fault::Setup) {
    portENTER_CRITICAL(&mux_);
    if (!fault_) faultReason_ = reason;
    fault_ = true; muteLocked();
    portEXIT_CRITICAL(&mux_);
    if (!running_.load()) release();
    return false;
  }
  void release() {
    if (rx_) { i2s_channel_disable(rx_); i2s_del_channel(rx_); rx_ = nullptr; }
    if (tx_) { i2s_channel_disable(tx_); i2s_del_channel(tx_); tx_ = nullptr; }
    if (aec_) { aec_destroy(aec_); aec_ = nullptr; }
    if (aligned_) { heap_caps_free(aligned_); aligned_ = nullptr; }
    chunk_ = 0;
  }
  i2s_chan_handle_t tx_ = nullptr, rx_ = nullptr;
  TaskHandle_t task_ = nullptr;
  std::atomic<bool> running_{false};
  aec_handle_t* aec_ = nullptr;
  int chunk_ = 0;
  int16_t *aligned_ = nullptr, *mic_ = nullptr, *ref_ = nullptr, *clean_ = nullptr;
  mutable portMUX_TYPE mux_ = portMUX_INITIALIZER_UNLOCKED;
  // Only task code touches these atomics. The pinned SDK disables hardware
  // atomics; every IRQ-shared field instead uses mux_ on both sides.
  std::atomic<bool> stopping_{false};
  bool fault_ = false, bargeArmed_ = false, interrupted_ = false;
  Fault faultReason_ = Fault::None;
  std::atomic<int> channel_{-1}, gain_{16};
  std::atomic<float> threshold_{500};
  uint32_t eof_ = 0, lastFill_ = 0;
  uint32_t rxSequence_ = 0, mutedAt_ = 0;
  int32_t referenceOffset_ = INT32_MIN;
  DmaTimeline txTimeline_, rxTimeline_;
  ReferencePhase phase_;
  bool muted_ = true, unmuteRequested_ = false;
  bool gatePreviousMuted_ = true;
  int64_t gateChangedUs_ = 0;
  int16_t output_[kOutputSamples] = {};
  size_t outputHead_ = 0, outputSize_ = 0;
  Reference references_[kReferences] = {};
  QueueHandle_t capture_ = nullptr;
  StaticQueue_t captureQueue_ = {};
  RxBlock rxBlocks_[kRxBlocks];
  size_t rxHead_ = 0, rxSize_ = 0;
  uint8_t* captureStorage_ = nullptr;
  Capture captureFrame_;
  int32_t rawLatest_[kFrame * 2] = {};
  size_t framePosition_ = 0, chunkPosition_ = 0;
  int loud_ = 0;
  double energy_[2] = {};
  Biquad vadHp1_, vadHp2_;
};
}  // namespace glasses_audio
