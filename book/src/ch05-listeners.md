# 5. Giving a tap meaning: listeners and closures

The reader's settings drawer has a Text tab and a More tab. A tap on More should change the selected tab, leave the book alone, and arrange for the drawer to show its new contents. The button needs behavior that survives longer than the render call that created it—but it must not hold the entire application mutably borrowed while waiting for a finger.

This is where the previous two chapters meet. An entity gives state a durable home. A slot table can store a value whose concrete type the container does not know. A Rust closure is just such a value. InkPaper stores the behavior separately and borrows the target state only when an event actually arrives.

## The state is an argument, not a capture

The drawer's handlers live in `crates/app/src/components/reader_menu.rs`. These two exact methods are shown together, with intervening methods omitted:

```rust,ignore
fn select_tab(&mut self, tab: ReaderMenuTab, cx: &mut Context<'_, Self>) {
    if self.tab != tab {
        self.tab = tab;
        cx.notify();
    }
}

fn activate_more_tab(&mut self, _: &ActivateEvent, cx: &mut Context<'_, Self>) {
    self.select_tab(ReaderMenuTab::More, cx);
}
```

The handler changes `self` and notifies only when the selected tab differs. During rendering, the drawer registers it with `cx.listener(Self::activate_more_tab)` and passes the resulting handle through `ReaderMenuProps`. A menu tab attaches that handle using `on:activate`, the macro spelling of the fluent `.on_activate(...)` API. Chapter 19 covers the macro; the behavior does not depend on knowing its syntax today.

`Self::activate_more_tab` is a method used as a function value. The state is its first argument, rather than something captured from the render call. That is why no long-lived `&mut ReaderDrawer` needs to sit inside the button. The framework supplies a fresh exclusive borrow when it invokes the listener.

The exact public convenience method in `crates/ui/src/context.rs` explains the contract:

```rust,ignore
pub fn listener<E, F>(&mut self, callback: F) -> Listener<E>
where
    E: 'static,
    F: Fn(&mut T, &E, &mut Context<'_, T>) + 'static,
{
    self.try_listener(callback)
        .unwrap_or_else(|error| panic!("could not allocate listener: {error:?}"))
}
```

Here `T` is the context's entity type, `E` is the event type, and `F` is the concrete callable type. The returned `Listener<E>` is a copyable handle, not the closure itself. Its event type makes it difficult to attach unrelated behavior to the wrong sort of event. Its runtime ID locates the stored callback.

The convenient method panics on allocation failure. `try_listener` exposes that failure as a `Result`, including fixed slot exhaustion, byte exhaustion, and unsupported alignment. A callback that captures no data still needs a slot; chapter 4's fixed storage also gives zero-sized values distinct places. “No captures” does not mean “unlimited listeners.”

## What a closure promises about tomorrow

Rust distinguishes callable values by what invocation needs. `FnOnce` permits consuming captured values and therefore guarantees only one call. `FnMut` supports repeated calls with mutable access to the closure. `Fn` supports calls through a shared reference. A callable implementing `Fn` also meets the weaker `FnMut` and `FnOnce` requirements.

InkPaper requires `Fn`. This keeps ordinary mutation in the explicit `&mut T` argument instead of requiring exclusive access to the callback object. It is not an absolute ban on mutable captures: a closure could hold a `Cell` or another interior-mutability type and still implement `Fn`. The useful design discipline is to put application state in entities, where access and notifications have a visible home, rather than hiding it in handler captures.

The `'static` requirement solves a different problem. The callback may run after the current render returns, so it cannot borrow a local string or the temporary `&mut self`. Owning a number, an entity handle, or even a `String` can satisfy `'static`; it means no contained borrow requires a shorter lifetime. An owned string would carry allocation costs, and a `move` closure that captures a borrowed reference still contains that reference. `move` changes capture mode, not the referent's lifetime.

Meanwhile, the references *passed into* the callback are temporary. The `Fn` bound accepts fresh short-lived references on each invocation; the callback is not entitled to retain them. This is the intuition behind higher-ranked lifetimes: the stored callable must work for the borrow durations selected when it is called, not demand that an event or state reference live as long as the callback.

These two directions are easy to confuse. The callback's captures must remain valid while stored. Its arguments need remain valid only for the call. Passing state as an argument lets the framework meet both requirements without a reference-counted pointer in every button.

## Store the callable, remember how to call it

Each closure expression has its own anonymous concrete type. That type contains its captures, and the compiler knows its size and alignment. A function item such as `Self::activate_more_tab` is zero-sized; it can later be coerced to a function pointer, which is a different representation. InkPaper's generic registration does not need to box a trait object merely to retain the callable.

This exact body excerpt from `register_listener` in `crates/ui/src/callback/arena.rs` shows how chapter 4's storage is used:

```rust,ignore
let reservation = store.reserve(
    Layout::new::<F>(),
    owner,
    entity.entity_id(),
    CallbackKind::Listener {
        event_type: TypeId::of::<E>(),
        invoke_fn: invoke_listener_callback::<T, E, F>,
    },
    drop_callback::<F>,
)?;

unsafe { reservation.ptr.cast::<F>().as_ptr().write(callback) };
unsafe { store.commit(reservation.id) };

Ok(Listener::from_id(reservation.id))
```

The slot stores the callable value, its destructor, the target entity, its owner, and enough metadata to invoke it correctly. The specialized `invoke_listener_callback::<T, E, F>` is a *trampoline*: a function that bridges an erased representation back to concrete types.

Before entering it, the caller validates a live callback ID, checks that the callback kind is a listener, and compares the event's `TypeId`. The trampoline then performs this exact body from the same file:

```rust,ignore
let borrow = RawEntityBorrow::acquire(
    runtime.entities,
    target,
    TypeId::of::<T>(),
    BorrowKind::Exclusive,
)?;

let state = unsafe { &mut *borrow.ptr().cast::<T>().as_ptr() };
let event = unsafe { &*event.cast::<E>() };
let callback = unsafe { &*closure.cast::<F>() };
let entity = Entity::<T>::from_id(target);
let mut cx = Context::new_in(entity, runtime);

callback(state, event, &mut cx);

Ok(())
```

The target borrow is exclusive for the entire call. A conflicting access returns an error before another mutable reference is created. The event and closure casts rely on the registration and dispatch checks agreeing about `E` and `F`. This is a connected safety argument: the raw pointers are not safe because a cast looks plausible, but because the exact types and lifetimes were established elsewhere.

With fixed callback storage, this happens without a general-purpose heap allocation for the closure. With heap callback storage, the same mechanism uses allocated places. The design separates dispatch semantics from the storage policy rather than requiring one choice for every platform.

## Behavior belongs to the rendered view

A new render may register new listeners. Keeping every old one would eventually fill a fixed table, while deleting every callback on every partial update would break unchanged buttons. InkPaper therefore distinguishes who *owns* a callback from whose state it *targets*.

This exact metadata definition in `crates/ui/src/callback/arena.rs` records that distinction:

```rust,ignore
#[derive(Clone, Copy)]
struct CallbackMeta {
    /// the entity whose render registered the callback, or the target outside rendering.
    /// The callback is released when its owner renders again
    owner: EntityId,
    target: EntityId,
    kind: CallbackKind,
    /// the arena's generation when the callback was registered. Its id carries it too
    generation: u32,
}
```

The owner is the rendering entity, or the target when registration happens outside rendering. It governs the callback's lifetime. The target governs which state the handler receives. They can differ when one entity constructs behavior targeting another through that target's context.

A full rebuild resets the callbacks. A partial rebuild takes a callback mark before rendering; after successfully rebuilding and compacting the affected frame, it releases older callbacks belonging to stale owners. Newly registered callbacks survive that release, and callbacks belonging to unaffected views stay callable. The released slots may be reused for later registrations.

A callback ID contains both a slot and a generation. Lookup compares the ID with the live slot's stored generation, rejecting stale handles rather than interpreting them as a newly placed closure. This is a finite generation counter, not an eternal uniqueness guarantee: the implementation advances a `u32` with wrapping arithmetic. Most importantly for callers, copying a `Listener` does not extend its validity. It is a view-bound handle, not a permanent subscription.

A separate subscription mechanism handles entity-to-entity events. For example, closing the reader drawer emits `DrawerEvent::Close`; subscribers receive it after the current runtime call returns. That delay lets the listener's mutable entity borrow end before subsequent handlers run. Canvas callbacks also use the callback arena, but receive shared `&T` access for painting rather than the listener's `&mut T` access for behavior.

Finally, not every row needs its own closure. `ActivateEvent` carries an element ID, and `index()` extracts its numeric part when representable as `usize`. The drawer's text rows share a listener; `activate_text_row` uses `index().and_then(...)` with `TextSetting::PANEL.get(index)`. Missing or out-of-range indices return early. This trades a small explicit dispatch step for fewer stored callbacks, while keeping row identity separate from closure identity.

## Try it: trace two taps without a screen

Optional, read-only: open `ReaderDrawer::activate_more_tab` and `select_tab` in `crates/app/src/components/reader_menu.rs`. Assume the drawer starts on Text. Trace one More activation, then a second More activation.

The expected result is one tab assignment and one notification: the second invocation still runs, but the equality check avoids another state change. Then find `on_more_tab: cx.listener(Self::activate_more_tab)` in the same file and follow the prop to `MenuTab`'s `on:activate`. You should be able to identify the stored behavior, the copyable handle, the target state, and the moment that state is borrowed. No hardware, unsafe changes, or new tests are needed to follow the complete path from a button to a visible decision.
