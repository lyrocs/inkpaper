# 8. Painting through a trait

> **Where we are:** commit `c3e72a5` ("add embedded-graphics frame renderer") and
> `b6a0814` ("add desktop UI simulator example"), 9 August 2026. The next day,
> `aee8a87` ("decouple painting from embedded-graphics") introduced the
> `Painter` trait. In September and October the e-ink backend grew
> (`137c021`, `fa0ff9f`) and finally moved into its own crate (`d113331`,
> "move the eink backend into its own crate", 17 October).

## The first pixels

At the end of day three the framework could lay out a tree, and someone wanted to
*see* it. The quickest route was
[`embedded-graphics`](https://crates.io/crates/embedded-graphics), the standard
2D drawing library for embedded Rust. Its central abstraction is a trait:

```rust
// from embedded-graphics (simplified)
pub trait DrawTarget: Dimensions {
    type Color: PixelColor;
    type Error;

    fn draw_iter<I>(&mut self, pixels: I) -> Result<(), Self::Error>
    where
        I: IntoIterator<Item = Pixel<Self::Color>>;

    // fill_contiguous, fill_solid and clear have default implementations
}
```

Every display driver in the ecosystem implements `DrawTarget`, and so does
`embedded-graphics-simulator`, which opens an SDL window on your desktop. So one
renderer written against `DrawTarget` can draw both to a window and to a real
panel.

Commit `c3e72a5` did exactly that, and `b6a0814` added a desktop example. From
then on, every feature could be tried in a window seconds after it compiled.
The simulator became the project's main development tool, long before there was
any firmware.

## Why add another trait?

Only one day later, `aee8a87` "decoupled painting from embedded-graphics". Why
wrap a perfectly good trait in another one?

Because `DrawTarget` speaks in *pixels*, and a UI framework wants to speak in
*operations*: "draw this box with these rounded corners and this border, clipped
to this rectangle"; "draw this glyph's coverage in this colour". Expressing those
operations directly makes three things possible:

- a backend can implement an operation natively and faster (filling a whole
  rectangle of a 1-bit framebuffer with byte writes, not pixel by pixel);
- a backend can interpret an operation for its medium (on e-ink, a grey
  becomes a dither pattern or a native grey level);
- the core crate no longer *depends* on `embedded-graphics`. Look back at
  chapter 1: it's an **optional** dependency behind the `embedded-graphics`
  feature.

Here is the trait today:

```rust
pub trait Painter {
    type Error;

    fn draw_box(
        &mut self,
        bounds: Rect,
        paint: BoxPaint,
        clip: Option<Rect>,
    ) -> Result<(), Self::Error>;

    fn draw_canvas(
        &mut self,
        bounds: Rect,
        clip: Option<Rect>,
        draw: &mut dyn FnMut(Rect, &mut dyn CanvasPainter),
    ) -> Result<(), Self::Error>;

    /// draws a rasterized glyph's coverage in `color`, its top-left corner at `origin`
    fn draw_glyph(
        &mut self,
        bitmap: &GlyphBitmap<'_>,
        origin: Point,
        color: Color,
        clip: Rect,
    ) -> Result<(), Self::Error>;

    fn fill_rect(&mut self, rect: Rect, color: Color, clip: Rect) -> Result<(), Self::Error>;

    /// draws a row of final image colors left to right from `origin`. They're already
    /// clipped
    fn draw_pixels(&mut self, origin: Point, pixels: &[Color]) -> Result<(), Self::Error>;
}
```

### Associated error types

`type Error;` lets each implementation choose its own error type. A simulator
display can't fail, so its error is `core::convert::Infallible`, an enum with no
variants, which no value can ever have. An SPI-backed display might fail with a bus
error. The framework doesn't need to know. It returns whatever the painter
returns, wrapped in its own enum:

```rust
/// why painting stopped: the painter failed, or text couldn't be rasterized or shaped
#[derive(Debug)]
pub enum PaintError<E> {
    Painter(E),
    Font(GlyphCacheError),
    Shape(ShapeError),
}
```

`PaintError<E>` is **generic over the inner error**. `PaintError<Infallible>`
and `PaintError<SpiError>` are different types, and a function painting with
painter `P` returns `Result<_, PaintError<P::Error>>`. That's the standard way to
compose errors across a library boundary without boxing them. You'll see the same
shape in the drivers of Part III (`Error<E>`) and in the EPUB parser
(`Error<S::Error>`).

### A closure as a parameter

`draw_canvas` takes `draw: &mut dyn FnMut(Rect, &mut dyn CanvasPainter)`. A
canvas callback (chapter 5) needs to issue arbitrary drawing commands, and the
painter needs to set up clipping around it. So the painter receives the callback
as a **trait object of a closure**. Here `dyn` keeps `Painter` *object-safe* with
respect to this method: a generic `fn draw_canvas<F: FnMut(...)>` would make
every painter generic over every canvas closure type.

## The embedded-graphics painter

The first backend adapts `Painter` to any `DrawTarget`:

```rust
pub struct EmbeddedGraphicsPainter<'target, D>
where
    D: EgDrawTarget,
{
    target: &'target mut D,
    coverage_mode: CoverageMode<D>,
}

impl<D> Painter for EmbeddedGraphicsPainter<'_, D>
where
    D: EgDrawTarget,
    D::Color: From<EgRgb888>,
{
    type Error = D::Error;

    fn draw_box(
        &mut self,
        bounds: Rect,
        paint: BoxPaint,
        clip: Option<Rect>,
    ) -> Result<(), Self::Error> {
        let mut target = self.target.color_converted::<EgRgb888>();

        if let Some(clip) = clip {
            let clip = to_embedded_rect(clip);
            let mut clipped = target.clipped(&clip);
            draw_box_to(&mut clipped, bounds, paint, to_rgb888)
        } else {
            draw_box_to(&mut target, bounds, paint, to_rgb888)
        }
    }
    // ...
}
```

Three things to notice.

**The painter borrows the display.** `target: &'target mut D` means a painter
can't outlive the display it draws on, and while it exists nobody else can draw
on that display. A painter is created for one frame and dropped after.

**Bounds on associated types.** `where D::Color: From<EgRgb888>` says "this works
for any draw target whose pixel colour can be built from 24-bit RGB". The UI
thinks in `Color { r, g, b }`, and whether the target is an RGB window or a
grayscale panel, the conversion is the target's business.

**Adapters as values.** `color_converted()` and `clipped()` come from
embedded-graphics. Each wraps the target in a new `DrawTarget` that converts
colours or discards pixels outside a rectangle. It's the iterator-adapter idea
(`map`, `filter`) applied to drawing. Because the wrappers are generic structs, the
compiler inlines them, so clipping costs a comparison per pixel and nothing more.

## The `()` resource set

Painting text and images needs fonts and images, which are big, optional, and
different on each platform. The runtime is generic over a second parameter for
them, `Runtime<S, RESOURCES = ()>`, bounded by a trait:

```rust
pub trait PaintResources {
    fn paint_text<P: Painter>(
        &mut self,
        painter: &mut P,
        text: &str,
        bounds: Rect,
        style: ResolvedTextStyle,
        clip: Option<Rect>,
    ) -> Result<(), PaintError<P::Error>>;

    fn paint_image<P: Painter>(
        &mut self,
        painter: &mut P,
        source: ImageSource,
        bounds: Rect,
        paint: ImagePaint,
        clip: Option<Rect>,
    ) -> Result<(), PaintError<P::Error>>;
    // ...
}

/// no fonts or images: text and image nodes paint nothing
impl PaintResources for () {
    fn paint_text<P: Painter>(
        &mut self,
        _: &mut P,
        _: &str,
        _: Rect,
        _: ResolvedTextStyle,
        _: Option<Rect>,
    ) -> Result<(), PaintError<P::Error>> {
        Ok(())
    }
    // ...
}
```

**Implementing a trait for the unit type `()`** is a neat trick: it provides a
"do nothing" implementation that costs zero bytes. With the default type
parameter `RESOURCES = ()`, unit tests and benchmarks that only care about layout
write `Runtime<TestStorage>` and never think about fonts.

`PaintResources` methods are **generic** (`<P: Painter>`), unlike `draw_canvas`
above. These methods are called from inside the paint loop for every text node,
so each painter gets its own fully inlined copy. The trade-off is the opposite of
`MountCx` in chapter 6, and that's fine. Choosing between `dyn` and generics is a
per-call-site decision about code size versus speed.

## Walking the tree without recursion

Painting visits nodes in tree order: parent first, then children, so children
draw on top. The natural way to write that is recursion, but each recursive
call needs stack, and on the ESP32-S3 the main task's stack is a few kilobytes.
Commit `d32997d`, "bound visual traversal stack", made the walk iterative with
an explicit stack of fixed capacity:

```rust
const VISUAL_CONTEXT_STACK_CAPACITY: usize = 16;

// ...
    branch_continuations: Vec<VisualContinuation, VISUAL_CONTEXT_STACK_CAPACITY>,
```

That's a `heapless::Vec`: a vector with inline storage and a capacity fixed by a
const generic. `push` returns `Err` when it's full instead of reallocating. The
stack only holds entries for *branches that still have siblings to visit*, so its
depth is bounded by the number of nested containers that still have work left,
not by the total depth of the tree.

## The simulator's paint loop

Here is the whole render function of the simulator (`crates/sim/src/render.rs`):

```rust
/// paints what changed since the last frame
pub(super) fn render_ui<R>(runtime: &mut R, display: &mut SimulatorDisplay<Rgb888>)
where
    R: RenderRuntimeApi,
{
    let damage = runtime
        .prepare_frame(DISPLAY_SIZE)
        .expect("UI rebuild failed");
    if damage.is_none() {
        return;
    }

    let mut painter = EmbeddedGraphicsPainter::new(display)
        .with_coverage_mode(CoverageMode::alpha_blend(read_simulator_pixel));

    painter.clear_damage(damage, Color::WHITE).unwrap();

    match runtime.paint_with_damage(damage, &mut painter) {
        Ok(Some(_)) => {}
        Ok(None) => panic!("painting requires a mounted root"),
        Err(_) => panic!("UI painting failed"),
    }
}
```

There are three steps. *Prepare* the frame (rebuild and lay out what changed),
which returns the **damage**, the region that needs repainting. *Clear* that
region. *Paint* into it. Chapter 9 is entirely about that damage value.

`R: RenderRuntimeApi` hides the runtime's two type parameters behind a trait.
This function works with any storage and any resources, which matters because
the simulator can be built with heap or fixed storage (chapter 4).

`CoverageMode::alpha_blend(read_simulator_pixel)` passes a **function** that reads
back a pixel from the display, so antialiased text edges can be blended with
whatever is already there. On the device, commit `0639587` ("let the painter read
framebuffer pixels back") added the same ability for e-ink.

## An e-ink painter

E-paper is not an RGB screen. The X4 PRO's panel shows **four grey levels**, and
a frame's refresh is cheaper and faster if it uses only black and white. So the
e-ink painter in `crates/ui-eink` implements `Painter` for targets whose colour
is a 2-bit grey:

```rust
impl<D> Painter for EInkPainter<'_, D>
where
    D: EgDrawTarget<Color = Gray2>,
{
    type Error = D::Error;
    // ...
}
```

`EgDrawTarget<Color = Gray2>` is an **associated type equality constraint**: this
impl applies only to targets whose `Color` *is* `Gray2`. The e-ink painter
also tracks which grey levels a frame actually used (`EInkPaintReport`), so the
firmware can choose a fast black-and-white refresh when nothing grey was drawn.
That's a direct payoff of painting *operations* instead of pixels.

Colours carry their own conversion to grey:

```rust
#[expect(
    clippy::cast_possible_truncation,
    reason = "the weights sum to 256, so the quotient is at most 255"
)]
pub const fn luminance(self) -> Luminance {
    // rec. 601 integer luminance
    // ...
}
```

Here's the `#[expect]` pattern from chapter 1 in action. The cast is fine, the
reason is written down, and if the code ever changes so that the cast no longer
triggers the lint, `expect` itself warns that the annotation is stale. `#[allow]`
would stay silent forever.

### Why a separate crate?

On 17 October the e-ink backend left `inkpaper-ui` for its own crate,
`inkpaper-ui-eink`. The commits just before it moved the shared logic (text and
image paint pipelines, `draw_box_to`, conversions) into public helpers in
`inkpaper_ui::backend`, documented as "shared embedded-graphics helpers". After
that, a backend is just a crate that implements `Painter`, using only the public
API. If the core's own backends can live outside the core, anyone else's can too:
a colour LCD, a memory LCD, a test recorder. That's a good test of whether an
abstraction really is one.

## Rust concepts in this chapter

- **Wrapping an ecosystem trait** (`DrawTarget`) behind a domain trait
  (`Painter`), and an optional dependency behind a feature.
- **Associated error types**, `Infallible`, and **generic error wrappers**
  (`PaintError<E>`).
- **Trait objects for closures** (`&mut dyn FnMut(...)`).
- **Bounds on associated types** (`D::Color: From<...>`) and **equality
  constraints** (`DrawTarget<Color = Gray2>`).
- **Borrowing a resource for the life of a helper** (`&'target mut D`).
- **Implementing a trait for `()`** as a zero-cost "nothing" implementation, and
  **default type parameters** (`RESOURCES = ()`).
- **Generics vs `dyn`** as a per-call-site trade-off.
- **`heapless::Vec`** and iterative traversal with a bounded stack.
- **`#[expect]` versus `#[allow]`**.

## Try it yourself

1. Run `cargo run -p inkpaper-ui --example simulator --features
   embedded-graphics`, the framework's own feature gallery that grew out of
   `b6a0814`. Find its source in `crates/ui/examples/`.
2. Write a `CountingPainter` that implements `Painter` with
   `type Error = Infallible` and only counts calls per method. Paint a small tree
   with it in a test. That's a cheap way to assert "this change paints fewer
   boxes".
3. Look for `Color = Gray2` in `crates/ui-eink/src`. Try to use `EInkPainter`
   with an `Rgb888` simulator display and read the compiler error.
