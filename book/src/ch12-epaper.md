# 12. Making the page visible

You press Next. The program has laid out the next paragraph, rasterized its letters, and filled a framebuffer. Yet the reader still shows the old page. That is not necessarily a bug: changing bytes in the microcontroller's memory does not change the material in the screen.

E-paper asks software to cross two boundaries. First, deliver an image to the display controller. Then ask that controller to move the panel into the desired visible state. Keeping those steps separate explains much of InkPaper's display architecture—and why speeding up an SPI transfer cannot, by itself, make a page turn instantaneous.

This session follows the monochrome SSD1677 path, with the shared bus and firmware providing context. The other supported controllers have their own command sequences; this is not a universal recipe to send to any panel.

## The image is a destination, not a light switch

An electrophoretic display forms an image by using electric fields to move charged pigment within the panel. Unlike a conventional emissive display, the visible surface is not simply a collection of lights that software turns on continuously. Reaching a new image requires a timed sequence of driving voltages—a waveform—and the previous physical state matters.

The controller handles that electrical sequence. Our software supplies pixel data and chooses a supported update mode; it does not toggle a GPIO once for every black dot. A slower cleaning update and a faster differential update are different ways of reaching a destination, with different assumptions about the starting state. The code cannot infer that starting state merely from a freshly allocated white framebuffer.

The SSD1677 driver makes this consequence explicit. In `crates/ssd1677/src/lib.rs`, `display` validates the incoming frame and any previous frame before this exact excerpt:

```rust,ignore
// a differential refresh cannot clean arbitrary physical pixels left
// on the panel from before boot/deep sleep.
if self.needs_initial_clean {
    if mode == RefreshMode::Fast {
        mode = RefreshMode::Clean;
    }

    self.needs_initial_clean = false;
}
```

That is a physical invariant expressed as a small software state machine. A request for speed does not override the need to establish a known image. The flag is driver bookkeeping, not a sensor measuring actual pigment positions. Communication failures and interrupted refreshes remain matters for the surrounding error policy; Rust's type system cannot certify the appearance of the panel.

InkPaper's framebuffer holds two bit-planes, one bit from each plane per pixel. Together they represent four luminance levels. In `firmware/x4-pro/src/firmware/framebuffer.rs`, each plane has 800 × 480 / 8 bytes, while the usual logical drawing orientation is 480 × 800. The orientation mapping turns portrait UI coordinates into physical coordinates. Equal corresponding plane bits encode the black-and-white endpoints; differing bits indicate an intermediate gray. This makes “does this image contain gray?” a comparison of planes rather than a scan of high-level drawing commands.

## Giving the controller its bytes

SPI shifts bytes under a clock. The display bus also owns chip select, data/command, reset, and busy pins. Chip select identifies when this peripheral is addressed. Data/command distinguishes an instruction byte from its payload. Reset establishes a controller starting state. Busy is an answer in the other direction: the controller telling firmware it is still working.

The exact `command` method in `crates/epd-bus/src/lib.rs` is short enough to reveal the wire protocol directly:

```rust,ignore
async fn command(&mut self, command: u8) -> Result<(), Self::Error> {
    self.ensure_no_stream()?;

    self.dc.set_low().map_err(Error::DataCommand)?;
    self.cs.set_low().map_err(Error::ChipSelect)?;

    let result = self.spi.write(&[command]).await;

    self.finish_transaction(result)
}
```

The generic `SpiEpdBus<SPI, CS, DC, RESET, BUSY>` owns those handles. Trait bounds require an asynchronous SPI bus, output pins, and a busy input that supports asynchronous waiting. Each hardware implementation brings its own error type. The bus wraps errors by origin, so a failed pin operation is distinguishable from an SPI failure.

`finish_transaction` attempts to raise chip select even after an SPI error. If both operations fail, it preserves the SPI error; if transmission succeeds but releasing select fails, it reports the select error. This is best-effort cleanup, not a promise that a broken output pin was successfully released. It also concerns completed error paths, not arbitrary cancellation of a future midway through a transfer.

Large payloads can be streamed with chip select held low between chunks. The `stream_open` flag rejects starting a second stream or writing a chunk outside one. A guard type could encode more of that protocol in ownership, but the current boolean keeps a single bus interface easy for several controller drivers to implement. It does not make the async trait a conventional `dyn`-compatible trait object.

The interface's async methods have no explicit `Send` guarantee. That means generic callers cannot assume their returned futures can move between threads. It does **not** mean every implementation necessarily returns a non-`Send` future. InkPaper's local embedded use does not require that stronger promise, and the `async_fn_in_trait` lint is explicitly allowed.

## Sending is not showing

In the SSD1677 `display` path, the driver sets the RAM area and uploads the new image. Full and clean modes also write the other RAM plane; fast mode can use a supplied previous frame there. The hardware name `WriteRedRam` appears even in this monochrome path, so reading a command name as a promise about visible color would be misleading.

The actual refresh is separate. After choosing the mode's profile, configuring update control and border behavior, and optionally writing a temperature value, `refresh` ends with this exact excerpt:

```rust,ignore
self.command_data(
    bus,
    Command::DisplayUpdateControl2,
    &[profile.sequence.raw()],
)
.await?;

self.command(bus, Command::MasterActivation).await?;

bus.wait_busy(BUSY_POLARITY, delay)
    .await
    .map_err(Error::Bus)?;

self.screen_on = !profile.sequence.powers_off();
```

There are now two different notions of completion. Uploading the pixels concerns the SPI transport and controller memory. `MasterActivation` starts a controller operation whose completion is observed through BUSY. Even the completion guarantee of a particular SPI write must be read according to its HAL contract; it should never be interpreted as “the reader can see the new page.” The shared bus shown here does not add a `flush` call.

Waiting on BUSY avoids replacing a real completion signal with a guessed fixed refresh delay. But an input can remain active forever because hardware is disconnected or malfunctioning. The shared bus races its wait against a 30-second limit. This is the exact body of `wait_busy`:

```rust,ignore
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
```

Polarity is an argument because controllers need not agree on which voltage means busy. `select` completes when one branch finishes and drops the other. Dropping the wait stops waiting in software; it does not undo an already-issued refresh command. A timeout is evidence that the completion condition was not observed, not proof that the panel is safe to command blindly afterward.

## One reader, several command languages

Firmware uses `X4Panel`, an enum containing SSD1677, UC8179, or UC8279 drivers. Its initialization method matches the variant and calls that driver's implementation. A closed enum makes the supported set visible and avoids requiring heap-allocated trait objects for methods that are async and generic over the bus and delay.

Before construction, the firmware probes the controller. Its inconclusive-result branch refuses to drive the panel rather than sending an assumed SSD1677 initialization sequence to potentially different silicon. Sharing `EpdInterface` means the drivers agree on transporting commands, not that their commands are interchangeable.

There is also no single best refresh policy for every interaction. Fast page turns, gray artwork, a small overlay, and recovery from an unknown initial image impose different requirements. The framebuffer and presenter describe what changed; controller capabilities and refresh policy determine a permissible update; the driver sends its concrete sequence. This separation keeps “draw a paragraph” independent of “activate this waveform,” while leaving the hardware constraints visible where decisions are made.

## Try it: find the moment a page can change

Make a read-only trace through `Ssd1677::display` and its private `refresh` helper. Assume a valid binary frame, no previous frame, `Fast` requested, and `needs_initial_clean` set. Write down the effective mode, the first upload phase, the activation, the busy wait, and the uploads after that wait.

You should find a promotion to `Clean`, writes to both RAM planes, activation followed by the BUSY wait, and further RAM writes because `previous` is absent. The last observation is especially useful: not every upload is itself a visible refresh. No hardware or command execution is needed. This trace establishes control flow only; waveform quality, ghosting, electrical timing, and actual refresh duration require measurements on the intended panel.
