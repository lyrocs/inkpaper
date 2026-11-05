# 19. Macros: from `macro_rules!` to `rsx!`

> **Where we are:** 11–14 September 2026. `5500bb4` "generate style utilities
> from a schema", `4abd05f` "add rsx macro", then a run of rsx features over two
> days (Tailwind values and colours, text, images, components, Svelte-style
> `{#if}` and `{#each}`, keyed lists, events), `7447a79` "add rsx compile tests",
> `c2998ea` "add component macro" and `d51aa2d` "add compile-time svg inclusion".

## Why macros at all?

By September the builder API from chapter 2 worked well, but real screens were
getting long:

```rust
div()
    .absolute()
    .left(px(20))
    .top(px(0))
    .w(px(440))
    .h(height)
    .rounded_md()
    .bg(background)
```

Rust has two kinds of macros to make code like this shorter and safer:

- **Declarative macros** (`macro_rules!`): pattern-matching on token trees, defined
  inline in any crate.
- **Procedural macros**: Rust functions, compiled into a separate crate, that
  receive tokens and return tokens. There are three flavours: function-like
  (`rsx!(...)`), attribute (`#[component]`) and derive (`#[derive(Encode)]`).

InkPaper uses both, and this chapter takes them in order of complexity.

## A schema, written once, read twice

The `Style` struct has dozens of fields, and each field has builder methods
(`flex()`, `w(...)`, `p(...)`) and *also* Tailwind-like class names for `rsx!`
(`flex`, `w-full`, `p-4`). Keeping three lists in sync by hand is a recipe for
drift. Commit `5500bb4` put the truth in one place, a **schema macro** in the tiny
crate `inkpaper-ui-style-schema`:

```rust
#[macro_export]
macro_rules! inkpaper_style_schema {
    ($declare:ident) => {
        $declare! {
            #[derive(Debug, Clone, Copy, PartialEq, Eq)]
            pub struct Style {
                layout {
                    display: Display = Display::Block => {
                        flex => Display::Flex;
                    },

                    position: Position = Position::Static => {
                        relative => Position::Relative;
                        absolute => Position::Absolute;
                    },

                    flex_direction: FlexDirection = FlexDirection::Row => {
                        flex_row => FlexDirection::Row;
                        flex_col => FlexDirection::Column;
                    },

                    flex_grow: u16 = 0 => {
                        @tailwind(Integer, class = "grow")
                        flex_grow(weight: u16) => weight;
                    },
                    // ... many more fields ...
                }
            }
        }
    };
}
```

Look at the first rule: `($declare:ident) => { $declare! { ... } }`. The schema
macro takes the **name of another macro** and calls it with the schema as input.
This is the **callback pattern** for `macro_rules!`: the schema doesn't know
what will be generated from it, and each consumer passes its own generator.

Two consumers exist. In `inkpaper-ui`, a `declare_style` macro generates the
`Style` struct, its `Default`, and the builder methods:

```rust
macro_rules! declare_style {
    (
        $(#[$attr:meta])*
        pub struct Style {
            $(
                $invalidation:ident {
                    $(
                        $field_name:ident: $field_ty:ty = $field_default:expr => {
                            $($field_utilities:tt)*
                        }
                    ),* $(,)?
                }
            )*
            // ...
        }
    ) => {
        $(#[$attr])*
        pub struct Style {
            $(
                $(
                    pub $field_name: $field_ty,
                )*
            )*
            pub(crate) text: TextStyle,
        }

        impl Default for Style {
            fn default() -> Self {
                Self {
                    $(
                        $(
                            $field_name: $field_default,
                        )*
                    )*
                    text: TextStyle::default(),
                }
            }
        }
        // ... builder methods from $field_utilities ...
    };
}

inkpaper_ui_style_schema::inkpaper_style_schema!(declare_style);
```

Reading a `macro_rules!` pattern takes some practice:

- `$field_name:ident` matches an identifier, `$field_ty:ty` a type,
  `$field_default:expr` an expression, `$attr:meta` an attribute's contents, and
  `$x:tt` any single **token tree** (a token, or a bracketed group).
- `$( ... ),*` repeats, separated by commas, and `$(,)?` allows an optional
  trailing comma.
- In the output, `$( ... )*` repeats once per match. **Nested repetitions** (`$(
  $( ... )* )*`) walk the nested structure: once per group, then once per field.
- `$($field_utilities:tt)*` captures a chunk of tokens *without interpreting
  them*, to be handed to another macro later.

Note that the schema groups fields under names like `layout`. That's the
`$invalidation:ident`: changing a `layout` field needs a new layout, while
changing a paint field only needs a repaint (chapter 9). The schema carries that
knowledge too.

In `inkpaper-ui-macros`, a different callback, `declare_style_specs`, reads the
same schema to build the table of utility classes that `rsx!` understands. One
schema produces the struct, the builder API and the class vocabulary, so they
can't disagree.

`#[macro_export]` places the macro at the root of its crate, so other crates call
it as `inkpaper_ui_style_schema::inkpaper_style_schema!`.

## Procedural macros: a crate that runs at compile time

A procedural macro crate is declared with:

```toml
[lib]
proc-macro = true

[dependencies]
proc-macro2 = { workspace = true }
quote = { workspace = true }
syn = { workspace = true, features = ["full"] }
rstml = "0.13.0"
roxmltree = "0.21.1"
svgtypes = "0.16.1"
```

It is compiled for the *host* and loaded into the compiler. Since it runs on
your laptop at build time, it can use `std`, read files and pull in big
dependencies, none of which end up in the firmware. That's why `inkpaper-ui-macros`
can parse SVG with `roxmltree` while the UI crate stays `no_std`.

The standard toolkit:

- **`syn`** parses Rust tokens into a syntax tree (`ItemStruct`, `Expr`,
  `LitStr`...).
- **`quote`** turns Rust-like templates back into tokens: `quote! { impl #name { ... } }`.
- **`proc-macro2`** wraps the compiler's token types so the code can be unit
  tested outside the compiler.

The entry points live in `lib.rs`:

```rust
#[proc_macro]
pub fn rsx(input: TokenStream) -> TokenStream {
    match rsx::expand_rsx(input.into()) {
        Ok(tokens) => tokens.into(),
        Err(error) => error.into_compile_error().into(),
    }
}

#[proc_macro_attribute]
pub fn component(args: TokenStream, input: TokenStream) -> TokenStream {
    if !args.is_empty() {
        return syn::Error::new(Span::call_site(), "`component` does not accept arguments")
            .into_compile_error()
            .into();
    }

    match component::expand_component(input.into()) {
        Ok(tokens) => tokens.into(),
        Err(error) => error.into_compile_error().into(),
    }
}
```

The pattern repeats: convert to `proc_macro2::TokenStream` with `.into()`, call
an inner function that returns `syn::Result<TokenStream>`, and on error emit
`into_compile_error()`. That produces a `compile_error!("...")` invocation
**attached to a span**, so the error appears underlined at the user's code, not
inside the macro. Proc macros should never `panic!` on bad input, because a panic
gives the user an unhelpful "proc macro panicked" message.

## `#[component]`: generating a props struct

App components are `RenderOnce` structs. `RenderOnce` is the stateless
counterpart of chapter 3's `Render`: a value that renders once, is consumed, and
needs no entity:

```rust
pub trait RenderOnce: Sized {
    fn render(self, cx: &AppContext<'_>) -> impl IntoElement;
}
``` Constructing them by struct literal would
expose their fields. The `#[component]` attribute generates a public **props**
struct and a `From` conversion:

```rust
Ok(quote! {
    #component

    #[doc(hidden)]
    #visibility struct #props_ident #generics {
        #props_fields
    }

    impl #impl_generics ::core::convert::From<#props_ident #ty_generics> for #ident #ty_generics
    #where_clause
    {
        fn from(#props_binding: #props_ident #ty_generics) -> Self {
            #construction
        }
    }
})
```

`#name` inside `quote!` interpolates a variable. `#( #attrs )*` repeats over an
iterator, like `macro_rules!` repetitions. The most important line is this one:

```rust
let (impl_generics, ty_generics, where_clause) = generics.split_for_impl();
```

A struct like `ListRow<'a>` has generics, and an `impl` needs them in three
slightly different forms (`impl<'a>`, `ListRow<'a>`, `where ...`). `syn` provides
`split_for_impl` so macro authors don't have to rebuild them by hand. If you
forget generics in a derive or attribute macro, it works on simple structs and
breaks on the first one with a lifetime.

The generated props struct is what components are built from:

```rust
BatteryLabel::from(BatteryLabelProps { battery })
```

and what `rsx!` builds when you write `<BatteryLabel battery={battery} />`.

The macro also has unit tests that run it as an ordinary function and parse the
output back with `syn` to check its structure:

```rust
#[test]
fn expands_named_component() {
    let output = expand_component(quote! {
        pub(crate) struct Row<'a> {
            label: &'a str,
            selected: bool,
        }
    })
    .expect("component should expand");

    let file: syn::File = syn::parse2(output).expect("expanded component should be valid Rust");

    assert_eq!(file.items.len(), 3);
    // ...
}
```

This is why the code is written against `proc_macro2`: the compiler's own
`proc_macro` types only exist inside a running compiler, but `proc_macro2` works
in tests too.

## `rsx!`: HTML-like syntax that compiles to builders

`rsx!` lets screens be written like markup. Here's a real component from the app:

```rust
rsx! {
    <div class="w-full h-{height} relative">
        <div class="absolute left-5 top-0 w-[440px] h-{height} rounded-md bg-{background}" />

        {#if let Some(listener) = self.on_activate}
            <div
                id={self.id}
                on:activate={listener}
                class="absolute left-5 top-0 w-[440px] h-{height} rounded-md"
            />
        {/if}

        <div class="absolute left-{label_left} top-0 w-{label_width} h-{height} flex items-center">
            <text class="text-xl no-wrap max-lines-1 text-ellipsis">
                {self.label}
            </text>
        </div>

        {#if self.chevron}
            <div class="absolute right-7 top-0 h-{height} flex items-center">
                <Icon kind={IconKind::ChevronRight} size={px(24)} />
            </div>
        {/if}
    </div>
}
```

The macro turns all of this into the builder calls from chapter 2 **at compile
time**. There's no string parsing at runtime, and nothing in the firmware knows
that "rounded-md" was ever a string.

- **Static classes** like `flex`, `items-center` and `w-[440px]` become method
  calls with constant arguments.
- **Dynamic classes** like `h-{height}` and `bg-{background}` interpolate Rust
  expressions into the call, `.h(height)`.
- **`{#if ...}...{/if}`** and **`{#each items as item}...{/each}`** are
  Svelte-style control flow, expanded to conditional and repeated children
  (`Either` and `PushEach`, relatives of chapter 2's `Push`).
- **`on:activate={listener}`** becomes `.on_activate(listener)`, and `id={...}`
  becomes `.id(...)`.
- **`<Icon kind={...} size={...} />`** builds the component's props struct.

Parsing the HTML-like syntax is delegated to `rstml`, with a **custom node type**
for the control-flow blocks:

```rust
pub(super) type RsxNode = Node<ControlFlow>;

pub(crate) fn expand_rsx(input: TokenStream) -> syn::Result<TokenStream> {
    let nodes =
        Parser::new(ParserConfig::new().custom_node::<ControlFlow>()).parse_simple(input)?;

    let root = match nodes.as_slice() {
        [] => {
            return Err(syn::Error::new(
                Span::call_site(),
                "rsx! requires exactly one root element",
            ));
        }
        [root] => root,
        [_, second, ..] => {
            return Err(syn::Error::new_spanned(
                second,
                "rsx! requires exactly one root element",
            ));
        }
    };

    match root {
        Node::Element(element) => element::expand_element(element),
        Node::Custom(control_flow) => control_flow::expand_control_flow(control_flow),
        Node::Fragment(_) => Err(syn::Error::new_spanned(
            root,
            "an rsx! fragment cannot be the root because InkPaper requires one mounted root element",
        )),
        _ => Err(syn::Error::new_spanned(
            root,
            "the root of rsx! must be an element or a control-flow block",
        )),
    }
}
```

Look at `match nodes.as_slice()` with **slice patterns**: `[]` is empty, `[root]`
has exactly one element, and `[_, second, ..]` has at least two, binding the
second. That last arm then points the error **at the second root** with
`Error::new_spanned(second, ...)`, so the compiler underlines exactly the
offending element. Good macro errors point at the user's mistake, not at the
macro call.

### Errors at compile time, tested

What happens when you write a class that doesn't exist?

```rust
fn main() {
    let _ = rsx! {
        <div class="definitely-not-a-utility" />
    };
}
```

```text
error: unknown utility class `definitely-not-a-utility`
 --> tests/ui/rsx/fail/unknown_utility.rs:5:20
  |
5 |         <div class="definitely-not-a-utility" />
  |                    ^^^^^^^^^^^^^^^^^^^^^^^^^^
```

And a layout class on text, which has no box?

```text
error: utility class `p-4` is not valid on <text>
 --> tests/ui/rsx/fail/layout_utility_on_text.rs:5:21
  |
5 |         <text class="p-4">
  |                     ^^^^^
```

Those messages are **tested**. Commit `7447a79` added
[`trybuild`](https://crates.io/crates/trybuild), which compiles small programs
that are *supposed to fail* and compares the compiler's output with saved
`.stderr` files:

```rust
#[test]
fn rsx_ui() {
    let tests = trybuild::TestCases::new();
    tests.compile_fail("tests/ui/rsx/fail/*.rs");
}
```

If a refactor makes an error message worse, or makes bad code compile, this test
fails. For any library whose job is to *reject* incorrect code, compile-fail tests
belong in the suite.

Under the hood, the class check uses the spec table generated from the schema,
and each class knows which elements accept it:

```rust
impl ClassTarget {
    fn accepts(self, spec: &UtilitySpec) -> bool {
        match self {
            Self::Div => matches!(
                spec.receiver,
                UtilityReceiver::Styled | UtilityReceiver::TextStyled
            ),
            Self::Text => spec.receiver == UtilityReceiver::TextStyled,
            Self::Image => spec.receiver == UtilityReceiver::Image,
            Self::Svg => matches!(
                spec.receiver,
                UtilityReceiver::Svg | UtilityReceiver::TextStyled
            ),
        }
    }
}
```

## `include_svg!`: assets compiled into code

Icons come from the Lucide icon set as SVG files. Parsing SVG on the device would
need an XML parser and a path parser in the firmware. Instead, `include_svg!`
parses the file **at compile time** and emits Rust constants describing the
paths:

```rust
const LIGHTBULB: SvgSource = include_svg!("assets/icons/lucide/lightbulb.svg");
```

The macro resolves the path relative to the calling crate, reads the file, parses
it with `roxmltree` and `svgtypes`, and reports problems as spanned errors:

```rust
pub(crate) fn expand_include_svg(path: LitStr) -> syn::Result<TokenStream> {
    let resolved = resolve_include_path(&path)?;

    let source = fs::read_to_string(&resolved).map_err(|error| {
        syn::Error::new(
            path.span(),
            format!("could not read SVG `{}`: {error}", resolved.display(),),
        )
    })?;

    let parsed = parse_svg(&source).map_err(|error| {
        syn::Error::new(
            path.span(),
            format!("could not parse SVG `{}`: {error}", resolved.display(),),
        )
    })?;

    let dependency_path = resolved.to_string_lossy();
    let dependency_path = LitStr::new(&dependency_path, path.span());

    Ok(emit_svg(parsed, dependency_path))
}
```

`resolve_include_path` reads the `CARGO_MANIFEST_DIR` environment variable, which
Cargo sets to the directory of the crate *being compiled*, so the path is
resolved against the crate that uses the macro, not against the macro crate.

There's one subtle trick in the generated code:

```rust
const _: &str = include_str!(#dependency_path);
```

Cargo doesn't know that a proc macro read a file, so editing the SVG wouldn't
trigger a rebuild. Emitting a dummy `include_str!` of the same file makes the
compiler record it as a dependency. `const _: ... = ...;` is an **unnamed
constant**, evaluated and then discarded. It exists purely for this side effect.
This is a well-known workaround until stable proc macros get a proper "track this
file" API.

## Rust concepts in this chapter

- **`macro_rules!`** fragments (`ident`, `ty`, `expr`, `meta`, `tt`), repetitions
  (`$(...),*`, nested repetitions, `$(,)?`), `#[macro_export]`.
- **The callback pattern**: passing a macro's name to another macro, one schema
  for many generators.
- **Procedural macros**: `proc-macro = true`, function-like, attribute and derive
  flavours, running on the host with `std`.
- **`syn`, `quote`, `proc-macro2`**; `split_for_impl` for generics.
- **Spanned errors** with `syn::Error` and `into_compile_error`, never panicking.
- **Slice patterns** in `match` (`[]`, `[x]`, `[_, second, ..]`).
- **Testing macros** as functions with `proc_macro2`, and **compile-fail tests**
  with `trybuild`.
- **Compile-time assets**, `CARGO_MANIFEST_DIR`, `const _` and `include_str!`
  for rebuild tracking.

## Try it yourself

1. Run `cargo expand` (install with `cargo install cargo-expand`) on a small
   function using `rsx!` in `crates/ui/tests/rsx.rs`, and read the builder calls
   it produces.
2. Add a new compile-fail case to `crates/ui/tests/ui/rsx/fail/`, for example an
   `on:activate` on a `<text>`, and run the `rsx_ui` test with
   `TRYBUILD=overwrite` to record its message. Is the message helpful? Where
   would you improve it?
3. Write a tiny `macro_rules!` callback pair of your own: a `colors!` schema
   listing `(name, r, g, b)` tuples, one generator that makes `const`s and one
   that makes a `fn all() -> &'static [(&'static str, Color)]`.
