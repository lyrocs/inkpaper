# 6. From elements to a frame tree

> **Where we are:** commits `d597d3d` ("add fixed capacity frame tree lowering"),
> `233876e` ("expand renderable entities into frame tree"), `46abc7e` ("add
> persistent element identity resolution") and `ff9c003` ("add runtime frame
> lifecycle"), 8 August 2026, the second day of the project.

## Two kinds of tree

So far we have two things:

- **Elements**: the strongly typed, nested values built by `div().child(...)`.
  They are great for writing UIs: the compiler checks everything, and nothing
  allocates. But every element tree has a *different type*, so you can't store
  one in a field, walk it with a loop or keep it between frames.
- **Entities**: persistent state that knows how to *render* elements.

Layout, hit testing and painting all need a third thing: one uniform data
structure that you can walk in a loop, measure, annotate with positions and
inspect when a tap arrives. InkPaper calls it the **frame**, and turning
elements into a frame is called **mounting** (the early commits said
"lowering", a compiler term for turning a high-level representation into a
lower-level one).

```text
Render::render()  ──►  element tree (typed, temporary)
                              │ Element::mount()
                              ▼
                       frame tree (uniform, indexed)
                              │
               ┌──────────────┼──────────────┐
               ▼              ▼              ▼
            layout       hit testing       paint
```

## A node is an enum

Every frame node has the same Rust type, and an enum carries the differences:

```rust
#[derive(Debug, Clone, Copy)]
pub(crate) enum NodeKind {
    Div { style: Style },
    Text { text: TextRange },
    Image { source: ImageSource, style: ImageStyle },
    Svg { source: SvgSource, style: SvgStyle },
    Canvas { draw: CanvasDraw, style: CanvasStyle },
    Entity {
        entity: EntityId,
        render: EntityRenderFn,
        expanded: bool,
    },
}
```

This is the standard way to give a closed set of things one type in Rust. A
`match` on `NodeKind` must handle every variant, so when the SVG variant was added
in September, the compiler listed every place that needed updating. Compare this
with chapter 2's open-ended `Element` trait, which anyone can implement. Traits
are good for *open* sets, and enums for *closed* ones. Elements are open (apps
define components), while frame nodes are closed (only the framework knows how to
lay out and paint).

Notice `#[derive(Clone, Copy)]` on `NodeKind`. Every variant holds only small
`Copy` data. Text in particular is not a `&str` or a `String` but a `TextRange`:

```rust
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct TextRange {
    start: usize,
    len: usize,
}
```

The frame keeps one big byte buffer for *all* text, and a text node just remembers
its slice of it. That keeps nodes small and `Copy`, gives text one bounded pool of
memory, and avoids lifetimes on the frame. A frame that contained `&'a str` would
need `'a`, and so would everything that touches it.

## Indices instead of pointers

Here is a node:

```rust
#[derive(Debug, Clone, Copy)]
pub(crate) struct Node {
    pub(crate) kind: NodeKind,
    pub(crate) parent: Option<NodeId>,
    pub(crate) first_child: Option<NodeId>,
    pub(crate) last_child: Option<NodeId>,
    pub(crate) next_sibling: Option<NodeId>,
    pub(crate) element_id: Option<ElementId>,
    pub(crate) layout: NodeLayout,
    // ...
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct NodeId(u16);
```

A tree needs links between nodes: parent, children, siblings. In C you would use
pointers. In Rust, a tree where children point to parents *and* parents point to
children fights the ownership model: who owns whom? The usual options are
`Rc<RefCell<Node>>` with `Weak` back-pointers (heap, runtime checks, verbosity) or
`unsafe` raw pointers.

InkPaper uses the third option, the one most Rust projects with complex graphs
settle on: **store all nodes in one vector and link them with indices**. A
`NodeId` is a `u16` index into that vector. Because all links are plain numbers:

- nodes are `Copy` and the whole tree can be cleared in O(1);
- there is exactly one owner, the frame's vector, and the borrow checker is happy;
- a `u16` link is 2 bytes instead of 4 or 8 for a pointer, and
  `Option<NodeId>` is 4.

The children are stored as a **singly linked list**: `first_child`, then
`next_sibling` from child to child. `last_child` exists so appending is O(1). This
"first child, next sibling" layout represents any tree with a fixed number of
fields per node, which is exactly what fixed-capacity storage needs.

Walking the children of a node is a loop over `Option<NodeId>`:

```rust
let mut current = self.node(parent).first_child;

while let Some(child) = current {
    // ... use child ...
    current = self.node(child).next_sibling;
}
```

`while let` keeps looping as long as the pattern matches. You'll find this loop in
many places in the layout code.

## Mounting

Mounting goes through `MountCx`, the mount context:

```rust
pub struct MountCx<'a> {
    frame: &'a mut dyn FrameStore,
    app: AppContext<'a>,
}
```

`&'a mut dyn FrameStore` is a **trait object**: a reference to *some* type
that implements `FrameStore`, chosen at runtime. Why `dyn` here when the rest of
the framework is so generic? Because `FrameArena<F>` is generic over its storage
`F`. If `MountCx` were generic too, every `Element::mount` implementation in every
app crate would have to be generic over the storage type, and each element would
be compiled once per storage. The trait object hides that parameter behind a
vtable. You pay one indirect call per node pushed, and in exchange the element
API stays simple and the code stays small. That's a good trade on a
microcontroller, where code size matters.

The `FrameStore` trait lists what an element can do while mounting:

```rust
pub(crate) trait FrameStore {
    fn push_div(&mut self, style: Style) -> Result<NodeId, MountError>;
    fn push_text(&mut self, text: &str, style: TextStyle) -> Result<NodeId, MountError>;
    fn push_entity(&mut self, entity: EntityId, render: EntityRenderFn)
        -> Result<NodeId, MountError>;
    fn append_child(&mut self, parent: NodeId, child: NodeId);
    fn identify(&mut self, node: NodeId, id: ElementId);
    // ...
}
```

And here is how a text node is pushed:

```rust
fn push_text(&mut self, text: &str, style: TextStyle) -> Result<NodeId, MountError> {
    let start = self.text.len();

    self.text
        .extend_from_slice(text.as_bytes(), MountError::TextStorageFull)?;

    let range = TextRange {
        start,
        len: text.len(),
    };

    let node = match self.push_node(NodeKind::Text { text: range }) {
        Ok(node) => node,
        Err(error) => {
            self.text.truncate(start);
            return Err(error);
        }
    };

    self.node_mut(node).text_style = style;

    Ok(node)
}
```

Look at the error path. The text is copied first. If the node table is full,
`truncate(start)` rolls the text buffer back, so a failed mount leaves no garbage
behind. This is the "strong exception guarantee" from C++: an operation either
fully succeeds or leaves the state unchanged. In Rust, with explicit `Result`s, you
can *see* every early return, so it's easy to check that each one cleans up.

Also note `text.len()`, which counts **bytes**, not characters. Rust strings are
UTF-8, and `str::len` is the byte length. `TextRange` stores byte offsets, which is
what slicing (`&text[start..start + len]`) needs. Chapter 17 is all about the
difference between bytes, characters and what a reader would call a "letter".

### Building the links

`append_child` turns two node ids into a parent/child relation:

```rust
fn append_child(&mut self, parent: NodeId, child: NodeId) {
    let parent_idx = parent.index();
    let child_idx = child.index();
    // a child may be mounted before its parent, so an existing subtree can be
    // attached under a newly mounted node
    debug_assert!(child != parent, "a frame node cannot be its own child");
    debug_assert!(
        self.nodes[child_idx].parent.is_none(),
        "a frame node can only be attached once"
    );

    self.nodes[child_idx].parent = Some(parent);

    match self.nodes[parent_idx].last_child {
        Some(last) => {
            debug_assert!(
                child_idx > last.index(),
                "frame siblings must be mounted in append order"
            );
            self.nodes[last.index()].next_sibling = Some(child)
        }
        None => self.nodes[parent_idx].first_child = Some(child),
    }

    self.nodes[parent_idx].last_child = Some(child);
}
```

`debug_assert!` checks are compiled in debug builds and tests and removed in
release builds. They document the *assumptions* this function relies on (no
cycles, no double attachment, append order) at zero cost to the firmware.
That's a middle ground between "trust me" comments and full runtime checks.

## Entities become nodes too

When an element tree contains an `Entity<T>` (for example
`.child(self.battery_indicator)`), mounting doesn't render it right away. It pushes
an `Entity` node that remembers *how* to render later:

```rust
impl<T> Element for Entity<T>
where
    T: Render,
{
    fn mount(self, cx: &mut MountCx<'_>) -> Result<NodeId, MountError> {
        cx.push_entity(self.entity_id(), render_entity::<T>)
    }
}
```

`render_entity::<T>` is the same trick as chapter 5's trampolines. The node stores
an `EntityRenderFn`, a plain function pointer:

```rust
pub(crate) type EntityRenderFn = fn(
    entity: EntityId,
    runtime: RuntimeCx<'_>,
    frame: &mut dyn FrameStore,
) -> Result<NodeId, MountError>;
```

and the monomorphised `render_entity::<T>` behind it borrows the entity, calls
`T::render`, and mounts the result:

```rust
let state = unsafe { &mut *borrow.ptr().cast::<T>().as_ptr() };
let entity = Entity::<T>::from_id(entity_id);
let runtime = runtime.rendering(entity_id);
let mut cx = Context::new_in(entity, runtime);
let element = state.render(&mut cx).into_element();
let app = AppContext::from_runtime(runtime);
let mut mount_cx = MountCx::new(frame, app);

element.mount(&mut mount_cx)
```

Why the delay? Because expansion then happens **breadth-first in a loop**
instead of by recursion:

```rust
while index < self.nodes.len() {
    let Ok(raw) = u16::try_from(index) else {
        break;
    };
    let node = NodeId::new(raw);
    index += 1;

    let NodeKind::Entity {
        entity,
        expanded: false,
        ..
    } = self.node(node).kind
    else {
        continue;
    };

    // ... reuse the previous content if the entity doesn't need to render ...

    self.render_entity_node(node, runtime)?;
}
```

Rendering an entity appends new nodes to the end of the vector, possibly more
unexpanded entities, and the loop simply keeps going until it reaches the end.
The stack depth doesn't grow with the depth of the UI, which matters on a
microcontroller with a few kilobytes of stack per task. The pattern in the
`let ... else` is worth reading closely, too. `expanded: false` inside a struct
pattern matches only nodes whose field *equals* `false`, and `..` ignores the
remaining fields.

It also gives a natural place to plug in the partial rebuilds of chapter 9. The
`needs_render` check above lets an entity that didn't change keep its previous
nodes.

`push_entity` refuses to mount the same entity twice in one frame
(`MountError::DuplicateEntityMount`). An entity has one place in the tree. If two
nodes rendered the same state, a tap on either would be ambiguous.

## Identity that survives frames

The frame is rebuilt often, and its `NodeId`s are just positions, so they change.
Yet some things must survive between frames: how far a list is scrolled, which
element is being pressed. Commit `46abc7e` introduced **persistent element
identity**.

When an element has `.id(...)`, the runtime looks up its state with a key made of
the element's id *and its parent's identity*:

```rust
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum IdentityParent {
    Entity(EntityId),
    Element(ElementStateId),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct ElementStateKey {
    pub(crate) parent: IdentityParent,
    pub(crate) local: ElementId,
}
```

So two rows that are both called `("row", 3)` but sit in different lists are
different elements, much like two files named `notes.txt` in different
folders. Each entry also records its **scope**, the nearest entity above the element, and
the frames in which it was seen. When that entity renders again, the entries it no
longer produced are swept, so a list that disappears doesn't leak its scroll
state.

## Errors as values

Every step in this chapter returns `Result<_, MountError>`:

```rust
pub enum MountError {
    NodesFull,
    TextStorageFull,
    EventBindingsFull,
    /// too many nodes have focused or pressed styles
    InteractionStylesFull,
    EntityAccess(EntityAccessError),
    DuplicateEntityMount(EntityId),
    /// heap-backed storage could not allocate
    AllocationFailed,
}

impl From<EntityAccessError> for MountError {
    fn from(value: EntityAccessError) -> Self {
        Self::EntityAccess(value)
    }
}
```

That `From` impl is what lets `?` convert errors automatically. Inside a
function returning `Result<_, MountError>`, writing `RawEntityBorrow::acquire(...)?`
on a `Result<_, EntityAccessError>` calls `MountError::from` for you. Small error
enums plus `From` conversions are the usual Rust style in `no_std` code, where
`Box<dyn Error>` isn't available.

On a device with fixed tables, "the UI didn't fit" is a real, expected outcome.
The runtime reports it, the platform can log it, and the developer raises a
capacity in the `RuntimeStorage` configuration from chapter 4. Nothing corrupts
memory, and nothing allocates behind your back.

## Rust concepts in this chapter

- **Enums for closed sets, traits for open sets.**
- **Index-based trees** (arena + `u16` ids) instead of `Rc`/`Weak` or raw
  pointers; first-child/next-sibling layout.
- **`while let`** loops over `Option` links.
- **Trait objects** (`&mut dyn FrameStore`) to keep APIs non-generic and code small.
- **Function pointers** (`fn(...) -> ...`) as stored behaviour.
- **Struct patterns with literal fields** (`expanded: false`) and `..`.
- **Iteration instead of recursion** to bound stack depth.
- **`debug_assert!`** for documented, zero-cost-in-release invariants.
- **Error enums**, **`From` for `?` conversions**, rolling back on error.
- **Bytes versus characters** in `str::len`.

## Try it yourself

1. In `crates/ui/src/frame/tests.rs`, find a test that mounts a tree. Add a
   helper that prints each node's kind with indentation by walking
   `first_child`/`next_sibling`.
2. Configure a runtime with `FixedFrame<4, 64>` (four nodes) and render a div with
   five children. Which `MountError` do you get? Where would you change the
   capacity in a real app?
3. Remove the `self.text.truncate(start)` line in `push_text` and write a test
   showing why it was needed.
