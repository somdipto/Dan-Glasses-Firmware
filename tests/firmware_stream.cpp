#include "../firmware/voice_assistant/stream_protocol.h"
#include "../firmware/voice_assistant/stream_controller.h"
#include "../firmware/voice_assistant/duplex_clock.h"
#include <array>
#include <cassert>
#include <cstdio>
#include <string>
#include <vector>

using namespace glasses_stream;

static bool control(TurnState& state, const char* text) {
  return state.receiveText(reinterpret_cast<const uint8_t*>(text), std::strlen(text));
}
static void drainEvents(TurnState& state) { Event event; while (state.pollEvent(event)) {} }
struct Fixture {
  std::vector<uint8_t> input, output;
  TurnState state;
  explicit Fixture(size_t inputCapacity = kInputLimit, size_t outputCapacity = kOutputLimit)
      : input(inputCapacity), output(outputCapacity) {
    state.begin(input.data(), input.size(), output.data(), output.size());
  }
  void speaking(const char* text = "speaking 1") {
    assert(control(state, "ready")); assert(control(state, text));
  }
};

static void parsing() {
  uint32_t revision = 99;
  for (const char* text : {"1", "2147483647", "42"}) {
    assert(parseRevision(reinterpret_cast<const uint8_t*>(text), std::strlen(text), revision));
  }
  for (const char* text : {"", "0", "00", "01", "2147483648", "99999999999", "+1", "-1", "1 ", " 1", "1\n", "1a"}) {
    revision = 99;
    assert(!parseRevision(reinterpret_cast<const uint8_t*>(text), std::strlen(text), revision));
    assert(revision == 99);
  }
  Event event;
  for (const char* text : {"ready", "speaking 1", "clear 2147483647", "done 20", "complete 5",
                           "error unauthorized", "error invalid_audio", "error protocol", "error silence",
                           "error timeout", "error upstream", "error busy"}) {
    assert(parseControl(reinterpret_cast<const uint8_t*>(text), std::strlen(text), event));
  }
  for (const char* text : {"READY", "ready ", "ready\n", "speaking 01", "done 0", "complete +1", "clear 1 2",
                           "error ", "error arbitrary", "played 1", "stop_input", "done 2147483648"}) {
    assert(!parseControl(reinterpret_cast<const uint8_t*>(text), std::strlen(text), event));
  }
  const uint8_t nul[] = {'d', 'o', 'n', 'e', ' ', '1', 0, '2'};
  assert(!parseControl(nul, sizeof(nul), event));
  const std::string oversized(65, 'a');
  assert(!parseControl(reinterpret_cast<const uint8_t*>(oversized.data()), oversized.size(), event));
  assert(!parseControl(nullptr, 1, event));
  assert(!validPcmSize(0) && !validPcmSize(1) && !validPcmSize(3201) && !validPcmSize(3202));
  assert(validPcmSize(2) && validPcmSize(640) && validPcmSize(3200));
}

static void ringWrapAndOverflow() {
  uint8_t backing[8] = {}, result[8] = {};
  const uint8_t first[] = {1, 2, 3, 4, 5, 6}, second[] = {7, 8, 9, 10, 11, 12};
  ByteRing ring; ring.bind(backing, sizeof(backing));
  assert(ring.push(first, sizeof(first)));
  assert(ring.pop(result, 4) == 4);
  assert(!std::memcmp(result, first, 4));
  assert(ring.push(second, sizeof(second)));
  assert(ring.size() == 8 && !ring.push(first, 1));
  const uint8_t expected[] = {5, 6, 7, 8, 9, 10, 11, 12};
  assert(ring.pop(result, sizeof(result)) == 8 && !std::memcmp(result, expected, 8));
  assert(ring.pop(result, 8) == 0);
  assert(ring.push(first, 6)); ring.clear();
  assert(ring.size() == 0 && ring.pop(result, 8) == 0);
}

static void inputBoundsAndReady() {
  Fixture fixture;
  std::array<uint8_t, 3200> frame{};
  std::array<uint8_t, 640> scratch{};
  auto& state = fixture.state;
  assert(state.pushInput(frame.data(), 0) == Status::Invalid);
  assert(state.pushInput(frame.data(), 3) == Status::Invalid);
  for (size_t i = 0; i < 100; ++i) assert(state.pushInput(frame.data(), frame.size()) == Status::Ok);
  assert(state.inputBytes() == kInputLimit && state.inputPending() == kInputLimit);
  assert(state.popInput(scratch.data(), scratch.size()) == 0);  // Handshake is not provider readiness.
  assert(control(state, "ready"));
  assert(state.popInput(scratch.data(), 639) == 638);
  assert(state.stopInput() == Status::Ok && state.stopInput() == Status::Ok);
  assert(state.pushInput(frame.data(), 2) == Status::InputStopped);
  size_t drained = 638;
  while (size_t count = state.popInput(scratch.data(), scratch.size())) drained += count;
  assert(drained == kInputLimit && !state.inputPending());

  Fixture over;
  for (size_t i = 0; i < 100; ++i) assert(over.state.pushInput(frame.data(), frame.size()) == Status::Ok);
  assert(over.state.pushInput(frame.data(), 2) == Status::Limit);
  assert(over.state.failure().reason == FailureReason::InputLimit);
  assert(!over.state.active() && !over.state.inputPending());
  Event event; assert(over.state.pollEvent(event) && event.error == Error::Limit);

  Fixture small(8, 8);
  assert(small.state.pushInput(frame.data(), 8) == Status::Ok);
  assert(small.state.pushInput(frame.data(), 2) == Status::Overflow);
  assert(small.state.failure().reason == FailureReason::InputOverflow);
  assert(!small.state.active() && !small.state.inputPending());
}

static void clearAndGenerations() {
  Fixture fixture;
  auto& state = fixture.state;
  fixture.speaking();
  std::array<uint8_t, 3200> first{}, second{};
  first.fill(0x35); second.fill(0x6a);
  assert(state.receiveBinary(first.data(), first.size()));
  uint8_t scratch[640]; uint32_t revision = 1, generation = state.generation();
  assert(state.popOutput(scratch, sizeof(scratch), revision, generation) == 640);
  assert(revision == 1 && state.current(generation));
  assert(!std::memcmp(scratch, first.data(), 640));
  const uint32_t oldGeneration = generation;
  assert(control(state, "clear 1"));
  assert(!state.current(oldGeneration) && state.outputPending() == 0);
  assert(state.outputBytes() == 3200);  // Clear never refunds aggregate output allowance.
  assert(control(state, "speaking 2"));
  assert(state.receiveBinary(second.data(), second.size()));
  revision = 2; generation = state.generation();
  assert(state.popOutput(scratch, sizeof(scratch), revision, generation) == 640);
  assert(revision == 2 && generation != oldGeneration && state.current(generation));
  assert(!std::memcmp(scratch, second.data(), 640));  // Never replay remaining rev1.
  Event event;
  const EventType order[] = {EventType::Ready, EventType::Speaking, EventType::Clear, EventType::Speaking};
  for (EventType type : order) { assert(state.pollEvent(event)); assert(event.type == type); }
  assert(!state.pollEvent(event));
  assert(control(state, "done 2"));
  assert(state.played(2, generation) == Status::Invalid);  // Receipt is not playback.
  while (state.popOutput(scratch, sizeof(scratch), revision, generation)) {}
  assert(state.played(1, generation) == Status::Invalid);
  assert(state.played(2, generation) == Status::Ok);
  assert(state.played(2, generation) == Status::Invalid);
  uint32_t acknowledgement = 0;
  assert(state.takePlayed(acknowledgement) && acknowledgement == 2);
  assert(!state.takePlayed(acknowledgement));
  assert(control(state, "complete 2"));
  assert(!state.active() && !state.current(generation));
  assert(state.pollEvent(event) && event.type == EventType::Done);
  assert(state.pollEvent(event) && event.type == EventType::Complete);
  assert(state.pushInput(scratch, sizeof(scratch)) == Status::NotActive);
  assert(!state.receiveBinary(second.data(), second.size()));
}

static void aggregateOutputAndOverflow() {
  Fixture fixture;
  auto& state = fixture.state;
  fixture.speaking(); drainEvents(state);
  std::array<uint8_t, 3200> frame{};
  uint8_t scratch[3200]; uint32_t revision = 1, generation = state.generation();
  for (size_t i = 0; i < 150; ++i) {
    assert(state.receiveBinary(frame.data(), frame.size()));
    assert(state.popOutput(scratch, sizeof(scratch), revision, generation) == frame.size());
  }
  assert(control(state, "clear 1")); assert(control(state, "speaking 2")); drainEvents(state);
  for (size_t i = 0; i < 150; ++i) assert(state.receiveBinary(frame.data(), frame.size()));
  assert(state.outputBytes() == kOutputLimit);
  assert(!state.receiveBinary(frame.data(), 2));
  assert(state.failure().reason == FailureReason::OutputLimit);
  assert(!state.active() && state.outputPending() == 0 && !state.current(generation));
  Event event; assert(state.pollEvent(event) && event.error == Error::Limit && !state.pollEvent(event));

  Fixture small(8, 8); small.speaking();
  assert(small.state.receiveBinary(frame.data(), 8));
  assert(!small.state.receiveBinary(frame.data(), 2));
  assert(small.state.failure().reason == FailureReason::OutputOverflow);
  assert(small.state.pollEvent(event) && event.error == Error::Overflow);
  assert(small.state.outputPending() == 0);
}

static void invalidOrderingAndTerminalEvents() {
  for (const char* invalid : {"speaking 1", "clear 1", "done 1", "complete 1"}) {
    Fixture fixture; assert(!control(fixture.state, invalid));
    Event event; assert(fixture.state.pollEvent(event) && event.error == Error::Protocol);
  }
  for (const char* invalid : {"ready", "speaking 1", "speaking 2", "clear 2", "done 2", "complete 1"}) {
    Fixture fixture; fixture.speaking(); assert(!control(fixture.state, invalid));
  }
  Fixture stale; stale.speaking(); assert(control(stale.state, "clear 1"));
  assert(!control(stale.state, "speaking 1"));
  Fixture done; done.speaking(); assert(control(done.state, "done 1"));
  const uint8_t pcm[2] = {1, 2}; assert(!done.state.receiveBinary(pcm, 2));

  for (size_t invalidSize : {size_t(0), size_t(1), size_t(3202)}) {
    Fixture fixture; fixture.speaking();
    std::array<uint8_t, 3202> frame{};
    assert(!fixture.state.receiveBinary(frame.data(), invalidSize));
  }
  Fixture terminal; terminal.speaking(); assert(terminal.state.receiveBinary(pcm, 2));
  const uint32_t generation = terminal.state.generation();
  assert(!control(terminal.state, "error unauthorized"));
  assert(!terminal.state.current(generation) && !terminal.state.outputPending());
  Event event; assert(terminal.state.pollEvent(event) && event.type == EventType::Error && event.error == Error::Unauthorized);
  assert(!terminal.state.pollEvent(event));  // Old ready/speaking events do not survive failure.
  terminal.state.begin(terminal.input.data(), terminal.input.size(), terminal.output.data(), terminal.output.size());
  assert(terminal.state.active() && !terminal.state.ready() && !terminal.state.outputPending());
  assert(!terminal.state.current(generation));
  terminal.speaking(); assert(terminal.state.receiveBinary(pcm, 2));
  terminal.state.fail(Error::Network, EventType::Closed);
  assert(!terminal.state.outputPending() && terminal.state.pollEvent(event) && event.type == EventType::Closed);
}

static void boundedEventsAndFrames() {
  Fixture events;
  events.speaking();
  for (uint32_t revision = 1; revision < 32 && events.state.active(); ++revision) {
    const std::string clear = "clear " + std::to_string(revision);
    if (!control(events.state, clear.c_str())) break;
    const std::string speaking = "speaking " + std::to_string(revision + 1);
    if (!control(events.state, speaking.c_str())) break;
  }
  assert(!events.state.active());
  Event event; assert(events.state.pollEvent(event) && event.error == Error::Overflow);
  assert(!events.state.pollEvent(event));
  assert(events.state.failure().reason == FailureReason::EventOverflow);

  Fixture frames; frames.speaking(); drainEvents(frames.state);
  const uint8_t pcm[2] = {0, 0};
  for (size_t i = 0; i < 8192; ++i) assert(frames.state.receiveBinary(pcm, 2));
  assert(!frames.state.receiveBinary(pcm, 2));
  assert(frames.state.pollEvent(event) && event.error == Error::Limit);
  assert(frames.state.failure().reason == FailureReason::OutputFrameLimit);

  Fixture controls; controls.speaking(); drainEvents(controls.state);
  for (uint32_t revision = 1; controls.state.active(); ++revision) {
    const std::string clear = "clear " + std::to_string(revision);
    if (!control(controls.state, clear.c_str())) break;
    drainEvents(controls.state);
    const std::string speaking = "speaking " + std::to_string(revision + 1);
    if (!control(controls.state, speaking.c_str())) break;
    drainEvents(controls.state);
  }
  assert(controls.state.pollEvent(event) && event.error == Error::Limit);
  assert(controls.state.failure().reason == FailureReason::ControlLimit);
}

// Exercise the actual portable consumer boundary with network events injected
// at the two preemption points in streamStep, not an independent protocol model.
static void replacementInterleavings() {
  Fixture fixture; fixture.speaking();
  auto& state = fixture.state;
  Controller controller; controller.begin(0); controller.onReady();
  assert(controller.onSpeaking(1));
  const uint32_t original = state.generation();
  const uint8_t oldPcm[] = {1, 2}, replacement[] = {3, 4, 5, 6};
  uint8_t scratch[8] = {};
  assert(state.receiveBinary(oldPcm, sizeof(oldPcm)));
  assert(state.popOutput(scratch, sizeof(scratch), 1, original) == sizeof(oldPcm));
  assert(control(state, "done 1"));
  assert(controller.onDone(1, 10, 0));
  assert(!controller.drained(14, true, true));
  assert(controller.drained(15, true, true));  // Four DMA slots plus final real EOF.
  assert(state.current(original));  // Loop's old generation snapshot is valid.
  assert(control(state, "clear 1"));  // Network preempts before pop/played.
  assert(control(state, "speaking 2"));
  assert(state.receiveBinary(replacement, sizeof(replacement)));
  assert(state.popOutput(scratch, sizeof(scratch), 1, original) == 0);
  assert(state.outputPending() == sizeof(replacement));  // No replacement lost.
  assert(state.played(1, original) == Status::Stale);  // Nonfatal invalidated drain.
  assert(state.active());
  Event event;
  while (state.pollEvent(event)) {
    if (event.type == EventType::Clear) assert(controller.onClear(event.revision));
    if (event.type == EventType::Speaking && event.revision == 2)
      assert(controller.onSpeaking(event.revision));
  }
  const uint32_t current = state.generation();
  assert(state.popOutput(scratch, sizeof(scratch), 2, current) == sizeof(replacement));
  assert(!std::memcmp(scratch, replacement, sizeof(replacement)));
  assert(control(state, "done 2") && controller.onDone(2, 20, 1));
  assert(controller.drained(25, true, true));
  assert(state.played(2, current) == Status::Ok);
  controller.onPlayed();
  uint32_t ack = 0;
  assert(state.takePlayed(ack) && ack == 2 && !state.takePlayed(ack));
  assert(control(state, "complete 2") && controller.onComplete(2));
}

static void queuedAcknowledgementInvalidation() {
  Fixture fixture; fixture.speaking();
  auto& state = fixture.state;
  assert(control(state, "done 1"));
  assert(state.played(1, state.generation()) == Status::Ok);
  assert(control(state, "clear 1"));
  uint32_t ack = 99;
  assert(!state.takePlayed(ack) && ack == 99);  // Adapter cannot send played 1.
  assert(control(state, "speaking 2"));
  assert(!state.takePlayed(ack));  // Still invalid after replacement begins.
  assert(state.active() && control(state, "done 2"));
  assert(state.played(2, state.generation()) == Status::Ok);
  assert(state.takePlayed(ack) && ack == 2);
  assert(control(state, "complete 2"));

  Fixture premature; premature.speaking(); assert(control(premature.state, "done 1"));
  assert(premature.state.played(1, premature.state.generation()) == Status::Ok);
  assert(!control(premature.state, "complete 1"));  // Queued is not sent.
  assert(!premature.state.takePlayed(ack));

  Fixture cancelled; cancelled.speaking(); assert(control(cancelled.state, "done 1"));
  assert(cancelled.state.played(1, cancelled.state.generation()) == Status::Ok);
  cancelled.state.fail(Error::Network);
  assert(!cancelled.state.takePlayed(ack));
  cancelled.state.begin(cancelled.input.data(), cancelled.input.size(),
                        cancelled.output.data(), cancelled.output.size());
  cancelled.speaking("speaking 2");
  assert(!cancelled.state.takePlayed(ack));  // Next turn cannot inherit any ack.
}

static void duplexInputAndActualDrain() {
  Controller controller;
  controller.begin(100); controller.onReady();
  assert(controller.acceptingInput() && controller.acceptInput(640));
  assert(controller.onSpeaking(1));
  // This is an input half-close, NOT a command to stop physical AEC capture.
  assert(!controller.acceptingInput() && !controller.acceptInput(640));
  assert(controller.onDone(1, 20, 200));
  assert(!controller.drained(25, false, true));
  assert(!controller.drained(25, true, false));
  controller.noteWrite(24, 240); // Final refill after done extends physical drain.
  assert(!controller.drained(28, true, true));
  assert(controller.drained(29, true, true));
  assert(!controller.drainExpired(1239, true, true));
  assert(controller.drainExpired(1240, true, true));
  controller.onPlayed();
  assert(controller.onComplete(1) && !controller.active());
}

static void bargeInRequiresRemoteRelease() {
  Fixture fixture; fixture.speaking();
  auto& state = fixture.state;
  const uint8_t oldPcm[] = {1, 2, 3, 4}, nearEnd[] = {9, 10, 11, 12};
  assert(state.receiveBinary(oldPcm, sizeof(oldPcm)));
  const uint32_t oldGeneration = state.generation();
  assert(state.requestCancellation() == Status::Ok);
  assert(state.cancelling() && state.active() && !state.cancellationAcknowledged());
  assert(!state.current(oldGeneration) && !state.outputPending());
  assert(state.pushInput(nearEnd, sizeof(nearEnd)) == Status::InputStopped);
  uint8_t output[8]; uint32_t ack = 0;
  assert(state.popOutput(output, sizeof(output), 1, oldGeneration) == 0);
  assert(state.played(1, oldGeneration) == Status::Stale && !state.takePlayed(ack));
  assert(state.receiveBinary(oldPcm, sizeof(oldPcm))); // In-flight old audio is discarded.
  assert(control(state, "done 1") && control(state, "complete 1"));
  assert(state.active() && !state.completed()); // Unplayed complete is NOT release.
  assert(control(state, "cancelled"));
  assert(!state.active() && state.cancellationAcknowledged() && !state.completed());
  Event event;
  assert(state.pollEvent(event) && event.type == EventType::Cancelled && !state.pollEvent(event));
  // New speech is retained outside the invalidated transport, then queued only
  // after its remote-release acknowledgement; begin resets every old generation.
  uint8_t retainedStorage[8]; ByteRing retained; retained.bind(retainedStorage, sizeof(retainedStorage));
  assert(retained.push(nearEnd, sizeof(nearEnd)));
  state.begin(fixture.input.data(), fixture.input.size(), fixture.output.data(), fixture.output.size());
  assert(!state.cancellationAcknowledged() && !state.completed());
  const size_t bytes = retained.pop(output, sizeof(output));
  assert(bytes == sizeof(nearEnd) && state.pushInput(output, bytes) == Status::Ok);
  assert(control(state, "ready"));
  assert(state.popInput(output, sizeof(output)) == bytes && !std::memcmp(output, nearEnd, bytes));

  Fixture queuedPlayed; queuedPlayed.speaking(); assert(control(queuedPlayed.state, "done 1"));
  assert(queuedPlayed.state.played(1, queuedPlayed.state.generation()) == Status::Ok);
  assert(queuedPlayed.state.requestCancellation() == Status::Ok);
  assert(!queuedPlayed.state.takePlayed(ack)); // A queued, unsent played never escapes barge-in.
  assert(control(queuedPlayed.state, "cancelled"));

  Fixture preReady;
  assert(preReady.state.requestCancellation() == Status::Ok);
  assert(control(preReady.state, "ready") && control(preReady.state, "cancelled"));
  assert(preReady.state.cancellationAcknowledged());

  for (const char* failure : {"error upstream", "error unauthorized"}) {
    Fixture failed; failed.speaking();
    assert(failed.state.requestCancellation() == Status::Ok);
    assert(!control(failed.state, failure) && !failed.state.cancellationAcknowledged());
  }
  Fixture closed; closed.speaking(); assert(closed.state.requestCancellation() == Status::Ok);
  closed.state.fail(Error::Network, EventType::Closed);
  assert(!closed.state.cancellationAcknowledged() && !closed.state.completed());
}

static void alreadyPlayedCompletionIsHandoff() {
  Fixture fixture; fixture.speaking();
  auto& state = fixture.state;
  assert(control(state, "done 1"));
  assert(state.played(1, state.generation()) == Status::Ok);
  uint32_t ack = 0;
  assert(state.takePlayed(ack) && ack == 1 && state.playedSent());
  assert(state.requestCancellation() == Status::NotActive);
  assert(!state.cancellationAcknowledged() && !state.completed());
  assert(control(state, "complete 1"));
  assert(state.completed() && !state.active() && !state.cancellationAcknowledged());
  Fixture unsolicited;
  assert(!control(unsolicited.state, "cancelled")); // Never trust an unsolicited handoff.
}

static void lostDmaCannotBecomeSuccessfulDrain() {
  using glasses_audio::DmaTimeline;
  using glasses_audio::ReferencePhase;
  int descriptors[4] = {};
  DmaTimeline timeline;
  for (int i = 0; i < 12; ++i)
    assert(timeline.observe(&descriptors[i & 3], 10000 + int64_t(i) * 10000));
  assert(!timeline.observe(&descriptors[1], 140000)); // Missing one EOF and wrong next descriptor.

  timeline.reset();
  assert(timeline.observe(&descriptors[0], 10000));
  assert(!timeline.observe(&descriptors[0], 20000)); // Premature descriptor reuse.
  timeline.reset();
  for (int i = 0; i < 4; ++i)
    assert(timeline.observe(&descriptors[i], 10000 + int64_t(i) * 10000));
  assert(!timeline.observe(&descriptors[1], 50000)); // Timestamp alone cannot hide a descriptor slip.
  timeline.reset();
  for (int i = 0; i < 4; ++i)
    assert(timeline.observe(&descriptors[i], 10000 + int64_t(i) * 10000));
  assert(!timeline.observe(&descriptors[0], 90000)); // Whole DMA-ring loss keeps pointer, not time.

  ReferencePhase phase;
  assert(phase.accept(16000) && phase.accept(16200) && phase.accept(15800));
  assert(!phase.accept(26000) && !phase.accept(6000)); // Either one-block slip fails closed.
  assert(!phase.accept(19000)); // Sub-block drift is bounded too, never silently retimed.
  phase.reset();
  assert(!phase.accept(-1000) && !phase.accept(40000));
  assert(phase.accept(12000)); // Explicit clean restart may select a new measured phase.
}

// This is SecureClient's real connected/available/read/write guard, not a
// separate timing model. Reads from a trickle peer must not refresh the frame.
static void absoluteIoAndCloseBudgets() {
  IoBudget io(0);
  io.budget(0, 1000);
  assert(io.blocked(999, false) == FailureReason::None);
  assert(io.blocked(1000, false) == FailureReason::OperationTimeout);
  // Idle polling already refreshed the old adapter's budget; no idle timeout.
  for (uint32_t now = 1000; now < 20000; now += 1000) {
    io.budget(now, 15000);
    assert(io.blocked(now, false) == FailureReason::None);
  }
  const uint32_t frameStart = 20000;
  io.budget(frameStart, 15000);
  for (uint32_t now = frameStart; now < frameStart + 15000; now += 997)
    assert(io.blocked(now, false) == FailureReason::None);
  assert(io.blocked(frameStart + 15000, false) == FailureReason::OperationTimeout);
  // An interleaved upload cannot renew the inbound frame's absolute deadline.
  io.budget(34000, 5000);
  io.budget(frameStart, 15000);
  assert(io.blocked(35000, false) == FailureReason::OperationTimeout);
  io.budget(59000, 15000);
  assert(io.blocked(60000, false) == FailureReason::TurnTimeout);
  assert(io.blocked(59000, true) == FailureReason::LocalCancel);
  io.closing(60000);
  assert(io.blocked(60199, true) == FailureReason::None);
  assert(io.blocked(60200, true) == FailureReason::OperationTimeout);

  IoBudget wrap(UINT32_MAX - 100);
  wrap.budget(UINT32_MAX - 50, 200);
  assert(wrap.blocked(148, false) == FailureReason::None);
  assert(wrap.blocked(149, false) == FailureReason::OperationTimeout);
}

static void causalFailureSurvivesCleanup() {
  FailureLatch network;
  network.record(FailureReason::TlsRead, -123);
  network.record(FailureReason::LibraryDisconnect);
  network.record(FailureReason::LocalCancel);
  assert(network.get().reason == FailureReason::TlsRead && network.get().detail == -123);
  network.reset();
  assert(network.get().reason == FailureReason::None);

  const uint8_t pcm[2] = {};
  Fixture input(2, 8);
  assert(input.state.pushInput(pcm, 2) == Status::Ok);
  assert(input.state.pushInput(pcm, 2) == Status::Overflow);
  assert(input.state.failure().reason == FailureReason::InputOverflow);
  drainEvents(input.state);
  input.state.fail(Error::Cancelled, EventType::Closed);
  assert(input.state.failure().reason == FailureReason::InputOverflow);

  Fixture output(8, 2); output.speaking(); drainEvents(output.state);
  assert(output.state.receiveBinary(pcm, 2));
  assert(!output.state.receiveBinary(pcm, 2));
  assert(output.state.failure().reason == FailureReason::OutputOverflow);
  assert(!output.state.outputPending());
  drainEvents(output.state);
  assert(output.state.failure().reason == FailureReason::OutputOverflow);

  Fixture order;
  assert(!order.state.receiveBinary(pcm, 2));
  assert(order.state.failure().reason == FailureReason::BinaryOrder);
  Fixture syntax;
  assert(!control(syntax.state, "unexpected"));
  assert(syntax.state.failure().reason == FailureReason::ControlSyntax);
  Fixture controlOrder;
  assert(!control(controlOrder.state, "speaking 1"));
  assert(controlOrder.state.failure().reason == FailureReason::ControlOrder);
  assert(controlOrder.state.failure().detail == static_cast<int32_t>(EventType::Speaking));
  Fixture remote;
  assert(!control(remote.state, "error upstream"));
  assert(remote.state.failure().reason == FailureReason::RemoteError);
  assert(remote.state.failure().detail == static_cast<int32_t>(Error::Upstream));
  remote.state.begin(remote.input.data(), remote.input.size(), remote.output.data(), remote.output.size());
  assert(remote.state.failure().reason == FailureReason::None);
}

int main() {
  parsing(); ringWrapAndOverflow(); inputBoundsAndReady(); clearAndGenerations();
  aggregateOutputAndOverflow(); invalidOrderingAndTerminalEvents(); boundedEventsAndFrames();
  replacementInterleavings(); queuedAcknowledgementInvalidation();
  duplexInputAndActualDrain(); bargeInRequiresRemoteRelease(); alreadyPlayedCompletionIsHandoff();
  lostDmaCannotBecomeSuccessfulDrain();
  absoluteIoAndCloseBudgets(); causalFailureSurvivesCleanup();
  std::puts("firmware streaming parser/ring/state invariants passed");
}
