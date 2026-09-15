"""The host command convention — the SSH analogue of test_sql_conventions.

The load-bearing test here is test_injection_value_is_inert_after_quoting:
everything else is defence in depth behind it.
"""

import pytest

from ebsmcp.connectors.host import (
    HostCommand,
    HostCommandError,
    MockHostConnector,
    render_command,
    validate_host_command,
)


def cmd(*argv, **kwargs) -> HostCommand:
    return HostCommand(argv=tuple(argv), **kwargs)


# ── Layer 1: argv, not a shell string ─────────────────────────────────────────

def test_injection_value_is_inert_after_quoting():
    """The whole design in one test: a value carrying shell syntax becomes a
    single quoted argument, so nothing chains off it."""
    rendered = render_command(cmd("grep", "; rm -rf /", "/tmp/x.log"))
    assert "; rm -rf /" not in rendered.replace("'; rm -rf /'", "")
    assert rendered == "grep '; rm -rf /' /tmp/x.log"


def test_backticks_and_substitution_are_quoted_not_executed():
    rendered = render_command(cmd("cat", "$(whoami)", "`id`"))
    assert rendered == "cat '$(whoami)' '`id`'"


def test_plain_command_renders_unchanged():
    assert render_command(cmd("ps", "-ef")) == "ps -ef"


# ── Layer 2: leading-binary allowlist ─────────────────────────────────────────

@pytest.mark.parametrize("binary", ["ps", "df", "cat", "tail", "grep", "netstat", "find"])
def test_read_only_binaries_pass(binary):
    validate_host_command(cmd(binary))


@pytest.mark.parametrize(
    "binary", ["rm", "curl", "wget", "scp", "dd", "tee", "mv", "chmod", "kill", "sh", "bash"]
)
def test_mutating_or_egress_binaries_are_rejected(binary):
    with pytest.raises(HostCommandError, match="allowlist"):
        validate_host_command(cmd(binary, "-x"))


@pytest.mark.parametrize("binary", ["sed", "awk", "env", "mount", "uniq"])
def test_deliberately_excluded_binaries_are_rejected(binary):
    """Each of these has a write or exec path that an argument allowlist
    cannot reliably close — see the module docstring."""
    with pytest.raises(HostCommandError, match="allowlist"):
        validate_host_command(cmd(binary, "x"))


def test_absolute_path_to_allowlisted_binary_passes():
    validate_host_command(cmd("/usr/bin/ps", "-ef"))


def test_path_traversal_in_program_is_rejected():
    with pytest.raises(HostCommandError, match=r"\.\."):
        validate_host_command(cmd("/usr/bin/../../bin/ps"))


# ── EBS tools: restricted to read-only subcommands ────────────────────────────

def test_adop_status_is_permitted():
    validate_host_command(cmd("adop", "-status"))


def test_adop_apply_is_rejected():
    with pytest.raises(HostCommandError, match="read-only"):
        validate_host_command(cmd("adop", "phase=apply", "patches=12345"))


def test_adop_without_subcommand_is_rejected():
    with pytest.raises(HostCommandError, match="read-only"):
        validate_host_command(cmd("adop"))


@pytest.mark.parametrize("sub", ["lspatches", "lsinventory"])
def test_opatch_read_only_subcommands_permitted(sub):
    validate_host_command(cmd("{ORACLE_HOME}/OPatch/opatch", sub))


def test_opatch_apply_is_rejected():
    with pytest.raises(HostCommandError, match="read-only"):
        validate_host_command(cmd("{ORACLE_HOME}/OPatch/opatch", "apply"))


# ── Layer 3: per-binary forbidden flags ───────────────────────────────────────

@pytest.mark.parametrize("flag", ["-delete", "-exec", "-execdir", "-fprintf"])
def test_find_write_and_exec_flags_are_rejected(flag):
    with pytest.raises(HostCommandError, match="forbidden"):
        validate_host_command(cmd("find", "/u01", flag, "rm", "{}", ";"))


def test_plain_find_passes():
    validate_host_command(cmd("find", "/u01/log", "-name", "*.log", "-mtime", "-1"))


@pytest.mark.parametrize("arg", ["-o", "-o/tmp/pwned", "--output", "--output=/tmp/pwned"])
def test_sort_output_flag_is_rejected_in_every_attached_form(arg):
    """Exact-match alone would miss -o/tmp/pwned and --output=..., either of
    which writes a file."""
    with pytest.raises(HostCommandError, match="forbidden"):
        validate_host_command(cmd("sort", arg, "/tmp/x"))


def test_sort_without_output_passes():
    validate_host_command(cmd("sort", "-u", "/tmp/x"))


def test_tail_follow_is_rejected():
    """-f would hold the channel open until the timeout for no benefit."""
    with pytest.raises(HostCommandError, match="forbidden"):
        validate_host_command(cmd("tail", "-f", "/u01/log/alert.log"))


def test_tail_with_line_count_passes():
    validate_host_command(cmd("tail", "-n", "200", "/u01/log/alert.log"))


# ── Environment variable placeholders ─────────────────────────────────────────

def test_allowlisted_placeholder_renders_as_expandable_variable():
    """shlex.quote() leaves a shell-safe literal alone, so the suffix needs no
    quotes of its own — the variable still expands and nothing word-splits."""
    rendered = render_command(cmd("ls", "{ORACLE_HOME}/OPatch"))
    assert rendered == 'ls "$ORACLE_HOME"/OPatch'


def test_placeholder_suffix_is_quoted_when_it_needs_quoting():
    rendered = render_command(cmd("ls", "{ORACLE_HOME}/some dir"))
    assert rendered == 'ls "$ORACLE_HOME"\'/some dir\''


def test_placeholder_only_element_renders_bare():
    assert render_command(cmd("ls", "{APPL_TOP}")) == 'ls "$APPL_TOP"'


def test_unknown_environment_variable_is_rejected():
    with pytest.raises(HostCommandError, match="not in the allowlist"):
        validate_host_command(cmd("cat", "{HOME}/.ssh/id_rsa"))


def test_literal_dollar_is_quoted_not_expanded():
    assert render_command(cmd("grep", "$ORACLE_HOME", "/tmp/x")) == "grep '$ORACLE_HOME' /tmp/x"


# ── env_file, edition, timeout ────────────────────────────────────────────────

def test_env_file_prelude_is_rendered():
    rendered = render_command(
        cmd("adop", "-status", env_file="/u01/install/APPS/EBSapps.env")
    )
    assert rendered == (
        ". /u01/install/APPS/EBSapps.env run >/dev/null 2>&1 && adop -status"
    )


def test_env_file_edition_is_carried_through():
    rendered = render_command(cmd("ps", env_file="/u01/EBSapps.env", edition="patch"))
    assert rendered.startswith(". /u01/EBSapps.env patch ")


def test_relative_env_file_is_rejected():
    with pytest.raises(HostCommandError, match="absolute path"):
        validate_host_command(cmd("ps", env_file="EBSapps.env"))


def test_env_file_traversal_is_rejected():
    with pytest.raises(HostCommandError, match="absolute path"):
        validate_host_command(cmd("ps", env_file="/u01/../../etc/passwd"))


def test_invalid_edition_is_rejected():
    with pytest.raises(HostCommandError, match="edition"):
        validate_host_command(cmd("ps", env_file="/u01/EBSapps.env", edition="prod"))


@pytest.mark.parametrize("timeout", [0, -5, 301, 10_000])
def test_timeout_bounds_are_enforced(timeout):
    with pytest.raises(HostCommandError, match="timeout"):
        validate_host_command(cmd("ps", timeout=timeout))


def test_control_characters_are_rejected():
    with pytest.raises(HostCommandError, match="control character"):
        validate_host_command(cmd("grep", "pattern\nrm -rf /", "/tmp/x"))


def test_multiple_violations_are_all_reported():
    """A tool author fixing one problem should see the rest in the same run."""
    with pytest.raises(HostCommandError) as exc:
        validate_host_command(cmd("rm", "-rf", "/", edition="nope", timeout=0))
    message = str(exc.value)
    assert "allowlist" in message and "edition" in message and "timeout" in message


# ── run() enforces the convention on every connector ──────────────────────────

def test_mock_connector_validates_exactly_like_the_real_one():
    """The mock inherits run(), so a command production would refuse cannot
    be certified by a test that happens to use the mock."""
    with pytest.raises(HostCommandError):
        MockHostConnector().run(cmd("rm", "-rf", "/"))


def test_mock_connector_returns_canned_output_and_records_the_call():
    conn = MockHostConnector({"ps -ef": "applmgr  1234  FNDLIBR"})
    result = conn.run(cmd("ps", "-ef"))
    assert result.ok
    assert "FNDLIBR" in result.stdout
    assert result.command == "ps -ef"
    assert conn.calls == ["ps -ef"]


def test_result_ok_is_false_on_non_zero_exit():
    from ebsmcp.connectors.host import CommandResult

    assert not CommandResult(exit_status=1, stdout="", stderr="no", command="ps").ok
