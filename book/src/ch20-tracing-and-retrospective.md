# 20. Measuring, and looking back

> **Where we are:** `01e4602` "instrument x4 perf" (17 September 2026),
> `20ed551` "add lightweight trace recorder" (18 September), exports to
> Speedscope (`118dafc`) and Perfetto (`ccc34dd`), the 22 September redesign
> (`7eed6fd` self-describing captures, `b06f3f3` "replace trace sessions with
> flight recorder", `68f2909` the `instrument` macro, `0d5d80d` async spans),
> and the stream of optimisations those tools enabled.

## Why build a profiler?

By mid-September the reader worked on the device, and it was slow. Turning a page
took long enough to notice. Was it pagination? Shaping? Rasterizing glyphs? SPI
transfers? Waiting for the panel?

On a desktop you'd reach for `perf` or Instruments. On a microcontroller connected
through a debug probe there's nothing like that, and adding `println!`-style
timing everywhere is noisy and slow. So the project built **`inkpaper-trace`**, a
small tracing library with three requirements:

1. **Zero cost when off.** Production firmware must not pay for tracing.
2. **Cheap when on.** Measuring must not distort what's measured.
3. **Readable results** in standard tools.

## Zero cost when off: the same API, two implementations

Tracing is behind a feature, `recording`. When it's off, the crate still exports
the same types, but they're empty:

```rust
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

A zero-sized `Span` with no `Drop` implementation compiles to nothing at all.
Code that creates spans compiles identically with and without the feature, so
there are no `#[cfg]` attributes scattered through the app. The firmware's
`trace` feature switches everything on at once:

```toml
[features]
trace = ["inkpaper-trace/recording", "inkpaper-ui/metrics", "inkpaper-ui-eink/metrics"]
```

This is a useful pattern: **one public API, two implementations chosen by a
feature**, with the "off" version made of zero-sized types and `const fn`s. (It's
also why `firmware/x4-pro/src/main.rs` has
`#![allow(clippy::drop_non_drop)]`: explicit `drop(span)` calls are meaningful
with tracing on and look pointless to Clippy with it off.)

## A span is a drop guard

Using it looks like this:

```rust
let _trace = span!(target: "reader.epub", "open");
// ... work ...
// the span ends when `_trace` is dropped, at the end of the scope
```

Note the name `_trace` and not `_`. In Rust, `let _ = value;` drops the value
**immediately**, while `let _trace = value;` keeps it alive until the end of the
scope. For guards, that difference is everything. Write `let _ = span!(...)` and
you'd measure nothing.

When recording, `Span`'s `Drop` implementation does the measuring:

```rust
impl Drop for Span {
    #[inline(always)]
    fn drop(&mut self) {
        let Some(clock) = self.clock else {
            return;
        };

        let ended_at = clock.now_cycles();

        let duration_cycles = ended_at.saturating_sub(self.start_cycles);

        critical_section::with(|cs| {
            let mut state = TRACE.borrow(cs).borrow_mut();

            if state.generation != self.generation {
                return;
            }
            // ... record the span ...
        });
    }
}
```

This is chapter 3's drop-guard idea used for measurement: the constructor reads
the CPU's cycle counter, and the destructor reads it again. Since `Drop` runs on
*every* exit from the scope (`return`, `?`, a panic), the measurement is always
closed. `#[inline(always)]` keeps the overhead to a few instructions.

### Global state without `static mut`

The recorder keeps its buffers in a global:

```rust
static TRACE: Mutex<RefCell<TraceState>> = Mutex::new(RefCell::new(TraceState::new()));
```

This is the standard embedded recipe for mutable global state:

- `critical_section::Mutex<T>` can only be accessed inside
  `critical_section::with(|cs| ...)`, which disables interrupts on a single-core
  chip. The `cs` token *proves* you're in a critical section, and
  `TRACE.borrow(cs)` requires it. That's another case of a **type as a proof**.
- `RefCell` then provides `&mut` access inside, with the usual runtime borrow
  check.
- `TraceState::new()` must be a `const fn` to appear in a `static` initialiser.

The `generation` check in `drop` handles a subtle case. If the trace buffer was
reset or captured while this span was open, the span belongs to an old
generation and is discarded instead of corrupting the new buffer. That's the
generational idea from chapters 3 and 5 once more.

On targets with 32-bit atomics, checking whether recording is enabled avoids the
critical section entirely:

```rust
#[cfg(target_has_atomic = "32")]
#[inline(always)]
fn set_recording_enabled(enabled: bool) {
    RECORDING_ENABLED.store(u32::from(enabled), Ordering::Relaxed);
}

#[cfg(not(target_has_atomic = "32"))]
#[inline(always)]
fn set_recording_enabled(enabled: bool) {
    critical_section::with(|cs| {
        *RECORDING_ENABLED.borrow(cs).borrow_mut() = enabled;
    });
}
```

`#[cfg(target_has_atomic = "32")]` is a **target feature cfg**. The same source
picks the best mechanism for each chip, and `Ordering::Relaxed` is enough for a
simple on/off flag that doesn't guard other data.

## Static call sites, built by a macro

The `span!` macro is a `macro_rules!` with one arm per number of fields (up to
two, and a `compile_error!` for more). Here is the one-field arm:

```rust
(
    target: $target:expr,
    $name:literal,
    $field0:ident = $value0:expr
    $(,)?
) => {{
    static __INKPAPER_TRACE_FIELDS: [$crate::Field; 1] = [
        $crate::Field::new(stringify!($field0)),
    ];

    static __INKPAPER_TRACE_CALLSITE: $crate::Callsite =
        $crate::Callsite::new(
            $name,
            $target,
            &__INKPAPER_TRACE_FIELDS,
        );

    if $crate::__private::enabled() {
        $crate::__private::start(
            &__INKPAPER_TRACE_CALLSITE,
            [
                $crate::__private::into_value($value0),
                $crate::Value::EMPTY,
            ],
        )
    } else {
        $crate::Span::disabled()
    }
}};
```

The trick: **a `static` declared inside a block is unique to that block**. Since
each macro invocation expands to its own block, every call site gets its own
`static Callsite` holding the span's name, target and field names. At runtime a
span records only a *pointer* to its call site and two values. Names are never
copied into the trace buffer. A capture is decoded later, on the laptop, by
following the call site pointers. The tracing crate in the wider ecosystem works
the same way.

Other details worth noticing:

- **`$crate`** refers to the crate that defines the macro, wherever it's
  expanded. Macros that call their own crate's items must use it, because the
  caller may have renamed the crate or not imported anything.
- **`stringify!($field0)`** turns an identifier into a string literal at compile
  time.
- **`$crate::__private`** is a hidden module of helpers that only the macro
  should call. That's a common convention for "public for macros, not for
  people".
- **`$name:literal`** forces the name to be a literal, so it can live in a static.

## `#[instrument]`: spans for whole functions

Writing `let _trace = span!(...)` at the start of every interesting function got
repetitive, so commit `68f2909` added an attribute macro:

```rust
#[inkpaper_trace::instrument(target = "reader.session", name = "open")]
async fn open(/* ... */) -> Result<ReaderSession<S>, ReaderError> {
    // ...
}
```

It wraps the function's body in a block that starts with a span:

```rust
let span_macro = if signature.asyncness.is_some() {
    quote! { ::inkpaper_trace::async_span }
} else {
    quote! { ::inkpaper_trace::span }
};
// ...
let guard = Ident::new("__inkpaper_trace_guard", Span::mixed_site());

syn::parse_quote!({
    let #guard = #span;

    #original
})
```

Two details are worth noting.

**Async functions need different spans.** A synchronous span assumes perfect
nesting: starting one increments a global depth counter and dropping it
decrements it, so spans form a tree. An `async fn` can be suspended at an
`.await` while other tasks run and open their own spans, so that nesting breaks.
An `AsyncSpan` never touches the depth counter. It just records its start time
and duration as a separate `SpanKind::Async` record. The macro checks
`signature.asyncness` and picks the right one automatically.

**Hygiene.** The guard variable is created with `Span::mixed_site()` (a
`proc_macro2::Span`, a source location, not a trace span). That gives the
identifier **macro hygiene**: the user's code inside the function can't see or
accidentally shadow `__inkpaper_trace_guard`, even if it uses the same name.

## A flight recorder, and tools on the laptop

The first design recorded "sessions" that had to be started and stopped. On
22 September it became a **flight recorder**: two buffers record continuously,
and when something interesting finishes (a page turn, say), the recent history is
frozen into a **capture** and printed over the debug probe as text. Like an
aircraft's black box, you don't need to know in advance when to start recording.

The captures are self-describing (`7eed6fd`): they contain the call site names,
so the laptop-side tools need no schema. The tools live in `tools/xtask`:

```rust
#[derive(Debug, Subcommand)]
enum Command {
    /// generate EPUB fixtures
    Fixtures,

    /// inspect performance data from trace/v4 firmware logs
    Perf {
        #[command(subcommand)]
        command: PerfCommand,
    },

    /// measure reader text as the device draws it
    TextQuality { /* ... */ },

    /// convert trace/v4 firmware logs to a Perfetto trace
    TracePerfetto {
        /// firmware log containing trace/v4 captures
        input: PathBuf,

        /// output Perfetto trace file
        #[arg(short, long)]
        output: Option<PathBuf>,
    },
}
```

That's `clap`'s derive API: **doc comments become `--help` text**, struct fields
become arguments, and enums become subcommands. And the `justfile` ties it
together:

```make
x4-trace-log output="target/perf/latest-trace.log":
    # ... archive the previous log as before-trace.log ...
    just x4-trace 2>&1 | tee "{{output}}"

x4-trace-report log="target/perf/latest-trace.log":
    cargo xtask perf summary "{{log}}"
    cargo xtask trace-perfetto "{{log}}"

x4-trace-compare before="target/perf/before-trace.log" after="target/perf/latest-trace.log":
    cargo xtask perf compare "{{before}}" "{{after}}"
```

The workflow is: record a trace, change something, record again, **compare**.
Perfetto (ui.perfetto.dev) shows the spans on a timeline.

## What the measurements found

The commit log right after the tracer arrived reads like a lab notebook:

- `e9de933` instrument the glyph cache → `715f92a` use linear probing →
  `4981e97` increase capacity (chapter 16).
- `4fa065f` "add x4 ordered coverage blitter": a specialised glyph-drawing path
  for the device framebuffer.
- `c65afdf`, `f1f1b98`, `852ac18`, `272ee03`: "reuse ttf face for ...". Parsing
  a font's tables once instead of once per glyph.
- `7d726b0` "cache repeated text pair positioning", `8cad282` "cache repeated
  reader text measurements" (chapter 18).
- `9cc9964`, `a916362`, `3e4bf3e`: defer power-off, pre-start power-on, and keep
  the UC8179 powered across page bursts (chapter 12).
- `8d32e44`, `f476444`: allow fast refreshes where the panel supports them
  (chapter 9).

None of these was a guess. Each one followed a trace showing where the time went,
and the next trace confirmed the gain. **Measure, change one thing, measure
again** is the most valuable lesson in this chapter, and it doesn't depend on
Rust at all.

## Looking back

We've followed InkPaper from a design document to a working e-reader. Here are
the threads that ran through the whole book.

**Types carry meaning.** `Pixels`, `BorderBox`, `ArchivePath`, `DateTime`,
`Entity<T>`, `Listener<E>`, `AvailableSpace`, `Verdict`. Again and again, a value
that could have been an `i32`, a `String` or a `bool` got its own type, and a
class of bugs became a compile error.

**Ownership models more than memory.** Peripherals that can be taken only once
(chapter 13), drivers that own their buses (chapter 11), painters that borrow a
display for one frame (chapter 8), glyph bitmaps that borrow the cache (chapter
16), one-shot replies consumed by `send` (chapter 14). Rust's ownership rules turn
out to describe hardware and protocols as naturally as they describe heap memory.

**Bounded everything, with documented degradation.** Damage lists collapse to a
bounding box, dependency tables mark themselves incomplete, bidi falls back to
neutral, the rasterizer fails explicitly, decompression refuses to exceed the
declared size. Each limit is a constant with a comment saying what happens when
it's reached. Choose the direction of the error: repaint too much, never too
little.

**`unsafe` in small, documented boxes.** The arena of chapter 4 and the
trampolines of chapter 5 contain nearly all of the UI's `unsafe` code, behind
safe, typed APIs, with written invariants. Everything built on top is safe Rust.

**Traits at every boundary.** `embedded-hal` between drivers and chips,
`EpdInterface` between controllers and buses, `Painter` between the UI and
displays, `TextMeasurer` between the paginator and fonts, `EpubSource` between the
parser and storage, `AppPlatform` between the app and the device. Each boundary is
a place where the simulator, a test or another device can plug in.

**Tests on the host.** Drivers, parsers, layout, shaping, pagination and the whole
app run on a laptop. The firmware is the thinnest layer possible. That's what
made three months of rapid development possible.

**History is part of the code.** Commit messages describe behaviour ("wait on the
busy pin by interrupt with a timeout"). Comments explain decisions and cite their
sources ("this matches crosspoint's bound"). Code that was wrong was removed
("remove focus navigation"). Reading InkPaper's history taught us at least as
much as reading its code.

## Where to go next

- **Read a subsystem end to end.** Pick one: the dictionary crate
  (`crates/dictionary`, StarDict files read through sampled indexes), USB mass
  storage (`firmware/x4-pro/src/firmware/usb_mass_storage.rs`), or WiFi and SNTP
  clock sync (`crates/sntp`, `firmware/x4-pro/src/firmware/wifi.rs`). All of them
  use the patterns from this book.
- **Pick an item from `TODO.md`.** "Derive bold and italic for card families
  without them", "word spacing and character spacing", "more hyphenation
  languages" (chapter 18 says it's "one feature away"), "draw a hyphen where a
  line breaks at a book's own soft hyphen".
- **Run the simulator with your own books.** Put EPUBs in `fixtures/` and see
  which ones break. Then reproduce the quirk as a generated fixture in
  `tools/xtask/src/fixtures`, as chapter 15 described.
- **Contribute.** The README welcomes PRs, issues and discussions.

Thanks for reading. Now go build something.

## Rust concepts in this chapter

- **Feature-selected implementations** behind one API; zero-sized no-op types.
- **`let _ = x` vs `let _guard = x`**: when a value is dropped.
- **Drop guards for measurement**; `#[inline(always)]`.
- **`critical_section::Mutex<RefCell<T>>`** for global mutable state; tokens as
  proof.
- **`#[cfg(target_has_atomic = "...")]`**, atomics and `Ordering::Relaxed`.
- **`static` items inside macro-generated blocks** for per-call-site data;
  `$crate`, `stringify!`, `$name:literal`, hidden `__private` modules.
- **Attribute macros that wrap function bodies**; detecting `async fn`; hygiene
  with `Span::mixed_site()`.
- **`clap` derive** for project tooling; the xtask pattern.

## Try it yourself

1. In a test with the `recording` feature (see `crates/trace/tests/`), write
   `let _ = span!(target: "t", "a");` and `let _guard = span!(target: "t", "b");`
   around some work and compare their recorded durations.
2. Apply `#[inkpaper_trace::instrument(target = "test", name = "work")]` to an
   `async fn` and use `cargo expand` to confirm it chose `async_span!`.
3. Run `cargo xtask --help` and `cargo xtask perf --help`, and find in
   `tools/xtask/src/main.rs` where each line of help text comes from.
