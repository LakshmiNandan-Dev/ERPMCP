"""EBS application-tier host connector, plus the standing command convention
for every tool that runs something over SSH.

The SQL side of this package rests on one idea: a tool never composes a
query from caller input — it carries a reviewed constant, and
validate_sql_conventions() enforces the house rules at call time rather
than leaving them as something to remember. This module is the same idea
for the app tier, where the stakes are higher: a bad SELECT returns a
wrong number, a bad shell command deletes a filesystem.

So there is deliberately NO way to hand this connector a shell string.
A tool builds a HostCommand out of an argv tuple; the connector quotes
every element itself and renders the final command line. Injection is
structurally impossible rather than filtered for — the caller never
controls shell syntax, only the values inside already-quoted words.

Three layers, each independently sufficient to stop the obvious attack:

  1. **argv, not a string.** shlex.quote() is applied to every element, so
     a value containing `; rm -rf /` is one (harmless) argument, not two
     commands. This is the layer that actually matters.
  2. **Leading-binary allowlist.** argv[0] must name a binary that cannot
     mutate state whatever its arguments (_READ_ONLY_BINARIES), or one of
     the two EBS tools whose read-only subcommands are explicitly named
     (_RESTRICTED_FIRST_ARG). `sed` and `awk` are deliberately absent:
     both can write files and execute (`sed -i`, `s///w file`, `s///e`,
     awk's `print > "file"`), and grep/cut/tr/sort cover the parsing this
     catalog actually needs.
  3. **Per-binary forbidden flags.** The few allowlisted binaries with a
     write or exec escape hatch have it closed by name — `find -delete`,
     `find -exec`, `sort -o`, and `tail -f` (which would otherwise hang
     the channel until the timeout).

Read-only is weaker here than on the database side, and that is worth
stating plainly rather than glossing: Oracle refuses a write from an
account granted only SELECT, but there is no OS equivalent — a shell user
can do whatever their account permits. The allowlist above is a real
control, not a fig leaf, but the deployment control is the one that
matters: point this at a dedicated low-privilege account that owns
nothing and can sudo to nothing.

Environment variables are the one place a literal `$` survives quoting.
An argv element may contain `{ORACLE_HOME}`-style placeholders, checked
against _ALLOWED_ENV_VARS and rendered as `"$ORACLE_HOME"` — expanded by
the remote shell, still immune to word-splitting. Everything around the
placeholder is quoted as usual.
"""

from __future__ import annotations

import re
import shlex
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

# Cap what a single command can return. The rows a SQL tool returns are
# capped for the same reason (see tools/registry.py): these payloads are
# read by a model with a finite context budget, and an unbounded `cat` of
# a multi-gigabyte alert log helps nobody.
MAX_OUTPUT_BYTES = 256 * 1024

DEFAULT_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 300

# Binaries that cannot mutate state whatever arguments they are given.
# Notable exclusions, all deliberate: `env` (sets variables and RUNS a
# command), `mount`, `tee`, `dd`, `uniq` (writes its second positional
# argument), `sed` and `awk` (see the module docstring), and anything that
# performs network egress (curl, wget, scp, ping).
_READ_ONLY_BINARIES = frozenset({
    # process and system state
    "ps", "top", "uptime", "free", "vmstat", "iostat", "mpstat", "nproc",
    "lscpu", "uname", "hostname", "id", "who", "w", "date", "printenv",
    # filesystem inspection
    "df", "du", "ls", "stat", "readlink", "basename", "dirname", "lsblk", "file",
    # file contents
    "cat", "head", "tail", "grep", "egrep", "fgrep", "zgrep", "wc", "cut",
    "tr", "sort", "md5sum", "sha256sum",
    # network state (inspection only, no egress)
    "netstat", "ss", "lsof",
    # conditionally safe — see _FORBIDDEN_ARGS
    "find",
})

# EBS tools that are useful read-only but destructive in general (`adop
# phase=apply`, `opatch apply`). The binary alone is never enough: argv[1]
# must name one of the read-only subcommands.
_RESTRICTED_FIRST_ARG = {
    "adop": frozenset({"-status"}),
    "opatch": frozenset({"lspatches", "lsinventory"}),
}

# Write/exec escape hatches on otherwise-safe binaries.
_FORBIDDEN_ARGS = {
    "find": frozenset({
        "-exec", "-execdir", "-delete", "-ok", "-okdir",
        "-fls", "-fprint", "-fprintf", "-fprint0",
    }),
    "sort": frozenset({"-o", "--output"}),
    "tail": frozenset({"-f", "--follow"}),
}

# EBS environment variables a command may reference as {NAME}.
_ALLOWED_ENV_VARS = frozenset({
    "ORACLE_HOME", "APPL_TOP", "INST_TOP", "APPLCSF", "APPLLOG", "APPLOUT",
    "ADMIN_SCRIPTS_HOME", "LOG_HOME", "FND_TOP", "AD_TOP", "CONTEXT_FILE",
    "CONTEXT_NAME", "TWO_TASK", "ORACLE_SID", "EBS_DOMAIN_HOME",
    "RUN_BASE", "PATCH_BASE",
})

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SAFE_PATH_RE = re.compile(r"^/[\w./\-]+$")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

_VALID_EDITIONS = frozenset({"run", "patch"})


class HostCommandError(ValueError):
    """Raised when a HostCommand violates the command convention. Fails the
    call outright rather than warning, for the same reason
    UnqualifiedSQLError does: the convention is never optional.
    """


@dataclass(frozen=True)
class HostCommand:
    """One command to run on an EBS application-tier host.

    argv is the program and its arguments as separate elements — never a
    shell string. The connector quotes each element, so any value inside
    one is inert as far as the shell is concerned.

    env_file, when given, is an EBSapps.env sourced before the command so
    $ORACLE_HOME and friends resolve; edition picks the run or patch
    filesystem, which on R12.2 is the difference between reading the live
    stack and reading the one being patched.
    """

    argv: tuple[str, ...]
    env_file: str | None = None
    edition: str = "run"
    timeout: int = DEFAULT_TIMEOUT_SECONDS


@dataclass(frozen=True)
class CommandResult:
    """What a host command returned. `command` is the rendered command line
    exactly as executed — it goes into the audit record, so what was run is
    never inferred from the tool name.
    """

    exit_status: int
    stdout: str
    stderr: str
    command: str
    truncated: bool = False
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.exit_status == 0


def _reject_control_chars(value: str, what: str) -> None:
    if _CONTROL_CHARS_RE.search(value):
        raise HostCommandError(
            f"{what} contains a control character (newline, NUL or similar) — "
            "a command line is a single line by construction."
        )


def _placeholders(value: str) -> set[str]:
    return {m.group(1) for m in _PLACEHOLDER_RE.finditer(value)}


def _matches_forbidden(arg: str, flag: str) -> bool:
    """True if `arg` is `flag`, or an attached-value form of it.

    Covers the three shapes a flag's value can arrive in: `-o out`,
    `-oout`, and `--output=out`. Without the prefix checks, `sort -o/tmp/x`
    would sail past an exact-match test and write a file.
    """
    if arg == flag:
        return True
    if arg.startswith(f"{flag}="):
        return True
    return len(flag) == 2 and flag.startswith("-") and arg.startswith(flag)


def validate_host_command(command: HostCommand) -> None:
    """Enforce the command convention. Raises HostCommandError on any
    violation, having collected every one it can find rather than stopping
    at the first — a tool author fixing one problem should see the rest.
    """
    violations: list[str] = []

    if not command.argv:
        raise HostCommandError("argv is empty — there is no command to run.")

    for element in command.argv:
        if not isinstance(element, str):
            raise HostCommandError(f"argv element {element!r} is not a string.")
        _reject_control_chars(element, f"argv element {element!r}")
        for name in _placeholders(element):
            if name not in _ALLOWED_ENV_VARS:
                violations.append(
                    f"argv element {element!r} references environment variable "
                    f"{name!r}, which is not in the allowlist "
                    f"({', '.join(sorted(_ALLOWED_ENV_VARS))})."
                )

    program = command.argv[0]
    if ".." in program:
        violations.append(f"program path {program!r} contains '..' — no path traversal.")

    binary = program.rsplit("/", 1)[-1].lower()
    rest = command.argv[1:]

    if binary in _RESTRICTED_FIRST_ARG:
        allowed = _RESTRICTED_FIRST_ARG[binary]
        if not rest or rest[0] not in allowed:
            violations.append(
                f"{binary!r} is only permitted with one of its read-only "
                f"subcommands ({', '.join(sorted(allowed))}); got "
                f"{rest[0]!r} — {binary} can otherwise modify the system."
                if rest else
                f"{binary!r} requires one of its read-only subcommands "
                f"({', '.join(sorted(allowed))})."
            )
    elif binary not in _READ_ONLY_BINARIES:
        violations.append(
            f"program {binary!r} is not in the read-only binary allowlist. "
            "Host tools run reviewed, non-mutating commands only."
        )

    for flag in _FORBIDDEN_ARGS.get(binary, ()):  # type: ignore[arg-type]
        for arg in rest:
            if _matches_forbidden(arg, flag):
                violations.append(
                    f"{binary} argument {arg!r} is forbidden ({flag} can write "
                    "files or execute other programs)."
                )

    if command.env_file is not None:
        _reject_control_chars(command.env_file, "env_file")
        if ".." in command.env_file or not _SAFE_PATH_RE.match(command.env_file):
            violations.append(
                f"env_file {command.env_file!r} must be an absolute path with no "
                "'..' and no shell-significant characters."
            )

    if command.edition not in _VALID_EDITIONS:
        violations.append(
            f"edition {command.edition!r} must be one of {sorted(_VALID_EDITIONS)}."
        )

    if not isinstance(command.timeout, int) or not (0 < command.timeout <= MAX_TIMEOUT_SECONDS):
        violations.append(
            f"timeout {command.timeout!r} must be a positive integer no greater "
            f"than {MAX_TIMEOUT_SECONDS} seconds."
        )

    if violations:
        raise HostCommandError(
            "command violates the host command convention:\n  - " + "\n  - ".join(violations)
        )


def _render_element(element: str) -> str:
    """Quote one argv element, leaving allowlisted {VAR} placeholders as
    shell-expandable `"$VAR"`. Everything else is shlex.quote()d, so a
    literal `$`, backtick or semicolon inside a value stays a literal.
    """
    parts: list[str] = []
    pos = 0
    for match in _PLACEHOLDER_RE.finditer(element):
        literal = element[pos:match.start()]
        if literal:
            parts.append(shlex.quote(literal))
        parts.append(f'"${match.group(1)}"')
        pos = match.end()
    tail = element[pos:]
    if tail:
        parts.append(shlex.quote(tail))
    return "".join(parts) if parts else shlex.quote(element)


def render_command(command: HostCommand) -> str:
    """Build the command line actually sent to the remote shell.

    Call validate_host_command() first — run() does. Rendering an
    unvalidated command is not a supported path; the quoting here is the
    last of the three layers, not the only one.
    """
    rendered = " ".join(_render_element(element) for element in command.argv)
    if command.env_file:
        prelude = (
            f". {shlex.quote(command.env_file)} {shlex.quote(command.edition)} "
            ">/dev/null 2>&1"
        )
        return f"{prelude} && {rendered}"
    return rendered


class HostConnector(ABC):
    """Interface every app-tier host connector implements.

    Same shape as EBSConnector: subclasses implement execute(), callers
    use run(), and run() is where the convention is enforced — so the
    check cannot be skipped by using a different connector, and the mock
    validates exactly as strictly as the real thing.
    """

    @abstractmethod
    def execute(self, rendered: str, *, timeout: int) -> CommandResult:
        """Run an already-validated, already-rendered command line.
        Callers should never call this directly — call run().
        """

    def run(self, command: HostCommand) -> CommandResult:
        validate_host_command(command)
        return self.execute(render_command(command), timeout=command.timeout)


class SSHHostConnector(HostConnector):
    """A real EBS application-tier host over SSH.

    Host-key policy is RejectPolicy, not paramiko's permissive default and
    not the WarningPolicy used elsewhere in this stack: an unknown host key
    fails the call. Point known_hosts at a provisioned file. Accepting
    unknown keys would mean a machine-in-the-middle could serve this
    connector's credentials a shell, which is precisely what the audit
    trail could not detect after the fact.
    """

    def __init__(
        self,
        hostname: str,
        username: str,
        *,
        password: str | None = None,
        key_filename: str | None = None,
        port: int = 22,
        known_hosts: str | None = None,
        connect_timeout: int = 15,
    ) -> None:
        if not password and not key_filename:
            raise ValueError(
                "SSHHostConnector needs either a password or a key_filename — "
                "refusing to fall back to agent/default-key discovery, which "
                "would make the effective identity depend on the process "
                "environment rather than on configuration."
            )
        self._hostname = hostname
        self._username = username
        self._password = password
        self._key_filename = key_filename
        self._port = port
        self._known_hosts = known_hosts
        self._connect_timeout = connect_timeout

    def execute(self, rendered: str, *, timeout: int) -> CommandResult:
        import paramiko

        started = time.monotonic()
        client = paramiko.SSHClient()
        try:
            if self._known_hosts:
                client.load_host_keys(self._known_hosts)
            else:
                client.load_system_host_keys()
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            client.connect(
                hostname=self._hostname,
                port=self._port,
                username=self._username,
                password=self._password,
                key_filename=self._key_filename,
                timeout=self._connect_timeout,
                allow_agent=False,
                look_for_keys=False,
            )
            _stdin, stdout, stderr = client.exec_command(rendered, timeout=timeout)
            # Read one byte past the cap so truncation is detected rather
            # than silently presented as the whole file.
            out_bytes = stdout.read(MAX_OUTPUT_BYTES + 1)
            err_bytes = stderr.read(MAX_OUTPUT_BYTES + 1)
            exit_status = stdout.channel.recv_exit_status()
        finally:
            client.close()

        truncated = len(out_bytes) > MAX_OUTPUT_BYTES or len(err_bytes) > MAX_OUTPUT_BYTES
        return CommandResult(
            exit_status=exit_status,
            stdout=out_bytes[:MAX_OUTPUT_BYTES].decode("utf-8", "replace"),
            stderr=err_bytes[:MAX_OUTPUT_BYTES].decode("utf-8", "replace"),
            command=rendered,
            truncated=truncated,
            duration_ms=int((time.monotonic() - started) * 1000),
        )


class MockHostConnector(HostConnector):
    """Canned output keyed by a substring of the rendered command, so the
    full pipeline — identity, entitlement, audit — is exercisable without
    a real host. Mirrors MockEBSConnector, including the substring match.

    Because it extends HostConnector, every command a test sends through
    it is validated by the same rules as production. A command the real
    connector would refuse is refused here too, which is the point: tests
    cannot accidentally certify a command that could never run.
    """

    def __init__(self, canned_responses: dict[str, str] | None = None) -> None:
        self._canned = canned_responses or {}
        self.calls: list[str] = []

    def execute(self, rendered: str, *, timeout: int) -> CommandResult:
        self.calls.append(rendered)
        for key, output in self._canned.items():
            if key.lower() in rendered.lower():
                return CommandResult(
                    exit_status=0, stdout=output, stderr="", command=rendered
                )
        return CommandResult(exit_status=0, stdout="", stderr="", command=rendered)
