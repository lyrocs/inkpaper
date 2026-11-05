# 2. Day one: a UI API made of types

> **Where we are:** commit `dceaacd`, "initial commit", 7 August 2026. The
> repository holds a 4,000-line design document and about 300 lines of Rust.
> Check it out with `git checkout dceaacd` (see [Appendix B](appendix-git.md)).

## Start with the API you want

Most projects begin with a `main.rs`. InkPaper began with a **design
document**, `crates/ui/docs/design.md`, which opens like this:

> This project is a declarative UI framework for embedded Rust built on top of
> `embedded-graphics`. [...] The implementation must respect embedded constraints
> that desktop UI frameworks normally ignore:
>
> - `no_std` must be a primary target, not an afterthought.
> - Heap allocation must not be required.
> - Memory usage should be bounded and predictable.
> - Application state and UI state should not rely on `Rc`, `Arc`, `RefCell`,
>   `Box<dyn Trait>`, or a general-purpose allocator.

It then shows the code a user of the framework should be able to write:

```rust
div()
    .flex()
    .flex_col()
    .child("Settings")
    .child(...)
```

and the code they should *never* have to write:

```rust
ui.begin_column();
ui.add_text(...);
ui.add_button(...);
ui.end_column();
```

The first style is **declarative**. You describe a tree of elements, and the
framework decides how to lay it out and draw it. The style is borrowed from
[GPUI](https://github.com/zed-industries/zed/tree/main/crates/gpui), the UI
framework behind the Zed editor. GPUI is free to use the heap, though, and
InkPaper is not. The whole first commit is an experiment to answer one question:
*can Rust's type system describe a tree of UI elements without allocating?*

The answer was yes. This chapter shows how.

## A first test

The first commit's `lib.rs` contains one test, and it is also the
specification:

```rust
#[test]
fn basic_api_compiles() {
    let _ = div()
        .flex()
        .flex_col()
        .w_full()
        .h_full()
        .p(px(8))
        .gap(px(4))
        .child("hello")
        .child(
            div()
                .flex()
                .flex_row()
                .w(px(100))
                .h(px(40))
                .p(px(4))
                .child("Nested"),
        );
}
```

The test asserts nothing. It passes if the code compiles. When you design an
API in Rust, a test like this is a cheap way to pin down the *shape* you want
before you worry about behaviour.

## Newtypes for units

Start with the smallest file, `units.rs`:

```rust
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Pixels(pub i32);

pub const fn px(value: i32) -> Pixels {
    Pixels(value)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
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

`Pixels` is a **newtype**: a struct with a single field that wraps another type.
At runtime it is exactly an `i32` with no overhead. At compile time it is a
different type, so you cannot pass a raw count, a font size or an index where a
pixel distance is expected. That catches a whole class of unit bugs.

`px` is a `const fn`, which means it can run at compile time, in a `const` or
`static` initialiser. InkPaper uses `const fn` heavily so that sizes and colours
can be constants.

`Length` is an **enum with data**: a length is either automatic, a fixed number of
pixels, or "fill the parent". The `From<Pixels>` implementation lets us turn
pixels into a length with `.into()`. Notice what it buys in the style methods
below.

The derives are worth a second look as well:

- `Clone, Copy`: the value can be duplicated by copying bits. For small
  plain-data types this makes them as easy to use as integers.
- `PartialEq, Eq`: `==` works. `Eq` promises equality is reflexive, which floats
  can't promise (`NaN != NaN`), so it can only be derived when every field is `Eq`.
- `Debug`: `{:?}` formatting for tests and logs.

## A style, and a trait that edits it

`style.rs` defines the data:

```rust
pub struct Style {
    pub display: Display,
    pub flex_direction: FlexDirection,

    pub width: Length,
    pub height: Length,

    pub padding: Edges<Pixels>,
    pub gap: Pixels,
}
```

`Edges<T>` is a small **generic struct**, one value per side:

```rust
#[derive(Debug, Clone, Copy)]
pub struct Edges<T> {
    pub top: T,
    pub right: T,
    pub bottom: T,
    pub left: T,
}

impl<T: Copy> Edges<T> {
    pub const fn all(value: T) -> Self {
        Self { top: value, right: value, bottom: value, left: value }
    }
}
```

`impl<T: Copy> Edges<T>` means "these methods exist for every `Edges<T>` whose `T`
is `Copy`". We need `Copy` because `all` uses `value` four times. Without the
bound, the first use would move it and the second would fail to compile.

Then comes the trait that gives every element its fluent methods:

```rust
pub trait Styled: Sized {
    fn style_mut(&mut self) -> &mut Style;

    fn flex(mut self) -> Self {
        self.style_mut().display = Display::Flex;
        self
    }

    fn w(mut self, width: impl Into<Length>) -> Self {
        self.style_mut().width = width.into();
        self
    }

    fn p(mut self, padding: impl Into<Pixels>) -> Self {
        self.style_mut().padding = Edges::all(padding.into());
        self
    }

    // ...and many more
}
```

This trait packs several ideas into a few lines.

**Default methods.** An implementor writes only `style_mut`. Every other method
has a body in the trait, written in terms of `style_mut`. Implementing `Styled`
for a new element is one line, and it gets the whole vocabulary.

**`self` by value.** Each method takes `mut self` and returns `Self`. The value
moves into the method, gets changed, and moves out again. That is what makes
chaining work: `div().flex().flex_col()` passes one value through a pipeline of
methods. Since nothing is borrowed, nothing outlives anything else.

**`Sized` as a supertrait.** Returning `Self` by value needs to know its size at
compile time. `trait Styled: Sized` states that once, instead of adding `where
Self: Sized` to every method.

**`impl Into<Length>` in argument position.** This is shorthand for a generic
parameter: `fn w<L: Into<Length>>(mut self, width: L)`. Because of the `From`
impl in `units.rs`, both `w(px(100))` and `w(Length::Fill)` compile. `From` and
`Into` are the conventional way to write "accept anything that can become an X".

## Elements, and things that become elements

`element.rs` declares two traits:

```rust
pub trait Element: Sized {}

pub trait IntoElement {
    type Element: Element;

    fn into_element(self) -> Self::Element;
}
```

`Element` is something the framework can put in a tree. `IntoElement` is
anything that can be *turned into* an element. Its **associated type**
`type Element` says which element it becomes. Each implementation picks
exactly one, unlike a generic parameter, which would allow many.

Then a **blanket implementation**:

```rust
impl<T> IntoElement for T
where
    T: Element,
{
    type Element = T;

    fn into_element(self) -> Self::Element {
        self
    }
}
```

"For every `T` that is an `Element`, `T` is also `IntoElement`, and turns into
itself." This one impl covers every element type that will ever exist, including
ones written in other crates.

Finally, a string slice can become a text element:

```rust
pub struct Text<'a> {
    pub text: &'a str,
}

impl Element for Text<'_> {}

impl<'a> IntoElement for &'a str {
    type Element = Text<'a>;

    fn into_element(self) -> Self::Element {
        Text { text: self }
    }
}
```

That is why `.child("hello")` works. `Text<'a>` carries a **lifetime**: it borrows
the string instead of copying it. In an allocator-free world, borrowing is the
default way to hold text. `Text<'_>` in the `impl Element` line is the
*anonymous lifetime*: "some lifetime, I don't need to name it here".

## The clever part: children as a type-level list

Now the real problem. A `div` has children, and they can be of different types: a
`Text<'a>`, another `Div`, a button. The usual answer is
`Vec<Box<dyn Element>>`, which allocates twice: once for the vector, once per
box. The design document rules that out.

The first commit's answer is to encode the children *in the type of the div*:

```rust
pub struct NoChildren;

pub struct Push<C, E> {
    pub previous: C,
    pub element: E,
}

pub struct Div<C = NoChildren> {
    pub(crate) style: Style,
    pub(crate) children: C,
}

pub fn div() -> Div<NoChildren> {
    Div { style: Style::default(), children: NoChildren }
}
```

`NoChildren` is a **unit struct**: it has no fields and takes zero bytes. `Push<C,
E>` is a pair: "the children before (`C`), plus one more element (`E`)". So:

| Expression | Type |
|------------|------|
| `div()` | `Div<NoChildren>` |
| `div().child("a")` | `Div<Push<NoChildren, &str>>` |
| `div().child("a").child(div())` | `Div<Push<Push<NoChildren, &str>, Div<NoChildren>>>` |

This is a **heterogeneous list built out of nested generic types**, a classic
trick from functional programming that Rust supports well. The whole tree lives
inline, in one value, on the stack. There are no pointers and no allocation. Each
type is known exactly at compile time, so the compiler can inline everything.

`C = NoChildren` in `Div<C = NoChildren>` is a **default type parameter**. Writing
plain `Div` means `Div<NoChildren>`.

### Making `child` return a different type

The `child` method has to take a `Div<C>` and return a `Div<Push<C, E>>`, a
different type. The trait that describes this uses a **generic associated type**
(GAT):

```rust
pub trait ParentElement: Sized {
    type WithChild<E>: ParentElement
    where
        E: IntoElement;

    fn child<E>(self, child: E) -> Self::WithChild<E>
    where
        E: IntoElement;
}
```

`type WithChild<E>` is an associated type that itself takes a type parameter.
"For any child type `E`, here is the type you get after adding it." The
implementation for `Div` is:

```rust
impl<C> ParentElement for Div<C> {
    type WithChild<E>
        = Div<Push<C, E>>
    where
        E: crate::IntoElement;

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
}
```

`child` takes `self` by value, moves the old children into `previous`, and builds
a new, bigger `Div`. The old value is consumed, so there is never a moment when
two divs claim the same children. Ownership makes the builder safe without any
runtime checks.

The last piece is a trait that says "this type is a valid list of children":

```rust
pub trait Children {}

impl Children for NoChildren {}

impl<C, E> Children for Push<C, E>
where
    C: Children,
    E: IntoElement,
{
}
```

That is a **recursive definition in the trait system**. The empty list is a list,
and a list plus one element is a list. `impl<C> Element for Div<C> where C:
Children {}` then says that a div is an element only if its children form a
valid list. If you try to put something that isn't an element in a div, you get a
compile error, not a crash.

## What happened next

Within a day, two more commits added **identity** (`0165eab`, "add stateful
element identities") and **entities** (`d3cf9df`). Today the identity looks
like this:

```rust
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ElementId {
    Name(&'static str),
    Value(u64),
    NamedValue(&'static str, u64),
}
```

and the conversion into it is written once for many integer types with a small
**declarative macro**:

```rust
macro_rules! impl_integer_element_id {
    ($($ty:ty),* $(,)?) => {
        $(
            impl IntoElementId for $ty {
                fn into_element_id(self) -> ElementId {
                    ElementId::Value(self as u64)
                }
            }

            impl IntoElementId for (&'static str, $ty) {
                fn into_element_id(self) -> ElementId {
                    ElementId::NamedValue(self.0, self.1 as u64)
                }
            }
        )*
    };
}

impl_integer_element_id! { u8, u16, u32, u64, usize }
```

`$($ty:ty),*` matches a comma-separated list of types, and `$( ... )*` repeats
the body once per type. So `.id("close")`, `.id(3u8)` and `.id(("row", index))`
all work. Chapter 19 covers macros in depth.

The traits grew too. `Element` gained the method that actually puts the element
into a frame:

```rust
pub trait Element: Sized {
    fn mount(self, cx: &mut MountCx<'_>) -> Result<NodeId, MountError>;
}
```

`Children` gained `mount_children`, implemented recursively for `Push`. It mounts
`previous` first and then `element`, so the children end up in source order:

```rust
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

The `?` operator returns early with the error if mounting fails. Chapter 6 shows
where those errors come from: a full frame table, for example.

Text grew an enum so it could also *own* a string when `alloc` is available:

```rust
pub enum TextContent<'a> {
    Borrowed(&'a str),
    #[cfg(feature = "alloc")]
    Owned(alloc::string::String),
}
```

A `#[cfg]` on an enum *variant* removes that variant entirely when the feature is
off. Every `match` on `TextContent` then needs the same `#[cfg]` on the
corresponding arm, and the compiler checks that it is there.

And dynamic lists arrived: `PushMany<C, I>` holds any `I: IntoIterator` whose
items are elements. That covers a list of rows coming from data, which a type-level
list can't express, still without boxing each element.

## Rust concepts in this chapter

- **Newtypes** (`Pixels`) and **`const fn`**.
- **Enums with data** (`Length`, `ElementId`, `TextContent`).
- **Derives**: `Debug`, `Clone`, `Copy`, `PartialEq`, `Eq`, `Hash`, `Default`.
- **Generic structs and bounded impls** (`impl<T: Copy> Edges<T>`).
- **Traits with default methods**, **supertraits** (`: Sized`), `self` by value
  for builders.
- **`From`/`Into`** and `impl Trait` in argument position.
- **Associated types**, **blanket implementations**, **generic associated
  types**.
- **Type-level lists** (`NoChildren`, `Push<C, E>`) and recursive trait impls.
- **Lifetimes in structs** (`Text<'a>`) and the anonymous lifetime `'_`.
- A first **`macro_rules!`**.

## Try it yourself

1. Check out `dceaacd` and add a `.child(42)` to the test. Read the compiler error:
   which trait is not implemented, and for which type?
2. Write a function `fn count<C: Children>(c: &C) -> usize` by adding a method to
   the `Children` trait and implementing it for `NoChildren` (returns 0) and
   `Push` (returns `previous.count() + 1`). This is the same recursion
   `mount_children` uses.
3. Print `core::any::type_name_of_val(&div().child("a").child(div()))` to see the
   nested type the builder produces.
