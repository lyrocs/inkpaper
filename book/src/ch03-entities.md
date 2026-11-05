# 3. Entities: state without `Rc<RefCell<T>>`

> **Where we are:** commits `d3cf9df` ("add render context and typed entities"),
> `496fe14` ("no alloc entity arena") and `45c3416` ("add entity context and
> state access API"), 7–8 August 2026. We also look at a much later refinement,
> `ce5b536` (5 November), which made entity access panic by default and added
> `try_` variants.

## The problem: who owns the state?

Chapter 2 built trees of elements. Those trees are **temporary**. They are built,
turned into a frame, and thrown away, many times per second if needed. But a UI
also has state that must survive between frames: which tab is open, the current
battery percentage, the page you are on.

In most GUI toolkits, a widget owns its state, and widgets that need to talk to
each other share it through reference-counted pointers. In Rust that usually
looks like `Rc<RefCell<State>>`: `Rc` for shared ownership, `RefCell` so you can
still mutate through a shared reference.

InkPaper's design document rules that out. `Rc` needs a heap. Worse, in a big
graph of `Rc`s it is easy to create a cycle that never gets freed, and hard to
know how much memory the UI uses. The document chooses another model, again
borrowed from GPUI: **entities**.

## An entity is a typed handle

Here is `Entity<T>` as it appeared in commit `d3cf9df`. It hasn't changed much
since:

```rust
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct EntityId {
    slot: u16,
    generation: u16,
}

pub struct Entity<T> {
    id: EntityId,
    _marker: PhantomData<fn() -> T>,
}
```

An `Entity<T>` does **not** contain a `T`. It holds a 4-byte id that says where
the runtime stores the `T`. Think of it like a database row key. The runtime owns
every piece of state. Everyone else holds keys.

That has big consequences:

- An entity handle is tiny and `Copy`. You can store it in as many places as you
  like.
- Cycles are harmless: two entities can hold each other's handles, and nothing
  leaks, because the handles don't own anything.
- The total memory for state is whatever the runtime's storage holds. You can
  bound it, which chapter 4 does.

### What is `PhantomData<fn() -> T>` for?

The struct never stores a `T`, but we still want `Entity<Counter>` and
`Entity<Battery>` to be different types, so the compiler stops us from mixing
them up. A struct must use each of its type parameters somewhere, though, so
`struct Entity<T> { id: EntityId }` alone doesn't compile.

`PhantomData<X>` is a zero-sized marker that tells the compiler "act as if this
struct contained an `X`". The choice of `X` matters:

| Marker | Acts as if it owns a `T`? | `Send`/`Sync` follow `T`? |
|--------|---------------------------|---------------------------|
| `PhantomData<T>` | yes (drop check considers `T`) | yes |
| `PhantomData<&'a T>` | borrows a `T` | yes |
| `PhantomData<fn() -> T>` | no | no, always `Send + Sync` |

`fn() -> T` is the type of a function pointer that *returns* a `T`. A function
pointer owns nothing and is always thread-safe, so `Entity<T>` is too, whatever
`T` is. That is the honest description of a handle: it only knows how to *get* a
`T` from somewhere else.

### Why implement `Copy` and `Clone` by hand?

The obvious thing would be `#[derive(Clone, Copy)]`. But derive adds bounds on
type parameters: it would generate `impl<T: Clone> Clone for Entity<T>`. Then
`Entity<Counter>` would be cloneable only if `Counter` were, which makes no sense
for a handle. So the code writes the impls by hand, with no bound on `T`:

```rust
impl<T> Copy for Entity<T> {}

impl<T> Clone for Entity<T> {
    fn clone(&self) -> Self {
        *self
    }
}

impl<T> PartialEq for Entity<T> {
    fn eq(&self, other: &Self) -> bool {
        self.id == other.id
    }
}
```

This is a common Rust gotcha. Whenever a generic struct only *mentions* `T`
through a marker, check whether derived impls add bounds you don't want.

### Generations

`EntityId` holds a `slot` and a `generation`. Many arenas reuse a freed slot for a
new value and bump its generation, so an old id with the old generation is
recognisably stale. That is the classic *generational index* pattern. InkPaper's
current entity arena never reuses slots, so its comment says "entity ids always
have generation 0", but the field keeps the door open. Callback ids, which *are*
recycled every frame, use a 32-bit generation for exactly this purpose
(chapter 5).

## Contexts: how you reach the runtime

If entities are keys, something has to turn a key into data. That is the job of
a **context**:

```rust
pub struct Context<'a, T> {
    pub(crate) entity: Entity<T>,
    pub(crate) runtime: RuntimeCx<'a>,
}
```

A `Context<'a, T>` is "a borrow of the runtime, plus the identity of the entity
`T` we are currently working on". The lifetime `'a` ties it to the runtime
borrow, so you can't keep a context around after the runtime call that created it
returns. The compiler won't let it escape.

The framework hands you a context whenever your code runs. The central trait is:

```rust
pub trait Render: Sized + 'static {
    fn render<'a>(&'a mut self, cx: &mut Context<'_, Self>) -> impl IntoElement + 'a;
}
```

Read the signature slowly, it holds a lot:

- `&'a mut self`: rendering may change the entity's own state.
- `cx: &mut Context<'_, Self>`: a context typed with *this* entity's type.
- `-> impl IntoElement + 'a`: **return-position `impl Trait`**. The function
  returns *some* type that implements `IntoElement`, and the caller doesn't need
  to know which. That is how a render can return the huge nested `Div<Push<...>>`
  type from chapter 2 without anyone writing it out.
- `+ 'a`: the returned tree may borrow from `self`. A label can borrow a `&str`
  straight out of the state, with no copy.
- `'static` on the trait: an entity's type can't contain non-`'static`
  borrows. The runtime keeps the value indefinitely, so it can't hold references
  that might expire.

## Creating entities

Entities are created through a context or the runtime:

```rust
impl<'a, T> Context<'a, T> {
    pub fn new<U>(&mut self, build: impl FnOnce(&mut Context<'_, U>) -> U) -> Entity<U>
    where
        U: 'static,
    {
        match self.try_new(build) {
            Ok(entity) => entity,
            Err(error) => panic!("could not allocate entity: {error:?}"),
        }
    }
}
```

`build` is a **closure** that produces the initial state. It is an `FnOnce`
because it runs exactly once. It receives the *new* entity's own context, which
lets the new entity create child entities or listeners while it is being built.

Here is the real `InkPaperApp::new`, slightly shortened:

```rust
pub fn new(cx: &mut Context<'_, Self>) -> Self {
    let battery = cx.new(|_| BatteryState::default());
    let battery_indicator = cx.new(|_| BatteryIndicator::new(battery));
    let frontlight = cx.new(|_| FrontlightState::default());
    let frontlight_panel = cx.new(|_| FrontlightPanel::new(frontlight));
    let app = cx.entity();
    let reader_menu = cx.new(|_| ReaderDrawer::new(app));
    cx.subscribe(reader_menu, Self::on_reader_drawer_event);
    // ...
    Self { battery, battery_indicator, frontlight, frontlight_panel, reader_menu, /* ... */ }
}
```

Notice `ReaderDrawer::new(app)`: the drawer gets a handle back to the app that
owns it. With `Rc` that would be a reference cycle. With entities it's just two
ids.

And the platform creates the root once, at startup (from `crates/sim/src/main.rs`):

```rust
let app = runtime
    .create_root(InkPaperApp::new)
    .expect("InkPaper application root must fit");
```

`InkPaperApp::new` is passed *as a function*. A function item implements `FnOnce`
like a closure does, so you don't need to write `|cx| InkPaperApp::new(cx)`.

### Building safely when the closure can fail

Look at how `create_entity` actually stores the value:

```rust
pub(crate) fn create_entity<T>(
    runtime: RuntimeCx<'_>,
    build: impl FnOnce(&mut Context<'_, T>) -> T,
) -> Result<Entity<T>, EntityAllocError>
where
    T: 'static,
{
    let store = runtime.entities;
    let reservation = store.reserve(Layout::new::<T>(), TypeId::of::<T>(), drop_value::<T>)?;

    let entity = Entity::from_id(reservation.id);

    let guard = ReservationGuard {
        store,
        entity: reservation.id,
        armed: true,
    };

    let mut cx = Context::new_in(entity, runtime);

    let value = build(&mut cx);

    unsafe { reservation.ptr.cast::<T>().as_ptr().write(value) };

    guard.commit();

    Ok(entity)
}
```

It happens in three steps. First, *reserve* a slot big enough for a `T`. Then
run the user's `build` closure. Finally, *write* the value and *commit*.

What if `build` panics? The slot has been reserved but holds no valid `T`. That
is what `ReservationGuard` is for. It is a **drop guard**: a value whose `Drop`
implementation cleans up if we leave the function early. If `build` panics, Rust
unwinds the stack, drops `guard`, and the guard marks the slot as abandoned.
`commit()` disarms it on the success path. The pattern shows up constantly in
systems code: put the cleanup in a destructor so that every exit path runs it.

We cover the `unsafe` line in chapter 4.

## Reading and updating

With a handle and a context you can reach the state. Here is how the header's
battery indicator reads the battery entity:

```rust
pub(crate) struct BatteryIndicator {
    battery: Entity<BatteryState>,
}

impl Render for BatteryIndicator {
    fn render<'a>(&'a mut self, cx: &mut Context<'_, Self>) -> impl IntoElement + 'a {
        let battery = self.battery.read(cx, |battery| battery.get());

        BatteryLabel::from(BatteryLabelProps { battery })
    }
}
```

And here is how the app writes it when a new reading arrives:

```rust
pub fn apply_battery_status(&mut self, battery: BatteryStatus, cx: &mut Context<'_, Self>) {
    self.battery.update(cx, |state, cx| {
        if state.set(battery) {
            cx.notify();
        }
    });
    // ...
}
```

Both APIs take a **closure** instead of returning a reference. Why not
`fn get(&self) -> &T`?

Because the runtime has to know when the borrow *ends*. With a closure, the borrow
starts when `read` calls `f` and ends when `f` returns, and the runtime can
record both moments. A returned reference could be kept for an arbitrary time,
and the runtime couldn't stop someone else from mutating the same entity in the
meantime. The closure style is how InkPaper gets `RefCell`-like dynamic borrow
checking without `RefCell`. Chapter 4 shows the bookkeeping.

`cx.notify()` tells the runtime this entity changed and must render again. The
battery only notifies when the *visible* value changed (`state.set` returns
`bool`), so a reading that moves from 73.4% to 73.5% doesn't repaint the screen.
On e-paper, every avoided refresh counts.

### Panicking versus `try_`

For three months these methods returned `Result<R, EntityAccessError>`, and
every call site had `.ok()`, `?` or `if let Ok(...)` around it. Commit `ce5b536`
changed the default:

```rust
/// # Panics
///
/// when the entity is gone, is still being built, or is being updated. See
/// [`Self::try_read`]
#[track_caller]
pub fn read<C, R>(self, cx: &Context<'_, C>, f: impl FnOnce(&T) -> R) -> R
where
    C: 'static,
{
    match self.try_read(cx, f) {
        Ok(value) => value,
        Err(error) => panic!("could not read entity: {error:?}"),
    }
}
```

The reasoning: inside a correct program these errors can't happen. An entity
read during render is always alive, and you can't create a borrow conflict
without a bug. Making every caller handle an impossible error just adds noise.
Most of the standard library works the same way. `vec[i]` panics, `vec.get(i)`
returns an `Option`, and you pick whichever fits.

`#[track_caller]` is a small gem. Without it, a panic inside `read` would report
the line inside `entity/mod.rs`. With it, the panic message points at *your* line
that called `read`. Use it on any function whose panics are really the caller's
fault.

The **`# Panics`** heading in the doc comment is a Rust convention: every public
function that can panic should say when. Clippy can enforce it
(`missing_panics_doc`).

## Globals

Some state belongs to nobody in particular: a theme, the fonts. For that the
runtime offers **globals**, at most one value per type:

```rust
pub fn global<G>(&self) -> GlobalRef<'a, G>
where
    G: Global,
{
    self.try_global::<G>()
        .unwrap_or_else(|_| panic!("requested global is not available"))
}
```

`Global` is a **marker trait**. A type opts in with `impl Global for Theme {}`,
and the runtime keys the storage by the type's `TypeId`. `GlobalRef<'a, G>` is a
*guard*: it implements `Deref<Target = G>` so it acts like a `&G`, and it holds
an inner borrow token whose `Drop` releases the borrow. That is the same pattern as `std::cell::Ref`.

## Rust concepts in this chapter

- **Handles instead of pointers**: `Entity<T>` as a typed key into a store.
- **`PhantomData`**, and how the choice of `PhantomData<fn() -> T>` affects
  ownership and auto traits.
- **Manual `Copy`/`Clone`/`PartialEq` impls** to avoid derive's bounds.
- **Generational indices**.
- **Lifetimes on structs** (`Context<'a, T>`), and lifetimes connecting a method's
  input and output (`&'a mut self` → `impl IntoElement + 'a`).
- **Return-position `impl Trait`**.
- **Closures**: `FnOnce`, passing function items as closures, closure-based access
  APIs.
- **Drop guards** for cleanup on every exit path, including panics.
- **`panic!` versus `Result`**, `try_` variants, `#[track_caller]`, `# Panics` docs.
- **Marker traits** and `Deref` guards.

## Try it yourself

1. In `crates/ui/src/runtime/mod.rs`, the tests module defines a `Counter`
   entity. Write a test that creates two counters, each holding the other's
   `Entity` handle. Confirm it compiles and nothing leaks. Why couldn't you do
   this with `Rc<Counter>` without leaking?
2. Write `fn assert_send<T: Send>() {}` in a test and call
   `assert_send::<Entity<std::rc::Rc<u8>>>()`. It compiles. Now change the marker
   to `PhantomData<T>` and try again. Explain the difference.
3. `Runtime::update` (in `crates/ui/src/runtime/mod.rs`) still returns a
   `Result`. Find where the firmware calls it in
   `firmware/x4-pro/src/firmware/mod.rs`. Why does the firmware log a warning
   instead of panicking there?
