# Appendix B: A small reading lab

A useful learning session ends with a prediction you can check. “This trait
separates drawing from the device” is an explanation; “changing the device should
not change the layout calculation” is something you can investigate. The second
turns reading into understanding.

You do not need to implement a second e-reader alongside this one. Keep the
working repository available, follow one question through a few files, and make
small experiments when they will resolve something you do not yet understand.

## Get to a real page

Run commands from the repository root. Install Rust through
[rustup](https://rustup.rs/) if needed. The desktop simulator also requires the
platform-specific SDL2 setup described by
[`embedded-graphics-simulator`](https://github.com/embedded-graphics/simulator#setup).
This native library is separate from Cargo's Rust dependencies.

```bash
cargo run -p inkpaper-simulator
```

In the file browser, enter `/Fixtures`, then open `typography.epub`. The simulator
maps the repository's `fixtures/` directory to that location. Its other
demonstration listings are not a library of bundled novels. A missing book from
one of those listings tells you nothing about whether the EPUB parser works.

The mouse's left button supplies touch input; holding it still can produce a
long press. Left and right arrow keys send previous and next actions. `H` sends
the home action. These mappings live in `crates/sim/src/main.rs`, which is also a
useful compact example of translating platform events into application intent.

If the build fails while linking SDL2, return to the simulator's setup guide.
If the window opens but `/Fixtures` is empty, check that the repository includes
the EPUB files in `fixtures/`. The fixture path is anchored to the simulator
crate's manifest directory, not to whichever folder happens to contain the
executable.

For a window-free starting point, exercise the document parser:

```bash
cargo test -p inkpaper-epub
```

That checks parser behavior on the host. It does not prove that a page looks
right, that a particular book is supported, or that a physical panel refreshes
correctly. Each form of evidence answers a different question.

## Read along one path, not across the whole workspace

Imagine you want to understand a page turn. Start at the input mapping, follow
the application action, and stop when you reach a request for reader work. That
is enough for a session about interaction. In another session, follow the request
through pagination. You do not need to inspect the font rasterizer just because
the call path eventually reaches it.

The workspace makes these boundaries visible:

| Your question | A useful starting place |
|---|---|
| How does mouse input become an application action? | `crates/sim/src/main.rs` |
| How is the simulated card populated? | `crates/sim/src/fake_fs.rs` |
| How does a frame reach the desktop display? | `crates/sim/src/render.rs` |
| How are UI state and rendering represented? | `crates/ui/src/` |
| How is an EPUB interpreted? | `crates/epub/src/` |
| How does the application coordinate the reading experience? | `crates/app/src/` |
| Where does software meet the board? | `firmware/x4-pro/src/` |

Use your editor's “go to definition” to follow one symbol, and return to the
caller after understanding its contract. For a trait, ask what the caller can
assume. For an enum, ask which cases the caller must handle. For a borrowed
value, ask who owns the storage and what operation would invalidate it. These
questions are more useful than trying to remember a module's entire contents.

## Make the experiment answer a question

Before changing anything, write a sentence with an expected observation. For
example: “If the same paragraph has less horizontal space, it should wrap into
more lines, but its text should stay unchanged.” Then choose a fixture and
change only the relevant setting. Compare the result with your prediction.

There are three equally useful forms of experiment. A simulator interaction
shows visible behavior. A focused test checks an invariant that is difficult to
see, such as rejecting an invalid handle. A paper trace follows a few values
through an algorithm when running hardware would obscure the idea. The chapters
say which kind they use.

When an exercise names a test filter, Cargo should report at least one matching
test. A successful command that ran zero tests is not evidence for the claim.
Keep expected output at the level of behavior: a rejected malformed path, an
unchanged reading position, or a bounded damage region. Exact timings and total
test counts are not portable promises.

Short excerpts in this book omit surrounding imports or implementations unless
they are explicitly presented as complete examples. Study them beside their
source file. A labelled sketch explains a model; it should not be mistaken for
a second public API that you need to add to the project.

## Choose the next question

The main chapter order takes you from describing a screen to delivering a page.
If you return with a specific curiosity, use a shorter route. For layout and
rendering, revisit chapters 6–9. For physical I/O and waiting, use chapters
11–14. For the journey from document bytes to readable lines, use chapters
15–18. Chapter 4's storage internals are worth revisiting after chapter 5 gives
their type erasure a second concrete use.

When you have finished the book, choose one small product requirement rather
than a language feature: a clearer empty-library screen, a new way to inspect a
reading position, or a visible indication that work is pending. Describe the
observable behavior first. Find the existing boundary that owns it. Only then
decide which Rust tools it needs. That is the habit the chapters are meant to
leave you with.
