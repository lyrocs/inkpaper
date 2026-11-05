# 3. Keeping the page alive: entities and state

A battery reading arrives while someone is halfway through a paragraph. The percentage in a header may need to change; the book's location must not. Opening a settings drawer should not create a second copy of the reader state, either. These are ownership questions disguised as interface details: where does durable state live, and who is allowed to change it?

The element descriptions from chapter 2 are temporary. A render produces them, mounting consumes them, and the frame keeps the representation needed for layout and painting. We should not make those short-lived descriptions own the reader's lasting state. InkPaper instead gives that state a home in the runtime and passes typed handles to the places that need it.

## A handle is permission to ask, not ownership

An entity combines a stored value with an identity. The runtime owns the value; application code holds an `Entity<T>`. These exact struct definitions appear in `crates/ui/src/entity/mod.rs`, with attributes omitted:

```rust,ignore
pub struct EntityId {
    slot: u16,
    generation: u16,
}

pub struct Entity<T> {
    id: EntityId,
    _marker: PhantomData<fn() -> T>,
}
```

There is no `T` value inside `Entity<T>`, and no pointer to dereference directly. The ID identifies a slot. The type parameter says what kind of state the handle expects to find there. A handle for a battery and a handle for a drawer are different Rust types even though their stored representation has the same shape.

`PhantomData` makes that relationship explicit without storing a value. The particular marker, `fn() -> T`, mentions `T` without claiming to own it. It also avoids making the handle's `Send` and `Sync` properties depend on the stored state's properties. That is not permission to access a non-thread-safe state value from anywhere: access still requires the appropriate runtime context. A portable key is not a portable database connection.

The handle implements `Copy` and `Clone` manually, without requiring `T: Copy` or `T: Clone`. Copying a handle duplicates an ID, not the potentially large state. Dropping the last handle does not destroy the entity. Current entities live with the runtime, and their slots are not recycled: the arena validates generation zero. The generation field should not be read as evidence that current entity storage supports removal and reuse.

This model is deliberately unlike `Rc<RefCell<T>>`. Reference counting gives values a lifetime tied to their owners; `RefCell` mediates mutation dynamically. That can be an excellent model for independently created and discarded objects. Here, central ownership makes memory and access easier to account for. Two entities may hold each other's handles without forming a reference-count cycle, because neither handle owns the other value. The cost is equally real: releasing handles does not reclaim entity storage. Create enduring application objects, not a fresh entity for every transient row on every render.

## Build state once, borrow it briefly

The application's constructor gives us a concrete example. This exact excerpt is from `InkPaperApp::new` in `crates/app/src/app/mod.rs`:

```rust,ignore
let battery = cx.new(|_| BatteryState::default());
let rtc = cx.new(|_| RtcState::default());
let battery_indicator = cx.new(|_| BatteryIndicator::new(battery));
let reader_battery_icon = cx.new(|_| ReaderBatteryIcon::default());
let frontlight = cx.new(|_| FrontlightState::default());
let frontlight_panel = cx.new(|_| FrontlightPanel::new(frontlight));
let app = cx.entity();
let reader_menu = cx.new(|_| ReaderDrawer::new(app));
cx.subscribe(reader_menu, Self::on_reader_drawer_event);
```

The battery indicator receives a handle to shared battery state. The drawer receives a handle back to the app. Neither construction copies the underlying state. The root's context can already provide its handle while the root is being built, which is useful for wiring relationships. It does not mean the unfinished root is readable: access to a reserved but uninitialized entity returns `NotReady`.

`cx.new` takes a building closure, `FnOnce(&mut Context<'_, U>) -> U`. The closure returns the initial value and may use the new entity's own context. `FnOnce` permits consuming captured values during construction; the framework calls it once. The allocation can fail, so `try_new` returns a `Result`. The convenient `new` form panics if the runtime cannot create the entity. With fixed storage, the capacity choice is therefore part of startup correctness, not an invisible implementation detail.

Internally, `create_entity` in `crates/ui/src/entity/store.rs` reserves space before running the builder, writes the completed value, then marks it initialized. A reservation guard abandons an unfinished slot if the builder unwinds. That cleanup applies to unwinding panics, not to a panic configuration that aborts execution. Chapter 4 will examine why initialization state matters for memory safety.

## A context keeps access in the present

A `Context<'a, T>` holds the current entity handle and access to the runtime for lifetime `'a`. It is the environment in which the entity runs, not another owner of its state. During rendering, the framework supplies both mutable access to that state and its context. Here is the exact trait from `crates/ui/src/element/render.rs`:

```rust,ignore
pub trait Render: Sized + 'static {
    fn render<'a>(&'a mut self, cx: &mut Context<'_, Self>) -> impl IntoElement + 'a;
}
```

Read the lifetimes as relationships. The returned element may borrow from `self` for `'a`; a label need not allocate a new string merely to describe the UI. The context has a separate lifetime. The returned element is not licensed to keep the temporary context after rendering ends.

The `'static` bound on the state type means it cannot contain references to shorter-lived data. It does not mean the value will exist forever. An owned `String`, for example, satisfies `'static` and is still dropped normally with its owner. This is a recurring Rust distinction: a bound describes what references a type may contain, not how long every instance must live.

To read another entity, the current API takes a closure. The battery indicator's complete render implementation in `crates/app/src/components/header.rs` is:

```rust,ignore
impl Render for BatteryIndicator {
    fn render<'a>(&'a mut self, cx: &mut Context<'_, Self>) -> impl IntoElement + 'a {
        let battery = self.battery.read(cx, |battery| battery.get());

        BatteryLabel::from(BatteryLabelProps { battery })
    }
}
```

`read` temporarily lends the closure an `&BatteryState`. Here `get` returns a copyable `Option<BatteryStatus>`, so the result can leave the closure while the borrowed state cannot. The closure-based signature lets the runtime release its dynamic borrow when the call finishes. It prevents returning a reference tied to that temporary borrow, while still allowing owned results, copied values, and references that are independently valid.

The runtime also records this read as a rendering dependency. If the battery state is notified, entities that rendered from it can render again. This is more useful than simply granting access: it connects “what this view used” with “what should be reconsidered when data changes.” Detailed dependency tracking belongs to chapter 9; the ownership rule is already doing useful work here.

## Mutation and visible change are different events

Receiving new data does not necessarily mean the screen has changed. This exact method from `crates/app/src/app/system.rs` makes that distinction explicit:

```rust,ignore
pub fn apply_battery_status(&mut self, battery: BatteryStatus, cx: &mut Context<'_, Self>) {
    self.battery.update(cx, |state, cx| {
        if state.set(battery) {
            cx.notify();
        }
    });
    self.reader_battery_icon
        .update(cx, |icon, cx| icon.set_battery(battery, cx));
}
```

Inside the first closure, `state` is an exclusive `&mut BatteryState`, and `cx` is that battery entity's context. `BatteryState::set` stores the latest reading but reports whether percentage or charging status changed. A voltage-only change therefore updates data without notifying readers of this entity. The separate reader icon has its own update policy. Notification marks state as changed for the runtime; it does not directly order a physical display refresh.

`update` does not notify automatically. That lets application code distinguish a meaningful visible change from a repeated sample, but it also makes notification the caller's responsibility. A state mutation without the needed `notify` can leave a view stale even though every memory access is safe.

The ergonomic methods `read` and `update` panic on invalid access. Their `try_read` and `try_update` counterparts return `EntityAccessError`, including `NotReady`, `TypeMismatch`, `InvalidEntity`, and `BorrowConflict`. Use the fallible forms where failure is part of the operation's contract; do not turn a broken invariant into a silent `.ok()` merely to keep the UI moving. `#[track_caller]` on the convenience methods helps a panic identify the application callsite.

Dynamic borrow checking remains necessary because freely copied handles can converge on the same slot. While an update holds exclusive access, reading that same entity through another handle is a conflict. Within a handler, use the `&mut self` already provided rather than trying to re-borrow yourself. Handles simplify ownership; they do not abolish Rust's many-readers-or-one-writer rule.

## Try it: predict a quiet battery update

Optional, read-only: inspect `BatteryState::set` in `crates/app/src/system.rs`. Start with a stored reading of 73%, not charging. Imagine a second reading with the same percentage and charging flag but a different voltage. Follow it through `apply_battery_status` above.

Your expected result is precise: the stored reading changes, `set` returns `false`, and the first update closure does not call `notify`. Now change the imagined charging flag; `set` returns `true`. You have traced a product decision—avoid unnecessary screen work—through an equality policy, typed access, and a runtime notification, without requiring a battery or launching the simulator.
