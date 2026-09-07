# Thought Capture AI

**A private place for the thoughts that are too small, too early, or too unstructured to file away.**

Thought Capture AI lets you send a quick message or attachment when it occurs to you, then turns those fragments into a daily picture of what mattered. The goal is simple: spend your attention on living and thinking, not on deciding which notebook, folder, title, or tag a thought deserves.

It is self-hosted and designed for one person first. Your original messages and files remain your durable record; the system's summaries and connections are useful working views you can revisit, trace, and replace.

## The idea

Most personal knowledge tools ask for organization at exactly the wrong time: when you are in the middle of something. That friction turns fleeting insights, loose ends, observations, and reminders into things you meant to write down.

Thought Capture AI lowers that bar to a message. Later, it gathers what you captured into a daily digest and evolving notes about the people, projects, and ideas in your life. When it makes a connection, you can follow it back to the original thought instead of taking a summary on faith.

## What it feels like

1. Send a thought to your private Discord bot — a sentence, a link, or an attachment.
2. Receive confirmation that it has been saved.
3. At the end of the day, get a digest that helps you notice themes, open loops, and changes worth carrying forward.
4. Build a memory that is yours to inspect, correct, and keep.

Attachments are preserved as originals. They are not silently interpreted or rewritten.

## Why it is different

- **Capture first.** No folders, titles, or tagging ritual before a thought can be saved.
- **Your source material stays intact.** Original thoughts are never replaced by a polished version.
- **Connections stay accountable.** Generated notes point back to the captured thoughts that support them.
- **Private by default.** It is intended to run in your own environment rather than become another hosted profile of your life.
- **Useful without pretending to be magic.** The system is built to make your memory easier to work with, not to invent certainty about it.

## Where the project is today

The current release can securely capture allowlisted Discord messages and attachments, organize closed capture windows, create versioned digests and entity notes, and queue digest delivery. `/organize`, `/status`, `/search`, and `/ask` Discord commands cover the day-to-day loop. Search works in three modes — exact (date, time, entity, phrase, and full-text filters), semantic (via a self-hosted Khoj instance), and hybrid (both, fused) — once an operator has synced the index. Ask answers questions over your own captured memory, gated behind two explicit opt-ins so nothing reaches a model without deliberate setup. Backup and restore workflows and the eventual unified interface are the next steps.

## Learn more

- [The product and system design](docs/DESIGN.md) explains the commitments, privacy boundaries, and release plan.
- [Operating and inspection guide](docs/OPERATING.md) explains how to run the local system and understand what it has stored.
- [Development guide](docs/DEVELOPMENT.md) contains setup, checks, and the repository map for contributors.
- [Architecture decisions](docs/adr/) records the choices that protect the product's core promises.

Thought Capture AI is for building a more faithful personal memory — one small capture at a time.
