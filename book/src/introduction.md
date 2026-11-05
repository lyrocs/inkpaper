# A page worth turning

You tap the edge of a screen. The next paragraph appears, your place in the book
is safe, and the device goes quiet again. An e-reader succeeds when you stop
noticing the machine.

Making that experience feel simple is an excellent way to learn Rust. Someone
must decide who owns the open book, how long a borrowed line of text remains
valid, what happens when memory fills up, and how a slow display can coexist with
responsive input. These are not language exercises dressed up as a product. They
are product questions that lead naturally to ownership, traits, enums, lifetimes,
and asynchronous work.

**InkPaper** is experimental e-reader software for the XTEINK X4 PRO. Its Rust
workspace contains an application, a UI framework, document and typography code,
device drivers, firmware, and a desktop simulator. We will study that code as a
working system, arranging the ideas in an order that makes them easier to learn.
This is a guided construction of understanding, not a promise that typing the
excerpts in sequence will recreate the repository.

## Begin with the reading experience

Suppose the first requirement is simply “show a page of text.” A string is not a
picture. The software must choose glyphs from a font, arrange them into lines,
place those lines on a page, and turn their shapes into pixel values. On an
e-paper device, even finished pixels are not the end of the job: a controller
must drive the physical panel to make the new image visible.

That journey gives us a useful map. We begin at the application boundary, where
we can describe a screen and try it on a laptop. We then follow that description
toward pixels and hardware. With a place to display a page, we work back toward
the book itself: opening an EPUB, understanding typography, and keeping a stable
reading position when the layout changes. Finally, we look at tools that make
the code easier to write and its performance easier to understand.

The order is deliberate. You do not need to understand an SPI transaction before
you can experiment with a button. You do need to know what painting means before
you can reason about avoiding unnecessary repainting. The chapters introduce
those dependencies as they become useful.

## One idea at a time

The book assumes you can read a Rust function, a `struct`, an `enum`, and a simple
`match`. If those are unfamiliar, the opening chapters of
[*The Rust Programming Language*](https://doc.rust-lang.org/book/) make a good
companion. Embedded programming, advanced lifetimes, `unsafe`, and async are not
prerequisites; we build their intuition around concrete problems here.

Aim for one chapter in a short sitting—roughly ten to fifteen minutes of reading,
with experiments as a separate activity. Each session follows one problem far
enough to explain the mechanism and its tradeoffs. You can stop when the central
idea clicks rather than trying to memorise every type. The storage and shaping
chapters are denser; taking them over two sittings loses nothing.

Code excerpts point into the current repository. An excerpt shows the part worth
studying, not necessarily a standalone program; shortened code and explanatory
sketches are labelled. Run the repository's examples and the commands given in
the exercises rather than pasting isolated method bodies into `main.rs`.
[Appendix A](appendix-concepts.md) helps you revisit a Rust concept, and
[Appendix B](appendix-reading-lab.md) offers a practical way to explore without
getting lost in the source.

## Keep a working reader nearby

From the repository root, with Rust and the simulator's
[SDL2 prerequisites](https://github.com/embedded-graphics/simulator#setup)
installed, run:

```bash
cargo run -p inkpaper-simulator
```

Use the file browser to enter `/Fixtures` and open an EPUB such as
`typography.epub`. These entries correspond to files in the repository's
`fixtures/` directory. Other folders contain demonstration listings, so start
with the fixtures when you want an actual book. The left and right arrow keys
send previous and next actions, a left mouse click acts as touch input, and `H`
returns home.

You now have something concrete to question. What determines the gap between two
lines? Why should a battery update leave the page alone? What survives when the
font size changes? Keep one of those questions in mind as you read.

The simulator exercises application behavior without a board, but it does not
reproduce e-paper waveforms, electrical faults, power consumption, or device
timing. Hardware-specific chapters make that boundary explicit. No chapter
requires you to flash a device. InkPaper remains experimental, and a supported
fixture is evidence for a particular behavior—not a claim that every EPUB or
writing system is fully supported.

Our first task is modest: give ourselves a place to build and observe a screen.
The Rust concepts will have a job to do from there.
