# Security policy

## Report a vulnerability

Report vulnerabilities in these skills or scripts through
[GitHub private vulnerability reporting](https://github.com/nanocoai/nanoclaw-oss-dev-tools/security/advisories/new).
Include the affected tool version or commit, the NanoClaw commit under test,
reproduction steps and the impact. Use minimal, sanitized examples.

Credentials, vault contents, private logs and exploitable details should stay out
of public issues and pull requests. If a credential was exposed, revoke or rotate
it through its provider.

For vulnerabilities in the NanoClaw runtime or setup code, follow
[NanoClaw's security policy](https://github.com/nanocoai/nanoclaw/blob/main/SECURITY.md).

## Versions

This project is under active development. Include the exact plugin version or
commit in a report; compatibility with NanoClaw is recorded in each skill's
`SKILL.md`. Fixes are developed on `main` and described in the
[changelog](CHANGELOG.md).
