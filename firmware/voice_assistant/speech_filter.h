#pragma once
#include <cmath>

// Shared 16 kHz speech-band filter for near-end VAD and uploaded near-end PCM.
struct Biquad {
  float b0 = 0, b1 = 0, b2 = 0, a1 = 0, a2 = 0;
  float x1 = 0, x2 = 0, y1 = 0, y2 = 0;
  void highPass(float fc, float q) {
    const float w = 2 * 3.14159265358979323846f * fc / 16000;
    const float alpha = std::sin(w) / (2 * q), c = std::cos(w), a0 = 1 + alpha;
    b0 = (1 + c) / 2 / a0; b1 = -(1 + c) / a0; b2 = (1 + c) / 2 / a0;
    a1 = -2 * c / a0; a2 = (1 - alpha) / a0;
  }
  void reset() { x1 = x2 = y1 = y2 = 0; }
  float process(float x) {
    const float y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2;
    x2 = x1; x1 = x; y2 = y1; y1 = y;
    return y;
  }
};
