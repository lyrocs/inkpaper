# 11. Asking the battery and clock

The corner of a reading screen looks reassuringly simple: a battery percentage and a time. Neither number belongs to the drawing code. The battery is an analogue system whose remaining charge must be estimated; the clock is another chip maintaining calendar registers. Before the UI can draw either value, software must ask a physical device a question, interpret its reply, and decide whether that reply deserves to be shown.

This session follows those questions through the CW2017 fuel-gauge and BM8563 clock drivers. The interesting Rust is not clever syntax. It is the way ownership, small value types, and fallible conversions keep electrical communication separate from trustworthy application data. You can follow the whole route without a board.

## A conversation on two wires

I²C carries clock and data on a shared pair of wires. A transaction addresses one chip, then transfers bytes according to that chip's protocol. The gauge driver uses address `0x63`; the clock driver uses `0x51`. These are device addresses, not memory locations. A second address—the register number inside the selected chip—identifies which measurement or setting to access.

For a register read, the microcontroller first writes that register number, then reads the response in the same combined transaction. Keeping those operations together matters when several clients share a bus: another transaction must not slip between selecting the register and retrieving its contents. The driver expresses this through `embedded_hal_async::i2c::I2c`, rather than through ESP-specific registers.

Here is the exact two-byte helper from `crates/cw2017/src/lib.rs`, inside its `I2C: I2c` implementation:

```rust,ignore
async fn read_u16(&mut self, register: Register) -> Result<u16, Error<I2C::Error>> {
    let mut value = [0; 2];
    self.i2c
        .write_read(ADDRESS, &[register.address()], &mut value)
        .await
        .map_err(Error::I2c)?;

    Ok(u16::from_be_bytes(value))
}
```

The two-byte array is local storage, not a heap allocation. `from_be_bytes` states the wire order explicitly: the first byte is the most significant. Reading raw memory as a `u16` would instead entangle the answer with alignment and the processor's byte order. A named conversion makes the protocol visible.

The bus implementation supplies its own error type through the associated type `I2C::Error`. `map_err(Error::I2c)` wraps that error without erasing it; `?` returns early on failure. A tuple enum variant doubles as a constructor function, so no extra closure is needed. Successful electrical communication still does not prove that the returned number makes sense. That is the next layer's job.

## Owning access, not owning the whole board

The driver stores the bus handle it is given. This exact excerpt from the same file contains its construction and release operations:

```rust,ignore
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

Moving the handle into `new` prevents its previous owner from continuing to use that same value. Consuming `self` in `release` returns the handle without leaving a functioning driver behind. Notice that neither method needs an `I2c` bound: storing and returning a value do not require bus operations. The bound appears on the separate implementation that actually communicates.

This is narrower than “only this driver can access the chip.” Firmware can give drivers handles to a shared bus; ownership of one handle is not exclusive ownership of the wires. InkPaper does exactly that for touch, battery, and clock. Chapter 13 examines the mutex-backed wrapper that serializes their transactions. The reusable driver need not know whether its handle represents a dedicated peripheral or shared access.

An ESP-specific driver would be simpler to connect directly to one board, but would mix two changing concerns: what the chip understands and how this microcontroller sends bytes. The trait boundary separates them. Portability still requires a correct HAL, compatible electrical setup, and appropriate hardware configuration; implementing a trait is not proof that arbitrary wiring will work.

The methods are asynchronous because a transfer can take time without requiring the caller to occupy the processor throughout. Calling an `async fn` creates a future; polling it drives the operation. An `.await` can suspend the surrounding task when the operation is pending. It does not promise to yield on every call, nor does the keyword itself turn a blocking implementation into interrupt-driven I/O.

## Turning bytes into believable measurements

A battery icon should not have to understand register layouts. `StateOfCharge` instead stores a whole percentage and a fraction in 256ths; its fields are private. Its `milli_percent` method returns thousandths of a percent using integer arithmetic. `Voltage` similarly exposes `millivolts` and `microvolts`, keeping units in the method names instead of leaving each caller to remember a scale factor.

There is a deliberate distinction between representing a reading and accepting it. `StateOfCharge::is_valid` checks that the whole part is at most 100. The source explains that larger values can occur before the gauge converges after initialization. Silently clamping such a value would manufacture a reassuring “100%” from an unsettled estimate.

Initialization is therefore a protocol, not just a constructor. The board supplies a battery profile; the driver reads the running state, resets if necessary, checks whether the profile is already present, and otherwise writes and reads it back. A mismatch produces `ProfileVerificationFailed`. Only after verification does it mark the profile updated and reset the gauge.

The concluding loop is an exact excerpt from `initialize`:

```rust,ignore
for _ in 0..READY_POLL_ATTEMPTS {
    let charge = self.state_of_charge().await?;
    if charge.is_valid() {
        return Ok(InitializationStats::Updated);
    }

    delay.delay_ms(READY_POLL_DELAY_MS).await;
}

Err(Error::InitializationTimeout)
```

The constants specify 50 attempts and a 20-millisecond delay between unsuccessful readings. That bounds the number of readiness polls, not the duration of every possible bus fault: each I²C operation must itself complete or fail. The injected `DelayNs` implementation keeps timing independent of a particular timer peripheral. Reading back the profile costs transactions, but avoids treating “the write returned successfully” as evidence that the desired configuration is installed.

Both chip crates use `#![no_std]`. That removes reliance on Rust's standard library; it does not mean Rust forbids allocation. These particular driver paths use fixed local buffers and value types. Firmware elsewhere can still provide an allocator and use `alloc`.

## A clock can answer and still be wrong

The clock adds another decoding problem: binary-coded decimal. In this format `0x59` encodes decimal 59, with one decimal digit in each nibble. Treating it as an ordinary byte would produce 89. The exact decoder in `crates/bm8563/src/lib.rs` checks the digits before interpreting them:

```rust,ignore
fn decode_bcd(register: u8, value: u8) -> Result<u8, DecodeError> {
    let units = value & 0x0f;
    let tens = value >> 4;
    if units > 9 || tens > 9 {
        return Err(DecodeError::InvalidBcd { register, value });
    }

    Ok(tens * 10 + units)
}
```

Valid decimal digits are only the first hurdle. `DateTime::new` checks years within 2000–2099, month and day including leap years, and the ranges of weekday, hour, minute, and second. Private fields make those checks part of ordinary construction. The constructor does not verify that a supplied weekday agrees with the calendar date; range validity and calendar consistency are different guarantees.

The wire decoder also preserves the chip's low-voltage flag and century marker. A `Reading` contains `Option<DateTime>`, and `valid()` requires both a date and no low-voltage warning. Invalid BCD returns a decoding error; well-formed digits describing an impossible date yield a reading without a date. The century marker is exposed separately rather than presented as an unambiguous absolute century.

That layered answer is more useful than a single “read succeeded” boolean. The UI can distinguish communication failure from data that arrived but should not yet be trusted. The physical world rarely offers the clean yes/no guarantees our first function signatures suggest.

## Try it: follow one bad clock byte

Without changing code, trace `decode_reading` in `crates/bm8563/src/lib.rs`. Start with the registers `[0x59, 0x59, 0x23, 0x28, 0x06, 0x02, 0x26]`. Then replace only the first byte with `0xd9`, and finally with `0x7c`.

The expected outcomes are distinct: the first reading has a date and is valid; `0xd9` still decodes seconds as 59 after masking, but sets the low-voltage flag so `valid()` is false; `0x7c` reaches `InvalidBcd`. This is a read-only code experiment, not an electrical test. For an optional executable check of the calendar rules, the existing host tests can be run with `cargo test -p bm8563`. Neither exercise establishes the accuracy of a physical clock or fuel gauge.
