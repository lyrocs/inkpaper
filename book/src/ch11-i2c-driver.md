# 11. Talking to chips: an embedded-hal driver

> **Where we are:** 21 August 2026. After two weeks of UI work, the project turns
> to the hardware. Commit `2f7f212` "add cw2017 crate" (the battery gauge) is the
> first driver; `0555a14` adds its profile initialisation, and `31dcd86` (24
> August) adds the `bm8563` real-time clock driver.

## Two worlds, one interface

A microcontroller talks to the chips on its board over a few standard buses. The
two most common are:

- **I²C**: two wires (clock and data), many chips on the same pair, each with a
  7-bit address. Slow (100–400 kHz) but simple. On the X4 PRO, the battery gauge
  (address `0x63`), the clock (`0x51`) and the touch controller share one I²C bus.
- **SPI**: four or more wires, faster (tens of MHz), one *chip select* line per
  device. The display uses SPI (chapter 12).

Every microcontroller family has its own way to drive these buses, so a driver
written for the ESP32's I²C peripheral wouldn't work on an STM32 or an nRF52. The
Rust embedded ecosystem solves this with **`embedded-hal`**, a crate of traits
that describe buses and pins abstractly. Chip makers' HAL crates (like
`esp-hal`) *implement* the traits, and drivers are written *against* the
traits. So any driver works with any microcontroller.

This chapter reads one complete driver: the CW2017 battery fuel gauge, in
`crates/cw2017`.

## The crate

```toml
[package]
name = "cw2017"
version = "0.1.0"
edition = "2024"

[features]
default = []
defmt = ["dep:defmt", "embedded-hal-async/defmt-03"]

[dependencies]
bitfield-struct = "0.13.0"
defmt = { workspace = true, optional = true }
embedded-hal-async = { workspace = true }
```

Note what's missing: no `esp-hal`, nothing specific to the X4 PRO. This crate
could be published on crates.io and used on any board with a CW2017. The
`defmt` feature (an efficient logging framework for microcontrollers) is optional,
so users who don't use defmt don't pay for it.

## A driver owns its bus

```rust
/// async driver for the `CellWize CW2017` fuel gauge.
pub struct Cw2017<I2C> {
    i2c: I2C,
}

impl<I2C> Cw2017<I2C> {
    pub const fn new(i2c: I2C) -> Self {
        Self { i2c }
    }

    pub fn release(self) -> I2C {
        self.i2c
    }
}
```

This is the standard **embedded-hal driver shape**:

- The driver is **generic over the bus type**. `Cw2017<I2C>` works with any
  `I2C`.
- `new` **takes ownership** of the bus. No other code can use that bus handle
  behind the driver's back.
- `release` **gives it back**, consuming the driver (`self` by value). That lets
  the firmware reclaim the bus, for example before deep sleep.

Ownership doubles as an access-control system here. If you hold a `Cw2017`, you
hold the right to talk to the chip, and the compiler guarantees nobody else does.
How several drivers share one physical bus is a firmware question, answered in
chapter 13 with a shared-bus wrapper that itself implements the `I2c` trait.

Notice that `new` and `release` have **no trait bound** on `I2C`. Bounds go only
where they're needed: the second `impl` block adds `where I2C: I2c` for the
methods that actually talk on the bus. Splitting impl blocks by bounds keeps
simple methods usable in more contexts.

## Async methods

```rust
impl<I2C> Cw2017<I2C>
where
    I2C: I2c,
{
    /// read the battery terminal voltage
    pub async fn voltage(&mut self) -> Result<Voltage, Error<I2C::Error>> {
        let raw = self.read_u16(Register::Vcell).await?;
        let register = VcellRegister::from(raw);

        Ok(Voltage::from_register(register))
    }

    /// read battery state of charge
    pub async fn state_of_charge(&mut self) -> Result<StateOfCharge, Error<I2C::Error>> {
        let raw = self.read_u16(Register::StateOfCharge).await?;
        let register = SocRegister::from(raw);

        Ok(StateOfCharge::from_register(register))
    }
    // ...
}
```

`I2c` here comes from **`embedded-hal-async`**, the async version of the traits.
An `async fn` doesn't run when called. It returns a **future**, a value that
an executor *polls* until it completes. While the I²C transfer is in progress,
the future reports "not ready", and the executor runs other tasks: the button
task, the touch task, the display. Async is how one microcontroller core juggles
a dozen devices without threads and without busy-waiting. Chapter 13 introduces
the executor that drives these futures.

From the driver's point of view, async costs almost nothing: write `async fn`,
add `.await` after each bus operation, and the code reads like blocking code.

## Generic errors

```rust
/// errors returned by the `CW2017` driver
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[cfg_attr(feature = "defmt", derive(defmt::Format))]
pub enum Error<E> {
    I2c(E),
    ProfileVerificationFailed,
    InitializationTimeout,
}
```

The driver can fail in two ways: the **bus** failed (no acknowledgement, a stuck
line), or the **chip** misbehaved. The bus error type belongs to whoever implements
`I2c`, so the driver's error is generic over it: `Error<I2C::Error>`. That's the
same shape as `PaintError<E>` in chapter 8. Every method returns
`Result<_, Error<I2C::Error>>`, and the raw bus calls convert with `map_err`:

```rust
async fn read_u16(&mut self, register: Register) -> Result<u16, Error<I2C::Error>> {
    let mut value = [0; 2];
    self.i2c
        .write_read(ADDRESS, &[register.address()], &mut value)
        .await
        .map_err(Error::I2c)?;

    Ok(u16::from_be_bytes(value))
}
```

`map_err(Error::I2c)` passes the **enum variant as a function**. Tuple-like
variants are functions: `Error::I2c` has type `fn(E) -> Error<E>`. It's a small
thing you'll see in almost every Rust driver.

`write_read` is the classic I²C register read: write the register address, then
read without releasing the bus. `u16::from_be_bytes` turns two bytes into a number
using **big-endian** order (most significant byte first), as the datasheet
specifies. Rust's integer types have `from_be_bytes`, `from_le_bytes`,
`to_be_bytes` and friends, so you never need manual shifting or unsafe casts to
decode wire formats.

## Registers as types

Chip datasheets describe registers as bit layouts: "bits 0–13 are the voltage in
units of 312.5 µV, bits 14–15 are reserved". InkPaper describes them with
`bitfield-struct`, a crate whose **attribute macro** generates getters and setters:

```rust
use bitfield_struct::bitfield;

/// raw 16-bit contents of `VCELL_H/VCELL_L`
#[bitfield(u16)]
pub(crate) struct VcellRegister {
    #[bits(14)]
    pub counts: u16,
    #[bits(2)]
    _reserved: u8,
}

/// `CONFIG` reisgter, 0x08
#[bitfield(u8)]
pub(crate) struct ConfigRegister {
    #[bits(4)]
    _reserved: u8,
    #[bits(2)]
    pub restart: u8,
    #[bits(2)]
    pub sleep: u8,
}

impl ConfigRegister {
    pub(crate) const fn sleep_command() -> Self {
        Self::new().with_restart(0b11).with_sleep(0b11)
    }

    pub(crate) const fn reset_command() -> Self {
        Self::new().with_restart(0b11).with_sleep(0)
    }
}
```

`#[bitfield(u8)]` turns the struct into a wrapper around a `u8`, with methods
`restart()`, `with_restart(value)`, `set_restart(value)` and conversions
`From<u8>`/`Into<u8>`. All of them are `const fn`, so `sleep_command()` is
computed at compile time. The code reads like the datasheet, and no hand-written
shift-and-mask code can get a bit position wrong. (The typo "reisgter" is in the
original source. Real code has typos too.)

The register *addresses* are an enum with explicit discriminants:

```rust
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub(crate) enum Register {
    Version = 0x00,
    Vcell = 0x02,
    StateOfCharge = 0x04,
    Temperature = 0x06,
    Config = 0x08,
    SocAlert = 0x0b,
    BatteryProfile = 0x10,
}

impl Register {
    pub(crate) const fn address(self) -> u8 {
        self as u8
    }
}
```

`#[repr(u8)]` fixes the enum's representation to a byte, and `self as u8` reads
the discriminant. An enum instead of loose constants means a function taking a
`Register` can't be handed an arbitrary number by mistake.

## Measurements with units

Raw register values become meaningful types:

```rust
/// battery state of charge
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct StateOfCharge {
    whole_percent: u8,
    fraction_256ths: u8,
}

impl StateOfCharge {
    /// whether the integer portion represents a physically meaningful SoC.
    ///
    /// values above 100 can appear while the gauge has not yet converged
    /// after initialization
    pub const fn is_valid(self) -> bool {
        self.whole_percent <= 100
    }

    /// state of charge in thousandths of a percent
    ///
    /// for example: 73.5% becomes ~ `73_500`
    pub const fn milli_percent(self) -> u32 {
        self.whole_percent as u32 * 1000 + self.fraction_256ths as u32 * 1000 / 256
    }
}
```

The fields are private, so code outside the crate can only read through methods.
A function name like `milli_percent` carries the unit, so nobody wonders whether
`73500` means percent or per-mille. The doc comment captures a hardware quirk
(values above 100 right after initialisation) that would otherwise cost someone
an afternoon.

## Initialisation as a protocol

The gauge needs a *battery profile*, 80 bytes describing the battery's chemistry,
loaded at boot. The firmware stores the exact profile recovered from the vendor's
code as a `const`:

```rust
/// exact 80-byte BATINFO profile recovered from the X4 PRO OEM Cw2017PowerHal
#[rustfmt::skip]
const X4_PRO_BATTERY_PROFILE: BatteryProfile = BatteryProfile::new([
    0x50, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
    0xaa, 0xbf, 0xb5, 0xb4,
    // ...
]);
```

`#[rustfmt::skip]` stops the formatter from reflowing the table into one long
column. Notice that the *profile* lives in the firmware while the *mechanism* to
load it lives in the driver. The driver stays board-independent.

The loading sequence reads like the datasheet's flowchart:

```rust
pub async fn initialize<D>(
    &mut self,
    delay: &mut D,
    profile: &BatteryProfile,
) -> Result<InitializationStats, Error<I2C::Error>>
where
    D: DelayNs,
{
    let version = self.version().await?;
    if !version.is_running() {
        self.reset(delay).await?;
    }

    let alert = self.read_soc_alert().await?;
    if alert.profile_updated() && self.profile_matches(profile).await? {
        return Ok(InitializationStats::AlreadyLoaded);
    }

    self.write_profile(profile).await?;
    if !self.profile_matches(profile).await? {
        return Err(Error::ProfileVerificationFailed);
    }

    let alert = alert.with_profile_updated(true);
    self.write_soc_alert(alert).await?;
    delay.delay_ms(PROFILE_UPDATE_DELAY_MS).await;

    self.reset(delay).await?;

    for _ in 0..READY_POLL_ATTEMPTS {
        let charge = self.state_of_charge().await?;
        if charge.is_valid() {
            return Ok(InitializationStats::Updated);
        }

        delay.delay_ms(READY_POLL_DELAY_MS).await;
    }

    Err(Error::InitializationTimeout)
}
```

The patterns to notice:

- **Delays are injected.** `D: DelayNs` is another `embedded-hal-async` trait. The
  driver never assumes a clock, and a test can pass a fake delay that returns
  instantly.
- **Idempotence.** If the profile is already loaded (the gauge keeps it across a
  reboot), the function returns early. It's safe to call on every boot.
- **Verify after write.** The profile is read back and compared before it's
  trusted.
- **Bounded polling.** The wait for the gauge to converge is a `for` loop with a
  maximum number of attempts, not a `loop` that might hang forever. Hardware does
  fail, and a driver that can hang the firmware is worse than one that returns
  `InitializationTimeout`.
- **A meaningful success value.** `InitializationStats::AlreadyLoaded` versus
  `Updated` lets the firmware log what happened.

## Validating in the constructor: the clock

The BM8563 real-time clock driver (`crates/bm8563`) shows the "parse, don't
validate" principle well. A `DateTime` can only be created through a constructor
that checks every field:

```rust
impl DateTime {
    pub const fn new(
        year: u16,
        month: u8,
        day: u8,
        weekday: u8,
        hour: u8,
        minute: u8,
        second: u8,
    ) -> Result<Self, DateTimeError> {
        if year < 2000 || year > 2099 {
            return Err(DateTimeError::Year { value: year });
        }
        if month < 1 || month > 12 {
            return Err(DateTimeError::Month { value: month });
        }

        let max_day = days_in_month(year, month);
        if day < 1 || day > max_day {
            return Err(DateTimeError::Day { value: day });
        }
        // ... weekday, hour, minute, second ...

        Ok(Self { year, month, day, weekday, hour, minute, second })
    }
}
```

Since the fields are private, *every* `DateTime` that exists is valid. Code that
receives one never has to check again. The error variants carry the offending
value (`Day { value: 29 }`), which makes failures self-explanatory in logs.

The chip stores time in **BCD** (binary-coded decimal: `0x59` means 59), and the
conversions are tiny functions:

```rust
fn decode_bcd(register: u8, value: u8) -> Result<u8, DecodeError> {
    let units = value & 0x0f;
    let tens = value >> 4;
    if units > 9 || tens > 9 {
        return Err(DecodeError::InvalidBcd { register, value });
    }

    Ok(tens * 10 + units)
}

const fn encode_bcd(value: u8) -> u8 {
    (value / 10) << 4 | (value % 10)
}
```

Decoding can fail: a corrupted register could hold `0x7c`, which isn't valid BCD.
Encoding can't fail, because a validated `DateTime` only holds valid values.
The asymmetry in the signatures (`Result` versus plain `u8`) reflects that.

And the calendar logic is a `match` with **or-patterns** and a **guard**:

```rust
const fn days_in_month(year: u16, month: u8) -> u8 {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if is_leap_year(year) => 29,
        2 => 28,
        // callers validate the month before reaching this helper.
        _ => 0,
    }
}
```

The unit tests check exactly the edge cases you'd worry about:

```rust
#[test]
fn rejects_impossible_calendar_dates() {
    assert_eq!(
        DateTime::new(2026, 2, 29, 0, 0, 0, 0,),
        Err(DateTimeError::Day { value: 29 },),
    );
}

#[test]
fn accepts_leap_day() {
    assert!(DateTime::new(2028, 2, 29, 2, 12, 0, 0,).is_ok(),);
}
```

These tests run on your laptop with `cargo test -p bm8563`, with no hardware.
That's the README's third goal, "make device independent code testable on a host
machine", applied all the way down to the drivers.

## Rust concepts in this chapter

- **`embedded-hal` traits** as the boundary between drivers and HALs.
- **Generic drivers** that own their bus; `new`/`release`; bounds only where needed.
- **`async fn` and `.await`**, and what a future is.
- **Generic error enums** (`Error<E>`), `map_err` with **enum variants as functions**.
- **Byte order**: `from_be_bytes` and friends.
- **Attribute macros** from crates (`#[bitfield]`), `#[repr(u8)]` enums, `as`
  for discriminants.
- **Newtypes with units in method names**; private fields.
- **Injected delays**, idempotent initialisation, bounded polling.
- **"Parse, don't validate"** with fallible constructors.
- **Or-patterns and match guards**; `#[rustfmt::skip]`.

## Try it yourself

1. Write a fake bus: a struct holding a `[u8; 256]` register file that implements
   `embedded_hal_async::i2c::I2c` (look at the trait's required method,
   `transaction`). Use it to test `Cw2017::state_of_charge` with
   `futures_lite::future::block_on`.
2. Make your fake bus fail every third transfer. Check that `initialize` returns
   an `Error::I2c` instead of hanging.
3. Add a test to `bm8563` that round-trips every second from 0 to 59 through
   `encode_bcd` and `decode_bcd`.
