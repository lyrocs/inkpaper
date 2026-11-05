# 19. Macros that make a screen readable

Imagine adjusting a book row so the title has more breathing room. You want to see the row, its title, and its optional icon together—not spend your attention matching a long chain of builder calls to the screen. But moving the interface into a separate runtime language would introduce another parser, another set of errors, and another boundary across which Rust's types cannot help.

InkPaper's macros offer a different bargain: write a convenient notation, translate it while compiling, and leave ordinary Rust to construct the interface. The interesting question is not how many keystrokes this saves. It is which facts the notation preserves, which mistakes it catches, and where its apparent simplicity ends.

## A screen description is still a Rust expression

Here is an exact test from `crates/ui/tests/rsx.rs`. It assumes that file's prelude import and `assert_into_element` helper; it is not a standalone program.

```rust,ignore
#[test]
fn rsx_supports_rust_values_inside_classes() {
    let padding = px(10);
    let width = px(240);
    let background = Color::rgb(240, 240, 240);

    let tree = rsx! {
        <div class="w-{width} p-{padding} bg-{background}" />
    };

    assert_into_element(tree);
}
```

The string-like appearance is deceptive. The macro reads the class attribute during compilation, recognizes width, padding, and background utilities, and emits calls containing those Rust expressions. The device does not split this class string when drawing. The values themselves can still be computed at runtime: compiling the notation away does not make `width` a constant.

Nor is this a browser. The supported elements, utilities, and rules belong to InkPaper. A `<text>` does not acquire a CSS box just because the syntax resembles HTML. Thinking of `rsx!` as an alternative spelling for the element API gives a much more reliable intuition than importing expectations from web development.

The bridge is visible in this exact function from `crates/ui-macros/src/rsx/element.rs`:

```rust,ignore
fn expand_div(element: &RsxElement, implicit_id: Option<&Expr>) -> syn::Result<TokenStream> {
    let attributes = parse_intrinsic_attributes(element)?;

    let expression = apply_intrinsic_attributes(
        quote! { ::inkpaper_ui::div() },
        attributes,
        ClassTarget::Div,
        implicit_id,
        element,
    )?;

    expand_children_into(expression, element.children())
}
```

`quote!` produces Rust tokens rather than executing `div()`. Attributes transform that token expression, then children are attached to it. `?` propagates an expansion error, not a device error. This function runs in the compiler's host environment: `crates/ui-macros/Cargo.toml` declares `proc-macro = true`.

That separation lets a procedural macro use parsing libraries and heap-backed collections without putting those parsers in firmware. The generated expression still incurs whatever construction and rendering work its builder equivalent would incur. Macros relocate interpretation; they do not repeal runtime costs.

## One vocabulary, two readers

The builder API and the class notation both need to know what width means. Maintaining unrelated inventories would make it easy to add a builder method but forget its class spelling. InkPaper instead describes style fields in a declarative macro, then lets different consumers read that description.

This exact fragment is inside `inkpaper_style_schema!` in `crates/ui-style-schema/src/lib.rs`; it is schema input, not a Rust struct declaration you can compile alone:

```rust,ignore
flex_grow: u16 = 0 => {
    @tailwind(Integer, class = "grow")
    flex_grow(weight: u16) => weight;
},

flex_shrink: u16 = 0 => {
    @tailwind(Integer, class = "shrink")
    flex_shrink(weight: u16) => weight;
},
```

The outer macro matches `($declare:ident)` and invokes `$declare!` with its entire schema. Here `ident` means “an identifier,” so the caller supplies another macro's name. This callback arrangement keeps the data in one place without forcing every consumer to generate the same thing.

In `crates/ui/src/style/mod.rs`, `declare_style` turns the schema into fields, defaults, style patches, and builder methods. In `crates/ui-macros/src/rsx/spec.rs`, `declare_style_specs` gathers utility descriptions used by the class parser. The annotation above supplies a class spelling and a value category; the method supplies its Rust-facing name and argument type.

Reading the consumers takes only a small piece of macro grammar. `$field_name:ident` captures a name, `$field_ty:ty` a type, and `$field_default:expr` an expression. `$( ... )*` repeats a pattern; nested repetitions walk groups and their fields. Capturing `$($field_utilities:tt)*` keeps a sequence of token trees for another macro to interpret. A token tree can be a token or a balanced delimiter group, so the first reader need not understand every embedded expression.

The groups also carry invalidation information: layout and paint changes need different amounts of subsequent work. This is a useful reason to generate code, beyond brevity. Several APIs need the same semantic fact. A shared schema reduces duplication, although the consumers still need tests: sharing input does not prove that both interpret it correctly.

## Errors belong at the screen declaration

A notation is only pleasant if its errors point back to what you wrote. `rsx!` uses `rstml` to parse its markup, with custom nodes for its control-flow syntax. It then requires exactly one root. This exact selection from `crates/ui-macros/src/rsx/mod.rs` shows the check after parsing:

```rust,ignore
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
```

These are Rust slice patterns. The empty slice has no root; `[root]` binds the only node; the last pattern identifies the second node in an overfull list. Notice that the error receives `second`, not merely a generic location. That makes the diagnostic refer to the extra element. The public entry point converts the `syn::Error` into compile-error tokens, so expansion failure becomes a compiler diagnostic.

Class checking adds another boundary. `ClassTarget::accepts` permits text-style utilities on `<text>`, but not box-layout utilities such as padding. The saved diagnostic in `crates/ui/tests/ui/rsx/fail/layout_utility_on_text.stderr` says that `p-4` is not valid on `<text>`. The `rsx_ui` test compiles these intentionally invalid programs with `trybuild` and compares their diagnostics with the saved expectations.

There are limits to what expansion can know. It can parse an expression and choose a utility, but it does not perform Rust's later type checking. The notation sometimes needs help with an overloaded prefix: tests use `text-{color:ink}` to distinguish a color from a length. A macro is a syntax translator, not a second Rust compiler.

## Components keep their types

Named components have the same underlying goal: convenient construction without erasing their Rust representation. `#[component]` accepts named-field and unit structs, preserves the struct, and generates a corresponding props type and a conversion. This exact output template comes from `crates/ui-macros/src/component.rs`; the `#` interpolations are `quote!` syntax, not runnable application code:

```rust,ignore
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

The component is not turned into an untyped property dictionary. Its props have the original field types. The generator uses `generics.split_for_impl()` to separate the generic parameters needed on an implementation from the arguments needed on a type and its `where` clause. A borrowed field therefore remains borrowed; macro convenience does not extend its lifetime. The attribute supplies construction plumbing, not the component's rendering behavior.

The alternative remains perfectly reasonable: write builder expressions and ordinary constructors. They are easier to step through mentally when computation dominates the layout. Markup is helpful when the visual hierarchy dominates. InkPaper even accepts Rust expression children, so a complicated branch need not become a complicated miniature language.

The price of the macro layer is compiler-side machinery, another syntax to learn, and errors that sometimes emerge from generated code. Its strongest justification is a readable screen declaration that still reaches the same typed API—not cleverness for its own sake.

## Try one translation

For a short, hardware-free experiment, follow `rsx_supports_rust_values_inside_classes` through `expand_div`, then `apply_classes` in `crates/ui-macros/src/rsx/class/mod.rs`. Predict which expressions survive into the output and which pieces of class syntax disappear. You should find the Rust values retained as expressions, with utility selection performed during expansion.

Optionally run `cargo test -p inkpaper-ui --test rsx rsx_supports_rust_values_inside_classes` from the repository root. The expected observation is a passing construction test, not a window or a rendered page. Then read the padding-on-text fixture and its `.stderr` neighbor; `cargo test -p inkpaper-ui --test rsx_ui` exercises the diagnostic contract without changing the saved files. The useful result is knowing exactly where this pleasant notation hands responsibility back to Rust.
