# Introduction

This book tells the story of how **InkPaper** was built, and uses that story to
teach Rust.

InkPaper is experimental firmware for the XTEINK X4 PRO, a small e-reader with an
800×480 e-paper panel, a capacitive touch screen, two side buttons, a frontlight,
a battery gauge, a real-time clock, an SD card and an ESP32-S3 microcontroller.
On top of that hardware sits a lot of software: a UI framework, an EPUB parser, a
text shaper, a paginator, a dictionary, device drivers and an async firmware.
All of it is written in Rust, most of it runs without the standard library, and
almost all of it can also run on your laptop in a simulator.

That makes it a very good textbook. Every Rust feature in it is there to solve a
real problem, and you can watch the problem appear in the git history before the
feature shows up to solve it.

## Who this book is for

You should be able to read basic Rust: functions, `struct`, `enum`, `match`,
`impl`, and roughly what a reference is. You do **not** need to know embedded
programming, `unsafe`, async, macros or traits in depth. Those are exactly the
things the book teaches, one chapter at a time, using InkPaper's own code.

If you have read the first half of *The Rust Programming Language*, you are ready.

## How the book is organised

The chapters follow the order the project was actually built. The first commit,
on 7 August 2026, contained a design document and about 300 lines of Rust. Three
months and around 560 commits later it boots on real hardware, opens EPUBs from
an SD card, hyphenates English, shapes Arabic and looks words up in a dictionary.

| Part | What gets built | Main Rust topics |
|------|-----------------|------------------|
| I. Starting from nothing | A declarative UI API, entities and storage | workspaces, `no_std`, traits, generics, `PhantomData`, `unsafe`, closures |
| II. Making pixels | Frame tree, layout, painting, damage, events | enums, newtypes, operator overloading, trait objects, `TypeId` |
| III. Down to the metal | Drivers, the e-paper bus, the firmware, services | `embedded-hal`, `async`/`await`, embassy, statics, channels |
| IV. Books | EPUB, fonts, shaping, pagination | parsing bytes, error handling, Unicode, lifetimes, `Cow` |
| V. Ergonomics and speed | Macros and tracing | `macro_rules!`, procedural macros, `Drop` guards, tooling |

Each chapter is meant to take **15 to 20 minutes** to read. Each one has:

- **Where we are**: the point in the history the chapter covers, with real
  commit hashes you can check out.
- The story: what problem came up and how the code solved it, with excerpts
  from the repository. Excerpts are copied from the source. When one is shortened,
  the text says so, and `// ...` marks what was left out.
- **Rust concepts in this chapter**: what the chapter taught.
- **Try it yourself**: small exercises you can do in the repository.

## Running the code

You will get more out of the book if you keep the repository open next to it.

```bash
git clone https://github.com/lyrocs/inkpaper
cd inkpaper
cargo test -p inkpaper-ui        # the UI framework's tests
cargo run -p inkpaper-simulator  # the whole app in a desktop window
```

The simulator needs SDL2 (see the
[`embedded-graphics-simulator`](https://crates.io/crates/embedded-graphics-simulator)
instructions). Building the firmware needs the Xtensa toolchain, but no chapter
requires it.

[Appendix B](appendix-git.md) shows how to check out the historical commits
the chapters mention, so you can see the code as it was at each step.

## A note on style

InkPaper's code has a recognisable voice. Comments are short, lowercase, and say
*why* rather than *what*. Commit messages describe behaviour ("wait on the GT911
interrupt instead of polling while idle") rather than files. Errors are values,
not panics, until the last moment. Memory limits are written down as numbers.
These habits are part of what this book tries to teach, so we point them out
along the way.

Let's start where the project started: an empty workspace.
