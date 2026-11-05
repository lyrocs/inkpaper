# 10. From a finger to an intention

You touch the drawer's close button, hesitate, slide away, and lift your finger. The drawer should remain open. If you instead press and release over the same control, it should close. If a long press opens something else, lifting your finger should not also perform an ordinary tap.

Those expectations sound simple because the interface hides several decisions. A device reports samples. Gesture logic interprets movement and time. The UI runtime finds an eligible element and manages its pressed state. A listener turns activation into an application intention, and the owner of that state decides what to change.

InkPaper keeps these layers distinct. That lets a desktop mouse and a touch controller share interaction policy without requiring the UI framework to know what closing a reader drawer means. This session follows that path through current source excerpts; none of the excerpts is a standalone program.

## A touch sample is not a tap

`TouchGesture` in `crates/app/src/gesture.rs` contains an `Option<ActiveTouch>`. `None` means no active finger; `Some` holds the origin, current position, press time, and flags for dragging and long pressing. The first sample produces `PointerDown`. Later samples can produce a drag when movement reaches the threshold of 12 pixels on either axis. Release produces `PointerUp`, including after a long press. Deciding whether release should activate anything belongs to the routing layer.

Time is supplied as milliseconds, rather than read from a clock inside the state machine. This exact method recognizes a stationary hold:

```rust,ignore
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
```

`as_mut()` borrows the state inside the option without taking it out. The `?` operator works with `Option` here: no active touch returns `None` immediately. `LONG_PRESS_MS` is 500. After emitting once, the flag suppresses further long presses from that touch. Small movement below the drag threshold can update the reported position without turning the gesture into a drag; “held still” means within the policy's tolerance, not mathematically motionless.

Injecting time makes the boundary cases inspectable without waiting half a second or scheduling real input. A platform must still call `tick` while a finger is down. Merely storing a timestamp does not create a timer.

The resulting `AppInputEvent` vocabulary includes pointer events and higher-level inputs such as `Previous`, `Next`, and `Home`. The application decides their meaning in its current screen. Gesture thresholds are app policy rather than universal truths about UI components; another product could reasonably want different values or gesture rules.

## Find the thing that can respond

A coordinate is not yet a target. The drawer may cover a book page, some nodes are decorative, and a control might listen for activation but not long presses. Hit testing therefore uses both visual geometry and the requested event type.

This exact function in `crates/ui/src/interaction/input/mod.rs` walks the frame's visual nodes:

```rust,ignore
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

The visual iterator includes clipping and scrolling. It follows paint order, so retaining the last eligible match selects the topmost matching node. That qualification matters: a decorative overlay with no listener for this event is not automatically an input barrier. Application routing and appropriate bindings must express modal behavior. “Last painted wins” alone would overstate what the function does.

`TypeId` distinguishes event types. The application defines `LongPressEvent`; the generic UI framework need not add it to a central enum. The lookup asks whether the node has a callback of that particular type, using `.next().is_some()` to test for at least one matching listener.

The runtime then constructs the event after it knows the target. This exact method is in `crates/ui/src/runtime/mod.rs`:

```rust,ignore
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

The closure receives the element's local ID and builds an `E`. `FnOnce` allows that factory to consume captured values because it need only run once. `E: 'static` rules out event types containing short-lived borrowed references; it does not mean every event value must live forever. The result separates “nothing handled this” from an invocation error.

## Keep the press and release together

Ordinary activation has a lifecycle rather than being a direct dispatch on every release. `begin_activation_at` records the resolved element identity and applies pressed styling. `complete_activation_at` compares the release target's identity with the remembered one, clears pressed state, and activates only if they match. `cancel_activation` clears the pending press without running activation.

The comparison uses persistent element state identity, not the current arena index from chapter 6. A node can move during rebuilding without becoming a different control. Conversely, releasing on another eligible control must not transfer the original press to it.

Pressed styling produces invalidation through the same machinery as other visual changes. A color-only style can need painting; a style affecting layout can require more. The runtime does not assume every visual response is merely a color swap.

`dispatch_input` in `crates/app/src/input.rs` adds the gesture policy around that lifecycle. A drag cancels activation before the app handles movement. A long press first asks the app whether to ignore, dispatch, or capture it. If a listener handles the long press, or the app captures it itself, activation is canceled and the remainder of the touch is captured. An unhandled long press need not consume an otherwise valid tap.

This is why gesture recognition and activation should not be collapsed into one boolean named `clicked`. They answer different questions: what did the finger do, and is the pending activation still permitted?

## Let a view say what happened

The close button's listener belongs to `ReaderDrawer`, but closing the drawer changes state owned by the app. The view communicates that intention with a typed emitted event. These are two exact, separate fragments from `crates/app/src/components/reader_menu.rs`, shown together; the method sits inside the drawer's implementation:

```rust,ignore
impl EventEmitter<DrawerEvent> for ReaderDrawer {}

fn activate_close(&mut self, _: &ActivateEvent, cx: &mut Context<'_, Self>) {
    cx.emit(DrawerEvent::Close);
}
```

`EventEmitter` is a marker trait: the empty implementation declares that this entity type may emit `DrawerEvent`. Bounds on `emit` and `subscribe` check that relationship. The owner creates the drawer and subscribes with `cx.subscribe(reader_menu, Self::on_reader_drawer_event)` in `crates/app/src/app/mod.rs`. Its handler in `app/reader.rs` interprets `Close`, text-setting choices, chapter selection, and link selection.

This does not make the drawer entirely independent of the app. The current drawer still holds an app entity and reads settings and links from it. The event boundary specifically separates reporting a choice from directly performing every owner mutation. It gives the owner one place to decide what that choice means.

Emitted entity events are also different from coordinate-targeted input. They need no hit test; they identify their emitter and reach subscribers. Events are small `Copy` values stored in a queue, with compile-time size and alignment checks: at most 16 bytes and alignment at most eight. Larger information should be referenced by an ID or index, rather than embedded in the payload. Queue capacity remains finite; `try_emit` returns an error when emission fails, while `emit` panics on that error.

Delivery is deferred until the surrounding runtime call finishes. This exact helper from `crates/ui/src/runtime/mod.rs` shows the boundary:

```rust,ignore
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

While a drawer listener runs, its entity state is mutably borrowed. Immediately calling an owner handler that updates the drawer could collide with that borrow. Waiting for the outer call to return gives that listener's borrow a chance to end. The depth guard handles nested runtime calls. This is controlled synchronous delivery after a boundary, not an asynchronous task or a general cure for every possible borrowing conflict.

## Try it: hold, then release

For an optional hardware-free experiment, read `a_still_touch_long_presses_once_after_the_delay` in `crates/app/src/gesture.rs`. Trace a touch starting at 1,000 ms: the tick at 1,499 returns `None`, the tick at 1,500 produces `PointerLongPress`, and a later tick returns `None` again. Release still produces `PointerUp`.

Now follow that release through `dispatch_input` after a handled long press. The routing layer has canceled activation and captured the touch, so the later release must not run the ordinary tap action. The observable outcome is the sequence of returned variants and selected branches. It demonstrates why raw samples, recognized gestures, activation listeners, and emitted intentions are four useful boundaries rather than four names for the same event.
