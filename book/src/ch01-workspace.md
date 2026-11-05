# 1. A page on your desk: the workspace

An e-reader earns its place by getting out of the way. Tap a book, see a page, turn it, keep reading. Yet that apparently quiet experience asks software to do several quite different jobs: understand a document, find sensible line breaks, draw letters, interpret a finger, and persuade a physical display to change. InkPaper lets us explore those jobs without starting with a circuit board.

The quickest useful destination is a working window, not a diagram of every crate. From the repository root, this is the simulator command documented in the README:

```sh
cargo run -p inkpaper-simulator
```

The desktop backend uses `embedded-graphics-simulator`; its [installation instructions](https://crates.io/crates/embedded-graphics-simulator) cover the native SDL prerequisites. Those are desktop dependencies, not the ESP32 toolchain. Once they are available, the command builds and launches the application in a portrait window.

Use the mouse's left button as a finger. Open the file browser and choose `/Fixtures`, whose entries come from real EPUB files in this repository. The plausible-looking titles elsewhere in the browser are illustrative listings, not bundled novels. In a book, the right and left arrow keys send Next and Previous; `H` sends Home. Getting a page on screen is our first win. The rest of this chapter explains what you have just exercised—and what a desktop window cannot tell you.

## One picture, several kinds of work

The simulator presents a 480-by-800 drawing surface. A page is not sent to that surface as an EPUB, a paragraph, or even a string. It becomes pixels. Keeping those representations separate makes the implementation understandable.

The document side starts with the EPUB's content and styling. The reader measures text and divides it into lines and pages. Measurement needs font information: letters have different widths, and adjacent characters can affect which glyphs are used and where they sit. A *glyph* is a drawable form from a font, not necessarily one character. InkPaper's reader measurer uses the UI text shaper to obtain advances—the distances needed to place text—before pagination commits to a line width.

Painting then works from a laid-out page. In `crates/app/src/reader_page.rs`, `paint_page` visits text and image fragments. Text fragments become styled text runs at known bounds; images become image draw operations. Around the page, the application's menus and labels use an element tree that the UI lays out. The two routes meet at painting, without pretending a book page must be thousands of tiny menu widgets.

The screen adds another separation. A desktop window can display the resulting pixels promptly. An e-paper panel must physically change its image, and updating it has costs that are not captured by a fast host render. That is why knowing *what changed* matters. Repainting the entire interface whenever the battery is sampled would be convenient software and an unnecessarily restless reading experience.

Here is an exact fragment from the simulator's `render_ui` in `crates/sim/src/render.rs`:

```rust,ignore
let damage = runtime
    .prepare_frame(DISPLAY_SIZE)
    .expect("UI rebuild failed");
if damage.is_none() {
    return;
}

let mut painter = EmbeddedGraphicsPainter::new(display)
    .with_coverage_mode(CoverageMode::alpha_blend(read_simulator_pixel));

painter.clear_damage(damage, Color::WHITE).unwrap();
```

This is a function-body excerpt, not a standalone program. `prepare_frame` prepares the UI and returns its damage: the area needing attention. No damage means no painting here. Otherwise the simulator creates a painter, clears the damaged area, and subsequently calls `paint_with_damage`. Notice the order: decide what needs work, then draw it. Later chapters will make that decision precise.

The simulator is therefore useful for application behavior, layout, and text appearance. It is not a measurement of panel refresh latency, battery use, or device memory pressure. Sharing code gives us a valuable testing surface, not a claim that two machines are equivalent.

## The boundary that makes the window possible

A Rust *crate* is a compilation unit with a public interface. Cargo packages describe those units and their dependencies. InkPaper's workspace collects related packages so they can share dependencies and be developed together. This exact root `Cargo.toml` excerpt is enough of the map for now:

```toml
[workspace]
resolver = "3"
members = ["crates/*", "firmware/*", "tools/*"]
default-members = ["crates/*"]
```

The `members` patterns include reusable crates, the firmware, and tools. `default-members` narrows the packages selected by root commands that do not explicitly choose a package. Our launch command uses `-p` because we want one particular program. A workspace is not one giant executable, and choosing a package is not choosing a hardware target.

The important architectural boundary is between application decisions and platform work. `inkpaper-app` depends on the UI, reader, EPUB, dictionary, and tracing crates; its dependency list does not include SDL or the ESP32 hardware abstraction layer. The simulator supplies desktop services. The firmware supplies device services. Both can create `InkPaperApp` because the application does not need to know which display transport or filesystem implements those services.

This is a practical use of crate boundaries. Code cannot casually import an undeclared driver dependency. That does not make poor architecture impossible—someone could add the dependency—but it makes crossing the boundary an explicit change rather than an accidental call buried inside a screen.

Compare this with a single executable whose screen code opens files and touches peripherals directly. That can be pleasantly simple at first. It also makes every screen test a platform test. Here, the extra boundary buys a short feedback loop: much of the reader can be exercised on the desk while the device integration remains a separate concern.

## Small Rust does not mean no heap

The UI library declares `#![no_std]`. That means it does not automatically link Rust's `std` library or use its prelude. It does **not** mean “no memory allocation,” “no operating system can run this,” or “all data must be on the stack.”

Rust's `core` provides language-level building blocks such as slices, iterators, `Option`, `Result`, formatting traits, and pointer operations. The separate `alloc` library provides owned heap-backed structures such as `Vec`, `String`, and `Box`, provided the final program supplies an allocator. `std` adds facilities such as files and threads, alongside re-exporting much of that common machinery. A desktop executable using `std` can happily depend on a `no_std` library.

These exact, adjacent declarations in `crates/ui/src/lib.rs` show the distinction:

```rust,ignore
#[cfg(test)]
extern crate std;

#[cfg(any(test, feature = "alloc", feature = "hinting"))]
extern crate alloc;
```

An attribute beginning with `cfg` conditionally includes the following item. Unit tests can use `std`; the listed conditions make `alloc` available. The UI's `alloc` feature exposes conveniences including heap storage and owned text. Its hinting support also needs allocation facilities. Meanwhile, the application explicitly enables the UI's `alloc` feature and uses heap-backed document data. “The UI can run without a heap” is a capability of the framework, not a description of the entire reader application.

There is a second choice beyond features. Each `Runtime<S>` selects storage through its type parameter `S`: fixed-capacity tables, or heap-backed tables when available. Enabling `alloc` does not force every runtime to use heap tables. A fixed table can coexist with heap-using application code; it bounds that table, not everything the application owns. We will inspect this distinction closely in chapter 4.

Cargo features are generally additive within a resolved dependency graph. A dependency can enable a feature even if the top-level package does not. In particular, disabling the simulator's default feature selects its fixed runtime configuration, but does not magically remove allocation from its application dependency. Following the dependency edges is more reliable than inferring memory behavior from one command-line switch.

## Try it: follow the first visible frame

Optional, after reading: launch the simulator with the command above, enter `/Fixtures`, and open an EPUB. The observable result should be actual book content, not just a filename. Turn a page with the right arrow, then press `H` to return home.

If SDL is unavailable, take a read-only route instead. Open `crates/sim/src/main.rs` at `main`: follow `new_runtime`, `register_resources`, `create_root`, and the first `render_ui` call. Then read `render_ui` in `crates/sim/src/render.rs`. You should be able to point to three distinct moments: application state is created, a frame is prepared, and damaged pixels are painted. Neither route requires hardware. That distinction between state, representation, and output will carry the next several chapters.
