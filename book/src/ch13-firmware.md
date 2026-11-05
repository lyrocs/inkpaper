# 13. Keeping the reader awake to events

While a page refreshes, a reader can press a button again. A battery reading can arrive, a USB cable can be connected, and a clock tick can become due. Firmware must let these activities coexist even though some hardware operations take much longer than others.

The goal is not to make everything happen simultaneously. It is to preserve the right information while work is pending, give each peripheral a clear owner, and resume useful work when something changes. InkPaper combines an asynchronous executor with ordinary Rust ownership to do that. The result is responsive in a specific sense: hardware-facing tasks can continue while another task awaits I/O. It does not mean the main UI loop processes every input during every long operation.

## Give each piece of hardware a home

The device entry point in `firmware/x4-pro/src/firmware/mod.rs` uses `#[esp_rtos::main]` and receives an Embassy `Spawner`. Its return type is `!`, Rust's never type: this entry point does not return an ordinary result to a desktop process launcher. Startup initializes the HAL, configures memory, establishes power rails, and launches tasks.

HAL peripheral handles are values that move into their owners. For example, storage receives the SD host and its pins, plus a separate SD-power handle. The main task keeps other rails and the display. This is stronger than an informal agreement that two modules should not reconfigure the same peripheral: ordinary moves prevent reuse of a consumed handle. It is not a magical prohibition on all possible low-level access; the guarantee depends on respecting the HAL's safe abstractions.

The firmware uses `no_std` on its Xtensa target, but it absolutely does allocate. It explicitly imports `alloc`, initializes PSRAM and internal-memory allocator regions, and uses heap-backed UI storage and framebuffer vectors. `no_std` means the standard library is unavailable, not that `Vec` and `Box` are impossible. Memory capacity and allocation behavior still need engineering attention.

Long-lived tasks also need long-lived borrowed storage. A local value in a function that might return is not enough. The shared I²C bus uses `StaticCell` to initialize a permanent slot at runtime. This exact excerpt is from `firmware/x4-pro/src/firmware/i2c_bus.rs`:

```rust,ignore
pub type HardwareI2c = I2c<'static, Async>;
pub type SharedI2cBus = Mutex<CriticalSectionRawMutex, HardwareI2c>;
pub type SharedI2cDevice = I2cDevice<'static, CriticalSectionRawMutex, HardwareI2c>;

static I2C_BUS: StaticCell<SharedI2cBus> = StaticCell::new();

pub fn init(i2c: HardwareI2c) -> &'static SharedI2cBus {
    I2C_BUS.init(Mutex::new(i2c))
}

pub fn device(bus: &'static SharedI2cBus) -> SharedI2cDevice {
    I2cDevice::new(bus)
}
```

`'static` describes how long the reference remains valid, not whether the bus is immutable or initialized at compile time. `StaticCell` permits one initialization and avoids creating unchecked references to `static mut` storage. The mutex allows device handles to share one underlying peripheral; each I²C transaction acquires access rather than overlapping another transaction on the wires.

That is the answer to chapter 11's ownership puzzle. The gauge owns a `SharedI2cDevice`, not an exclusively reserved set of wires. The wrapper implements the same bus trait as the hardware handle, so the chip driver does not need a second shared-bus API. Locking individual transactions does not make an entire multi-transaction initialization atomic; that distinction matters when reasoning about larger protocols.

## Waiting is useful work

A physical button does not produce a tidy logical event. Its contacts can bounce between levels. InkPaper's debouncer accepts a new state after three consecutive samples, using a 10-millisecond scan interval while a transition is underway. When settled, it waits for the input level to differ instead of waking periodically just to discover nothing changed.

The complete current task from `firmware/x4-pro/src/firmware/buttons.rs` shows the scheduling boundary:

```rust,ignore
#[embassy_executor::task]
pub async fn button_task(mut buttons: Buttons<'static>) {
    loop {
        let events = buttons.sample();
        for event in events.into_iter().flatten() {
            // don't silently discard physical input
            // if the display task falls behind, backpressure here is preferrable
            // to losing the button edge
            INPUT_EVENTS.send(InputEvent::Button(event)).await;
        }

        // only scan on a timer while a button is changing; otherwise sleep until
        // one of the pins leaves its accepted level
        if buttons.is_settled() {
            buttons.wait_for_change().await;
        } else {
            Timer::after(Duration::from_millis(SCAN_INTERVAL_MS)).await;
        }
    }
}
```

`sample` returns optional events; `flatten` skips absent ones. When a send or hardware wait is pending, the executor can poll another ready task. An `.await` whose future is already ready need not suspend at all. Likewise, a long synchronous calculation inside an async function can occupy its executor until it returns control. Async syntax is a way to represent waiting, not a general preemption guarantee.

The wait uses a level rather than requiring a fresh edge. If the pin changes between sampling and starting the wait, the desired level is already present and the wait can complete. That closes a small but important gap where an edge-only design could sleep despite a pressed button.

## Preserve events, replace snapshots

A button edge and a battery percentage have different meanings. Losing a press may lose a requested action. Retaining every old battery reading while the display is busy merely delays the newest truth.

These exact declarations come from `input.rs` and `battery.rs`, respectively; they are two separate source excerpts combined for comparison:

```rust,ignore
pub static INPUT_EVENTS: Channel<CriticalSectionRawMutex, InputEvent, INPUT_QUEUE_CAPACITY> =
    Channel::new();

pub static BATTERY_UPDATES: Signal<CriticalSectionRawMutex, BatteryReading> = Signal::new();
```

The input channel has capacity 16. `send(...).await` waits for space when full rather than deliberately discarding the already-produced event. This backpressure has a cost: while the producer is waiting to send, it is not sampling more button changes. A bounded queue cannot promise to capture an unlimited stream of physical transitions.

A signal holds at most one unread value. A newer battery update replaces an older unread update, and waiting consumes the available value. It is neither a queue of readings nor a broadcast subscription that every listener independently receives. The chosen primitive says what information the application considers worth retaining.

`CriticalSectionRawMutex` protects brief access to synchronization bookkeeping through the platform's critical-section mechanism. It does not mean interrupts remain disabled during a whole asynchronous bus transfer. Confusing the raw mutex protecting a queue with the async mutex governing ownership would make the design appear far more blocking than it is.

## Wake once, then do the right work

The main loop waits for eight possibilities: input, battery, RTC, display-idle timeout, USB-host state, auto-sleep, WiFi events, and deferred application work. `next_wake` stores pinned references to futures in an array and calls `select_array`. Its declared element type is `Pin<&mut dyn Future<Output = Wake>>`.

Why this type? Each async block has a distinct anonymous type, while an array needs one element type. `dyn Future` hides those differences behind their common output, the `Wake` enum. `Pin` supports polling futures without moving state that may have become address-sensitive. The local `pin!` calls do not require boxing each future on the heap.

Polling order is policy: input is first, deferred work last. If several sources are ready together, the earlier source wins. Losing pending receive futures are dropped; the channel retains queued events, and the signals retain unread values. This cancellation argument is specific to those operations. Dropping an arbitrary future in the middle of a hardware protocol is not automatically safe.

Once an input wins, the loop can combine already-queued events. This exact excerpt from the input arm in `firmware/mod.rs` deliberately stops short of unlimited draining:

```rust,ignore
action = handle_input_event(runtime, app, event);
// combine events accumulated while the e-ink panel was busy.
while action == InputAction::Continue
    && runtime.invalidation() != Invalidation::Rebuild
{
    let Ok(event) = INPUT_EVENTS.try_receive() else {
        break;
    };
    action = handle_input_event(runtime, app, event);
}
```

`try_receive` does not wait. The loop stops when the queue is empty, an action changes control flow, or the UI needs rebuilding. It is therefore wrong to promise that every burst of three presses always becomes exactly one three-page jump. The application's invalidation and request state participate in that decision.

The loop then services application work, renders if needed, and awaits presentation. During those awaits, other firmware tasks may run and collect events, but this same main loop is not simultaneously dispatching them. Chapter 14 explores how the application handles slow work without pretending that async automatically makes the UI concurrent.

## Try it: predict two wake-ups

Read `next_wake` in `firmware/x4-pro/src/firmware/mod.rs`. Imagine one queued button event, one unread battery update, and `deferred` set to true. Assume nothing else arrives, input handling queues no further input, and the next iteration still has deferred work.

The first selection is `Wake::Input`. The next selection is `Wake::Battery`, not `Wake::DeferredWork`: input won without consuming the unread signal, and the battery branch precedes the deferred branch. Now imagine a newer battery value arrives between iterations. The second wake carries that newer value, not both values in sequence.

This read-only experiment needs no command, executor, or board. Its observable result is the ordered branch trace you can check against the array and primitive semantics. It tests your scheduling model, not real interrupt latency or battery consumption.
