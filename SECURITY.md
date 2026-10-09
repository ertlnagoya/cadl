# Security Policy

## Supported versions

CADL is alpha software. Only the latest release receives fixes.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's
private reporting instead: on <https://github.com/ertlnagoya/cadl>, open the
**Security** tab and choose **Report a vulnerability**.

Include the affected version, a `.cadl` input or command that reproduces the
problem, and what an attacker could do with it.

## Scope notes

- `cadl` parses `.cadl` files with a safe YAML loader and does not execute
  their contents, but the code it *generates* (Python, Solidity, Rego, C#) is
  derived from the input. Review generated code before running or deploying
  it, as you would any code from a file you did not write.
- `cadl ai` sends your description to the Anthropic API using the key in
  `ANTHROPIC_API_KEY`.
