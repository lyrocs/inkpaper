# 12. E-paper displays: SPI, busy pins and timeouts

> **Where we are:** 21 August 2026, a day of seven display commits: `cd7e070`
> "add ssd1677 driver", `0431a08` "add xteink display controller probe",
> `4b54268` "support streamed e-paper transactions", `e6e4c86` "add uc8179
> driver", `ce32802` "add uc8279-x4 driver", plus framing fixes. Grayscale came
> on 14 September (`7a171de` → `74c01fa`). Later commits made the bus interrupt
> driven (`34911f3`, 13 October) and moved shared code into `epd-bus` (`5fa8c5f`).

## How you talk to an e-paper controller

An e-paper panel is driven by a controller chip that holds the image in its own
RAM and generates the voltage waveforms that move the ink particles. The
microcontroller talks to it over SPI using four signals besides the clock and
data lines:

- **CS** (chip select): low while a transfer is addressed to this chip.
- **DC** (data/command): low means the byte is a *command*, high means it's
  *data* for the last command.
- **RESET**: a pulse resets the controller.
- **BUSY**: an *input* the controller drives while it's working (a refresh can
  take seconds).

A typical exchange is "command `0x24` (write RAM), then 48,000 bytes of pixel
data, then command `0x22` + `0x20` (update the display), then wait for BUSY to
clear".

## Three controllers, one board

X4 devices can carry **three different display controllers** (SSD1677, UC8179,
UC8279), and the firmware has no reliable way to know in advance which one a given
device has. They speak different command sets. So the firmware must *detect* the
controller before sending a single initialisation command. As a comment in the
firmware puts it, an SSD1677 initialisation sequence must never be sent to
"potential UC silicon".

That gave the project a nice structure:

- `epd-bus`: what all controllers share, the four-wire protocol.
- `ssd1677`, `uc8179`, `uc8279-x4`: one driver crate per controller.
- `xteink-display-probe`: detection.
- The firmware: an enum that picks the right driver at runtime.

## The bus trait

```rust
#[allow(async_fn_in_trait)]
pub trait EpdInterface {
    type Error;

    async fn command(&mut self, command: u8) -> Result<(), Self::Error>;
    async fn data(&mut self, data: &[u8]) -> Result<(), Self::Error>;
    async fn command_data(&mut self, command: u8, data: &[u8]) -> Result<(), Self::Error>;

    async fn reset<D>(&mut self, delay: &mut D) -> Result<(), Self::Error>
    where
        D: DelayNs;

    fn is_busy(&mut self, polarity: BusyPolarity) -> Result<bool, Self::Error>;

    async fn wait_busy<D>(
        &mut self,
        polarity: BusyPolarity,
        delay: &mut D,
    ) -> Result<(), Self::Error>
    where
        D: DelayNs;

    /// begin a command + streamed-data transaction.
    ///
    /// CS remains asserted until `end_stream()`.
    async fn begin_data_stream(&mut self) -> Result<(), Self::Error>;

    /// write another chunk of data while the stream remains selected.
    async fn stream_data(&mut self, data: &[u8]) -> Result<(), Self::Error>;

    /// finish a command/data stream and deassert CS.
    fn end_data_stream(&mut self) -> Result<(), Self::Error>;
}
```

### `async fn` in traits

Since Rust 1.75 you can write `async fn` directly in a trait. The
`#[allow(async_fn_in_trait)]` silences a lint that warns *library* authors: the
futures these methods return have no `Send` bound, so they can't be moved
between threads. On a single-core executor like the firmware's, that doesn't
matter. The lint exists to make you decide, and InkPaper decided. The same
attribute appears on `EpubSource` and `AppPlatform` later in the book.

Some methods are `async` and some aren't. `is_busy` reads a pin, which is
instantaneous. `end_data_stream` only raises CS. Making them async would force
`.await` on callers for nothing. The trait mirrors the real timing of each
operation.

## One implementation, five type parameters

```rust
pub struct SpiEpdBus<SPI, CS, DC, RESET, BUSY> {
    spi: SPI,
    cs: CS,
    dc: DC,
    reset: RESET,
    busy: BUSY,
    stream_open: bool,
}

impl<SPI, CS, DC, RESET, BUSY> SpiEpdBus<SPI, CS, DC, RESET, BUSY>
where
    SPI: SpiBus<u8>,
    CS: OutputPin,
    DC: OutputPin,
    RESET: OutputPin,
    BUSY: InputPin + Wait,
{
    pub fn new(
        spi: SPI,
        mut cs: CS,
        mut dc: DC,
        mut reset: RESET,
        busy: BUSY,
    ) -> Result<Self, <Self as EpdInterface>::Error> {
        cs.set_high().map_err(Error::ChipSelect)?;
        dc.set_high().map_err(Error::DataCommand)?;
        reset.set_high().map_err(Error::Reset)?;

        Ok(Self { spi, cs, dc, reset, busy, stream_open: false })
    }
}
```

Each pin is its own type parameter because in `esp-hal` (and most HALs) each GPIO
can be a distinct type, and their errors can differ. So the error enum has five
parameters too:

```rust
#[derive(Debug)]
pub enum Error<Spi, Cs, Dc, Reset, Busy> {
    Spi(Spi),
    ChipSelect(Cs),
    DataCommand(Dc),
    Reset(Reset),
    Busy(Busy),
    BusyTimeout,
    Protocol(ProtocolError),
}
```

It looks heavy, but nobody has to spell it out. The constructor's return type is
written `<Self as EpdInterface>::Error`, a **fully qualified path** to the
associated type: "whatever error type my `EpdInterface` impl declares". That
keeps the long type in exactly one place.

`new` puts every output in a known idle state (CS high means "not selected").
Initialising hardware state in the constructor means a `SpiEpdBus` value can't
exist in an undefined electrical state.

### Getting errors right when two things fail

```rust
fn finish_transaction(
    &mut self,
    spi_result: Result<(), SPI::Error>,
) -> Result<(), <Self as EpdInterface>::Error> {
    let cs_result = self.cs.set_high().map_err(Error::ChipSelect);

    match spi_result {
        Err(error) => {
            let _ = cs_result;
            Err(Error::Spi(error))
        }
        Ok(()) => cs_result,
    }
}
```

When an SPI write fails, CS must *still* be released, or the bus stays stuck
selected. So the function always raises CS first, *then* decides which error to
report: the SPI error is the root cause and wins, and a CS error is reported
only when the SPI write succeeded. `let _ = cs_result;` explicitly discards a
`Result`. `Result` is marked `#[must_use]`, so silently ignoring one triggers a
warning, and `let _ =` says "I thought about this".

## Protocol state checked at runtime

Large images are streamed in chunks while CS stays low. Starting a stream twice,
or streaming without starting, would corrupt the transfer. A boolean tracks it:

```rust
async fn stream_data(&mut self, data: &[u8]) -> Result<(), Self::Error> {
    if !self.stream_open {
        return Err(Error::Protocol(ProtocolError::StreamNotOpen));
    }

    if let Err(error) = self.spi.write(data).await {
        self.stream_open = false;
        let _ = self.cs.set_high();

        return Err(Error::Spi(error));
    }

    Ok(())
}
```

A more advanced design would encode "stream open" in the *type*: `begin_data_stream`
would consume the bus and return a `DataStream<'_>` guard with a `write` method,
and dropping the guard would end the stream. That's the **typestate pattern**, and
it would make `StreamNotOpen` impossible at compile time. The project chose a
runtime flag, which keeps the trait simple and object-friendly. Both are
reasonable, and it's worth knowing the option exists.

## Waiting for BUSY, with a timeout

The first version waited by polling: check the pin, sleep a millisecond, repeat.
Commit `34911f3`, "wait on the epd busy pin by interrupt with a timeout", replaced
that:

```rust
/// longest a BUSY wait may last before the panel is treated as stuck.
///
/// a cold full refresh takes a few seconds; this matches crosspoint's bound
const BUSY_TIMEOUT_MS: u32 = 30_000;

async fn wait_busy<D>(
    &mut self,
    polarity: BusyPolarity,
    delay: &mut D,
) -> Result<(), Self::Error>
where
    D: DelayNs,
{
    let idle = async {
        match polarity {
            BusyPolarity::ActiveHigh => self.busy.wait_for_low().await,
            BusyPolarity::ActiveLow => self.busy.wait_for_high().await,
        }
    };

    match select(idle, delay.delay_ms(BUSY_TIMEOUT_MS)).await {
        Either::First(result) => result.map_err(Error::Busy),
        Either::Second(()) => Err(Error::BusyTimeout),
    }
}
```

This is one of the most useful async patterns, so let's go slowly.

- `wait_for_low()` comes from the `embedded_hal_async::digital::Wait` trait.
  The HAL implements it with a **pin interrupt**: the task sleeps, the CPU can
  idle, and the interrupt wakes the task when the pin changes. No polling.
- `async { ... }` is an **async block**: an anonymous future, built in place,
  that borrows `self.busy`.
- `select(a, b)` from `embassy-futures` polls both futures and completes when
  **the first one** does, returning `Either::First` or `Either::Second`. The
  other future is **dropped**, which in async Rust *cancels* it.
- So the whole thing reads: "wait for the pin, or 30 seconds, whichever comes
  first."

**Every wait on hardware should have a timeout.** A disconnected flex cable or a
crashed controller would otherwise hang the device forever. The constant's
comment records where the number came from (crosspoint, another open-source
firmware for these readers), which saves the next person from wondering whether
30 seconds was a guess.

## Probing the controller

How do you tell three chips apart before talking to any of them? The probe crate
**bit-bangs** the bus, toggling pins by hand, to send each family's "read
version" command and listen for an answer. It works against a small trait of
pin operations:

```rust
pub trait ProbeIo {
    type Error;

    fn cs_high(&mut self) -> Result<(), Self::Error>;
    fn cs_low(&mut self) -> Result<(), Self::Error>;
    fn clock_high(&mut self) -> Result<(), Self::Error>;
    fn clock_low(&mut self) -> Result<(), Self::Error>;
    fn data_output(&mut self) -> Result<(), Self::Error>;
    fn data_input_pullup(&mut self) -> Result<(), Self::Error>;
    fn data_is_high(&mut self) -> Result<bool, Self::Error>;
    // ...
}
```

The firmware implements it with `esp_hal::gpio::Flex` pins, which can switch
between input and output, and that's how a single data line can both send and
receive. Because the probe only depends on this trait, its decision logic can be
tested on a laptop with a fake that plays back recorded answers.

The result isn't a boolean. It's a verdict with three outcomes:

```rust
pub enum Verdict {
    /// neither probe pass found a UC81xx response.
    ///
    /// on X4/X4 Pro the conservative primary controller is SSD1677.
    PrimaryAssumed,
    /// two probe passes confirmed a UC81xx-family controller.
    Confirmed,
    /// the probe observed something inconsistent.
    ///
    /// callers should conservatively use the primary controller and avoid
    /// persisting the result.
    Inconclusive,
}
```

Real hardware is noisy. A floating data line with a pull-up reads `0xff`, and so
do some genuine chips. The probe runs **two passes** and requires them to agree,
and a comment documents a quirk seen by another project (FreeInk) along with
the extra check that handles it. The firmware treats `Inconclusive` as fatal:

```rust
// an inconclusive result means something on the desplay bus responded, but not
// consistently enough to identify it safely. Do not send an SSD1677 initialization
// sequence to potential UC silicon
if detection.verdict == Verdict::Inconclusive {
    error!("display probe inconclusive, refusing to drive panel");
    stay_alive().await;
}
```

The probe also cleans up carefully. It must release the pins even when probing
fails, so the real SPI peripheral can take them afterwards:

```rust
pub async fn detect_x4_controller<IO, D>(
    io: &mut IO,
    delay: &mut D,
) -> Result<ProbeResult, Error<IO::Error>>
where
    IO: ProbeIo,
    D: DelayNs,
{
    let result = detect_x4_controller_inner(io, delay).await;
    let release_result = io.release().map_err(Error::Io);

    match (result, release_result) {
        (Err(error), _) => Err(error),
        (Ok(_), Err(error)) => Err(error),
        (Ok(result), Ok(())) => Ok(result),
    }
}
```

It's the same "always clean up, report the root cause" rule as
`finish_transaction`, expressed as a `match` over a tuple of two `Result`s. The
"inner function" split is a common idiom: the inner function uses `?` freely, and
the outer one guarantees cleanup no matter how the inner one exited.

## Choosing a driver at runtime: enum dispatch

The firmware wraps the three drivers in an enum:

```rust
pub enum X4Panel {
    Ssd1677(Ssd1677),
    Uc8179(Uc8179),
    Uc8279(Uc8279X4),
}

impl X4Panel {
    pub const fn new(controller: Controller) -> Self {
        match controller {
            Controller::Ssd1677 => Self::Ssd1677(Ssd1677::new(GDEQ0426T82)),
            Controller::Uc8179 => Self::Uc8179(Uc8179::new(UC8179_X4_PRO)),
            Controller::Uc8279 => Self::Uc8279(Uc8279X4::new(UC8279_X4_PRO)),
        }
    }

    pub async fn initialize<B, D>(
        &mut self,
        bus: &mut B,
        delay: &mut D,
    ) -> Result<(), Error<B::Error>>
    where
        B: EpdInterface,
        D: DelayNs,
    {
        match self {
            X4Panel::Ssd1677(panel) => panel.initialize(bus, delay).await.map_err(Error::Ssd1677),
            X4Panel::Uc8179(panel) => panel.initialize(bus, delay).await.map_err(Error::Uc8179),
            X4Panel::Uc8279(panel) => panel.initialize(bus, delay).await.map_err(Error::Uc8279),
        }
    }
}
```

Why an enum and not `Box<dyn Panel>`? Several reasons:

- The drivers' methods are `async` and generic (`<B, D>`), which makes a trait
  object awkward or impossible: generic methods can't go in a vtable.
- The set of controllers is **closed and known**, the case where chapter 6 said
  enums shine.
- No heap, and the compiler can inline each arm.

Methods that only one controller supports use `let ... else` to return early for
the others:

```rust
pub async fn begin_idle_power_off<B, D>(&mut self, bus: &mut B, delay: &mut D)
    -> Result<(), Error<B::Error>>
where
    B: EpdInterface,
    D: DelayNs,
{
    let Self::Uc8179(panel) = self else {
        return Ok(());
    };
    // ...
}
```

The UC8179 can be kept powered between page turns (`3e4bf3e`, "keep uc8179
powered across page bursts"), so quickly flipping several pages skips a slow
power-up each time. That optimisation came from tracing (chapter 20), and this
enum is where it plugs in.

## Four grey levels, two bit-planes

Commit `e5563f3`, "add 4 level x4 framebuffer", made the firmware store two
**bit-planes**. Each pixel has one bit in an LSB plane and one in an MSB plane,
which together encode 4 levels. That's the format the controllers' grayscale mode
expects. The framebuffer is 800×480 physical pixels, stored in rows of 100 bytes,
while the UI draws in 480×800 portrait, so every pixel write goes through an
orientation mapping:

```rust
fn set_pixel(&mut self, x: u16, y: u16, color: Gray2) {
    let (physical_x, physical_y) = self.orientation.map_point(x, y);
    let index = physical_y as usize * PHYSICAL_STRIDE + physical_x as usize / 8;

    let mask = 0x80u8 >> (physical_x as usize % 8);

    let level = color.luma();

    set_mask(&mut self.storage.lsb[index], mask, level & 0b01 != 0);
    set_mask(&mut self.storage.msb[index], mask, level & 0b10 != 0);
}
```

And deciding whether a region needs a (slower) grayscale refresh is a slice
comparison. Wherever the two planes are equal, every pixel is pure black or white:

```rust
/// Whether a 2-bit frame holds gray inside `region`: whether its two planes,
/// `stride` bytes a row, differ there. `region` must be byte-aligned
/// horizontally.
pub fn region_has_grayscale(lsb: &[u8], msb: &[u8], stride: usize, region: Region) -> bool {
    let x_byte = region.x as usize / 8;
    let row_bytes = region.width as usize / 8;

    for row in 0..region.height as usize {
        let start = (region.y as usize + row) * stride + x_byte;
        let end = start + row_bytes;

        if lsb[start..end] != msb[start..end] {
            return true;
        }
    }

    false
}
```

`lsb[start..end] != msb[start..end]` compares two **slices** element by element.
`PartialEq` is implemented for slices, and the compiler turns this into a fast
memory comparison. This is the hardware end of the "binary vs grayscale" refresh
decision from chapter 9.

## Rust concepts in this chapter

- **`async fn` in traits** and the `async_fn_in_trait` lint.
- **Many type parameters** and **fully qualified associated-type paths**
  (`<Self as Trait>::Error`).
- **Constructors that establish invariants** (hardware idle states).
- **Error priority when two operations fail**; `#[must_use]` and `let _ =`.
- **Runtime protocol flags vs the typestate pattern.**
- **Async blocks, `select`, cancellation by drop**, timeouts on every hardware wait.
- **Interrupt-driven waits** with `embedded_hal_async::digital::Wait`.
- **Bit-banging behind a trait** for testable probing.
- **Inner/outer function split** for guaranteed cleanup; matching on
  `(Result, Result)`.
- **Enum dispatch** over a closed set of implementations.
- **Bit-planes**, masks and slice comparison.

## Try it yourself

1. Implement `EpdInterface` for a `RecordingBus` that pushes every command byte
   into a `Vec<u8>` and never reports busy. Run `Ssd1677::initialize` with it and
   print the command sequence. Compare with `crates/ssd1677/src/command.rs`.
2. Write a small async test with `futures_lite::future::block_on` that does
   `select(core::future::pending::<()>(), async {})`. Which side wins? Now
   imagine the BUSY line of a disconnected panel: what would `wait_busy` do
   without the `select`?
3. Sketch a typestate version of streaming: `fn begin(self) -> Streaming<Self>`,
   with `Streaming::write` and `Streaming::end(self) -> Self`. What becomes
   impossible to write?
