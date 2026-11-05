# 8. Turning rectangles into ink

The settings drawer is now laid out: every row has a rectangle, the text has a width, and the overlay covers the page beneath it. None of that has changed a pixel. Layout describes where things belong; painting must turn those descriptions into visible marks.

On a desktop, those marks are RGB pixels in a simulator display. On the reader, they are shades in an e-paper framebuffer, which the display controller will later transfer to the panel. We want the same drawer on both, but not necessarily the same low-level drawing strategy. A rectangle fill, a soft glyph edge, and a photograph have different opportunities for optimization and different consequences on e-paper.

The boundary between those concerns is `Painter`. This session reads that boundary and the simulator's actual paint loop. Source excerpts are not standalone programs; the small complete function shown near the end still depends on its module's imports and constants.

## Ask for a box, not a thousand pixels

A UI framework knows more than “set this pixel.” It knows that a shape is a box, that it has a background and border, and that it is clipped to a visible region. Keeping that information until the backend can use it is valuable. A backend may fill spans efficiently or translate colors according to its medium.

This shortened excerpt from `crates/ui/src/rendering/paint/mod.rs` shows three methods of the actual trait; its canvas and pixel-row methods are omitted:

```rust,ignore
pub trait Painter {
    type Error;

    fn draw_box(
        &mut self,
        bounds: Rect,
        paint: BoxPaint,
        clip: Option<Rect>,
    ) -> Result<(), Self::Error>;

    fn draw_glyph(
        &mut self,
        bitmap: &GlyphBitmap<'_>,
        origin: Point,
        color: Color,
        clip: Rect,
    ) -> Result<(), Self::Error>;

    fn fill_rect(&mut self, rect: Rect, color: Color, clip: Rect) -> Result<(), Self::Error>;
}
```

`BoxPaint` carries the visual properties, while `bounds` carries geometry. Keeping them separate means painting need not repeat layout. A glyph arrives as coverage: how much of each pixel belongs to the letter. The backend decides how that coverage becomes marks against the destination. Images ultimately arrive through `draw_pixels` as rows of final colors that are already clipped.

The associated `Error` type lets the implementation describe its own failure. The runtime can return `PaintError<P::Error>` without requiring every painter to share one error enum. That wrapper distinguishes a painter failure from font-cache and shaping failures. An infallible drawing target can use `Infallible`; this does not make the entire text-painting pipeline infallible.

A single pixel-writing trait would be smaller, but would throw away useful structure early. Conversely, asking the backend to understand whole application widgets would tie it to the app. This trait sits between those extremes: enough meaning for efficient drawing, not enough to decide what a settings drawer does.

## A backend borrows its destination

The embedded-graphics adapter connects `Painter` to an ecosystem `DrawTarget`. Its state is small. This exact declaration comes from `crates/ui/src/backend/embedded_graphics/mod.rs`:

```rust,ignore
pub struct EmbeddedGraphicsPainter<'target, D>
where
    D: EgDrawTarget,
{
    target: &'target mut D,
    coverage_mode: CoverageMode<D>,
}
```

The lifetime says that the painter cannot outlive its borrowed target. The mutable reference gives it exclusive access through that borrow, rather than shared access guarded by a lock. The type parameter `D` allows the same adapter to work with different display implementations. Its `Painter` implementation additionally requires `D::Color: From<EgRgb888>`: the target's color type must accept the RGB representation used by this backend.

Within `draw_box`, target adapters perform color conversion and clipping. These are ordinary values wrapping a draw target, not another application tree. Generic wrappers give the compiler opportunities to inline the work, but they do not make conversion or clipping conceptually disappear. Each backend still needs to honor the same visible bounds.

There is a deliberate contrast in the canvas method. It receives a `&mut dyn FnMut(Rect, &mut dyn CanvasPainter)` callback. The concrete closure type is hidden, letting the backend establish a drawing context and then run application-supplied commands through a limited interface. Here a trait object avoids making the operation generic over every callback type. InkPaper uses both generics and dynamic dispatch where their different costs fit; “always avoid `dyn`” would be a poor explanation of this API.

## Fonts are resources, not display drivers

A framebuffer does not know how to turn UTF-8 into positioned glyphs. Nor should a draw target own the application's image registry. `PaintResources`, alongside `Painter`, handles those resources and translates text and images into drawing operations.

The exact text entry point from `crates/ui/src/rendering/paint/mod.rs` makes both responsibilities visible:

```rust,ignore
fn paint_text<P: Painter>(
    &mut self,
    painter: &mut P,
    text: &str,
    bounds: Rect,
    style: ResolvedTextStyle,
    clip: Option<Rect>,
) -> Result<(), PaintError<P::Error>>;
```

This method is generic over the destination painter. The text machinery can shape and rasterize using its own resources, then send glyph coverage to whatever backend the platform chose. Later sessions will examine the shaping work; here the important observation is that changing the destination need not change the application's words.

There is also a `PaintResources` implementation for `()`, Rust's unit type. It intentionally paints no text or images. That is useful for layout-only tests and graphics-only runtimes, but it is not a fallback font system. If boxes appear while text is absent, the selected resources are part of the investigation.

Painting visits the visual tree in parent-before-child order, so later content can cover earlier content. The shared traversal applies scrolling and clipping, and input uses the same visual interpretation. Its continuation storage is bounded; if the small stack fills, the implementation recomputes context by climbing ancestors rather than silently dropping the rest of the tree. The trade is bounded storage for occasional extra traversal work, not a hard promise that deeply nested content stops existing.

## Painting is not refreshing

The simulator's `render_ui`, in `crates/sim/src/render.rs`, provides a compact view of the pipeline. This is the exact function, excluding its documentation comment:

```rust,ignore
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

Preparation performs the rebuild and layout work that pending invalidation requires. It returns damage: the region needing new pixels. No damage means no painting. Otherwise the caller clears that region and paints the current frame into it. Clearing matters when a label shrinks or a control disappears; drawing only the new content would leave the old marks behind.

The coverage mode uses a pixel-reading function so glyph edges can blend with pixels already in the simulator display. It is a rendering choice, not a simulation of physical panel timing. The function's panic paths are also platform policy: it treats rendering failure as fatal rather than presenting recovery UI.

The e-paper backend in `crates/ui-eink/src/lib.rs` implements the same `Painter` trait for `DrawTarget<Color = Gray2>`. That equality bound requires a two-bit grayscale destination, not merely a color that can be converted from RGB. The backend supports native grayscale and binary-dither UI modes and records tone information in its paint report. Gray-looking content may therefore be represented by native intermediate tones or by a pattern of black and white, depending on the selected mode.

Neither implementation alone chooses the panel waveform. Painting changes the destination image; refresh policy decides how the physical display transitions to it. Even a complete repaint need not imply a complete flashing waveform. Controller state, supported update modes, and periodic cleanup also matter. Keeping these decisions separate is essential to understanding why drawing less and refreshing differently are related but not interchangeable optimizations.

## Try it: follow the missing label

As an optional hardware-free exercise, trace `render_ui` for a frame in which a label becomes shorter. First assume preparation returns damage covering the old and new label. Identify the call that removes the old tail, then the call that draws the replacement. Next follow `EmbeddedGraphicsPainter::clear_damage`: full damage calls the target's native clear operation; partial damage is clipped to target bounds and filled rectangle by rectangle.

Your observable result is a two-case call trace. If you mentally omit the clear, the new label cannot erase pixels it never touches. If preparation returns no damage, neither clear nor paint runs. This is enough to explain a surprisingly large class of stale-pixel bugs before involving a physical display at all.
