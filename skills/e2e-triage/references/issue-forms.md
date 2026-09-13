# Follow the target repository's issue forms

Read this before drafting a new issue. Use the target repository's current
requirements, not a cached template from the tested product revision or a form
copied from this dev-tools repository.

## Discover the reporting route

Inspect the target's live issue chooser and default branch. Read
`.github/ISSUE_TEMPLATE/` (including `config.yml`), applicable legacy Markdown
templates, `CONTRIBUTING.md`, relevant README reporting instructions and
`SECURITY.md`. Follow organization-level community defaults and contact links
when the repository delegates reporting. A missing local template is not proof
that the project has no form; check the live chooser and inherited requirements.

Select the form for the observed problem: product bug, tooling bug, documentation,
or a support/private-reporting route when directed. Record the repository, source
URL, template path and revision, and inspection time in the local triage note.
If the current form cannot be retrieved, mark template verification incomplete
and retain a provisional draft; do not claim that it is ready to submit.

The chooser may allow blank issues while contribution instructions still
require forms. Respect those instructions. Only when no applicable template or
form exists, and the repository allows it, use a concise Markdown draft with
versions, expected/observed behavior, reproduction and relevant redacted evidence.

## Map evidence into the form

Read the entire selected form, including instructional Markdown and descriptions.
Make a local mapping of each field's ID, exact label, type, required status,
answer and supporting evidence. This mapping is a review aid, not public prose.

- Preserve the form's field order and labels in the copy-ready draft. Follow its
  title prefix and any instructions about what belongs in each section.
- For input and textarea fields, answer from the observed run. Defaults and
  placeholders are examples, not evidence. Keep requested log formatting and
  include only the useful sanitized excerpt. Include optional fields only when
  relevant and known.
- For dropdowns, use actual allowed option text and honor single/multiple
  selection. Do not invent a new option. If none fits, choose an existing Other
  option only when accurate and explain it where permitted; otherwise flag the
  mismatch and request the missing choice or reporting route.
- Honor field-level `validations.required` and each checkbox option's own
  `required` flag. Mark factual checkboxes only after performing the stated check.
  Human review, agreements or personal attestations require the corresponding
  human confirmation; never infer them from permission to file an issue.
- Do not fabricate a last-known-good release, reproduction, checked box or answer
  just to satisfy validation. Collect missing evidence within the authorized
  scope, or ask for the essential missing information. An unknown required answer
  keeps the draft incomplete unless the form expressly accepts unknown values.
- Separate hypotheses from observations inside the applicable fields. Do not
  state that a workaround or fix passed unless it was actually tested.

Verify every required field and checkbox before calling the draft complete.
Retain any unresolved requirement in the local note, outside the proposed public
body. Do not ask for submission approval until the required content is resolved.

## Submit only the reviewed result

Show the exact title, destination, form and public body before seeking permission
to submit. Existing authorization for those exact details remains sufficient.
Use the live form when the repository requires it, allowing GitHub to apply its
form metadata and starting labels. A CLI/API accepting a Markdown body is not
evidence that the form's validation ran; do not bypass a form-only requirement.

If a CLI/API route is permitted, reproduce the selected form's content and
required validations yourself, and preserve prescribed starting labels when the
tool and your permissions allow it. Do not invent maintainer-owned priority,
area or assignment decisions. If the chosen tool cannot honor required form
behavior, provide the copy-ready draft and plain form link for user submission
or use another authorized supported interface. Do not call a draft submitted.

Refresh the form before submission. If required fields or attestations changed,
resolve them and show material changes for approval. Verify the created issue's
body, destination and URL, then report the result. Comments on existing work do
not require a new-issue form, but still follow the target's contribution guidance.

## References

- [GitHub form schema](https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/syntax-for-githubs-form-schema)
- [Configuring issue templates and the chooser](https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/configuring-issue-templates-for-your-repository)

The schema was checked on 2026-09-12. The initial NanoClaw form mapping used
`.github/ISSUE_TEMPLATE/bug-report.yml` and `CONTRIBUTING.md` at
`74224f62a6c08418acccc727114ab02f92e403bf`. This is validation provenance,
not a template or a pinned requirement for future reports; fetch the current
target requirements each time.
