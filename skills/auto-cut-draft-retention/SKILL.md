---
name: auto-cut-draft-retention
description: Keep only the most recent JianYing draft projects for one project family while limiting fallback clutter. Use when the user asks to clean old draft projects, keep only the latest drafts, remove earlier fallback drafts, reduce too many generated drafts without lowering revision accuracy, or enforce a per-project draft retention rule.
---

# JianYing Draft Retention

Use this skill when the user wants old JianYing draft projects cleaned up and only a small recent set kept.

For this repository, the default retention rule is:

- keep only the **3 latest** drafts
- within those retained drafts, keep at most **1 fallback** draft such as `__fallback_...`
- for versioned drafts such as `*_v08_*`, sort by `vNN` version first
- when a family has no `vNN` versions, sort by modified time
- apply the rule to **one project family at a time**
- do not delete unrelated drafts from other projects

## Project scope

Treat a "project family" as the draft name group for the current job, such as:

- `C2597*`
- `A1024*`
- one user-specified project prefix or exact draft family

If the current thread is already clearly about one project, use that project family directly.

If the scope is ambiguous and cannot be inferred safely, ask which draft family should be cleaned.

## Safety rules

Before deletion:

1. Resolve the JianYing drafts root path.
2. List only drafts that match the target project family.
3. Sort versioned drafts by `vNN` descending; fall back to `LastWriteTime` descending.
4. Keep the first 3 entries after applying the fallback limit.
5. Delete only the remaining entries.

Never:

- delete outside the JianYing drafts root
- delete drafts from a different project family
- delete a non-fallback draft only because there are multiple fallback drafts
- delete anything when the matching family already satisfies both the total keep count and fallback limit

Before recursive deletion, verify each resolved path still starts with the resolved drafts root path.

## Workflow

1. Identify the target project family.
2. Enumerate matching draft directories under the JianYing drafts root.
3. Sort by version descending when possible, otherwise by last modified time descending.
4. Compute:
   - `keep = first 3`
   - `fallback_keep = latest 1 fallback at most`
   - `delete = rest`
5. Delete only the `delete` set.
6. Re-list the project family and confirm only 3 remain, with no more than 1 fallback.

## Reporting

When finished, report:

- the project family cleaned
- the 3 drafts that were kept
- which older drafts were deleted

## Default rule for this repo

Unless the user explicitly asks for a different count, use:

- **retain 3 latest drafts per project family**
- **retain at most 1 fallback draft per project family**

## Automatic trigger

The repository enforces this rule from both `JyProject.save()` and the lower-level saved-draft JSON write path, so normal project-generation scripts and CLI patch commands do not need a separate cleanup call.

Revision jobs defer automatic retention until after editable-draft validation and final acceptance pass. If a fallback revision draft fails before acceptance, clean only that failed draft and keep the previous accepted draft/fallback available for rollback.
