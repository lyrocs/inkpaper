# 18. The reader: pagination and positions

> **Where we are:** 3 September 2026: `db4d8f8` "add stable epub locations",
> `ce9bc96` "add reader text pagination", `395eee7` "paginate reader images",
> `939b5d1` "add reader page render model", `44b2c96` the app adapter,
> `8e8d4b9` "bridge reader metrics to ui fonts". Then the September integration
> (`a32966d` stable positions, `bd4528f` saved progress, `c863c97` whole-book
> progress) and the October typography work: justification (`c9c0491`),
> hyphenation (`8caf0f4`), links (`4a7a796`, `a8bbdb7`).

## The job

A chapter from chapter 15 is a list of blocks (paragraphs, headings, images)
with text runs and computed styles. A screen is 480×800 pixels. **Pagination**
cuts the first into pieces that fit the second:

```rust
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

This signature packs in most of the chapter's ideas:

- `chapter: &'a Chapter` and `-> Pagination<'a>`: **the pages borrow from the
  chapter**. A page's text is a slice of the chapter's text, not a copy.
- `measurer: &mut M` with `M: TextMeasurer + ImageMeasurer`: the paginator
  doesn't know about fonts. It asks a measurer how wide a piece of text is. The
  `+` combines two trait bounds.
- `Result<_, M::Error>`: errors are whatever the measurer's errors are.

## Measuring through a trait

```rust
pub trait TextMeasurer {
    type Error;

    fn measure_text(&mut self, text: &str, style: TextStyle) -> Result<u32, Self::Error>;

    fn line_height(&mut self, style: TextStyle) -> Result<u32, Self::Error>;

    fn next_boundary(
        &mut self,
        text: &str,
        from: usize,
        _style: TextStyle,
    ) -> Result<Option<usize>, Self::Error> {
        Ok(scalar_boundary(text, from))
    }
}

pub trait ImageMeasurer {
    fn image_dimensions(&mut self, _image: &ChapterImage) -> Option<ImageDimensions> {
        None
    }
}
```

The `inkpaper-reader` crate depends on `inkpaper-epub` and on nothing graphical.
The app implements `TextMeasurer` by shaping with the UI's fonts (`8e8d4b9`,
"bridge reader metrics to ui fonts"). The tests in `crates/reader/tests/pagination.rs`
implement it with a trivial `MonoMeasurer`, where every character is one unit
wide and every line one unit tall:

```rust
impl TextMeasurer for MonoMeasurer {
    type Error = Infallible;

    fn measure_text(&mut self, text: &str, _style: TextStyle) -> Result<u32, Self::Error> {
        Ok(text.chars().fold(0u32, |width, _| width.saturating_add(1)))
    }

    fn line_height(&mut self, _style: TextStyle) -> Result<u32, Self::Error> {
        Ok(1)
    }
}
```

That makes pagination tests deterministic and readable. A second test measurer,
`FailingMeasurer`, always returns an error, to check that errors propagate. **Inverting a dependency through a trait** is what
keeps the paginator testable without a single font file.

`ImageMeasurer` has *only* a default method. Implementing it is one line,
`impl ImageMeasurer for MyMeasurer {}`, for measurers that don't handle images.
Parameters prefixed with `_` (`_style`, `_image`) are used only by overriding
implementations. The underscore silences the unused-variable warning in the
default.

`&mut self` on measurement methods is deliberate. The real measurer *caches*
(see below), and caching mutates.

## Borrowing pages, then owning them

Here's a text fragment on a page:

```rust
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

**`Cow`** ("clone on write") is an enum from `alloc::borrow`:

```rust
enum Cow<'a, B: ?Sized + ToOwned> {
    Borrowed(&'a B),
    Owned(<B as ToOwned>::Owned),
}
```

A `Cow<'a, str>` is *either* a borrowed `&'a str` *or* an owned `String`, and
derefs to `&str` either way. Pagination produces borrowed fragments, which are
cheap to make, while the chapter is in memory. But the app wants to keep pages
around after the chapter's XHTML is dropped (to save memory), so every type
offers an `into_owned`:

```rust
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

The return type is `TextFragment<'static>`: nothing in it borrows anymore, so it
can live as long as anyone likes. The conversion cascades up:
`Page::into_owned` maps its items, and `Pagination::into_owned` maps its pages:

```rust
/// detatches this chapter's pages from their borrowed EPUB content.
///
/// allocation happens here, before the pages enter the rendering path
pub fn into_owned(self) -> Pagination<'static> {
    Pagination {
        pages: self.pages.into_iter().map(Page::into_owned).collect(),
    }
}
```

`self.pages.into_iter()` consumes the vector (taking ownership of each page),
`.map(Page::into_owned)` converts each, and `.collect()` builds a new `Vec`. The
type of the new vector is inferred from the struct field it's assigned to.

This "borrow while working, own when storing" pattern with `Cow` and a
`'static`-returning `into_owned` is common in Rust parsers and serialisers. The
doc comment pins down *where* the allocation cost is paid: before rendering, not
during it.

## The paginator's lifetimes

The paginator holds two kinds of borrows with different lifetimes:

```rust
struct Paginator<'chapter, 'context, M> {
    chapter: &'chapter Chapter,
    styles: &'context ChapterStyles,
    viewport: Viewport,
    settings: ReaderSettings,
    measurer: &'context mut M,

    pages: Vec<Page<'chapter>>,
    page_items: Vec<PageItem<'chapter>>,
    // ...
}
```

Why two lifetimes? The pages it produces borrow from the **chapter** and must be
allowed to outlive the paginator. The styles and the measurer are only needed
**during** pagination. If everything shared one lifetime, the returned pages would
be tied to the measurer's borrow too, and the caller couldn't use the measurer
again while holding the pages. Separate lifetimes say precisely which borrow each
output depends on. When a struct borrows several things for different reasons,
give each reason its own lifetime.

### Merging text with pointer identity

While laying out a line, the paginator creates pending pieces of text and merges
adjacent ones with the same style:

```rust
fn can_merge_with(&self, next: &Self) -> bool {
    core::ptr::eq(self.source, next.source)
        && self.end == next.start
        && self.style == next.style
        && self.link == next.link
        && self.x.saturating_add(self.width) == next.x
        && !self.hyphen
        && (self.word_spacing.is_none()
            || next.word_spacing.is_none()
            || self.word_spacing == next.word_spacing)
}
```

`core::ptr::eq(self.source, next.source)` checks that both pieces point into the
**same string in memory**, not just equal strings. Two runs that happen to
contain the same text must not be merged, because their byte ranges refer to
different sources. `==` on `&str` would compare contents, while `ptr::eq`
compares addresses. Fewer, longer fragments mean less shaping work at paint time
(`b478d6d`, "coalesce reader text fragments").

## Justification

Justified text stretches the spaces in a line so it reaches the right margin.
The spare pixels rarely divide evenly between the gaps:

```rust
fn justify(items: &mut [PendingText<'_>], spare: u32) {
    let gaps = items.iter().filter(|item| item.is_gap()).count();
    let Ok(gaps) = u32::try_from(gaps) else {
        return;
    };

    if gaps == 0 || spare == 0 {
        return;
    }

    let extra = spare / gaps;
    let mut wider = spare % gaps;
    let mut shift = 0u32;

    for item in items {
        item.x = item.x.saturating_add(shift);

        if !item.is_gap() {
            continue;
        }

        let stretch = if wider > 0 {
            wider -= 1;
            extra.saturating_add(1)
        } else {
            extra
        };

        item.width = item.width.saturating_add(stretch);
        item.word_spacing = Some(stretch);
        shift = shift.saturating_add(stretch);
    }
}
```

Every gap gets `spare / gaps` pixels, and the first `spare % gaps` gaps get one
more. The sum is exactly `spare`. It's the same "never lose a pixel" concern as
flex layout in chapter 7, solved with quotient and remainder this time.

`for item in items` over a `&mut [T]` yields `&mut T`, so `item.x = ...` mutates
the slice in place. The `'_` in `PendingText<'_>` says "this function doesn't care
which lifetime the pending texts borrow from".

Only *wrapped* lines are justified, never the last line of a paragraph. That
distinction gets its own enum instead of a `bool`:

```rust
/// why a line ended. Only wrapped lines are justified, so the last line of a
/// paragraph, or one before a `<br>` or an image, keeps its natural spacing
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum LineEnd {
    Wrapped,
    Last,
}
```

`self.flush_line(LineEnd::Last)` reads better than `self.flush_line(false)`. Two-variant
enums instead of boolean parameters are a cheap readability win.

## Hyphenation

When a long word doesn't fit, it can be broken at a hyphenation point. The
`hypher` crate provides patterns, and the reader wraps it:

```rust
/// A language the reader can hyphenate. Each comes from a `hypher` feature in
/// the workspace's `Cargo.toml`, so supporting another is one feature away.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct HyphenationLanguage(Lang);

impl HyphenationLanguage {
    pub const ENGLISH: Self = Self(Lang::English);

    /// The language of a tag like `en` or `en-US`, or `None` without patterns
    /// for it.
    pub fn from_tag(tag: &str) -> Option<Self> {
        let primary = tag.trim().split(['-', '_']).next()?;

        let &[first, second] = primary.as_bytes() else {
            return None;
        };

        Lang::from_iso([first.to_ascii_lowercase(), second.to_ascii_lowercase()]).map(Self)
    }

    /// Byte offsets inside `word` where it may break, in order.
    pub(crate) fn breaks(self, word: &str) -> impl Iterator<Item = usize> + '_ {
        let (left, right) = self.bounds();

        hypher::hyphenate_bounded(word, self.0, left, right)
            .scan(0, |offset, syllable| {
                *offset += syllable.len();
                Some(*offset)
            })
            .filter(move |&offset| offset < word.len())
    }
}
```

There are three neat features here.

- **`split(['-', '_'])`**: `str::split` accepts an array of `char`s as a pattern
  and splits on any of them.
- **`let &[first, second] = primary.as_bytes() else { ... }`**: a **slice
  pattern**. It matches only slices of exactly two bytes and binds them by value.
  One line checks the length and destructures the data.
- **`scan`** is `fold` that yields every intermediate value: the running sum of
  syllable lengths gives the byte offset of each break. The result is returned as
  `impl Iterator<Item = usize> + '_`, a lazy iterator that borrows `word` (the
  `'_`). `filter(move |&offset| ...)` uses `move` so the closure owns its copy of
  `word` (a `&str`, so copying it is cheap), and `|&offset|` destructures the
  `&usize` the filter receives.

## Positions that survive repagination

When you change the font size, every page boundary moves. "Page 37" becomes
meaningless. So reading progress is stored as a **position in the text**, not a
page number:

```rust
/// a position in one book's normalized chapter flow
/// - `location` counts text as EPUB Unicode scalar offsets.
/// - `non_text` counts all preceeding images and explicit breaks in the chapter, including
///   hiddren or unrenderable ones.
///
/// Neither coordinates depend on fonts or page geometry.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct ReadingPosition {
    location: BookLocation,
    non_text: u64,
}
```

`BookLocation` is `(spine index, character offset)`. Both derive `Ord`, and
because derived ordering is lexicographic over fields (chapter 15), positions
compare in reading order: first by chapter, then by offset, then by images.

The offset counts **Unicode scalar values** (`char`s), not bytes:

```rust
pub fn advance_text(self, text: &str) -> Self {
    let length = text
        .chars()
        .fold(0u64, |length, _| length.saturating_add(1));

    Self(self.0.saturating_add(length))
}
```

Why chars and not bytes, when chapter 17 preferred bytes? Because this number
is *persisted* and must mean the same thing across versions of the parser.
Character counts are independent of how the text happened to be stored, so
they're the more stable choice for a saved position. Byte offsets are the right
tool for slicing in memory, and character offsets for describing a place in a
book.

Finding the page for a position after repagination is a binary search:

```rust
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

`partition_point(pred)` returns the first index where `pred` turns false, in a
sorted slice in O(log n). Here that's the first page starting *after* the
position, so the page before it (`checked_sub(1)?`, which returns `None` if
there is none) is the candidate. A boolean expression followed by
`.then_some(index)` turns "is it really inside?" into an `Option`. The comment
explains the one odd case, an empty chapter.

## Caching measurements

Pagination measures the same short strings again and again ("the", "and", a
space). The app's measurer keeps a small cache (`8cad282`, "cache repeated reader
text measurements"):

```rust
pub(super) const READER_MEASURE_CACHE_SLOTS: usize = 64;
pub(super) const READER_MEASURE_CACHE_TEXT_BYTES: usize = 32;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct ReaderMeasureCacheEntry<const TEXT_BYTES: usize> {
    style: TextStyle,
    len: u8,
    text: [u8; TEXT_BYTES],
    width: u32,
}

impl<const TEXT_BYTES: usize> ReaderMeasureCacheEntry<TEXT_BYTES> {
    fn matches(&self, text: &str, style: TextStyle) -> bool {
        let len = usize::from(self.len);

        self.style == style && len == text.len() && self.text[..len] == *text.as_bytes()
    }
}

pub(super) struct ReaderMeasureCache<const SLOTS: usize, const TEXT_BYTES: usize> {
    slots: [Option<ReaderMeasureCacheEntry<TEXT_BYTES>>; SLOTS],
}
```

Keys are stored **inline** as up to 32 bytes, so the cache never allocates and
never holds a borrow. Longer strings bypass the cache, which is the
`ReaderMeasureCacheLookup::Bypass` variant. `self.text[..len] == *text.as_bytes()`
compares a slice of the array with the string's bytes. The `*` dereferences
`&[u8]` to `[u8]` so both sides have the same type.

## Rust concepts in this chapter

- **Dependency inversion through traits** (`TextMeasurer`), multiple bounds with
  `+`, default-only traits, `_`-prefixed parameters.
- **Outputs that borrow inputs** (`Pagination<'a>` from `&'a Chapter`).
- **`Cow<'a, str>`**, `into_owned`, returning `'static` versions of borrowed types.
- **Several lifetimes on one struct** for borrows with different purposes.
- **`core::ptr::eq`** vs `==` on references.
- **Quotient and remainder** to distribute evenly; `for x in &mut slice`.
- **Two-variant enums instead of `bool` parameters.**
- **Slice patterns** (`let &[a, b] = bytes else`), `split` with char arrays,
  `scan`, returning `impl Iterator + '_`, `move` closures.
- **Derived `Ord` for composite positions**; chars for persisted offsets, bytes
  for slicing.
- **`partition_point`**, `checked_sub(1)?`, `bool::then_some`.
- **Inline fixed-size cache keys.**

## Try it yourself

1. Write a `FixedWidthMeasurer` (every `char` is 10 px, lines are 20 px) and
   paginate a chapter from one of the `fixtures/` books in a test. Check how many
   pages you get at a 200×200 viewport.
2. Try to return a `Pagination<'a>` from a function after dropping the `Chapter`
   it came from. Read the error, then fix it with `into_owned`.
3. Call `HyphenationLanguage::from_tag("en-US")`, `("EN")`, `("eng")` and
   `("")`. Predict each result from the slice pattern.
