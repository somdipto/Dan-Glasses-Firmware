#include "duplex_audio.h"

namespace glasses_audio {

// Keep these definitions out of the class/header: Xtensa header-inline IRAM
// COMDATs can leave l32r literals after their users. Ordinary IRAM functions let
// the pinned linker script place their literal pools with the code correctly.
bool IRAM_ATTR DuplexAudio::faultFromIsr(Fault reason) {
  portENTER_CRITICAL_ISR(&mux_);
  if (!fault_) faultReason_ = reason;
  fault_ = true;
  if (!muted_) { gatePreviousMuted_ = false; gateChangedUs_ = esp_timer_get_time(); }
  muted_ = true; unmuteRequested_ = false; outputHead_ = outputSize_ = 0;
  bargeArmed_ = false;
  // Public mute/boot set the GPIO latch low before enabling I2S. Only its ROM
  // mux operation is needed here; no flash-resident GPIO wrapper is called.
  esp_rom_gpio_connect_out_signal(4, SIG_GPIO_OUT_IDX, false, false);
  portEXIT_CRITICAL_ISR(&mux_);
  return false;
}

bool IRAM_ATTR DuplexAudio::sent(i2s_chan_handle_t, i2s_event_data_t* event, void* context) {
  auto* self = static_cast<DuplexAudio*>(context);
  const int64_t completedAt = esp_timer_get_time();
  portENTER_CRITICAL_ISR(&self->mux_);
  if (self->fault_) { portEXIT_CRITICAL_ISR(&self->mux_); return false; }
  if (event->size != kBlock * 8 || !event->dma_buf) {
    portEXIT_CRITICAL_ISR(&self->mux_); return self->faultFromIsr(Fault::TxDma);
  }
  if (!self->txTimeline_.observe(event->dma_buf, completedAt)) {
    portEXIT_CRITICAL_ISR(&self->mux_); return self->faultFromIsr(Fault::TxTimeline);
  }
  auto* wire = static_cast<volatile int32_t*>(event->dma_buf);
  const uint32_t sequence = self->eof_ + 1;
  auto& reference = self->references_[sequence % kReferences];
  reference.sequence = sequence; reference.time = completedAt;
  for (size_t i = 0; i < kBlock; ++i) {
    // Gate timestamps preserve the sent prefix when DIN is muted mid-block.
    const int64_t sampleTime = completedAt - 10000 + int64_t((i * 125u) / 2u);
    const bool gated = sampleTime < self->gateChangedUs_ ? self->gatePreviousMuted_ : self->muted_;
    reference.pcm[i] = gated ? 0 : int16_t(wire[i * 2] >> 16);
  }
  self->eof_ = sequence;
  // Traverse every stale descriptor with zero before reconnecting DIN. RX and
  // clock ownership remain unchanged, so reference phase does not restart.
  if (self->muted_ && self->unmuteRequested_ && sequence - self->mutedAt_ > kDescriptors) {
    esp_rom_gpio_connect_out_signal(4, I2S0O_SD_OUT_IDX, false, false);
    self->gatePreviousMuted_ = true; self->gateChangedUs_ = esp_timer_get_time();
    self->muted_ = false;
  }
  bool filled = false;
  for (size_t i = 0; i < kBlock; ++i) {
    int16_t sample = 0;
    if (!self->muted_ && self->outputSize_) {
      sample = self->output_[self->outputHead_];
      self->outputHead_ = (self->outputHead_ + 1) % kOutputSamples;
      --self->outputSize_; filled = true;
    }
    // Volatile DMA stores prohibit replacement by a flash memset/memcpy helper.
    wire[i * 2] = int32_t(sample) * 65536;
    wire[i * 2 + 1] = int32_t(sample) * 65536;
  }
  if (filled) self->lastFill_ = sequence;
  portEXIT_CRITICAL_ISR(&self->mux_);
  return false;
}

bool IRAM_ATTR DuplexAudio::received(i2s_chan_handle_t, i2s_event_data_t* event, void* context) {
  auto* self = static_cast<DuplexAudio*>(context);
  const int64_t completedAt = esp_timer_get_time();
  portENTER_CRITICAL_ISR(&self->mux_);
  if (self->fault_) { portEXIT_CRITICAL_ISR(&self->mux_); return false; }
  if (event->size != kBlock * 8 || !event->dma_buf) {
    portEXIT_CRITICAL_ISR(&self->mux_); return self->faultFromIsr(Fault::RxDma);
  }
  if (!self->rxTimeline_.observe(event->dma_buf, completedAt)) {
    portEXIT_CRITICAL_ISR(&self->mux_); return self->faultFromIsr(Fault::RxTimeline);
  }
  if (self->rxSize_ == kRxBlocks) {
    portEXIT_CRITICAL_ISR(&self->mux_);
    return self->faultFromIsr(Fault::RxOverflow);
  }
  size_t tail = self->rxHead_ + self->rxSize_;
  if (tail >= kRxBlocks) tail -= kRxBlocks;
  auto& block = self->rxBlocks_[tail];
  const auto* source = static_cast<const volatile int32_t*>(event->dma_buf);
  volatile int32_t* destination = block.raw;
  // I2S DMA buffers and raw[] are 32-bit aligned. Volatile word operations make
  // the bounded copy explicit: neither libc memcpy nor a queue copy runs in IRQ.
  for (size_t i = 0; i < kBlock * 2; ++i) destination[i] = source[i];
  block.sequence = ++self->rxSequence_; block.time = completedAt;
  ++self->rxSize_;
  portEXIT_CRITICAL_ISR(&self->mux_);
  return false; // Owner polls this ring at a one-tick cadence; no kernel copy.
}
}  // namespace glasses_audio
