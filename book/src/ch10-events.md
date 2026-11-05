# 10. Interaction and typed events

> **Where we are:** `12b6e4e` ("add pointer hit testing and click dispatch",
> 9 August 2026), the typed event work of 13 August (`e388090` → `dc63d2b`), the
> app taking over input policy (`8f4372f`, 26 August), the removal of focus
> navigation (`75dc5fe`, 26 September), long presses (`f5c8a1a`, 27 September)
> and finally typed events between entities (`93f403e`, 5 November).

## From a tap to a listener

The device reports "a finger touched the screen at (212, 640)". Somewhere in the
UI, a `div` with `.on_activate(listener)` should react. Getting from one to the
other takes three steps:

1. **Hit testing**: which node is at (212, 640)?
2. **Targeting**: which of the nodes there wants this kind of event?
3. **Dispatch**: call the listener from chapter 5 with the right event value.

### Hit testing in paint order

```rust
pub(crate) fn hit_test_event(
    &self,
    root: NodeId,
    position: Point,
    event_type: TypeId,
) -> Option<NodeId> {
    let mut hit = None;

    for visual in self.visual_nodes(root) {
        if !visual.contains(position) {
            continue;
        }

        let node = visual.node();
        if self.event_callbacks(node, event_type).next().is_some() {
            hit = Some(node);
        }
    }

    hit
}
```

The function walks the nodes **in paint order** (`visual_nodes` yields them the
way chapter 8's painter visits them, with clipping and scroll offsets applied)
and remembers the *last* matching node. The last node painted is the one on top,
so that's the one the user sees and touches. It's a simple rule, and it
automatically handles overlays like the control centre or the reader's drawer:
they're mounted later, so they win.

`visual_nodes(root)` returns an **iterator**. The traversal state (the bounded
stack from chapter 8) lives inside the iterator struct, and `for` drives it. By
implementing `Iterator` for a traversal, every consumer can use `for`, `filter`,
`find`, `any` and friends, instead of writing its own tree walk.

`event_callbacks(node, event_type).next().is_some()` asks "does this node have
at least one listener for this event type?". `.next().is_some()` on an iterator is
the idiomatic and lazy "is it non-empty?".

### Events are types

The `event_type: TypeId` parameter is the key idea of the 13 August commits:
**events are distinguished by their Rust type**. `ActivateEvent` is one type. The
app defines its own, like `LongPressEvent`:

```rust
/// A touch held still on an element. Like `ActivateEvent`, it carries the
/// element's id, so one listener can serve many elements. Elements take it
/// with `on:LongPressEvent`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct LongPressEvent {
    id: ElementId,
    position: Point,
}
```

The framework has never heard of `LongPressEvent`, and doesn't need to. An element
binds a `Listener<LongPressEvent>` with `.on(listener)`. The binding stores
`TypeId::of::<LongPressEvent>()`, and dispatch finds it again:

```rust
pub fn dispatch_at_with<E>(
    &self,
    position: Point,
    event: impl FnOnce(ElementId) -> E,
) -> Result<bool, ListenerInvokeError>
where
    E: 'static,
{
    let Some(target) = self.target_at::<E>(position) else {
        return Ok(false);
    };

    self.dispatch_to_with(target, event)
}
```

The caller *names the event type* through the generic parameter, and that type
selects both which nodes count as hits and which listeners are called. One generic
function covers every event type the app will ever invent. That's the payoff of
`TypeId` plus generics: an **open set of event types** without a central enum
listing them all.

`event: impl FnOnce(ElementId) -> E` builds the event *after* the target is
found, so the event can carry the target's id (chapter 5's "one listener, many
buttons"). Taking a closure instead of a value is a nice way to say "I'll give
you information you don't have yet".

The returned `bool` says whether anything handled the event. The app's input code
(`crates/app/src/input.rs`) uses it for long presses. If an element took the long
press, the rest of that touch is *captured* and can no longer end as a tap:

```rust
let captured = match action {
    LongPressAction::Ignore => false,
    LongPressAction::Dispatch => {
        runtime.dispatch_at_with(position, |id| LongPressEvent { id, position })?
    }
    LongPressAction::Capture => true,
};
```

## Press, release, cancel

A tap isn't a single event. The finger goes down on a button (it should look
pressed), may slide off (it should look unpressed, and nothing should happen),
and lifts. The runtime models this as an **activation lifecycle**:

```rust
pub fn begin_activation_at(&mut self, position: Point) -> bool {
    let target = self.activation_target_at(position);
    let next = target.map(EventTarget::element);
    let invalidation = self.set_pressed(next);

    self.invalidate_render(invalidation);

    target.is_some()
}

pub fn complete_activation_at(&mut self, position: Point) -> Result<bool, ListenerInvokeError> {
    let Some(pressed) = self.activation.pressed() else {
        return Ok(false);
    };

    let target = self.activation_target_at(position);
    let activated = target.map(EventTarget::element) == Some(pressed);

    // ... clear the pressed state, update focus ...

    let Some(target) = target.filter(|target| target.element() == pressed) else {
        return Ok(false);
    };

    self.activate(target)
}

pub fn cancel_activation(&mut self) { /* ... */ }
```

The activation completes only if the finger is released over the **same
element** it pressed: `target.filter(|t| t.element() == pressed)`. `Option::filter`
keeps the value only if the predicate holds. `target.map(EventTarget::element)`
passes a *method path* as the mapping function, which is shorter than
`|t| t.element()`.

`set_pressed` computes what changed and returns an invalidation (chapter 9).
Pressing a button usually changes only its colour, so it's a `Paint`
invalidation with damage limited to that button. The framework's `when_pressed`
styles make that automatic.

## Gestures are app policy

Who decides that a finger held still for 500 ms is a long press? Not the UI
framework. Commit `8f4372f`, "move interaction policy into app", moved those
decisions into `inkpaper-app`. The app turns raw touch samples into gestures with
a small **state machine**:

```rust
/// Movement past this, in either direction, turns a touch into a drag.
const DRAG_THRESHOLD_PX: i32 = 12;

/// How long a touch must stay still to become a long press.
pub const LONG_PRESS_MS: u64 = 500;

#[derive(Debug, Default)]
pub struct TouchGesture {
    active: Option<ActiveTouch>,
}

#[derive(Debug, Clone, Copy)]
struct ActiveTouch {
    origin: Point,
    position: Point,
    pressed_at_ms: u64,
    dragging: bool,
    long_pressed: bool,
}

impl TouchGesture {
    /// The finger has been held still long enough, once per touch.
    pub fn tick(&mut self, now_ms: u64) -> Option<AppInputEvent> {
        let active = self.active.as_mut()?;

        if active.dragging
            || active.long_pressed
            || now_ms.saturating_sub(active.pressed_at_ms) < LONG_PRESS_MS
        {
            return None;
        }

        active.long_pressed = true;

        Some(AppInputEvent::PointerLongPress(active.position))
    }
}
```

`Option<ActiveTouch>` *is* the state machine's top level. `None` means no finger,
and `Some` means a finger is down, with the details in the struct. You can't
read a touch position when no finger is down, because there is no field to read.
That's "make illegal states unrepresentable", the most useful slogan in Rust
design.

`self.active.as_mut()?` converts `&mut Option<ActiveTouch>` to
`Option<&mut ActiveTouch>`, and the `?` operator works on `Option` too: if there
is no active touch, `tick` returns `None` immediately.

The struct takes the time as a *parameter* (`now_ms`) instead of reading a clock.
The firmware passes milliseconds from its timer, the simulator passes
`Instant::now()` converted to milliseconds, and tests pass whatever numbers they
like. **Injecting time** makes time-dependent logic deterministic to test.

The gestures come out as one platform-independent enum:

```rust
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AppInputEvent {
    Previous,
    Next,
    Home,
    PointerDown(Point),
    PointerDrag { origin: Point, position: Point },
    /// The pointer stayed down without moving; see [`crate::TouchGesture`].
    PointerLongPress(Point),
    PointerUp(Point),
    PointerCancel,
}
```

The firmware maps its side buttons to `Previous`/`Next`, and the simulator maps
the arrow keys to the same variants. From there on, the app can't tell them apart.

### Deleting a feature

On 26 September, two commits removed features: `c144865` "remove focus
highlights" and `75dc5fe` "remove focus navigation" (117 lines deleted). Focus
navigation, moving a highlight between buttons with the side buttons, had been
in the framework since day three. But the X4 PRO has a touch screen, and on a
reader the side buttons should turn pages. The framework kept its focus support
(`focus_next` and friends still exist for other devices), and the app stopped
using it. Removing code that doesn't serve the product is as much a part of
building software as writing it.

## Events between entities

By November the app had grown a pattern that hurt. Views like the reader drawer
were separate entities (for partial rebuilds, chapter 9), but most of their
taps had to change state that belonged to the app: close the drawer, open a
screen, change a setting. Until then, each listener in the drawer reached back
with `self.app.update(cx, ...)` and changed app internals directly. The drawer
knew far too much about the app.

Commit `93f403e`, "add typed events that entities emit and their owners subscribe
to", introduced a GPUI-style **emit/subscribe** mechanism. A view declares which
events it can emit with a **marker trait implementation**:

```rust
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum DrawerEvent {
    Close,
    OpenTextSettings,
    ToggleTextSetting(TextSetting),
    ChooseTextOption(TextSetting, usize),
    SelectChapter,
    /// entry `0` of Links and footnotes was chosen
    FollowLink(usize),
}

impl EventEmitter<DrawerEvent> for ReaderDrawer {}
```

It emits them from its own listeners, without knowing who listens:

```rust
fn activate_close(&mut self, _: &ActivateEvent, cx: &mut Context<'_, Self>) {
    cx.emit(DrawerEvent::Close);
}
```

And the owner subscribes once, when it creates the view:

```rust
let reader_menu = cx.new(|_| ReaderDrawer::new(app));
cx.subscribe(reader_menu, Self::on_reader_drawer_event);
```

with a handler that decides what the event *means*:

```rust
pub(super) fn on_reader_drawer_event(
    &mut self,
    _: Entity<ReaderDrawer>,
    event: &DrawerEvent,
    cx: &mut Context<'_, Self>,
) {
    let changed = match *event {
        DrawerEvent::Close => return self.close_reader_menu(cx),
        DrawerEvent::OpenTextSettings => {
            self.reader.close_menu();
            self.open_screen(Screen::TextSettings, cx);
            return;
        }
        DrawerEvent::SelectChapter => return self.show_table_of_contents(cx),
        DrawerEvent::ToggleTextSetting(setting) => self.reader.toggle_text_setting(setting),
        // the page under the drawer repaginates, so it previews the choice
        DrawerEvent::ChooseTextOption(setting, index) => {
            self.reader.choose_text_option(setting, index)
        }
        DrawerEvent::FollowLink(index) => self.reader.follow_page_link(index),
    };

    if changed {
        cx.notify();
    }
}
```

`match *event` matches on the event value. Because `DrawerEvent` is `Copy`,
dereferencing is free and the arms can bind fields by value. Some arms `return`
early. Since `return` has type `!` (never), it fits in any arm, whatever type the
other arms produce. The match as a whole produces a `bool` ("did reader settings
change?") from the remaining arms.

### Compile-time checks at the boundary

The trait bounds make misuse impossible:

```rust
pub fn emit<E>(&mut self, event: E)
where
    T: EventEmitter<E>,
    E: Copy + 'static,

pub fn subscribe<U, E>(&mut self, emitter: Entity<U>, handler: Handler<T, U, E>)
where
    U: EventEmitter<E>,
    E: 'static,
```

A `ReaderDrawer` can't emit a `SettingsEvent`, because there is no `impl
EventEmitter<SettingsEvent> for ReaderDrawer`. You can't subscribe to an event
the emitter doesn't declare either. The marker trait has no methods at all. Its
whole job is to be a *compile-time fact* that bounds can check.

The handler type is a plain function pointer, not a closure:

```rust
/// a subscriber's handler: its state, the emitter and the event
pub(crate) type Handler<T, U, E> = fn(&mut T, Entity<U>, &E, &mut Context<'_, T>);
```

Subscriptions live as long as the runtime, so storing a function pointer avoids
any arena bookkeeping. Methods like `Self::on_reader_drawer_event` coerce to it
directly.

### Small events, checked at compile time

Pending events are copied into a fixed queue, each into a 16-byte payload.
`emit` checks the size *at compile time*:

```rust
pub(crate) fn emit<E>(
    events: &dyn EventStore,
    emitter: EntityId,
    event: E,
) -> Result<(), EventError>
where
    E: Copy + 'static,
{
    const {
        assert!(
            mem::size_of::<E>() <= EVENT_BYTES,
            "an event is at most 16 bytes; send an index or an id instead of larger data"
        );
        assert!(mem::align_of::<E>() <= EVENT_ALIGN);
    }

    let mut payload = EventPayload([MaybeUninit::uninit(); EVENT_BYTES]);
    // SAFETY: the payload is large and aligned enough for `E`, checked above
    unsafe { payload.0.as_mut_ptr().cast::<E>().write(event) };
    // ...
}
```

`const { ... }` is an **inline const block** (stable since Rust 1.79). It is
evaluated at compile time *for each `E` the function is instantiated with*. If
someone emits a 40-byte event, the build fails with that friendly message,
pointing at the problem before the code ever runs. Chapter 9 achieved the same
with an associated constant. Inline `const` is the newer, more direct form.

`E: Copy` matters for safety, too. A `Copy` type has no destructor, so bytes in
the queue can be duplicated or forgotten without leaking anything.

### Delivered after the call

Events don't run handlers immediately. They wait in a queue "until the runtime
call that emitted them returns", in the words of the module documentation:

```rust
/// runs `call`, which runs user code, then delivers the events it emitted unless
/// an outer runtime call still runs, which delivers them when it returns
fn deliver_events_after<R>(&self, call: impl FnOnce() -> R) -> R {
    let depth = CallDepth::enter(&self.call_depth);
    let result = call();
    drop(depth);

    if self.call_depth.get() == 0 {
        emitter::deliver(&self.events, self.runtime_cx());
    }

    result
}
```

Why wait? When the drawer emits `Close` from inside its own listener, the drawer
is **exclusively borrowed** (the trampoline from chapter 5 holds the borrow).
If the app's handler ran right away and tried to reset the drawer, it would hit a
`BorrowConflict`. Delivering after the outermost call returns guarantees every
borrow has been released. `CallDepth` is a drop guard (chapter 3) that decrements
a counter, so nested runtime calls only deliver once, at the outermost level.

## Rust concepts in this chapter

- **Iterators** for tree traversal; `.next().is_some()`.
- **`TypeId` + generics** for an open set of event types.
- **Closures that receive information later** (`impl FnOnce(ElementId) -> E`).
- **`Option` combinators** `filter`, `map` with method paths, `as_mut`, and `?` on
  `Option`.
- **State machines with `Option`**; "make illegal states unrepresentable".
- **Injecting time** for testable logic.
- **Marker traits** as compile-time facts (`EventEmitter<E>`).
- **Function pointer types** with `type` aliases.
- **`match *value`** on `Copy` enums; `return` in match arms and the never type.
- **Inline `const { assert!(...) }`** blocks.
- **Deferred execution** to avoid re-entrant borrows.

## Try it yourself

1. Read `crates/ui/src/runtime/event_tests.rs`. Add a test where an emitter
   emits two events from one listener, and check the order in which the
   subscriber receives them.
2. Add a `[u8; 32]` field to `DrawerEvent::FollowLink` and build. Read the error.
3. Write a unit test for `TouchGesture`: touch at t=0, tick at t=499 (expect
   `None`), tick at t=500 (expect a long press), tick at t=900 (expect `None`
   again).
