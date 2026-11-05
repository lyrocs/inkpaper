# 2. Describing a screen: a UI language of types

Imagine a settings panel with a title, two options, and enough space around them that a finger can choose comfortably. Before we care how pixels are drawn, we need a way to describe that relationship: these things belong together, this arrangement is vertical, this gap separates neighbors. InkPaper expresses those decisions as ordinary Rust values.

That is an important distinction from issuing drawing commands. “Put this text at these coordinates” decides the answer immediately. “Put these children in a column with padding” preserves the intention so layout can decide the coordinates later. On a small screen, that separation makes it easier to reason about available space without scattering arithmetic through every component.

## Read the screen from the outside in

Here is a complete pedagogical Rust function using the current public API. It constructs an element; it does not launch a window or paint one. The function is suitable for a small consumer crate that depends on `inkpaper-ui`.

```rust,noplayground
use inkpaper_ui::prelude::*;

fn settings_panel() -> impl IntoElement {
    div()
        .flex()
        .flex_col()
        .w_full()
        .p(px(12))
        .gap(px(8))
        .child("Reading settings")
        .child("Font size")
        .child("Line spacing")
}
```

The outside `div` groups the content. `flex_col` chooses a vertical flex direction; `w_full` asks for the available width. Padding belongs inside the container's edges, while the gap belongs between its children. The string slices become text elements. None of these calls needs to know the final screen coordinates.

The return type, `impl IntoElement`, says that this function returns one concrete type implementing `IntoElement`, while keeping that type's spelling private. It does not mean “any type chosen at runtime.” The compiler still knows the exact result. That will matter when the children make its type surprisingly elaborate.

The prelude imports the traits that supply the builder methods. Rust can make methods available through traits as well as directly on a struct. If a method exists in the source but is missing at a callsite, a missing trait import is one useful thing to check. Here `Styled` supplies layout styling, while `ParentElement` supplies children.

## Units make intentions harder to confuse

A width is not just an integer. It might be a fixed number of pixels, an automatic size, or a request to fill space. These exact definitions from `crates/ui/src/geometry.rs` show the vocabulary; the first definition is shown without its derive attributes:

```rust,ignore
pub struct Pixels(i32);

pub const fn px(value: i32) -> Pixels {
    Pixels(value)
}
```

`Pixels` is a *newtype*: a distinct Rust type around an existing representation. Its field is private, so callers construct it through `px` and inspect its number through methods such as `get`. The wrapper gives APIs a place to express units rather than treating a pixel distance, an item count, and an index as interchangeable integers. `const fn` permits construction in a constant expression as well as at runtime.

For the more general sizing request, the same file contains these exact declarations, again omitting derive attributes:

```rust,ignore
pub enum Length {
    Auto,
    Pixels(Pixels),
    Fill,
}

impl From<Pixels> for Length {
    fn from(value: Pixels) -> Self {
        Self::Pixels(value)
    }
}
```

Unlike three special numeric values, these alternatives explain themselves. The `Pixels` variant contains a payload; `Auto` and `Fill` do not. The `From` implementation allows a pixel value to become a `Length`. Consequently the width builder accepts `impl Into<Length>`: `.w(px(100))` and `.w(Length::Fill)` describe different intentions through the same method.

The fluent style methods are generated from declarations in `crates/ui-style-schema/src/lib.rs`, using machinery in `crates/ui/src/style/mod.rs`. Their common behavior is simple: take `mut self`, modify its `Style`, return `self`. They consume and return the element value instead of borrowing a long-lived builder. A chain is therefore a sequence of ownership transfers, not a sequence of allocations. This does not promise the optimizer will eliminate every move; it tells us what the API requires semantically.

## A different child means a different type

A container might hold text and another container. A conventional representation could use a vector of boxed trait objects: each child hides its concrete type behind a common interface. That is flexible, especially when a program must store an open-ended mix of element types. It also introduces dynamic dispatch and heap-backed ownership.

InkPaper's ordinary builder takes a different route. `Div<C>` stores its children directly in a field of type `C`. `NoChildren` is the empty case. Each `.child(...)` wraps the previous children and the new child in a `Push<C, E>`. These are plain nested structs, not a linked list of heap nodes.

Here is the exact `child` method from the `ParentElement` implementation in `crates/ui/src/element/div.rs`:

```rust,ignore
fn child<E>(self, child: E) -> Self::WithChild<E>
where
    E: crate::IntoElement,
{
    Div {
        style: self.style,
        children: Push {
            previous: self.children,
            element: child,
        },
    }
}
```

The associated type `WithChild<E>` resolves here to `Div<Push<C, E>>`. Because it accepts its own parameter `E`, it is a *generic associated type*. Read it as “the container type obtained when this child type is added.” One child produces `Div<Push<NoChildren, &str>>`; adding a nested empty container produces `Div<Push<Push<NoChildren, &str>, Div<NoChildren>>>`.

You rarely need to write those names. Type inference follows the construction, and `impl IntoElement` hides the unwieldy spelling at a function boundary. But understanding the shape explains an important constraint: a builder with three statically chained children has a different type from one with four. An `if` cannot return two unrelated builder types just because both eventually draw a screen.

The current API has tools for that problem. `.children(iterator)` stores a `PushMany` and mounts each item; the item type stays fixed while the number of items can vary. `Either<L, R>` represents two different branches in one enum. With `alloc`, `AnyElement` offers heap-backed type erasure when hiding the shape is worth that cost. These are choices for different problems, not evidence that one representation must serve every screen.

## A description is not yet a frame

The builder only describes content. The `Element` trait's real operation is `mount(self, cx)`, which consumes that description and returns a `Result<NodeId, MountError>`. A node ID refers to the frame representation that later stages can lay out and paint. This is where a compile-time-shaped value becomes runtime records.

Children mount in source order. The exact implementation in `crates/ui/src/element/mod.rs` is small enough to see the whole operation:

```rust,ignore
impl<C, E> Children for Push<C, E>
where
    C: Children,
    E: IntoElement,
{
    fn mount_children(self, parent: NodeId, cx: &mut MountCx<'_>) -> Result<(), MountError> {
        self.previous.mount_children(parent, cx)?;

        let child = self.element.into_element();
        let child = child.mount(cx)?;

        cx.append_child(parent, child);

        Ok(())
    }
}
```

Recursing into `previous` first preserves the order we wrote. `into_element` converts convenient inputs into elements; for `&str`, the associated element type is `Text<'a>`. The question marks propagate failures instead of continuing with a missing child. Static typing can reject a non-element, but it cannot guarantee that a chosen fixed frame has enough space for every node and text byte.

Borrowed text also has a boundary. `Text<'a>` can borrow a string while the description exists; with `alloc`, its content can instead own a `String`. Mounting passes that content to the frame's text storage. Borrowing in the builder therefore avoids requiring a separate owned string there, but it is not a claim that text is never copied anywhere in the rendering pipeline.

The benefit of this design is local clarity: the settings panel reads like a description, while ownership and trait bounds make its pieces checkable. The tradeoff is more work for the type system, potentially larger generated code from many concrete shapes, and less effortless runtime heterogeneity than a uniform boxed tree. The framework keeps an escape hatch rather than denying that tradeoff.

## Try it: predict the builder's shape

Optional, read-only: trace `div().child("Title").child(div())` through `Div::child` in `crates/ui/src/element/div.rs`. Write down the type after each call, then compare with the nested type above. Next follow `Push::mount_children`: which child mounts first?

The expected result is that the text mounts before the nested container, even though the outermost `Push` stores the container as its newest element. For an optional compile experiment, use the complete `settings_panel` function in a consumer crate, then replace one string child with `42u32`. Compilation should reject it because the integer does not implement `IntoElement`. The error is about what a screen can contain, not about drawing pixels—a useful boundary to establish before we add persistent state.
