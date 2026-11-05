# 1. The project, the workspace and the toolbox

> **Where we are:** the whole repository, as it stands today. Before following the
> history, we take a tour of the finished building so we know where each room is.

## The hardware we are writing for

The XTEINK X4 PRO is a pocket e-reader. For a programmer, what matters is this:

- an **ESP32-S3** microcontroller: two Xtensa cores at 240 MHz, about 512 KB of
  internal RAM, plus a few megabytes of slower external PSRAM;
- an **800×480 e-paper panel** that we hold in portrait as 480×800. It can show
  four grey levels and takes hundreds of milliseconds to refresh;
- a **GT911** capacitive touch controller, a **CW2017** battery gauge and a
  **BM8563** real-time clock, all on one I²C bus;
- two side buttons, a power button, a two-channel frontlight, an SD card slot and
  USB.

Compared with a laptop, memory is small, the display is slow and there is no
operating system. These three facts explain most of the design decisions in this
book.

## The README's three goals

The README states the project's goals:

> - keep hardware-specific code separate from application logic
> - keep the core firmware compatible with `no_std`
> - make device independent code testable on a host machine

Keep them in mind. Most of the structure we are about to see exists to serve them.

## A Cargo workspace

InkPaper is not one crate but about twenty-five. They live in one **Cargo
workspace**, described by the `Cargo.toml` at the root:

```toml
[workspace]
resolver = "3"
members = ["crates/*", "firmware/*", "tools/*"]
default-members = ["crates/*"]

[workspace.dependencies]
critical-section = "1.2.0"
defmt = "1.1.1"
embedded-graphics = "0.8.2"
embedded-hal = "1.0.0"
embedded-hal-async = "1.0.0"
esp-hal = "1.2.1"
heapless = "0.9.3"
# ...

[profile.firmware]
inherits = "release"
debug = 2
lto = "fat"
codegen-units = 1
```

A few things to notice.

**`members` uses globs.** Every folder under `crates/`, `firmware/` and `tools/`
is a package of the workspace. Adding a crate is a matter of creating a folder.

**`default-members` is narrower than `members`.** When you run `cargo test` or
`cargo build` at the root without `-p`, Cargo only touches `crates/*`. That
matters because `firmware/x4-pro` only makes sense for the Xtensa target, and
`tools/xtask` pulls in heavy desktop dependencies. Day-to-day commands stay fast
and work on any machine.

**`[workspace.dependencies]` pins versions in one place.** A member crate
then writes:

```toml
[dependencies]
heapless = { workspace = true }
defmt = { workspace = true, optional = true }
```

Every crate that uses `heapless` gets the same version, and upgrading means
editing one line.

**`resolver = "3"`** selects the newest feature resolver. You can ignore it for
now. It mostly changes how features are unified between build-dependencies and
normal ones, and how `rust-version` is respected.

**Custom profiles.** `[profile.firmware]` *inherits* from `release` and turns on
fat link-time optimisation and a single codegen unit. That makes compilation
slower and the binary smaller and faster, which matters on a microcontroller.
`debug = 2` keeps full debug info. It doesn't end up on the device, but
`probe-rs` uses it to print readable stack traces. You select a profile with
`--profile firmware`.

## The crates and their layers

Here is the map, from the bottom up.

| Layer | Crates | `no_std`? |
|-------|--------|-----------|
| Chip drivers | `cw2017`, `bm8563`, `gt911`, `ssd1677`, `uc8179`, `uc8279-x4`, `frontlight`, `sntp`, `epd-bus`, `xteink-display-probe` | yes |
| UI framework | `ui` (`inkpaper-ui`), `ui-eink`, `ui-macros`, `ui-style-schema` | yes (macros run on the host) |
| Documents | `epub`, `reader`, `dictionary` | yes, with `alloc` |
| Application | `app` (`inkpaper-app`) | yes, with `alloc` |
| Diagnostics | `trace`, `trace-macros` | yes |
| Platforms | `sim` (desktop), `firmware/x4-pro` (device) | sim: no, firmware: yes |
| Tooling | `tools/xtask` | no |

The important arrow is that **`app` depends on `ui`, `epub`, `reader` and
`dictionary`, but not on any driver.** Only the two *platform* crates, the
simulator and the firmware, know about real hardware or about SDL windows. Both
of them create the same `InkPaperApp` and feed it the same input events. That is
how the README's first and third goals are met: the app is tested and run on a
laptop, and the firmware is a thin layer that wires real chips to it.

You can check this yourself. Open `crates/app/Cargo.toml`:

```toml
[dependencies]
inkpaper-dictionary = { path = "../dictionary" }
inkpaper-epub = { path = "../epub" }
inkpaper-reader = { path = "../reader" }
inkpaper-trace = { path = "../trace" }
inkpaper-ui = { path = "../ui", features = ["alloc"] }
minicbor = { version = "2.3.0", default-features = false, features = ["alloc", "derive"] }
```

No `esp-hal`, no `embedded-hal`, no SDL. Rust's crate graph enforces the
architecture. If someone tries to call a driver from the app, the code fails to
compile, so nobody has to catch it in review.

## `no_std`: Rust without an operating system

Almost every library crate starts with this line:

```rust
#![no_std]
```

That attribute tells the compiler not to link the **standard library**, `std`.
`std` assumes an operating system: files, threads, a heap, a clock, sockets. On the
ESP32-S3 there is none of that. What remains are two smaller libraries:

- **`core`**: always available. Contains `Option`, `Result`, iterators, slices,
  `str`, `char`, `fmt`, `cell`, `mem`, `ptr`, integer and float types, traits like
  `Clone`, `Iterator` and `Add`. No allocation, no I/O.
- **`alloc`**: available if you provide a global allocator. Contains `Vec`,
  `String`, `Box`, `Rc`, `Arc`, `BTreeMap`, `Cow`.

A crate that needs heap collections opts into `alloc` explicitly:

```rust
#![no_std]

extern crate alloc;

use alloc::vec::Vec;
```

That is exactly how `crates/epub/src/lib.rs` begins. The UI crate is more
careful. It only pulls in `alloc` when asked to:

```rust
#![no_std]

#[cfg(test)]
extern crate std;

#[cfg(any(test, feature = "alloc", feature = "hinting"))]
extern crate alloc;
```

Read those `#[cfg(...)]` attributes as *conditional compilation*. The line below
the attribute exists only when the condition holds. In tests, the crate links
`std` so the test harness can print and allocate. In normal builds it links
`alloc` only if the `alloc` or `hinting` feature is on. This is how
`inkpaper-ui` can promise to run "without heap allocation" and still offer
convenient heap-backed storage to those who want it.

## Features: optional pieces of a crate

**Features** are named switches declared in a crate's `Cargo.toml`. Here is the
UI crate's list:

```toml
[features]
default = ["ttf", "variable-fonts", "macros"]
alloc = []
macros = ["dep:inkpaper-ui-macros"]
defmt = ["dep:defmt", "heapless/defmt"]
embedded-graphics = ["dep:embedded-graphics", "embedded-graphics/defmt"]
ttf = ["dep:ttf-parser"]
variable-fonts = ["ttf", "ttf-parser/variable-fonts"]
hinting = ["ttf", "dep:skrifa", "dep:spin"]
metrics = []
```

There are three kinds of entries here:

- `alloc = []` is a pure flag. It turns on no dependency, and code tests it
  with `#[cfg(feature = "alloc")]`.
- `ttf = ["dep:ttf-parser"]` turns on an **optional dependency**. The `dep:`
  prefix means "the dependency called `ttf-parser`", as opposed to a feature
  of that name.
- `defmt = ["dep:defmt", "heapless/defmt"]` also turns on a feature *in another
  crate*, so that `heapless` types can be logged with `defmt` too.

Features are **additive**: if two crates in the build graph enable different
features of `inkpaper-ui`, Cargo builds it once with the union of both. So a
feature must never *remove* functionality, and code behind
`#[cfg(not(feature = "x"))]` has to keep working when someone else turns `x` on.
The `justfile` checks this explicitly:

```make
ui-features:
    cargo check -p inkpaper-ui --lib --no-default-features
    cargo check -p inkpaper-ui --lib --no-default-features --features embedded-graphics
    cargo check -p inkpaper-ui --lib --no-default-features --features alloc,metrics
    cargo check -p inkpaper-ui --tests --examples --benches --no-default-features
    cargo check -p inkpaper-ui --tests --examples --benches --all-features
```

This habit is worth copying. A feature combination nobody compiles will
eventually stop compiling.

## Two targets, one source tree

The firmware's `main.rs` is only fourteen lines:

```rust
#![cfg_attr(target_arch = "xtensa", no_std)]
#![cfg_attr(target_arch = "xtensa", no_main)]

#[cfg(target_arch = "xtensa")]
extern crate alloc;

#[cfg(target_arch = "xtensa")]
mod firmware;

#[cfg(not(target_arch = "xtensa"))]
fn main() {}
```

`cfg_attr(condition, attribute)` applies `attribute` only if `condition` holds.
On the Xtensa target the crate is `no_std` and `no_main`. There is no ordinary
`main` because the hardware abstraction layer provides the real entry point.
On any other target, such as your laptop when you run `cargo check
--workspace`, the crate compiles to an empty program. This trick keeps
workspace-wide commands like `cargo clippy --workspace` from failing on a
machine without the Xtensa toolchain.

## Toolchains

Xtensa is not an official Rust target yet, so it needs a forked compiler. The
`setup-toolchain.sh` script installs one with `espup` under a custom name:

```bash
TOOLCHAIN="inkpaper-esp"
VERSION="1.97.0.0"

espup install \
    --name "${TOOLCHAIN}" \
    --toolchain-version "${VERSION}" \
    --targets esp32s3
```

You then choose it per command with `cargo +inkpaper-esp ...`. Your normal
stable toolchain keeps building everything else.

The repository's `.cargo/config.toml` holds the target-specific settings:

```toml
[target.xtensa-esp32s3-none-elf]
runner = ["probe-rs", "run", "--chip=esp32s3", "--preverify", ...]

rustflags = [
    "-C", "link-arg=-nostartfiles",
    "-C", "link-arg=-Wl,-Tlinkall.x",
    "-C", "link-arg=-Tdefmt.x"
]

[alias]
xtask = "run -p xtask --"

[unstable]
build-std = ["core", "alloc"]
```

- The **runner** makes `cargo run` flash the chip and stream logs back over the
  debug probe instead of executing a local binary.
- The **rustflags** pass linker scripts that place code and data in the right
  memory regions of the chip.
- **`build-std`** asks Cargo to compile `core` and `alloc` from source for the
  target, because no precompiled versions exist for it.
- The **alias** turns `cargo xtask fixtures` into `cargo run -p xtask --
  fixtures`. This is the "xtask" pattern: project automation written in Rust
  instead of shell scripts. We meet it again in chapter 20.

## The `justfile`

[`just`](https://github.com/casey/just) is a command runner. The project's
`justfile` gives names to long commands:

```make
test: ui-features
    cargo nextest run
    cargo nextest run -p inkpaper-ui --all-features
    # without alloc, the unit tests run against fixed storage
    cargo nextest run -p inkpaper-ui

sim:
    cargo run -p {{simulator_package}}

x4-run:
    cargo +inkpaper-esp run -p {{firmware_package}} --target {{x4_pro_target}} --profile firmware
```

Look at `test`. The UI crate's tests run twice, once with all features and once
without. The comment explains why: without `alloc`, the internal type
`TestStorage` resolves to fixed-capacity storage, so the same tests exercise both
storage backends. We will see this switch in chapter 4.

## Edition 2024 and lints

Every crate declares `edition = "2024"`. Editions let Rust make small
backwards-incompatible changes without splitting the ecosystem. Crates on
different editions link together fine. Edition 2024 brings, among other things,
`let` chains (`if let Some(x) = a && x > 3 { ... }`), which InkPaper uses, and
stricter rules for `unsafe` blocks inside `unsafe fn`.

The UI crate also turns on some extra Clippy lints:

```toml
[lints.clippy]
# casts that silently change a value. Convert with `try_from`, round floats through
# `float`, or `#[expect]` the lint with the reason the value fits
cast_possible_truncation = "warn"
cast_sign_loss = "warn"
cast_possible_wrap = "warn"
```

In Rust, `x as u8` never fails. If `x` is 300, you silently get 44. In a layout
engine full of pixel arithmetic, that kind of bug is miserable to track down.
These lints flag every such cast, and the codebase answers each one in one of
two ways. It uses `u8::try_from(x)`, which returns a `Result`, or it adds an
explicit `#[expect(clippy::cast_possible_truncation, reason = "16 samples")]`
that documents why the cast is safe. You will see both patterns again.

## Rust concepts in this chapter

- **Workspaces**: `members`, `default-members`, `[workspace.dependencies]`,
  custom profiles with `inherits`.
- **`no_std`**: the split between `core`, `alloc` and `std`, and `extern crate`.
- **Conditional compilation**: `#[cfg(...)]`, `#[cfg_attr(...)]`, `target_arch`.
- **Features**: flags, optional dependencies with `dep:`, enabling features of
  dependencies, additivity.
- **Toolchains and targets**: `cargo +toolchain`, `build-std`, runners, aliases.
- **Lints**: `[lints.clippy]`, `#[expect(lint, reason = "...")]`.

## Try it yourself

1. Run `cargo tree -p inkpaper-app --depth 1` and confirm that no driver crate
   appears.
2. Run `cargo check -p inkpaper-ui --no-default-features`. Then open
   `crates/ui/src/lib.rs` and find two items that disappear in that build.
3. In a scratch crate, write `let x: i32 = 300; let y = x as u8;` and print `y`.
   Then add `#![warn(clippy::cast_possible_truncation)]` and run `cargo clippy`.
