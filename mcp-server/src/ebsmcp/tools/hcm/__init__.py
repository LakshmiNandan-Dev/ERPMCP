"""HCM (Human Capital Management) toolset family — no toolsets registered
yet.

HR data is personal data. Every tool here MUST resolve through
tools.sensitive.resolve_sensitive_call(domain="hcm", ...), never
resolve_scoped_call or run_scoped_list directly. That gives, in one place:

  * domain pinning — only an hcm mapping runs these tools;
  * row scope — scope.person_predicate("<alias>.person_id") restricts a
    query to the caller's own record and reporting line (or everyone, only
    where an admin granted hr_scope="all"). It is built from the resolved
    identity, never from a tool argument;
  * field scope — salary, national ID, date of birth, bank account, tax ID,
    medical and home-contact columns are declared as FieldSpec(...,
    field_class=...) and rendered through scope.select_fields: withheld
    unless explicitly granted, visible on the caller's own record only, and
    reported back in the response so a missing value is not read as zero;
  * audit — released/withheld field classes land in the record's
    `_security` block, and arguments that are themselves personal (a name
    being searched) go through audit_safe.

tests/test_sensitive_security.py fails the build if a module in this
package calls the plain resolvers. A tool should list only what it needs:
the safest sensitive column is the one it never selects.

Each HCM tool category gets its own module here with its own ToolSet export,
same pattern as tools/scm; see tools/sensitive.py for the framework.
"""

__all__: list[str] = []
