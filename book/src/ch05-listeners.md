# 5. Listeners: storing closures without `Box`

> **Where we are:** commits `f309c6d` ("add fixed capacity typed listener arena")
> and `b592d3e` ("add typed click interactions"), 8 August 2026, then
> `e655da0` ("add entity-bound canvas callbacks", 13 August), `02973df` ("pass
> the activated element id to activate listeners", 27 September) and `0bf3c30`
> ("release callbacks by owning entity", 4 October).

## The problem: behaviour, not just data

A button has to *do* something when you tap it. In most Rust GUI code, that
something is a closure stored as `Box<dyn Fn(...)>`. A `Box` needs a heap, and our
fixed-storage runtime doesn't have one.

There is a second, subtler problem. A closure that changes the app's state needs
access to that state. If the closure captured `&mut app`, it would hold a
mutable borrow for as long as the closure exists, which is forever, and nothing
else could touch the app. Capturing an `Rc<RefCell<App>>` brings us back to the
heap and to runtime borrow panics.

InkPaper answers both problems with one idea, again inherited from GPUI: **a
listener is a closure stored in an arena and bound to an entity**. When an event
arrives, the runtime borrows the entity and *passes* `&mut state` to the closure as
an argument. The closure itself captures nothing but cheap, `Copy` data.

## What it looks like to the user

Here is how the reader's drawer wires its buttons, in
`crates/app/src/components/reader_menu.rs` (shortened):

```rust
impl ReaderDrawer {
    fn activate_text_tab(&mut self, _: &ActivateEvent, cx: &mut Context<'_, Self>) {
        self.select_tab(ReaderMenuTab::Text, cx);
    }

    fn activate_close(&mut self, _: &ActivateEvent, cx: &mut Context<'_, Self>) {
        cx.emit(DrawerEvent::Close);
    }
}

impl Render for ReaderDrawer {
    fn render<'a>(&'a mut self, cx: &mut Context<'_, Self>) -> impl IntoElement + 'a {
        // ...
        let menu = ReaderMenu::from(ReaderMenuProps {
            tab: self.tab,
            on_close: cx.listener(Self::activate_close),
            on_text_tab: cx.listener(Self::activate_text_tab),
            // ...
        });
        // ...
    }
}
```

Inside `ReaderMenu`, an element attaches the listener. The menu is written with
the `rsx!` macro from chapter 19, where `on:activate={...}` is shorthand for the
builder call `.on_activate(listener)`:

```rust
on:activate={self.on_close}
```

There are two things to notice.

1. `Self::activate_close` is a plain **method** passed where a closure is
   expected. Methods are functions whose first parameter is `self`, so
   `ReaderDrawer::activate_close` has the type
   `fn(&mut ReaderDrawer, &ActivateEvent, &mut Context<'_, ReaderDrawer>)`.
2. `cx.listener(...)` returns a `Listener<ActivateEvent>`, a small `Copy`
   value that can be passed down through components like any other prop.

## The signature

```rust
impl<T: 'static> Context<'_, T> {
    pub fn listener<E, F>(&mut self, callback: F) -> Listener<E>
    where
        E: 'static,
        F: Fn(&mut T, &E, &mut Context<'_, T>) + 'static,
    {
        self.try_listener(callback)
            .unwrap_or_else(|error| panic!("could not allocate listener: {error:?}"))
    }
}
```

Let's read the bound `F: Fn(&mut T, &E, &mut Context<'_, T>) + 'static`.

### `Fn`, `FnMut` or `FnOnce`?

Rust has three closure traits, and each describes how the closure uses what it
captured:

| Trait | Can be called | May mutate captures | May move captures out |
|-------|---------------|---------------------|-----------------------|
| `FnOnce` | once | yes | yes |
| `FnMut` | many times, needs `&mut` | yes | no |
| `Fn` | many times, `&` is enough | no | no |

Every `Fn` is also `FnMut` and `FnOnce`. A listener may run many times, so it
can't be `FnOnce`. And InkPaper wants **all mutable state in entities**, not
hidden in closure captures, so it requires `Fn`. When a handler needs to change
something, it changes `&mut T`, the entity state it receives as an argument. That
state is visible to the rest of the app, and the runtime can track changes to it.
Captured state would be invisible to both.

### Lifetimes you didn't write

The parameter types contain references with no named lifetime: `&mut T`, `&E`,
`&mut Context<'_, T>`. In a closure *trait bound*, elided lifetimes become
**higher-ranked**. The bound really means

```rust
F: for<'x, 'y, 'z, 'w> Fn(&'x mut T, &'y E, &'z mut Context<'w, T>)
```

"for *every* choice of lifetimes, `F` can be called". The closure must accept
borrows of any duration, so the runtime can pass short-lived borrows each time
it calls it. You rarely write `for<...>` yourself, but it's good to know that
this is what makes the API work.

### Why `'static`?

The closure is stored in the runtime and called later, after the current function
has returned. If it captured a reference to a local variable, that reference
would dangle. `F: 'static` means "`F` contains no borrowed data that could expire".
Closures that capture nothing, or capture only owned `Copy` values like an
`Entity<T>` handle or an index, satisfy it automatically. This is another reason
handles are so convenient: a `'static` closure can capture an `Entity<App>` and
reach the app through it later, and the compiler won't complain.

## Storing a closure in raw bytes

Every closure in Rust has its **own anonymous type**, even two textually
identical ones. That type is just a struct of its captures, and its size is known
to the compiler. A closure that captures nothing, like `Self::activate_close`, is
**zero-sized**.

So the trick from chapter 4 applies directly. Reserve a slot with the closure's
`Layout`, write the closure's bytes into it, and keep a monomorphised function
that knows how to call it:

```rust
pub(crate) fn register_listener<T, E, F>(
    store: &dyn CallbackStore,
    owner: EntityId,
    entity: Entity<T>,
    callback: F,
) -> Result<Listener<E>, CallbackAllocError>
where
    T: 'static,
    E: 'static,
    F: Fn(&mut T, &E, &mut Context<'_, T>) + 'static,
{
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
}
```

The metadata stores:

- the **event type**, as a `TypeId`, so that a `Listener<ActivateEvent>` can
  never be called with some other event;
- **`invoke_fn`**, a *trampoline* function pointer;
- the **target entity**, whose state the closure will receive;
- the **owner**, whose next render will free the closure (more below).

### The trampoline

`invoke_listener_callback::<T, E, F>` is generic over all three types, but once
instantiated it is a plain function with an erased signature:

```rust
unsafe fn invoke_listener_callback<T, E, F>(
    closure: *const u8,
    target: EntityId,
    event: *const u8,
    runtime: RuntimeCx<'_>,
) -> Result<(), ListenerInvokeError>
where
    T: 'static,
    E: 'static,
    F: Fn(&mut T, &E, &mut Context<'_, T>) + 'static,
{
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
}
```

It is the bridge between the untyped world of the arena (`*const u8`) and the
typed world of user code. Inside, it knows `T`, `E` and `F` again, so it can:

1. take an **exclusive** borrow of the target entity, which fails with
   `BorrowConflict` instead of creating two `&mut`;
2. cast the three pointers back to their real types;
3. build a fresh `Context` for the target;
4. call the closure.

The caller side checks the event type before using the trampoline:

```rust
let CallbackKind::Listener {
    event_type,
    invoke_fn,
} = meta.kind
else {
    return Err(ListenerInvokeError::InvalidListener);
};

if event_type != TypeId::of::<E>() {
    return Err(ListenerInvokeError::EventTypeMismatch);
}
```

That is **`let ... else`**, stabilised in Rust 1.65: destructure a value with a
pattern, and if the pattern doesn't match, run the `else` block, which must
leave the function with `return`, `break`, `continue` or a panic. It keeps the
happy path flat instead of nesting it inside `if let`. InkPaper uses it
everywhere.

## Typed handles again

`Listener<E>` mirrors `Entity<T>`:

```rust
pub struct Listener<E> {
    pub(crate) id: CallbackId,
    _event: PhantomData<fn(&E)>,
}
```

This time the marker is `PhantomData<fn(&E)>`, a function that *takes* an `E`.
That is the honest description of a listener: it *consumes* events of type `E`.
The choice also affects **variance**, which controls when a
`Listener<Something<'long>>` may be used as a `Listener<Something<'short>>`. Our
events are `'static`, so in practice it doesn't matter here. The point is that
the marker is chosen to describe the relationship, not picked at random.

## When do listeners die?

Listeners are created *during render*, and a render can happen many times. If
each render added new closures and nothing removed the old ones, the arena would
fill up within minutes.

A full rebuild still clears every callback before the frame is rendered again. Callback ids carry a generation precisely for that. A `Listener` held
from an older frame has a stale generation, and the arena rejects it instead of
calling whatever closure now sits in that slot:

```rust
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct CallbackId {
    slot: u16,
    generation: u32,
}
```

When partial rebuilds arrived in October (chapter 9), "clear everything each
frame" no longer worked: an entity that does *not* render again keeps its
existing nodes, and those nodes still point at its listeners. So commit `0bf3c30`
gave each callback an **owner**, the entity whose render registered it:

```rust
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

When the drawer renders again, only the drawer's callbacks are released. Later,
commit `5ebdab8` ("reuse the callback slots a partial rebuild releases") made new
registrations refill those freed slots (`reserve_reusing` in the slot table)
instead of growing forever.

The owner and the target can differ. A parent component may render a listener
that targets a child entity. The owner decides *lifetime*, and the target decides
*whose state* the closure receives.

## Canvases: callbacks for painting

The same mechanism stores a second kind of closure: **canvas callbacks**, which
paint directly instead of producing elements. The reader page is drawn this way,
because it's one big block of positioned text and laying it out as hundreds of
`div`s would be wasteful:

```rust
cx.canvas(|app: &InkPaperApp, paint| app.paint_reader_page(paint))
```

Its bound is `F: Fn(&T, &mut PaintCx<'_>) + 'static`, a **shared** `&T` this time,
because painting must not change state. The callback kind enum distinguishes the
two:

```rust
pub enum CallbackKind {
    Listener {
        event_type: TypeId,
        invoke_fn: ListenerInvokeFn,
    },
    Canvas {
        invoke_fn: CanvasInvokeFn,
    },
}
```

`try_canvas` also records that the render *read* its target entity, so changing the
entity repaints the canvas. That's the dependency tracking we meet in chapter 9.

## One listener, many buttons

Early on, `ActivateEvent` carried nothing. That made lists awkward: a list of 30
chapters needed 30 listeners, all slight variations of the same closure. Commit
`02973df` put the activated element's id into the event:

```rust
/// An element was activated. It carries the element's id, so one listener can
/// serve many elements, such as every row of a list or every key of a keyboard.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ActivateEvent {
    id: ElementId,
}

impl ActivateEvent {
    /// The number in the activated element's id, such as 3 for `("row", 3)`. `None`
    /// when the number doesn't fit a `usize`
    pub fn index(self) -> Option<usize> {
        match self.id {
            ElementId::Value(value) | ElementId::NamedValue(_, value) => {
                usize::try_from(value).ok()
            }
            ElementId::Name(_) => None,
        }
    }
}
```

Now a single handler serves every row:

```rust
fn activate_text_row(&mut self, event: &ActivateEvent, cx: &mut Context<'_, Self>) {
    let Some(&setting) = event
        .index()
        .and_then(|index| TextSetting::PANEL.get(index))
    else {
        return;
    };
    // ...
}
```

`Option::and_then` chains two fallible steps (is there an index? is it in
range?), `slice::get` returns `None` instead of panicking when out of range, and
`let ... else` exits if either step fails. Three lines, no panics, no nesting.
Notice also the `|` in the `match` arm: `ElementId::Value(value) |
ElementId::NamedValue(_, value)` binds `value` from either variant, which works
because both bind the same name with the same type.

## Rust concepts in this chapter

- **Closure traits** `Fn`, `FnMut`, `FnOnce`, and why `Fn` fits event handlers.
- **Methods as function values** (`Self::activate_close`).
- **Higher-ranked trait bounds** (`for<'a>`), implied by elided lifetimes in
  `Fn(...)` bounds.
- **`'static` bounds** on stored closures.
- **Closure types are unique and sized**, and zero-sized when they capture nothing.
- **Trampolines**: monomorphised `unsafe fn` pointers that restore erased types.
- **`let ... else`**, `Option::and_then`, `slice::get`, or-patterns in `match`.
- **Variance** and choosing `PhantomData<fn(&E)>`.
- **Generational ids** to reject stale handles.

## Try it yourself

1. Write a closure that captures a `&str` borrowed from a local `String` and pass
   it to `cx.listener` in a test. Read the error about `'static`. Then capture an
   owned `u32` instead and watch it compile.
2. Change the bound in `Context::listener` from `Fn` to `FnMut`. Which line in
   `invoke_listener_callback` stops compiling, and why? (Hint: what kind of
   reference to the closure does it create?)
3. Print `core::mem::size_of_val(&ReaderDrawer::activate_close)` and the size of a
   closure capturing an `Entity<_>`. Relate the numbers to the `Layout` passed to
   `reserve`.
