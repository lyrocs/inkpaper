# Summary

[Introduction](introduction.md)

# Part I — Starting from nothing

- [1. The project, the workspace and the toolbox](ch01-workspace.md)
- [2. Day one: a UI API made of types](ch02-first-commit.md)
- [3. Entities: state without `Rc<RefCell<T>>`](ch03-entities.md)
- [4. An allocator-free arena](ch04-slot-table.md)
- [5. Listeners: storing closures without `Box`](ch05-listeners.md)

# Part II — Making pixels

- [6. From elements to a frame tree](ch06-frame-tree.md)
- [7. Layout with integers](ch07-layout.md)
- [8. Painting through a trait](ch08-painting.md)
- [9. Drawing less: damage and invalidation](ch09-damage.md)
- [10. Interaction and typed events](ch10-events.md)

# Part III — Down to the metal

- [11. Talking to chips: an embedded-hal driver](ch11-i2c-driver.md)
- [12. E-paper displays: SPI, busy pins and timeouts](ch12-epaper.md)
- [13. Firmware: an async main loop](ch13-firmware.md)
- [14. Services: platforms, requests and replies](ch14-services.md)

# Part IV — Books

- [15. Opening an EPUB: zip, XML and CSS](ch15-epub.md)
- [16. Fonts and glyphs](ch16-fonts.md)
- [17. Shaping text: Unicode, bidi and Arabic](ch17-shaping.md)
- [18. The reader: pagination and positions](ch18-reader.md)

# Part V — Ergonomics and speed

- [19. Macros: from `macro_rules!` to `rsx!`](ch19-macros.md)
- [20. Measuring, and looking back](ch20-tracing-and-retrospective.md)

---

[Appendix A: Rust concept index](appendix-concepts.md)
[Appendix B: Following along in git](appendix-git.md)
