# 13. Firmware: an async main loop

> **Where we are:** commit `83cd7e5`, "basic x4pro firmware", 22 August 2026,
> followed by a hardware bring-up sprint: rendering the app (`d649626`), buttons
> (`6e728d2`), touch (`a568d2a`), deep sleep (`f914889`), frontlight
> (`5546a01`), a shared I²C bus (`6f45f0a`), the RTC, the SD card (`b93a626` →
> `a49f9da`) and `defmt` logging (`a43a008`). Later refinements replaced polling
> with interrupts (`8de9068`, `b61c3f6`, October).

## No operating system, no `main`

On a laptop, the OS loads your program, sets up a stack and calls `main`. On the
ESP32-S3 there is no OS. A bootloader jumps into your binary, and *something* has
to set up memory, interrupts and clocks. In InkPaper that something is
`esp-rtos`, so the firmware's entry point looks like this:

```rust
#[esp_rtos::main]
async fn main(spawner: Spawner) -> ! {
    rtt_target::rtt_init_defmt!();
    info!("InkPaper X4 Pro boot");

    let config = esp_hal::Config::default().with_cpu_clock(CpuClock::max());
    let peripherals = esp_hal::init(config);
    // ...
}
```

Several things are unusual here.

- **`#[esp_rtos::main]`** is an attribute macro that generates the real entry
  point, starts an async executor and runs this function as its first task. This
  is why `main.rs` had `#![no_main]` in chapter 1.
- **`async fn main`**. The whole firmware is async from the top.
- **`-> !`**, the **never type**. This function never returns, because there is
  nothing to return *to*. The compiler checks it: the body must end in an
  infinite `loop` or a call that itself returns `!`.
- **`spawner: Spawner`** is a handle for starting more tasks.

## Peripherals are values you own

```rust
let peripherals = esp_hal::init(config);
```

`peripherals` is a struct with one field per hardware block: `peripherals.GPIO0`,
`peripherals.SPI2`, `peripherals.I2C0`, `peripherals.SDHOST`... Each field is a
**zero-sized singleton type**. `esp_hal::init` can be called only once, so each
peripheral exists exactly once in the whole program. To use the SPI controller,
you *move* `peripherals.SPI2` into an `Spi::new(...)` call, and from then on
nobody else can.

That's Rust's ownership model applied to hardware. In C, two modules can both
write to the same GPIO register and fight over it. In Rust, the second module
doesn't compile: `peripherals.GPIO6` has already been moved.

```rust
let busy = Input::new(peripherals.GPIO6, InputConfig::default());
let mut bus = SpiEpdBus::new(spi, cs, dc, reset, busy).unwrap();
```

GPIO6 now belongs to the display bus, and the compiler will enforce that.

## Heaps, plural

The ESP32-S3 has fast internal RAM and slower, larger external PSRAM. The
firmware registers several allocators, and the order matters:

```rust
esp_alloc::psram_allocator!(peripherals.PSRAM, esp_hal::psram);
// the WiFi driver allocates from internal RAM only. Registered after PSRAM,
// these regions stay free for it while ordinary allocations land in PSRAM
esp_alloc::heap_allocator!(#[esp_hal::ram(reclaimed)] size: 64 * 1024);
esp_alloc::heap_allocator!(size: 36 * 1024);
```

This is where `alloc` from chapter 1 gets its memory. Rust's `#[global_allocator]`
mechanism lets a crate provide the allocator that `Vec`, `String` and `Box` use.
`esp-alloc` is such a crate, and these macros hand it memory regions. The comment
explains a real-world constraint (the WiFi driver needs internal RAM) that would
be painful to rediscover.

With a heap available, the device's UI runtime uses the **heap** tables from
chapter 4, with initial reservations sized for the app:

```rust
impl RuntimeStorage for UiStorage {
    type Entities = HeapEntityArena<UI_ENTITY_SLOTS>;
    type Callbacks = HeapCallbackArena<UI_CALLBACK_SLOTS>;
    type Globals = HeapGlobalArena<UI_GLOBAL_SLOTS>;
    type Frame = HeapFrame<UI_FRAME_NODES, UI_FRAME_TEXT_BYTES>;
    type ElementStates = HeapElementStates<UI_ELEMENT_STATES>;
    type Events = HeapEvents;
}

pub type UiRuntime =
    Runtime<UiStorage, AppResources<'static, UI_GLYPH_CACHE_SLOTS, UI_GLYPH_CACHE_BYTES>>;
```

The fixed tables remain available for smaller devices, and the tests keep them
working.

## `'static` without leaking: `StaticCell`

Tasks in an embedded executor can live forever, so anything they borrow must be
`'static`. But some values can only be built at runtime, after `esp_hal::init`.
The UI runtime, for example, is big and should not live on the stack of `main`.
`static_cell` solves this:

```rust
static FRAMEBUFFER: StaticCell<FramebufferStorage> = StaticCell::new();
static UI_RUNTIME: StaticCell<UiRuntime> = StaticCell::new();

// later, inside main:
let frame = FRAMEBUFFER.init_with(FramebufferStorage::white);
let runtime = UI_RUNTIME.init_with(UiRuntime::default);
```

A `StaticCell<T>` is an empty `static` slot. `init_with(f)` runs `f` once, stores
the result in the slot, and returns `&'static mut T`. Calling it a second time
panics, so you can never get two `&mut` to the same value. It's a safe
replacement for `static mut`, which is easy to misuse. Since edition 2024, taking
a reference to a `static mut` is denied by default (the `static_mut_refs` lint).

## Tasks

The firmware splits the device into independent **tasks**:

```rust
spawner.spawn(frontlight_task(peripherals.LEDC, peripherals.GPIO8, peripherals.GPIO9).unwrap());
spawner.spawn(button_task(buttons).unwrap());
spawner.spawn(power_button_task(peripherals.GPIO3, peripherals.LPWR).unwrap());
spawner.spawn(storage::storage_task(/* SD card and USB pins */).unwrap());
// ... after the display is up:
spawner.spawn(touch_task(touch).unwrap());
spawner.spawn(battery_task(i2c_bus::device(shared_i2c), peripherals.GPIO21).unwrap());
spawner.spawn(rtc_task(i2c_bus::device(shared_i2c)).unwrap());
spawner.spawn(wifi_task(peripherals.WIFI).unwrap());
```

A task is an `async fn` with an attribute:

```rust
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

`#[embassy_executor::task]` turns the function into something whose future is
stored in a **statically allocated** slot. Embassy, the async framework
`esp-rtos` builds on, needs no heap for tasks. Calling `button_task(buttons)`
returns a `Result` holding a spawn token (it fails if the task's slot is already
in use), and `spawner.spawn(...)` hands it to the executor.

There are no threads here. All tasks run on one core, taking turns at every
`.await`. A task that awaits a pin interrupt costs nothing while it waits. The
comment in `button_task` describes the October change (`b61c3f6`, "wait on the
button gpios instead of scanning while idle"): the task polls every 10 ms only
while a button is bouncing, and otherwise sleeps until a pin changes. The CPU can
then idle, which matters for battery life.

`events.into_iter().flatten()` is a neat iterator idiom. `sample()` returns
`[Option<ButtonEvent>; 2]`, and `flatten` on an iterator of `Option`s skips the
`None`s and unwraps the `Some`s.

### Debouncing as a small state machine

Mechanical buttons "bounce": one press produces several on/off flickers within a
few milliseconds. The debouncer accepts a new level only after it's been seen for
three consecutive samples:

```rust
fn sample(&mut self) -> Option<ButtonEdge> {
    let pressed = self.input.is_low();

    // we're back at the already accepted state.
    // any transient candidate was just switch bounce
    if pressed == self.stable_pressed {
        self.candidate_pressed = pressed;
        self.candidate_samples = 0;
        return None;
    }

    // the raw level changed to a new candidate
    if pressed != self.candidate_pressed {
        self.candidate_pressed = pressed;
        self.candidate_samples = 1;
        return None;
    }

    self.candidate_samples = self.candidate_samples.saturating_add(1);
    if self.candidate_samples < DEBOUNCE_SAMPLES {
        return None;
    }

    // the candidate has remained stable long enough
    self.stable_pressed = self.candidate_pressed;
    self.candidate_samples = 0;

    Some(if self.stable_pressed {
        ButtonEdge::Pressed
    } else {
        ButtonEdge::Released
    })
}
```

Each early return is commented with the situation it handles. The final
expression shows `if` as a value inside `Some(...)`.

## Talking between tasks

Tasks need to send each other data. Embassy provides synchronisation primitives
that work in `static`s:

```rust
pub static INPUT_EVENTS: Channel<CriticalSectionRawMutex, InputEvent, INPUT_QUEUE_CAPACITY> =
    Channel::new();

pub static BATTERY_UPDATES: Signal<CriticalSectionRawMutex, BatteryReading> = Signal::new();
```

- A **`Channel<M, T, N>`** is a bounded queue of `N` items. `send(...).await`
  waits when it's full (that's the "backpressure" in the button task's comment),
  and `receive().await` waits when it's empty. Input events use a channel because
  every button edge matters.
- A **`Signal<M, T>`** holds at most *one* value: a new `signal(value)` replaces an
  unread one. Battery readings use a signal because only the latest reading
  matters.

Choosing between them is a design decision, and the code makes it visible in the
type.

`CriticalSectionRawMutex` is the locking strategy. On a microcontroller, "locking"
means briefly disabling interrupts (a *critical section*) so an interrupt handler
can't observe a half-updated queue. The type parameter lets the same `Channel`
code work with different strategies on different platforms.

`Channel::new()` is a **`const fn`**, which is what allows it in a `static`
initialiser. That's another reason so many InkPaper constructors are `const`.

### Sharing one I²C bus

Chapter 11's drivers each *own* their bus. But the battery gauge, the clock and
the touch controller sit on the same two wires. The answer is a mutex-protected
bus and lightweight per-device handles that implement the `I2c` trait themselves:

```rust
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

Each `I2cDevice` locks the async mutex for the duration of one transaction. The
drivers don't change at all: `Cw2017<SharedI2cDevice>` works because
`SharedI2cDevice: I2c`. That's the payoff of writing drivers against traits. The
**type aliases** (`type SharedI2cDevice = ...`) keep the long generic types
readable.

## The main loop

After setup, `main` becomes the UI task. It waits for "something happened",
updates the app, and presents a frame:

```rust
loop {
    let mut action = InputAction::Continue;
    let deferred = app_service.has_deferred_work();

    match next_wake(&display_power, &auto_sleep, deferred).await {
        Wake::Input(event) => {
            auto_sleep.reset();

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
        }
        Wake::Battery(reading) => apply_battery_reading(runtime, app, reading),
        Wake::Rtc(state) => apply_rtc_state(runtime, app, state),
        // ...
    }

    // ... sleep, forced refresh, service app requests (chapter 14) ...

    let Some(update) = presenter.render_pending(runtime, frame, panel.capabilities()) else {
        continue;
    };

    display_power
        .present(&mut panel, &mut bus, &mut delay, frame, update)
        .await
        .unwrap();
}
```

Notice the inner `while` loop. A display refresh can take hundreds of
milliseconds, during which the user may tap several times. Instead of rendering
one frame per tap, the loop **drains** queued events with `try_receive` (which
doesn't wait) and renders once. If you flip three pages quickly, you see the third
page, not an animation of all three.

### Waiting on eight things at once

What woke the loop? An input event, a battery reading, a clock tick, the display
going idle, a USB cable, an auto-sleep timeout, a WiFi result, or deferred work.
Each source is a future. The firmware waits on all of them:

```rust
/// What woke the main loop.
enum Wake {
    Input(InputEvent),
    Battery(BatteryReading),
    Rtc(RtcState),
    DisplayIdle,
    UsbHost(UsbHostState),
    AutoSleep,
    Wifi(WifiEvent),
    DeferredWork,
}

/// Waits for the next thing the main loop handles.
///
/// Sources are polled in order, so input wins when several are ready. The
/// losers are dropped, which is safe: the update signals keep their stored
/// value, and the channels keep queued events.
async fn next_wake(
    display_power: &DisplayPowerManager,
    auto_sleep: &AutoSleep,
    deferred: bool,
) -> Wake {
    let wakes: [Pin<&mut dyn Future<Output = Wake>>; 8] = [
        pin!(async { Wake::Input(INPUT_EVENTS.receive().await) }),
        pin!(async { Wake::Battery(BATTERY_UPDATES.wait().await) }),
        pin!(async { Wake::Rtc(RTC_UPDATES.wait().await) }),
        pin!(async {
            display_power.wait_idle_timeout().await;
            Wake::DisplayIdle
        }),
        pin!(async { Wake::UsbHost(USB_HOST_UPDATES.wait().await) }),
        pin!(async {
            auto_sleep.wait().await;
            Wake::AutoSleep
        }),
        pin!(async { Wake::Wifi(WIFI_EVENTS.receive().await) }),
        pin!(async {
            deferred_work(deferred).await;
            Wake::DeferredWork
        }),
    ];

    select_array(wakes).await.0
}
```

This block is dense, so let's unpack it.

**Each async block has its own anonymous type.** Eight blocks means eight
different types, and an array needs one element type. So each is converted to a
**trait object**, `dyn Future<Output = Wake>`. Every block produces a `Wake`, so
they agree on `Output`.

**Futures must be pinned before polling.** An async block may hold references
into itself (a local variable borrowed across an `.await`). Moving it in memory
would break those references, so Rust requires a future to be **pinned**,
guaranteed not to move, before it's polled. `Pin<&mut T>` is a pointer that
carries that guarantee. The `pin!` macro pins a value *on the stack*, with no
heap. So the array holds eight `Pin<&mut dyn Future<Output = Wake>>`.

**`select_array`** polls all eight and returns the first to finish, with its
index (`.0` takes the value).

**Cancellation is safe because of the primitives chosen.** The doc comment
argues it: dropping the losing futures loses nothing, because a `Signal` keeps
its value until read and a `Channel` keeps its queue. When you `select` in async
Rust, always ask "what happens to the futures that lose?". Here the answer is
written down.

`Wake` turns "which future won" into data with a payload, which is the enum
pattern again. The `match` in the loop then handles each case.

`deferred_work` is a small trick:

```rust
/// Ready at once when the app service put work off until a frame showed it,
/// so it goes on without waiting for input.
async fn deferred_work(deferred: bool) {
    if !deferred {
        core::future::pending::<()>().await;
    }
}
```

`core::future::pending()` is a future that never completes. Awaiting it means
"this branch never wins". So the eighth future is either ready immediately or
never ready, depending on a flag, and the array always has eight elements.

## Logging with `defmt`

The firmware logs like this:

```rust
info!(
    "display probe controller={} verdict={}",
    detection.controller, detection.verdict,
);
```

That looks like `println!`, but `defmt` ("deferred formatting") never formats
strings on the device. The format string is stored in a separate section of the
ELF file, and the device sends only an index and the raw argument bytes over the
debug probe. The host formats them. Logs are tiny and fast, and the strings take
no flash space. Types opt in with `#[derive(defmt::Format)]`, which is what all
those `#[cfg_attr(feature = "defmt", derive(defmt::Format))]` lines in the driver
crates were for.

## Power: making invalid electrical states impossible

The last example shows the same care around power rails:

```rust
pub fn new(
    peripheral: impl OutputPin + 'd,
    touch: impl OutputPin + 'd,
    sd: impl OutputPin + 'd,
) -> (Self, SdPower<'d>) {
    // configure the awake states while any RTC pad holds left by the previous
    // deep-sleep cycle are still active.
    // once all 3 output configurations are ready we can release the holds without
    // allowing the pins to pass through their reset state
    let peripheral = Output::new(peripheral, Level::High, OutputConfig::default());
    let touch = Output::new(touch, Level::High, OutputConfig::default());
    let sd = Output::new(sd, Level::High, OutputConfig::default());

    sleep_pins::release_power_holds();

    (Self { peripheral, touch }, SdPower { enable: sd })
}
```

The constructor returns a **tuple**: the rails the main task keeps, and the SD
power switch, handed separately to the storage task. Splitting ownership at
construction means each task holds exactly the pins it may touch. The comment
describes an ordering requirement (configure, *then* release the sleep holds) that
prevents a glitch on the power rails during wake-up.

## Rust concepts in this chapter

- **`#![no_main]`**, entry-point macros, `async fn main`, the never type `!`.
- **Peripherals as owned singletons**: ownership as hardware access control.
- **Global allocators** and multiple heap regions.
- **`StaticCell`** for runtime-initialised `'static` values; why not `static mut`.
- **Embassy tasks**, spawning, cooperative scheduling, interrupt-driven waiting.
- **`Channel` vs `Signal`**; critical-section mutexes; `const fn` constructors in
  `static`s.
- **Shared buses** through wrapper types that implement the same trait.
- **Pinning** (`Pin`, `pin!`), `dyn Future`, `select_array`, cancellation safety.
- **`core::future::pending`**, `try_receive`, draining queues.
- **`defmt`** and `cfg_attr` derives.
- **Returning tuples** to split ownership.

## Try it yourself

1. In `firmware/x4-pro/src/firmware/mod.rs`, add a ninth `Wake` variant for a
   hypothetical `Signal`. What else has to change for the array type to
   compile?
2. Change `BATTERY_UPDATES` from a `Signal` to a `Channel` of capacity 1 in a
   scratch copy. Think about what happens when two readings arrive while the main
   loop is busy refreshing the display.
3. Without hardware: write a host test that runs the button debouncer's
   `sample` logic against a scripted sequence of levels. (Hint: extract the logic
   into a function that takes `pressed: bool` instead of reading the pin.)
