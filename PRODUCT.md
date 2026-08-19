# Product

<!-- impeccable:product-schema 1 -->

## Platform

desktop (Windows, PyQt6/Qt widgets + QSS styling -- not web/iOS/Android/adaptive; outside Impeccable's platform enum, recorded this way by explicit user decision so browser-based tooling in this skill, e.g. live mode and screenshot-based critique/audit, is understood not to apply here)

## Users

The developer themselves: a computer science student (current courses include CS223, CS311, CS210, CS285, cs102) using the app as their own personal, daily study-management tool. Single-user by design, not built for distribution to or use by anyone else.

## Product Purpose

Study Warden is a personal, locally-run study-accountability system combining a Pomodoro-style focus timer, webcam-based distraction monitoring, an AI chat agent with full read/write access to the user's real Notion workspace, and an AI Study Planner that generates day-by-day study/work calendars -- culminating in "Monitor Lockdown," a full-screen enforced study environment. It exists to solve the user's own difficulty staying on-task and organized across several concurrent courses, deadlines, and study materials. Success means: distraction genuinely caught and interrupted during a session, calendars that realistically account for competing deadlines instead of cramming, and Notion staying in sync without manual data entry.

## Positioning

What a plain Pomodoro timer, a plain Notion integration, or a plain to-do app couldn't truthfully copy:

- Webcam-enforced accountability with real physical consequences (pose-challenge / push-up penalties) for looking away or using a phone during a session.
- A local LLM (via LM Studio) with full non-destructive tool access to the user's actual Notion schedule and to-do data -- the model itself decides what to read or write; it isn't a scripted integration.
- A true greedy, earliest-deadline-first cross-course scheduler: a day has exactly one study slot and one work slot, competing deadlines are prioritized by urgency regardless of the order plans are generated in, and sessions spread across the whole runway instead of cramming into the final days.
- "Monitor Lockdown," which unifies the timer, live webcam feed, and today's actual study material (auto-extracted topic summary + the full source PDF) into one enforced full-screen view, with OS-level "soft" enforcement (minimizes other windows, whitelists only Discord/Spotify) -- deliberately stopping short of a keyboard-hook hard-lock for safety.

## Operating Context

- Runs as a local Windows desktop app (`main.py`), launched by the user directly.
- Deeply integrated with the user's real, live Notion workspace: a schedule table (courses as columns; Quiz-1..5, assignments, Project, Major-1, Major-2, Final as rows), a To-Do List page, and a "To Do Date" calendar database -- all real production data the user actively relies on, not a demo/sandbox.
- Depends on LM Studio running locally with one or more models loaded: a tool-calling-capable model for the Notion chat agent, optionally a vision-capable model for slide/material understanding. No cloud AI dependency.
- Study material is typically PDF slide decks or notes the user uploads themselves.
- Used during real study sessions, including a "Monitor Lockdown" full-screen mode meant to run for the duration of a focus block with the webcam active.
- Expects Discord and Spotify as the user's normal companion apps during a lockdown session -- launched and tiled automatically on a second monitor when one is present.

## Capabilities and Constraints

- Windows-only currently (uses `ctypes`/Win32 APIs for window management, exit-key polling, and multi-monitor placement).
- No cloud AI dependency: every LLM call goes to a locally-hosted LM Studio server (OpenAI-compatible API), never an external API.
- Notion integration is deliberately non-destructive: no delete/archive tool exists anywhere in the agent's toolset, by explicit design decision -- a wrong tool call must always be recoverable by hand.
- Credentials (Notion integration token, table/page/database IDs) resolve from an environment variable, then a value saved via the Settings dialog, then an empty fallback -- no real credential is ever hardcoded in source, since this project is public. (An earlier private-only version of this file hardcoded a real token as the default; that's no longer true as of the project going public.)
- "Monitor Lockdown" is explicitly soft enforcement only: it minimizes other windows (re-enforced every couple of seconds) and stays on top, but does not use a low-level keyboard hook to block Alt+Tab or the Windows key -- a deliberate safety decision, since a buggy hard-lock could leave the user unable to task-switch at all. Ctrl+Shift+Alt is the always-available safe exit, detected by key-state polling rather than a hook.
- Two independent LLM "roles" exist and can use different loaded models at once: a tool-calling chat model (the Notion-editing conversational agent) and a vision-capable model (slide-image reading, topic extraction). Which loaded LM Studio model serves which role is pinnable in Settings, since a single "whichever model happens to be listed first" heuristic previously caused the chat agent to silently stop calling tools.
- Terminology: "the schedule table" is the Notion table block with courses as columns and exam/assignment fields as rows; "the To-Do List" is a separate Notion page; "the calendar" / "To Do Date" is a separate Notion database the Study Planner writes generated sessions into; "a plan" is one generated day-by-day study or work schedule for a course + deadline, reviewed before being committed to the calendar; "lockdown" is the full-screen enforced study view.

## Brand Commitments

- App name: "Study Warden."
- App icon: a custom user-provided image (`ui/App Icon/Pepopolice.jpg`, a Pepe-the-frog-as-police-officer graphic) -- an intentionally informal, personal choice, not a corporate mark.
- Default visual identity: a retro terminal look (green on black, monospace type intended to be Roboto Mono, falling back to Consolas), alongside several palette-driven alternate themes (Molten Fire, Ocean Twilight, Forest Sage, Blossom, Royal Violet) and a plain light theme, switchable live from Settings.
- Custom cursor: a Wii-style pointer graphic (`ui/cursor/wii-pointer.cur`).

## Evidence on Hand

The real Notion workspace (schedule table, to-do list, calendar database) is live production data the user actively depends on, not sample or demo content -- future design or content work must not fabricate example data that could be confused with it. No testimonials, press, benchmarks, or pricing exist or apply: this is a single-user personal tool, not a distributed or commercial product.

## Product Principles

1. Local-first and private -- no cloud AI calls; all inference runs through a locally-hosted LM Studio model.
2. Non-destructive by default -- every Notion-writing capability is additive or overwrite-only; nothing can silently delete the user's real data.
3. Enforcement without risk -- accountability features (webcam monitoring, lockdown) are built to be hard to casually circumvent, but never irreversible or capable of locking the user out of their own machine.
4. Built for one real user's real workflow -- courses, deadlines, and material are the user's actual current coursework, not a generalized or configurable audience.
5. Convenience over hardening for everything EXCEPT credentials -- runtime behavior favors sensible defaults over setup friction wherever nothing sensitive is at stake, but real credentials specifically are never hardcoded, since the project is public.
