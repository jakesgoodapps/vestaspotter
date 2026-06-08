# Security Policy

## Supported versions

Only the current `main` branch is supported. There are no LTS branches.

## Reporting a vulnerability

**Please do not file public GitHub issues for security vulnerabilities.**

If you believe you've found a security issue (credential exposure, remote
code execution, sensitive data leak via the dashboard, etc.), email:

**jake@jakesgoodapps.com**

Include:
- A description of the issue and its impact
- Steps to reproduce, ideally with a minimal example
- Any suggested mitigation if you have one
- Whether you'd like to be credited in the fix announcement

## What to expect

- **Initial acknowledgment:** within ~3 business days
- **Triage + plan:** within ~7 business days
- **Fix + public disclosure:** depends on severity and complexity

This is a hobby/indie open-source project maintained by one person, so
response times reflect that. If something is actively being exploited,
flag it in the subject line ("URGENT — active exploit") and I'll prioritize.

## Scope

In scope:
- The VestaSpotter codebase itself
- Default configuration choices that create insecure defaults
- Documentation that encourages insecure setups

Out of scope:
- Vulnerabilities in upstream dependencies (please report those upstream;
  if it affects VestaSpotter materially, open a GitHub issue here so we
  can pin a fixed version)
- Issues that require physical access to the host running VestaSpotter
- Self-inflicted issues from running the dashboard exposed to the public
  internet without auth (see the README — that's documented as a thing
  users have to handle themselves)
