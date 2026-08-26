---
name: auto-cut-profile-onboarding
description: Use only when the user explicitly asks to create, inspect, update, bind, or rebind a pointer profile library. Never invoke it automatically for an Auto-Cut Lite editing request.
---

# Explicit Profile Administration

The ordinary Auto-Cut Lite editing workflow does not use pointer profiles, project bindings,
target geometry, scale references, or onboarding gates. Read the
[Lite execution contract](../auto-cut-lite/references/lite-execution-contract.md).

Invoke this skill only for an explicit profile-library administration request. Such
administration may inspect or update target-local profile data, but it must not change the fixed
Lite editing behavior and must not become a prerequisite for inserting a supplied pointer.

For a normal Lite request with no supplied pointer file, retain the review label and report the
missing insertion. Do not open an onboarding handoff.
