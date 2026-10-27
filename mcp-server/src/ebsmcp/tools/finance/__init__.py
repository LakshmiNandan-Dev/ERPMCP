"""Finance toolset family — accounts payable and accounts receivable.

This is where GL/AP/AR interface monitoring belongs, not tools/dba/:
those tables (GL_INTERFACE, AP_INVOICES_INTERFACE, RA_INTERFACE_LINES_ALL)
are functional business data, and a functional user's access is
Org-ID-scoped (target_system="ebs"), not the ebs_dba persona's
all-or-nothing grant — a DBA-persona tool exposing per-invoice/per-line
detail would bypass that scoping entirely. A prior tools/dba/interfaces.py
(gl_interface_backlog) got this wrong and was removed once the
categorization issue was caught, rather than widened further.

Financial records are sensitive data. Every tool here MUST resolve through
tools.sensitive.resolve_sensitive_call(domain="finance", ...), never
resolve_scoped_call or run_scoped_list directly:

  * it runs only under a mapping in the finance domain — a user onboarded
    for SCM or HCM is not thereby entitled to the ledger;
  * it applies Org scope as every functional tool does;
  * bank-account and tax-identifier columns go through
    SensitiveScope.select_fields, so they are withheld unless that field
    class was explicitly granted to the mapping;
  * the call's released and withheld field classes are written to the
    audit record.

tests/test_sensitive_security.py fails the build if a module in this
package calls the plain resolvers. Ledger / GL data-access-set scoping is not
implemented yet — until it is, finance tools are scoped by operating unit
only, which is wider than a Ledger-restricted EBS user's own view.
"""

from .accounts_payable import ACCOUNTS_PAYABLE_TOOLSET
from .accounts_receivable import ACCOUNTS_RECEIVABLE_TOOLSET
from .diagnose_invoice import DIAGNOSE_INVOICE_TOOLSET

__all__ = [
    "ACCOUNTS_PAYABLE_TOOLSET",
    "ACCOUNTS_RECEIVABLE_TOOLSET",
    "DIAGNOSE_INVOICE_TOOLSET",
]
