# 20. Where the page-turn time goes

You press Next. The words change, but the pause is just long enough to interrupt the sentence you were following. Making that pause shorter sounds like one problem. Inside an e-reader it is several: selecting or loading content, preparing an interface, drawing pixels, transferring them, and waiting for a physical display to finish its work.

The useful question is not “Which function looks expensive?” It is “Which interval can we shorten without changing the page the reader receives?” InkPaper's tracing tools make that question concrete. Reading their boundaries also teaches a final Rust lesson: ownership can describe the lifetime of a measurement as precisely as it describes the lifetime of a buffer.

## Start with the reader's budget

Treat a page turn as a budget running from accepted input to usable new text. There is no universal target duration here. First separate the work on that path, then decide which part matters for the experience you want.

Even two presses of Next need not exercise the same path. In `crates/app/src/reader/state/navigation.rs`, an ordinary turn within the loaded document advances `page_index` and queues a progress update. Reaching its end requests the adjacent chapter instead. That can involve content loading and pagination, whose spans live in `crates/app/src/reader/loader.rs`. Averaging those cases together hides a meaningful difference in workload.

For the visible frame, the firmware separates rendering from presentation. This exact excerpt begins `render_frame`'s work in `firmware/x4-pro/src/firmware/presenter.rs`; it depends on the surrounding function's parameters:

```rust,ignore
let render_trace = inkpaper_trace::span!(
    target: "ui.render",
    "render",
    frame = frame_id,
);

let damage = runtime
    .prepare_frame_with(DISPLAY_SIZE, |phase| match phase {
        FramePhase::Rebuild => inkpaper_trace::span!(target: "ui.render", "rebuild"),
        FramePhase::Layout => inkpaper_trace::span!(target: "ui.render", "layout"),
    })
    .expect("UI rebuild capacity exceeded");
```

Further spans surround clearing damaged pixels, painting, and converting damage into physical coordinates. The outer render interval includes those children. Adding all their durations to the outer duration would count work twice. Instead, use the outer span for the budget and the nested spans to explain it.

Presentation begins elsewhere, in `firmware/x4-pro/src/firmware/display/mod.rs`. Its profiled bus distinguishes I/O operations from waits for the controller's busy signal. Faster glyph drawing can reduce rendering; it cannot directly shorten a panel waveform. Conversely, changing refresh policy might affect latency while also changing ghosting or visible flashing. The measurement suggests a place to investigate, not permission to sacrifice the reading experience.

## Give an interval an owner

`span!` returns a guard. Starting it reads a clock; dropping it calculates elapsed cycles and records the interval. A named binding such as `render_trace` keeps the guard alive until its scope ends or the code explicitly drops it. Writing `let _ = span!(...)` instead would discard that newly created temporary at the statement, ending the interval immediately.

This uses the same `Drop` mechanism that releases an owned resource. Normal returns and `?` exits drop guards too. Unwinding can run destructors, but aborts and power loss do not promise a completed measurement. A trace is not a crash-proof diary.

In `crates/trace/src/recording.rs`, the recorder stores shared state in a `critical_section::Mutex<RefCell<TraceState>>`. The critical-section token controls access to the global state; `RefCell` supplies checked mutable borrowing within that access. The target's critical-section implementation supplies the actual exclusion mechanism. Recording is bounded, but it still involves clock reads and protected buffer updates, so “lightweight” must not be mistaken for “free.”

When recording is not enabled, the crate supplies this exact replacement:

```rust,ignore
#[cfg(not(feature = "recording"))]
#[derive(Debug, Default)]
pub struct Span;

#[cfg(not(feature = "recording"))]
impl Span {
    pub const fn disabled() -> Self {
        Self
    }
}
```

This is a zero-sized type with no recording destructor. A separate disabled macro arm returns it without executing field expressions. The firmware's `trace` feature enables the recorder and the UI metrics together. That is preferable to sprinkling printing calls through every hot path, but it creates two configurations: compare like with like, and do not assume a trace-enabled build has identical timing to the ordinary firmware.

The enabled macro also gives each call site static metadata: its target, name, and up to two field names. Individual records can refer to that metadata rather than allocate new strings on every entry. Chapter 19's code generation is doing practical work here: preparing a stable description once, while leaving small varying values such as `frame_id` for runtime.

## Waiting is not CPU work

A synchronous span participates in the recorder's nesting depth. An asynchronous operation may suspend while other operations run, so its lifetime cannot safely be represented by that single nesting stack. `AsyncSpan` records a separate kind of interval without changing synchronous depth. The `#[instrument]` macro selects `async_span!` when the function signature is asynchronous.

That interval measures the function body's lifetime after execution starts, including suspension. It is not a measurement of how long the CPU actively executed that function, and it does not start merely because somebody constructed the future. A long busy-wait span can therefore be the right explanation for a slow page turn even while the executor is free to perform other work.

The host test `crates/trace/tests/async_instrument.rs` makes this distinction visible without a panel. Its future yields once; the test advances injected clock values by hand. These are exact, contiguous assertions around the capture boundary:

```rust,ignore
let first_capture = capture().unwrap();

// The async function has started,
// but its lifetime is not complete.
assert_eq!(first_capture.spans(), 0);

drop(first_capture);

assert_eq!(future.as_mut().poll(&mut context), Poll::Ready(18));

let second_capture = capture().unwrap();

assert_eq!(second_capture.spans(), 1);
```

The completed span later has a duration of 80 synthetic cycles. That tests lifetime accounting, not the speed of the laptop or device.

The recorder has two buffers. A capture freezes the active one and switches recording to the other; dropping the capture releases the frozen buffer. Only one capture may be held at a time. Importantly, an interval still open at the switch is recorded when it closes, in the then-active buffer. A buffer switch is not a reset of that interval. The generation check in guard destruction instead protects against recorder reinitialization.

This explains the explicit `drop(render_trace)` before the presenter captures its records. The display path likewise drops its presentation guard before capture. Each buffer holds at most 256 span records and 128 metric entries, with overwritten-span and dropped-metric counts carried into the exported data. Missing records are evidence about the measurement's limits, not evidence that the missing work was instantaneous.

## Read the report without enlarging its promise

The firmware emits self-describing `trace/v4` text containing metadata as well as values. Laptop tools can therefore decode a saved capture without recovering names from a firmware image. With an existing device log at the stated path, the current commands are:

```sh
cargo xtask perf summary target/perf/latest-trace.log
cargo xtask trace-perfetto target/perf/latest-trace.log --output target/perf/page-turn.json
cargo xtask perf compare target/perf/before-trace.log target/perf/latest-trace.log
```

These commands analyze files; they do not collect measurements. `cargo xtask` is the repository's alias for running the `xtask` package. For readers who already have the supported device and probe setup, `just x4-trace-log` runs the profiling configuration and saves its output. Hardware capture is optional for this session.

The summary joins render and presentation captures by frame ID. Its definition of total is deliberately small. This exact method is in `tools/xtask/src/trace/perf.rs`:

```rust,ignore
fn total_cycles(&self) -> u64 {
    self.render.saturating_add(self.present)
}
```

That is not automatically button-to-page latency. Input handling, chapter loading before rendering, scheduling gaps, or logging between intervals can lie outside it. Use the Perfetto timeline to understand relationships, but do not add overlapping asynchronous intervals as though they were sequential CPU work.

Presentation's “other” time is a residual after subtracting recorded I/O and busy waits. It is not the name of a newly discovered subsystem. I/O timing itself wraps bus operations, including awaits, so its byte rate is effective throughput at that boundary rather than a direct measurement of raw SPI clock speed. The report also tracks incomplete frames and lost records. Read those qualifications before trusting the totals.

For a comparison, keep the content, font settings, refresh mode, build configuration, and cache state comparable. A warm glyph cache and a cold one answer different questions. Host measurements can isolate parser, layout, or painting work, but the simulator does not supply device storage, transfer, and physical panel timings. Use the host to test a mechanism; use firmware captures to establish its contribution to the device budget. Neither result should impersonate the other.

## Follow one boundary yourself

The small optional experiment is `cargo test -p inkpaper-trace --features recording --test async_instrument`. It needs no hardware. Read the test first and predict which capture contains the span. The expected result is a passing test with the first capture empty and the completed asynchronous interval in the second; the asserted 80 cycles come from the injected clocks, not a benchmark.

For your next independent session, trace `next_page` at a chapter boundary into the loader's `reader.pagination/paginate` span, then locate where `ui.render/render` begins. On paper, mark which work the summary's total includes and which it excludes. Choose one excluded interval you would need to measure before promising a faster chapter transition. That leaves you with a precise experimental question—and a way to avoid optimizing a number the reader never experiences.
