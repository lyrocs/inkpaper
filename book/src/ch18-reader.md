# 18. Keep my place, change the page

You make the text larger halfway through a chapter. Lines wrap sooner, paragraphs grow taller, and the chapter needs more pages. What should remain unchanged is your place in the story. Returning to “page 37” is not enough when page 37 now contains different words.

This is the reader's central distinction: **content has an order; pages are one arrangement of that content**. InkPaper builds pages for a chosen viewport and typography, while maintaining positions in the underlying chapter flow. Rust's borrowing and ownership tools let it make that arrangement cheaply, then keep it after the parsed chapter is no longer needed.

## Measure before deciding where to break

The EPUB layer gives the reader blocks, inline text, images and styles. The paginator turns them into positioned page items. It does not choose a display driver or rasterize glyphs. This exact entry point from `crates/reader/src/pagination.rs` shows the boundary:

```rust,ignore
pub fn paginate_chapter<'a, M>(
    chapter: &'a Chapter,
    styles: &ChapterStyles,
    spine: SpineIndex,
    viewport: Viewport,
    settings: ReaderSettings,
    measurer: &mut M,
) -> Result<Pagination<'a>, M::Error>
where
    M: TextMeasurer + ImageMeasurer,
{
    Paginator::new(chapter, styles, spine, viewport, settings, measurer).paginate()
}
```

The two trait bounds ask for text measurement and image dimensions, not a font engine. Tests can give every scalar a width of one and every line a height of one. The app supplies a different measurer that uses the UI's registered fonts and prepared shapers. This is why a paginator test can discuss an exact seven-unit line without depending on a particular typeface.

`&mut M` is intentional. Measurement may cache results, and the app does: short text/style pairs use a fixed 64-slot cache with inline keys up to 32 bytes. Longer strings bypass it. More importantly, the real measurer uses `shape_line_with` to obtain an advance without collecting glyphs. It does not estimate width from character count while the painter uses a different model.

The paginator takes break opportunities from `unicode-linebreak`, fits segments, and moves overflowing content onto another line. When a word is too long, configured hyphenation can offer a split whose prefix plus drawn hyphen fits. Without a usable hyphenation point, oversized-word handling asks `TextMeasurer::next_boundary`. The trait's default advances by a Unicode scalar; the app overrides it with the shaper's cluster boundary. That override preserves the supported base-and-mark groups discussed in chapter 17 rather than merely staying inside valid UTF-8.

This is a greedy layout, not a search for the best arrangement of an entire paragraph. Such a search could balance spacing across many lines, but would require a different cost model and more retained candidates. The current approach makes local decisions with a direct relationship to available width. It can still report a shaping error through `M::Error`; generic measurement does not erase the possibility of failure.

## Make the lines feel like a page

Good pagination needs more than “stop when full.” Line height, paragraph spacing, indentation and alignment establish the rhythm of reading. Images flush the current text line, use measured intrinsic dimensions to obtain a fitted rectangle, and may begin another page when they cannot fit below existing content. If dimensions are unavailable, there is no image rectangle to lay out—but the image still belongs to the content flow.

Justification expands the gaps on wrapped lines. If there are three gaps and eight spare pixels, each gets two extra pixels and the first two receive one more. Quotient and remainder distribute the available space without losing pixels to rounding. The final line of a paragraph should not stretch a handful of words across the whole screen, so the paginator distinguishes `LineEnd::Wrapped` from `LineEnd::Last`. An explicit break or image ends a line with natural spacing too.

A hyphen added by layout is also not part of the source text. `TextFragment` carries a `hyphenated` flag, allowing rendering to show that extra mark without counting it as a new character in the book. Otherwise changing font size could change the supposedly stable content length simply by adding different line endings.

Pending pieces can be merged when they share style, link, geometry and adjacent ranges in the same source string. The “same source” check uses pointer equality, not equal contents. Two paragraphs containing the same words still have different source ranges. Coalescing legitimate neighbors reduces the number of fragments later work must process without inventing a new textual relationship.

The result is intentionally smaller in ambition than browser layout. It lays out the EPUB model and styles InkPaper supports. Arbitrary publisher CSS, unsupported shaping behavior or absent image dimensions do not become fully supported merely because the page model can hold rectangles.

## Borrow while arranging, own while keeping

Page fragments usually refer to text already held by the chapter. Copying every tentative piece while finding line endings would create unnecessary allocation and cleanup. The exact fragment declaration in `crates/reader/src/page.rs` uses `Cow` to postpone that ownership choice:

```rust,ignore
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TextFragment<'a> {
    text: Cow<'a, str>,
    bounds: Rect,
    style: TextStyle,
    link: Option<Cow<'a, LinkTarget>>,
    word_spacing: u32,
    hyphenated: bool,
}
```

`Cow<'a, str>` can contain either a borrowed `&'a str` or an owned `String`. Consumers can read either as text. The lifetime on `Pagination<'a>` reflects these possible chapter borrows, not the duration of the pagination calculation.

Inside the paginator, chapter and temporary context have separate lifetimes. Pages need the chapter's text after the algorithm finishes; they do not need to retain the mutable borrow of the measurer. Keeping those lifetimes distinct allows the caller to use its measurer again while still holding the result. A lifetime describes which data an output depends on, not a general “keep everything alive” instruction.

At the app boundary, the pages must outlive the parsed chapter. The loader converts the pagination to owned form before retaining it. This exact method performs the text-fragment part of that conversion:

```rust,ignore
pub fn into_owned(self) -> TextFragment<'static> {
    TextFragment {
        text: Cow::Owned(self.text.into_owned()),
        bounds: self.bounds,
        style: self.style,
        link: self.link.map(|link| Cow::Owned(link.into_owned())),
        word_spacing: self.word_spacing,
        hyphenated: self.hyphenated,
    }
}
```

Consuming `self` lets already owned data move into the result; borrowed data becomes owned. `'static` here does not mean the fragment is leaked or must live forever. It means it no longer depends on a short-lived chapter borrow. Normal ownership still determines when its storage is dropped.

Keeping the whole chapter alive would avoid this copying but retain its parse and style structure. Owning page fragments lets the app release that structure and render from a detached page model. Neither choice is universally cheaper. The useful design property is an explicit allocation boundary rather than hidden copying on every paint.

## Save a place that is not a page number

`ReadingPosition`, in `crates/reader/src/position.rs`, combines a `BookLocation` with a non-text counter. The location contains a spine index and a Unicode-scalar offset in normalized chapter text. The counter counts preceding images and explicit breaks, including hidden or unrenderable ones. An image-only page can therefore have a meaningful place even though it advances no text offset.

Hidden text likewise advances the content cursor without taking layout space. Source order remains stable when visibility or geometry changes. Derived ordering compares the location first and the non-text coordinate second, so page starts can be searched in reading order.

This stability has a scope: the same book revision and normalization rules. Scalar offsets are not magic identifiers that survive edited text or a parser changing its normalization. Nor are they byte indices suitable for directly slicing a Rust string. The reader uses bytes to select text in memory and scalar counts to describe its normalized content extent; each coordinate serves a different task.

After reflow, `page_at_position` finds the containing page. Here is the exact method from `pagination.rs`:

```rust,ignore
pub fn page_at_position(&self, position: ReadingPosition) -> Option<usize> {
    let index = self
        .pages
        .partition_point(|page| page.position() <= position)
        .checked_sub(1)?;
    let page = &self.pages[index];

    // empty chapters still have a single page that can be reopened
    (position < page.end_position()
        || (self.pages.len() == 1
            && position == page.position()
            && page.position() == page.end_position()))
    .then_some(index)
}
```

`partition_point` locates the first start greater than the requested position. The preceding page is the candidate; `checked_sub` handles the absence of one. The final test enforces exclusive ends, with an exception for the single empty page. A position outside the chapter is not silently accepted by this method.

The app's reflow flow captures the current page's **start**, not the reader's exact gaze location. It loads the newly paginated chapter and selects the page containing that saved start. The words remain reachable, although their vertical position may change. Paint-only settings can avoid repagination altogether. That is a realistic comfort guarantee, grounded in what the application can actually observe.

## Make one chapter wider

Optional, without hardware: trace `repagination_changes_page_count_not_canonical_content_extent` in `crates/reader/tests/pagination.rs`. It builds an EPUB containing “one two three four”, loads it through the public EPUB API and paginates with the one-unit test measurer. The two viewports are 7×2 and 10×2; all other inputs remain the same.

Run `cargo test -p inkpaper-reader --test pagination repagination_changes_page_count_not_canonical_content_extent` to exercise those existing assertions. The expected page counts are two and one respectively, while both final content offsets remain 18. Before running, account for the spaces as well as the letters. This small example isolates the principle behind comfortable reflow: the page arrangement changes, but the content coordinate system does not.
