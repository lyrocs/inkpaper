# 15. Opening an EPUB: zip, XML and CSS

> **Where we are:** 2 September 2026, a day of seven commits that built the
> `inkpaper-epub` crate from nothing: `d1f5f46` "add epub package parser",
> `246b8f6` navigation, `80d4773` resource access, `23144f8` XHTML content,
> `8222b3f` style context, `a3a8339` CSS, `858ea16` lazy images. Then weeks of
> hardening against real-world books (`05c9509`, `18bb5b7`, `3b63506`,
> `a3e0b7b`, `70afd3d`, `5c42220`).

## What an EPUB is

An EPUB is a **zip archive** containing:

- `META-INF/container.xml`, which says where the *package document* is;
- the package document (`.opf`), with metadata (title, author), a **manifest** of
  every file, and a **spine**, the reading order;
- a navigation document (EPUB 3 `nav.xhtml`, or EPUB 2 `toc.ncx`);
- chapters as **XHTML** files, styled with **CSS**, plus images and fonts.

So "open a book" means parsing four formats: zip, XML, XHTML and CSS. On a
laptop you'd pull in four big crates. On a microcontroller with a few hundred KB
of RAM, the project wrote its own small parsers on top of two lightweight
dependencies: `miniz_oxide` for decompression and `xmlparser` for tokenising XML.

## Reading without a filesystem

The parser can't assume `std::fs::File`. It defines what it needs, an
async random-access source:

```rust
#[allow(async_fn_in_trait)]
#[allow(clippy::len_without_is_empty)]
pub trait EpubSource {
    type Error;

    async fn len(&mut self) -> Result<u64, Self::Error>;

    async fn read_exact_at(&mut self, offset: u64, buffer: &mut [u8]) -> Result<(), Self::Error>;
}
```

That's two methods. On the device, the implementation sends RPCs to the storage
task (chapter 14). In the simulator it reads a host file. In tests it reads a
byte slice:

```rust
#[derive(Debug, Clone, Copy)]
pub struct SliceSource<'a> {
    bytes: &'a [u8],
}

impl EpubSource for SliceSource<'_> {
    type Error = SliceSourceError;

    async fn len(&mut self) -> Result<u64, Self::Error> {
        u64::try_from(self.bytes.len()).map_err(|_| SliceSourceError::OutOfBounds)
    }

    async fn read_exact_at(&mut self, offset: u64, buffer: &mut [u8]) -> Result<(), Self::Error> {
        let start = usize::try_from(offset).map_err(|_| SliceSourceError::OutOfBounds)?;
        let end = start
            .checked_add(buffer.len())
            .ok_or(SliceSourceError::OutOfBounds)?;

        let source = self
            .bytes
            .get(start..end)
            .ok_or(SliceSourceError::OutOfBounds)?;

        buffer.copy_from_slice(source);

        Ok(())
    }
}
```

This is careful conversion code. `u64` to `usize` can fail on a 32-bit target, so
it uses `try_from`. `start + len` can overflow, so it uses `checked_add`. And
`get(start..end)` returns `None` instead of panicking when the range is out of
bounds. Every `?` leads to the same error.

`#[allow(clippy::len_without_is_empty)]` silences a lint that expects every `len`
to come with an `is_empty`. An async source of unknown size has no cheap
`is_empty`, so the code says so instead of adding a pointless method.

## The opening sequence

```rust
pub struct Epub<S> {
    archive: Archive<S>,
    package: Package,
}

impl<S> Epub<S>
where
    S: EpubSource,
{
    pub async fn open(source: S) -> Result<Self, Error<S::Error>> {
        let mut archive = Archive::open(source).await?;

        let container_path = ArchivePath::new("META-INF/container.xml")
            .expect("the EPUB container path is statically valid");

        let container_bytes = archive.read_entry(&container_path).await?;
        let container_xml = core::str::from_utf8(&container_bytes).map_err(Error::Utf8)?;
        let container = parse_container(container_xml).map_err(Error::Container)?;
        let package_bytes = archive.read_entry(container.package_path()).await?;
        let package_xml = core::str::from_utf8(&package_bytes).map_err(Error::Utf8)?;
        let package =
            parse_package(package_xml, container.package_path().clone()).map_err(Error::Package)?;

        Ok(Self { archive, package })
    }
}
```

Read it top to bottom: open the zip, read `container.xml`, find the package path,
read and parse the package. Every step can fail, and each failure becomes a
variant of one error enum:

```rust
#[derive(Debug)]
pub enum Error<E> {
    Source(E),
    Archive(ArchiveError),
    Utf8(Utf8Error),
    Container(ContainerError),
    Package(PackageError),
    Navigation(NavigationError),
    Xhtml(XhtmlError),
}
```

It's generic over the source's error (the `Error<E>` pattern for the third time),
with one variant per stage. When a book fails to open, the variant tells you
where.

`.expect("the EPUB container path is statically valid")` is a deliberate panic.
`ArchivePath::new` returns a `Result` because paths from books can be invalid, but
this one is a literal that's known to be valid. `expect` with a message saying
*why it can't fail* is the idiomatic way to write that down.

`core::str::from_utf8` validates bytes as UTF-8 and returns a `&str` that
**borrows** the `Vec<u8>`. There's no copy. The XML parsers then work on slices of
that one string.

## Zip: reading a format from the end

A zip file's table of contents, the **central directory**, sits at the *end*,
located by an "end of central directory" (EOCD) record. That record ends with a
variable-length comment, so you have to *search backwards* for its signature:

```rust
const EOCD_SIGNATURE: u32 = 0x0605_4b50;
const EOCD_MIN_LEN: usize = 22;
const MAX_EOCD_SEARCH: usize = EOCD_MIN_LEN + u16::MAX as usize;

fn find_eocd(tail: &[u8]) -> Option<usize> {
    if tail.len() < EOCD_MIN_LEN {
        return None;
    }

    for index in (0..=tail.len() - EOCD_MIN_LEN).rev() {
        if le_u32(&tail[index..index + 4]) != EOCD_SIGNATURE {
            continue;
        }

        let comment_len = usize::from(le_u16(&tail[index + 20..index + 22]));

        if index + EOCD_MIN_LEN + comment_len == tail.len() {
            return Some(index);
        }
    }

    None
}
```

There are several small Rust lessons in here.

- **Numeric literals with underscores**: `0x0605_4b50` reads better than
  `0x06054b50`.
- **`(0..=n).rev()`**: an inclusive range iterated backwards. Ranges are
  iterators, and `rev` works because they're *double-ended*.
- **Validation, not just matching**: four bytes equal to the signature could
  appear by chance inside compressed data. The code accepts a candidate only if
  its comment length makes it end *exactly* at the end of the file.
- **Bounded search**: the comment is at most `u16::MAX` bytes, so the code reads
  at most `MAX_EOCD_SEARCH` bytes from the end, never the whole file.

The entry reader shows the same discipline with offsets read from the file,
which is **untrusted input**:

```rust
let data_offset = u64::from(entry.local_header_offset)
    .checked_add(LOCAL_HEADER_LEN as u64)
    .and_then(|offset| offset.checked_add(u64::from(name_len)))
    .and_then(|offset| offset.checked_add(u64::from(extra_len)))
    .ok_or(Error::Archive(ArchiveError::SizeOverflow))?;

let data_end = data_offset
    .checked_add(u64::from(entry.compressed_size))
    .ok_or(Error::Archive(ArchiveError::SizeOverflow))?;

if data_end > self.archive_len {
    return Err(Error::Archive(ArchiveError::InvalidLocalHeader));
}
```

A malicious or corrupted EPUB could claim an entry starts at offset
4,294,967,295. Every addition is checked, and the end is compared with the real
file length before anything is read. Then decompression:

```rust
COMPRESSION_DEFLATE => {
    let decompressed = decompress_to_vec_with_limit(&compressed, expected_len)
        .map_err(|_| Error::Archive(ArchiveError::Inflate))?;

    if decompressed.len() != expected_len {
        return Err(Error::Archive(ArchiveError::SizeMismatch {
            expected: entry.uncompressed_size,
            actual: decompressed.len(),
        }));
    }

    Ok(decompressed)
}
```

`decompress_to_vec_with_limit` refuses to inflate beyond the size the archive
declared. Without that, a "zip bomb" (a tiny file that expands to gigabytes)
would exhaust the device's memory. **Parsers of untrusted data must bound every
allocation**, and Rust's checked arithmetic and `Option`/`Result` APIs make that
cheap to write.

The error enum lists what's deliberately unsupported (`Zip64Unsupported`,
`EncryptedEntry`, `MultiDiskUnsupported`), so an unusual book fails with a clear
reason instead of garbage.

## Paths as a validated newtype

Zip entries are named by paths, and EPUBs reference each other with relative
paths like `../Images/cover.jpg`. The crate turns them into a validated
newtype:

```rust
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct ArchivePath(String);

impl ArchivePath {
    pub fn new(path: &str) -> Result<Self, PathError> {
        if path.is_empty() {
            return Err(PathError::Empty);
        }
        if path.starts_with('/') {
            return Err(PathError::Absolute);
        }
        if path.contains('\\') {
            return Err(PathError::Backslash);
        }

        let mut segments = Vec::new();

        for segment in path.split('/') {
            match segment {
                "" | "." => {}
                ".." => {
                    if segments.pop().is_none() {
                        return Err(PathError::EscapesRoot);
                    }
                }
                segment => segments.push(segment),
            }
        }
        // ... join the segments with '/' ...
    }
}
```

A `match` on **string slices** handles each segment: skip `""` and `"."`, pop on
`".."`, and fail if `..` would climb above the root. That last check is the
defence against *path traversal*. The final arm, `segment => ...`, binds whatever
didn't match earlier.

`segments` is a `Vec<&str>` that borrows from `path`, so normalising allocates one
small vector of slices and then one `String` for the result. Once an
`ArchivePath` exists, it's normalised and safe, and every other part of the crate
can rely on that.

`resolve` handles references relative to the current file, strips `#fragment` and
`?query`, and decodes percent-encoding (`My%20Book.xhtml`). Each of those was
added after a real book failed to open (`05c9509` "support encoded EPUB paths",
`8cd0fbc` "decode percent-encoded EPUB rootfile paths"). Commit `5c42220` changed
references that escape the book from "fail to open" to "skip that reference",
because one bad link shouldn't make a whole book unreadable.

## XML: a token loop and a trait

`xmlparser` is a **zero-copy tokeniser**: it yields tokens whose text fields are
`StrSpan`s, slices of the input string. No strings are allocated while scanning.
The simplest parser, for `container.xml`, matches tokens directly:

```rust
for token in Tokenizer::from(xml) {
    match token.map_err(ContainerError::Xml)? {
        Token::ElementStart { local, .. } => {
            in_rootfile = local.as_str() == "rootfile";
            // ...
        }

        Token::Attribute { local, value, .. }
            if in_rootfile && local.as_str() == "full-path" =>
        {
            full_path = Some(decode_xml_value(value.as_str()));
        }
        // ...
    }
}
```

Match arms with **guards** (`if in_rootfile && ...`) pick out exactly the
attribute we care about.

As the number of XML formats grew (package, nav, NCX, XHTML), commit `8ce45a3`
"shared the xml token loop" behind a trait:

```rust
/// A parser that [`parse_xml`] feeds one token at a time.
pub(crate) trait XmlParser {
    type Error;

    fn element_start(&mut self, name: &str);
    fn attribute(&mut self, name: &str, value: &str);
    fn text(&mut self, text: &str, cdata: bool);
    /// The end of a start tag; `empty` when the element closes itself.
    fn element_boundary(&mut self, empty: bool) -> Result<(), Self::Error>;
    fn close_element(&mut self, name: &str) -> Result<(), Self::Error>;
    /// Once an element has ended, after `element_boundary` for an empty one and
    /// `close_element` for the others.
    fn element_end(&mut self) {}
}

pub(crate) fn parse_xml<P: XmlParser>(
    xml: &str,
    parser: &mut P,
    xml_error: fn(xmlparser::Error) -> P::Error,
) -> Result<(), P::Error> {
    for token in Tokenizer::from(xml) {
        match token.map_err(xml_error)? {
            Token::ElementStart { local, .. } => parser.element_start(local.as_str()),
            Token::Text { text } => parser.text(text.as_str(), false),
            // ...
            _ => {}
        }
    }

    Ok(())
}
```

That's the **visitor pattern**, or SAX-style parsing. The loop drives, and each
format implements callbacks. `element_end` has a default empty body, so parsers
that don't care don't write it. `xml_error: fn(xmlparser::Error) -> P::Error`
lets each parser wrap tokeniser errors into its own error type, and callers pass
an enum variant like `XhtmlError::Xml`, a function once again.

### Entities

XHTML is full of `&amp;`, `&#x2014;` and `&nbsp;`. Decoding uses string-slice
methods instead of indexing by hand:

```rust
fn decode_entity(entity: &str) -> Option<char> {
    match entity {
        "amp" => Some('&'),
        "lt" => Some('<'),
        "gt" => Some('>'),
        "quot" => Some('"'),
        "apos" => Some('\''),
        _ if !entity.starts_with('#') => entities::lookup(entity),
        _ => {
            let value = if let Some(hex) = entity
                .strip_prefix("#x")
                .or_else(|| entity.strip_prefix("#X"))
            {
                u32::from_str_radix(hex, 16).ok()?
            } else {
                let decimal = entity.strip_prefix('#')?;
                decimal.parse::<u32>().ok()?
            };

            char::from_u32(value)
        }
    }
}
```

`strip_prefix` returns the rest of the string if the prefix matches, and `None`
otherwise. `from_str_radix` parses hexadecimal. `char::from_u32` returns `None`
for values that aren't valid Unicode scalar values (surrogates, or numbers above
`0x10FFFF`), so `&#xD800;` can't produce an invalid `char`. In Rust a `char` is
*always* valid, and the API forces you to handle the cases where it wouldn't be.
Unknown entities are kept verbatim (`&custom;`), which is the forgiving choice
for a reader.

## CSS: a cascade in a few lines

Books come with stylesheets, and InkPaper honours a useful subset: font weight
and style, alignment, margins, indent, line height, display. Each declaration
competes in the **cascade**: `!important` beats normal, then higher
**specificity** wins, then the later rule wins.

Specificity is a triple, and its ordering is derived:

```rust
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub(super) struct Specificity {
    ids: u16,
    classes: u16,
    elements: u16,
}
```

Deriving `PartialOrd`/`Ord` on a **struct** compares fields **lexicographically,
in declaration order**: first `ids`, then `classes` if the ids tie, then
`elements`. That's exactly CSS specificity. The field order *is* the algorithm, so
reordering the fields would silently change the cascade, which is worth a
comment in your own code if you use this trick.

The full rank isn't derived, because the tie-break uses `>=` (a later rule with
equal rank wins):

```rust
/// How a declaration ranks in the cascade.
#[derive(Debug, Clone, Copy)]
struct Rank {
    important: bool,
    specificity: Specificity,
    order: usize,
}

impl Rank {
    fn outranks(self, current: Self) -> bool {
        if self.important != current.important {
            return self.important;
        }
        if self.specificity != current.specificity {
            return self.specificity > current.specificity;
        }

        self.order >= current.order
    }
}

/// Keeps `value` when it outranks the candidate already in `current`.
fn consider<T>(current: &mut Option<Candidate<T>>, value: T, rank: Rank) {
    if current
        .as_ref()
        .is_none_or(|current| rank.outranks(current.rank))
    {
        *current = Some(Candidate { value, rank });
    }
}
```

`consider` is **generic over the property's value type**: the same function
handles `FontWeight`, `TextAlign` and `CssLength`. `Option::is_none_or` (stable
since Rust 1.82) reads exactly like the rule: keep the new value if there's no
current one, or if the new one outranks it.

Lengths are stored in **fixed point**, thousandths of a unit, so pagination never
sees a float:

```rust
/// a CSS length stored in thousandths of its unit.
///
/// keeping the parsed value fixed-point avoids carrying floating-point CSS values into pagination.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CssLength {
    value_milli: i32,
    unit: CssLengthUnit,
}

impl CssLength {
    pub fn resolve(self, font_size: u32, percentage_basis: u32) -> i32 {
        let (basis, divisor) = match self.unit {
            CssLengthUnit::Pixels => (1i64, FIXED_SCALE),
            CssLengthUnit::Em => (i64::from(font_size), FIXED_SCALE),
            CssLengthUnit::Percent => (
                i64::from(percentage_basis),
                100i64.saturating_mul(FIXED_SCALE),
            ),
        };

        let resolved = i64::from(self.value_milli).saturating_mul(basis) / divisor;

        i32::try_from(resolved).unwrap_or(if resolved < 0 { i32::MIN } else { i32::MAX })
    }
}
```

A `match` that returns a **tuple** picks the basis and divisor per unit, and one
formula handles all three. This is chapter 7's determinism argument again,
applied to book styles.

## Hardened by real books

Many commits in this crate have titles like:

- "keep reading a stylesheet past non-ASCII values and unclosed strings"
- "decode XHTML named entities"
- "resolve EPUB manifest fallback chains"
- "skip book references that point outside it instead of failing to open"

Each comes with a **fixture**: a small EPUB in `fixtures/` (like
`compat-encoded-container-path.epub`) generated by `cargo xtask fixtures` (see
`tools/xtask/src/fixtures`). The fixtures are built by code, not collected from the
internet, so they're reproducible, license-free, and each tests exactly one quirk.
When a real book fails, the workflow is: reproduce the quirk in a generated
fixture, write a failing test, fix the parser.

## Rust concepts in this chapter

- **A minimal I/O trait** (`EpubSource`) instead of `std::fs`.
- **Checked conversions and arithmetic** on untrusted input; `slice::get` with
  ranges.
- **Stage-specific error enums** generic over the source error; `expect` with a
  reason.
- **Zero-copy parsing**: `str::from_utf8`, string slices, tokens that borrow.
- **Reverse ranges** (`(0..=n).rev()`), numeric literal separators.
- **Bounded decompression** against zip bombs.
- **Validated newtypes** (`ArchivePath`); matching on `&str`.
- **Visitor/SAX pattern** with a trait and default methods; function pointers for
  error mapping.
- **`strip_prefix`, `from_str_radix`, `char::from_u32`**, and why `char` is always
  valid.
- **Derived lexicographic `Ord` on structs**; hand-written comparisons when the
  rule differs.
- **Generic helpers over value types** (`consider<T>`), `Option::is_none_or`.
- **Fixed-point numbers.**

## Try it yourself

1. Run `cargo test -p inkpaper-epub`. Then pick a fixture from `fixtures/`, unzip
   it, and read its `container.xml` and `.opf` side by side with `container.rs`
   and `package.rs`.
2. Add a test for `ArchivePath::new("OEBPS/../../etc/passwd")`. Which error do you
   expect?
3. Reorder the fields of `Specificity` to `elements, classes, ids` and run the CSS
   tests. Which one fails, and why?
